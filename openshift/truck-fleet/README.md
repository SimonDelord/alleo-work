# Truck fleet on OpenShift (`truck-fleet` namespace)

MQTT-based haul truck demo: three simulated trucks publish telemetry; **mqtt-ingest** persists to PostgreSQL.

## Architecture

```text
┌─────────────┐     fleet/trucks/TR*/telemetry     ┌─────────────┐
│ truck-tr1   │ ─────────────────────────────────►│             │
│ truck-tr2   │ ─────────────────────────────────►│ mqtt-broker │
│ truck-tr3   │ ─────────────────────────────────►│  :1883      │
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

Or apply all numbered manifests except builds first, then build, then trucks + ingest:

```bash
oc apply -f openshift/truck-fleet/01-namespace.yaml \
         -f openshift/truck-fleet/02-configmaps-secrets.yaml \
         -f openshift/truck-fleet/03-mqtt-broker.yaml \
         -f openshift/truck-fleet/04-postgresql.yaml
oc apply -f openshift/truck-fleet/05-buildconfigs.yaml
oc start-build truck-agent mqtt-ingest -n truck-fleet --wait
oc apply -f openshift/truck-fleet/06-truck-agents.yaml \
         -f openshift/truck-fleet/07-mqtt-ingest.yaml
```

**Note:** BuildConfigs pull from `https://github.com/SimonDelord/alleo-work.git` on `main`. Push this repo first, or edit `git.uri` in `05-buildconfigs.yaml` to your fork.

## Verify

```bash
oc get pods -n truck-fleet
oc logs -n truck-fleet deploy/truck-tr1 --tail=5
oc logs -n truck-fleet deploy/mqtt-ingest --tail=10
```

Query PostgreSQL (port-forward or `oc rsh`):

```bash
oc port-forward -n truck-fleet svc/postgresql 5432:5432
PGPASSWORD=truckfleet-demo psql -h localhost -U truckfleet -d truckfleet -c \
  "SELECT truck_id, state, load_pct, speed_kmh, destination_crusher, recorded_at
   FROM truck_state ORDER BY truck_id;"
```

History sample:

```sql
SELECT truck_id, state, load_pct, position_x, position_y, recorded_at
FROM truck_telemetry
ORDER BY recorded_at DESC
LIMIT 20;
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
