"""Scheduling: the plan, a simulator, VRAM measuring, and the live pool. Saving the plan applies it to the engine."""
from __future__ import annotations

import time
from typing import Optional

from flask import Blueprint, request

from ..telemetry import recent_requests
from ..always import apply_always
from ..command import parse_command
from ..integrations import engine_api
from ..library import scan_models
from ..scheduler import (Engine, PlanError, clean_plan, effective_size, engine_file, render_fragment, simulate,
                         write_fragment)
from ..schedule_rows import build_rows, plan_sizes
from ..web import body, check_id, ctx, err, ok
from . import core

from ..model_procs import ModelProcs
bp = Blueprint("schedule", __name__)
APPLY_CHECK_S = 300  # how often the background tick re-checks the engine's copy (it scans the library)
APPLY_RETRY_S = 60   # ...and how often while models are left out because the engine does not list them yet
KNOWN_TTL_S = 30


# ----- shared pieces -----------------------------------------------------------------
def known_ids() -> tuple[Optional[set[str]], str]:
    """Model ids the engine lists at /v1/models, or (None, "") when it cannot be asked.

    Only the live list is trusted: the engine makes models from files and from profiles itself, so
    they are not in its config file. Without it nothing is filtered. Cached for a few seconds because
    this is an HTTP call and the Schedule view loads often.
    """
    c = ctx()
    url = c.settings.get("integrations")["engine_url"]
    cached = c.schedule_state.get("_known")
    if cached and cached["key"] == url and time.time() - cached["at"] < KNOWN_TTL_S:
        return cached["value"]
    value: tuple[Optional[set[str]], str] = (None, "")
    if url:
        try:
            ids = engine_api.model_ids(url)
            # profile variants are left out of /v1/models but the pool plans them, so its list counts too;
            # trusting /v1/models alone marked every profile "not in the engine" and sent it no size
            try:
                ids |= {str(m["id"]) for m in engine_api.pool_state(url).get("models") or [] if m.get("id")}
            except Exception:  # noqa: BLE001 - another router: /v1/models is all there is
                pass
            value = (ids, "live")
        except Exception:  # noqa: BLE001 - the Live view and Integrations page show connection problems
            pass
    c.schedule_state["_known"] = {"at": time.time(), "key": url, "value": value}
    return value


def cpu_fallback_models() -> list[str]:
    """The assistant's chat and embedding models, when they may fall back to the CPU (Settings -> Assistant)."""
    st = ctx().settings
    a = st.get("assistant")
    if not a.get("cpu_fallback", True):
        return []
    out = [a.get("model") or ""] if a.get("mode", "engine") == "engine" else []
    m = st.get("memory")
    if m.get("enabled") and not (m.get("endpoint") or "").strip():  # blank endpoint = the engine's own /v1
        out.append(m.get("model") or "")
    return [x for x in out if x]


def compute(plan: Optional[dict] = None) -> dict:
    c = ctx()
    plan = plan or c.plans.get()
    known, known_from = known_ids()
    core._custom_cache()
    library = scan_models(c.settings.get("paths")["models_dir"])
    presets = core.cookbook_state()["presets"]
    rows, commands = build_rows(library, presets, plan, c.settings.get("prefab"), known)
    sizes = plan_sizes(rows)
    # a model the engine serves that has no row here (not in the library, no profile) still gets its
    # measured size, otherwise the engine keeps its own rougher guess for it
    for mid, m in plan["measured"].items():
        if mid not in sizes and m.get("gib"):
            sizes[mid] = m["gib"]
    text, skipped = render_fragment(plan, sizes, known, cpu_fallback_models())
    return {"plan": plan, "rows": rows, "commands": commands, "sizes": sizes, "fragment": text,
            "skipped": skipped, "known_from": known_from}


def detected_cards() -> list[dict]:
    """The GPUs the telemetry has seen, with their VRAM in GiB."""
    return [{"gpu": g["gpu"], "name": g.get("name") or f"GPU {g['gpu']}",
             "total_gib": round((g.get("mem_total_mb") or 0) / 1024, 2)} for g in ctx().store.gpu_latest()]


def apply_plan(force: bool = True, data: Optional[dict] = None) -> None:
    """Give the plan to the engine: write its routing block into the engine's config folder, which the engine
    watches. Called on every save and profile change; the background tick passes force=False.

    ``data`` is a compute() result the caller already has, to avoid scanning the library twice.
    """
    c = ctx()
    path = engine_file()
    if not path:
        return
    plan = data["plan"] if data else c.plans.get()
    wait = APPLY_RETRY_S if c.schedule_state.get("_retry") else APPLY_CHECK_S
    if not force and time.time() - c.schedule_state.get("_checked", 0.0) < wait:
        return
    c.schedule_state["_checked"] = time.time()
    try:
        data = data or compute(plan)
        c.schedule_state["_retry"] = bool(data["skipped"])  # a new profile reaches the engine within its refresh
        if write_fragment(path, data["fragment"]):
            c.schedule_state.update(written_at=time.time(), error="")
            c.event("schedule", "applied the scheduling plan to the engine",
                    data={"models": len(data["sizes"]), "skipped": data["skipped"], "file": path})
        c.schedule_state.update(error="")
    except OSError as exc:
        if c.schedule_state.get("error") != str(exc):
            c.event("schedule", f"could not apply the plan to the engine ({path}): {exc}", level="error")
        c.schedule_state.update(error=str(exc))


def _payload(data: dict) -> dict:
    c = ctx()
    return {"plan": data["plan"], "rows": data["rows"], "fragment": data["fragment"], "skipped": data["skipped"],
            "known_from": data["known_from"], "detected_cards": detected_cards(),
            "applied": {"connected": bool(engine_file()), "at": c.schedule_state.get("written_at"),
                        "error": c.schedule_state.get("error", "")},
            "live_configured": bool(c.settings.get("integrations")["engine_url"])}


# ----- plan --------------------------------------------------------------------------
@bp.get("/api/schedule")
def schedule_get():
    return ok(**_payload(compute()))


@bp.put("/api/schedule")
def schedule_put():
    c = ctx()
    try:
        c.plans.update(body())
    except PlanError as exc:
        return err(str(exc))
    c.event("schedule", "saved the scheduling plan")
    data = compute()
    apply_plan(data=data)
    return ok(**_payload(data))


# ----- simulator ---------------------------------------------------------------------
@bp.post("/api/schedule/simulate")
def schedule_simulate():
    """Run steps through the same rules the pool router uses. ``plan`` overrides the saved plan for this run."""
    c, d = ctx(), body()
    steps = d.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) for s in steps):
        return err("steps must be a non-empty list of {op, model}")
    if len(steps) > 200:
        return err("at most 200 steps")
    try:
        plan = {**c.plans.get(), **clean_plan(d.get("plan") or {})}
        overrides = {str(k): float(v) for k, v in (d.get("sizes") or {}).items()}
    except (PlanError, TypeError, ValueError) as exc:
        return err(str(exc))
    data = compute(plan)
    sizes = {r["id"]: r["effective_gib"] for r in data["rows"]}
    sizes.update(plan["sizes"])  # models Argos has that this app has no file or profile for
    for mid, m in plan["measured"].items():
        sizes.setdefault(mid, m["gib"])
    sizes.update(overrides)
    cards = [x["total_gib"] for x in detected_cards()]
    if not plan["cards"] and not cards:
        return err("no cards: set the card sizes in the plan, or let the GPU telemetry see them first")
    eng = Engine(plan, sizes, detected_gib=cards)
    measured = {k: v["load_seconds"] for k, v in plan["measured"].items() if v.get("load_seconds")}
    return ok(results=simulate(eng, steps, measured), cards=[{"id": i, "cap_gib": round(cap, 2)}
                                                              for i, cap in eng.cards],
              budget_gib=round(eng.budget, 2), placement=bool(eng.cards),
              unknown=[m for m in sizes if not sizes[m]][:50])


# ----- measure -----------------------------------------------------------------------
@bp.post("/api/schedule/measure")
def measure_start():
    c, d = ctx(), body()
    row_id = (d.get("id") or "").strip()
    data = compute()
    command = data["commands"].get(row_id)
    if not command:
        return err("unknown model: save it as a profile or put the file in the models folder first", 404)
    try:
        parse_command(command)
        timeout = max(10.0, min(float(d.get("timeout_s", 900)), 3600.0))
    except (ValueError, TypeError) as exc:
        return err(str(exc))
    if any(j["state"] in ("queued", "baseline", "loading", "settling") for j in c.measurer.jobs.values()):
        return err("a measurement is already running; wait for it or cancel it", 409)
    always = c.settings.get("always")
    command = apply_always(command, always if always["on_launch"] else {})[0]  # env presets either way
    job = c.measurer.start(command, row_id, label=row_id, timeout_s=timeout,
                           settle_s=float(d.get("settle_s", 6.0)))
    return ok(job=c.measurer.get(job))


@bp.get("/api/schedule/measure/<job_id>")
def measure_status(job_id):
    job = ctx().measurer.get(check_id(job_id))
    return ok(job=job) if job else err("not found", 404)


@bp.post("/api/schedule/measure/<job_id>/cancel")
def measure_cancel(job_id):
    return ok(cancelled=ctx().measurer.cancel(check_id(job_id)))


@bp.post("/api/schedule/measured/<path:model_id>/use")
def measure_use(model_id):
    """Copy a measurement into the plan as a size set by hand."""
    c = ctx()
    plan = c.plans.get()
    m = plan["measured"].get(model_id)
    if not m:
        return err("nothing measured for this model", 404)
    c.plans.update({"sizes": {**plan["sizes"], model_id: m["gib"]}})
    data = compute()
    apply_plan(data=data)
    return ok(**_payload(data))


# ----- live --------------------------------------------------------------------------
@bp.get("/api/schedule/live")
def schedule_live():
    """The engine's /api/pool, passed through so the browser needs no access to the engine."""
    url = ctx().settings.get("integrations")["engine_url"]
    if not url:
        return ok(connected=False, error="the engine is not running", pool=None)
    try:
        real = {str(g["gpu"]): round((g.get("mem_used_mb") or 0) / 1024, 2) for g in ctx().store.gpu_latest()}
        activity = [{"ts": e["ts"], "level": e["level"], "message": e["message"]}
                    for e in ctx().store.list_events(kind="schedule", limit=40)]
        runners = {}
        try:  # which runner serves each model; anything not listed is llama.cpp
            runners = {mid: e.get("metadata", {}).get("argos", {}) for mid, e in ctx().runners.entries().items()}
        except Exception:  # noqa: BLE001 - the column falls back to llama.cpp
            pass
        try:  # the requests the GPUs are working on, newest first, straight from the engine
            requests = recent_requests(url, max(1, min(request.args.get("requests", 25, type=int), 200)))
        except Exception:  # noqa: BLE001 - the feed is empty rather than the page broken
            requests = []
        try:  # and the ones still running, so they show while they work, not once they finish
            running_now = engine_api.inflight(url)
        except Exception:  # noqa: BLE001
            running_now = []
        pool = engine_api.pool_state(url)
        procs = {}
        if any(m.get("on_cpu") and m.get("state") != "stopped" for m in pool.get("models") or []):
            try:  # what each model on the CPU really uses: its own process's RAM and cores busy
                procs = _model_procs.sample(engine_api.running(url))
            except Exception:  # noqa: BLE001 - the card shows the estimate then
                procs = {}
        return ok(connected=True, error="", pool=pool, at=time.time(), real_gib=real, activity=activity,
                  cpu=ctx().store.cpu_latest(), procs=procs,
                  runners=runners, requests=requests, running=running_now)
    except Exception as exc:  # noqa: BLE001
        return ok(connected=False, error=str(exc), pool=None)


# ----- one model in the Live view: load, unload, keep loaded, its log, its launch command --------
_model_procs = ModelProcs()


def _engine_url():
    url = ctx().settings.get("integrations")["engine_url"]
    if not url:
        raise ValueError("the engine is not running")
    return url


@bp.post("/api/schedule/live/<path:model_id>/<action>")
def live_action(model_id, action):
    """load (in the background; it can take minutes), unload, or keep (body {"on": bool}: never evict it)."""
    import threading
    c = ctx()
    if action == "one_at_a_time":
        on = bool(body().get("on", True))
        models = [m for m in c.plans.get()["one_at_a_time"] if m != model_id] + ([model_id] if on else [])
        c.plans.update({"one_at_a_time": models})
        c.event("schedule", f"one at a time {'on' if on else 'off'} for {model_id}: "
                + ("one request goes in, the rest wait in order" if on else "requests go in together again"))
        apply_plan()
        return ok(one_at_a_time=on)
    if action == "answer_first":
        on = bool(body().get("on", True))
        models = [m for m in c.plans.get()["answer_first"] if m != model_id] + ([model_id] if on else [])
        c.plans.update({"answer_first": models})
        c.event("schedule", f"answer first {'on' if on else 'off'} for {model_id}: "
                + ("a long new prompt waits while another chat on it is answering" if on else "every request goes straight in"))
        apply_plan()
        return ok(answer_first=on)
    if action == "keep":
        on = bool(body().get("on", True))
        persistent = [m for m in c.plans.get()["persistent"] if m != model_id] + ([model_id] if on else [])
        c.plans.update({"persistent": persistent})
        c.event("schedule", f"{'keep ' + model_id + ' loaded' if on else model_id + ' may be unloaded again'} (never evict: {'on' if on else 'off'})")
        apply_plan()
        return ok(persistent=on)
    try:
        url = _engine_url()
    except ValueError as exc:
        return err(str(exc), 409)
    if action == "dismiss":  # the crash note goes; the model is not started
        try:
            engine_api.clear_crashes(url, model_id)
        except Exception as exc:  # noqa: BLE001
            return err(f"could not reach the engine: {exc}", 502)
        return ok()
    # say what is true: unloading a model that is not loaded, or loading one the engine has never heard of,
    # used to answer "ok" and leave a misleading event behind
    try:
        states = {r["model"]: r.get("state") for r in engine_api.running(url)}
        known = set(engine_api.model_ids(url))
    except Exception as exc:  # noqa: BLE001
        return err(f"could not reach the engine: {exc}", 502)
    if action == "unload" and model_id not in states:
        return ok(already=True, message=f"{model_id} is not loaded")
    if action == "load":
        if model_id not in known:
            return err(f"the engine has no model called {model_id}", 404)
        if states.get(model_id) in ("ready", "starting"):
            return ok(already=True, message=f"{model_id} is already {states[model_id]}")
    if action == "unload":
        try:
            engine_api.unload_model(url, model_id)
        except Exception as exc:  # noqa: BLE001
            return err(f"the engine could not unload {model_id}: {exc}", 502)
        c.event("schedule", f"unloaded {model_id} by hand", data={"model": model_id})
        return ok()
    if action == "load":
        def run():
            try:
                engine_api.clear_crashes(url, model_id)  # loading by hand is the retry after a crash loop
                engine_api.load_model(url, model_id)
            except Exception as exc:  # noqa: BLE001 - reported as an event
                c.event("schedule", f"could not load {model_id}: {exc}", level="error", data={"model": model_id})
        threading.Thread(target=run, daemon=True).start()
        c.event("schedule", f"loading {model_id} by hand", data={"model": model_id})
        return ok(started=True)
    return err("use load, unload or keep", 404)


@bp.get("/api/schedule/live/<path:model_id>/log")
def live_log(model_id):
    try:
        return ok(log=engine_api.model_log(_engine_url(), model_id))
    except ValueError as exc:
        return err(str(exc), 409)
    except Exception as exc:  # noqa: BLE001
        return err(f"could not read the log: {exc}", 502)


@bp.get("/api/schedule/live/<path:model_id>/command")
def live_command(model_id):
    """What starts this model, for editing: the profile it comes from, else the library model's prefab, else the
    command the engine is running."""
    from ..cookbook import variant_id
    from ..library import prefab_command, prefab_with_launcher
    c = ctx()
    for p in core.cookbook_state()["presets"]:
        if variant_id(p) == model_id:
            pid = (p.get("fields") or {}).get("profile_id")
            prof = c.store.get_profile(pid) if pid else None
            if prof:
                return ok(source="profile", profile_id=prof["profile_id"], name=prof["name"], base_model=prof.get("base_model") or "",
                          command=prof["launch_command"])
            return ok(source="odysseus", name=p.get("label") or model_id, base_model="", command=p["cmd"])
    lib = next((m for m in scan_models(c.settings.get("paths")["models_dir"]) if m["id"] == model_id), None)
    if lib:
        return ok(source="prefab", name="", base_model=lib["name"], command=core._render(prefab_command(lib, prefab_with_launcher(c.settings.get("prefab"), c.servers.default_launcher())), "odysseus")["command"])
    try:
        cmd = next((m.get("cmd") for m in engine_api.running(_engine_url()) if m.get("model") == model_id), "")
    except Exception:  # noqa: BLE001
        cmd = ""
    if not cmd:
        return err(f"no profile, library model or running command for {model_id}", 404)
    return ok(source="engine", name="", base_model=model_id, command=cmd)


__all__ = ["bp", "apply_plan", "compute"]
