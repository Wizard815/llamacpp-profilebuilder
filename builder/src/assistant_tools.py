"""Read-only lookups the assistant answers from, chosen by the question.

The assistant is a 0.6B model. Handed a wall of numbers it mixed them up: it gave a GPU's total VRAM to
each model, ranked by the wrong column, and listed two models out of thirty. So the arithmetic, filtering
and ranking happen here, in code, and the model is told to copy the result. Each tool returns a few plain
lines with the unit on every number. Nothing here changes anything.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from .integrations import engine_api
from .inventory import params_from_name

LIST_LIMIT = 40

_PARAM = re.compile(r"(?<![\w.])(\d*\.?\d+)\s?b(?![a-z])", re.I)
_NEAR = re.compile(r"\b(close|closest|near|nearest|around|about|roughly|approximately)\b|~", re.I)


def _gib(v: Optional[float]) -> str:
    return "?" if v is None else f"{v:.2f} GiB"


def _params_b(text: str) -> Optional[float]:
    """'9B' -> 9.0, '0.6B' -> 0.6, '.6B' -> 0.6."""
    m = _PARAM.search(text or "")
    return float(m.group(1)) if m else None


def _rows(inv) -> list[dict]:
    """Models with a file on disk, one row each, with parameters and size worked out."""
    extra = {m["model_id"]: (m.get("extra") or {}) for m in inv.store.list_models_seen()}
    out = []
    for m in inv.models_summary():
        if not m["present"] or m["origin"] == "engine" or m["kind"] in ("mmproj", "mtp", "draft"):
            continue
        p = extra.get(m["model_id"], {}).get("params") or params_from_name(m["name"])
        out.append({**m, "params": p, "params_b": _params_b(p) if p else None})
    return out


def _line(m: dict) -> str:
    bits = [f"{m['name']}", _gib(m["size_gib"]), m["params"] or "params unknown", "/".join(m["quants"]) or m.get("arch") or ""]
    return "- " + ", ".join(b for b in bits if b)


# ----- the tools ---------------------------------------------------------------------
def models_on_disk(ctx: dict, q: str) -> list[str]:
    rows = _rows(ctx["inventory"])
    if not rows:
        return ["MODELS ON DISK: none found."]
    want = _params_b(q)
    low = q.lower()
    title = f"MODELS ON DISK ({len(rows)} in total, sizes are file sizes in GiB)"
    if want is not None and _NEAR.search(q):
        known = [m for m in rows if m["params_b"] is not None]
        rows = sorted(known, key=lambda m: (abs(m["params_b"] - want), m["size_gib"] or 0))
        title += f", closest to {want:g}B first"
        near = [m for m in rows if abs(m["params_b"] - want) <= max(1.0, want * 0.25)]
        rows = near or rows
        title += " (the 'about' list below is every model within 25%)" if near else ""
    elif want is not None:
        same = [m for m in rows if m["params_b"] == want]
        if same:
            rows, title = same, title + f", only {want:g}B models"
    if any(w in low for w in ("smallest", "lightest", "least", "tiniest")):
        rows = sorted(rows, key=lambda m: m["size_gib"] or 0)
        title += ", smallest file first"
    elif any(w in low for w in ("largest", "biggest", "heaviest")):
        rows = sorted(rows, key=lambda m: -(m["size_gib"] or 0))
        title += ", largest file first"
    elif want is None:
        rows = sorted(rows, key=lambda m: m["name"].lower())
    more = len(rows) - LIST_LIMIT
    return [title + ":"] + [_line(m) for m in rows[:LIST_LIMIT]] + ([f"(+{more} more)"] if more > 0 else [])


def loaded_now(ctx: dict, q: str) -> list[str]:
    url = ctx["engine_url"]
    if not url:
        return ["LOADED NOW: the engine address is not set, so loaded models are unknown."]
    try:
        running = engine_api.running(url)
    except Exception as exc:  # noqa: BLE001 - said in the answer
        return [f"LOADED NOW: could not reach the engine ({exc})."]
    if not running:
        return ["LOADED NOW: no model is loaded."]
    try:
        pool = {m["id"]: m for m in engine_api.pool_state(url).get("models") or []}
    except Exception:  # noqa: BLE001 - group router: no sizes
        pool = {}
    plan = ctx["measured"]
    own = ctx.get("own_models") or set()
    lines = [f"LOADED NOW ({len(running)} model(s); each size is that model's own memory, not the GPU total). "
             "Speeds are that model's own, from its log and its last request; copy them as written:"]
    total = 0.0

    def busy(m: dict) -> bool:
        return bool(m.get("slots") or m.get("in_flight"))
    # the models working now first, the assistant's own last: "what is running" means the work, not the helper
    order = sorted(running, key=lambda r: (r["model"] in own, not busy(pool.get(r["model"]) or {}), r["model"]))
    for r in order:
        mid = r["model"]
        p = pool.get(mid) or {}
        gib = (plan.get(mid) or {}).get("gib") or p.get("size_gib")
        kind = "measured" if (plan.get(mid) or {}).get("gib") else "estimated"
        cards = p.get("cards") or []
        where = "on the CPU" if p.get("on_cpu") else f"on GPU {','.join(cards)}" if cards else ""
        total += 0 if p.get("on_cpu") else (gib or 0)
        doing = []
        for sl in p.get("slots") or []:  # what each busy slot does right now
            if sl.get("phase") == "prefill":
                doing.append(f"reading a prompt ({sl.get('tokens', 0)} of {sl.get('total') or '?'} tokens) at {sl.get('tokens_per_second') or 0:.0f} t/s prefill")
            else:
                doing.append(f"writing an answer at {sl.get('gen_tokens_per_second') or 0:.1f} t/s decode")
        status = "; ".join(doing) or ("busy" if p.get("in_flight") else "idle")
        last = ""
        try:
            req = next((x for x in ctx["store"].list_requests(model=mid, limit=5) if (x.get("tokens_per_second") or 0) > 0), None)
        except Exception:  # noqa: BLE001 - speeds are a bonus
            req = None
        if req:
            rng = (f", fastest {req['prefill_high_tps']:.0f} slowest {req['prefill_low_tps']:.0f}"
                   if req.get("prefill_high_tps") and req.get("prefill_low_tps") else "")
            last = (f"; last request: prefill {req.get('prompt_per_second') or 0:.0f} t/s{rng} over {req.get('input_tokens') or 0} "
                    f"tokens, decode {req.get('tokens_per_second') or 0:.1f} t/s")
        mine = " [the assistant's own model]" if mid in own else ""
        lines.append(f"- {mid}{mine} ({r.get('state')}, {where or 'placement unknown'}): {_gib(gib)} {kind if gib else ''}; now: {status}{last}")
    lines.append(f"Models on the GPUs together: about {_gib(total)}.")
    return lines


def gpu_status(ctx: dict, q: str) -> list[str]:
    gpus = ctx["store"].gpu_latest()
    if not gpus:
        return ["GPU: no readings recorded yet."]
    return ["GPU NOW (the whole card, including anything else using it):"] + [
        f"- GPU{g['gpu']} {g['name']}: {(g['mem_used_mb'] or 0) / 1024:.1f} of {(g['mem_total_mb'] or 0) / 1024:.1f} GiB VRAM used, "
        f"{g['util'] or 0:.0f}% busy, {g['temp_c'] or 0:.0f} °C, {g['power_w'] or 0:.0f} W" for g in gpus]


_STOP = {"list", "show", "info", "about", "what", "whats", "which", "the", "for", "model", "models", "details", "detail",
         "tell", "give", "and", "with", "how", "fast", "speed", "speeds", "decode", "prefill", "is", "are", "me", "on", "of",
         "my", "this", "that", "does", "do", "run", "runs", "profile", "profiles", "please", "can", "you"}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9.]+", (text or "").lower()) if w]


def _fuzzy(q: str, name: str) -> bool:
    """Every word the question gives (besides the filler) starts a word of the name: "qwen coder" finds
    Qwen3-Coder-Next-Q5_K_S, "coder q5" too. One short word alone is not enough to name a model."""
    want = [w for w in _words(q) if w not in _STOP and len(w) >= 2]
    if not want or (len(want) == 1 and len(want[0]) < 4):
        return False
    have = _words(name)
    return all(any(h.startswith(w) or (len(w) >= 4 and w in h) for h in have) for w in want)


def _match(ctx: dict, q: str, limit: int = 3) -> list[dict]:
    """Models the question names: by name, by the words of its name, or by '.6B' style size."""
    low = re.sub(r"[^a-z0-9.]+", " ", q.lower())
    rows = ctx["inventory"].models_summary()
    hits = []
    for m in rows:
        name = re.sub(r"[^a-z0-9.]+", " ", (m["name"] or "").lower()).strip()
        if name and name in low:
            hits.append(m)
    if not hits:
        hits = [m for m in rows if _fuzzy(q, f"{m['name']} {m.get('model_id') or ''}")]
    if not hits:
        want = _params_b(q)
        if want is not None:
            hits = [m for m in rows if _params_b(params_from_name(m["name"]) or "") == want and m["origin"] != "engine"]
    return sorted(hits, key=lambda m: -(m["loaded_seconds"] or 0))[:limit]


def _speed(span: Optional[dict]) -> str:
    if not span:
        return "none recorded"
    return f"{span['min']:g}" if span["min"] == span["max"] else f"{span['min']:g} to {span['max']:g}"


def model_details(ctx: dict, q: str) -> list[str]:
    hits = _match(ctx, q)
    if not hits:
        return []
    extra = {m["model_id"]: (m.get("extra") or {}) for m in ctx["store"].list_models_seen()}
    best = {b["base_model"]: b for b in ctx["analytics"].best_by_base_model()}
    out = []
    for m in hits:
        e = extra.get(m["model_id"], {})
        b = best.get(m["name"]) or best.get(m["model_id"]) or {}
        out.append(f"MODEL {m['name']}:")
        out.append(f"- file {_gib(m['size_gib'])}, {e.get('params') or params_from_name(m['name']) or 'params unknown'}, "
                   f"{'/'.join(m['quants']) or '?'}, arch {m['arch'] or '?'}, kind {m['kind'] or 'llm'}")
        out.append(f"- VRAM when loaded: {_gib(m['vram_gib_avg'])}; loaded {m['sessions']} time(s), {m['loaded_seconds'] / 3600:.1f} h in total")
        out.append(f"- decode speed {_speed(m['decode'])} t/s, prefill speed {_speed(m['prefill'])} t/s (from real requests and benchmarks)")
        if b:
            out.append(f"- best benchmark: {b.get('tokens_per_second') or 0:.1f} t/s decode, {b.get('prompt_per_second') or 0:.0f} t/s prefill, "
                       f"ngl {b.get('gpu_layers') or '?'}, ctx {b.get('n_ctx') or '?'}")
    return out


def performance(ctx: dict, q: str) -> list[str]:
    hits = _match(ctx, q, limit=4)
    low = q.lower()
    if len(hits) >= 2 or (hits and any(w in low for w in ("perform", "speed", "fast", "slow", "t/s", "compare", "vs"))):
        out = ["SPEED (decode = generating, prefill = reading the prompt; t/s = tokens per second):"]
        for m in hits:
            out.append(f"- {m['name']}: decode {_speed(m['decode'])} t/s, prefill {_speed(m['prefill'])} t/s, VRAM {_gib(m['vram_gib_avg'])}")
        return out
    ranked = [m for m in ctx["inventory"].models_summary() if m["decode"]]
    ranked.sort(key=lambda m: -m["decode"]["max"])
    if not ranked:
        return ["SPEED: no model has recorded speeds yet."]
    return ["FASTEST BY DECODE (best recorded, t/s):"] + [
        f"- {m['name']}: {m['decode']['max']:g} t/s decode, VRAM {_gib(m['vram_gib_avg'])}" for m in ranked[:6]]


def problems(ctx: dict, q: str) -> list[str]:
    import time
    ev = [e for e in ctx["store"].list_events(since=time.time() - 86400, limit=80) if e["level"] in ("warn", "error")]
    if not ev:
        return ["PROBLEMS in the last 24 h: none recorded."]
    return ["PROBLEMS in the last 24 h:"] + [f"- {e['message'][:160]}" for e in ev[:8]]


def runners_info(ctx: dict, q: str) -> list[str]:
    """The local runners (Servers -> Local): what is installed, on what, and how a client calls each model."""
    runners = ctx.get("runners")
    if runners is None:
        return []
    view = runners.view()
    entries = runners.entries()
    out = ["LOCAL RUNNERS (Servers -> Local; servers installed inside Argos for models llama-server cannot run):"]
    for r in view["runners"]:
        state = "installed" if r.get("installed") else "not installed"
        if r.get("enabled") is False:
            state += ", turned off"
        if r.get("addon"):
            out.append(f"- add-on {r['label']}: {state}" + ("; Argos converts uploaded audio (m4a, mp3, ogg...) to WAV before whisper" if r.get("installed") else ""))
            continue
        models = [m["id"] for m in r.get("models") or []]
        line = f"- {r['label']} ({r.get('task') or 'custom'}), {state}, on {str(r.get('device') or 'cpu').upper()}"
        if r.get("endpoint"):
            line += f", called at {r['endpoint']}"
        if models:
            names = []
            for mid in models:
                alias = (entries.get(mid) or {}).get("aliases") or []
                names.append(mid + (f" (also {', '.join(alias)})" if alias else ""))
            line += "; models: " + ", ".join(names)
        out.append(line)
    out.append("Requests pick the model by name, like chat: POST /v1/audio/transcriptions with model whisper-1 and the audio file, "
               "or POST /v1/audio/speech with model tts-1, input text and a voice (e.g. af_heart, bm_george). Everything else uses llama.cpp.")
    return out


# ----- choosing -----------------------------------------------------------------------
_RULES: list[tuple[re.Pattern, Callable[[dict, str], list[str]]]] = [
    (re.compile(r"\b(loaded|running|in use|memory|vram|loaded now|on the gpu|resident|right now|working|on the cpu)\b", re.I), loaded_now),
    (re.compile(r"\b(gpu|temp|temperature|power|watts|busy|utili[sz]ation)\b|\bvram\b", re.I), gpu_status),
    (re.compile(r"\b(have|list|which|what models|models do|installed|on disk|close to|around|about|smallest|largest|biggest|params?|size|gb|gib)\b|\d\s?b\b|~", re.I), models_on_disk),
    (re.compile(r"\b(spec|specs|details?|tell me about|info)\b", re.I), model_details),
    (re.compile(r"\b(perform|performed|fast|fastest|slow|speed|t/s|tok|benchmark|compare|vs|decode|prefill)\b", re.I), performance),
    (re.compile(r"\b(error|errors|problem|problems|crash|crashed|fail|failed|wrong|broken)\b", re.I), problems),
    (re.compile(r"\b(speech|speak|voice|voices|tts|transcri\w*|whisper|kokoro|audio|runner|runners|ffmpeg|npc|stt|comfy\w*|image|local)\b", re.I), runners_info),
]


def answer_lines(ctx: dict, question: str) -> list[str]:
    """The tool results this question calls for. A named model always gets its details."""
    q = (question or "").strip()
    if len(q) < 3:
        return []
    out: list[str] = []
    done: set[Callable] = set()
    for pat, fn in _RULES:
        if fn in done or not pat.search(q):
            continue
        done.add(fn)
        try:
            out += fn(ctx, q)
        except Exception as exc:  # noqa: BLE001 - one broken tool must not hide the rest
            out.append(f"({fn.__name__} failed: {exc})")
    if model_details not in done:
        try:
            out += model_details(ctx, q)
        except Exception:  # noqa: BLE001
            pass
    if not out:
        return []
    return ["TOOL RESULTS (computed from the live system for this question; copy these numbers and names exactly, do not recalculate):"] + out


# ----- answers that need no model ---------------------------------------------------------
_LIST_ALL = re.compile(
    r"(\b(list|show|name|enumerate|give me|print)\b.*\b(all|every|them|models)\b)|(\bhow many models\b)|(\bwhat models (do i|have i|are there)\b)|(\ball (my |the )?models\b)",
    re.I)
_FILTERED = re.compile(r"\b(close|closest|near|around|about|smallest|largest|biggest|fastest|loaded|running|\d+(\.\d+)?\s?b)\b|~", re.I)


def list_everything(ctx: dict) -> str:
    """Every model Argos knows, grouped, with the counts adding up to the total."""
    rows = ctx["inventory"].models_summary()
    if not rows:
        return "Argos has no models recorded yet."
    extra = {m["model_id"]: (m.get("extra") or {}) for m in ctx["inventory"].store.list_models_seen()}
    files = [m for m in rows if m["present"] and m["origin"] != "engine" and m["kind"] not in ("mmproj", "mtp", "draft")]
    helpers = [m for m in rows if m["present"] and m["origin"] != "engine" and m["kind"] in ("mmproj", "mtp", "draft")]
    runner_models = ctx.get("runner_models") or {}
    by_runner = [m for m in rows if m["present"] and m["model_id"] in runner_models]
    served = [m for m in rows if m["present"] and m["origin"] == "engine" and m["model_id"] not in runner_models]
    files = [m for m in files if m["model_id"] not in runner_models]
    gone = [m for m in rows if not m["present"]]

    def line(i: int, m: dict) -> str:
        p = extra.get(m["model_id"], {}).get("params") or params_from_name(m["name"])
        bits = [b for b in (_gib(m["size_gib"]) if m["size_gib"] else "", p, "/".join(m["quants"])) if b]
        return f"{i}. {m['name']}" + (f" ({', '.join(bits)})" if bits else "")

    out = [f"Argos knows **{len(rows)} models**: {len(files)} model files, {len(by_runner)} served by local runners, "
           f"{len(helpers)} helper files (draft, mtp, mmproj), "
           f"{len(served)} names the engine serves that are not files here, {len(gone)} no longer on disk."]

    def runner_line(i: int, m: dict) -> str:
        meta = runner_models.get(m["model_id"]) or {}
        return f"{i}. {m['name']} ({meta.get('runner', '?')}, {meta.get('task') or '?'}, {str(meta.get('device') or '').upper()})"

    if by_runner:
        out += ["", f"**Served by local runners ({len(by_runner)})**"] + [runner_line(i, m) for i, m in enumerate(sorted(by_runner, key=lambda m: m["name"].lower()), 1)]
    for title, group in ((f"Model files ({len(files)})", files), (f"Helper files ({len(helpers)})", helpers),
                         (f"Served by the engine, no file found here ({len(served)})", served), (f"No longer on disk ({len(gone)})", gone)):
        if group:
            out += ["", f"**{title}**"] + [line(i, m) for i, m in enumerate(sorted(group, key=lambda m: m["name"].lower()), 1)]
    return "\n".join(out)


_RUNNING_Q = re.compile(r"\b(what|which|whats|what's)\b.*\b(running|loaded|working|busy|in use|on the gpus?|on the cpu)\b|"
                        r"\b(running|loaded) (right )?now\b|\bwhat is running\b|\bcurrent(ly)? (running|loaded)\b", re.I)
_SHOW_Q = re.compile(r"^\s*(list|show|info|details?|tell me about|about|what about|how fast is|speeds? (of|for))\b", re.I)


def _as_markdown(lines: list[str]) -> str:
    """Tool lines as an answer: headings bold, the rest as they are."""
    out = []
    for line in lines:
        if line.startswith("- "):
            out.append(line)
        elif line.strip():
            head, _, rest = line.partition(":")
            out.append(f"**{head.strip()}**{(': ' + rest.strip()) if rest.strip() else ''}")
    return "\n".join(out)


def _profiles_for(ctx: dict, model: dict) -> list[str]:
    names = []
    try:
        for p in ctx["store"].list_profiles():
            if p.get("model_id") == model.get("model_id") or (p.get("base_model") and p.get("base_model") == model.get("name")):
                names.append(p.get("name") or "")
    except Exception:  # noqa: BLE001
        pass
    return [n for n in names if n]


def direct_answer(ctx: dict, question: str) -> Optional[str]:
    """A full answer built in code, else None: 'list everything', what is running now, and one model's details.

    A 0.6B model asked to copy 31 lines drops some and miscounts, and handed a model's speeds it reports its own;
    code does neither."""
    q = (question or "").strip()
    if not q:
        return None
    try:
        if _RUNNING_Q.search(q):
            return _as_markdown(loaded_now(ctx, q))
        if _LIST_ALL.search(q) and not _FILTERED.search(q) and ("model" in q.lower() or "them" in q.lower()):
            return list_everything(ctx)
        if _SHOW_Q.search(q) or len(_words(q)) <= 3:
            hits = _match(ctx, q)
            if hits:
                lines = model_details(ctx, q)
                for m in hits:
                    profiles = _profiles_for(ctx, m)
                    if profiles:
                        lines.append(f"- profiles of {m['name']}: {', '.join(profiles)}")
                return _as_markdown(lines) if lines else None
    except Exception:  # noqa: BLE001 - fall back to the model
        return None
    return None
