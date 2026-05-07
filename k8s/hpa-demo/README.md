# HPA demo (OpenShift)

Small web app plus manifests that demonstrate **Horizontal Pod Autoscaling (HPA)** on OpenShift: a dashboard shows **Deployment / Pod** counts and **HPA CPU target vs current**, and a built-in **load generator** drives CPU so the autoscaler can scale the Deployment out and back.

---

## What you get

| Piece | Purpose |
|-------|--------|
| **FastAPI app** | Serves the UI, `/api/status` (reads Deployment, Pods, HPA in-namespace), `/stress` (CPU burn), `/api/health`. |
| **`deploy-openshift.yaml`** | Namespace, RBAC, Deployment, Service, **HPA** (`autoscaling/v2`, CPU averageUtilization). |
| **Binary build on cluster** | `Dockerfile` builds on OpenShift; image pushed to the internal registry. |

The UI polls **`/api/status` every 2s**. Use **Start load** to fire parallel **`/stress`** requests through the **Route** so traffic spreads across replicas as they appear.

---

## Prerequisites

- OpenShift CLI **`oc`**, logged in with rights to create projects, builds, and workloads.
- Cluster **metrics** for CPU (e.g. metrics-server / monitoring stack). **ROSA** and typical OpenShift installs provide this; without it, HPA shows **`<unknown>`** for CPU until metrics exist.

---

## Deploy

From this directory (`hpa-demo/`):

```bash
oc new-project hpa-demo
oc new-build --name=hpa-demo-app --strategy=docker --binary=true -n hpa-demo
oc start-build hpa-demo-app --from-dir=. --follow -n hpa-demo
oc apply -f deploy-openshift.yaml
oc expose svc/hpa-demo-app -n hpa-demo
oc get route hpa-demo-app -n hpa-demo
```

Open the **Route URL** in a browser (use **https** if http fails).

### Rebuild after code changes

```bash
oc project hpa-demo
oc start-build hpa-demo-app --from-dir=. --follow -n hpa-demo
oc rollout restart deployment/hpa-demo-app -n hpa-demo
```

---

## HPA behavior (this demo)

In **`deploy-openshift.yaml`**, the **`HorizontalPodAutoscaler`** scales **`Deployment/hpa-demo-app`**:

- **minReplicas:** 1  
- **maxReplicas:** 8  
- **CPU metric:** `averageUtilization: 35`

For **`type: Utilization`** / **`resource.name: cpu`**, that percentage is **relative to each Pod’s CPU request**, not the limit. This Deployment sets **`requests.cpu`** (and limits) so the metric is valid.

Scaling is **not instant**: allow roughly **30–90+ seconds** after load increases before replicas change, and again after load stops before scale-down.

---

## Troubleshooting

| Symptom | Things to check |
|--------|-------------------|
| HPA **`Targets`** shows **`unknown`** briefly | Wait until metrics pipeline is ready; `oc describe hpa -n hpa-demo`. |
| UI **`failed to fetch`** while load is running | Older builds could starve the server with CPU stress; current **`/stress`** yields so **`/api/status`** can still respond. Rebuild/redeploy if needed. |
| No scale-up | Confirm **`requests.cpu`** on the Pod template; increase load intensity or lower **`averageUtilization`** in the HPA if the bar is too high. |

---

## Files

| Path | Role |
|------|------|
| `app/main.py` | API and CPU stress endpoint. |
| `static/index.html` | Dashboard and load generator UI. |
| `Dockerfile` | Container image for OpenShift build. |
| `deploy-openshift.yaml` | Full OpenShift/Kubernetes manifests. |
