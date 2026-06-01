# Fleet integration on OpenShift (`fleet-integration` namespace)

Kafka orchestration for truck destination routing and crusher fill from truck dumps. See design docs: **`../../docs/mining-fleet/fleet-integration/README.md`**.

## Apply order

```bash
# Prerequisites: truck-fleet + crusher-fleet running; Kafka/AMQ Streams optional (services retry until available)
oc apply -f openshift/fleet-integration/01-namespace.yaml
oc apply -f openshift/fleet-integration/02-configmaps.yaml
oc apply -f openshift/fleet-integration/03-kafka-topics.yaml   # applies in mining-fleet-kafka namespace
oc apply -f openshift/fleet-integration/04-buildconfigs.yaml
oc start-build kafka-truck-bridge destination-router mqtt-routing-bridge crusher-fill-bridge \
  -n fleet-integration --wait
oc apply -f openshift/fleet-integration/05-kafka-truck-bridge.yaml \
         -f openshift/fleet-integration/09-crusher-fill-bridge.yaml \
         -f openshift/fleet-integration/07-destination-router.yaml \
         -f openshift/fleet-integration/08-mqtt-routing-bridge.yaml
```

Remove deprecated crusher-assignment from truck-fleet:

```bash
oc delete deployment crusher-assignment -n truck-fleet --ignore-not-found
```

## Verify

```bash
oc get pods -n fleet-integration
oc logs -n fleet-integration deploy/crusher-fill-bridge --tail=20
oc logs -n fleet-integration deploy/destination-router --tail=15
oc exec -n crusher-fleet deploy/postgresql -- \
  env PGPASSWORD=crusherfleet-demo psql -U crusherfleet -d crusherfleet \
  -c "SELECT crusher_id, fill_pct, dump_count, updated_at FROM crusher_state;"
```

## Files

| File | Purpose |
|------|---------|
| `01-namespace.yaml` | `fleet-integration` namespace |
| `02-configmaps.yaml` | Env ConfigMap + crusher Modbus targets |
| `03-kafka-topics.yaml` | Strimzi topics `fleet.*` (apply in `mining-fleet-kafka`) |
| `04-buildconfigs.yaml` | ImageStreams + BuildConfigs |
| `05-kafka-truck-bridge.yaml` | MQTT → Kafka telemetry bridge |
| `06-crusher-state-producer.yaml` | Deprecated mock (replicas=0) |
| `07-destination-router.yaml` | Routing intelligence |
| `08-mqtt-routing-bridge.yaml` | Kafka → MQTT new-destination bridge |
| `09-crusher-fill-bridge.yaml` | MQTT truck telemetry → crusher Modbus + Kafka state |

Application source: **`../../poc/fleet-integration/`**.
