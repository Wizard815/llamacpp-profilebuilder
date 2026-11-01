"""Long-term logs: events, GPU history, Argos request history."""
from __future__ import annotations

import time

from flask import Blueprint, request

from ..gpu_pick import note_cards
from ..web import ctx, err, ok

bp = Blueprint("logs", __name__)

# range -> (seconds, bucket seconds)
RANGES = {"30s": (30, 2), "1m": (60, 2), "2m": (120, 2), "15m": (900, 10), "5m": (300, 5), "10m": (600, 10), "30m": (1800, 15), "1h": (3600, 30), "6h": (6 * 3600, 120), "24h": (86400, 300), "7d": (7 * 86400, 3600),
          "30d": (30 * 86400, 3 * 3600), "90d": (90 * 86400, 86400), "1y": (365 * 86400, 86400)}


@bp.get("/api/events")
def events():
    a = request.args
    since = time.time() - a.get("hours", 0, type=float) * 3600 if a.get("hours") else None
    return ok(events=ctx().store.list_events(kind=a.get("kind") or None, level=a.get("level") or None,
                                             since=since, q=a.get("q") or None,
                                             limit=max(1, min(a.get("limit", 300, type=int), 5000))))


@bp.get("/api/gpu/series")
def gpu_series():
    c = ctx()
    rng = request.args.get("range", "24h")
    if rng not in RANGES:
        return err(f"range must be one of {', '.join(RANGES)}")
    span, bucket = RANGES[rng]
    if request.args.get("live") == "1":  # the page is live: read the GPUs every 2 s while it polls
        c.collector.watch_live()
    since = time.time() - span
    # the page keeps what it has and asks only for what is newer, in buckets of its choosing
    if request.args.get("since"):
        since = max(since, request.args.get("since", type=float) or since)
    bucket = max(1, min(request.args.get("bucket", bucket, type=int), 3600))
    rows, source = c.store.gpu_series(since, bucket), "raw"
    cpu_models = []  # what runs on the CPU now: Usage shows the CPU while there is any
    url = c.settings.get("integrations")["engine_url"]
    if url:
        from ..integrations import engine_api
        try:
            cpu_models = [m["id"] for m in (engine_api.pool_state(url, timeout=1.0).get("models") or [])
                          if m.get("on_cpu") and m.get("state") != "stopped"]
        except Exception:  # noqa: BLE001 - another router or a busy engine: none known
            pass
    return ok(range=rng, bucket=bucket, source=source, rows=rows, latest=c.store.gpu_latest(),
              cpu=c.store.cpu_series(since, bucket), cpu_latest=c.store.cpu_latest(), cpu_models=cpu_models,
              collector={"source": c.collector.last_source,
                         "enabled": c.settings.get("telemetry")["enabled"]})


@bp.get("/api/gpu/latest")
def gpu_latest():
    """Just the newest reading per GPU: what the header bars need, without the whole series."""
    c = ctx()
    latest = c.store.gpu_latest()
    note_cards(latest)
    return ok(latest=latest)


@bp.get("/api/debug/threads")
def debug_threads():
    """What every thread is doing and how much CPU it has used: for finding the one that keeps the process busy.
    Only answers from the machine the app runs on (docker exec ... curl), never from the network."""
    import sys
    import threading
    import traceback
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return err("this is only for the machine the app runs on", 403)
    ticks = float(__import__("os").sysconf("SC_CLK_TCK"))
    names = {t.ident: t for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        t = names.get(ident)
        cpu = None
        try:
            with open(f"/proc/self/task/{t.native_id}/stat") as fh:
                f = fh.read().rsplit(")", 1)[1].split()
                cpu = round((int(f[11]) + int(f[12])) / ticks, 1)  # utime + stime, seconds
        except (OSError, AttributeError, IndexError, ValueError):
            pass
        out.append({"thread": t.name if t else str(ident), "cpu_s": cpu,
                    "stack": [f"{fs.filename.split('/src/')[-1]}:{fs.lineno} {fs.name}" for fs in traceback.extract_stack(frame)][-8:]})
    out.sort(key=lambda x: -(x["cpu_s"] or 0))
    return ok(threads=out)


@bp.get("/api/requests/recent")
def requests_recent():
    """The newest requests, live from the engine (the store's copy when the engine is not there), and how
    many each model is working on right now."""
    from ..integrations import engine_api
    from ..telemetry import recent_requests
    c = ctx()
    limit = max(1, min(request.args.get("limit", 25, type=int), 200))
    url = c.settings.get("integrations")["engine_url"]
    in_flight, running_now = {}, []
    try:
        rows, source = (recent_requests(url, limit), "engine") if url else (c.store.list_requests(limit=limit), "stored")
        if url:
            pool = engine_api.pool_state(url)
            in_flight = {m["id"]: m["in_flight"] for m in pool.get("models") or [] if m.get("in_flight")}
            try:  # still running: shown while they work. An older engine has none to say
                running_now = engine_api.inflight(url)
            except Exception:  # noqa: BLE001
                running_now = []
    except Exception:  # noqa: BLE001 - fall back to what was synced
        rows, source = c.store.list_requests(limit=limit), "stored"
    return ok(requests=rows, in_flight=in_flight, running=running_now, source=source, at=time.time())


@bp.post("/api/requests/<inflight_id>/next")
def request_next(inflight_id):
    """A waiting request goes first (Schedule -> Live)."""
    from ..integrations import engine_api
    url = ctx().settings.get("integrations")["engine_url"]
    if not url:
        return err("the engine is not running")
    try:
        engine_api.request_next(url, inflight_id)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", 0)
        return err("that request is not waiting any more" if code == 404 else f"the engine refused: {exc}", 404 if code == 404 else 502)
    return ok()


@bp.post("/api/requests/<inflight_id>/<action>")
def request_action(inflight_id, action):
    """Cancel a request, or hold or release one waiting for a model to load (Schedule -> Live)."""
    from ..integrations import engine_api
    if action not in ("cancel", "hold", "release"):
        return err("action is cancel, hold or release", 404)
    url = ctx().settings.get("integrations")["engine_url"]
    if not url:
        return err("the engine is not running")
    try:
        if action == "cancel":
            engine_api.request_cancel(url, inflight_id)
        else:
            engine_api.request_hold(url, inflight_id, action == "hold")
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", 0)
        return err("that request is not running any more" if code == 404 else f"the engine refused: {exc}", 404 if code == 404 else 502)
    return ok()


@bp.get("/api/requests")
def requests_list():
    a = request.args
    since = time.time() - RANGES.get(a.get("range", "24h"), RANGES["24h"])[0]
    return ok(requests=ctx().store.list_requests(model=a.get("model") or None, since=since,
                                                 limit=max(1, min(a.get("limit", 300, type=int), 5000))),
              stats=ctx().store.request_stats(since))


# ----- what is really on the GPUs ------------------------------------------------------
def _gpu_listing() -> dict:
    from .. import gpu_procs
    from ..integrations import engine_api
    c = ctx()
    running, pool = [], []
    url = c.settings.get("integrations")["engine_url"]
    if url:
        try:
            running = engine_api.running(url)
        except Exception:  # noqa: BLE001 - the list is still useful without owners
            pass
        try:  # sizes and cards: what names a model the engine started in another container
            pool = engine_api.pool_state(url).get("models") or []
        except Exception:  # noqa: BLE001
            pass
    launches = [{"port": r.port, "running": r.running, "profile_name": r.profile_name, "base_model": r.base_model,
                 "launch_id": r.launch_id} for r in c.runner.list()]
    return gpu_procs.listing(running, launches, pool)


@bp.get("/api/gpu/processes")
def gpu_processes():
    return ok(**_gpu_listing())


@bp.get("/api/gpu/processes/<int:pid>")
def gpu_process_watch(pid):
    """One process in detail: what it is, what started it, what it serves and whether it is busy. Read only."""
    from .. import gpu_procs
    row = next((p for p in _gpu_listing()["processes"] if p["pid"] == pid or p.get("host_pid") == pid), None)
    if row is None:
        return err("that process is not using a GPU any more", 404)
    return ok(process=gpu_procs.watch(row["pid"], row))


@bp.post("/api/gpu/processes/<int:pid>/kill")
def gpu_process_kill(pid):
    """Stop a process that is using a GPU. One the engine owns must be unloaded from Schedule instead,
    unless ``force`` says otherwise: killing it behind the engine's back leaves the engine thinking it runs."""
    from .. import gpu_procs
    from ..web import body
    data = _gpu_listing()
    row = next((p for p in data["processes"] if p["pid"] == pid), None)
    if row is None:
        return err("that process is not using a GPU as far as this app can see", 404)
    b = body()
    if b.get("dry_run"):  # say what would happen, touch nothing
        return ok(dry_run=True, **gpu_procs.kill_plan(pid, row, bool(b.get("hard"))))
    if not row.get("visible", True) and (row.get("owner") or "").startswith("model "):
        return err(f"{row['owner']} is loaded by the engine in another container: unload it from Schedule → Live", 409)
    if not row.get("visible", True):
        # its number is the host's; inside this container the same number can be another process entirely
        return err(f"PID {pid} is outside this container, so Argos cannot stop it. On the host: kill {row.get('host_pid', pid)} "
                   "(or give Argos the host's PID namespace, pid: host)", 409)
    if (row.get("owner") or "").startswith("model ") and not b.get("force"):
        return err(f"{row['owner']} is loaded by the engine: unload it from Schedule → Live instead", 409)
    try:
        result = gpu_procs.kill(pid, {p["pid"] for p in data["processes"]}, bool(b.get("hard")))
    except PermissionError as exc:
        return err(str(exc), 403)
    except OSError as exc:
        return err(f"could not stop it: {exc}", 500)
    ctx().event("launch", f"{result}: PID {pid} ({row['name']}), which held {row['vram_gib'] or '?'} GiB of GPU memory",
                level="warn", data={"pid": pid})
    return ok(result=result)
