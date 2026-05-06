from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.utils.quantity import parse_quantity

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

app = FastAPI(title="Quota Viewer", version="1.0.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_config_loaded = False


def _api_exception_detail(exc: ApiException) -> str:
    if exc.reason:
        return exc.reason
    if exc.body:
        body = exc.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        return body[:800]
    return str(exc)


def _ensure_kube_config() -> None:
    global _config_loaded
    if _config_loaded:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _config_loaded = True


def _resource_kind(key: str) -> str:
    k = key.lower()
    if "memory" in k:
        return "memory"
    if "cpu" in k:
        return "cpu"
    return "count"


def _parse_cpu_millicores(value: str) -> float:
    s = str(value).strip()
    if not s:
        return 0.0
    if s.endswith("m"):
        return float(s[:-1])
    return float(s) * 1000.0


def _parse_memory_bytes(value: str) -> int:
    return int(parse_quantity(str(value)))


def _parse_count(value: str) -> float:
    return float(int(str(value).strip()))


def _parse_for_kind(kind: str, value: str) -> float:
    if kind == "cpu":
        return _parse_cpu_millicores(value)
    if kind == "memory":
        return float(_parse_memory_bytes(value))
    return _parse_count(value)


def _format_display(used: str, hard: str) -> str:
    return f"{used} / {hard}"


def _percent(used: float, hard: float) -> float | None:
    if hard <= 0:
        return None
    return round(min(used / hard * 100.0, 999.0), 1)


def _quota_to_payload(rq: Any) -> dict[str, Any]:
    name = rq.metadata.name
    spec = rq.spec
    hard = dict(spec.hard or {}) if spec else {}
    used = dict((rq.status.used or {}) if rq.status else {})

    resources: list[dict[str, Any]] = []
    for key, hard_raw in hard.items():
        kind = _resource_kind(key)
        used_raw = used.get(key, "0")
        try:
            u = _parse_for_kind(kind, str(used_raw))
            h = _parse_for_kind(kind, str(hard_raw))
            pct = _percent(u, h)
        except (ValueError, TypeError, ArithmeticError):
            u = h = 0.0
            pct = None

        level = "ok"
        if pct is not None:
            if pct >= 100:
                level = "bad"
            elif pct >= 85:
                level = "warn"

        resources.append(
            {
                "key": key,
                "kind": kind,
                "used_raw": str(used_raw),
                "hard_raw": str(hard_raw),
                "percent": pct,
                "level": level,
                "display": _format_display(str(used_raw), str(hard_raw)),
            }
        )

    resources.sort(key=lambda r: r["key"])
    return {"name": name, "resources": resources}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/namespaces")
def list_namespaces() -> dict[str, Any]:
    _ensure_kube_config()
    v1 = client.CoreV1Api()
    try:
        resp = v1.list_namespace()
    except ApiException as e:
        raise HTTPException(
            status_code=e.status or 500,
            detail=_api_exception_detail(e),
        ) from e
    names = sorted(ns.metadata.name for ns in resp.items if ns.metadata and ns.metadata.name)
    return {"namespaces": names}


@app.get("/api/quotas")
def list_quotas(namespace: str = Query(..., min_length=1)) -> dict[str, Any]:
    _ensure_kube_config()
    v1 = client.CoreV1Api()
    try:
        resp = v1.list_namespaced_resource_quota(namespace)
    except ApiException as e:
        if e.status == 404:
            return {"namespace": namespace, "quotas": [], "error": "Namespace not found"}
        raise HTTPException(
            status_code=e.status or 500,
            detail=_api_exception_detail(e),
        ) from e

    quotas = [_quota_to_payload(rq) for rq in resp.items]
    return {"namespace": namespace, "quotas": quotas, "error": None}


def _deployment_row(dep: Any) -> dict[str, Any]:
    spec_r = dep.spec.replicas
    desired = int(spec_r) if spec_r is not None else 1
    st = dep.status
    ready = int(st.ready_replicas or 0) if st else 0
    unavailable = int(st.unavailable_replicas or 0) if st else 0
    shortfall = max(0, desired - ready)
    level = "ok"
    if shortfall > 0:
        level = "warn"
    if unavailable and shortfall > 0:
        level = "bad"
    return {
        "name": dep.metadata.name if dep.metadata else "",
        "desired": desired,
        "ready": ready,
        "unavailable": unavailable,
        "shortfall": shortfall,
        "level": level,
    }


@app.get("/api/deployments")
def list_deployments(namespace: str = Query(..., min_length=1)) -> dict[str, Any]:
    """Workload status: desired vs ready replicas (shows quota backlog when pods cannot be created)."""
    _ensure_kube_config()
    apps = client.AppsV1Api()
    try:
        resp = apps.list_namespaced_deployment(namespace)
    except ApiException as e:
        if e.status == 404:
            return {"namespace": namespace, "deployments": [], "error": "Namespace not found"}
        raise HTTPException(
            status_code=e.status or 500,
            detail=_api_exception_detail(e),
        ) from e
    rows = [_deployment_row(d) for d in resp.items]
    rows.sort(key=lambda r: r["name"])
    return {"namespace": namespace, "deployments": rows, "error": None}


def _is_quota_related_event(message: str, reason: str) -> bool:
    """Match quota admission failures without pulling in unrelated events (e.g. namespace paths like quota-demo/)."""
    m = (message or "").lower()
    r = (reason or "").lower()
    if "exceeded quota" in m:
        return True
    if "resourcequota" in m or "resource quota" in m:
        return True
    if r == "failedcreate" and "forbidden" in m and (
        "quota" in m or "exceeded" in m or "limited:" in m
    ):
        return True
    if "forbidden:" in m and "limited:" in m and ("requested:" in m or "requests." in m):
        return True
    return False


def _event_timestamp(ev: Any) -> datetime:
    et = getattr(ev, "event_time", None)
    if et is not None:
        if getattr(et, "tzinfo", None) is None:
            return et.replace(tzinfo=timezone.utc)
        return et
    for attr in ("last_timestamp", "first_timestamp"):
        t = getattr(ev, attr, None)
        if t is not None:
            if getattr(t, "tzinfo", None) is None:
                return t.replace(tzinfo=timezone.utc)
            return t
    meta = ev.metadata
    if meta and meta.creation_timestamp:
        t = meta.creation_timestamp
        if getattr(t, "tzinfo", None) is None:
            return t.replace(tzinfo=timezone.utc)
        return t
    return datetime.min.replace(tzinfo=timezone.utc)


def _event_row(ev: Any) -> dict[str, Any]:
    msg = ev.message or ""
    io = ev.involved_object
    kind = io.kind if io else ""
    name = io.name if io else ""
    series = getattr(ev, "series", None)
    occurrences = int(series.count) if series and series.count is not None else 1
    ts = _event_timestamp(ev)
    return {
        "time": ts.isoformat(),
        "event_type": ev.type or "Normal",
        "reason": ev.reason or "",
        "object_kind": kind,
        "object_name": name,
        "object": f"{kind}/{name}".strip("/"),
        "message": msg[:4000],
        "occurrences": occurrences,
    }


@app.get("/api/quota-events")
def list_quota_events(
    namespace: str = Query(..., min_length=1),
    limit: int = Query(60, ge=1, le=200),
) -> dict[str, Any]:
    """Namespace Events filtered to quota / FailedCreate forbidden messages (e.g. ReplicaSet failures)."""
    _ensure_kube_config()
    v1 = client.CoreV1Api()
    try:
        resp = v1.list_namespaced_event(namespace)
    except ApiException as e:
        if e.status == 404:
            return {"namespace": namespace, "events": [], "error": "Namespace not found"}
        raise HTTPException(
            status_code=e.status or 500,
            detail=_api_exception_detail(e),
        ) from e

    rows: list[dict[str, Any]] = []
    for ev in resp.items:
        msg = ev.message or ""
        reason = ev.reason or ""
        if not _is_quota_related_event(msg, reason):
            continue
        rows.append(_event_row(ev))

    rows.sort(key=lambda r: r["time"], reverse=True)
    rows = rows[:limit]
    return {"namespace": namespace, "events": rows, "error": None}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    # Avoid 404 noise in logs during demos
    return FileResponse(os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml")
