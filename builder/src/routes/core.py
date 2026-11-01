"""Commands, profiles, catalog, launches, perf sheet, integrations, settings."""
from __future__ import annotations

import hmac
import json
import os
import re
import shlex
import time
from typing import Optional
from urllib.parse import quote

from flask import Blueprint, abort, current_app, request

from ..command import guess_base_model, parse_command, swap_model_id, to_odysseus
from ..always import apply_always, parse_always
from ..servers import on_server
from ..gpu_pick import apply_pick, picked, resolve as resolve_gpus, vars_for as gpu_vars_for
from ..cookbook import build_state, normalize_preset
from ..integrations import engine_api
from ..library import model_id_for_command
from ..log_analyzer import insight as log_insight
from ..model_perf import model_perf, signature
from ..runner import LaunchError
from ..web import body, check_id, ctx, err, ok

bp = Blueprint("core", __name__)
CATALOG_KINDS = ("mmproj", "draft")


def _render(cmd, fmt: str) -> dict:
    if fmt == "odysseus":
        port = cmd.summary()["port"] or (cmd.get("port") or 8080)
        cmd = to_odysseus(cmd, port=port)
    d = cmd.to_dict()
    style = "odysseus" if fmt == "odysseus" else "plain"
    d["command"], d["multiline"] = cmd.to_string(style=style), cmd.to_string(multiline=True, style=style)
    model = cmd.summary()["model"]
    d["base_model"] = guess_base_model(model)
    d["model_id"] = swap_model_id(model) if model.lower().endswith(".gguf") else ""
    d["format"] = fmt
    return d


def _with_always(out: dict, cmd, fmt: str) -> dict:
    """Adds what Settings -> Always set puts on this command at launch and export: ``always_env`` and
    ``always_args`` (only what the command does not set itself), and ``run_command`` / ``run_multiline``,
    the whole command as it runs. The preview shows them; the profile keeps only its own."""
    import copy

    from ..always import expand_presets, linked_presets, presets
    linked = linked_presets(cmd)
    if linked:  # the presets this profile links to: what they add, shown like Always set, apart
        known = presets()
        expanded = copy.deepcopy(cmd)
        expand_presets(expanded)
        out["preset_env"] = {k: v for k, v in expanded.env.items() if k not in cmd.env}
        out["preset_args"] = [{"flag": a.flag, "name": a.name, "value": a.value} for a in expanded.args if a.name and not cmd.has(a.name)]
        out["presets_linked"] = [{"id": p, "name": (known.get(p) or {}).get("name") or p, "missing": p not in known} for p in linked]
        cmd = expanded
    else:
        out["preset_env"], out["preset_args"], out["presets_linked"] = {}, [], []
    env, args = parse_always(ctx().settings.get("always"))
    add_env = {k: v for k, v in env.items() if k not in cmd.env}
    add_args = [a for a in args if a.name and not cmd.has(a.name)]
    out["always_env"] = add_env
    out["always_args"] = [{"flag": a.flag, "name": a.name, "value": a.value} for a in add_args]
    out["always_added"] = [f"{k}={v}" for k, v in add_env.items()] + [" ".join(a.tokens()) for a in add_args]
    full, added = apply_always(cmd.to_string(), ctx().settings.get("always"))
    run = _render(parse_command(full), fmt) if added or linked else out
    out["run_command"], out["run_multiline"] = run["command"], run["multiline"]
    return out


def _format(d: dict) -> str:
    fmt = d.get("format") or ctx().settings.get("builder")["command_format"]
    return fmt if fmt in ("odysseus", "plain") else "plain"


# ----- commands -----------------------------------------------------------
@bp.post("/api/command/parse")
def command_parse():
    d = body()
    try:
        cmd = resolve_gpus(parse_command(d.get("command") or ""))  # an old ARGOS_GPUS shows as what it runs as
    except ValueError as exc:
        return err(str(exc))
    return ok(**_with_always(_render(cmd, _format(d)), cmd, _format(d)), gpu_vars=gpu_vars_for(cmd, picked(cmd)))


@bp.post("/api/command/build")
def command_build():
    """Apply ``edits`` ({flag: value|true|null}), ``arg_edits`` and ``env`` to ``command``.

    ``gpus`` (a list of card numbers, [] for none) pins it to those cards with the variable their
    runtime reads, decided here from the cards and where the command runs."""
    d = body()
    edits, env, arg_edits = d.get("edits") or {}, d.get("env"), d.get("arg_edits") or {}
    if not all(isinstance(x, dict) for x in (edits, arg_edits, env if env is not None else {})):
        return err("edits, arg_edits and env must be objects")
    try:
        cmd = resolve_gpus(parse_command(d.get("command") or "").with_edits(edits, env, arg_edits))
    except ValueError as exc:
        return err(str(exc))
    if isinstance(d.get("gpus"), list):
        apply_pick(cmd, d["gpus"])
    out = _with_always(_render(cmd, _format(d)), cmd, _format(d))
    out["gpu_vars"] = gpu_vars_for(cmd, picked(cmd))
    return ok(**out)


# ----- profiles (no limit, any number per model) --------------------------
@bp.get("/api/profiles")
def profiles_list():
    profiles = ctx().store.list_profiles(request.args.get("model_id") or None)
    for p in profiles:
        try:
            p["summary"] = parse_command(p["launch_command"]).summary()
        except ValueError:
            p["summary"] = {}
    return ok(profiles=profiles)


@bp.post("/api/estimate")
def estimate():
    """VRAM a command needs: model + KV cache + buffers, and the KV cache for other cache types and contexts."""
    from ..schedule_rows import estimate_command
    d = body()
    _custom_cache()
    vid = _profile_variants().get(str(d.get("profile_id") or ""))
    measured = ctx().plans.get()["measured"].get(vid) if vid else None
    return ok(estimate=estimate_command(str(d.get("command") or ""), compare=True, measured=measured), variant_id=vid)


def _custom_cache() -> None:
    from ..scheduler import set_custom_cache_types
    set_custom_cache_types(ctx().settings.get("kv")["custom_types"])


def _preset_key(preset: dict):
    """What the engine treats as one command: the model, the env and the argv."""
    try:
        cmd = parse_command(preset.get("cmd") or "")
    except ValueError:
        return None
    return swap_model_id(cmd.summary()["model"] or ""), tuple(cmd.env.items()), tuple(cmd.argv())


def _profile_variants() -> dict[str, str]:
    """profile id -> the model id the engine runs it as (what measurements are filed under).

    The engine makes one variant per model and command; the first preset with a command names it, so a
    profile saved twice with the same command shares that variant, as the engine does.
    """
    from ..cookbook import variant_id
    first: dict = {}
    out = {}
    for p in cookbook_state()["presets"]:
        key = _preset_key(p)
        vid = first.setdefault(key, variant_id(p)) if key is not None else variant_id(p)
        pid = (p.get("fields") or {}).get("profile_id")
        if pid and vid:
            out[pid] = vid
    return out


# ----- which profile a model's own name runs (what a client like Hermes asks for) -----------------
def _split_variant(vid: str) -> tuple[str, str]:
    """``<model>--<label>`` -> (model, label)."""
    model, _, label = vid.partition("--")
    return model, label


@bp.get("/api/profiles/served")
def profiles_served():
    """For each profile: the engine's name for it, and whether its model's own name runs it.

    A client asking for ``ornith-1.5-9b-q6_k`` gets that model's default command; a profile is the
    variant ``ornith-1.5-9b-q6_k--<label>``. The engine can make the plain name run a profile instead.
    """
    c = ctx()
    variants = _profile_variants()
    url = c.settings.get("integrations")["engine_url"]
    chosen: dict[str, str] = {}
    if url:
        for model in {_split_variant(v)[0] for v in variants.values()}:
            try:
                info = engine_api.model_profile(url, model)
            except Exception:  # noqa: BLE001 - shown as not chosen
                continue
            if isinstance(info, dict) and info.get("chosen"):
                chosen[model] = info["chosen"]
    return ok(profiles={pid: {"variant": vid, "model": _split_variant(vid)[0],
                              "serving": chosen.get(_split_variant(vid)[0]) == _split_variant(vid)[1]}
                        for pid, vid in variants.items()},
              engine=bool(url))


@bp.put("/api/profiles/<profile_id>/favorite")
def profile_favorite(profile_id):
    """Star (``on``) or unstar a profile."""
    check_id(profile_id)
    if not ctx().store.set_profile_favorite(profile_id, bool(body().get("on", True))):
        return err("no such profile", 404)
    return ok(profile=ctx().store.get_profile(profile_id))


@bp.put("/api/profiles/<profile_id>/serve")
def profile_serve(profile_id):
    """Make (``on``) or stop this profile being what its model's own name runs."""
    c, d = ctx(), body()
    url = c.settings.get("integrations")["engine_url"]
    if not url:
        return err("the engine is not running")
    vid = _profile_variants().get(profile_id)
    if not vid:
        return err("not a saved profile the engine can run", 404)
    model, label = _split_variant(vid)
    on = bool(d.get("on", True))
    try:
        if on:
            known = set((engine_api.model_profile(url, model) or {}).get("labels") or [])
            if label not in known:  # saved a moment ago: the engine re-reads the profiles, then reloads
                engine_api.odysseus_refresh(url)
                deadline = time.time() + PROFILE_WAIT_S
                while label not in known and time.time() < deadline:
                    time.sleep(0.5)
                    known = set((engine_api.model_profile(url, model) or {}).get("labels") or [])
            engine_api.set_model_profile(url, model, label)
            c.store.touch_profile(profile_id)
        elif (engine_api.model_profile(url, model) or {}).get("chosen") == label:
            engine_api.set_model_profile(url, model, None)
    except Exception as exc:  # noqa: BLE001
        detail = exc.read().decode("utf-8", "replace")[:200] if hasattr(exc, "read") else str(exc)
        return err(f"the engine refused: {detail}", 502)
    c.event("profile", (f"requests for {model} now run the profile {label}" if on else f"requests for {model} run its own command again"),
            data={"model": model, "variant": vid})
    return ok(model=model, variant=vid, serving=on)


@bp.get("/api/profiles/estimates")
def profiles_estimates():
    """profile id -> its VRAM estimate. Separate from /api/profiles so the list never waits on reading model headers."""
    from ..schedule_rows import estimate_command
    _custom_cache()
    variants, measured = _profile_variants(), ctx().plans.get()["measured"]
    out = {}
    for p in ctx().store.list_profiles():
        e = estimate_command(p["launch_command"], measured=measured.get(variants.get(p["profile_id"], "")))
        out[p["profile_id"]] = {k: e.get(k) for k in ("ok", "error", "gib", "weights_gib", "kv_gib", "overhead_gib", "ctx",
                                                     "cache_k", "cache_v", "notes", "measured", "estimate_gib")}
    return ok(estimates=out)


@bp.post("/api/profiles")
def profiles_save():
    c, d = ctx(), body()
    if not (d.get("name") or "").strip() or not (d.get("launch_command") or "").strip():
        return err("name and launch_command are required")
    try:
        parse_command(d["launch_command"])
    except ValueError as exc:
        return err(f"launch_command: {exc}")
    if d.get("profile_id"):
        check_id(d["profile_id"])
    d["model_id"] = model_id_for_command(d["launch_command"])
    old = c.store.get_profile(d["profile_id"]) if d.get("profile_id") else None
    existed = bool(old)
    if old and old["launch_command"].strip() != d["launch_command"].strip():
        # the measured size was for the old command: drop it, so the estimate shows until the new one loads
        vid = _profile_variants().get(old["profile_id"])
        plan = c.plans.get()
        if vid and vid in plan["measured"]:
            plan["measured"].pop(vid)
            c.plans.update({"measured": plan["measured"]})
    profile = c.store.save_profile(d)
    c.inventory.observe_command(d["launch_command"], profile["name"])
    c.event("profile", f"{'updated' if existed else 'saved'} profile “{profile['name']}”",
            data={"profile_id": profile["profile_id"], "model_id": profile["model_id"]})
    refresh_schedule()
    return ok(profile=profile)


def _delete_profile(c, profile_id: str) -> Optional[dict]:
    """Delete one profile; None when there is no such profile. A profile pulled from Odysseus is deleted
    there too, and kept out of later pulls, so it cannot come back."""
    p = c.store.get_profile(profile_id)
    if not p or not c.store.delete_profile(profile_id):
        return None
    extra = {}
    if p.get("source") == "odysseus":
        from .odysseus_hub import delete_preset
        try:
            extra = {"odysseus": delete_preset(p["name"], p["launch_command"])}
        except ValueError:
            pass
    c.event("profile", f"deleted profile “{p['name']}”" + (f"; {extra['odysseus']['odysseus']}" if extra else ""),
            data={"profile_id": profile_id})
    return extra


@bp.delete("/api/profiles/<profile_id>")
def profiles_delete(profile_id):
    c = ctx()
    extra = _delete_profile(c, check_id(profile_id))
    if extra is None:
        return err("not found", 404)
    refresh_schedule()
    return ok(**extra)


# ----- catalog of extra files ---------------------------------------------
@bp.get("/api/catalog")
def catalog_list():
    return ok(files=ctx().store.list_files(request.args.get("kind") or None,
                                           request.args.get("base_model") or None))


@bp.post("/api/catalog")
def catalog_add():
    d = body()
    kind, base = d.get("kind"), (d.get("base_model") or "").strip()
    if kind not in CATALOG_KINDS:
        return err(f"kind must be one of {', '.join(CATALOG_KINDS)}")
    if not base or not (d.get("file_path") or d.get("hf_repo")):
        return err("base_model and a file path or HF repo are required")
    fid = ctx().store.add_file(kind, base, name=(d.get("name") or "").strip(),
                               hf_repo=(d.get("hf_repo") or "").strip(),
                               file_path=(d.get("file_path") or "").strip(), notes=(d.get("notes") or "").strip())
    return ok(id=fid)


@bp.delete("/api/catalog/<int:file_id>")
def catalog_delete(file_id):
    return ok() if ctx().store.delete_file(file_id) else err("not found", 404)


@bp.get("/api/models")
def models():
    return ok(models=ctx().store.base_models())


# ----- launches -------------------------------------------------------------
ENGINE_PREFIX = "engine:"


def _engine_running(c) -> list[dict]:
    """Models the engine runs, as launches: the Playground benchmarks and reads them like its own."""
    url = c.settings.get("integrations").get("engine_url")
    if not url:
        return []
    try:
        running = engine_api.running(url, timeout=1.5)
    except Exception:  # noqa: BLE001 - an unreachable engine just adds nothing
        return []
    seen = {mid: {"started_at": t} for mid, t in c.store.open_session_starts().items()}
    out = []
    # a model that failed to start, or crashed, stays listed with why, instead of just vanishing
    failed = {}
    try:
        for m in (engine_api.pool_state(url, timeout=1.5) or {}).get("models") or []:
            if m.get("crash") and m.get("state") == "stopped":
                failed[m["id"]] = m["crash"]
    except Exception:  # noqa: BLE001 - another router, or the engine is busy: nothing to add
        pass
    for m in running:
        mid, state = str(m["model"]), str(m.get("state") or "ready")
        out.append({"launch_id": ENGINE_PREFIX + mid, "command": m.get("cmd") or "", "base_model": mid,
                    "profile_name": "", "host": "engine", "port": None,
                    "started_at": (seen.get(mid) or {}).get("started_at") or time.time(),
                    "running": state in ("ready", "starting"), "ready": state == "ready", "exit_code": None,
                    "stopped": False, "engine": True, "state": state})
    for mid, cr in failed.items():
        if any(x["base_model"] == mid for x in out):
            continue
        at = cr.get("at") or ""
        try:
            from datetime import datetime
            when = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            when = time.time()
        out.append({"launch_id": ENGINE_PREFIX + mid, "command": "", "base_model": mid, "profile_name": "", "host": "engine",
                    "port": None, "started_at": when, "running": False, "ready": False, "exit_code": 1, "stopped": False,
                    "engine": True, "state": "failed", "error": cr.get("line") or "", "tail": cr.get("tail") or []})
    return out


def _profiles_by_command(c):
    """A function giving the name of the saved profile a command is ("" for none). What Settings -> Always set adds is left
    out on both sides, since the engine runs a profile with them and the profile is saved without."""
    import json
    env, args = parse_always(c.settings.get("always"))
    drop = set(env) | {a.name for a in args if a.name}

    def key(command: str) -> str:
        _, s = signature(command)
        return json.dumps({k: v for k, v in s.items() if k not in drop}, sort_keys=True, default=str)
    names: dict[str, str] = {}
    for p in c.store.list_profiles():
        if p.get("launch_command"):
            names.setdefault(key(p["launch_command"]), p.get("name") or "")
    return lambda command: names.get(key(command or ""), "")


def _engine_profile(c, mid: str, command: str = "") -> tuple[str, str]:
    """(the plain model id, the profile's name) for a model the engine runs: ``<model>--<label>`` is that
    profile; the plain name runs the default profile chosen for it, if any; else the command is matched
    against the saved profiles. No name: the model's own command."""
    base, label = _split_variant(mid)
    url = c.settings.get("integrations").get("engine_url")
    if not label and url:
        try:
            label = (engine_api.model_profile(url, base, timeout=1.5) or {}).get("chosen") or ""
        except Exception:  # noqa: BLE001 - matched by its command instead
            label = ""
    if label:
        vid = f"{base}--{label}"
        for pid, v in _profile_variants().items():
            if v == vid:
                p = c.store.get_profile(pid)
                return base, (p or {}).get("name") or label
        return base, label
    if command:
        return base, _profiles_by_command(c)(command)
    return base, ""


class _EngineRun:
    """What the Recorder needs of a launch, for a model the engine runs."""

    def __init__(self, launch: dict):
        self.launch_id = launch["launch_id"]
        self.command = launch["command"]
        self.base_model, self.profile_name = _engine_profile(ctx(), launch["base_model"], self.command)
        self.port = None
        try:
            self.env = dict(parse_command(self.command).env) if self.command else {}
        except ValueError:
            self.env = {}


def _engine_launch(launch_id: str):
    """(engine url, the launch) for an ``engine:<model>`` id, or None when it is not one; 404 when not running."""
    if not launch_id.startswith(ENGINE_PREFIX):
        return None
    c = ctx()
    found = next((x for x in _engine_running(c) if x["launch_id"] == launch_id), None)
    if found is None:
        abort(404)
    return c.settings.get("integrations")["engine_url"], found


# llama-server colours its own log; shown as text the codes are noise ("[34m0.26.458[0m").
# The ESC byte is optional: some paths drop it and leave the rest behind.
ANSI = re.compile(r"\x1b?\[\d{1,3}(?:;\d{1,3})*m")


def _engine_log(url: str, model_id: str) -> str:
    try:
        return ANSI.sub("", engine_api.model_log(url, model_id))
    except Exception as exc:  # noqa: BLE001 - shown in place of the log
        return f"(could not read the engine's log of {model_id}: {exc})"


@bp.get("/api/engine/log/<which>")
def engine_log(which):
    """The engine's own log: "proxy" (routing, loads, the scheduler) or "upstream" (every model's output)."""
    if which not in ("proxy", "upstream"):
        abort(404)
    url = ctx().settings.get("integrations").get("engine_url")
    if not url:
        return err("the engine is not running", 503)
    tail = request.args.get("tail", 0, type=int)
    text = _engine_log(url, which)
    return current_app.response_class(text[-tail:] if tail else text, mimetype="text/plain")


@bp.get("/api/launches")
def launches():
    c = ctx()
    r = c.runner
    return ok(launches=[{**x.to_dict(), "ready": r.health(x, timeout=0.3)} for x in r.list()] + _engine_running(c))


def _engine_model(c, command: str) -> str:
    """The engine's own id for the model a command starts, or "" when it is not its model.

    A build whose llama.cpp lives in another container has no llama-server here to
    run, so a model the engine already serves belongs to the engine: it loads it on
    whichever host it is configured to launch on, with no launcher string to set.
    """
    url = c.settings.get("integrations").get("engine_url")
    model_id = model_id_for_command(command)
    if not url or not model_id:
        return ""
    try:
        return model_id if model_id in engine_api.model_ids(url) else ""
    except Exception:  # noqa: BLE001 - an unreachable engine is not this route's error
        return ""


PROFILE_WAIT_S = 10.0  # how long a launch waits for the engine to take up a just-saved profile


def _same_command(a: str, b: str) -> bool:
    try:
        return parse_command(a).to_string() == parse_command(b).to_string()
    except ValueError:
        return a.strip() == b.strip()


def _profile_variant(c, profile_id: str, command: str) -> tuple[str, str]:
    """(the engine's id for this profile's own command, why it cannot be used).

    The engine runs a base model with its own default command; a profile's command is a
    separate variant, ``<model>--<label>``. Loading the base model for a profile is how a
    Builder launch ended up at the prefab's context on one card.
    """
    p = c.store.get_profile(profile_id)
    if not p:
        return "", ""
    if not _same_command(p["launch_command"], command):
        return "", ("This profile has unsaved changes, and the engine runs saved profiles. "
                    "Save it (or Save as new), then launch.")
    vid = _profile_variants().get(profile_id, "")
    if not vid:
        return "", ""
    url = c.settings.get("integrations")["engine_url"]

    def known() -> Optional[bool]:
        """Whether the engine has the variant; None when it cannot say (variants are unlisted, and only
        the pool router lists every model)."""
        if vid in engine_api.model_ids(url):
            return True
        pool = engine_api.pool_state(url)
        if not pool.get("enabled"):
            return None
        return vid in {m.get("id") for m in pool.get("models") or []}

    try:
        if known() is not False:
            return vid, ""
        # saved a moment ago: the engine re-reads the profiles now, then picks the variant up on its
        # next config reload (it checks the file every couple of seconds)
        engine_api.odysseus_refresh(url)
        deadline = time.time() + PROFILE_WAIT_S
        while time.time() < deadline:
            if known() is not False:
                return vid, ""
            time.sleep(0.5)
    except Exception:  # noqa: BLE001 - an unreachable engine is reported below
        pass
    return "", (f"The engine does not have this profile ({vid}) yet. Check that its odysseus.baseURL points at "
                "this builder, then try again.")


def _start_in_engine(c, model_id: str, name: str, command: str, warning: str = ""):
    """Hand the load to the engine. A large model takes minutes, and its progress and
    log live on the Schedule page, so this only starts it and reports that it did."""
    import threading
    url = c.settings.get("integrations")["engine_url"]

    def load():
        try:
            engine_api.load_model(url, model_id)
        except Exception as exc:  # noqa: BLE001 - reported as an event
            c.event("launch", f"the engine could not load {model_id}: {exc}", level="error",
                    data={"model": model_id})
            return
        c.event("launch", f"the engine loaded {model_id}", data={"model": model_id})

    threading.Thread(target=load, daemon=True).start()
    c.event("launch", f"asked the engine to load {name or model_id}",
            data={"model": model_id, "command": command[:500]})
    return ok(engine=True, model=model_id, name=name or model_id, **({"warning": warning} if warning else {}))


@bp.post("/api/launches")
def launch():
    c, d = ctx(), body()
    command = (d.get("command") or "").strip()
    if not command:
        return err("command is required")
    name = (d.get("profile_name") or "").strip()
    profile_id = (d.get("profile_id") or "").strip()
    if profile_id:
        c.store.touch_profile(profile_id)
    if profile_id and c.settings.get("integrations").get("engine_url"):
        variant, problem = _profile_variant(c, profile_id, command)
        if variant:
            return _start_in_engine(c, variant, name, command)
        if problem:
            return err(problem, 409)
    always = c.settings.get("always")
    command = apply_always(command, always if always["on_launch"] else {})[0]  # env presets either way
    served = _engine_model(c, command)
    if served:
        # not a saved profile: the engine loads the model with its own command, not this one
        return _start_in_engine(c, served, name, command, warning=(
            f"The engine started {served} with its own command, not this one. "
            "Save this as a profile to launch with these settings."))
    try:
        command = on_server(parse_command(command), c.servers.default_parts()).to_string()
    except ValueError:
        pass  # the runner reports a command it cannot parse
    try:
        run = c.runner.launch(command, base_model=(d.get("base_model") or "").strip(), profile_name=name)
    except LaunchError as exc:
        c.event("launch", f"launch failed: {exc}", level="error", data={"command": command[:500]})
        return err(str(exc))
    c.event("launch", f"launched {name or run.base_model or run.launch_id} on :{run.port}",
            data={"launch_id": run.launch_id, "command": run.command})
    return ok(launch=run.to_dict())


def _run_or_404(launch_id):
    run = ctx().runner.get(check_id(launch_id))
    if run is None:
        abort(404)
    return run


@bp.get("/api/launches/<launch_id>")
def launch_status(launch_id):
    run = _run_or_404(launch_id)
    return ok(launch={**run.to_dict(), "ready": ctx().runner.health(run)})


@bp.post("/api/launches/<launch_id>/stop")
def launch_stop(launch_id):
    eng = _engine_launch(launch_id)
    if eng:
        url, launch = eng
        try:
            engine_api.unload_model(url, launch["base_model"])
        except Exception as exc:  # noqa: BLE001
            return err(f"the engine could not unload {launch['base_model']}: {exc}", 502)
        ctx().event("launch", f"unloaded {launch['base_model']} from the playground", data={"model": launch["base_model"]})
        return ok(launch={**launch, "running": False, "stopped": True})
    c, run = ctx(), _run_or_404(launch_id)
    was_running = run.running
    c.runner.stop(run.launch_id)
    if was_running:
        c.event("launch", f"stopped {run.profile_name or run.base_model or run.launch_id}",
                data={"launch_id": run.launch_id})
    return ok(launch=run.to_dict())


@bp.get("/api/launches/<launch_id>/log")
def launch_log(launch_id):
    eng = _engine_launch(launch_id)
    if eng:
        tail = request.args.get("tail", 0, type=int)
        text = _engine_log(eng[0], eng[1]["base_model"])
        return current_app.response_class(text[-tail:] if tail else text, mimetype="text/plain")
    text = ctx().runner.read_log(check_id(launch_id), tail_bytes=request.args.get("tail", 0, type=int))
    if text is None:
        abort(404)
    return current_app.response_class(ANSI.sub("", text), mimetype="text/plain")


@bp.get("/api/launches/<launch_id>/insight")
def launch_insight(launch_id):
    eng = _engine_launch(launch_id)
    text = (_engine_log(eng[0], eng[1]["base_model"]) if eng
            else ctx().runner.read_log(check_id(launch_id), tail_bytes=1_000_000))
    if text is None:
        abort(404)
    return ok(**log_insight(text))


@bp.post("/api/similar")
def similar_configs():
    """Configs and presets closest to a command: by meaning when embeddings are on, by words otherwise."""
    c, d = ctx(), body()
    command = str(d.get("command") or "")
    _cid, settings = signature(command)
    model = str(d.get("model") or settings.get("model") or "")
    query = f"Config of {model}: " + ", ".join(f"{k} {v}" for k, v in settings.items() if k not in ("model", "mmproj") and v is not True)[:600]
    hits = c.memory.search(query, max(1, min(int(d.get("k") or 6), 20)), {"config", "preset"})
    out = []
    for hit in hits:
        row = {"source": hit["source"], "key": hit["key"], "text": hit["text"], "similarity": hit["similarity"], "matched_by": hit["matched_by"]}
        if hit["source"] == "config":
            mid, _, cfg = hit["key"].partition("|")
            conf = next((x for x in model_perf(c.store, mid, days=180)["configs"] if x["id"] == cfg), None)
            if conf:
                row.update(model=mid, label=conf["label"], decode=conf["decode"].get("median"), prefill=conf["prefill"].get("median"))
        out.append(row)
    return ok(results=out)


@bp.post("/api/launches/<launch_id>/bench")
def launch_bench(launch_id):
    c, d = ctx(), body()
    try:
        words = max(1, min(int(d.get("prompt_words", 400)), 50_000))
        n_predict = max(1, min(int(d.get("n_predict", 128)), 8192))
    except (TypeError, ValueError):
        return err("prompt_words and n_predict must be integers")
    eng = _engine_launch(launch_id)
    if eng:
        url, launch = eng
        mid = launch["base_model"]
        try:
            timings = c.runner.bench_url(f"{url.rstrip('/')}/upstream/{quote(mid, safe='')}/completion",
                                         prompt_words=words, n_predict=n_predict, where=f"through the engine ({mid})")
        except LaunchError as exc:
            c.event("bench", f"benchmark failed for {mid}: {exc}", level="error")
            return err(str(exc), 502)
        rec = c.recorder.record(launch_id, timings=timings, note=d.get("note") or "", run=_EngineRun(launch),
                                log_text=_engine_log(url, mid))
        c.assistant.warm_embed()  # the new numbers are worth searching: start the embedder
        c.event("bench", f"benchmark {rec['base_model']}: {rec['tokens_per_second'] or 0:.1f} t/s gen, "
                         f"{rec['prompt_per_second'] or 0:.0f} t/s prompt", data={"run_id": rec["run_id"]})
        return ok(run=rec)
    run = _run_or_404(launch_id)
    if not run.running:
        return err("process is not running")
    try:
        timings = c.runner.bench(run, prompt_words=words, n_predict=n_predict)
    except LaunchError as exc:
        c.event("bench", f"benchmark failed for {run.base_model or run.launch_id}: {exc}", level="error")
        return err(str(exc), 502)
    rec = c.recorder.record(run.launch_id, timings=timings, note=d.get("note") or "")
    c.assistant.warm_embed()  # the new numbers are worth searching: start the embedder
    c.event("bench", f"benchmark {rec['base_model']}: {rec['tokens_per_second'] or 0:.1f} t/s gen, "
                     f"{rec['prompt_per_second'] or 0:.0f} t/s prompt", data={"run_id": rec["run_id"]})
    return ok(run=rec)


@bp.post("/api/launches/<launch_id>/record")
def launch_record(launch_id):
    eng = _engine_launch(launch_id)
    if eng:
        rec = ctx().recorder.record(launch_id, note=body().get("note") or "", run=_EngineRun(eng[1]),
                                    log_text=_engine_log(eng[0], eng[1]["base_model"]))
        return ok(run=rec)
    run = _run_or_404(launch_id)
    rec = ctx().recorder.record(run.launch_id, note=body().get("note") or "")
    ctx().assistant.warm_embed()  # the new numbers are worth searching: start the embedder
    return ok(run=rec)


# ----- perf sheet -------------------------------------------------------------
@bp.get("/api/runs")
def runs_list():
    limit = max(1, min(request.args.get("limit", 1000, type=int), 100_000))
    runs = ctx().store.list_runs(request.args.get("base_model") or None, limit)
    # runs saved without a profile name (a model the engine ran was always saved that way): name the profile
    # its command is, or the one its engine name says
    unnamed = [r for r in runs if not r.get("profile_name")]
    if unnamed:
        profile_of = _profiles_by_command(ctx())
        for r in unnamed:
            name = profile_of(r.get("launch_command") or "")
            lid = str(r.get("launch_id") or "")
            if not name and lid.startswith(ENGINE_PREFIX) and "--" in lid:
                name = _split_variant(lid[len(ENGINE_PREFIX):])[1]
            if name:
                r["profile_name"] = name
    # The count comes back either way, so a caller never has to ask for a hundred
    # thousand rows to find out how many there are.
    return ok(runs=runs, total=ctx().store.count_runs())


@bp.delete("/api/runs/<run_id>")
def runs_delete(run_id):
    return ok() if ctx().store.delete_run(check_id(run_id)) else err("not found", 404)


@bp.get("/api/analytics/best")
def analytics_best():
    return ok(rows=ctx().analytics.best_by_base_model())


@bp.get("/api/env-flags")
def env_flags():
    return ok(rows=ctx().analytics.env_table())


@bp.get("/api/flags")
def server_flags():
    return ok(rows=ctx().analytics.flag_table())


# ----- your changes to the Flags page ----------------------------------------------------
def _flag_known(kind: str) -> set[str]:
    a = ctx().analytics
    return {r["flag"] for r in (a.flag_table(edits=False) if kind == "flag" else a.env_table(edits=False))}


@bp.post("/api/flags/<kind>")
def flag_add(kind):
    """Add a flag (kind "flag") or an environment variable (kind "env") of your own."""
    from ..flag_edits import KINDS, FlagEditError, FlagEdits
    if kind not in KINDS:
        return err("kind is flag or env", 404)
    try:
        name = FlagEdits(ctx().store).add(kind, body(), _flag_known(kind))
    except FlagEditError as exc:
        return err(str(exc))
    ctx().event("settings", f"added {kind} {name} to the Flags page")
    return ok(name=name)


# ----- env presets: named sets of env vars and flags a profile links to ------------------
@bp.get("/api/env-presets")
def env_presets_list():
    from ..always import linked_presets, presets
    used: dict[str, list[str]] = {}  # preset id -> the profiles linked to it
    for p in ctx().store.list_profiles():
        try:
            cmd = parse_command(p.get("launch_command") or "")
        except ValueError:
            continue
        for pid in linked_presets(cmd):
            used.setdefault(pid, []).append(p.get("name") or "")
    return ok(presets=[{"id": k, **v, "profiles": used.get(k, [])} for k, v in sorted(presets().items(), key=lambda x: x[1].get("name", "").lower())])


@bp.put("/api/env-presets/<pid>")
def env_preset_save(pid):
    """Body: {name, env (KEY=VALUE per line), flags}. A new id creates one; every profile linked to it follows."""
    from ..always import parse_always, presets, save_presets
    d = body()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", pid or ""):
        return err("the id is lower case letters, digits and dashes")
    name = str(d.get("name") or "").strip()[:60]
    if not name:
        return err("give the preset a name")
    entry = {"name": name, "env": str(d.get("env") or "").strip()[:4000], "flags": str(d.get("flags") or "").strip()[:2000]}
    env, args = parse_always(entry)
    if not env and not args:
        return err("a preset needs at least one KEY=VALUE line or flag")
    all_ = presets()
    all_[pid] = entry
    save_presets(ctx().store, all_)
    refresh_schedule()  # the engine's commands follow the preset
    ctx().event("settings", f"saved env preset {name}")
    return ok(id=pid)


@bp.post("/api/env-presets/<pid>/apply")
def env_preset_apply(pid):
    """Body: {command, part: "env" | "flags" | "all"}. Copies the preset's values into the command as its own,
    to edit like any other: what the command sets already stays. A link to this preset is dropped (its values
    are now in the command)."""
    import copy

    from ..always import PRESET_VAR, linked_presets, parse_always, presets
    p = presets().get(pid)
    if not p:
        return err("no such preset", 404)
    d = body()
    part = d.get("part") or "all"
    try:
        cmd = parse_command(d.get("command") or "")
    except ValueError as exc:
        return err(str(exc))
    env, args = parse_always(p)
    added = []
    if part in ("env", "all"):
        for k, v in env.items():
            if k not in cmd.env:
                cmd.env[k] = v
                added.append(f"{k}={v}")
    if part in ("flags", "all"):
        for a in args:
            if a.name and not cmd.has(a.name):
                cmd.args.append(copy.copy(a))
                added.append(" ".join(a.tokens()))
    rest = [x for x in linked_presets(cmd) if x != pid]
    if rest:
        cmd.env[PRESET_VAR] = ",".join(rest)
    else:
        cmd.env.pop(PRESET_VAR, None)
    return ok(command=cmd.to_string(), added=added)


@bp.delete("/api/env-presets/<pid>")
def env_preset_delete(pid):
    from ..always import presets, save_presets
    all_ = presets()
    if pid not in all_:
        return err("no such preset", 404)
    name = all_.pop(pid).get("name") or pid
    save_presets(ctx().store, all_)
    refresh_schedule()
    ctx().event("settings", f"deleted env preset {name}: profiles linked to it run without it")
    return ok()


@bp.post("/api/flags/import")
def flag_import():
    """Body: {text}: a command or a line of KEY=VALUE. Adds each flag and variable in it that the page lacks."""
    from ..flag_edits import FlagEditError, FlagEdits, name_for
    text = str(body().get("text") or "").strip()
    if not text:
        return err("paste a command or some KEY=VALUE pairs")
    try:
        words = shlex.split(text)
    except ValueError as exc:
        return err(f"that does not read as a command line: {exc}")
    found: list[tuple[str, str]] = []
    for w in words:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            found.append(("env", w.split("=", 1)[0]))
        elif re.match(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*(=.*)?$", w):
            found.append(("flag", w.split("=", 1)[0]))
    edits = FlagEdits(ctx().store)
    known = {"env": _flag_known("env"), "flag": _flag_known("flag")}
    added, listed = [], []
    for kind, raw in found:
        try:
            name = name_for(kind, raw)
            current = edits.all()[kind].get(name)
            if (name in known[kind] and not (current or {}).get("removed")) or (current and not current.get("removed")):
                listed.append(raw)
                continue
            edits.add(kind, {"name": name}, known[kind])
            added.append(raw)
        except FlagEditError:
            continue
    if added:
        ctx().event("settings", f"added {', '.join(added)} to the Flags page")
    return ok(added=added, known=listed)


@bp.post("/api/flags/<kind>/describe")
def flag_describe(kind):
    """Add a flag or variable with this description, or give the one already on the list this description:
    what the assistant's "describe" proposal does when the user accepts it."""
    from ..flag_edits import KINDS, FlagEditError, FlagEdits, name_for
    if kind not in KINDS:
        return err("kind is flag or env", 404)
    d = body()
    edits, known = FlagEdits(ctx().store), _flag_known(kind)
    try:
        name = name_for(kind, str(d.get("name") or ""))
        current = edits.all()[kind].get(name)
        if (name in known and not (current or {}).get("removed")) or (current and not current.get("removed")):
            edits.update(kind, name, {"description": d.get("description") or ""})
            added = False
        else:
            edits.add(kind, {"name": name, "description": d.get("description") or ""}, known)
            added = True
    except FlagEditError as exc:
        return err(str(exc))
    ctx().event("settings", f"{'added' if added else 'described'} {kind} {name} on the Flags page")
    return ok(name=name, added=added)


@bp.put("/api/flags/<kind>/<path:name>")
def flag_update(kind, name):
    from ..flag_edits import KINDS, FlagEditError, FlagEdits
    if kind not in KINDS:
        return err("kind is flag or env", 404)
    edits = FlagEdits(ctx().store)
    if name not in _flag_known(kind) and name not in edits.all()[kind]:
        return err("not on the list", 404)
    try:
        edits.update(kind, name, body())
    except FlagEditError as exc:
        return err(str(exc))
    return ok()


@bp.delete("/api/flags/<kind>/<path:name>")
def flag_remove(kind, name):
    """A row you added is deleted; a built-in one is hidden and can be restored."""
    from ..flag_edits import KINDS, FlagEdits
    if kind not in KINDS:
        return err("kind is flag or env", 404)
    edits = FlagEdits(ctx().store)
    if name not in _flag_known(kind) and name not in edits.all()[kind]:
        return err("not on the list", 404)
    result = edits.remove(kind, name)
    ctx().event("settings", f"{'deleted' if result == 'deleted' else 'hid'} {kind} {name} on the Flags page")
    return ok(result=result)


@bp.post("/api/flags/<kind>/<path:name>/restore")
def flag_restore(kind, name):
    """Bring a built-in row back as shipped: shown again, with its original texts."""
    from ..flag_edits import KINDS, FlagEdits
    if kind not in KINDS:
        return err("kind is flag or env", 404)
    return ok() if FlagEdits(ctx().store).restore(kind, name) else err("nothing to restore", 404)


# ----- Odysseus presets, and the Odysseus-compatible state the engine reads -------------
@bp.get("/api/integrations")
def integrations():
    """What the builder can import from Odysseus: its saved Cookbook presets, read through its API."""
    from .odysseus_hub import cookbook_presets

    found = cookbook_presets()
    commands = [{"source": "odysseus", "kind": "preset", "name": p.get("label") or p.get("name") or "preset",
                 "base_model": str(p.get("model") or "").split("/")[-1], "command": p["cmd"].strip()}
                for p in map(normalize_preset, found["presets"]) if p["cmd"].strip()]
    return ok(odysseus={"configured": bool(c_odysseus_url()), "ok": not found["error"], "error": found["error"], "commands": commands})


def c_odysseus_url() -> str:
    return ctx().settings.get("odysseus")["url"]


def cookbook_state() -> dict:
    from .odysseus_hub import cookbook_presets

    c = ctx()
    always = c.settings.get("always")
    return build_state(c.store.list_profiles(), c.runner.list(), cookbook_presets(), always if always["on_export"] else None,
                       server=c.servers.default_parts())


@bp.get("/api/cookbook/state")
def cookbook_state_route():
    """Same path and shape as Odysseus: the engine's odysseus.baseURL points here. ARGOS_COOKBOOK_TOKEN, when set, is required."""
    token = os.environ.get("ARGOS_COOKBOOK_TOKEN", "")
    if token and not hmac.compare_digest(request.headers.get("X-Odysseus-Internal-Token", ""), token):
        return err("bad or missing X-Odysseus-Internal-Token", 401)
    state = cookbook_state()
    state.pop("warnings", None)
    return state


def refresh_schedule() -> None:
    """A profile was saved or deleted: that adds or removes a model in the scheduling plan, and the engine
    should run the new command now, not at its next poll (odysseus.refreshSeconds, five minutes by default).
    A running model whose command changed restarts with it; one busy with a request waits until it is done."""
    from . import schedule

    schedule.apply_plan()
    url = ctx().settings.get("integrations")["engine_url"]
    if url:
        import threading

        def tell():
            try:
                engine_api.odysseus_refresh(url)
            except Exception:  # noqa: BLE001 - the engine still picks it up on its own poll
                pass
        threading.Thread(target=tell, daemon=True, name="profile-refresh").start()


# ----- settings -------------------------------------------------------------------
@bp.get("/api/settings")
def settings_get():
    return ok(settings=ctx().settings.public())


@bp.put("/api/settings/<group>")
def settings_put(group):
    c = ctx()
    try:
        saved = c.settings.update(group, body())
    except (KeyError, ValueError) as exc:
        return err(str(exc).strip("'\""))
    c.event("settings", f"saved {group} settings")
    if group == "gpu":
        from .. import gputools
        gputools.configure(saved)
        try:
            c.runners.apply()  # the runners' environment follows the toolkit
        except Exception:  # noqa: BLE001 - the next tick writes it
            pass
    if group in ("odysseus", "always"):
        c.export_state.pop("_ody_presets", None)
    if group in ("assistant", "memory"):
        refresh_schedule()  # its models, and whether they fall back to the CPU, are in the plan
    return ok(settings=c.settings.public()[group], saved=bool(saved))
