# Fleet integration on OpenShift (`fleet-integration` namespace)

Kafka orchestration for truck destination routing. See design docs: **`../../docs/mining-fleet/fleet-integration/README.md`**.

## Apply order

```bash
# Prerequisites: truck-fleet running; Kafka/AMQ Streams optional (services retry until available)
oc apply -f openshift/fleet-integration/01-namespace.yaml
oc apply -f openshift/fleet-integration/02-configmaps.yaml
oc apply -n kafka-demo -f openshift/fleet-integration/03-kafka-topics.yaml   # skip if no Kafka
oc apply -f openshift/fleet-integration/04-buildconfigs.yaml
oc start-build kafka-truck-bridge destination-router mqtt-routing-bridge crusher-state-producer \
  -n fleet-integration --wait
oc apply -f openshift/fleet-integration/05-kafka-truck-bridge.yaml \
         -f openshift/fleet-integration/06-crusher-state-producer.yaml \
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
oc logs -n fleet-integration deploy/destination-router --tail=15
oc logs -n fleet-integration deploy/mqtt-routing-bridge --tail=15
oc logs -n truck-fleet deploy/truck-tr1 --tail=5
```

## Files

| File | Purpose |
|------|---------|
| `01-namespace.yaml` | `fleet-integration` namespace |
| `02-configmaps.yaml` | Env ConfigMap + demo crusher state |
| `03-kafka-topics.yaml` | Strimzi topics `fleet.*` (apply in `kafka-demo`) |
| `04-buildconfigs.yaml` | ImageStreams + BuildConfigs |
| `05-kafka-truck-bridge.yaml` | MQTT → Kafka telemetry bridge |
| `06-crusher-state-producer.yaml` | Demo crusher state publisher |
| `07-destination-router.yaml` | Routing intelligence |
| `08-mqtt-routing-bridge.yaml` | Kafka → MQTT new-destination bridge |

Application source: **`../../poc/fleet-integration/`**.
