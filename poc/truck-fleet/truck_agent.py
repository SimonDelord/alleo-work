#!/usr/bin/env python3
"""
Simulated haul truck: cycles loading → hauling → dumping → returning,
publishing JSON telemetry to MQTT on each tick.
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("truck_agent")

TRUCK_ID = os.environ.get("TRUCK_ID", "TR1")
MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TICK_SEC = float(os.environ.get("TICK_SEC", "2.0"))
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "fleet/trucks")

# Mine layout (arbitrary demo coordinates in meters).
LOADING_AREA = (-1200.0, 0.0)
CRUSHERS = {
    "crusher-1": (800.0, 400.0),
    "crusher-2": (800.0, -400.0),
}
TRUCK_CRUSHER = {
    "TR1": "crusher-1",
    "TR2": "crusher-2",
    "TR3": "crusher-1",
}
HAUL_SPEED_KMH = float(os.environ.get("HAUL_SPEED_KMH", "35"))
RETURN_SPEED_KMH = float(os.environ.get("RETURN_SPEED_KMH", "40"))
LOAD_RATE_PCT_PER_TICK = float(os.environ.get("LOAD_RATE_PCT_PER_TICK", "25"))
DUMP_RATE_PCT_PER_TICK = float(os.environ.get("DUMP_RATE_PCT_PER_TICK", "50"))
LOADING_DWELL_TICKS = int(os.environ.get("LOADING_DWELL_TICKS", "2"))
DUMPING_DWELL_TICKS = int(os.environ.get("DUMPING_DWELL_TICKS", "2"))


class TruckState(str, Enum):
    LOADING = "loading"
    HAULING = "hauling"
    DUMPING = "dumping"
    RETURNING = "returning"


@dataclass
class SimState:
    state: TruckState = TruckState.LOADING
    x: float = LOADING_AREA[0]
    y: float = LOADING_AREA[1]
    load_pct: float = 0.0
    progress: float = 0.0
    dwell_ticks: int = 0
    destination_crusher: str = "crusher-1"


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
    if sim.state in (TruckState.HAULING, TruckState.RETURNING):
        return HAUL_SPEED_KMH if sim.state == TruckState.HAULING else RETURN_SPEED_KMH
    return 0.0


def _advance(sim: SimState) -> None:
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
    return {
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
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    crusher = TRUCK_CRUSHER.get(TRUCK_ID, "crusher-1")
    sim = SimState(destination_crusher=crusher)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"truck-{TRUCK_ID}")
    topic = f"{TOPIC_PREFIX}/{TRUCK_ID}/telemetry"

    LOG.info(
        "Truck %s starting: broker %s:%s topic %s crusher %s tick %.1fs",
        TRUCK_ID,
        MQTT_HOST,
        MQTT_PORT,
        topic,
        crusher,
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
            client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=0)
            LOG.info(
                "Published %s state=%s load=%.0f%% pos=(%.0f, %.0f)",
                TRUCK_ID,
                sim.state.value,
                sim.load_pct,
                sim.x,
                sim.y,
            )
            time.sleep(TICK_SEC)
    finally:
        client.loop_stop()
        client.disconnect()

    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
