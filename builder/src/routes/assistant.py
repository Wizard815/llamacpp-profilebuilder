"""The assistant chat: state, streaming turns, compacting, loading and unloading its models, and one-click setup."""
from __future__ import annotations

import json
import os
import re

from flask import Blueprint, Response, request, stream_with_context

from ..assistant import (DEFAULT_QUANT, DEFAULT_REPOS, ESCALATE_PROFILE_NAME, ESCALATE_REPOS, PROFILE_NAME,
                         AssistantError, pick_quant)
from ..downloader import DownloadError
from ..library import forget_scans, model_id_for_command, prefab_command, scan_models
from ..web import body, ctx, err, ok

bp = Blueprint("assistant", __name__)


@bp.get("/api/assistant")
def state():
    c = ctx()
    a = c.assistant
    return ok(messages=c.store.list_messages(), summary=a.summary(), tokens=a.context_tokens(),
              budget=a.budget(), server=a.server_state(), settings=c.settings.get("assistant"), models=a.models())


@bp.get("/api/assistant/status")
def status():
    """Only which models are loaded: the nav dot, without the conversation, the digest or the memory."""
    return ok(models=ctx().assistant.models())


@bp.post("/api/assistant/compact")
def compact():
    """Shrink the conversation now: every turn but the last two goes into the summary."""
    try:
        return ok(**ctx().assistant.compact_now())
    except AssistantError as exc:
        return err(str(exc), 409)
    except Exception as exc:  # noqa: BLE001 - the model may be down
        return err(f"could not compact: {exc}", 502)


@bp.post("/api/assistant/models/<which>/<action>")
def model_action(which, action):
    """Load or unload the chat model ("chat") or the memory's embedding model ("embed")."""
    if which not in ("chat", "embed") or action not in ("load", "unload"):
        return err("use /api/assistant/models/chat|embed/load|unload", 404)
    a = ctx().assistant
    try:
        state = a.load(which) if action == "load" else a.unload(which)
    except AssistantError as exc:
        return err(str(exc), 409)
    return ok(model=state, models=a.models())


@bp.get("/api/assistant/digest")
def digest():
    return ok(digest=ctx().assistant.digest(request.args.get("q", "")))


@bp.post("/api/assistant/chat")
def chat():
    d = body()
    text = str(d.get("message") or "")
    builder = d.get("builder") if isinstance(d.get("builder"), dict) else None
    escalate = bool(d.get("escalate"))
    assistant = ctx().assistant

    def events():
        for ev in assistant.chat(text, builder=builder, escalate=escalate):
            yield f"data: {json.dumps(ev)}\n\n"

    return Response(stream_with_context(events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.post("/api/assistant/ask")
def ask():
    """A one-off question about some log lines (the Events view sends the ones on screen)."""
    d = body()
    question = str(d.get("question") or "").strip()
    if not question:
        return err("ask a question")
    lines = [str(x)[:300] for x in (d.get("lines") or [])][:60]
    try:
        answer = ctx().assistant.ask(question, "\n".join(lines), str(d.get("title") or "Log lines"))
    except Exception as exc:  # noqa: BLE001 - the assistant may not be set up
        return err(f"the assistant could not answer: {exc}", 502)
    return ok(answer=answer)


@bp.get("/api/assistant/memory")
def memory_status():
    return ok(**ctx().memory.status())


@bp.post("/api/assistant/memory/search")
def memory_search():
    d = body()
    q = str(d.get("q") or "").strip()
    if not q:
        return err("type something to search for")
    sources = set(d.get("sources") or []) or None
    return ok(results=ctx().memory.search(q, max(1, min(int(d.get("k") or 10), 50)), sources))


@bp.post("/api/assistant/memory/sync")
def memory_sync():
    return ok(result=ctx().memory.sync(budget=max(1, min(int(body().get("budget") or 256), 5000))), **ctx().memory.status())


@bp.post("/api/assistant/memory/rebuild")
def memory_rebuild():
    c = ctx()
    c.memory.rebuild()
    c.event("memory", "cleared the assistant's memory vectors; they are rebuilt in the background")
    return ok(**c.memory.status())


@bp.post("/api/assistant/reset")
def reset():
    ctx().assistant.reset()
    return ok()


@bp.post("/api/assistant/stop")
def stop():
    ctx().assistant.stop()
    return ok()


@bp.get("/api/assistant/choices")
def choices():
    """What the settings dropdowns offer: chat models (library models and profiles) and embedding models,
    all models Argos has. Each says whether the engine lists it right now."""
    from ..cookbook import variant_id
    from ..integrations import engine_api
    from . import core
    c = ctx()
    url = c.settings.get("integrations")["engine_url"]
    listed: set[str] = set()
    connected = False
    if url:
        try:
            listed, connected = engine_api.model_ids(url), True
        except Exception:  # noqa: BLE001 - offer the library and profiles anyway
            pass
    library = scan_models(c.settings.get("paths")["models_dir"])
    chat, embed, seen = [], [], set()
    variants = {(p.get("fields") or {}).get("profile_id"): variant_id(p) for p in core.cookbook_state()["presets"]}
    kind_by_file = {os.path.basename(m["path"]): m["kind"] for m in library}
    for p in c.store.list_profiles():
        vid = variants.get(p["profile_id"])
        if vid and vid not in seen:
            seen.add(vid)
            item = {"id": vid, "label": p["name"], "group": "Profiles", "listed": vid in listed}
            (embed if _embeds(p.get("launch_command") or "", kind_by_file) else chat).append(item)
    for m in library:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        item = {"id": m["id"], "label": m["name"], "group": "Models", "listed": m["id"] in listed, "quant": m["quant"]}
        (embed if m["kind"] in ("embedding", "reranker") else chat).append(item)
    for mid in sorted(listed - seen):  # odysseus presets and anything else the engine makes
        item = {"id": mid, "label": mid, "group": "Also in the engine", "listed": True}
        (embed if re.search(r"(?i)embed|(?<![a-z])(bge|e5|gte)-|rerank", mid) else chat).append(item)
    return ok(chat=chat, embed=embed, engine_connected=connected)


def _embeds(command: str, kind_by_file: dict) -> bool:
    """A profile that serves embeddings: --embedding (or --pooling / --rerank) in its command, or a model file the
    library knows as an embedding or reranker model."""
    from ..command import parse_command
    try:
        cmd = parse_command(command)
    except ValueError:
        return False
    if any(cmd.has(f) for f in ("embedding", "embeddings", "pooling", "rerank", "reranking")):
        return True
    return kind_by_file.get(os.path.basename(cmd.summary()["model"] or "")) in ("embedding", "reranker")


def _local_model(models_dir: str, pattern: str, want: str):
    """A local file for this model already, preferring the quant that was asked for."""
    forget_scans()  # deciding whether to download: look at the disk, not at a minute-old answer
    found = [m for m in scan_models(models_dir) if re.search(pattern, m["name"], re.IGNORECASE) and m["kind"] == "chat"]
    found.sort(key=lambda m: (m["quant"] != want, m["size"]))
    return found[0] if found else None


@bp.post("/api/assistant/setup")
def setup():
    """Point the assistant at a model: reuse a local file, or download it.

    ``which`` picks the model: the assistant's own ("chat", the default) or the one a
    hard question is handed to ("escalate"). They keep separate quants, because the
    escalation model may run on CPU, where the smaller quant is the faster one.
    """
    c, d = ctx(), body()
    which = "escalate" if str(d.get("which") or "").strip().lower() == "escalate" else "chat"
    cfg = c.settings.get("assistant")
    repos = ESCALATE_REPOS if which == "escalate" else DEFAULT_REPOS
    want = str(cfg.get("escalate_quant" if which == "escalate" else "quant") or DEFAULT_QUANT)
    pattern = r"qwen3\.5-2b" if which == "escalate" else r"qwen3-0\.6b"
    profile_name = ESCALATE_PROFILE_NAME if which == "escalate" else PROFILE_NAME
    label = "Qwen3.5-2B" if which == "escalate" else "Qwen3-0.6B"
    models_dir = c.settings.get("paths")["models_dir"]
    model, job = _local_model(models_dir, pattern, want), None
    if not model:
        info, quant, problems = None, None, []
        for repo in ([d["repo"]] if d.get("repo") else repos):
            try:
                info = c.downloads.client().repo(repo)
            except DownloadError as exc:
                problems.append(f"{repo}: {exc}")
                continue
            quant = pick_quant(info["quants"], want=want)
            if quant:
                break
        if not quant:
            return err(f"Could not find {label} on Hugging Face at {want}. " + "; ".join(problems), 502)
        sizes = {f["name"]: f["size"] for f in info["files"]}
        try:
            job = c.downloads.start(info["id"], [{"name": n, "size": sizes.get(n, 0)} for n in quant["files"]])
        except DownloadError as exc:
            return err(str(exc), 502)
        model = {"path": os.path.join(job["dest"], quant["files"][0]), "kind": "chat", "mmproj": []}
    cfg = c.settings.get("assistant")
    prefab = c.settings.get("prefab")
    prefab = {**prefab, "host": "127.0.0.1", "ctx": str(cfg["context_tokens"]), "cache_type": "",
              "split_mode": "none" if prefab.get("split_mode") else ""}
    cmd = prefab_command(model, prefab, port="${PORT}").to_string(style="odysseus")
    existing = c.store.get_profile(cfg["profile_id"]) if cfg["profile_id"] else None
    profile = c.store.save_profile({
        "profile_id": existing["profile_id"] if existing and existing["name"] == profile_name else None,
        "name": profile_name, "base_model": label, "model_id": model_id_for_command(cmd),
        "source": "assistant", "launch_command": cmd,
        "notes": "Built-in assistant model" if which == "chat" else "Assistant escalation model",
    })
    from .workspace import _variant_for_profile
    variant = _variant_for_profile(profile["profile_id"]) or model_id_for_command(cmd)
    # The escalation model is named rather than switched on: the assistant's own model
    # keeps answering, and this one is only asked when a turn hands over.
    if which == "escalate":
        c.settings.update("assistant", {"escalate_model": variant})
    else:
        c.settings.update("assistant", {"mode": "engine", "model": variant, "profile_id": profile["profile_id"]})
    c.event("assistant", f"assistant set to {profile['name']}" + (" (downloading model)" if job else ""))
    return ok(profile=profile, download=job, model=variant, which=which)
