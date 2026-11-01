"""Flask backend for Argos.

Run with ``python -m src.app`` (or ``python src/app.py``) and open
http://localhost:7861 (Docker: http://localhost:8080, one port for the UI and the
OpenAI API; the builder passes every path it does not own to the engine). Environment variables set the defaults; most of them
can then be changed in the Settings view (stored in the database):

  ARGOS_DB                 SQLite file                      (default ./data.db)
  ARGOS_RUNS_DIR           launch logs                      (default ./runs)
  MODELS_DIR               GGUF library + download target   (default ./models)
  ARGOS_ENGINE_URL         the engine in this container      (default none; Docker sets http://127.0.0.1:8081)
  HF_ENDPOINT / HF_TOKEN   Hugging Face mirror and token for downloads
  ARGOS_SSH_DIR            SSH keys for servers          (default <db folder>/ssh)
  ARGOS_SERVERS_FILE       servers.yaml / .json that seeds the Servers view at start
  ARGOS_SERVERS            the same as JSON in the environment
  HOST / PORT              bind address                     (default 0.0.0.0:7861; Docker 8080)

The API launches arbitrary commands by design: only expose it on networks
where everyone who can reach it may run programs on this machine.
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
import time

from flask import Flask, request, send_from_directory

if __package__ in (None, ""):  # `python src/app.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "src"

from .assistant import Assistant  # noqa: E402
from .db import Store  # noqa: E402
from .downloader import Downloads  # noqa: E402
from . import engine_proxy, readcache  # noqa: E402
from .log_analyzer import parse_log_text  # noqa: E402
from .routes import assistant as assistant_routes  # noqa: E402
from .routes import runners as runners_routes  # noqa: E402
from .routes import bench as bench_routes, core, inventory as inventory_routes, logs, models, odysseus_hub, schedule, servers as servers_routes, workspace  # noqa: E402
from .measure import Measurer  # noqa: E402
from .runner import Runner  # noqa: E402
from .scheduler import Plans, idle_unload  # noqa: E402
from .runners import Runners  # noqa: E402
from .library import scan_media  # noqa: E402
from .servers import Servers, gguf_index, load_seed  # noqa: E402
from .integrations import engine_api  # noqa: E402
from .gpu_pick import note_cards  # noqa: E402
from .inventory import Inventory  # noqa: E402
from .library import scan_media, scan_models  # noqa: E402
from .memory import Memory  # noqa: E402
from .services import Analytics, Recorder  # noqa: E402
from .settings import Settings, env_defaults  # noqa: E402
from .telemetry import Collector  # noqa: E402
from .web import Ctx, err  # noqa: E402

log = logging.getLogger("argos.app")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOW_REQUEST_S = 2.0  # a request slower than this is logged, so a stall shows up in the container log
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def _prewarm_library(settings) -> None:
    """Walk the models folder once, off the request path.

    The walk is cached, but the first request after a restart pays for it in
    full, and on a share that is seconds. Doing it here means the first page
    load is served from a warm cache instead of waiting for a walk.
    """
    try:
        models_dir = settings.get("paths")["models_dir"]
        scan_models(models_dir)
        scan_media(models_dir)
    except Exception:  # noqa: BLE001 - a warm-up must never stop the app
        log.exception("library prewarm failed")


def _prewarm_digest(assistant) -> None:
    """Build the assistant's digest once, off the request path.

    It is a dozen database queries plus whatever the tools answer, it is
    memoised after the first build, and until this ran the first viewer paid for
    it on a page load.
    """
    try:
        assistant.context_tokens()
    except Exception:  # noqa: BLE001 - a warm-up must never stop the app
        log.exception("assistant prewarm failed")


def _env_config() -> dict:
    env = os.environ.get
    return {
        "ROOT": ROOT,
        "DB": env("ARGOS_DB") or env("PROFILEBUILDER_DB", os.path.join(ROOT, "data.db")),
        "RUNS_DIR": env("ARGOS_RUNS_DIR") or env("PROFILEBUILDER_RUNS_DIR", os.path.join(ROOT, "runs")),
        "MODELS_DIR": env("MODELS_DIR", ""),
        "ENGINE_URL": env("ARGOS_ENGINE_URL", ""),
        "HF_ENDPOINT": env("HF_ENDPOINT", ""),
        "HF_TOKEN": env("HF_TOKEN", ""),
        "SSH_DIR": env("ARGOS_SSH_DIR", ""),
        "RUNNERS_DIR": env("ARGOS_RUNNERS_DIR", ""),
        "SERVERS_FILE": env("ARGOS_SERVERS_FILE", ""),
        "SERVERS_JSON": env("ARGOS_SERVERS", ""),
        "TELEMETRY": (env("ARGOS_TELEMETRY") or env("PROFILEBUILDER_TELEMETRY", "1")) not in ("0", "false", "off"),
        "BACKGROUND": True,  # tests turn the background thread off and drive it by hand
    }


def create_app(config: dict | None = None) -> Flask:
    cfg = {**_env_config(), **(config or {})}
    app = Flask(__name__, static_folder=None)
    app.config.update(cfg)

    store = Store(cfg["DB"])
    settings = Settings(store, env_defaults(cfg))
    from .always import load_presets
    load_presets(store)
    runner = Runner(cfg["RUNS_DIR"])
    analytics = Analytics(store)

    def event(kind, message, level="info", data=None):
        store.add_event(kind, message, level=level, data=data)

    def download_settings():
        s = settings.all()
        return {"models_dir": s["paths"]["models_dir"], **s["downloads"]}

    seen_exits: set[str] = set()

    def watch_exits():
        """Log launches that ended on their own (crash, OOM, bad flag)."""
        for run in runner.list():
            if run.running or run.launch_id in seen_exits:
                continue
            seen_exits.add(run.launch_id)
            if run.stopped:
                continue
            problem = parse_log_text(runner.read_log(run.launch_id, tail_bytes=20000) or "").error
            name = run.profile_name or run.base_model or run.launch_id
            event("launch", f"{name} exited with code {run.exit_code}" + (f": {problem}" if problem else ""),
                  level="error" if run.exit_code else "info", data={"launch_id": run.launch_id})

    def refresh_plan():
        with app.app_context():
            schedule.apply_plan(force=False)
            try:
                app.extensions["pb"].runners.apply()  # a new whisper model in the folder becomes an engine model
            except Exception as exc:  # noqa: BLE001 - one bad runner must not stop the tick
                log.warning("runners apply failed: %s", exc)

    inventory = Inventory(store, event)
    try:  # the last cards the telemetry saw: AMD or NVIDIA, before the first new reading
        note_cards(store.gpu_latest())
    except Exception:  # noqa: BLE001 - a hint, never a reason not to start
        pass
    memory = Memory(store, settings.all, event)
    memory.extra_sources.append(odysseus_hub.brain_source(settings.all))

    def watch_memory():
        """Keep the assistant's memory current: new records every tick. Embedding waits for a search (see sync),
        unless the embedding model runs on the CPU: it takes nothing from the GPUs, so it embeds as it goes."""
        if not settings.get("memory")["enabled"]:
            return
        pb = app.extensions.get("pb")
        on_cpu = bool(pb and pb.assistant and pb.assistant.embed_on_cpu())
        done = memory.sync(budget=128, embed=on_cpu)
        if (done or {}).get("embedded"):
            memory.last_used = time.time()  # it is working: the idle timer does not unload it under the work

    def watch_inventory():
        """Keep the load history current, and look for new models and settings every ten minutes."""
        with app.app_context():
            inventory_routes.sync_running()
            inventory_routes.refresh_if_due()

    pool_seen: dict = {"models": None, "queue_reported": {}, "gpu": {}, "measuring": {}}

    def gpu_used() -> dict[str, float]:
        return {str(g["gpu"]): float(g.get("mem_used_mb") or 0) for g in store.gpu_latest()}

    def measure_loads(now: dict, before: dict, gpu_before: dict, gpu_now: dict, pool: dict) -> None:
        """Measure what a model really takes: the VRAM the cards gained while it loaded and nothing else changed.

        The baseline is the look before the model started; the reading is the first look after it has been
        ready for a whole tick, so its buffers are allocated. Saved as the plan's measured size, which replaces
        the estimate on the Schedule page and in the engine's sizes."""
        measuring = pool_seen["measuring"]
        for mid, (state, _) in now.items():
            was = before.get(mid, ("stopped", ()))[0]
            if state in ("ready", "starting") and was not in ("ready", "starting") and gpu_before:
                measuring[mid] = {"gpu": gpu_before, "others": {k: v[0] for k, v in before.items() if k != mid}}
        for mid in list(measuring):
            state, cards = now.get(mid, ("stopped", ()))
            if state not in ("ready", "starting"):
                measuring.pop(mid)
                continue
            if state != "ready" or before.get(mid, ("",))[0] != "ready":
                continue
            m = measuring.pop(mid)
            others_now = {k: v[0] for k, v in now.items() if k != mid}
            if any(others_now.get(k, "stopped") != v for k, v in m["others"].items()):
                continue  # something else loaded or unloaded meanwhile: the difference is not this model's
            ids = [str(c) for c in cards] or sorted(gpu_now)
            per_card = [max(0.0, gpu_now.get(i, 0.0) - m["gpu"].get(i, 0.0)) / 1024 for i in ids]
            gib = sum(per_card)
            if gib < 0.2:
                continue
            info = next((x for x in pool.get("models") or [] if x.get("id") == mid), {})
            app.extensions["pb"].plans.set_measured(mid, gib, per_card, info.get("load_seconds") or None)
            with app.app_context():
                schedule.apply_plan(force=True)  # the engine sizes by this from now on, not in five minutes
            event("schedule", f"{mid} measured at {gib:.1f} GiB while loaded" + (f" on GPU {','.join(ids)}" if cards else ""),
                  data={"model": mid, "gib": round(gib, 2), "per_card": [round(x, 2) for x in per_card]})

    def watch_pool():
        """Log what the pool router did between two looks: models loaded, moved or unloaded, and long waits."""
        url = settings.get("integrations")["engine_url"]
        if not url:
            return
        try:
            pool = engine_api.pool_state(url)
        except Exception:  # noqa: BLE001 - the Live view shows connection problems
            return
        if not pool.get("enabled"):
            pool_seen["models"] = None
            return
        now = {m["id"]: (m["state"], tuple(m.get("cards") or [])) for m in pool.get("models") or []}
        before = pool_seen["models"]
        pool_seen["models"] = now
        gpu_before, gpu_now = pool_seen["gpu"], gpu_used()
        pool_seen["gpu"] = gpu_now
        if before is None:
            return
        try:
            measure_loads(now, before, gpu_before, gpu_now, pool)
        except Exception as exc:  # noqa: BLE001 - a failed measurement must not stop the pool log
            event("schedule", f"could not measure a load: {exc}", level="warn")
        size_of = {m["id"]: m.get("size_gib") for m in pool.get("models") or []}
        up = {m for m, (s, _) in now.items() if s in ("ready", "starting") and before.get(m, ("stopped",))[0] not in ("ready", "starting")}
        down = {m for m, (s, _) in now.items() if s == "stopped" and before.get(m, ("stopped",))[0] in ("ready", "starting")}
        for mid, (state, cards) in now.items():
            was = before.get(mid, ("stopped", ()))
            on = f" on GPU {','.join(cards)}" if cards else ""
            if mid in up:
                need = f"{size_of[mid]:.1f} GiB" if size_of.get(mid) else "its size"
                if down:
                    freed = ", ".join(f"{d} ({size_of[d]:.1f} GiB)" if size_of.get(d) else d for d in sorted(down))
                    why = f"; unloaded {freed} to make room, it needs {need}"
                else:
                    why = f"; it needs {need} and fit beside the others"
                event("schedule", f"{mid} loaded{on}{why}", data={"model": mid, "cards": list(cards), "evicted": sorted(down)})
            elif mid in down:
                if not up:
                    event("schedule", f"{mid} unloaded (no longer running: idle timeout, someone unloaded it, or it stopped)", data={"model": mid})
            elif state == "ready" and was[0] == "ready" and cards != was[1]:
                event("schedule", f"{mid} moved{on}", data={"model": mid, "cards": list(cards)})
        for q in pool.get("queue") or []:
            if q.get("waited_seconds", 0) > 30 and time.time() - pool_seen["queue_reported"].get(q["model"], 0) > 300:
                pool_seen["queue_reported"][q["model"]] = time.time()
                event("schedule", f"a request for {q['model']} has waited {q['waited_seconds']:.0f}s", level="warn",
                      data={"model": q["model"], "position": q.get("position")})

    def watch_assistant():
        """Unload the assistant's chat and embedding models once they sit unused for the set number of minutes."""
        app.extensions["pb"].assistant.unload_idle()

    def watch_schedule():
        """Unload the models the schedule loaded once they sit unused for its own idle timer."""
        pb = app.extensions["pb"]
        idle_unload(pb.plans, pb.store, pb.settings.get("integrations").get("engine_url") or "", pb.event)

    collector = Collector(store, settings.all, event,
                          hooks=[watch_exits, refresh_plan, watch_pool, watch_inventory, watch_memory, watch_assistant,
                                 watch_schedule])
    plans = Plans(store)
    servers = Servers(store, cfg["SSH_DIR"] or os.path.join(os.path.dirname(os.path.abspath(cfg["DB"])), "ssh"), run=cfg.get("SSH_RUN"),
                      local_models=lambda: gguf_index([settings.get("paths")["models_dir"]]))
    servers.ensure_local()
    try:
        seeded = servers.seed(load_seed(cfg["SERVERS_FILE"], cfg["SERVERS_JSON"]), source="file")
        for problem in seeded["errors"]:
            event("server", f"servers seed: {problem}", level="warn")
    except Exception as exc:  # noqa: BLE001 - a bad seed file must not stop the app
        event("server", f"servers seed ignored: {exc}", level="warn")
    app.extensions["pb"] = Ctx(
        store=store, settings=settings, runner=runner, recorder=Recorder(store, runner), analytics=analytics,
        downloads=Downloads(store, download_settings, event), collector=collector,
        assistant=Assistant(store, settings, runner, analytics, event, inventory, memory, plans=plans),
        plans=plans, measurer=Measurer(runner, plans, lambda: collector.sample(), event), inventory=inventory,
        memory=memory, servers=servers,
        runners=Runners(store, settings, event, lambda: settings.get("paths")["models_dir"], scan_media))
    app.extensions["pb"].assistant.runners = app.extensions["pb"].runners
    from .bench import Benches, run_rows

    def save_bench(run):
        """A finished llama-bench run goes to Benchmarks: one row per test."""
        rows = run_rows(run)
        for row in rows:
            store.add_run(row)
        run.saved = len(rows)
        event("bench", f"llama-bench finished: {len(rows)} result(s) saved to Benchmarks"
              + (" (stopped)" if run.stopped else f", exit code {run.exit_code}" if run.exit_code else ""),
              level="info" if rows else "warn", data={"bench_id": run.bench_id})
    app.extensions["pb"].benches = Benches(on_done=save_bench)
    from . import gputools
    gputools.configure(settings.get("gpu"))
    app.extensions["runner"] = runner
    atexit.register(runner.stop_all)
    atexit.register(inventory.close_all)
    if cfg["BACKGROUND"]:
        collector.start()
        # Start the slow reads now, so the first visit to a page finds them ready
        # rather than paying for them. Every one of these is cached afterwards, so
        # this is about the first page load after a restart and nothing else.
        # Each runs in its own thread and none of them may stop the app.
        prewarm = (
            ("inventory-prewarm", lambda: inventory.models_summary()),
            ("library-prewarm", lambda: _prewarm_library(settings)),
            ("assistant-prewarm", lambda: _prewarm_digest(app.extensions["pb"].assistant)),
        )
        for name, work in prewarm:
            threading.Thread(target=work, name=name, daemon=True).start()

    for bp in (bench_routes.bp, core.bp, models.bp, logs.bp, assistant_routes.bp, schedule.bp, inventory_routes.bp, workspace.bp, odysseus_hub.bp, servers_routes.bp,
               runners_routes.bp):
        app.register_blueprint(bp)
    # last: anything the builder does not own goes to the engine (one port for both)
    app.register_blueprint(engine_proxy.bp)
    # In front of everything, including the proxy: a repeated read is answered from
    # memory so the polling cannot queue up behind the database's single lock.
    readcache.install(app)

    def ui_version() -> str:
        """Changes whenever a UI file changes, so a browser never runs an old app.js against a new server."""
        stamp = 0.0
        for name in os.listdir(UI_DIR):
            try:
                stamp = max(stamp, os.path.getmtime(os.path.join(UI_DIR, name)))
            except OSError:
                pass
        return str(int(stamp))

    @app.get("/")
    def index():
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        v = ui_version()
        for asset in ("app.js", "style.css", "theme.js"):
            html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={v}"')
        resp = app.response_class(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/static/<path:filename>")
    def static_files(filename):
        resp = send_from_directory(UI_DIR, filename)
        # the page asks for ?v=<version>, so a versioned file can be kept; an unversioned one is always re-checked
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "no-cache"
        return resp

    @app.before_request
    def _start_clock():
        request.environ["argos.t0"] = time.monotonic()

    @app.after_request
    def _log_slow(resp):
        took = time.monotonic() - request.environ.get("argos.t0", time.monotonic())
        if took > SLOW_REQUEST_S:
            app.logger.warning("slow request: %s %s answered %s after %.1fs", request.method, request.full_path.rstrip("?"),
                               resp.status_code, took)
        return resp

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return err("not found", 404)
        return "Not found", 404

    @app.errorhandler(405)
    def not_allowed(_e):
        return err("method not allowed", 405) if request.path.startswith("/api/") else ("Method not allowed", 405)

    @app.errorhandler(500)
    def server_error(e):
        if request.path.startswith("/api/"):
            return err(f"internal error: {getattr(e, 'original_exception', e)}", 500)
        return "Internal error", 500

    return app


def main() -> None:
    # Launched servers live in their own process groups; turn SIGTERM (docker
    # stop, systemd) into a normal exit so the atexit hook stops them too.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app = create_app()
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "7861"))
    try:
        from waitress import serve
    except ImportError:  # not installed: Flask's development server still works, only slower
        app.run(host=host, port=port, debug=False, threaded=True)
        return
    # A fixed pool and kept-alive connections, instead of a new thread and a new HTTP/1.0 connection per request.
    # The long channel timeout is for the chat and log streams, which can sit quiet while a model thinks.
    # 16 threads is not much once a couple of long-lived log and chat streams are
    # open: each holds a thread for as long as it is connected. A few slow
    # requests on top of that and the pool is full and the queue grows until the
    # page times out. The slow endpoints are fixed, and this is the headroom.
    serve(app, host=host, port=port, threads=int(os.environ.get("ARGOS_THREADS", "32")),
          channel_timeout=900, ident="argos-builder", expose_tracebacks=False)


if __name__ == "__main__":
    main()
