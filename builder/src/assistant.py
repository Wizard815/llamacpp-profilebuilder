"""The built-in assistant: one continuous chat with a small local model.

Every turn the model gets a compact LIVE DATA block built from this app's
database (GPU history, request speeds, benchmarks, running launches, recent
problems), so a 0.6B model can answer "how have my GPUs been doing" from facts
instead of guessing. The conversation is stored in ``chat_messages``; when it
would no longer fit the context window, or when you press Compact, the oldest
turns are summarised by the model itself into a running summary and marked
compacted (still shown in the UI, no longer sent).

The model is normally one the engine has ("engine": a library model or a
profile, picked from a list). It can also be any OpenAI-compatible server
("endpoint"), or a profile this app launches beside the engine ("managed").
The chat and embedding models can be loaded and unloaded by hand, and are
unloaded after ``idle_unload_min`` minutes without use.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Iterator, Optional

from .db import Store
from .integrations import engine_api

logger = logging.getLogger(__name__)
from .runner import LaunchError, Runner
from .settings import Settings

DEFAULT_REPOS = ("Qwen/Qwen3-0.6B-GGUF", "ggml-org/Qwen3-0.6B-GGUF", "unsloth/Qwen3-0.6B-GGUF")
DEFAULT_QUANT = "Q8_0"
PROFILE_NAME = "Assistant · Qwen3-0.6B"
# The model a hard question is handed to. Deliberately small: it may run on CPU, so
# it costs RAM instead of VRAM, and its default quant is the smaller one because on
# CPU that is the faster one and the job does not need more.
ESCALATE_REPOS = ("bartowski/Qwen_Qwen3.5-2B-GGUF",)
ESCALATE_PROFILE_NAME = "Assistant · Qwen3.5-2B"
ANSWER_TOKENS = 768
SUMMARY_KEY = "_chat_summary"

SYSTEM_PROMPT = """You are the Argos assistant, a concise helper for running llama.cpp models on this machine.
TOOL RESULTS, when present, are exact answers worked out in code for this question: answer from them first, copy names and numbers as written, keep their units (GiB, t/s), and never do your own sums, rankings or unit changes. A model's VRAM is its own line; the GPU's used VRAM is the whole card and is not any one model's.
Use the LIVE DATA block for anything about GPUs, speeds, models, profiles, launches or errors; quote its numbers.
RELATED RECORDS are the stored records closest to the question; prefer them for anything about a specific model, setting, note or past event.
INVENTORY lines list the models seen, how long each was loaded and on which GPUs, and settings (quants, cache types, flags) seen for the first time that are waiting for review.
Lines with "vs the usual" (and RELATED RECORDS starting "Change:") say exactly which flags or env vars differ between two launch configs of a model and what that did to speed and VRAM; use them to explain small changes.
You can propose model actions. When the user asks you to load, unload or keep a model, answer in one short sentence and then end with one ACTION line per model, exactly:
ACTION load <model id>
ACTION unload <model id>
ACTION keep <model id> on
Use only model ids that appear in LIVE DATA, INVENTORY or the model lines above -- never invent one, and never claim an action is already done: the user confirms each one in the UI.
WHERE THINGS LIVE in this app (say these, do not invent screens):
Builder tunes a llama-server command and saves it as a profile; Profiles lists the saved ones and can apply or diff them; Playground launches a model, runs a speed test and shows speed per request; Perf holds measured speeds per model and setup; Schedule decides how models share the GPUs; Inventory is every model and setting ever seen; Models is your files and downloads; Servers has two tabs: Remote is other machines Argos launches llama.cpp on over SSH, Local is runners installed inside Argos (whisper for speech to text, Kokoro for text to speech, custom servers cloned from git, and the ffmpeg add-on that converts uploaded audio); a model's type picks its runner automatically; Logs and Events are what happened; Settings holds integrations, telemetry and this assistant.
If the user asks how to do something, name the view to open and the setting to change. A saved profile is the way to keep a launch command: build it in Builder, name it, then it appears in Profiles.
If LIVE DATA does not cover the question, say so in one line instead of guessing.
Know llama-server flags (-ngl, -c, --flash-attn, --cache-type-k/v, -lm, --split-mode, --n-cpu-moe, --spec-type) and suggest concrete changes.
Your job is to read off the numbers in LIVE DATA, exactly as written: which model, which speed, how much memory. Never give a speed or size that is not written there, and never report your own model as the one running unless LIVE DATA says it is the only one.
If the question needs something you cannot do - the past, advice, comparing many models, the full flag reference, reading a long log - reply with the single word ESCALATE on its own line and nothing else, and a larger model answers it instead, with the database's memory. Do not guess when you could hand over.
When BUILDER appears in LIVE DATA, the user is in the Builder view and that is the command on their screen: answer about it directly. To propose a change to it, end with one line per flag or environment variable, "ACTION set <flag or VAR> <value>" or "ACTION unset <flag or VAR>" (for example: ACTION set --flash-attn off, ACTION set HSA_ENABLE_SDMA 0). Propose only flags you know; the user applies each one. Never rewrite the whole command unless asked to.
FLAGS PAGE lines are the user's own notes on flags and environment variables: prefer them. To add a flag or variable to the Flags page, or to write or fix its description, end with "ACTION describe <flag or VAR> <one-sentence description>", one line each; the user adds each one with a click.
Answer in a few short sentences or a short list."""

# Questions about the past, or that want judgment, go straight to the bigger model with the memory search: the
# small one only reads off the live data (and hands anything else over with ESCALATE).
NEEDS_BIG = re.compile(
    r"\b(why|when did|last (time|week|month|night)|yesterday|history|historical|earlier|before|remember|ever|"
    r"recommend|suggest|should i|advice|best (settings?|profile|config|way)|compare|comparison|versus|vs\.?|difference|"
    r"explain|how (do|can|should|would) i|tune|tuning|optimi[sz]e|improve|similar|trend|over time)\b", re.I)

# How many models one question may pull into focus. Two was tight: a question
# that compares three setups silently lost the third.
FOCUS_MODELS = 4

# The one line shape an action may take. Deliberately strict -- the answer is
# free text, so anything looser becomes guesswork about what was meant.
ACTION_RE = re.compile(r"ACTION\s+(load|unload|keep)\s+(\S+)(?:\s+(on|off))?\s*$", re.IGNORECASE)

# The small model says this, alone on a line, when the question is beyond it. A
# declared hand-off beats a guess: the alternative is inferring "too hard" from the
# question, which is wrong in both directions and silently so.
ESCALATE_RE = re.compile(r"^\s*ESCALATE\s*[.!]?\s*$", re.MULTILINE | re.IGNORECASE)

# The Builder panel lets the assistant propose an edit to the command on screen.
# The flag name is checked against a flag's shape before it is offered, because the
# answer is free text and this one ends up in a command line: a model that writes
# "ACTION set $(curl ...) x" must not produce a button.
ACTION_EDIT_RE = re.compile(r"ACTION\s+(set|unset|describe)\s+(\S+)(?:\s+(.*))?\s*$", re.IGNORECASE)
FLAG_NAME_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9-]*$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{1,127}$")  # an environment variable, upper case as they are written
ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_.,:/=+@-]{0,200}$")  # what may follow it: no quotes, spaces or $()

SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0}  # Qwen3 non-thinking defaults
THINK = re.compile(r"<think>.*?(?:</think>\s*|$)", re.DOTALL)


def _builder_line(builder: dict) -> str:
    """What the Builder has open, as one line the model can answer about.

    The command is the important part; the flags are what a suggestion has to name,
    and the models are what a comparison is against.
    """
    parts: list[str] = []
    command = str(builder.get("command") or "").strip()
    if command:
        parts.append("command: " + command[:400])
    program = str(builder.get("program") or "").strip()
    if program:
        parts.append("program: " + program)
    flags = builder.get("flags")
    if isinstance(flags, dict) and flags:
        named = [f"{k}={v}" for k, v in flags.items() if not str(k).isupper() and v not in ("", None, True)]
        if named:
            parts.append("flags: " + ", ".join(named[:40]))
    env = builder.get("env")
    if env:
        text = env if isinstance(env, str) else ", ".join(str(x) for x in env)
        if text.strip():
            parts.append("env: " + text[:200])
    other = builder.get("other")
    if other:
        parts.append("other flags: " + ", ".join(str(x) for x in list(other)[:20]))
    always = builder.get("always")
    if always:
        parts.append("always applied: " + ", ".join(str(x) for x in list(always)[:10]))
    models = builder.get("models")
    if models:
        parts.append("comparing against: " + ", ".join(str(m) for m in list(models)[:8]))
    return " | ".join(parts) or "(nothing set yet)"


def pick_quant(quants: list[dict], want: str = DEFAULT_QUANT) -> Optional[dict]:
    """The wanted quant if the repo has it, else the smallest 4-8 bit one."""
    exact = [q for q in quants if q["quant"].upper() == want.upper()]
    if exact:
        return exact[0]
    usable = [q for q in quants if re.match(r"(UD-)?I?Q[4-8]", q["quant"].upper())]
    return min(usable, key=lambda q: q["size"]) if usable else None


def first_json(text: str):
    """The first JSON array or object in a model's reply (code fences and chatter around it are ignored)."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch in "[{":
            try:
                return decoder.raw_decode(text[i:])[0]
            except ValueError:
                continue
    return None


def estimate_tokens(text: str) -> int:
    return len(text) // 3 + 1  # deliberately pessimistic: numbers and code tokenize densely


def _f(v, digits=0) -> str:
    return "?" if v is None else f"{v:.{digits}f}"


class AssistantError(Exception):
    pass


class Assistant:
    def __init__(self, store: Store, settings: Settings, runner: Runner, analytics,
                 on_event: Callable[..., None], inventory=None, memory=None, plans=None):
        self.plans = plans
        self.runners = None  # set by the app once Servers -> Local exists
        self.inventory = inventory
        self.memory = memory
        self.store = store
        self.settings = settings
        self.runner = runner
        self.analytics = analytics
        self.on_event = on_event
        self.launch_id: Optional[str] = None
        self._launched_stamp: tuple = ()
        self._turn = threading.Lock()
        self.last_used = 0.0  # when the chat model last answered, for unloading it when idle
        self._escalated = False  # this turn is being answered by the escalation model
        self._builder: Optional[dict] = None  # what is on screen this turn, for digest()

    # ----- the facts the model sees ----------------------------------------
    def digest(self, question: str = "", now: Optional[float] = None, memory: bool = True) -> str:
        """What the model answers from. ``memory`` False leaves out the memory search (the embedding model): the
        small model reads off the live data, and only the bigger one searches the database."""
        now = now or time.time()
        lines = [f"LIVE DATA ({datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')})"]
        lines += self._tool_lines(question)
        if getattr(self, "_builder", None):
            lines.append("BUILDER (open in the Builder view right now): " + _builder_line(self._builder))
        lines += self._flags_page_lines(question)
        latest = self.store.gpu_latest()
        if latest and now - latest[0]["ts"] < 600:
            lines.append("GPU now: " + " | ".join(
                f"GPU{g['gpu']} {g['name']}: {_f(g['util'])}% busy, VRAM {_f((g['mem_used_mb'] or 0) / 1024, 1)}"
                f"/{_f((g['mem_total_mb'] or 0) / 1024, 1)} GiB, {_f(g['temp_c'])}°C, {_f(g['power_w'])} W"
                for g in latest))
        # GPU readings and request history are kept for telemetry.keep_minutes (2 h by default): say so
        kept_s = int(self.settings.get("telemetry").get("keep_minutes") or 120) * 60
        kept = f"last {kept_s // 3600} h" if kept_s % 3600 == 0 else f"last {kept_s // 60} min"
        for label, span in (("last 1h", min(3600, kept_s)), (kept, kept_s)):
            rows = self.store.gpu_summary(now - span)
            if rows:
                lines.append(f"GPU {label}: " + " | ".join(
                    f"GPU{g['gpu']} avg {_f(g['util_avg'])}% (max {_f(g['util_max'])}%), VRAM peak "
                    f"{_f((g['mem_max_mb'] or 0) / 1024, 1)}/{_f((g['mem_total_mb'] or 0) / 1024, 1)} GiB, "
                    f"temp max {_f(g['temp_max'])}°C, avg {_f(g['power_avg'])} W" for g in rows))
        if not latest:
            lines.append("GPU: no samples recorded yet (Settings -> Telemetry).")
        week = [h for h in self.store.gpu_hourly(now - 7 * 86400)]
        if week:
            by_gpu: dict[int, list] = {}
            for h in week:
                by_gpu.setdefault(h["gpu"], []).append(h)
            lines.append("GPU last 7d (hourly): " + " | ".join(
                f"GPU{g} busiest hour {_f(max(x['util_avg'] or 0 for x in hs))}% avg, hottest "
                f"{_f(max(x['temp_max'] or 0 for x in hs))}°C" for g, hs in sorted(by_gpu.items())))
        reqs = self.store.request_stats(now - kept_s)
        if reqs:
            lines.append(f"Requests {kept} (Argos): " + " | ".join(
                f"{r['model']}: {r['requests']} req, gen {_f(r['tg_avg'], 1)} t/s avg (max {_f(r['tg_max'], 1)}), "
                f"prompt {_f(r['pp_avg'])} t/s, {r['errors']} errors" for r in reqs[:6]))
        month = self.store.request_stats(now - kept_s)
        if len(month) > 1:
            lines.append(f"Model speeds {kept} (compare models): " + " | ".join(
                f"{r['model']}: gen {_f(r['tg_avg'], 1)} t/s, prompt {_f(r['pp_avg'])} t/s, {r['requests']} req"
                for r in sorted(month, key=lambda r: -(r["tg_avg"] or 0))[:8]))
        best = self.analytics.best_by_base_model()[:5]
        if best:
            lines.append("Best benchmarks: " + " | ".join(
                f"{b['base_model']} {_f(b['tokens_per_second'], 1)} t/s gen, {_f(b['prompt_per_second'])} t/s prompt "
                f"(ngl {b.get('gpu_layers') or '?'}, ctx {b.get('n_ctx') or '?'}, fa {b.get('flash_attention') or '-'}, "
                f"kv {b.get('cache_type_k') or 'f16'})" for b in best))
        running = [r for r in self.runner.list() if r.running]
        if running:
            lines.append("Running here: " + ", ".join(
                f"{r.profile_name or r.base_model or r.launch_id} on :{r.port}" for r in running))
        lines.append("ACTIONS you may propose (the user confirms each in the UI): "
                     "load <model id>, unload <model id>, keep <model id> on|off")
        profiles = self.store.list_profiles()
        if profiles:
            # Speeds are keyed by the model the engine actually served, which for
            # a saved profile is its variant id; look up whichever of the two
            # matches rather than assuming.
            speeds = {r["model"]: r for r in self.store.request_stats(now - 90 * 86400)}
            rows = []
            for p in sorted(profiles, key=lambda p: (p.get("base_model") or "", p.get("name") or "")):
                mid = p.get("model_id") or ""
                stats = speeds.get(mid) or speeds.get(p.get("base_model") or "")
                speed = ""
                if stats and stats.get("tg_avg"):
                    speed = f", {_f(stats['tg_avg'], 1)} t/s gen"
                rows.append(f"{p.get('name') or '?'} [{(p.get('base_model') or mid or '?')}{speed}]")
            more = f" (+{len(profiles) - len(rows)} more)" if len(profiles) > len(rows) else ""
            lines.append(f"Profiles: {len(profiles)} saved -- " + "; ".join(rows) + more)
        problems = [e for e in self.store.list_events(since=now - 86400, limit=50) if e["level"] in ("warn", "error")]
        if problems:
            lines.append("Recent problems: " + " | ".join(
                f"{datetime.fromtimestamp(e['ts']).strftime('%H:%M')} {e['message'][:140]}" for e in problems[:5]))
        if self.inventory is not None:
            lines += self.inventory.digest_lines(now)
        if memory:
            lines += self.related(question)
        lines += self._model_focus(question, now)
        if len(lines) > 1 and lines[1].startswith("TOOL RESULTS"):
            # a 0.6B model reads less when it has been handed the answer: leave out the broad summaries
            lines = [l for l in lines if not l.startswith(("GPU last", "Model speeds last", "Requests last", "MODELS ON DISK", "Most loaded"))]
        return "\n".join(lines)

    def _flags_page_lines(self, question: str, limit: int = 16) -> list[str]:
        """The Flags page rows for what is in the Builder's command and what the question names, so the assistant
        answers from the user's own notes, and a name with no row is said to have none."""
        from .command import canonical, parse_command
        names: list[tuple[str, str]] = []
        builder = getattr(self, "_builder", None) or {}
        try:
            cmd = parse_command(str(builder.get("command") or "")) if builder.get("command") else None
        except ValueError:
            cmd = None
        if cmd:
            names += [("env", k) for k in cmd.env] + [("flag", a.name) for a in cmd.args if a.flag and a.name]
        names += [("env", w) for w in re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", question or "")]
        names += [("flag", canonical(w)) for w in re.findall(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9-]*", question or "")]
        if not names or self.analytics is None:
            return []
        try:
            rows = {("env", r["flag"]): r for r in self.analytics.env_table()}
            rows.update({("flag", r["flag"]): r for r in self.analytics.flag_table()})
        except Exception:  # noqa: BLE001 - the answer goes on without the notes
            return []
        out, seen = [], set()
        for key in names:
            if key in seen or key[1] in ("model", "port", "host"):
                continue
            seen.add(key)
            r = rows.get(key)
            label = key[1] if key[0] == "env" else "--" + key[1]
            if not r or r.get("removed"):
                out.append(f"- {label}: not on the Flags page")
            elif r.get("description") or r.get("recommended") or r.get("notes"):
                extra = r.get("recommended") or r.get("notes") or ""
                out.append(f"- {label}: {r.get('description') or ''}{f' ({extra})' if extra else ''}"[:400])
            if len(out) >= limit:
                break
        return ["FLAGS PAGE (the user's notes):"] + out if out else []

    def _tool_ctx(self) -> dict:
        measured = {}
        try:
            measured = (self.plans.get() or {}).get("measured") or {} if self.plans is not None else {}
        except Exception:  # noqa: BLE001 - sizes are a bonus
            pass
        runner_models = {}
        if self.runners is not None:
            try:
                runner_models = {mid: (e.get("metadata") or {}).get("argos") or {} for mid, e in self.runners.entries().items()}
            except Exception:  # noqa: BLE001 - the runners are a bonus here
                pass
        return {"inventory": self.inventory, "store": self.store, "analytics": self.analytics, "measured": measured,
                "engine_url": self.settings.get("integrations").get("engine_url") or "",
                "runners": self.runners, "runner_models": runner_models,
                # the assistant's own chat and memory models: never the answer to "what is running"
                "own_models": {x for x in (self.settings.get("assistant").get("model"), self.settings.get("memory").get("model")) if x}}

    def _direct(self, question: str) -> Optional[str]:
        if self.inventory is None:
            return None
        from . import assistant_tools
        return assistant_tools.direct_answer(self._tool_ctx(), question)

    def _tool_lines(self, question: str) -> list[str]:
        """Answers worked out in code for this question (see assistant_tools), placed first so they win."""
        if self.inventory is None:
            return []
        from . import assistant_tools
        ctx = self._tool_ctx()
        return assistant_tools.answer_lines(ctx, question)

    def _actionable_ids(self) -> set[str]:
        """Every model id an action may name.

        A proposal is only turned into a button when the id is one Argos already
        knows, so a small model inventing a plausible name cannot produce a
        button that launches nothing.
        """
        ids: set[str] = set()
        try:
            ids |= {m["model_id"] for m in self.store.list_models_seen() if m.get("model_id")}
        except Exception:  # noqa: BLE001 - validation is best-effort
            pass
        if self.inventory is not None:
            try:
                ids |= {m["model_id"] for m in self.inventory.models_summary() if m.get("model_id")}
            except Exception:  # noqa: BLE001
                pass
        return ids

    def _actions(self, answer: str) -> tuple[str, list[dict]]:
        """Split ACTION lines out of an answer: (text without them, the actions).

        The assistant proposes; it never performs. The caller shows each action
        for confirmation, so a wrong id costs a click rather than a launch.
        """
        known = self._actionable_ids()
        actions: list[dict] = []
        kept: list[str] = []
        for line in (answer or "").splitlines():
            m = ACTION_RE.match(line.strip())
            if not m:
                edit = ACTION_EDIT_RE.match(line.strip())
                name = edit.group(2) if edit else ""
                verb = edit.group(1).lower() if edit else ""
                value = (edit.group(3) or "").strip() if edit else ""
                is_env = bool(ENV_NAME_RE.match(name))
                if edit and (FLAG_NAME_RE.match(name) or is_env) and (
                        verb == "describe" and value or verb == "unset"
                        or verb == "set" and (not is_env or ENV_VALUE_RE.match(value))):
                    actions.append({"verb": verb, "flag": name, "value": value, "kind": "env" if is_env else "flag", "model": ""})
                    continue
                kept.append(line)
                continue
            verb, model, on = m.group(1).lower(), m.group(2), m.group(3)
            if model not in known:
                logger.warning("assistant proposed %s on an unknown model: %r", verb, model)
                continue
            action: dict = {"verb": verb, "model": model}
            if verb == "keep":
                action["on"] = (on or "on").lower() != "off"
            actions.append(action)
        return "\n".join(kept).strip(), actions

    def related(self, question: str) -> list[str]:
        """The records from memory closest to the question, by meaning and by words."""
        if self.memory is None or len((question or "").strip()) < 3 or not self.settings.get("memory")["enabled"]:
            return []
        try:
            hits = self.memory.search(question, int(self.settings.get("memory")["results"] or 8))
        except Exception:  # noqa: BLE001 - memory is a bonus, never a reason to fail an answer
            return []
        if not hits:
            return []
        return ["RELATED RECORDS (closest to the question, from the database):"] + [f"- [{h['source']}] {h['text'][:400]}" for h in hits]

    def _model_focus(self, question: str, now: float) -> list[str]:
        q = question.lower()
        if len(q) < 3:
            return []
        names = {r["base_model"] for r in self.store.list_runs(limit=2000)}
        names |= {r["model"] for r in self.store.list_requests(since=now - 30 * 86400, limit=2000) if r["model"]}
        # Every model Argos can see, not only the ones with history. A model that
        # exists but has never been launched or benchmarked is still a real model
        # the user can ask about, and the way the digest is built it otherwise
        # appears only as "+1" in a count -- so the assistant answered "no model
        # found" about a model sitting in its own catalog. Real case: asking for
        # a dual-GPU 256K profile on a coder model that had never been run.
        catalog: dict[str, dict] = {}
        if self.inventory is not None:
            try:
                for m in self.inventory.models_summary(now):
                    mid = m.get("model_id")
                    if not mid:
                        continue
                    catalog[mid] = m
                    names.add(mid)
                    if m.get("name"):
                        names.add(m["name"])
            except Exception:  # noqa: BLE001 - extra context only, never fatal
                logger.debug("assistant: model catalog unavailable", exc_info=True)
        hits = sorted((n for n in names if n and (n.lower() in q or q.strip("?. ") in n.lower())), key=len)[:FOCUS_MODELS]
        out = []
        for name in hits:
            runs = self.store.list_runs(base_model=name, limit=5)
            if runs:
                out.append(f"{name} benchmarks: " + "; ".join(
                    f"{_f(r['tokens_per_second'], 1)} t/s gen, {_f(r['prompt_per_second'])} prompt, "
                    f"ngl {r.get('gpu_layers') or '?'}, ctx {r.get('n_ctx') or '?'}" for r in runs))
            else:
                out.append(self._model_exists_line(name, catalog))
            try:
                from .compare import change_lines
                out += change_lines(self.store, name, limit=4)
            except Exception:  # noqa: BLE001 - extra context only
                pass
            reqs = self.store.list_requests(model=name, since=now - 7 * 86400, limit=500)
            if reqs:
                tg = [r["tokens_per_second"] for r in reqs if r["tokens_per_second"]]
                out.append(f"{name} last 7d: {len(reqs)} requests" +
                           (f", gen {sum(tg) / len(tg):.1f} t/s avg, min {min(tg):.1f}, max {max(tg):.1f}" if tg else ""))
        return out

    @staticmethod
    def _model_exists_line(name: str, catalog: dict[str, dict]) -> str:
        """One line saying a model exists even though nothing has run it yet.

        Facts come from the catalog row when we have one, so the model has
        something concrete to reason about (where it lives, what kind it is)
        instead of only being told the name is real.
        """
        row = catalog.get(name) or next(
            (v for v in catalog.values()
             if name.lower() in ((v.get("model_id") or "").lower(), (v.get("name") or "").lower())),
            {},
        )
        facts = [x for x in (
            f"origin {row['origin']}" if row.get("origin") else "",
            f"kind {row['kind']}" if row.get("kind") else "",
            f"quant {row['quant']}" if row.get("quant") else "",
            f"path {row['path']}" if row.get("path") else "",
            "present" if row.get("present") else ("gone" if row else ""),
        ) if x]
        line = f"{name}: in Argos's model list, no launches or benchmarks recorded yet"
        return line + (f" ({', '.join(facts)})" if facts else "")

    # ----- where the model runs ---------------------------------------------
    def endpoint(self, status: Callable[[str], None] = lambda s: None, wait_s: float = 300) -> tuple[str, str]:
        cfg = self.settings.get("assistant")
        if cfg["mode"] == "engine":
            url = self.settings.get("integrations")["engine_url"]
            if not cfg["model"]:
                raise AssistantError("Pick a model for the assistant under Assistant → Settings, or click “Set up Qwen3-0.6B”.")
            if not url:
                raise AssistantError("The engine is not running, so the assistant's model cannot be loaded.")
            # A turn the small model handed over, or one the user sent straight up.
            if self._escalated and self.escalate_model():
                return url.rstrip("/") + "/v1", self.escalate_model()
            return url.rstrip("/") + "/v1", cfg["model"]
        if cfg["mode"] == "endpoint":
            if not cfg["endpoint"]:
                raise AssistantError("Set the assistant endpoint under Assistant → Settings, or switch it to a model the engine has.")
            return cfg["endpoint"].rstrip("/"), cfg["model"] or "assistant"
        profile = self.store.get_profile(cfg["profile_id"]) if cfg["profile_id"] else None
        if not profile:
            raise AssistantError("No assistant profile yet. Click “Set up Qwen3-0.6B” or pick a profile under Assistant → Settings.")
        stamp = (profile["profile_id"], profile["launch_command"])
        run = self.runner.get(self.launch_id) if self.launch_id else None
        if run and run.running and stamp != self._launched_stamp:
            self.runner.stop(run.launch_id)  # the profile changed since we started it
            run = None
        if not run or not run.running:
            status("Starting the assistant model…")
            try:
                run = self.runner.launch(profile["launch_command"], base_model=profile.get("base_model") or "",
                                         profile_name=profile["name"])
            except LaunchError as exc:
                raise AssistantError(f"Could not start the assistant: {exc}") from exc
            self.launch_id, self._launched_stamp = run.launch_id, stamp
            self.on_event("assistant", f"started assistant model ({profile['name']}) on :{run.port}",
                          data={"launch_id": run.launch_id})
        deadline = time.time() + wait_s
        while not self.runner.health(run, timeout=1.0):
            if not run.running:
                tail = (self.runner.read_log(run.launch_id, tail_bytes=600) or "").strip().splitlines()
                raise AssistantError("The assistant model exited while loading: " + (tail[-1] if tail else "no output"))
            if time.time() > deadline:
                raise AssistantError("The assistant model did not become ready in time.")
            status("Loading the assistant model…")
            time.sleep(0.5)
        return f"http://{run.host}:{run.port}/v1", "assistant"

    def server_state(self) -> dict:
        cfg = self.settings.get("assistant")
        if cfg["mode"] == "engine":
            return {"mode": "engine", "model": cfg["model"]}
        if cfg["mode"] == "endpoint":
            return {"mode": "endpoint", "endpoint": cfg["endpoint"], "model": cfg["model"]}
        run = self.runner.get(self.launch_id) if self.launch_id else None
        profile = self.store.get_profile(cfg["profile_id"]) if cfg["profile_id"] else None
        return {"mode": "managed", "profile": profile and {"profile_id": profile["profile_id"], "name": profile["name"]},
                "running": bool(run and run.running), "ready": bool(run and self.runner.health(run, timeout=0.3)),
                "launch_id": run.launch_id if run else None}

    def stop(self) -> None:
        if self.launch_id:
            self.runner.stop(self.launch_id)

    # ----- conversation ------------------------------------------------------
    def summary(self) -> str:
        return self.store.get_settings().get(SUMMARY_KEY) or ""

    def _prompt(self, digest: str) -> list[dict]:
        system = SYSTEM_PROMPT + "\n\n" + digest
        if self.summary():
            system += "\n\nEARLIER IN THIS CHAT (summary):\n" + self.summary()
        msgs = [{"role": "system", "content": system}]
        msgs += [{"role": m["role"], "content": m["content"]} for m in self.store.list_messages() if not m["compacted"]]
        return msgs

    def budget(self) -> int:
        return max(1024, int(self.settings.get("assistant")["context_tokens"]) - ANSWER_TOKENS)

    def compact(self, base: str, model: str, digest: str) -> bool:
        """Summarise old turns into the running summary when the prompt gets too big."""
        cfg = self.settings.get("assistant")
        live = [m for m in self.store.list_messages() if not m["compacted"]]
        size = sum(estimate_tokens(m["content"]) for m in self._prompt(digest))
        keep = max(2, int(cfg["keep_recent"]))
        if size <= self.budget() * 0.7 or len(live) <= keep:
            return False
        old = live[:-keep]
        transcript = "\n".join(f"{m['role'].title()}: {m['content']}" for m in old)
        prompt = [
            {"role": "system", "content": "You write compact memory notes for a chat assistant."},
            {"role": "user", "content": (
                "Update the running summary of this conversation. Keep facts, numbers, model names, "
                "decisions and open questions; drop chit-chat. At most 180 words.\n\n"
                f"Current summary:\n{self.summary() or '(none)'}\n\nNew turns:\n{transcript[-12000:]}")},
        ]
        try:
            text = self._complete(base, model, prompt, max_tokens=400)
        except AssistantError:
            text = (self.summary() + "\n(earlier messages were dropped to fit the context window)").strip()
        self.store.set_setting(SUMMARY_KEY, text.strip())
        self.store.mark_compacted(old[-1]["id"])
        self.on_event("assistant", f"compacted {len(old)} chat messages into the summary")
        return True

    def compact_now(self) -> dict:
        """The Compact button: summarise every turn but the last two, whatever the size. Loads the model if needed."""
        live = [m for m in self.store.list_messages() if not m["compacted"]]
        if len(live) <= 2:
            return {"compacted": 0, "summary": self.summary()}
        if not self._turn.acquire(blocking=False):
            raise AssistantError("The assistant is answering a message; compact when it is done.")
        try:
            base, model = self.endpoint()
            old = live[:-2]
            transcript = "\n".join(f"{m['role'].title()}: {m['content']}" for m in old)
            prompt = [
                {"role": "system", "content": "You write compact memory notes for a chat assistant."},
                {"role": "user", "content": (
                    "Update the running summary of this conversation. Keep facts, numbers, model names, "
                    "decisions and open questions; drop chit-chat. At most 180 words.\n\n"
                    f"Current summary:\n{self.summary() or '(none)'}\n\nNew turns:\n{transcript[-12000:]}")},
            ]
            text = self._complete(base, model, prompt, max_tokens=400).strip()
            self.last_used = time.time()
            self.store.set_setting(SUMMARY_KEY, text or self.summary())
            self.store.mark_compacted(old[-1]["id"])
            self.on_event("assistant", f"compacted {len(old)} chat messages into the summary (by hand)")
            return {"compacted": len(old), "summary": self.summary()}
        finally:
            self._turn.release()

    # ----- loading, unloading and idling ------------------------------------
    def _engine(self) -> str:
        return (self.settings.get("integrations")["engine_url"] or "").rstrip("/")

    def models(self) -> dict:
        """What the chat and the memory run on, whether each is loaded, and whether Argos can load or unload it."""
        cfg, mem = self.settings.get("assistant"), self.settings.get("memory")
        url = self._engine()

        def in_engine(model_id: str, last: float) -> dict:
            out = {"model": model_id, "where": "engine", "controllable": bool(url and model_id), "state": "unknown",
                   "last_used": last or None}
            if url and model_id:
                try:
                    out["state"] = engine_api.model_state(url, model_id)
                except Exception:  # noqa: BLE001 - the engine is down: unknown, not "stopped"
                    out["state"] = "unknown"
            return out

        if cfg["mode"] == "engine":
            chat = in_engine(cfg["model"], self.last_used)
        elif cfg["mode"] == "endpoint":
            chat = {"model": cfg["model"], "where": cfg["endpoint"], "controllable": False, "state": "unknown",
                    "last_used": self.last_used or None}
        else:
            sv = self.server_state()
            chat = {"model": (sv.get("profile") or {}).get("name", ""), "where": "here", "controllable": bool(sv.get("profile")),
                    "state": "ready" if sv.get("ready") else "starting" if sv.get("running") else "stopped",
                    "last_used": self.last_used or None}
        last_embed = getattr(self.memory, "last_used", 0.0) if self.memory is not None else 0.0
        if not mem.get("model"):
            embed = {"model": "", "where": "", "controllable": False, "state": "none", "last_used": None}
        elif mem.get("endpoint"):
            embed = {"model": mem["model"], "where": mem["endpoint"], "controllable": False, "state": "unknown", "last_used": last_embed or None}
        else:
            embed = in_engine(mem["model"], last_embed)
        return {"chat": chat, "embed": embed, "idle_unload_min": int(cfg.get("idle_unload_min") or 0)}

    def load(self, which: str) -> dict:
        """Load the chat ("chat") or embedding ("embed") model now, instead of on the first message."""
        info = self.models()[which]
        if not info["controllable"]:
            raise AssistantError("This model runs on another server, so Argos cannot load or unload it." if info["model"]
                                 else "Pick a model first under Assistant settings.")
        if info["where"] == "here":
            self.endpoint()
        else:
            try:
                engine_api.load_model(self._engine(), info["model"])
            except Exception as exc:  # noqa: BLE001
                raise AssistantError(f"The engine could not load {info['model']}: {exc}") from exc
        if which == "chat":
            self.last_used = time.time()
        elif self.memory is not None:
            self.memory.last_used = time.time()
        self.on_event("assistant", f"loaded the assistant's {'chat' if which == 'chat' else 'embedding'} model {info['model']}")
        return self.models()[which]

    def warm_embed(self) -> bool:
        """Start the embedding model the moment a model's stats are finalised.

        Newly logged numbers are meant to be searchable, and searching memory needs
        embeddings. Loading it here also stamps its clock, so the usual idle timer
        covers it from the moment it was needed - the same setting as the chat model,
        counted separately, not a special case for embedding models.
        """
        if self.memory is None:
            return False
        info = self.models()["embed"]
        if not info["model"] or not info["controllable"]:
            return False
        self.memory.last_used = time.time()  # the clock starts now, loaded or not
        if info["state"] in ("ready", "starting"):
            return False
        url, model_id = self._engine(), info["model"]

        def load():
            try:
                engine_api.load_model(url, model_id)
                self.memory.sync(budget=512)  # embed the new numbers now; the idle timer unloads it later
            except Exception as exc:  # noqa: BLE001 - reported as an event, never fatal
                self.on_event("assistant", f"could not start the embedding model {model_id}: {exc}",
                              level="warn")

        threading.Thread(target=load, daemon=True).start()
        self.on_event("assistant", f"started the embedding model {model_id}: the new numbers are searchable")
        return True

    def embed_on_cpu(self) -> bool:
        """Whether the memory's embedding model runs on the CPU: kept off the GPUs by its command (CPU only),
        or fallen back to the CPU now. Then embedding takes nothing from the models on the GPUs, so the memory
        embeds as records come in instead of waiting for a search. Rechecked once a minute."""
        now = time.time()
        model = str(self.settings.get("memory").get("model") or "")
        cached = getattr(self, "_embed_cpu", None)
        if cached and cached[0] == model and now - cached[1] < 60:
            return cached[2]
        url, on = self._engine(), False
        if url and model:
            try:
                launch = engine_api.model_launch(url, model)
                text = " ".join([launch.get("cmd") or ""] + [str(e) for e in launch.get("env") or []])
                on = bool(re.search(r"(HIP|CUDA|ROCR)_VISIBLE_DEVICES=-1\b|(^|\s)(-ngl|--n-gpu-layers|--gpu-layers)[ =]0\b", text))
                if not on:  # fallen back to the CPU while the GPUs were full
                    on = any(m.get("id") == model and m.get("on_cpu") for m in engine_api.pool_state(url, timeout=1.5).get("models") or [])
            except Exception:  # noqa: BLE001 - unknown: keep embedding on search only
                on = False
        self._embed_cpu = (model, now, on)
        return on

    def escalate_model(self) -> str:
        """The bigger model a hard question goes to, or "" when there is none."""
        return str(self.settings.get("assistant").get("escalate_model") or "").strip()

    def _ask_bigger(self, digest: str, question: str) -> str:
        """Put the same question to the escalation model, with the same context."""
        base, model = self.endpoint()
        return self._complete(base, model, [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + digest},
                                            {"role": "user", "content": question}], ANSWER_TOKENS)

    def unload(self, which: str, reason: str = "") -> dict:
        info = self.models()[which]
        if not info["controllable"]:
            raise AssistantError("This model runs on another server, so Argos cannot load or unload it." if info["model"]
                                 else "No model is set.")
        if info["where"] == "here":
            self.stop()
        else:
            try:
                engine_api.unload_model(self._engine(), info["model"])
            except Exception as exc:  # noqa: BLE001
                raise AssistantError(f"The engine could not unload {info['model']}: {exc}") from exc
        if which == "chat":
            self.last_used = 0.0
        elif self.memory is not None:
            self.memory.last_used = 0.0
        self.on_event("assistant", f"unloaded the assistant's {'chat' if which == 'chat' else 'embedding'} model {info['model']}"
                      + (f" ({reason})" if reason else ""))
        return self.models()[which]

    def unload_idle(self, now: Optional[float] = None) -> list[str]:
        """Called every telemetry tick. Unloads a model only after the assistant itself used it and then left it
        alone for ``idle_unload_min`` minutes, so a model some other app loaded is never touched."""
        minutes = int(self.settings.get("assistant").get("idle_unload_min") or 0)
        if minutes <= 0 or self._turn.locked():
            return []
        now = now or time.time()
        done = []
        last = {"chat": self.last_used, "embed": getattr(self.memory, "last_used", 0.0) if self.memory is not None else 0.0}
        # One clock per model. A model of the assistant's own that is loaded but has no
        # clock was started by something else - the schedule, another app - and used to
        # be skipped forever. Give it a clock now, so this timer covers it too.
        for which in ("chat", "embed"):
            if last[which]:
                continue
            info = self.models()[which]
            if not info["controllable"] or info["state"] not in ("ready", "starting"):
                continue
            if which == "chat":
                self.last_used = last["chat"] = now
            elif self.memory is not None:
                self.memory.last_used = last["embed"] = now
        for which, ts in last.items():
            if not ts or now - ts < minutes * 60:
                continue
            info = self.models()[which]
            if not info["controllable"] or info["state"] not in ("ready", "starting"):
                if which == "chat":
                    self.last_used = 0.0
                elif self.memory is not None:
                    self.memory.last_used = 0.0
                continue
            try:
                self.unload(which, reason=f"unused for {minutes} min")
                done.append(which)
            except AssistantError:
                pass
        return done

    def chat(self, text: str, builder: Optional[dict] = None, escalate: bool = False) -> Iterator[dict]:
        """Run one turn; yields {"type": status|delta|compacted|done|error, ...}."""
        if not self._turn.acquire(blocking=False):
            yield {"type": "error", "error": "The assistant is still answering the previous message."}
            return
        try:
            text = text.strip()
            if not text:
                yield {"type": "error", "error": "Empty message."}
                return
            direct = self._direct(text)
            if direct:
                # "list all my models" needs a complete, correct list, not a model's attempt at copying one
                self.store.add_message("user", text)
                yield {"type": "delta", "text": direct}
                self.store.add_message("assistant", direct)
                yield {"type": "done", "tokens": 0}
                return
            # What is on screen for this turn only: digest() reads it while answering.
            self._builder = builder or None
            # The user can send a question straight to the bigger model; one about the past or wanting judgment
            # goes there too. The small model only reads off the live data.
            self._escalated = bool(self.escalate_model()) and (bool(escalate) or bool(NEEDS_BIG.search(text)))
            # Starting the model can take a while: run it aside and stream its progress.
            updates: queue.Queue = queue.Queue()
            result: dict = {}

            def start() -> None:
                try:
                    result["endpoint"] = self.endpoint(status=updates.put)
                except AssistantError as exc:
                    result["error"] = str(exc)
                finally:
                    updates.put(None)

            threading.Thread(target=start, daemon=True).start()
            last = None
            while (update := updates.get()) is not None:
                if update != last:
                    yield {"type": "status", "text": update}
                    last = update
            if "error" in result:
                yield {"type": "error", "error": result["error"]}
                return
            base, model = result["endpoint"]
            self.store.add_message("user", text)
            # the memory search (the embedding model) is the bigger model's; with none set up the small one has it
            digest = self.digest(text, memory=self._escalated or not self.escalate_model())
            if self.compact(base, model, digest):
                yield {"type": "compacted", "summary": self.summary()}
            answer = ""
            try:
                for piece in self._stream(base, model, self._prompt(digest)):
                    answer += piece
                    yield {"type": "delta", "text": piece}
            except AssistantError as exc:
                if answer:
                    self.store.add_message("assistant", answer.strip())
                yield {"type": "error", "error": str(exc)}
                return
            clean, actions = self._actions(answer)
            clean = ESCALATE_RE.sub("", clean).strip()
            # The small model handed the question over: ask the bigger one, once.
            if ESCALATE_RE.search(answer or "") and not self._escalated and self.escalate_model():
                self._escalated = True
                yield {"type": "status", "text": f"This is beyond the small model — asking {self.escalate_model()}…"}
                try:
                    answer = self._ask_bigger(self.digest(text, memory=True), text)
                except AssistantError as exc:  # noqa: BLE001 - the small answer stands
                    yield {"type": "status", "text": f"The bigger model could not answer: {exc}"}
                else:
                    clean, actions = self._actions(answer)
                    clean = ESCALATE_RE.sub("", clean).strip()
            self.store.add_message("assistant", clean or "(no answer)")
            for action in actions:
                yield {"type": "action", "action": action}
            self.last_used = time.time()
            yield {"type": "done", "tokens": self.context_tokens(digest)}
        finally:
            self._turn.release()

    def context_tokens(self, digest: Optional[str] = None) -> int:
        if digest is None:
            # The page asks on every load and every few seconds in the Assistant
            # view, and building the digest is a dozen database queries plus
            # whatever the tools answer. A count that is half a minute old is as
            # good for a meter - so past the memo's age the last one is served
            # and rebuilt off the request, rather than making the page wait for
            # it. Rebuilding inline is what made this endpoint take 28 seconds.
            stamp, text = getattr(self, "_digest_memo", (0.0, ""))
            if not text:
                text = self.digest()
                self._digest_memo = (time.time(), text)
            elif time.time() - stamp > 30 and not getattr(self, "_digest_busy", False):
                self._digest_busy = True
                threading.Thread(target=self._refresh_digest, daemon=True,
                                 name="assistant-digest").start()
            digest = text
        return sum(estimate_tokens(m["content"]) for m in self._prompt(digest))

    def _refresh_digest(self) -> None:
        try:
            text = self.digest()
        except Exception:  # noqa: BLE001 - keep the last good digest
            logger.exception("could not rebuild the assistant digest")
        else:
            self._digest_memo = (time.time(), text)
        finally:
            self._digest_busy = False

    def reset(self) -> None:
        self.store.clear_chat()
        self.store.set_setting(SUMMARY_KEY, None)
        self.on_event("assistant", "chat reset")

    # ----- OpenAI-compatible calls ------------------------------------------
    def _body(self, model: str, messages: list[dict], stream: bool, max_tokens: int) -> bytes:
        temp = self.settings.get("assistant")["temperature"]
        return json.dumps({"model": model, "messages": messages, "stream": stream, "max_tokens": max_tokens,
                           **SAMPLING, "temperature": temp,
                           "chat_template_kwargs": {"enable_thinking": False}}).encode()

    def _open(self, base: str, body: bytes, timeout: float):
        req = urllib.request.Request(base + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AssistantError(f"model server returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AssistantError(f"model server not reachable: {getattr(exc, 'reason', exc)}") from exc

    def _complete(self, base: str, model: str, messages: list[dict], max_tokens: int) -> str:
        with self._open(base, self._body(model, messages, False, max_tokens), 300) as r:
            data = json.loads(r.read().decode("utf-8"))
        return THINK.sub("", (data.get("choices") or [{}])[0].get("message", {}).get("content") or "")

    def ask(self, question: str, extra: str = "", title: str = "") -> str:
        """One question answered from LIVE DATA plus an extra block (e.g. log lines); not added to the chat."""
        base, model = self.endpoint()
        digest = self.digest(question)
        user = (f"{title}:\n{extra}\n\n" if extra else "") + f"QUESTION: {question}"
        answer = self._complete(base, model, [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + digest},
                                              {"role": "user", "content": user}], ANSWER_TOKENS)
        self.last_used = time.time()
        self.on_event("assistant", f"answered a question about {title.lower() or 'the data'}: {question[:80]}")
        return answer.strip()

    def ask_json(self, system: str, user: str, max_tokens: int = 700):
        """One question, answered with JSON: the first JSON value in the reply, or None when there is none."""
        base, model = self.endpoint()
        text = self._complete(base, model, [{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens)
        return first_json(text)

    def _stream(self, base: str, model: str, messages: list[dict]) -> Iterator[str]:
        full, sent = "", ""
        with self._open(base, self._body(model, messages, True, ANSWER_TOKENS), 300) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0].get("delta", {}).get("content") or ""
                except (ValueError, KeyError, IndexError):
                    continue
                full += delta
                visible = THINK.sub("", full)
                if visible.startswith(sent) and len(visible) > len(sent):
                    yield visible[len(sent):]
                    sent = visible


__all__ = ["Assistant", "AssistantError", "first_json", "DEFAULT_REPOS", "DEFAULT_QUANT", "PROFILE_NAME", "estimate_tokens",
           "pick_quant"]
