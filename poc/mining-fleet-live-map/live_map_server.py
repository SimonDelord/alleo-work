#!/usr/bin/env python3
"""HTTP server for the mining fleet live map (Kafka → JSON API + Leaflet UI)."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from kafka_commands import KafkaCommandProducer
from kafka_state import FleetLiveMapState, KafkaStateConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("live_map")

PORT = int(os.environ.get("LIVE_MAP_PORT", "8080"))
MAP_HTML = os.path.join(os.path.dirname(__file__), "static", "index.html")

STATE = FleetLiveMapState()
KAFKA = KafkaStateConsumer(STATE)
COMMANDS = KafkaCommandProducer()


class MapHandler(BaseHTTPRequestHandler):
    server_version = "MiningFleetLiveMap/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        if self.path.startswith("/api/"):
            return
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, payload: object) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/api/state", "/api/positions"):
            try:
                self._send_json(200, STATE.build_api_state())
            except Exception as exc:
                LOG.exception("API error")
                self._send_json(500, {"error": str(exc)})
            return
        if path in ("/", "/map", "/index.html"):
            try:
                with open(MAP_HTML, encoding="utf-8") as f:
                    self._send_html(200, f.read())
            except OSError as exc:
                self._send_html(500, f"<pre>Map UI missing: {exc}</pre>")
            return
        if path == "/healthz":
            self._send_json(200, {"status": "ok", "kafka": STATE.build_api_state().get("kafka_connected")})
            return
        if path == "/api/command":
            self._send_json(
                200,
                {
                    "endpoint": "/api/command",
                    "method": "POST",
                    "actions": [
                        "reroute",
                        "stop",
                        "resume",
                        "stop_fleet",
                        "resume_fleet",
                        "acknowledge",
                    ],
                    "examples": {
                        "reroute": {
                            "action": "reroute",
                            "truck_id": "TR1",
                            "crusher_name": "crusher-2",
                            "reason": "manual_reroute",
                        },
                        "stop_fleet": {"action": "stop_fleet", "reason": "manual_stop_fleet"},
                    },
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/command":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            result = COMMANDS.dispatch(body)
            if body.get("action") == "acknowledge":
                STATE.apply_acknowledgement(body)
            self._send_json(200, result)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            LOG.exception("Command dispatch failed")
            self._send_json(500, {"error": str(exc)})


def main() -> None:
    if not os.path.isfile(MAP_HTML):
        raise SystemExit(f"Missing {MAP_HTML}")

    def _stop(*_: object) -> None:
        LOG.info("Stop requested")
        KAFKA.stop()
        COMMANDS.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    KAFKA.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), MapHandler)
    LOG.info(
        "Mining fleet live map on http://0.0.0.0:%s/ (Kafka → /api/state, POST /api/command)",
        PORT,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
