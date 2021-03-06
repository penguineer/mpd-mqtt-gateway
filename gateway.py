import time
import logging
import queue
import threading
import socket
from mpd import MPDClient, MPDError, CommandError
import paho.mqtt.client


logger = logging.getLogger(__name__)


class MpdMqttGateway():
    """
    The gateway creates workers for MPD and MQTT servers and wires them together
    using a queue.
    """
    def __init__(self, mpd_server, mqtt_server, mqtt_topic):
        self.exit = threading.Event()
        self.mpd_server = mpd_server
        self.mqtt_server = mqtt_server
        self.mqtt_topic = mqtt_topic

    def run(self):
        """
        Run the workers. This method is blocking. To shutdown the gateway, call
        shutdown() from e.g. a signal handler.
        """
        music_events = queue.Queue(maxsize=100)
        reader_thread = MpdReaderThread(
            mpd_server=self.mpd_server,
            target_queue=music_events,
        )
        writer_thread = MqttWriterThread(
            source_queue=music_events,
            mqtt_server=self.mqtt_server,
            topic=self.mqtt_topic
        )
        logger.info("Starting mpd mqtt gateway")
        reader_thread.start()
        writer_thread.start()
        self.__wait_until_shutdown()
        logger.info("Trying to stop mpd mqtt gateway gracefully")
        reader_thread.shutdown()
        writer_thread.shutdown()
        reader_thread.join()
        writer_thread.join()
        logger.info("Stopped mpd mqtt gateway")

    def __wait_until_shutdown(self):
        while not self.exit.is_set():
            self.exit.wait(timeout=0.05)

    def shutdown(self):
        self.exit.set()


class MpdReaderThread(threading.Thread):
    """
    Connect to an mpd server, retrieve the current playing song and push it to
    the target queue. If the network goes down, try to reconnect forever until
    the server comes back online or shutdown() is called.
    """

    def __init__(self, mpd_server, target_queue, polling_interval=5.0, retry_interval=5.0):
        """
        mpd_server: MpdReader to access mpd server
        target_queue: Queue where metadata will be written to
        polling_interval: how often mpd data is refreshed
        retry_interval: how long to wait before trying to reconnect
        """
        threading.Thread.__init__(self, name="MpdReaderThread")
        self.exit = threading.Event()
        self.mpd_server = mpd_server
        self.target_queue = target_queue
        self.polling_interval = polling_interval
        self.retry_interval = retry_interval

    def run(self):
        logger.info("MPD worker started")
        while not self.exit.is_set():
            try:
                self.__run_polling_loop()
            except:
                logger.info("Waiting %s second(s) before reconnecting", self.retry_interval)
                self.exit.wait(self.retry_interval)
        logger.info("MPD worker stopped")

    def shutdown(self):
        self.exit.set()

    def __run_polling_loop(self):
        try:
            last_metadata = None
            self.mpd_server.connect()
            while not self.exit.is_set():
                metadata = self.mpd_server.metadata()
                if metadata != last_metadata:
                    logger.info("Detected new metadata: %s", metadata)
                    self.__push_to_queue(metadata)
                    last_metadata = metadata
                self.exit.wait(self.polling_interval)
        except (OSError, IOError) as err:
            logger.error("MPD connection failed: %s", err.strerror)
            raise
        except MPDError as e:
            logger.error("MPD connection failed: %s", e)
            raise
        except:
            logger.exception("MPD connection failed")
            raise
        finally:
            self.mpd_server.disconnect()

    def __push_to_queue(self, metadata):
        try:
            self.target_queue.put(metadata, block=False)
        except queue.Full:
            logger.error("Queue full, dropping metadata", event)


class MqttWriterThread(threading.Thread):
    def __init__(self, source_queue, mqtt_server, topic, retry_interval=5.0):
        threading.Thread.__init__(self, name="MqttWriterThread")
        self.exit = threading.Event()
        self.mqtt_server = mqtt_server
        self.topic = topic
        self.source_queue = source_queue
        self.retry_interval = retry_interval

    def run(self):
        logger.info("MQTT worker started")
        while not self.exit.is_set():
            try:
                self.__run_polling_loop()
            except:
                logger.info("Waiting %s second(s) before reconnecting", self.retry_interval)
                self.exit.wait(self.retry_interval)
        logger.info("MQTT worker stopped")

    def shutdown(self):
        self.exit.set()

    def __run_polling_loop(self):
        try:
            self.mqtt_server.connect()
            while not self.exit.is_set():
                metadata = self.__read_from_queue()
                if metadata != None:
                    logger.info("Pushing metadata to topic '%s': %s", self.topic, metadata)
                    self.mqtt_server.publish(self.topic, metadata)
                self.mqtt_server.loop()
        except (OSError, IOError) as err:
            logger.error("MQTT connection failed: %s", err.strerror)
            raise
        except:
            logger.exception("MQTT connection failed")
            raise
        finally:
            self.mqtt_server.disconnect()

    def __read_from_queue(self):
        try:
            return self.source_queue.get(block=True, timeout=0.05)
        except queue.Empty:
            return None


class MpdMetadata():
    def __init__(self, mpdsong):
        """
        Creates a new metadata object based on the result from currentsong().
        """
        file = mpdsong.get("file")
        title = mpdsong.get("title")
        artist = mpdsong.get("artist")
        album = mpdsong.get("album")
        if not artist and not album and title:
            if " - " in title:
                artist = title.split(" - ")[0]
                title = title.split(" - ")[1]
        if not artist and not album and not title:
            title = file
        self.title = title
        self.artist = artist
        self.album = album

    def __str__(self):
        return str(self.__dict__)

    def __eq__(self, other): 
        return other != None and self.__dict__ == other.__dict__


class MpdServer():
    """
    Connect to an mpd server, retrieve the current playing song and push it to
    the target queue. If the network goes down, try to reconnect forever until
    the server comes back online or shutdown() is called.
    """

    def __init__(self, hostname, port=6600, timeout=5):
        self.hostname = hostname
        self.port = port
        self.timeout = timeout
        self.mpd = None

    def connect(self):
        logger.info("Connecting to mpd server at %s:%s", self.hostname, self.port)
        self.mpd = MPDClient()
        self.mpd.timeout = self.timeout
        self.mpd.connect(self.hostname, self.port)
        logger.info("Connected to mpd server, version: %s", self.mpd.mpd_version)

    def metadata(self):
        song = self.mpd.currentsong()
        metadata = MpdMetadata(song)
        logger.debug("Received metadata from MPD server: %s", metadata)
        return metadata

    def disconnect(self):
        try:
            self.mpd.close()
            logger.info("Sent close command to mpd server")
        except (MPDError, IOError):
            pass
        try:
            self.mpd.disconnect()
            logger.info("Disconnected from mpd server")
        except (MPDError, IOError):
            pass


class MqttServer():
    def __init__(self, hostname, port=1883, timeout=5):
        self.exit = threading.Event()
        self.hostname = hostname
        self.port = port
        self.timeout = timeout
        self.mqtt = None

    def connect(self):
        logger.info("Connecting to mqtt server at %s:%s", self.hostname, self.port)
        self.mqtt = paho.mqtt.client.Client()
        self.mqtt.connect(
            host=self.hostname,
            port=self.port,
            keepalive=self.timeout
        )
        logger.info("Connected to mqtt server")

    def loop(self):
        logger.debug("Processing incoming/outgoing mqtt packets")
        self.mqtt.loop(timeout=0.05)

    def publish(self, topic, metadata):
        logger.debug("Publishing metadata to topic '%s': %s", topic, metadata)
        self.mqtt.publish("{}/source".format(topic), "mpd")
        self.mqtt.publish("{}/title".format(topic), metadata.title)
        self.mqtt.publish("{}/artist".format(topic), metadata.artist)
        self.mqtt.publish("{}/album".format(topic), metadata.album)

    def disconnect(self):
        try:
            self.mqtt.disconnect()
            logger.info("Disconnected from mqtt server")
            self.mqtt = None
        except (IOError, OSError):
            pass
