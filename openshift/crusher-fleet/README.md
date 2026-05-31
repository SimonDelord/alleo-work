# Crusher fleet on OpenShift (`crusher-fleet` namespace)

Modbus TCP crusher PLCs with a plant historian that persists telemetry to PostgreSQL. Fully independent from `truck-fleet` and `fleet-integration` — no MQTT or Kafka in this namespace.

Phase 2 integration: `fleet-integration` can consume crusher state via a future Kafka CDC/bridge from this PostgreSQL (replacing the demo `crusher-state-producer`).

## Architecture

```text
┌─────────────┐         ┌─────────────┐
│ crusher-1   │         │ crusher-2   │
│  (Modbus)   │         │  (Modbus)   │
└──────┬──────┘         └──────┬──────┘
       │ Modbus poll           │ Modbus poll
       └───────────┬───────────┘
                   ▼
            ┌─────────────┐
            │  historian  │
            └──────┬──────┘
                   │ SQL
                   ▼
            ┌─────────────┐
            │ postgresql  │
            └─────────────┘
```

## Apply order

```bash
oc apply -f openshift/crusher-fleet/01-namespace.yaml
oc apply -f openshift/crusher-fleet/02-configmaps-secrets.yaml
oc apply -f openshift/crusher-fleet/03-postgresql.yaml
oc apply -f openshift/crusher-fleet/05-buildconfigs.yaml
oc start-build crusher-plc historian -n crusher-fleet --wait
oc apply -f openshift/crusher-fleet/04-crusher-plc.yaml
oc apply -f openshift/crusher-fleet/06-historian.yaml
```

**Note:** BuildConfigs pull from `https://github.com/SimonDelord/alleo-work.git` on `main`. Push this repo first, or edit `git.uri` in `05-buildconfigs.yaml` to your fork.

## Verify

```bash
oc get pods -n crusher-fleet
oc logs -n crusher-fleet deploy/historian --tail=10
oc logs -n crusher-fleet deploy/crusher-1 --tail=5

# Query latest crusher state
oc exec -n crusher-fleet deploy/postgresql -- \
  env PGPASSWORD=crusherfleet-demo psql -U crusherfleet -d crusherfleet \
  -c "SELECT * FROM crusher_state ORDER BY crusher_id;"
```

## Files

| File | Purpose |
|------|---------|
| `01-namespace.yaml` | `crusher-fleet` namespace |
| `02-configmaps-secrets.yaml` | Env ConfigMap, Postgres Secret |
| `03-postgresql.yaml` | Postgres 16 PVC + Deployment + Service |
| `04-crusher-plc.yaml` | crusher-1 and crusher-2 Modbus Deployments + Services |
| `05-buildconfigs.yaml` | ImageStreams + BuildConfigs for app images |
| `06-historian.yaml` | historian Deployment |

Application source: **`../../poc/crusher-fleet/`**. Design docs: **`../../docs/mining-fleet/crusher-fleet/README.md`**.
