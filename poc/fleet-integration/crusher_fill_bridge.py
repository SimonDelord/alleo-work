#!/usr/bin/env python3
"""
Crusher fill bridge: subscribes to truck MQTT telemetry and writes crusher Modbus
registers when trucks dump at a crusher bay.

Lives in fleet-integration — connects truck-fleet (via MQTT, read-only) to
crusher-fleet (via Modbus TCP) without direct coupling between those namespaces.

Also publishes fleet.crushers.state after each Modbus update and on a periodic Modbus
poll so destination-router and the live map see PLC drain, not just dump events.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable
from pymodbus.client import ModbusTcpClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("crusher_fill_bridge")


def _parse_mqtt_broker() -> tuple[str, int]:
    broker = os.environ.get("MQTT_BROKER", "").strip()
    if broker:
        if ":" in broker:
            host, port_str = broker.rsplit(":", 1)
            return host, int(port_str)
        return broker, 1883
    host = os.environ.get("MQTT_HOST", "mqtt-broker.truck-fleet.svc")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    return host, port


MQTT_HOST, MQTT_PORT = _parse_mqtt_broker()
MQTT_TOPIC = os.environ.get("MQTT_TOPIC_SUBSCRIBE", "fleet/trucks/+/telemetry")
BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    os.environ.get("KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"),
)
TOPIC_CRUSHER_STATE = os.environ.get("KAFKA_TOPIC_CRUSHER_STATE", "fleet.crushers.state")
VALID_CRUSHERS = frozenset(
    c.strip()
    for c in os.environ.get("VALID_CRUSHERS", "crusher-1,crusher-2").split(",")
    if c.strip()
)
CAPACITY_FILL_PCT = float(os.environ.get("CAPACITY_FILL_PCT", "90"))
LOAD_DROP_THRESHOLD = float(os.environ.get("LOAD_DROP_THRESHOLD", "5.0"))
FILL_PER_LOAD_PCT = float(os.environ.get("FILL_PER_LOAD_PCT", "0.12"))
MODBUS_TIMEOUT_SEC = float(os.environ.get("MODBUS_TIMEOUT_SEC", "5.0"))
POLL_PUBLISH_INTERVAL_SEC = float(os.environ.get("POLL_PUBLISH_INTERVAL_SEC", os.environ.get("PUBLISH_INTERVAL_SEC", "10")))

# Register indices (must match crusher_plc.py / historian.py)
REG_FILL_PCT = 0
REG_AT_CAPACITY = 1
REG_STATUS = 2
REG_THROUGHPUT = 3
REG_DUMP_COUNT = 4
REG_READY = 5
REGISTER_COUNT = 6

STATUS_NAMES = {
    0: "empty",
    1: "accepting",
    2: "full",
    3: "fault",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(raw: bytes | str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            data = json.loads(raw.decode("utf-8"))
        else:
            data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_crusher_targets(raw: str) -> dict[str, tuple[str, int]]:
    """Parse crusher_id:host:port,... into a lookup map."""
    targets: dict[str, tuple[str, int]] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(":")
        if len(pieces) != 3:
            raise ValueError(f"Invalid CRUSHER_MODBUS_TARGETS entry: {part!r}")
        crusher_id, host, port_str = pieces
        targets[crusher_id] = (host, int(port_str))
    if not targets:
        raise ValueError("CRUSHER_MODBUS_TARGETS must list at least one crusher")
    return targets


class CrusherModbusClient:
    def __init__(self, targets: dict[str, tuple[str, int]]) -> None:
        self._targets = targets
        self._lock = threading.Lock()

    def _connect(self, crusher_id: str) -> tuple[ModbusTcpClient, str, int] | None:
        host_port = self._targets.get(crusher_id)
        if host_port is None:
            LOG.warning("No Modbus target configured for %s", crusher_id)
            return None
        host, port = host_port
        client = ModbusTcpClient(host, port=port, timeout=MODBUS_TIMEOUT_SEC)
        if not client.connect():
            LOG.warning("Modbus connect failed for %s at %s:%s", crusher_id, host, port)
            return None
        return client, host, port

    def read_registers(self, crusher_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect(crusher_id)
            if conn is None:
                return None
            client, _, _ = conn
            try:
                result = client.read_holding_registers(0, REGISTER_COUNT, slave=0)
                if result.isError():
                    LOG.warning("Modbus read error for %s: %s", crusher_id, result)
                    return None
                regs = [int(v) & 0xFFFF for v in result.registers]
                status_code = regs[REG_STATUS]
                return {
                    "crusher_id": crusher_id,
                    "crusher_name": crusher_id,
                    "fill_pct": float(regs[REG_FILL_PCT]),
                    "at_capacity": bool(regs[REG_AT_CAPACITY]),
                    "status_code": status_code,
                    "status": STATUS_NAMES.get(status_code, "unknown"),
                    "throughput_tph": float(regs[REG_THROUGHPUT]),
                    "dump_count": int(regs[REG_DUMP_COUNT]),
                    "ready": bool(regs[REG_READY]),
                }
            finally:
                client.close()

    def apply_dump(self, crusher_id: str, fill_delta: float, increment_dump_count: bool) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect(crusher_id)
            if conn is None:
                return None
            client, _, _ = conn
            try:
                result = client.read_holding_registers(0, REGISTER_COUNT, slave=0)
                if result.isError():
                    LOG.warning("Modbus read error for %s: %s", crusher_id, result)
                    return None

                regs = [int(v) & 0xFFFF for v in result.registers]
                fill = min(100.0, float(regs[REG_FILL_PCT]) + fill_delta)
                dump_count = int(regs[REG_DUMP_COUNT])
                if increment_dump_count:
                    dump_count = min(65535, dump_count + 1)

                fill_int = max(0, min(100, int(round(fill))))
                at_capacity = 1 if fill_int >= CAPACITY_FILL_PCT else 0

                if at_capacity:
                    status = 2  # full
                    ready = 0
                elif fill_int <= 5:
                    status = 0  # empty
                    ready = 1
                else:
                    status = 1  # accepting
                    ready = 1

                regs[REG_FILL_PCT] = fill_int
                regs[REG_AT_CAPACITY] = at_capacity
                regs[REG_STATUS] = status
                regs[REG_DUMP_COUNT] = dump_count
                regs[REG_READY] = ready

                write_result = client.write_registers(0, regs, slave=0)
                if write_result.isError():
                    LOG.warning("Modbus write error for %s: %s", crusher_id, write_result)
                    return None

                state = {
                    "crusher_id": crusher_id,
                    "crusher_name": crusher_id,
                    "fill_pct": float(fill_int),
                    "at_capacity": bool(at_capacity),
                    "status_code": status,
                    "status": STATUS_NAMES.get(status, "unknown"),
                    "throughput_tph": float(regs[REG_THROUGHPUT]),
                    "dump_count": dump_count,
                    "ready": bool(ready),
                }
                LOG.info(
                    "Modbus write %s fill=%.0f%% dumps=%d at_capacity=%s (delta=%.1f)",
                    crusher_id,
                    state["fill_pct"],
                    dump_count,
                    state["at_capacity"],
                    fill_delta,
                )
                return state
            finally:
                client.close()


class CrusherFillBridge:
    def __init__(self) -> None:
        targets_raw = os.environ.get(
            "CRUSHER_MODBUS_TARGETS",
            "crusher-1:crusher-1.crusher-fleet.svc:502,"
            "crusher-2:crusher-2.crusher-fleet.svc:502",
        )
        self._targets = _parse_crusher_targets(targets_raw)
        self._modbus = CrusherModbusClient(self._targets)
        self._lock = threading.Lock()
        self._truck_state: dict[str, dict[str, Any]] = {}
        self._producer = self._create_producer()
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="crusher-fill-bridge")
        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_message = self._on_mqtt_message

    def _create_producer(self) -> KafkaProducer:
        while True:
            try:
                producer = KafkaProducer(
                    bootstrap_servers=BOOTSTRAP.split(","),
                    value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
                    key_serializer=lambda k: k.encode("utf-8") if k else None,
                    acks="all",
                    retries=3,
                )
                LOG.info("Kafka producer connected to %s", BOOTSTRAP)
                return producer
            except NoBrokersAvailable as exc:
                LOG.warning("Kafka unavailable (%s), retrying in 5s", exc)
                time.sleep(5)

    def _on_mqtt_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: dict,
        reason_code: int,
        properties: object,
    ) -> None:
        del userdata, flags, properties
        if reason_code == 0:
            client.subscribe(MQTT_TOPIC, qos=0)
            LOG.info("Subscribed to MQTT %s on %s:%s", MQTT_TOPIC, MQTT_HOST, MQTT_PORT)
        else:
            LOG.error("MQTT connect failed with code %s", reason_code)

    def _on_mqtt_message(
        self,
        client: mqtt.Client,
        userdata: object,
        msg: mqtt.MQTTMessage,
    ) -> None:
        del client, userdata
        event = _parse_json(msg.payload)
        if event is None:
            LOG.warning("Invalid JSON on topic %s", msg.topic)
            return
        self._handle_truck_telemetry(event)

    def connect_mqtt(self, running: list[bool]) -> None:
        while running[0]:
            try:
                self._mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                return
            except OSError as exc:
                LOG.warning("MQTT connect failed (%s), retrying in 5s", exc)
                time.sleep(5)

    def _publish_crusher_state(self, state: dict[str, Any]) -> None:
        crusher_name = str(state["crusher_name"])
        event = {
            "crusher_name": crusher_name,
            "status": state["status"],
            "fill_pct": state["fill_pct"],
            "at_capacity": state["at_capacity"],
            "dump_count": state["dump_count"],
            "ready": state["ready"],
            "updated_at": _now_iso(),
            "source": "crusher-fill-bridge",
        }
        self._producer.send(TOPIC_CRUSHER_STATE, key=crusher_name, value=event)
        self._producer.flush(timeout=5)
        LOG.info(
            "Published %s to %s fill=%.0f%% at_capacity=%s",
            crusher_name,
            TOPIC_CRUSHER_STATE,
            event["fill_pct"],
            event["at_capacity"],
        )

    def _apply_fill_delta(
        self,
        crusher_id: str,
        fill_delta: float,
        increment_dump_count: bool,
    ) -> None:
        if fill_delta <= 0:
            return
        state = self._modbus.apply_dump(crusher_id, fill_delta, increment_dump_count)
        if state is not None:
            self._publish_crusher_state(state)

    def _handle_truck_telemetry(self, event: dict[str, Any]) -> None:
        truck_id = str(event.get("truck_id", ""))
        if not truck_id:
            return

        state = str(event.get("state", "unknown"))
        destination = str(event.get("destination_crusher", ""))
        load_pct = float(event.get("load_pct", 0))

        with self._lock:
            prev = self._truck_state.get(truck_id, {})
            prev_state = str(prev.get("state", ""))
            prev_load = float(prev.get("load_pct", load_pct))
            dump_started = bool(prev.get("dump_started", False))

            self._truck_state[truck_id] = {
                "state": state,
                "load_pct": load_pct,
                "destination_crusher": destination,
                "dump_started": dump_started,
            }

        if destination not in VALID_CRUSHERS:
            return

        # Truck just arrived at crusher and started dumping.
        if state == "dumping" and prev_state != "dumping":
            fill_delta = max(1.0, load_pct * FILL_PER_LOAD_PCT)
            self._apply_fill_delta(destination, fill_delta, increment_dump_count=True)
            with self._lock:
                if truck_id in self._truck_state:
                    self._truck_state[truck_id]["dump_started"] = True
            return

        # While dumping, apply incremental fill as load drops.
        if state == "dumping" and prev_state == "dumping":
            load_drop = prev_load - load_pct
            if load_drop >= LOAD_DROP_THRESHOLD:
                fill_delta = load_drop * FILL_PER_LOAD_PCT
                self._apply_fill_delta(destination, fill_delta, increment_dump_count=False)
            return

        # Reset dump tracking when truck leaves dumping state.
        if prev_state == "dumping" and state != "dumping":
            with self._lock:
                if truck_id in self._truck_state:
                    self._truck_state[truck_id]["dump_started"] = False

    def publish_all_crusher_state(self) -> None:
        """Publish current Modbus readings for all configured crushers."""
        for crusher_id in sorted(self._targets):
            state = self._modbus.read_registers(crusher_id)
            if state is not None:
                self._publish_crusher_state(state)

    def run(self, running: list[bool]) -> None:
        LOG.info(
            "Crusher fill bridge started: MQTT %s:%s topic %s → Modbus %s → produce %s",
            MQTT_HOST,
            MQTT_PORT,
            MQTT_TOPIC,
            ", ".join(sorted(self._targets)),
            TOPIC_CRUSHER_STATE,
        )
        self.publish_all_crusher_state()
        self.connect_mqtt(running)
        if not running[0]:
            return

        self._mqtt.loop_start()
        last_poll = time.monotonic()
        try:
            while running[0]:
                now = time.monotonic()
                if now - last_poll >= POLL_PUBLISH_INTERVAL_SEC:
                    self.publish_all_crusher_state()
                    last_poll = now
                time.sleep(1)
        finally:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            self._producer.close()


def main() -> None:
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    bridge = CrusherFillBridge()
    try:
        bridge.run(running)
    finally:
        LOG.info("Stop requested, exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
