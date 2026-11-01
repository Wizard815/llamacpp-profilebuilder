"""The assistant's memory: the database as short texts, searchable by meaning.

Every useful row (events, notes, presets, benchmarks, loads, per-model speed summaries,
new settings, and Odysseus' Brain when connected) becomes one short text in
``memory_records``. When an embedding model is set, each text gets a vector from any
OpenAI-compatible ``/v1/embeddings`` endpoint (normally the engine serving nomic-embed),
stored as float32 in SQLite.

A question is answered from the records closest to it: vector similarity and keyword
match are combined with reciprocal-rank fusion, so exact names (a model id, "turbov3")
are found even when the embedding model blurs them, and everything still works by
keyword alone when no embedding model is configured.
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from .db import Store
from .compare import change_lines_from
from .model_perf import model_perf

try:  # optional: makes search over many records fast
    import numpy as _np
except ImportError:  # pragma: no cover - the pure-Python path is tested instead
    _np = None

MAX_TEXT = 1200
RRF_K = 60
TOKEN = re.compile(r"[a-z0-9][a-z0-9_.\-]+")


class MemoryError_(Exception):
    pass


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _vec_bytes(v: list[float]) -> bytes:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array.array("f", (x / n for x in v)).tobytes()


def _vec(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


# records embedded on the way into a search: the ones changed since the last search, bounded
SEARCH_EMBED_BUDGET = 32


class Memory:
    def __init__(self, store: Store, settings: Callable[[], dict], on_event: Callable[..., None]):
        self.store = store
        self.settings = settings  # -> {"memory": {...}, "integrations": {...}}
        self.on_event = on_event
        self._lock = threading.Lock()
        self._matrix: Optional[tuple] = None  # (stamp, ids, numpy matrix) cache for vector search
        self.last_error = ""
        self.last_used = 0.0  # when the embedding model last answered, for unloading it when idle
        self.extra_sources: list[Callable[[], Iterable[tuple]]] = []  # e.g. Odysseus' Brain
        self.pick: Optional[Callable[[], str]] = None  # which embedding model to use now (Assistant.pick)

    # ----- configuration ---------------------------------------------------------------
    def config(self) -> dict:
        s = self.settings()
        m = dict(s.get("memory") or {})
        if not m.get("endpoint") and s["integrations"].get("engine_url"):
            m["endpoint"] = s["integrations"]["engine_url"].rstrip("/") + "/v1"
        if self.pick is not None:  # the GPU model, or its CPU alternative when the GPUs are full
            try:
                m["model"] = self.pick() or m.get("model") or ""
            except Exception:  # noqa: BLE001 - the model set
                pass
        return m

    def vector_key(self) -> str:
        """What a vector is filed under: the model, not the profile (<model>--<label>). Two profiles of one
        embedding model, one on a GPU and one on the CPU, make the same vectors, so switching between them
        re-embeds nothing."""
        return (self.config().get("model") or "").split("--", 1)[0]

    def _same_model(self) -> tuple[str, tuple]:
        """A WHERE fragment matching vectors of this model, under its key or (older ones) a profile of it."""
        key = self.vector_key()
        like = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "--%"
        return "(model = ? OR model LIKE ? ESCAPE '\\')", (key, like)

    def vectors_on(self) -> bool:
        c = self.config()
        return bool(c.get("enabled") and c.get("endpoint") and c.get("model"))

    # ----- what gets remembered ----------------------------------------------------------
    def collect(self) -> list[tuple[str, str, float, str]]:
        """(source, key, ts, text) for everything worth remembering."""
        st, out = self.store, []
        for e in st.list_events(limit=5000):
            out.append(("event", str(e["id"]), e["ts"], f"{_day(e['ts'])} {e['level']} {e['kind']}: {e['message']}"))
        for n in st.list_notes():
            out.append(("note", str(n["id"]), n["updated_at"], f"Note “{n['title'] or 'untitled'}” ({_day(n['updated_at'])}): {n['body']}"))
        for p in st.list_profiles():
            out.append(("preset", p["profile_id"], p.get("updated_at") or 0,
                        f"Preset “{p['name']}” for {p.get('model_id') or p.get('base_model')}: {p['launch_command']}"))
        for r in st.list_runs(limit=5000):
            out.append(("benchmark", r["run_id"], r["created_at"],
                        f"Benchmark {_day(r['created_at'])} {r.get('model_id') or r['base_model']}: {r.get('tokens_per_second') or 0:.1f} t/s gen, "
                        f"{r.get('prompt_per_second') or 0:.0f} t/s prompt, ctx {r.get('n_ctx') or '?'}, kv {r.get('cache_type_k') or 'f16'}; "
                        f"command: {r['launch_command'][:400]}"))
        for s in st.list_sessions(limit=5000):
            gpus = ",".join(s["gpus"]) or "?"
            end = "still loaded" if s["open"] else f"until {_day(s['ended_at'])}"
            out.append(("load", str(s["id"]), s["started_at"],
                        f"Load {_day(s['started_at'])} {end}: {s['model_id']} on GPU {gpus}, quant {s['quant'] or '?'}, ctx {s['ctx'] or '?'}, "
                        f"kv {s['cache_k'] or 'f16'}/{s['cache_v'] or 'f16'}, split {s['split_mode'] or 'default'}, spec {s['spec_type'] or 'none'}, "
                        f"np {s['parallel'] or 'default'}, VRAM {s['measured_gib'] or s['size_gib'] or '?'} GiB"))
        for t in st.list_traits():
            out.append(("setting", str(t["id"]), t["last_seen"],
                        f"Setting {t['kind']} {t['value']}: {t['status']}, used {t['count']} times, first seen {_day(t['first_seen'])}"
                        + (f", same as {t['canonical']}" if t["canonical"] else "") + (f". Note: {t['note']}" if t["note"] else "")))
        day = 86400
        for r in st.request_stats(time.time() - 30 * day):
            out.append(("speed", r["model"] or "?", r["last_ts"] or time.time(),
                        f"Speed last 30 days for {r['model']}: {r['requests']} requests, generation {r['tg_avg'] or 0:.1f} t/s avg "
                        f"(max {r['tg_max'] or 0:.1f}), prompt {r['pp_avg'] or 0:.0f} t/s avg, {r['errors']} errors"))
        out.extend(self._model_records())
        for source in self.extra_sources:
            try:
                out.extend(source())
            except Exception as exc:  # noqa: BLE001 - an outside source must not break the rest
                self.last_error = f"{getattr(source, '__name__', 'source')}: {exc}"
        return [(s, k, ts or 0.0, text[:MAX_TEXT]) for s, k, ts, text in out if text.strip()]

    def _model_records(self) -> list[tuple[str, str, float, str]]:
        """One record per model ever seen, and one per launch config that has measured speeds."""
        out: list[tuple[str, str, float, str]] = []
        for m in self.store.list_models_seen():
            extra = m.get("extra") or {}
            bits = [f"Model {m['model_id']}", f"kind {m['kind'] or 'llm'}", f"arch {m['arch'] or '?'}", f"quant {m['quant'] or '?'}"]
            if m.get("size_bytes"):
                bits.append(f"{m['size_bytes'] / 2**30:.1f} GiB")
            for k in ("params", "context_length", "experts", "n_layers"):
                if extra.get(k):
                    bits.append(f"{k} {extra[k]}")
            bits.append("loaded now" if m.get("present") else "not on disk")
            out.append(("model", m["model_id"], m["last_seen"], ", ".join(bits)))
            try:
                perf = model_perf(self.store, m["model_id"], days=180)
            except Exception:  # noqa: BLE001 - one odd model must not stop the rest
                continue
            for c in perf["configs"]:
                dec, pre = c["decode"].get("median"), c["prefill"].get("median")
                if not (dec or pre):
                    continue
                st = c.get("settings") or {}
                flags = " ".join(f"--{k}" + ("" if v is True else f" {v}") for k, v in sorted(st.items()) if not k.isupper() and k != "model")
                env = " ".join(f"{k}={v}" for k, v in sorted(st.items()) if k.isupper())
                out.append(("config", f"{m['model_id']}|{c['id']}", c.get("last_used") or m["last_seen"],
                            f"Config of {m['model_id']} ({m['quant'] or '?'}, {m['arch'] or '?'}): {c['label']}. "
                            f"Flags: {flags or 'defaults'}. Env: {env or 'none'}. "
                            f"Decode {dec or 0:.1f} t/s, prefill {pre or 0:.0f} t/s over {c['requests'] + c['benchmarks']} samples, "
                            f"VRAM {c.get('vram_gib') or '?'} GiB, peak {c.get('concurrency_max') or 1} at once"))
            # what each small change did, next to the config used most (e.g. "ctx-size 32768→65536; decode 62→58 t/s")
            for i, line in enumerate(change_lines_from(m["model_id"], perf["configs"], limit=8)):
                out.append(("change", f"{m['model_id']}|{i}", m["last_seen"], "Change: " + line))
        return out

    # ----- keeping it current ------------------------------------------------------------
    def sync(self, budget: int = 64, embed: bool = True) -> dict:
        """Add new and changed records, drop ones that are gone, and embed up to ``budget`` texts.

        The background tick passes ``embed=False``: records change all the time (every request moves a speed
        record), so embedding them on every tick kept the embedding model busy forever - its own requests
        changed the records it then had to embed again, and it was never idle long enough to unload. Pending
        records are embedded when memory is searched instead, which needs the model anyway."""
        with self._lock:
            current = self.collect()
            seen = {(s, k) for s, k, _, _ in current}
            have = {(r["source"], r["key"]): r for r in self.store._q("SELECT id, source, key, hash FROM memory_records")}
            added = changed = 0
            for s, k, ts, text in current:
                h = hashlib.sha1(text.encode()).hexdigest()
                row = have.get((s, k))
                if row is None:
                    self.store._x("INSERT INTO memory_records (source, key, ts, text, hash) VALUES (?,?,?,?,?)", (s, k, ts, text, h))
                    added += 1
                elif row["hash"] != h:
                    self.store._x("UPDATE memory_records SET ts=?, text=?, hash=?, vector=NULL, model='', dims=0 WHERE id=?", (ts, text, h, row["id"]))
                    changed += 1
            gone = [r["id"] for key, r in have.items() if key not in seen]
            for rid in gone:
                self.store._x("DELETE FROM memory_records WHERE id=?", (rid,))
            embedded = self._embed_pending(budget) if embed and self.vectors_on() else 0
            if added or changed or gone or embedded:
                self._matrix = None
            return {"added": added, "changed": changed, "removed": len(gone), "embedded": embedded}

    def _embed_pending(self, budget: int) -> int:
        c = self.config()
        model = self.vector_key()
        same, args = self._same_model()
        rows = self.store._q(f"SELECT id, text FROM memory_records WHERE vector IS NULL OR NOT {same} ORDER BY ts DESC LIMIT ?",
                             (*args, budget))
        done, batch = 0, max(1, int(c.get("batch") or 16))
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            try:
                vecs = self.embed([r["text"] for r in chunk])
            except MemoryError_ as exc:
                if self.last_error != str(exc):
                    self.on_event("memory", f"could not embed records: {exc}", level="warn")
                self.last_error = str(exc)
                break
            self.last_error = ""
            for r, v in zip(chunk, vecs):
                self.store._x("UPDATE memory_records SET vector=?, model=?, dims=?, indexed_at=? WHERE id=?",
                              (_vec_bytes(v), model, len(v), time.time(), r["id"]))
                done += 1
        return done

    def embed(self, texts: list[str]) -> list[list[float]]:
        c = self.config()
        body = json.dumps({"model": c["model"], "input": texts}).encode()
        req = urllib.request.Request(c["endpoint"].rstrip("/") + "/embeddings", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=float(c.get("timeout_s") or 120)) as r:  # noqa: S310
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise MemoryError_(f"embedding server returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise MemoryError_(f"embedding server not reachable: {getattr(exc, 'reason', exc)}") from exc
        items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        vecs = [d.get("embedding") for d in items]
        if len(vecs) != len(texts) or not all(isinstance(v, list) and v for v in vecs):
            raise MemoryError_("embedding server returned an unexpected answer (is the model an embedding model?)")
        self.last_used = time.time()
        return vecs

    def rebuild(self) -> None:
        """Forget every vector; the next syncs embed everything again (after changing the model)."""
        self.store._x("UPDATE memory_records SET vector=NULL, model='', dims=0, indexed_at=NULL")
        self._matrix = None

    # ----- asking it ---------------------------------------------------------------------
    def search(self, query: str, k: int = 8, sources: Optional[set[str]] = None) -> list[dict]:
        """The ``k`` records closest to ``query``: by meaning and by words, fused."""
        query = (query or "").strip()
        if not query:
            return []
        rows = self.store._q("SELECT id, source, key, ts, text FROM memory_records")
        if sources:
            rows = [r for r in rows if r["source"] in sources]
        if not rows:
            return []
        by_id = {r["id"]: r for r in rows}
        ranked_kw = self._keyword(query, rows)
        ranked_vec: list[tuple[int, float]] = []
        if self.vectors_on():
            try:
                with self._lock:
                    if self._embed_pending(SEARCH_EMBED_BUDGET):
                        self._matrix = None
                ranked_vec = self._vector(query, set(by_id))
            except MemoryError_ as exc:
                self.last_error = str(exc)
        scores: dict[int, float] = {}
        how: dict[int, set] = {}
        for label, ranked in (("words", ranked_kw), ("meaning", ranked_vec)):
            for rank, (rid, _) in enumerate(ranked[:50]):
                scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
                how.setdefault(rid, set()).add(label)
        sim = dict(ranked_vec)
        best = sorted(scores, key=lambda rid: -scores[rid])[:k]
        return [{**by_id[rid], "score": round(scores[rid], 4), "similarity": round(sim[rid], 3) if rid in sim else None,
                 "matched_by": sorted(how[rid])} for rid in best]

    def similar(self, source: str, key: str, k: int = 6, sources: Optional[set[str]] = None) -> list[dict]:
        """Records closest to one stored record, by its own vector (or its words when it has none)."""
        rec = self.store._q("SELECT id, text, vector, model, dims FROM memory_records WHERE source=? AND key=?", (source, key))
        if not rec:
            return []
        rec = rec[0]
        want = sources or {source}
        rows = [r for r in self.store._q("SELECT id, source, key, ts, text FROM memory_records") if r["source"] in want and r["id"] != rec["id"]]
        by_id = {r["id"]: r for r in rows}
        sims: list[tuple[int, float]] = []
        same, args = self._same_model()
        if rec["vector"] is not None and (rec["model"] or "").split("--", 1)[0] == self.vector_key():
            other = self.store._q(f"SELECT id, vector FROM memory_records WHERE vector IS NOT NULL AND {same} AND dims=?", (*args, rec["dims"]))
            mine = _vec(rec["vector"])
            for r in other:
                if r["id"] in by_id:
                    sims.append((r["id"], sum(a * b for a, b in zip(_vec(r["vector"]), mine))))
            sims.sort(key=lambda x: -x[1])
        how = "meaning"
        if not sims:
            sims, how = self._keyword(rec["text"], list(by_id.values())), "words"
        return [{**by_id[rid], "similarity": round(sc, 3) if how == "meaning" else None, "matched_by": [how]}
                for rid, sc in sims[:k]]

    def _keyword(self, query: str, rows: list[dict]) -> list[tuple[int, float]]:
        q = set(tokens(query))
        if not q:
            return []
        docs = [(r["id"], set(tokens(r["text"]))) for r in rows]
        n = len(docs)
        df = {t: sum(1 for _, d in docs if t in d) for t in q}
        out = []
        for rid, d in docs:
            s = sum(math.log(1 + n / df[t]) for t in q if t in d and df[t])
            if s:
                out.append((rid, s))
        return sorted(out, key=lambda x: -x[1])

    def _vector(self, query: str, allowed: set[int]) -> list[tuple[int, float]]:
        model = self.config()["model"]
        qv = array.array("f")
        qv.frombytes(_vec_bytes(self.embed([query])[0]))
        same, args = self._same_model()
        rows = self.store._q(f"SELECT id, vector, dims FROM memory_records WHERE vector IS NOT NULL AND {same}", args)
        rows = [r for r in rows if r["id"] in allowed and r["dims"] == len(qv)]
        if not rows:
            return []
        if _np is not None:
            stamp = (model, len(rows), max(r["id"] for r in rows))
            if not self._matrix or self._matrix[0] != stamp:
                self._matrix = (stamp, [r["id"] for r in rows], _np.vstack([_np.frombuffer(r["vector"], dtype=_np.float32) for r in rows]))
            _, ids, mat = self._matrix
            sims = mat @ _np.frombuffer(qv.tobytes(), dtype=_np.float32)
            pairs = list(zip(ids, (float(x) for x in sims)))
        else:
            pairs = [(r["id"], sum(a * b for a, b in zip(_vec(r["vector"]), qv))) for r in rows]
        return sorted(pairs, key=lambda x: -x[1])

    def status(self) -> dict:
        c = self.config()
        same, args = self._same_model()
        counts = self.store._q(f"SELECT COUNT(*) AS n, SUM(CASE WHEN vector IS NOT NULL AND {same} THEN 1 ELSE 0 END) AS v, "
                               "MAX(indexed_at) AS last FROM memory_records", args)[0]
        by_source = {r["source"]: r["n"] for r in self.store._q("SELECT source, COUNT(*) AS n FROM memory_records GROUP BY source")}
        return {"records": counts["n"] or 0, "embedded": counts["v"] or 0, "last_indexed": counts["last"],
                "vectors_on": self.vectors_on(), "endpoint": c.get("endpoint") or "", "model": c.get("model") or "",
                "enabled": bool(c.get("enabled")), "error": self.last_error, "by_source": by_source,
                "fast_search": _np is not None}


__all__ = ["Memory", "tokens"]
