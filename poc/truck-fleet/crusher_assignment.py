#!/usr/bin/env python3
"""
Publishes truck→crusher assignments to MQTT from a Kubernetes ConfigMap.

Phase 1: bootstrap static allocation from ConfigMap truck-crusher-assignments.
Phase 2: same service can run in crusher-fleet and publish based on capacity.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
import yaml
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("crusher_assignment")

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
ASSIGNMENT_TOPIC = os.environ.get("MQTT_ASSIGNMENT_TOPIC", "fleet/crushers/assignments")
TRUCK_ASSIGNMENT_PREFIX = os.environ.get(
    "MQTT_TRUCK_ASSIGNMENT_PREFIX", "fleet/trucks"
)
CONFIGMAP_NAME = os.environ.get("ASSIGNMENTS_CONFIGMAP", "truck-crusher-assignments")
CONFIGMAP_KEY = os.environ.get("ASSIGNMENTS_CONFIGMAP_KEY", "assignments.yaml")
NAMESPACE = os.environ.get("POD_NAMESPACE", "truck-fleet")
SOURCE = os.environ.get("ASSIGNMENT_SOURCE", "crusher-fleet")
WATCH_TIMEOUT_SEC = int(os.environ.get("CONFIGMAP_WATCH_TIMEOUT_SEC", "300"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_assignments(raw: str) -> dict[str, str]:
    data = yaml.safe_load(raw) or {}
    assignments = data.get("assignments", data)
    if not isinstance(assignments, dict):
        raise ValueError("assignments must be a mapping of truck_id -> crusher_id")
    return {str(truck_id): str(crusher_id) for truck_id, crusher_id in assignments.items()}


def _load_assignments_from_configmap(v1: client.CoreV1Api) -> dict[str, str]:
    cm = v1.read_namespaced_config_map(CONFIGMAP_NAME, NAMESPACE)
    raw = cm.data.get(CONFIGMAP_KEY, "")
    if not raw.strip():
        raise ValueError(
            f"ConfigMap {NAMESPACE}/{CONFIGMAP_NAME} key {CONFIGMAP_KEY} is empty"
        )
    return _parse_assignments(raw)


class AssignmentPublisher:
    def __init__(self) -> None:
        self._running = True
        self._last_payload: str | None = None
        self._mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="crusher-assignment"
        )

    def stop(self) -> None:
        self._running = False

    def connect_mqtt(self) -> None:
        while self._running:
            try:
                self._mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                LOG.info("Connected to MQTT %s:%s", MQTT_HOST, MQTT_PORT)
                return
            except OSError as exc:
                LOG.warning("MQTT connect failed (%s), retrying in 5s", exc)
                time.sleep(5)

    def publish_assignments(self, assignments: dict[str, str]) -> None:
        assigned_at = _now_iso()
        broadcast: dict[str, Any] = {
            "assignments": assignments,
            "assigned_at": assigned_at,
            "source": SOURCE,
        }
        payload = json.dumps(broadcast, separators=(",", ":"))
        if payload == self._last_payload:
            LOG.debug("Assignments unchanged, skipping publish")
            return

        self._mqtt.publish(ASSIGNMENT_TOPIC, payload, qos=1, retain=True)
        LOG.info(
            "Published broadcast to %s: %s",
            ASSIGNMENT_TOPIC,
            ", ".join(f"{k}→{v}" for k, v in sorted(assignments.items())),
        )

        for truck_id, crusher_id in assignments.items():
            individual = {
                "truck_id": truck_id,
                "crusher_id": crusher_id,
                "assigned_at": assigned_at,
                "source": SOURCE,
            }
            topic = f"{TRUCK_ASSIGNMENT_PREFIX}/{truck_id}/assignment"
            self._mqtt.publish(
                topic,
                json.dumps(individual, separators=(",", ":")),
                qos=1,
                retain=True,
            )
            LOG.info("Published %s → %s on %s", truck_id, crusher_id, topic)

        self._last_payload = payload

    def run(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
            LOG.info("Using local kubeconfig (dev mode)")

        v1 = client.CoreV1Api()
        self.connect_mqtt()
        self._mqtt.loop_start()

        try:
            assignments = _load_assignments_from_configmap(v1)
            self.publish_assignments(assignments)
        except (ApiException, ValueError) as exc:
            LOG.error("Initial ConfigMap load failed: %s", exc)
            raise

        w = watch.Watch()
        LOG.info(
            "Watching ConfigMap %s/%s for assignment changes",
            NAMESPACE,
            CONFIGMAP_NAME,
        )
        while self._running:
            try:
                for event in w.stream(
                    v1.list_namespaced_config_map,
                    namespace=NAMESPACE,
                    field_selector=f"metadata.name={CONFIGMAP_NAME}",
                    timeout_seconds=WATCH_TIMEOUT_SEC,
                ):
                    if not self._running:
                        break
                    if event["type"] not in ("ADDED", "MODIFIED"):
                        continue
                    cm = event["object"]
                    raw = (cm.data or {}).get(CONFIGMAP_KEY, "")
                    if not raw.strip():
                        LOG.warning("ConfigMap update ignored: empty %s", CONFIGMAP_KEY)
                        continue
                    try:
                        assignments = _parse_assignments(raw)
                        self.publish_assignments(assignments)
                    except ValueError as exc:
                        LOG.error("Invalid assignments in ConfigMap: %s", exc)
            except ApiException as exc:
                if not self._running:
                    break
                LOG.warning("ConfigMap watch error (%s), retrying in 5s", exc)
                time.sleep(5)

        w.stop()
        self._mqtt.loop_stop()
        self._mqtt.disconnect()


def main() -> None:
    publisher = AssignmentPublisher()

    def _stop(*_: object) -> None:
        publisher.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        publisher.run()
    finally:
        publisher.stop()
        LOG.info("Stop requested, exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
