#!/usr/bin/env python3
"""
Simulated haul truck: cycles loading → hauling → dumping → returning,
publishing JSON telemetry to MQTT on each tick.

Crusher destination is bootstrapped from DEFAULT_CRUSHER and updated at runtime
via MQTT new-destination messages. Stop/resume commands arrive on
fleet/trucks/{truck_id}/command.
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("truck_agent")

TRUCK_ID = os.environ.get("TRUCK_ID", "TR1")
MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TICK_SEC = float(os.environ.get("TICK_SEC", "2.0"))
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "fleet/trucks")
NEW_DESTINATION_TOPIC = os.environ.get("MQTT_NEW_DESTINATION_TOPIC", "new-destination")
DEFAULT_CRUSHER = os.environ.get("DEFAULT_CRUSHER", "crusher-1")
VALID_CRUSHERS = frozenset(
    c.strip()
    for c in os.environ.get("VALID_CRUSHERS", "crusher-1,crusher-2").split(",")
    if c.strip()
)

# Mine layout (arbitrary demo coordinates in meters).
LOADING_AREA = (-1200.0, 0.0)
CRUSHERS = {
    "crusher-1": (800.0, 400.0),
    "crusher-2": (800.0, -400.0),
}
HAUL_SPEED_KMH = float(os.environ.get("HAUL_SPEED_KMH", "70"))
RETURN_SPEED_KMH = float(os.environ.get("RETURN_SPEED_KMH", "80"))
LOAD_RATE_PCT_PER_TICK = float(os.environ.get("LOAD_RATE_PCT_PER_TICK", "25"))
DUMP_RATE_PCT_PER_TICK = float(os.environ.get("DUMP_RATE_PCT_PER_TICK", "50"))
LOADING_DWELL_TICKS = int(os.environ.get("LOADING_DWELL_TICKS", "2"))
DUMPING_DWELL_TICKS = int(os.environ.get("DUMPING_DWELL_TICKS", "2"))


class TruckState(str, Enum):
    LOADING = "loading"
    HAULING = "hauling"
    DUMPING = "dumping"
    RETURNING = "returning"
    STOPPED = "stopped"


@dataclass
class SimState:
    state: TruckState = TruckState.LOADING
    x: float = LOADING_AREA[0]
    y: float = LOADING_AREA[1]
    load_pct: float = 0.0
    progress: float = 0.0
    dwell_ticks: int = 0
    destination_crusher: str = DEFAULT_CRUSHER
    assignment_source: str = "bootstrap"
    paused_from_state: TruckState | None = None
    stop_reason: str | None = None
    haul_hold: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _validate_crusher(crusher_id: str) -> bool:
    if crusher_id in VALID_CRUSHERS:
        return True
    LOG.warning("Unknown crusher %s (valid: %s)", crusher_id, sorted(VALID_CRUSHERS))
    return False


def _apply_destination(sim: SimState, crusher_name: str, source: str) -> None:
    if not _validate_crusher(crusher_name):
        return
    with sim._lock:
        previous = sim.destination_crusher
        sim.destination_crusher = crusher_name
        sim.assignment_source = source
    if previous != crusher_name:
        LOG.info(
            "Destination updated: %s → %s (source=%s)",
            previous,
            crusher_name,
            source,
        )


def _crusher_from_topic(topic: str) -> str | None:
    """Parse crusher name from new-destination/{truck_id}/{crusher_name}."""
    prefix = f"{NEW_DESTINATION_TOPIC}/"
    if not topic.startswith(prefix):
        return None
    parts = topic[len(prefix) :].split("/")
    if len(parts) != 2 or parts[0] != TRUCK_ID:
        return None
    return parts[1]


def _handle_new_destination(sim: SimState, topic: str, payload: dict | None) -> None:
    crusher_name = _crusher_from_topic(topic)
    source = "mqtt"

    if payload:
        payload_truck = payload.get("truck_id")
        if payload_truck is not None and str(payload_truck) != TRUCK_ID:
            return
        source = str(payload.get("source", source))
        crusher_name = (
            payload.get("crusher_name")
            or payload.get("crusher_id")
            or crusher_name
        )

    if crusher_name is None:
        LOG.warning("Could not parse crusher from topic %s", topic)
        return

    _apply_destination(sim, str(crusher_name), source)


def _handle_truck_command(sim: SimState, payload: dict | None) -> None:
    if not payload:
        return
    payload_truck = payload.get("truck_id")
    if payload_truck is not None and str(payload_truck) != TRUCK_ID:
        return

    action = str(payload.get("action", "")).lower()
    reason = str(payload.get("reason", "command"))

    with sim._lock:
        if action == "stop":
            if sim.state == TruckState.STOPPED:
                return
            if sim.state != TruckState.HAULING:
                LOG.debug(
                    "Ignoring stop while %s (only hauling trucks can be held)",
                    sim.state.value,
                )
                return
            sim.paused_from_state = TruckState.HAULING
            sim.state = TruckState.STOPPED
            sim.haul_hold = reason.startswith("manual")
            sim.stop_reason = reason
            LOG.info(
                "Haul hold (reason=%s, manual=%s)",
                reason,
                sim.haul_hold,
            )
        elif action in ("resume", "clear"):
            if sim.state != TruckState.STOPPED:
                return
            if sim.haul_hold and not reason.startswith("manual"):
                LOG.info(
                    "Ignoring auto-resume while manual haul hold active (reason=%s)",
                    reason,
                )
                return
            resume_state = sim.paused_from_state or TruckState.HAULING
            sim.state = resume_state
            sim.paused_from_state = None
            sim.haul_hold = False
            sim.stop_reason = None
            LOG.info("Resumed to %s (reason=%s)", resume_state.value, reason)
        else:
            LOG.warning("Unknown truck command action: %s", action)


def _kmh_to_m_per_tick(speed_kmh: float, tick_sec: float) -> float:
    return (speed_kmh * 1000.0 / 3600.0) * tick_sec


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _move_toward(
    x: float,
    y: float,
    target: tuple[float, float],
    step_m: float,
) -> tuple[float, float, float]:
    dx = target[0] - x
    dy = target[1] - y
    dist = math.hypot(dx, dy)
    if dist <= step_m or dist == 0:
        return target[0], target[1], 1.0
    ratio = step_m / dist
    new_x = x + dx * ratio
    new_y = y + dy * ratio
    travelled = step_m / dist
    return new_x, new_y, travelled


def _current_speed_kmh(sim: SimState) -> float:
    if sim.state == TruckState.STOPPED:
        return 0.0
    if sim.state in (TruckState.HAULING, TruckState.RETURNING):
        return HAUL_SPEED_KMH if sim.state == TruckState.HAULING else RETURN_SPEED_KMH
    return 0.0


def _advance(sim: SimState) -> None:
    with sim._lock:
        if sim.state == TruckState.STOPPED:
            return

        crusher = sim.destination_crusher
        crusher_pos = CRUSHERS.get(crusher, CRUSHERS["crusher-1"])

        if sim.state == TruckState.LOADING:
            sim.x, sim.y = LOADING_AREA
            sim.load_pct = min(100.0, sim.load_pct + LOAD_RATE_PCT_PER_TICK)
            if sim.load_pct >= 100.0:
                sim.dwell_ticks += 1
                if sim.dwell_ticks >= LOADING_DWELL_TICKS:
                    sim.state = TruckState.HAULING
                    sim.progress = 0.0
                    sim.dwell_ticks = 0
            return

        if sim.state == TruckState.HAULING:
            step = _kmh_to_m_per_tick(HAUL_SPEED_KMH, TICK_SEC)
            sim.x, sim.y, travelled = _move_toward(sim.x, sim.y, crusher_pos, step)
            sim.progress = min(1.0, sim.progress + travelled)
            if _distance((sim.x, sim.y), crusher_pos) < 1.0:
                sim.state = TruckState.DUMPING
                sim.progress = 1.0
                sim.dwell_ticks = 0
            return

        if sim.state == TruckState.DUMPING:
            sim.x, sim.y = crusher_pos
            sim.load_pct = max(0.0, sim.load_pct - DUMP_RATE_PCT_PER_TICK)
            if sim.load_pct <= 0.0:
                sim.dwell_ticks += 1
                if sim.dwell_ticks >= DUMPING_DWELL_TICKS:
                    sim.state = TruckState.RETURNING
                    sim.progress = 0.0
                    sim.dwell_ticks = 0
            return

        if sim.state == TruckState.RETURNING:
            step = _kmh_to_m_per_tick(RETURN_SPEED_KMH, TICK_SEC)
            sim.x, sim.y, travelled = _move_toward(sim.x, sim.y, LOADING_AREA, step)
            sim.progress = min(1.0, sim.progress + travelled)
            if _distance((sim.x, sim.y), LOADING_AREA) < 1.0:
                sim.state = TruckState.LOADING
                sim.progress = 0.0
                sim.dwell_ticks = 0


def _telemetry_payload(sim: SimState) -> dict:
    with sim._lock:
        payload = {
            "truck_id": TRUCK_ID,
            "state": sim.state.value,
            "lat": round(sim.y / 111_000.0, 6),
            "lon": round(sim.x / (111_000.0 * math.cos(math.radians(sim.y / 111_000.0))), 6),
            "position_x": round(sim.x, 2),
            "position_y": round(sim.y, 2),
            "progress": round(sim.progress, 4),
            "speed_kmh": round(_current_speed_kmh(sim), 2),
            "load_pct": round(sim.load_pct, 2),
            "destination_crusher": sim.destination_crusher,
            "assignment_source": sim.assignment_source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if sim.state == TruckState.STOPPED and sim.paused_from_state:
            payload["paused_from_state"] = sim.paused_from_state.value
        if sim.stop_reason:
            payload["stop_reason"] = sim.stop_reason
        if sim.haul_hold:
            payload["haul_hold"] = True
        return payload


def main() -> None:
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    sim = SimState(destination_crusher=DEFAULT_CRUSHER, assignment_source="bootstrap")
    destination_subscribe = f"{NEW_DESTINATION_TOPIC}/{TRUCK_ID}/+"
    command_subscribe = f"{TOPIC_PREFIX}/{TRUCK_ID}/command"
    telemetry_topic = f"{TOPIC_PREFIX}/{TRUCK_ID}/telemetry"

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"truck-{TRUCK_ID}")

    def on_connect(
        client: mqtt.Client,
        userdata: object,
        flags: dict,
        reason_code: int,
        properties: object,
    ) -> None:
        del userdata, flags, properties
        if reason_code == 0:
            client.subscribe(destination_subscribe, qos=1)
            client.subscribe(command_subscribe, qos=1)
            LOG.info("Subscribed to %s and %s", destination_subscribe, command_subscribe)
        else:
            LOG.error("MQTT connect failed with code %s", reason_code)

    def on_message(
        client: mqtt.Client,
        userdata: object,
        msg: mqtt.MQTTMessage,
    ) -> None:
        del client, userdata
        payload: dict | None = None
        if msg.payload:
            try:
                decoded = json.loads(msg.payload.decode("utf-8"))
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                LOG.debug("Non-JSON payload on %s", msg.topic)

        if msg.topic.startswith(f"{NEW_DESTINATION_TOPIC}/"):
            _handle_new_destination(sim, msg.topic, payload)
        elif msg.topic == command_subscribe:
            _handle_truck_command(sim, payload)

    client.on_connect = on_connect
    client.on_message = on_message

    LOG.info(
        "Truck %s starting: broker %s:%s telemetry=%s crusher=%s (bootstrap) tick %.1fs",
        TRUCK_ID,
        MQTT_HOST,
        MQTT_PORT,
        telemetry_topic,
        DEFAULT_CRUSHER,
        TICK_SEC,
    )

    while running[0]:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except OSError as exc:
            LOG.warning("MQTT connect failed (%s), retrying in 5s", exc)
            time.sleep(5)

    client.loop_start()
    try:
        while running[0]:
            _advance(sim)
            payload = _telemetry_payload(sim)
            client.publish(telemetry_topic, json.dumps(payload, separators=(",", ":")), qos=0)
            LOG.info(
                "Published %s state=%s load=%.0f%% crusher=%s pos=(%.0f, %.0f)",
                TRUCK_ID,
                payload["state"],
                payload["load_pct"],
                payload["destination_crusher"],
                payload["position_x"],
                payload["position_y"],
            )
            time.sleep(TICK_SEC)
    finally:
        client.loop_stop()
        client.disconnect()

    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
