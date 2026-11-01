"""The scheduling plan for Argos's pool router.

This app is where the plan is made; Argos carries it out. The plan
holds the cards, the reserve, which models never get evicted, and how much VRAM
each model needs. ``render_fragment`` turns it into a YAML file that goes in
Argos's ``-config-dir`` (the ``routing`` block, and the sizes under
``routing.router.settings.pool.sizes`` so no model has to be redefined).

``Engine`` is a Python port of the pool router's decisions (fits, evict, place)
so a plan can be tried here before it is applied. The Go code is the source of
truth; ``tests/test_scheduler.py`` repeats its test cases.
"""
from __future__ import annotations

import copy
import os
import tempfile
import re
import time
from typing import Any, Optional

import yaml

from .db import Store

GIB = 2 ** 30
PLAN_KEY = "schedule"

DEFAULT_PLAN: dict[str, Any] = {
    "enabled": False,        # False writes no routing block, so the engine keeps its own router
    "cards": [],             # GiB per GPU, numbered from 0; [] lets the engine detect them
    "reserve_gib": 2.0,      # kept free for KV growth and buffers
    "protect_seconds": 600,  # a model used this recently is evicted last
    "protect_waits": True,   # ...and is not unloaded for another model at all: that request waits (Go next sends it)
    "placement": "auto",     # auto | off
    "persistent": [],        # model ids that are never evicted to make room
    # model ids where a long new prompt waits while another chat on the model is answering, so the answer keeps
    # its speed; prompts under answer_first_min_tokens go straight in, and none waits past answer_first_max_wait_s
    # model ids that get one request at a time: the rest wait in Argos, in order (each runs at its own full speed)
    "one_at_a_time": [],
    # share guard: hold a model's new request while another model on the same GPU is answering, when that model was
    # seen (in its log) to answer more than share_guard_slowdown % slower beside it; for share_guard_max_wait_s at most
    "share_guard": False,
    "share_guard_slowdown": 50,
    "share_guard_max_wait_s": 120,
    "answer_first": [],
    "answer_first_min_tokens": 4000,
    "answer_first_max_wait_s": 300,
    # Unload a model this schedule loaded once nothing has asked for it for this
    # long. 0 = never. This is the schedule's counterpart to the assistant's
    # idle_unload_min: the timing lives here, not as a per-model ttl in the config.
    "idle_unload_min": 30,
    "sizes": {},             # model id -> GiB, set by hand
    "measured": {},          # model id -> {"gib", "per_card", "at", "load_seconds"}, set by Measure
}
ENGINE_FILE_ENV = "ARGOS_SCHEDULE_FILE"  # the file in the engine's -config-dir that carries the plan (Docker sets it)


def engine_file() -> str:
    """Where the plan is written for the engine, or "" when Argos runs without one (outside Docker)."""
    return os.environ.get(ENGINE_FILE_ENV, "").strip()


def env_plan() -> dict:
    """Starting values from setup.sh / .env: ARGOS_SCHEDULE_POOL=1 turns the pool router on, ARGOS_SCHEDULE_CARDS=32,32
    gives each card's GiB. Anything saved in the Schedule view wins over these."""
    out: dict[str, Any] = {}
    if os.environ.get("ARGOS_SCHEDULE_POOL", "").strip().lower() in ("1", "true", "yes", "on"):
        out["enabled"] = True
    cards = os.environ.get("ARGOS_SCHEDULE_CARDS", "").strip()
    if cards:
        try:
            out.update(clean_plan({"cards": cards}))
        except PlanError:
            pass
    return out

# bytes per KV element for ggml cache types (block size and header included)
CACHE_BYTES = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
               "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32,
               # TurboQuant forks of llama.cpp: about 4.25, 3.5 and 2.5 bits a value (approximate)
               "turbo4": 4.25 / 8, "turbo3": 3.5 / 8, "turbo2": 2.5 / 8}

# Your own cache types (Settings -> KV cache types), name -> bytes per value. Set by set_custom_cache_types.
CUSTOM_CACHE: dict[str, float] = {}


def parse_cache_types(text: str) -> dict[str, float]:
    """"turbo5=3.0, mykv=4.25" -> {"turbo5": 0.375, "mykv": 0.53125} (bits per value -> bytes). Bad entries are skipped."""
    out: dict[str, float] = {}
    for part in re.split(r"[,;\n]+", text or ""):
        name, _, bits = part.partition("=")
        name = name.strip().lower()
        try:
            v = float(bits)
        except ValueError:
            continue
        if name and 0 < v <= 32:
            out[name] = v / 8
    return out


def set_custom_cache_types(text: str) -> None:
    CUSTOM_CACHE.clear()
    CUSTOM_CACHE.update(parse_cache_types(text))


def cache_bytes(name: str) -> Optional[float]:
    return CUSTOM_CACHE.get(name) if name in CUSTOM_CACHE else CACHE_BYTES.get(name)


# The cache types the estimator compares side by side, smallest last.
KV_COMPARE = ("f16", "q8_0", "q5_0", "q4_0", "turbo4", "turbo3", "turbo2")


def kv_gib(arch: Optional[dict], ctx: int, cache_k: str = "", cache_v: str = "") -> Optional[float]:
    """KV cache size alone, for ``ctx`` tokens; None without a model header."""
    if not arch or not ctx:
        return None
    return estimate_gib(0, arch, ctx, cache_k, cache_v)["kv_gib"]


class PlanError(ValueError):
    pass


# ----- the stored plan ---------------------------------------------------------------
def _num(v: Any, what: str, minimum: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise PlanError(f"{what} must be a number") from None
    if x < minimum or x != x:
        raise PlanError(f"{what} must be at least {minimum:g}")
    return x


def _ids(v: Any, what: str) -> list[str]:
    if isinstance(v, str):
        v = [x for x in v.replace("\n", ",").split(",")]
    if not isinstance(v, (list, tuple)):
        raise PlanError(f"{what} must be a list")
    out: list[str] = []
    for x in v:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def clean_plan(values: dict) -> dict:
    """Validate and normalise a (possibly partial) plan; unknown keys are an error."""
    out: dict[str, Any] = {}
    for key, v in values.items():
        if key not in DEFAULT_PLAN:
            raise PlanError(f"unknown schedule setting {key!r}")
        if key in ("enabled", "share_guard", "protect_waits"):
            out[key] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif key == "cards":
            if isinstance(v, str):
                v = [x for x in v.replace(";", ",").split(",") if x.strip()]
            if not isinstance(v, (list, tuple)):
                raise PlanError("cards must be a list of GiB values")
            out[key] = [_num(x, f"cards[{i}]", 0.001) for i, x in enumerate(v)]
        elif key == "reserve_gib":
            out[key] = _num(v, "reserve_gib")
        elif key == "protect_seconds":
            out[key] = int(_num(v, "protect_seconds"))
        elif key == "placement":
            if v not in ("auto", "off"):
                raise PlanError("placement must be auto or off")
            out[key] = v
        elif key in ("persistent", "answer_first", "one_at_a_time"):
            out[key] = _ids(v, key)
        elif key in ("answer_first_min_tokens", "answer_first_max_wait_s", "share_guard_max_wait_s"):
            out[key] = int(_num(v, key, 1))
        elif key == "share_guard_slowdown":
            out[key] = int(_num(v, key, 1))
            if out[key] > 99:
                raise PlanError("share_guard_slowdown must be at most 99 (%)")
        elif key == "sizes":
            if not isinstance(v, dict):
                raise PlanError("sizes must be an object of model id -> GiB")
            out[key] = {str(k).strip(): _num(x, f"sizes.{k}", 0.001) for k, x in v.items() if str(k).strip()}
        elif key == "measured":
            if not isinstance(v, dict):
                raise PlanError("measured must be an object")
            out[key] = v
    return out


class Plans:
    """The plan lives in the settings table under one key, as JSON."""

    def __init__(self, store: Store):
        self.store = store

    def get(self) -> dict:
        saved = self.store.get_settings().get(PLAN_KEY)
        plan = copy.deepcopy(DEFAULT_PLAN)
        plan.update(env_plan())
        if isinstance(saved, dict):
            plan.update({k: v for k, v in saved.items() if k in DEFAULT_PLAN})
        return plan

    def update(self, values: dict) -> dict:
        plan = self.get()
        plan.update(clean_plan(values))
        self.store.set_setting(PLAN_KEY, plan)
        return plan

    def set_measured(self, model_id: str, gib: float, per_card: list[float], load_seconds: Optional[float] = None) -> dict:
        plan = self.get()
        plan["measured"][model_id] = {"gib": round(gib, 2), "per_card": [round(x, 2) for x in per_card],
                                      "at": time.time(), "load_seconds": load_seconds}
        self.store.set_setting(PLAN_KEY, plan)
        return plan


def _iso_ts(value) -> float:
    """The engine's last_used (RFC 3339) as a Unix time; 0 when missing or unreadable."""
    if not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def idle_unload(plans, store, engine_url, event, now=None, loaded_at: Optional[dict] = None) -> list[str]:
    """Unload the models this schedule loaded once nothing has asked for them for a while.

    The engine unloads its own models when a new one needs the room, but a model
    nothing else is waiting for lives forever. This is the schedule's timer, so the
    timing is a setting here rather than a per-model ttl in the engine's config.

    A model the plan keeps loaded is never touched, as is one the engine still counts
    as protected (used inside its own protect window). A model with no request
    history is left alone too: without evidence it was ever used, unloading it is a
    guess, and a guess here costs a reload of minutes.
    """
    from .integrations import engine_api
    plan = plans.get()
    minutes = int(plan.get("idle_unload_min") or 0)
    if loaded_at is None:
        try:
            loaded_at = store.open_session_starts()  # model id -> when its current load began
        except Exception:  # noqa: BLE001 - older stores: the other signs of use still count
            loaded_at = {}
    if minutes <= 0 or not engine_url:
        return []
    now = now or time.time()
    keep = {str(m) for m in plan.get("persistent") or []}
    try:
        pool = engine_api.pool_state(engine_url)
    except Exception:  # noqa: BLE001 - the pool page reports an unreachable engine
        return []
    done = []
    for model in pool.get("models") or []:
        model_id = str(model.get("id") or "")
        if not model_id or model.get("persistent") or model.get("protected") or model_id in keep:
            continue
        # only a model that is up: one still starting is being loaded because something just asked for it
        if str(model.get("state") or "") != "ready":
            continue
        history = store.list_requests(model=model_id, limit=1)
        if not history:
            continue
        # idle since the latest sign of use: its last recorded request, the engine's own last-used time,
        # or the start of this load. The request history alone said "unused for hours" about a model
        # loaded a second ago for a request still in flight, and unloading it failed that request.
        seen = [float(history[0].get("ts") or 0), (loaded_at or {}).get(model_id) or 0.0, _iso_ts(model.get("last_used"))]
        idle_s = now - max(seen)
        if idle_s < minutes * 60:
            continue
        try:
            engine_api.unload_model(engine_url, model_id)
        except Exception:  # noqa: BLE001 - try again on the next tick
            continue
        done.append(model_id)
        event("schedule", f"unloaded {model_id}: unused for {idle_s / 60:.0f} min",
              data={"model": model_id, "idle_seconds": round(idle_s)})
    return done


def effective_size(plan: dict, model_id: str, estimate: float = 0.0) -> tuple[float, str]:
    """GiB and where it came from: a size set by hand, a measurement, or the estimate."""
    if plan["sizes"].get(model_id):
        return float(plan["sizes"][model_id]), "plan"
    m = plan["measured"].get(model_id)
    if m and m.get("gib"):
        return float(m["gib"]), "measured"
    if estimate:
        return float(estimate), "estimate"
    return 0.0, ""


# ----- size estimate -----------------------------------------------------------------
def estimate_gib(weights_bytes: int, arch: Optional[dict], ctx: int, cache_k: str = "", cache_v: str = "",
                 extra_bytes: int = 0) -> dict:
    """VRAM a launch needs: weights, KV cache for ``ctx`` tokens, and buffers.

    The buffer term (1 GiB + 10% of the weights) is a rule of thumb that matches
    a measured 35B MoE; a Measure run replaces the whole estimate.
    """
    weights = (weights_bytes + extra_bytes) / GIB
    notes: list[str] = []
    kv = 0.0
    if arch and ctx:
        bk, bv = cache_bytes(cache_k or "f16"), cache_bytes(cache_v or cache_k or "f16")
        if bk is None or bv is None:
            bad = cache_k if bk is None else (cache_v or cache_k)
            bk, bv = bk or CACHE_BYTES["q8_0"], bv or CACHE_BYTES["q8_0"]
            notes.append(f"unknown cache type {bad!r}, counted as q8_0: set its bits per value under Settings -> KV cache types")
        per_token = arch["kv_heads"] * (arch["key_len"] * bk + arch["value_len"] * bv)
        swa, window = int(arch.get("swa_layers") or 0), int(arch.get("swa_window") or 0)
        full = arch["attn_layers"] - swa
        kv = (full * ctx + swa * min(ctx, window)) * per_token / GIB
        if swa:
            notes.append(f"{swa} sliding-window layers keep {window} tokens")
        if any(t.startswith("turbo") or t in CUSTOM_CACHE for t in (cache_k, cache_v) if t):
            notes.append("turbo and custom cache sizes come from bits per value, so they are approximate")
    else:
        notes.append("no model header, KV cache not counted")
    overhead = 1.0 + 0.10 * weights
    return {"gib": round(weights + kv + overhead, 2), "weights_gib": round(weights, 2), "kv_gib": round(kv, 2),
            "overhead_gib": round(overhead, 2), "notes": notes}


# ----- the engine's copy ---------------------------------------------------------------
HEADER = ("# Written by Argos from the Schedule view. Edits are overwritten.\n"
          "# The engine reloads it on change and keeps running models whose own\n"
          "# config did not change.\n")


def render_fragment(plan: dict, sizes: Optional[dict[str, float]] = None,
                    known: Optional[set[str]] = None, cpu_fallback: Optional[list[str]] = None) -> tuple[str, list[str]]:
    """The YAML the engine reads, and the model ids left out because the engine does not know them.

    ``sizes`` are the effective sizes to use. When ``known`` is given, ids
    outside it are skipped: the engine refuses a config that names a model it
    does not have.
    """
    if not plan["enabled"]:
        return HEADER + "# Scheduling is turned off in the Schedule view, so the engine uses its own router.\n", []
    skipped: list[str] = []

    def keep(mid: str) -> bool:
        if known is None or mid in known:
            return True
        skipped.append(mid)
        return False

    pool: dict[str, Any] = {}
    if plan["cards"]:
        pool["cards"] = [round(c, 3) for c in plan["cards"]]
    pool["reserveGiB"] = plan["reserve_gib"]
    pool["protectSeconds"] = plan["protect_seconds"]
    pool["protectWaits"] = bool(plan.get("protect_waits", True))
    pool["placement"] = plan["placement"]
    persistent = [m for m in plan["persistent"] if keep(m)]
    if persistent:
        pool["persistent"] = persistent
    if plan.get("share_guard"):
        pool["shareGuard"] = {"enabled": True, "maxSlowdownPercent": plan.get("share_guard_slowdown") or 50,
                              "maxWaitSeconds": plan.get("share_guard_max_wait_s") or 120}
    cpu = [m for m in dict.fromkeys(cpu_fallback or []) if m and keep(m)]
    if cpu:
        pool["cpuFallback"] = cpu
    serial = [m for m in plan.get("one_at_a_time") or [] if keep(m)]
    if serial:
        pool["oneAtATime"] = serial
    answer_first = [m for m in plan.get("answer_first") or [] if keep(m)]
    if answer_first:
        pool["answerFirst"] = {"models": answer_first, "minPromptTokens": plan.get("answer_first_min_tokens") or 4000,
                               "maxWaitSeconds": plan.get("answer_first_max_wait_s") or 300}
    known_sizes = {m: round(g, 2) for m, g in sorted((sizes or {}).items()) if g and keep(m)}
    if known_sizes:
        pool["sizes"] = known_sizes
    doc = {"routing": {"router": {"use": "pool", "settings": {"pool": pool}}}}
    return HEADER + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False), sorted(set(skipped))


def write_fragment(path: str, text: str) -> bool:
    """Atomic write, so the engine never reads half a file. False when nothing changed."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".scheduler-", suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


# ----- the engine: a port of the pool router's decisions ------------------------------
class State:
    """What the pool router tracks per model while running."""

    def __init__(self) -> None:
        self.placed: dict[str, list[str]] = {}
        self.busy: dict[str, int] = {}
        self.last_used: dict[str, float] = {}
        self.load_time: dict[str, float] = {}
        self.now = 0.0


class Engine:
    def __init__(self, plan: dict, sizes: dict[str, float], detected_gib: Optional[list[float]] = None):
        cards = list(plan["cards"]) or list(detected_gib or [])
        self.reserve = float(plan["reserve_gib"])
        self.protect = float(plan["protect_seconds"])
        self.persistent = set(plan["persistent"])
        self.sizes = {k: float(v) for k, v in sizes.items() if v and v > 0}
        self.known = set(sizes)
        n = len(cards)
        self.cards = [(str(i), max(c - self.reserve / n, 0.0)) for i, c in enumerate(cards)] if plan["placement"] == "auto" else []
        self.budget = sum(cards) - self.reserve

    # sizes: a model with no size takes the whole pool, so it runs alone
    def size(self, mid: str) -> float:
        return self.sizes.get(mid) or max(self.budget, 0.0) + 1

    def usage(self, mid: str, card: tuple[str, float], st: State) -> float:
        if not self.sizes.get(mid):
            return card[1]
        cards = st.placed.get(mid)
        if not cards:
            return self.sizes[mid] / len(self.cards)
        return self.sizes[mid] / len(cards) if card[0] in cards else 0.0

    def fits(self, target: str, others: list[str], st: State) -> tuple[bool, Optional[list[str]]]:
        if not self.cards:
            return sum(self.size(o) for o in others) + self.size(target) <= self.budget + 1e-9, None
        if not self.sizes.get(target):
            return not others, [c[0] for c in self.cards]
        used = {c[0]: sum(self.usage(o, c, st) for o in others) for c in self.cards}
        models = {c[0]: sum(1 for o in others if self.usage(o, c, st) > 0) for c in self.cards}
        free = {c[0]: c[1] - used[c[0]] for c in self.cards}
        need = self.sizes[target]
        best = None
        for c in self.cards:
            if free[c[0]] + 1e-9 < need:
                continue
            if best is None or models[c[0]] < models[best] or (models[c[0]] == models[best] and free[c[0]] > free[best]):
                best = c[0]
        if best is not None:
            return True, [best]
        by_free = sorted(self.cards, key=lambda c: -free[c[0]])
        for k in range(2, len(by_free) + 1):
            if free[by_free[k - 1][0]] + 1e-9 >= need / k:
                return True, sorted((c[0] for c in by_free[:k]), key=int)
        return False, None

    def reload_cost(self, mid: str, st: State) -> float:
        return st.load_time.get(mid, self.size(mid))  # one second per GiB until measured

    def rank(self, ids: list[str], st: State) -> list[str]:
        def tier(i: str) -> int:
            if st.busy.get(i, 0) > 0:
                return 2
            return 1 if st.now - st.last_used.get(i, float("-inf")) < self.protect else 0

        def keep(i: str) -> float:
            idle_min = (st.now - st.last_used.get(i, 0.0)) / 60
            return self.reload_cost(i, st) / (1 + idle_min)

        return sorted(ids, key=lambda i: (tier(i), keep(i), i))

    def eviction_for(self, target: str, running: list[str], st: State) -> list[str]:
        others = [r for r in running if r != target]
        if not others or self.fits(target, others, st)[0]:
            return []
        candidates = self.rank([o for o in others if o not in self.persistent], st)
        evict: list[str] = []
        remaining = list(others)
        for c in candidates:
            if self.fits(target, remaining, st)[0]:
                break
            evict.append(c)
            remaining.remove(c)
        if not self.fits(target, remaining, st)[0]:
            return candidates
        kept = []
        for c in reversed(evict):
            if self.fits(target, remaining + [c], st)[0]:
                remaining.append(c)
            else:
                kept.append(c)
        return sorted(kept)


# ----- the simulator ------------------------------------------------------------------
def simulate(engine: Engine, steps: list[dict], measured_load: Optional[dict[str, float]] = None) -> list[dict]:
    """Run request/finish/wait/unload steps through the engine.

    Mirrors the router: a request for a running model is served; otherwise the
    engine picks what to evict, and the request queues while any model it would
    evict is still serving. Loading takes the model's measured load time, or one
    second per GiB.
    """
    st = State()
    st.load_time.update(measured_load or {})
    running: list[str] = []
    queue: list[str] = []
    out: list[dict] = []

    def snapshot() -> dict:
        cards = []
        for c in engine.cards:
            used = sum(engine.usage(m, c, st) for m in running)
            cards.append({"id": c[0], "cap_gib": round(c[1], 2), "used_gib": round(used, 2),
                          "free_gib": round(max(c[1] - used, 0), 2),
                          "models": [m for m in running if engine.usage(m, c, st) > 0]})
        total = sum(engine.size(m) for m in running)
        return {"clock_s": round(st.now, 1), "cards": cards, "queue": list(queue),
                "running": [{"id": m, "cards": st.placed.get(m, []), "busy": st.busy.get(m, 0)} for m in running],
                "used_gib": round(total, 2), "budget_gib": round(engine.budget, 2)}

    def request(model: str) -> dict:
        if model not in engine.known:
            return {"result": "error", "note": f"{model} has no size in the plan and is not in the model list"}
        if model in running:
            st.busy[model] = st.busy.get(model, 0) + 1
            st.last_used[model] = st.now
            return {"result": "served", "cards": st.placed.get(model, []), "note": "already loaded"}
        evict = engine.eviction_for(model, running, st)
        busy = [e for e in evict if st.busy.get(e, 0) > 0]
        if busy:
            if model not in queue:
                queue.append(model)
            return {"result": "queued", "evicted": [], "note": f"waits for {', '.join(busy)} to finish"}
        for e in evict:
            running.remove(e)
            st.placed.pop(e, None)
            st.busy.pop(e, None)
        ok, cards = engine.fits(model, running, st)
        if not engine.sizes.get(model) and running:
            return {"result": "error", "note": f"{model} has no size and cannot share the pool"}
        st.placed[model] = cards or []
        running.append(model)
        took = st.load_time.get(model) or engine.size(model)
        st.now += took
        st.load_time[model] = took
        st.busy[model] = 1
        st.last_used[model] = st.now
        note = f"loaded in {took:.0f}s" + (f", evicting {', '.join(evict)}" if evict else "")
        return {"result": "loaded", "cards": cards or [], "evicted": evict, "note": note, "load_seconds": round(took, 1)}

    for i, step in enumerate(steps):
        op, model = step.get("op"), (step.get("model") or "").strip()
        entry: dict[str, Any] = {"step": i + 1, "op": op, "model": model}
        if op == "request":
            entry.update(request(model))
        elif op == "finish":
            if st.busy.get(model, 0) <= 0:
                entry.update(result="error", note=f"{model} has no request in flight")
            else:
                st.busy[model] -= 1
                st.last_used[model] = st.now
                entry.update(result="finished", note="")
                drained = []
                for q in list(queue):
                    r = request(q)
                    if r["result"] != "queued":
                        queue.remove(q)
                        drained.append(f"{q}: {r.get('note') or r['result']}")
                if drained:
                    entry["drained"] = drained
        elif op == "wait":
            minutes = float(step.get("minutes") or 0)
            st.now += minutes * 60
            entry.update(result="waited", note=f"{minutes:g} min")
        elif op == "unload":
            if model in running:
                running.remove(model)
                st.placed.pop(model, None)
                st.busy.pop(model, None)
                entry.update(result="unloaded", note="")
            else:
                entry.update(result="error", note=f"{model} is not loaded")
        else:
            entry.update(result="error", note=f"unknown step {op!r}")
        entry["state"] = snapshot()
        out.append(entry)
    return out


__all__ = ["DEFAULT_PLAN", "PlanError", "Plans", "clean_plan", "effective_size", "estimate_gib",
           "render_fragment", "write_fragment", "Engine", "State", "simulate"]
