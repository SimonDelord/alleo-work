from __future__ import annotations

import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from kubernetes import client, config
from kubernetes.client.rest import ApiException

NAMESPACE = os.environ.get("NAMESPACE", "hpa-demo")
DEPLOY_NAME = os.environ.get("DEPLOYMENT_NAME", "hpa-demo-app")
HPA_NAME = os.environ.get("HPA_NAME", "hpa-demo-app")
APP_LABEL = os.environ.get("APP_LABEL", "app=hpa-demo-app")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

app = FastAPI(title="HPA Demo", version="1.0.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_config_loaded = False


def _ensure_kube() -> None:
    global _config_loaded
    if _config_loaded:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _config_loaded = True


def _hpa_summary(hpa: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": hpa.metadata.name if hpa.metadata else HPA_NAME,
        "minReplicas": None,
        "maxReplicas": None,
        "currentReplicas": None,
        "desiredReplicas": None,
        "targetCpuPercent": None,
        "currentCpuPercent": None,
    }
    spec = hpa.spec
    if spec:
        out["minReplicas"] = spec.min_replicas
        out["maxReplicas"] = spec.max_replicas
        if spec.metrics:
            for m in spec.metrics:
                if m.resource and m.resource.name == "cpu":
                    t = m.resource.target
                    if t and t.average_utilization is not None:
                        out["targetCpuPercent"] = t.average_utilization
    status = hpa.status
    if status:
        out["currentReplicas"] = status.current_replicas
        out["desiredReplicas"] = status.desired_replicas
        if status.current_metrics:
            for cm in status.current_metrics:
                if cm.resource and cm.resource.name == "cpu":
                    cur = cm.resource.current
                    if cur and cur.average_utilization is not None:
                        out["currentCpuPercent"] = cur.average_utilization
    return out


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    _ensure_kube()
    apps = client.AppsV1Api()
    v1 = client.CoreV1Api()
    autoscaling = client.AutoscalingV2Api()

    try:
        dep = apps.read_namespaced_deployment(DEPLOY_NAME, NAMESPACE)
    except ApiException as e:
        raise HTTPException(status_code=e.status or 500, detail=e.reason or str(e)) from e

    desired = dep.spec.replicas or 0
    st = dep.status
    ready = int(st.ready_replicas or 0) if st else 0
    updated = int(st.updated_replicas or 0) if st else 0

    try:
        pods = v1.list_namespaced_pod(NAMESPACE, label_selector=APP_LABEL)
    except ApiException as e:
        raise HTTPException(status_code=e.status or 500, detail=e.reason or str(e)) from e

    pod_rows: list[dict[str, str]] = []
    for p in pods.items:
        name = p.metadata.name if p.metadata else ""
        phase = p.status.phase if p.status else ""
        pod_rows.append({"name": name, "phase": phase})

    hpa_block: dict[str, Any] | None = None
    try:
        hpa = autoscaling.read_namespaced_horizontal_pod_autoscaler(HPA_NAME, NAMESPACE)
        hpa_block = _hpa_summary(hpa)
    except ApiException:
        hpa_block = None

    return {
        "namespace": NAMESPACE,
        "deployment": DEPLOY_NAME,
        "desiredReplicas": desired,
        "readyReplicas": ready,
        "updatedReplicas": updated,
        "podCount": len(pod_rows),
        "pods": sorted(pod_rows, key=lambda x: x["name"]),
        "hpa": hpa_block,
    }


@app.get("/stress")
def stress(
    seconds: float = Query(2.0, ge=0.25, le=20.0, description="Busy-loop duration per request"),
) -> dict[str, Any]:
    """Burn CPU so metrics-server sees utilization (shared across replicas via Route)."""
    deadline = time.monotonic() + seconds
    x = 0
    while time.monotonic() < deadline:
        x = (x + 1) % 1000000007
    return {"ok": True, "seconds": seconds}


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml")
