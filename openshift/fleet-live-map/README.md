# Fleet live map on OpenShift (`fleet-live-map` namespace)

Real-time mining fleet dashboard — observability/UI layer consuming **`mining-fleet-kafka`** topics only (no MQTT/Modbus writes).

## Apply order

```bash
oc apply -f openshift/fleet-live-map/
oc start-build fleet-live-map -n fleet-live-map --wait
```

BuildConfig pulls from `https://github.com/SimonDelord/alleo-work.git` on `main`. Push this repo before building from Git.

Local binary build without pushing:

```bash
oc start-build fleet-live-map --from-dir=poc/mining-fleet-live-map -n fleet-live-map --wait
```

## Verify

```bash
oc get pods -n fleet-live-map -l app=fleet-live-map
oc logs -n fleet-live-map deploy/fleet-live-map --tail=20
curl -s https://mining-fleet-live-map.apps.rosa.rosa-g74q8.ybzo.p3.openshiftapps.com/api/state | python3 -m json.tool | head -40
```

## Route

| Host | Service |
|------|---------|
| `mining-fleet-live-map.apps.rosa.rosa-g74q8.ybzo.p3.openshiftapps.com` | `fleet-live-map:8080` |

## Files

| File | Purpose |
|------|---------|
| `01-namespace.yaml` | Dedicated `fleet-live-map` namespace |
| `02-configmap.yaml` | Kafka bootstrap + topic names |
| `03-buildconfig.yaml` | ImageStream + BuildConfig |
| `04-deployment.yaml` | Deployment |
| `05-service-route.yaml` | ClusterIP Service + edge Route |
