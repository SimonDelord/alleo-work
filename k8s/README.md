# Kubernetes demos (quota, HPA, observability)

This repository contains **hands-on demos** for OpenShift/Kubernetes: **`ResourceQuota`** / **`LimitRange`**, a **quota viewer** UI, an **HPA** demo app, and notes on **observability** (Prometheus/Grafana). Use it for internal walkthroughs or customer demos.

---

## Repository layout

| Path | Purpose |
|------|--------|
| [`quota-demo/`](quota-demo/) | Ordered YAML manifests: namespace, quota, limit range, and example workloads. |
| [`quota-viewer/`](quota-viewer/) | FastAPI app + Dockerfile + Kubernetes `deploy.yaml` (RBAC, Deployment, Service). |
| [`hpa-demo/`](hpa-demo/) | HPA demo: FastAPI UI + OpenShift manifests (`deploy-openshift.yaml`). |
| [`observability-and-logging/`](observability-and-logging/) | Notes on Prometheus and Grafana on OpenShift (this folder’s README). |

---

## Concepts (short)

- **`ResourceQuota`** — Caps **aggregate** consumption for a **namespace** (for example total CPU/memory **requests**, **limits**, **pod** count, **`count/deployments.apps`**, and so on). The scheduler and admission controller enforce these caps.
- **`LimitRange`** — Defines **defaults, minimums, and maximums per Pod/container** in that namespace. If a Pod omits `resources`, defaults may be applied; those requests still **count toward** the namespace `ResourceQuota`.

The demo keeps quota limits **intentionally tight** so failures show up quickly.

---

## Prerequisites

- A Kubernetes cluster and **`kubectl`** configured (`kubectl cluster-info`).
- **Docker** (or another builder) if you build the quota viewer image yourself.

---

## Part 1 — Apply the demo manifests

Apply files **in numeric order** so the namespace and policies exist before workloads.

```bash
kubectl apply -f quota-demo/01-namespace.yaml
kubectl apply -f quota-demo/02-resourcequota.yaml
kubectl apply -f quota-demo/03-limitrange.yaml
```

### What each manifest does

| File | Description |
|------|-------------|
| `01-namespace.yaml` | Creates the **`quota-demo`** namespace. |
| `02-resourcequota.yaml` | **`demo-quota`**: hard limits including **250m** CPU **requests**, memory requests/limits, **pods**, **`count/deployments.apps`**, etc. |
| `03-limitrange.yaml` | **`demo-limit-range`**: per-container **min / max / default / defaultRequest** (for example **100m** CPU default request when a container does not set resources). |
| `04-deployment-within-quota.yaml` | **`demo-app`** at **2** replicas with **100m** CPU request each → **200m** total requests (stays under **250m**). |
| `05-deployment-exceed-quota.yaml` | Same **`demo-app`** bumped to **3** replicas → **300m** CPU requests (**over** the **250m** quota). Expect Pending Pods and **Forbidden / quota exceeded** style messages in events. |
| `06-pod-defaults-from-limitrange.yaml` | **`demo-app-no-resources`**: Deployment **without** `resources`; the **LimitRange** injects **defaultRequest** values (inspect the Pod with **`kubectl describe pod`**). |

### Useful commands

```bash
kubectl describe resourcequota demo-quota -n quota-demo
kubectl get pods -n quota-demo
kubectl get events -n quota-demo --sort-by='.lastTimestamp'
```

### Important: do not combine conflicting demos blindly

Running **`04-deployment-within-quota.yaml`** (two replicas at **200m** CPU requests) **together with** **`06-pod-defaults-from-limitrange.yaml`** can exceed the **250m** CPU request quota, because the LimitRange adds another **100m** default request for the extra Deployment. Run **`06`** on its own after **`01`–`03`**, or delete **`demo-app`** before applying **`06`**, as noted in the YAML comments.

---

## Part 2 — Show quota enforcement (step 3)

1. Apply **`04-deployment-within-quota.yaml`** and wait until Pods are **Running**.
2. Apply **`05-deployment-exceed-quota.yaml`** (or **`kubectl apply -f`** the same file again — it updates **`demo-app`** to three replicas).
3. Observe Pending Pods and namespace **events**; **`kubectl describe resourcequota`** shows **used** versus **hard**.

---

## Part 3 — Quota viewer (graphical UI)

The quota viewer is a containerized **FastAPI** application. It uses the **official Kubernetes Python client** to list **`ResourceQuota`** objects (the same information you see from **`kubectl`**). It does **not** shell out to **`kubectl`**.

### APIs

- **`GET /api/health`** — Liveness/readiness style health check.
- **`GET /api/namespaces`** — Lists namespaces (requires RBAC).
- **`GET /api/quotas?namespace=<ns>`** — Lists all **`ResourceQuota`** objects in that namespace with **used / hard** and a computed **percentage** per resource key.

The browser UI loads **`/`**, lets you pick a namespace, **refresh**, and optional **auto-refresh**. Progress bars turn **amber** from **85%** usage and **red** from **100%**.

### Run locally (optional)

From **`quota-viewer/`** with a working **`kubeconfig`**:

```bash
cd quota-viewer
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Open **http://127.0.0.1:8080** (optional: **`?namespace=quota-demo`**).

### Deploy to the cluster

1. Build an image tagged to match **`deploy.yaml`** (default **`quota-viewer:dev`**):

   ```bash
   docker build -t quota-viewer:dev ./quota-viewer
   ```

   Load the image into your cluster if needed (for example **kind**: **`kind load docker-image quota-viewer:dev`**), or push to a registry and update the **`image`** field in **`quota-viewer/deploy.yaml`**.

2. Ensure the **`quota-demo`** namespace exists (from **`01-namespace.yaml`**).

3. Apply the viewer manifests:

   ```bash
   kubectl apply -f quota-viewer/deploy.yaml
   ```

4. **`deploy.yaml`** installs a **ClusterRole** allowing **`get/list/watch`** on **`namespaces`** and **`resourcequotas`**. The Deployment runs as user **1000** and uses the **`quota-viewer`** ServiceAccount.

5. Access the UI (example):

   ```bash
   kubectl -n quota-demo port-forward svc/quota-viewer 8080:80
   ```

   Open **http://localhost:8080?namespace=quota-demo**.

---

## What this UI does *not* show

**`LimitRange`** objects are separate from **`ResourceQuota`**. This viewer only charts **`ResourceQuota`** **used/hard** pairs. To demonstrate **`LimitRange`**, use **`kubectl describe pod`** on a Pod created from **`06-pod-defaults-from-limitrange.yaml`**.

---

## Observability and logging

Here's a concise way to think about **Prometheus + Grafana on OpenShift**.

### Prometheus (metrics)

**Use what OpenShift already ships:**

- **Cluster Monitoring Operator** runs a **Prometheus-based** stack for the cluster (platform metrics, Alertmanager, etc.).
- For **your apps' metrics** in user namespaces, enable **User Workload Monitoring** (UWM) so those targets get scraped into the **same** monitoring stack (with the right `ServiceMonitor` / `PodMonitor` CRs and RBAC).

You explore/query via:

- **OpenShift Console → Observe** (metrics, alerts, dashboards where available), and/or  
- **Prometheus/Thanos API** (depending on how your cluster exposes querying—often via internal routes or console integration).

So for "show Prometheus" to someone in a supported way, the usual answer is: **OpenShift's built-in monitoring + UWM**, not a random standalone Prometheus unless you have a strong reason.

### Grafana (dashboards)

OpenShift **does not always ship Grafana as the default primary UI** in the same way older docs implied; **what Red Hat recommends has shifted by version**, so check your **exact OCP version** in the docs. Practical patterns:

1. **OperatorHub → Grafana Operator** (community or vendor Grafana operator)  
   - Deploy **Grafana** in your cluster, wire **Prometheus** as a **data source** (often the **Thanos Querier** or internal monitoring Prometheus URL your admin gives you).  
   - Good when you want **classic Grafana** dashboards for demos.

2. **Console / Observability**  
   - Use **Observe** in the web console for metrics/alerts; some environments also ship or integrate **dashboard** UIs—depends on subscription and version.

3. **Bring your own (Helm, etc.)**  
   - Possible (e.g. kube-prometheus-stack–style), but you must deal with **Routes, TLS, SCCs, and support boundaries**; fine for labs, less ideal for production without design review.

### Short recommendation

| Goal | Typical approach |
|------|------------------|
| **Show metrics like Prometheus** | **Built-in monitoring + User Workload Monitoring** + Console **Observe** + `ServiceMonitor`/`PodMonitor` on your app. |
| **Show Grafana-style dashboards** | **Grafana Operator** (OperatorHub) + data source pointing at your cluster's **query endpoint** (often Thanos Querier / monitoring Prometheus—**cluster admins** usually provide the URL and TLS trust). |

If you say whether this is **self-managed OCP**, **ROSA**, or **OpenShift Dedicated**, and your **rough version (e.g. 4.14 vs 4.16)**, the exact "click here" path (and whether Grafana is bundled vs operator-only) can be narrowed to match what's supported on your platform.

---

## License

Add a license file in this repository if you intend to publish or share it broadly.
