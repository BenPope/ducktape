# Copyright 2014 Confluent Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .service import Service
from .core import HadoopV1Service
import threading


class PerformanceService(Service):
    def start(self):
        super(PerformanceService, self).start()
        self.worker_threads = []
        self.results = [None] * len(self.nodes)
        self.stats = [[] for x in range(len(self.nodes))]
        for idx,node in enumerate(self.nodes,1):
            self.logger.info("Running %s node %d on %s", self.__class__.__name__, idx, node.account.hostname)
            worker = threading.Thread(
                name=self.__class__.__name__ + "-worker-" + str(idx),
                target=self._worker,
                args=(idx,node)
            )
            worker.daemon = True
            worker.start()
            self.worker_threads.append(worker)

    def wait(self):
        super(PerformanceService, self).wait()
        for idx,worker in enumerate(self.worker_threads,1):
            self.logger.debug("Waiting for %s worker %d to finish", self.__class__.__name__, idx)
            worker.join()
        self.worker_threads = None

    def stop(self):
        super(PerformanceService, self).stop()
        assert self.worker_threads is None, "%s.stop should only be called after wait" % self.__class__.__name__
        for idx,node in enumerate(self.nodes,1):
            self.logger.debug("Stopping %s node %d on %s", self.__class__.__name__, idx, node.account.hostname)
            node.free()


class ProducerPerformanceService(PerformanceService):
    def __init__(self, cluster, num_nodes, kafka, topic, num_records, record_size, throughput, settings={}, intermediate_stats=False):
        super(ProducerPerformanceService, self).__init__(cluster, num_nodes)
        self.kafka = kafka
        self.args = {
            'topic': topic,
            'num_records': num_records,
            'record_size': record_size,
            'throughput': throughput
        }
        self.settings = settings
        self.intermediate_stats = intermediate_stats

    def _worker(self, idx, node):
        args = self.args.copy()
        args.update({'bootstrap_servers': self.kafka.bootstrap_servers()})
        cmd = "/opt/kafka/bin/kafka-run-class.sh org.apache.kafka.clients.tools.ProducerPerformance "\
              "%(topic)s %(num_records)d %(record_size)d %(throughput)d bootstrap.servers=%(bootstrap_servers)s" % args

        for key,value in self.settings.items():
            cmd += " %s=%s" % (str(key), str(value))
        self.logger.debug("Producer performance %d command: %s", idx, cmd)
        def parse_stats(line):
            parts = line.split(',')
            return {
                'records': int(parts[0].split()[0]),
                'records_per_sec': float(parts[1].split()[0]),
                'mbps': float(parts[1].split('(')[1].split()[0]),
                'latency_avg_ms': float(parts[2].split()[0]),
                'latency_max_ms': float(parts[3].split()[0]),
                'latency_50th_ms': float(parts[4].split()[0]),
                'latency_95th_ms': float(parts[5].split()[0]),
                'latency_99th_ms': float(parts[6].split()[0]),
                'latency_999th_ms': float(parts[7].split()[0]),
            }
        last = None
        for line in node.account.ssh_capture(cmd):
            self.logger.debug("Producer performance %d: %s", idx, line.strip())
            if self.intermediate_stats:
                try:
                    self.stats[idx-1].append(parse_stats(line))
                except:
                    # Sometimes there are extraneous log messages
                    pass
            last = line
        try:
            self.results[idx-1] = parse_stats(last)
        except:
            self.logger.error("Bad last line: %s", last)


class RestProducerPerformanceService(PerformanceService):
    def __init__(self, cluster, num_nodes, rest, topic, num_records, record_size, batch_size, throughput, settings={}):
        super(RestProducerPerformanceService, self).__init__(cluster, num_nodes)
        self.rest = rest
        self.args = {
            'topic': topic,
            'num_records': num_records,
            'record_size': record_size,
            'batch_size': batch_size,
            # Because of the way this test tries to match the requested
            # throughput, we need to make sure any negative values are at least
            # batch_size
            'throughput': throughput if throughput > 0 else -batch_size
        }
        self.settings = settings

    def _worker(self, idx, node):
        args = self.args.copy()
        args.update({'rest_url': self.rest.url()})
        cmd = "/opt/kafka-rest/bin/kafka-rest-run-class io.confluent.kafkarest.tools.ProducerPerformance "\
              "'%(rest_url)s' %(topic)s %(num_records)d %(record_size)d %(batch_size)d %(throughput)d" % args
        for key,value in self.settings.items():
            cmd += " %s=%s" % (str(key), str(value))
        self.logger.debug("REST producer performance %d command: %s", idx, cmd)
        last = None
        for line in node.account.ssh_capture(cmd):
            self.logger.debug("REST producer performance %d: %s", idx, line.strip())
            last = line
        # Parse and save the last line's information
        parts = last.split(',')
        self.results[idx-1] = {
            'records': int(parts[0].split()[0]),
            'records_per_sec': float(parts[1].split()[0]),
            'mbps': float(parts[1].split('(')[1].split()[0]),
            'latency_avg_ms': float(parts[2].split()[0]),
            'latency_max_ms': float(parts[3].split()[0]),
            'latency_50th_ms': float(parts[4].split()[0]),
            'latency_95th_ms': float(parts[5].split()[0]),
            'latency_99th_ms': float(parts[6].split()[0]),
            'latency_999th_ms': float(parts[7].split()[0]),
        }


class ConsumerPerformanceService(PerformanceService):
    def __init__(self, cluster, num_nodes, kafka, topic, num_records, throughput, threads=1, settings={}):
        super(ConsumerPerformanceService, self).__init__(cluster, num_nodes)
        self.kafka = kafka
        self.args = {
            'topic': topic,
            'num_records': num_records,
            'throughput': throughput,
            'threads': threads,
        }
        self.settings = settings

    def _worker(self, idx, node):
        args = self.args.copy()
        args.update({'zk_connect': self.kafka.zk.connect_setting()})
        cmd = "/opt/kafka/bin/kafka-consumer-perf-test.sh "\
              "--topic %(topic)s --messages %(num_records)d --zookeeper %(zk_connect)s" % args
        for key,value in self.settings.items():
            cmd += " %s=%s" % (str(key), str(value))
        self.logger.debug("Consumer performance %d command: %s", idx, cmd)
        last = None
        for line in node.account.ssh_capture(cmd):
            self.logger.debug("Consumer performance %d: %s", idx, line.strip())
            last = line
        # Parse and save the last line's information
        parts = last.split(',')
        self.results[idx-1] = {
            'total_mb': float(parts[3]),
            'mbps': float(parts[4]),
            'records_per_sec': float(parts[6]),
        }


class RestConsumerPerformanceService(PerformanceService):
    def __init__(self, cluster, num_nodes, rest, topic, num_records, throughput, settings={}):
        super(RestConsumerPerformanceService, self).__init__(cluster, num_nodes)
        self.rest = rest
        self.args = {
            'topic': topic,
            'num_records': num_records,
            # See note in producer version. For consumer, must be as large as
            # the default # of messages returned per request, currently 100
            'throughput': throughput if throughput > 0 else -100
        }
        self.settings = settings

    def _worker(self, idx, node):
        args = self.args.copy()
        args.update({'rest_url': self.rest.url()})
        cmd = "/opt/kafka-rest/bin/kafka-rest-run-class io.confluent.kafkarest.tools.ConsumerPerformance "\
              "'%(rest_url)s' %(topic)s %(num_records)d %(throughput)d" % args
        for key, value in self.settings.items():
            cmd += " %s=%s" % (str(key), str(value))
        self.logger.debug("REST Consumer performance %d command: %s", idx, cmd)
        last = None
        for line in node.account.ssh_capture(cmd):
            self.logger.debug("REST Consumer performance %d: %s", idx, line.strip())
            last = line
        # Parse and save the last line's information
        self.results[idx-1] = parse_performance_output(last)


class SchemaRegistryPerformanceService(PerformanceService):
    def __init__(self, cluster, num_nodes, schema_registry, subject, num_schemas, schemas_per_sec, settings={}):
        super(SchemaRegistryPerformanceService, self).__init__(cluster, num_nodes)
        self.schema_registry = schema_registry

        self.args = {
            'subject' : subject,
            'num_schemas' : num_schemas,
            'schemas_per_sec' : schemas_per_sec
        }
        self.settings = settings

    def _worker(self, idx, node):
        args = self.args.copy()

        args.update({'schema_registry_url': self.schema_registry.url()})

        cmd = "/opt/schema-registry/bin/schema-registry-run-class io.confluent.kafka.schemaregistry.tools.SchemaRegistryPerformance "\
              "'%(schema_registry_url)s' %(subject)s %(num_schemas)d %(schemas_per_sec)d" % args
        for key, value in self.settings.items():
            cmd += " %s=%s" % (str(key), str(value))

        self.logger.debug("Schema Registry performance %d command: %s", idx, cmd)
        last = None
        for line in node.account.ssh_capture(cmd):
            self.logger.info("Schema Registry performance %d: %s", idx, line.strip())
            last = line
        # Parse and save the last line's information
        self.results[idx-1] = parse_performance_output(last)


class HadoopPerformanceService(PerformanceService):
    def __init__(self, cluster, num_nodes, hadoop, settinss={}):
        super(HadoopPerformanceService, self).__init__(cluster, num_nodes)
        self.hadoop = hadoop
        self.settings = settinss
        self.args = {
            'hadoop_path': '/opt/hadoop-cdh',
            'hadoop_example_jar': 'share/hadoop/mapreduce/hadoop-mapreduce-examples-2.5.0-cdh5.3.0.jar',
            'hadoop_conf_dir': '/mnt'
        }

    def _worker(self, idx, node):
        args = self.args.copy()
        self.hadoop.distribute_hdfs_confs(node)

        if isinstance(self.hadoop, HadoopV1Service):
            args.update({'hadoop_example_jar': 'share/hadoop/mapreduce1/hadoop-examples-2.5.0-mr1-cdh5.3.0.jar'})
            self.hadoop.distribute_mr1_confs(node)
        else:
            self.hadoop.distribute_yarn_confs(node)

        cmd = "HADOOP_CONF_DIR=%(hadoop_conf_dir)s %(hadoop_path)s/bin/hadoop jar " \
              "%(hadoop_path)s/%(hadoop_example_jar)s pi 2 10" % args
        for key, value in self.settings.items():
            cmd += " %s=%s" % (str(key), str(value))

        self.logger.debug("Hadoop performance %d command: %s", idx, cmd)
        for line in node.account.ssh_capture(cmd):
            self.logger.info("Camus performance %d: %s", idx, line.strip())


class CamusPerformanceService(PerformanceService):
    def __init__(self, cluster, num_nodes, kafka, hadoop, schema_registry, settings={}):
        super(CamusPerformanceService, self).__init__(cluster, num_nodes)
        self.kafka = kafka
        self.hadoop = hadoop
        self.schema_registry = schema_registry
        self.settings = settings
        self.args = {
            'hadoop_path': '/opt/hadoop-cdh',
            'camus_path': '/opt/camus/camus-example/',
            'camus_jar': 'camus-example-0.1.0-SNAPSHOT-shaded.jar',
            'camus_property': '/mnt/camus.properties',
            'camus_main': 'com.linkedin.camus.etl.kafka.CamusJob',
            'broker_list': self.kafka.bootstrap_servers(),
            'schema_registry_url': self.schema_registry.url(),
            'avro_producer_path': '/vagrant/avro-producer',
            'avro_producer': 'avro-producer-1.0-SNAPSHOT-jar-with-dependencies.jar',
            'topic': 'testAvro',
            'num_messages': 10
        }

    def _worker(self, idx, node):
        args = self.args.copy()

        produce_cmd = "java -jar %(avro_producer_path)s/%(avro_producer)s " \
                      "%(topic)s %(broker_list)s %(schema_registry_url)s %(num_messages)d" % args
        self.logger.debug("Avro producer %d command: %s", idx, produce_cmd)
        for line in node.account.ssh_capture(produce_cmd):
            self.logger.info("Avro producer %d: %s", idx, line.strip())

        self.hadoop.distribute_hdfs_confs(node)
        if isinstance(self.hadoop, HadoopV1Service):
            self.hadoop.distribute_mr1_confs(node)
        else:
            self.hadoop.distribute_yarn_confs(node)
        self.create_camus_props(node)

        cmd = "HADOOP_CONF_DIR=/mnt %(hadoop_path)s/bin/hadoop jar %(camus_path)s/target/%(camus_jar)s %(camus_main)s " \
              "-D schema.registry.url=%(schema_registry_url)s -P %(camus_property)s " \
              "-Dlog4j.configuration=file:%(camus_path)s/log4j.xml" % args

        for key, value in self.settings.items():
            cmd += " %s=%s" % (str(key), str(value))

        self.logger.debug("Camus performance %d command: %s", idx, cmd)
        # last = None
        for line in node.account.ssh_capture(cmd):
            self.logger.info("Camus performance %d: %s", idx, line.strip())
            # last = line
        # Parse and save the last line's information
        # self.results[idx-1] = parse_performance_output(last)
        # node.account.ssh("rm -rf /mnt/camus.properties")

    def create_camus_props(self, node):
        camus_props_template = open('templates/camus.properties').read()
        camus_props_params = {
            'kafka_brokers': self.args['broker_list'],
            'kafka_whitelist_topics': self.args['topic']
        }
        camus_props = camus_props_template % camus_props_params
        node.account.create_file(self.args['camus_property'], camus_props)


class EndToEndLatencyService(PerformanceService):
    def __init__(self, cluster, num_nodes, kafka, topic, num_records, consumer_fetch_max_wait=100, acks=1):
        super(EndToEndLatencyService, self).__init__(cluster, num_nodes)
        self.kafka = kafka
        self.args = {
            'topic': topic,
            'num_records': num_records,
            'consumer_fetch_max_wait': consumer_fetch_max_wait,
            'acks': acks
        }

    def _worker(self, idx, node):
        args = self.args.copy()
        args.update({
            'zk_connect': self.kafka.zk.connect_setting(),
            'bootstrap_servers': self.kafka.bootstrap_servers(),
        })
        cmd = "/opt/kafka/bin/kafka-run-class.sh kafka.tools.TestEndToEndLatency "\
              "%(bootstrap_servers)s %(zk_connect)s %(topic)s %(num_records)d "\
              "%(consumer_fetch_max_wait)d %(acks)d" % args
        self.logger.debug("End-to-end latency %d command: %s", idx, cmd)
        results = {}
        for line in node.account.ssh_capture(cmd):
            self.logger.debug("End-to-end latency %d: %s", idx, line.strip())
            if line.startswith("Avg latency:"):
                results['latency_avg_ms'] = float(line.split()[2])
            if line.startswith("Percentiles"):
                results['latency_50th_ms'] = float(line.split()[3][:-1])
                results['latency_99th_ms'] = float(line.split()[6][:-1])
                results['latency_999th_ms'] = float(line.split()[9])
        self.results[idx-1] = results


def parse_performance_output(summary):
        parts = summary.split(',')
        results = {
            'records': int(parts[0].split()[0]),
            'records_per_sec': float(parts[1].split()[0]),
            'mbps': float(parts[1].split('(')[1].split()[0]),
            'latency_avg_ms': float(parts[2].split()[0]),
            'latency_max_ms': float(parts[3].split()[0]),
            'latency_50th_ms': float(parts[4].split()[0]),
            'latency_95th_ms': float(parts[5].split()[0]),
            'latency_99th_ms': float(parts[6].split()[0]),
            'latency_999th_ms': float(parts[7].split()[0]),
        }
        # To provide compatibility with ConsumerPerformanceService
        results['total_mb'] = results['mbps'] * (results['records'] / results['records_per_sec'])
        results['rate_mbps'] = results['mbps']
        results['rate_mps'] = results['records_per_sec']

        return results

