import pickle
from typing import Dict, Optional, Union
from threading import Thread

import pika
from pika.adapters.blocking_connection import BlockingConnection
import dataclasses as dc


from qcodes.dataset.rmq_setup import (read_config_file,
                                      setup_exchange_and_queues_from_conf)
from qcodes.dataset.data_storage_interface import (DataWriterInterface,
                                                   VALUES,
                                                   MetaData)


@dc.dataclass
class MessageHeader:
    guid: str
    messagetype: str
    version: int
    db_location: str


class Heart(Thread):
    """
    A separate thread to send heartbeats to RMQ
    """

    def __init__(self, conn: BlockingConnection):
        super().__init__()
        self.conn = conn
        self.keep_beating = True
        self.conf = read_config_file()
        self.sleep_time = self.conf['heartbeat']/2

    def stop(self):
        self.keep_beating = False

    def run(self):
        while self.keep_beating:
            self.conn.sleep(self.sleep_time)
            self.conn.process_data_events()


class BakedPotato:
    """
    A non-functional heart for mocking
    """

    def __init__(self, conn: BlockingConnection):
        pass

    def stop(self):
        pass

    def start(self):
        pass

    def join(self):
        pass


class RabbitMQWriterInterface(DataWriterInterface):

    def __init__(self, guid: str, path_to_db: str,
                 disable_heartbeat: bool = False,
                 use_test_queue: bool = False):
        super().__init__(guid)

        conf = read_config_file(testing=use_test_queue)
        conn, chan = setup_exchange_and_queues_from_conf(conf)
        self.conn = conn
        self.channel = chan

        if not disable_heartbeat:
            self.heart: Union[Heart, BakedPotato] = Heart(self.conn)
        else:
            self.heart: Union[Heart, BakedPotato] = BakedPotato(self.conn)

        self.heart.start()

        self.headers: Dict[str, Dict] = {}
        self.headers['data'] = dc.asdict(MessageHeader(guid=self.guid,
                                                       messagetype='data',
                                                       version=0,
                                                       db_location=path_to_db))
        self.headers['metadata'] = dc.asdict(MessageHeader(
                                                guid=self.guid,
                                                messagetype='metadata',
                                                version=0,
                                                db_location=path_to_db))


    def store_results(self, results: Dict[str, VALUES]):
        results_dump = pickle.dumps(results)
        self.channel.publish(exchange='mydata',
                             routing_key='',
                             body=results_dump,
                             properties=pika.BasicProperties(
                                 headers=self.headers['data'],
                                 delivery_mode=2))

    def create_run(self, **kwargs) -> None:
        exp_id: Optional[int] = kwargs.get('exp_id', None)
        name: Optional[str] = kwargs.get('name', None)
        exp_name: Optional[str] = kwargs.get('exp_name', None)
        sample_name: Optional[str] = kwargs.get('sample_name', None)
        # TODO: this function ought to produce the run_started value
        metadata = MetaData(tags={'exp_id': exp_id},
                                      name=name,
                                      exp_name=exp_name,
                                      sample_name=sample_name)

        self.store_meta_data(metadata)

    def prepare_for_storing_results(self):
        raise NotImplementedError

    def store_meta_data(self, metadata: MetaData) -> None:
        md_dump = pickle.dumps(dc.asdict(metadata))
        self.channel.publish(exchange='mydata',
                             routing_key='',
                             body=md_dump,
                             properties=pika.BasicProperties(
                                 headers=self.headers['metadata'],
                                 delivery_mode=2))

    def resume_run(self, *args):
        raise NotImplementedError

    def close(self):
        self.heart.stop()
        self.heart.join()
        self.conn.close()
