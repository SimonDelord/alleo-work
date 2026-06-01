# Mining Fleet Dedicated Kafka Stack (`mining-fleet-kafka`)

Dedicated AMQ Streams / Strimzi stack for the mining fleet demo, separated from `kafka-demo`.

## Resources

- Namespace: `mining-fleet-kafka`
- Kafka cluster: `mining-fleet-cluster` (`KafkaNodePool` `mining-fleet-pool`)
- Kafka Connect: `fleet-cdc-connect`
- Kafka console: `mining-fleet-console` (Streamshub Console operator)
- Debezium connectors:
  - `truck-postgres-source` (`postgresql.truck-fleet.svc:5432`, DB `truckfleet`)
  - `crusher-postgres-source` (`postgresql.crusher-fleet.svc:5432`, DB `crusherfleet`)

## Apply order

```bash
oc apply -f openshift/mining-fleet-kafka/01-namespace.yaml
oc apply -f openshift/mining-fleet-kafka/02-kafka-cluster.yaml
oc wait kafka/mining-fleet-cluster -n mining-fleet-kafka --for=condition=Ready --timeout=10m

oc apply -f openshift/mining-fleet-kafka/03-kafka-connect.yaml
oc wait kafkaconnect/fleet-cdc-connect -n mining-fleet-kafka --for=condition=Ready --timeout=10m

oc apply -f openshift/mining-fleet-kafka/04-kafka-topics.yaml
oc apply -f openshift/mining-fleet-kafka/05-debezium-connectors.yaml
oc apply -f openshift/mining-fleet-kafka/06-kafka-console.yaml
oc wait console.console.streamshub.github.com/mining-fleet-console -n mining-fleet-kafka --for=condition=Ready --timeout=10m
oc wait kafkaconnector/truck-postgres-source -n mining-fleet-kafka --for=condition=Ready --timeout=10m
oc wait kafkaconnector/crusher-postgres-source -n mining-fleet-kafka --for=condition=Ready --timeout=10m
```

## Verify

```bash
oc get ns mining-fleet-kafka
oc get kafka -n mining-fleet-kafka
oc get kafkaconnect -n mining-fleet-kafka
oc get kafkaconnector -n mining-fleet-kafka
oc get console.console.streamshub.github.com -n mining-fleet-kafka
oc get route -n mining-fleet-kafka
oc describe kafkaconnector truck-postgres-source -n mining-fleet-kafka
oc describe kafkaconnector crusher-postgres-source -n mining-fleet-kafka
```

Access URL:

```bash
oc get route -n mining-fleet-kafka -l app.kubernetes.io/name=console -o jsonpath='{.items[0].spec.host}{"\n"}'
```

## Notes

- This stack is independent from `kafka-demo` and does not modify existing `my-cluster` resources.
- Connector credentials are stored in namespace-local secrets consumed via Strimzi `KubernetesSecretConfigProvider`.
- `05-debezium-connectors.yaml` includes Role/RoleBinding `fleet-cdc-connect-secret-reader` so the Connect service account can read connector secrets.
- AMQ Streams operator must watch both `kafka-demo` and `mining-fleet-kafka` namespaces (OperatorGroup `amq-streams-og`).
