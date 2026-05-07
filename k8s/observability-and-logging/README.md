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
