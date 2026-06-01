"""Aggregate mining fleet live-map state from Kafka topics."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable

LOG = logging.getLogger("kafka_state")

BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    "mining-fleet-cluster-kafka-bootstrap.mining-fleet-kafka.svc:9092",
)
TOPIC_TRUCK_TELEMETRY = os.environ.get("KAFKA_TOPIC_TRUCK_TELEMETRY", "fleet.trucks.telemetry")
TOPIC_CRUSHER_STATE = os.environ.get("KAFKA_TOPIC_CRUSHER_STATE", "fleet.crushers.state")
TOPIC_ROUTING_COMMANDS = os.environ.get("KAFKA_TOPIC_ROUTING_COMMANDS", "fleet.routing.commands")
CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "mining-fleet-live-map")
TRAIL_MAX_POINTS = int(os.environ.get("LIVE_MAP_TRAIL_MAX_POINTS", "500"))
EXCEPTION_LIMIT = int(os.environ.get("LIVE_MAP_EXCEPTION_LIMIT", "20"))

# Mine layout — matches truck_agent.py demo coordinates mapped to Leaflet lat/lon.
LOADING = {"site_id": "loading-area", "name": "Loading area", "lat": -23.360, "lon": 119.710, "site_type": "loading"}
CRUSHER_SITES = {
    "crusher-1": {
        "bay_id": "bay-1",
        "name": "Crusher North",
        "site_id": "crusher-north",
        "lat": -23.350,
        "lon": 119.720,
    },
    "crusher-2": {
        "bay_id": "bay-2",
        "name": "Crusher South",
        "site_id": "crusher-south",
        "lat": -23.365,
        "lon": 119.735,
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(raw: bytes | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _crusher_status_label(status: str | None, fill_pct: int | None, at_capacity: bool | None) -> str:
    pct = fill_pct or 0
    if at_capacity or pct >= 90 or status in ("full", "occupied"):
        return "RED"
    return "GREEN"


def _crusher_map_status(fill_pct: int | None) -> str:
    pct = fill_pct or 0
    if pct >= 90:
        return "full"
    if pct > 0:
        return "partial"
    return "empty"


def _load_state(truck_state: str | None, load_pct: float | None) -> str:
    if truck_state == "dumping":
        return "dumping"
    if truck_state == "loading":
        return "waiting"
    if load_pct is not None and load_pct >= 50:
        return "full"
    return "empty"


def _payload_state(load_pct: float | None) -> str:
    if load_pct is not None and load_pct >= 50:
        return "full"
    return "empty"


def _mission_state(truck_state: str | None) -> str:
    return truck_state or "unknown"


def _route_phase(truck_state: str | None) -> str:
    mapping = {
        "hauling": "en_route",
        "returning": "returning",
        "dumping": "dumping",
        "loading": "loading",
    }
    return mapping.get(truck_state or "", truck_state or "")


def _mine_xy_to_latlon(x: float, y: float) -> tuple[float, float]:
    """Map truck_agent meter coordinates to Leaflet demo lat/lon (index.html sites)."""
    lon = 119.710 + (x + 1200.0) / 2000.0 * 0.010
    if y >= 0:
        lat = -23.360 + (y / 400.0) * 0.010
    else:
        lat = -23.360 + (y / 400.0) * 0.005
    return round(lat, 6), round(lon, 6)


def _truck_latlon(payload: dict[str, Any]) -> tuple[float, float]:
    if payload.get("position_x") is not None and payload.get("position_y") is not None:
        return _mine_xy_to_latlon(float(payload["position_x"]), float(payload["position_y"]))
    return LOADING["lat"], LOADING["lon"]


class FleetLiveMapState:
    """Thread-safe in-memory state built from Kafka consumer updates."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trucks: dict[str, dict[str, Any]] = {}
        self._crushers: dict[str, dict[str, Any]] = {}
        self._trails: dict[str, deque[list[float]]] = {}
        self._exceptions: deque[dict[str, Any]] = deque(maxlen=EXCEPTION_LIMIT)
        self._kafka_connected = False
        self._last_kafka_message_at: str | None = None

    def apply_truck_telemetry(self, payload: dict[str, Any]) -> None:
        truck_id = str(payload.get("truck_id", ""))
        if not truck_id:
            return

        lat, lon = _truck_latlon(payload)
        state = str(payload.get("state", "unknown"))
        load_pct = float(payload.get("load_pct", 0))
        destination = payload.get("destination_crusher")
        crusher_meta = CRUSHER_SITES.get(str(destination), {}) if destination else {}

        with self._lock:
            trail = self._trails.setdefault(truck_id, deque(maxlen=TRAIL_MAX_POINTS))
            if not trail or trail[-1] != [lat, lon]:
                trail.append([lat, lon])

            self._trucks[truck_id] = {
                "truck_id": truck_id,
                "display_name": truck_id,
                "lat": lat,
                "lon": lon,
                "payload_state": _payload_state(load_pct),
                "overload_status": False,
                "mission_state": _mission_state(state),
                "assigned_bay_id": crusher_meta.get("bay_id"),
                "target_site_id": crusher_meta.get("site_id"),
                "route_phase": _route_phase(state),
                "target_site_name": crusher_meta.get("name"),
                "target_lat": crusher_meta.get("lat"),
                "target_lon": crusher_meta.get("lon"),
                "assigned_crusher_name": crusher_meta.get("name"),
                "destination_crusher": destination,
                "load_pct": load_pct,
                "speed_kmh": payload.get("speed_kmh"),
                "updated_at": payload.get("timestamp") or _now_iso(),
                "source": payload.get("source", "kafka"),
            }
            self._last_kafka_message_at = _now_iso()

    def apply_crusher_state(self, payload: dict[str, Any]) -> None:
        crusher_name = str(payload.get("crusher_name", ""))
        if crusher_name not in CRUSHER_SITES:
            return

        fill_pct = int(round(float(payload.get("fill_pct", 0))))
        status = str(payload.get("status", "unknown"))
        at_capacity = bool(payload.get("at_capacity", False))
        meta = CRUSHER_SITES[crusher_name]

        dump_count = int(payload.get("dump_count", 0))

        with self._lock:
            self._crushers[crusher_name] = {
                "bay_id": meta["bay_id"],
                "name": meta["name"],
                "lat": meta["lat"],
                "lon": meta["lon"],
                "status": "occupied" if at_capacity or fill_pct >= 90 else status,
                "fill_pct": fill_pct,
                "map_status": _crusher_map_status(fill_pct),
                "status_label": _crusher_status_label(status, fill_pct, at_capacity),
                "at_capacity": at_capacity,
                "dump_count": dump_count,
                "updated_at": payload.get("updated_at") or _now_iso(),
            }
            self._last_kafka_message_at = _now_iso()

    def apply_routing_command(self, payload: dict[str, Any]) -> None:
        truck_id = str(payload.get("truck_id", ""))
        crusher_name = str(payload.get("crusher_name", ""))
        reason = str(payload.get("reason", "reroute"))
        crusher_meta = CRUSHER_SITES.get(crusher_name, {})

        severity = "warning" if "capacity" in reason or "full" in reason else "info"
        crusher_label = crusher_meta.get("name", crusher_name)
        message = f"{truck_id} re-routed to {crusher_label} ({reason.replace('_', ' ')})"

        with self._lock:
            self._exceptions.appendleft(
                {
                    "created_at": payload.get("decided_at") or _now_iso(),
                    "truck_id": truck_id,
                    "bay_id": crusher_meta.get("bay_id"),
                    "severity": severity,
                    "message": message,
                    "reason": reason,
                    "source": payload.get("source", "destination-router"),
                }
            )
            self._last_kafka_message_at = _now_iso()

    def set_kafka_connected(self, connected: bool) -> None:
        with self._lock:
            self._kafka_connected = connected

    def build_api_state(self) -> dict[str, Any]:
        with self._lock:
            trucks_raw = list(self._trucks.values())
            crushers = [self._crushers.get(k, self._default_crusher(k)) for k in CRUSHER_SITES]
            exceptions = list(self._exceptions)
            trails = {tid: list(trail) for tid, trail in self._trails.items()}
            kafka_connected = self._kafka_connected
            last_msg = self._last_kafka_message_at

        trucks = []
        for t in trucks_raw:
            row = dict(t)
            row["load_state"] = _load_state(row.get("mission_state"), row.get("load_pct"))
            row["overload_label"] = "OK"
            crusher = next(
                (c for c in crushers if c["bay_id"] == row.get("assigned_bay_id")),
                None,
            )
            fill = crusher["fill_pct"] if crusher else 0
            row["crusher_fill_pct"] = fill
            row["crusher_status"] = crusher["status"] if crusher else None
            row["crusher_fill"] = f"{fill}%"
            row["crusher_status_label"] = crusher["status_label"] if crusher else "GREEN"
            row["trail"] = trails.get(row["truck_id"], [])
            trucks.append(row)

        routing = []
        for t in trucks:
            routing.append(
                {
                    "truck": t["display_name"],
                    "mission": t.get("mission_state", "—"),
                    "target_site": t.get("target_site_name") or "Loading area",
                    "payload": t.get("payload_state", "—"),
                    "crusher_fill_pct": t.get("crusher_fill_pct", 0),
                    "crusher_fill": t.get("crusher_fill", "0%"),
                    "crusher_status_raw": t.get("crusher_status"),
                    "overload_status": False,
                    "crusher_status": t.get("crusher_status_label", "GREEN"),
                    "overload": "OK",
                }
            )

        north = next((c for c in crushers if c["bay_id"] == "bay-1"), self._default_crusher("crusher-1"))
        south = next((c for c in crushers if c["bay_id"] == "bay-2"), self._default_crusher("crusher-2"))

        title = "Mining Fleet — Trucks & Crushers (Kafka)"
        if not kafka_connected:
            title += " · Kafka reconnecting…"

        return {
            "generated_at": _now_iso(),
            "dashboard_title": title,
            "kafka_connected": kafka_connected,
            "last_kafka_message_at": last_msg,
            "sites": [LOADING, north, south],
            "crushers": crushers,
            "crusher_north": north,
            "crusher_south": south,
            "trucks": sorted(trucks, key=lambda t: t["display_name"]),
            "routing": routing,
            "exceptions": exceptions,
        }

    @staticmethod
    def _default_crusher(crusher_name: str) -> dict[str, Any]:
        meta = CRUSHER_SITES[crusher_name]
        return {
            "bay_id": meta["bay_id"],
            "name": meta["name"],
            "lat": meta["lat"],
            "lon": meta["lon"],
            "status": "available",
            "fill_pct": 0,
            "map_status": "empty",
            "status_label": "GREEN",
            "at_capacity": False,
            "dump_count": 0,
        }


def _topics() -> list[str]:
    return [TOPIC_TRUCK_TELEMETRY, TOPIC_CRUSHER_STATE, TOPIC_ROUTING_COMMANDS]


class KafkaStateConsumer:
    """Background Kafka consumer updating shared FleetLiveMapState."""

    def __init__(self, state: FleetLiveMapState) -> None:
        self._state = state
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="kafka-consumer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        while self._running:
            try:
                consumer = KafkaConsumer(
                    *_topics(),
                    bootstrap_servers=BOOTSTRAP.split(","),
                    group_id=CONSUMER_GROUP,
                    auto_offset_reset="latest",
                    enable_auto_commit=True,
                    value_deserializer=lambda v: v,
                )
                self._state.set_kafka_connected(True)
                LOG.info("Kafka consumer connected to %s topics=%s", BOOTSTRAP, _topics())
                for msg in consumer:
                    if not self._running:
                        break
                    payload = _parse_json(msg.value)
                    if not payload:
                        continue
                    topic = msg.topic
                    if topic == TOPIC_TRUCK_TELEMETRY:
                        self._state.apply_truck_telemetry(payload)
                    elif topic == TOPIC_CRUSHER_STATE:
                        self._state.apply_crusher_state(payload)
                    elif topic == TOPIC_ROUTING_COMMANDS:
                        self._state.apply_routing_command(payload)
                consumer.close()
            except NoBrokersAvailable as exc:
                self._state.set_kafka_connected(False)
                LOG.warning("Kafka unavailable (%s), retrying in 5s", exc)
                time.sleep(5)
            except Exception:
                self._state.set_kafka_connected(False)
                LOG.exception("Kafka consumer error, retrying in 5s")
                time.sleep(5)
