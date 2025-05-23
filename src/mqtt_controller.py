from typing import Dict, Any
import json
import paho.mqtt.client as mqtt
import config


class MQTTController:

    def __init__(self):
        self.mqtt_connected = False
        self.client = mqtt.Client(client_id=config.NODE_ID)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.packet_counter = 0

        try:
            print("Authenticating with user:", config.MQTT_USER, "on MQTT connection")
            self.client.username_pw_set(config.MQTT_USER, config.MQTT_PASS)
        except AttributeError:
            print("Using no authentication on MQTT connection")

        if config.MQTT_USE_TLS:
            print("using TLS for MQTT Connection")
            self.client.tls_set()

        try:
            self.client.connect(config.MQTT_SERVER, config.MQTT_PORT)
        except Exception as e:
            print(f"Can't connect to MQTT Broker:{config.MQTT_SERVER} at port:{config.MQTT_PORT}, dump: {e}")

        self.client.loop_start()  # Start MQTT handling in a new thread

    def _get_next_packet_count(self) -> int:
        self.packet_counter += 1
        return self.packet_counter

    def get_connected(self) -> bool:
        return self.mqtt_connected

    def _on_connect(self, _client, _userdata, _flags, _rc) -> None:
        print("Connected to MQTT Broker:", config.MQTT_SERVER, "at port:", config.MQTT_PORT)
        self.mqtt_connected = True

    def _on_disconnect(self, _client, _userdata, _rc) -> None:
        print("Disconnected from MQTT Broker:", config.MQTT_SERVER, "at port:", config.MQTT_PORT)
        self.mqtt_connected = False

    def publish_data(self, data: Dict[str, Any]) -> None:
        data["tele"]["packet_count"] = self._get_next_packet_count()
        json_data = json.dumps(data, indent=4)
        try:
            self.client.publish("sensors" + "/" + config.MQTT_BASE_TOPIC + "/" + config.NODE_ID, json_data, qos=2)
            print("mqtt publish: ", data)
        except Exception as e:
            print("could not push to mqtt: ", e)

    def stop(self) -> None:
        self.client.loop_stop()
