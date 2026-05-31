# Truck fleet on OpenShift (`truck-fleet` namespace)

MQTT-based haul truck demo: three simulated trucks publish telemetry; **mqtt-ingest** persists to PostgreSQL.

Destination routing is handled by **`fleet-integration`** — see **`../../docs/mining-fleet/fleet-integration/README.md`**.

## Architecture

```text
┌─────────────┐     fleet/trucks/TR*/telemetry     ┌─────────────┐
│ truck-tr1   │ ─────────────────────────────────►│             │
│ truck-tr2   │ ◄── new-destination/{id}/{crusher}│ mqtt-broker │
│ truck-tr3   │         (from fleet-integration)   │  :1883      │
└─────────────┘                                   └──────┬──────┘
                                                         │ subscribe
                                                         ▼
                                                  ┌─────────────┐
                                                  │ mqtt-ingest │
                                                  └──────┬──────┘
                                                         │ SQL
                                                         ▼
                                                  ┌─────────────┐
                                                  │ postgresql  │
                                                  └─────────────┘
```

## Apply order

```bash
oc apply -f openshift/truck-fleet/01-namespace.yaml
oc apply -f openshift/truck-fleet/02-configmaps-secrets.yaml
oc apply -f openshift/truck-fleet/03-mqtt-broker.yaml
oc apply -f openshift/truck-fleet/04-postgresql.yaml
oc apply -f openshift/truck-fleet/05-buildconfigs.yaml
oc start-build truck-agent mqtt-ingest -n truck-fleet --wait
oc apply -f openshift/truck-fleet/06-truck-agents.yaml
oc apply -f openshift/truck-fleet/07-mqtt-ingest.yaml
```

Then deploy **`openshift/fleet-integration/`** for Kafka-based destination routing.

**Note:** BuildConfigs pull from `https://github.com/SimonDelord/alleo-work.git` on `main`. Push this repo first, or edit `git.uri` in `05-buildconfigs.yaml` to your fork.

## Verify

```bash
oc get pods -n truck-fleet
oc logs -n truck-fleet deploy/truck-tr1 --tail=5
oc logs -n truck-fleet deploy/mqtt-ingest --tail=10
```

## Files

| File | Purpose |
|------|---------|
| `01-namespace.yaml` | `truck-fleet` namespace |
| `02-configmaps-secrets.yaml` | Env ConfigMap, Postgres Secret, Mosquitto config |
| `03-mqtt-broker.yaml` | Eclipse Mosquitto Deployment + Service |
| `04-postgresql.yaml` | Postgres 16 PVC + Deployment + Service |
| `05-buildconfigs.yaml` | ImageStreams + BuildConfigs for app images |
| `06-truck-agents.yaml` | Deployments for TR1, TR2, TR3 |
| `07-mqtt-ingest.yaml` | mqtt-ingest Deployment |

Application source: **`../../poc/truck-fleet/`**. Design docs: **`../../docs/mining-fleet/truck-fleet/README.md`**.
