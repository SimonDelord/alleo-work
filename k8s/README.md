# Kubernetes quota demo

This repository contains a **hands-on demo** of Kubernetes **`ResourceQuota`** and **`LimitRange`**, plus a small **web UI** that reads `ResourceQuota` objects through the Kubernetes API and charts **used versus hard** limits.

Use it for internal walkthroughs or customer demos when you need to show how namespace-level budgets work and what happens when workloads exceed them.

---

## Repository layout

| Path | Purpose |
|------|--------|
| [`quota-demo/`](quota-demo/) | Ordered YAML manifests: namespace, quota, limit range, and example workloads. |
| [`quota-viewer/`](quota-viewer/) | FastAPI app + Dockerfile + Kubernetes `deploy.yaml` (RBAC, Deployment, Service). |

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

## License

Add a license file in this repository if you intend to publish or share it broadly.
