"""MQTTS (port 8883) connection management for a fleet of Bambu Lab printers."""

import asyncio
import json
import logging
import ssl
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

MQTTS_PORT = 8883
MQTT_KEEPALIVE = 30
MQTT_USERNAME = "bblp"
#: How long send_command waits for the link before reporting the node offline.
MQTT_SEND_WAIT = 8.0


def build_tls_context() -> ssl.SSLContext:
    """Printer certificates are self-signed, so hostname/CA checks are disabled."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class BambuMqttClient:
    """A single MQTTS link to one printer, pumping telemetry into the asyncio loop."""

    def __init__(
        self,
        printer_id: str,
        ip: str,
        sn: str,
        access_code: str,
        loop: asyncio.AbstractEventLoop,
        on_telemetry: Callable[[str, Dict[str, Any]], None],
    ):
        self.printer_id = printer_id
        self.ip = ip
        self.sn = sn
        self.access_code = access_code
        self.loop = loop
        self.on_telemetry = on_telemetry
        self.connected = False
        self.last_error: Optional[str] = None
        self._seq = 0
        self._lock = threading.Lock()
        # A unique client id per process: two servers sharing "bfm_<sn>" would
        # kick each other off the printer's broker on every reconnect.
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bfm_{sn}_{uuid.uuid4().hex[:6]}",
            protocol=mqtt.MQTTv311,
        )

    # ------------------------------------------------------------------ setup

    def start(self) -> None:
        self.client.username_pw_set(MQTT_USERNAME, self.access_code)
        self.client.tls_set_context(build_tls_context())
        self.client.tls_insecure_set(True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        try:
            self.client.connect_async(self.ip, MQTTS_PORT, keepalive=MQTT_KEEPALIVE)
            self.client.loop_start()
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("MQTT setup failed for %s (%s): %s", self.sn, self.ip, exc)

    def stop(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception as exc:
            logger.warning("MQTT shutdown for %s raised: %s", self.sn, exc)
        self.connected = False

    # -------------------------------------------------------------- callbacks

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # paho 2.x hands us a ReasonCode object; ``is_failure`` is its stable
        # success/failure test across MQTT 3.1.1 and 5.
        if not reason_code.is_failure:
            self.connected = True
            self.last_error = None
            client.subscribe(f"device/{self.sn}/report")
            # Ask the printer for a full state / telemetry dump.
            self.send_command({"pushing": {"sequence_id": self.next_sequence_id(), "command": "start"}}, wait=False)
            self.send_command({"info": {"sequence_id": self.next_sequence_id(), "command": "get_version"}}, wait=False)
            logger.info("MQTT connected to %s (%s)", self.sn, self.ip)
        else:
            self.connected = False
            self.last_error = f"connection rejected with rc={reason_code}"
            logger.error("MQTT connection rejected for %s, rc=%s", self.sn, reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self.connected = False
        logger.warning("MQTT disconnected from %s, rc=%s", self.sn, reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            logger.debug("Dropping non-JSON MQTT payload on %s: %s", msg.topic, exc)
            return

        if "print" not in payload:
            return

        data = payload["print"]
        try:
            asyncio.run_coroutine_threadsafe(self.on_telemetry(self.printer_id, data), self.loop)
        except Exception as exc:
            logger.debug("Could not forward telemetry for %s: %s", self.printer_id, exc)

    # ------------------------------------------------------------- publishing

    def next_sequence_id(self) -> str:
        with self._lock:
            self._seq = self._seq % 2_147_483_647 + 1
            return str(self._seq)

    def wait_connected(self, timeout: float = MQTT_SEND_WAIT) -> bool:
        """Block briefly for the link to come up before declaring failure.

        Without this, ``publish`` silently queues while offline and the API
        reports success for a command the printer never received.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected:
                return True
            time.sleep(0.1)
        return self.connected

    def send_command(self, cmd_dict: Dict[str, Any], wait: bool = True) -> bool:
        """Publish one command; returns False if it could not be delivered.

        ``wait`` must be False when called from a paho callback: blocking on
        PUBACK inside the network thread stops that thread from processing
        acknowledgements (deadlock) and silently starves the telemetry feed.
        """
        if wait and not self.wait_connected():
            self.last_error = "not connected to MQTTS"
            logger.warning("Dropping command for %s: not connected", self.sn)
            return False
        topic = f"device/{self.sn}/request"
        payload = self.stamp_sequence_ids(cmd_dict)
        try:
            info = self.client.publish(topic, json.dumps(payload))
            if wait:
                info.wait_for_publish(timeout=5)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                self.last_error = f"publish failed rc={info.rc}"
                logger.error("MQTT publish to %s failed: %s", self.sn, self.last_error)
                return False
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("MQTT publish to %s failed: %s", self.sn, exc)
            return False

    def stamp_sequence_ids(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Add a ``sequence_id`` to every command block that lacks one.

        The firmware answers ``json wrong format`` to a command with no
        sequence_id - it is not optional, even though nothing documents it.
        """
        stamped = dict(payload)
        for channel, block in stamped.items():
            if isinstance(block, dict) and "command" in block and "sequence_id" not in block:
                block = dict(block)
                block["sequence_id"] = self.next_sequence_id()
                stamped[channel] = block
        return stamped


class FleetMqttManager:
    """Registry of every configured printer node and its MQTTS client."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_telemetry: Callable[[str, Dict[str, Any]], None]):
        self.loop = loop
        self.on_telemetry = on_telemetry
        self.clients: Dict[str, BambuMqttClient] = {}

    def register_printer(self, p_id: str, ip: str, sn: str, access_code: str) -> None:
        if p_id in self.clients:
            self.unregister_printer(p_id)
        client = BambuMqttClient(p_id, ip, sn, access_code, self.loop, self.on_telemetry)
        client.start()
        self.clients[p_id] = client

    def unregister_printer(self, p_id: str) -> None:
        client = self.clients.pop(p_id, None)
        if client:
            client.stop()

    def send_payloads(self, p_id: str, payloads: List[Dict[str, Any]]) -> bool:
        """Publish a sequence of payloads; stops at the first failure."""
        if p_id not in self.clients:
            logger.warning("No MQTT client registered for printer %s", p_id)
            return False
        return all(self.clients[p_id].send_command(p) for p in payloads)

    def send_to_printer(self, p_id: str, payload: Dict[str, Any]) -> bool:
        return self.send_payloads(p_id, [payload])

    def status(self) -> Dict[str, Dict[str, Any]]:
        return {
            p_id: {"connected": c.connected, "error": c.last_error}
            for p_id, c in self.clients.items()
        }

    def shutdown(self) -> None:
        for client in list(self.clients.values()):
            client.stop()
        self.clients.clear()
