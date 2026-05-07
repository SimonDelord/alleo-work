# Observability and logging

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

### Logging (simple deployment demo)

Here's a practical way to **demo logging** on OpenShift with a **simple deployment**, from “works everywhere” to “centralized.”

#### 1. Start with what always works: container logs → Console / `oc`

Every workload that writes to **stdout/stderr** has logs the platform already collects at the node (for **short retention** on the node; how long depends on cluster settings).

**Demo flow:**

1. Deploy something tiny that **prints to stdout** on an interval (e.g. timestamp + level + message).
2. In **OpenShift Console**: **Workloads → Deployments → your deployment → Pods → click a Pod → Logs** (live tail, download, previous container).
3. On the CLI:  
   `oc logs -f deployment/your-app -n your-namespace`  
   and optionally **`--previous`** after a crash restart.

**Teaching point:** Apps should log to **stdout/stderr** (not only files inside the container) so OpenShift and aggregators can pick them up consistently.

That alone is a solid “logging 101” demo **without** installing anything extra.

#### 2. Show multi-stream behavior (still simple)

- **Multiple replicas:** hit the Route and show **different Pod names** in the log stream (round-robin).
- **Structured-ish lines:** e.g. `level=INFO msg=…` so people see grepping/filtering in the console (if available) or in `oc logs | grep`.

#### 3. Centralized logging (if your cluster has it)

Many OpenShift installs use the **cluster logging** stack (components and UIs change by **version** and **what your admin enabled**—Elasticsearch/Kibana vs **Loki**/Vector vs **Console Log explorer**).

**Demo pattern:**

1. Confirm with admin whether **cluster logging** is installed and **where to view** aggregated logs (Console **Observe → Logs** / **Logging** / external **Kibana/Grafana Loki**—names vary).
2. Run the same noisy app; show the same lines **in the aggregation UI** with **namespace / pod / search** filters.

**Teaching point:** Node/console logs are great for **live debugging**; the logging stack is for **search, retention, and correlation** across namespaces.

#### 4. Optional “failure” beat (very effective in 2 minutes)

- Exit non-zero or throw errors so you get **stack traces** in logs.
- Restart the Pod and show **`oc logs --previous`** or **Previous logs** in the console.

#### Minimal “demo app” idea

A **Deployment + Service + Route** (or `oc new-app` from an image) whose only job is:

```text
every second: print ISO time + pod name + random INFO/WARN line
```

Use **`DOWNWARD_API`** env for pod name so each replica’s lines are clearly distinct.

**Bottom line:** For a **simple OpenShift deployment demo**, use **stdout logging + Console Pod logs + `oc logs -f`**. Add **aggregated logging** only if the cluster already has the logging stack—then repeat the same app and show **filter/search** in the central UI.
