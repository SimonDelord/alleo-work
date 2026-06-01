# Mining fleet live map

Real-time operational dashboard for the **mining fleet demo**, wired to the dedicated **`mining-fleet-kafka`** cluster (AMQ Streams / Strimzi). Visual layout matches the existing [fleet-live-map](https://fleet-live-map.apps.rosa.rosa-g74q8.ybzo.p3.openshiftapps.com/) PostgreSQL dashboard in `mine-ops`, but data comes from Kafka orchestration topics instead of Postgres.

Deployed in the dedicated **`fleet-live-map`** namespace (observability/UI layer — reads Kafka only, separate from `fleet-integration` orchestration and `mining-fleet-kafka` infra).

Parent: [fleet-integration README](../fleet-integration/README.md) · [mining fleet overview](../README.md)

---

## Architecture

```text
truck-fleet MQTT          fleet-integration bridges              mining-fleet-kafka
  truck agents    →   kafka-truck-bridge  →  fleet.trucks.telemetry  ──┐
  (TR1–TR3)           crusher-fill-bridge →  fleet.crushers.state    ──┼──► fleet-live-map
                      destination-router  →  fleet.routing.commands  ──┘         │
                                                                                 ▼
                                                                         Leaflet map UI
                                                                         (poll /api/state)
```

| Layer | Component | Role |
|-------|-----------|------|
| **Produce** | `kafka-truck-bridge` | MQTT truck telemetry → `fleet.trucks.telemetry` |
| **Produce** | `crusher-fill-bridge` | Modbus fill updates → `fleet.crushers.state` |
| **Produce** | `destination-router` | Reroute decisions → `fleet.routing.commands` |
| **Consume** | `fleet-live-map` | Aggregates topics → JSON API + map UI |
| **Optional CDC** | Debezium | `fleet.truckdb.*`, `fleet.crusherdb.*` (not used by live map v1) |

Bootstrap server (in-cluster): `mining-fleet-cluster-kafka-bootstrap.mining-fleet-kafka.svc:9092`

---

## Kafka topics consumed

| Topic | Producer | Key fields used |
|-------|----------|-----------------|
| `fleet.trucks.telemetry` | `kafka-truck-bridge` | `truck_id`, `state`, `lat`/`lon` or `position_x`/`position_y`, `load_pct`, `destination_crusher`, `speed_kmh`, `timestamp` |
| `fleet.crushers.state` | `crusher-fill-bridge` | `crusher_name`, `status`, `fill_pct`, `at_capacity`, `updated_at` |
| `fleet.routing.commands` | `destination-router`, `fleet-live-map` | `truck_id`, `crusher_name`, `reason`, `decided_at` |
| `fleet.truck.commands` | `destination-router`, `fleet-live-map` | `truck_id`, `action` (`stop`/`resume`), `reason`, `decided_at` |

Routing commands populate the **Controller exceptions** panel (reroute reasons such as `crusher-1_at_capacity`).

## Operator commands (POST /api/command)

The live map can publish manual commands to Kafka:

```bash
# Manual reroute
curl -s -X POST https://mining-fleet-live-map.apps.rosa.rosa-g74q8.ybzo.p3.openshiftapps.com/api/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"reroute","truck_id":"TR1","crusher_name":"crusher-2","reason":"manual_reroute"}'

# Stop all trucks
curl -s -X POST .../api/command -H 'Content-Type: application/json' \
  -d '{"action":"stop_fleet","reason":"manual_stop_fleet"}'

# API schema
curl -s .../api/command
```

Actions: `reroute`, `stop`, `resume`, `stop_fleet`, `resume_fleet`, `acknowledge`.

Debezium CDC topics (`fleet.truckdb.public.truck_state`, `fleet.crusherdb.public.crusher_state`, etc.) are available for Phase 2 enrichment but are **not** consumed by this v1 dashboard.

---

## Repository layout

| Path | Contents |
|------|----------|
| [`poc/mining-fleet-live-map/`](../../poc/mining-fleet-live-map/) | `live_map_server.py`, `kafka_state.py`, Leaflet UI, Dockerfile |
| [`openshift/fleet-live-map/`](../../openshift/fleet-live-map/) | Namespace, ConfigMap, BuildConfig, Deployment, Service, Route |

---

## Deployment

Prerequisites: `truck-fleet`, `crusher-fleet`, `mining-fleet-kafka`, and `fleet-integration` bridges running with producers pointed at `mining-fleet-kafka`.

```bash
oc apply -f openshift/fleet-live-map/
oc start-build fleet-live-map -n fleet-live-map --wait
```

For a local binary build without pushing to GitHub:

```bash
oc start-build fleet-live-map --from-dir=poc/mining-fleet-live-map -n fleet-live-map --wait
```

### Route

**https://mining-fleet-live-map.apps.rosa.rosa-g74q8.ybzo.p3.openshiftapps.com/**

---

## Verify

```bash
# Pod + Kafka consumer logs
oc get pods -n fleet-live-map -l app=fleet-live-map
oc logs -n fleet-live-map deploy/fleet-live-map --tail=30

# API state (expect trucks, crushers, exceptions after demo runs ~30s)
curl -s https://mining-fleet-live-map.apps.rosa.rosa-g74q8.ybzo.p3.openshiftapps.com/api/state \
  | python3 -m json.tool | head -50

# Confirm fleet-integration producers use mining-fleet-kafka (not kafka-demo)
oc logs -n fleet-integration deploy/kafka-truck-bridge --tail=5
# Expect: mining-fleet-cluster-kafka-bootstrap.mining-fleet-kafka.svc:9092
```

Open the route in a browser: truck markers move on the mine map, crusher gauges show fill %, routing table updates, and reroute events appear in the exceptions panel when `destination-router` emits commands.

---

## Comparison with `mine-ops/fleet-live-map`

| | **mine-ops/fleet-live-map** | **fleet-live-map** (this app) |
|--|---------------------------|---------------------------|
| Data source | PostgreSQL (`mine-ops`) | Kafka (`mining-fleet-kafka`) |
| Namespace | `mine-ops` | `fleet-live-map` |
| Truck positions | SQL + position log | `fleet.trucks.telemetry` |
| Crusher fill | `overload_bays` table | `fleet.crushers.state` |
| Exceptions | `controller_exceptions` table | `fleet.routing.commands` |

---

## Future work

- Consume Debezium CDC topics as fallback when MQTT bridge is offline
- SSE/WebSocket push instead of 500 ms polling
- Operator action to acknowledge / stop rerouting (control plane — **POST /api/command** implemented)
