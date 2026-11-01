"""Persistence -- the app's database.

Runs on either SQLite (the default, a file under /data) or Postgres (set
ARGOS_DATABASE_URL). The two differ in a handful of places - placeholders, a
time bucket, introspection, and how an INSERT reports the id it just made - and
those live in dialect.py so that everything below is ordinary SQL.

Tables
------
runs            one row per benchmark / recorded launch (the perf sheet)
profiles        saved launch commands; no limit, any number per model
catalog_files   extra mmproj / draft-model files keyed to a base model
env_flags       last performance seen with each env var set
settings        UI-editable settings (JSON values), overriding env defaults
events          long-term app log: launches, exits, benches, downloads, errors
gpu_samples     raw GPU readings (kept for the raw retention window)
gpu_hourly      hourly GPU rollups, kept forever
requests        per-request speeds pulled from Argos's activity log
downloads       Hugging Face downloads and their progress
chat_messages   the assistant's single conversation
models_seen     every model ever seen (library, Argos, profiles), present or gone
model_sessions  every time a model was loaded: GPUs, VRAM, quant, cache types, flags
traits          every quant / cache type / split mode / spec type / flag / env var ever seen
host_snapshots  hardware and container facts, kept when they change
notes           sticky notes: open ones float over every page, minimized ones wait in the Notes menu
servers         machines models run on: Local plus SSH servers, and what Check found on each
memory_records  the assistant's memory: text from every other table, with an embedding vector each

Databases written by earlier versions are migrated in place on open.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import threading
import weakref

from . import dialect
import time
from typing import Any, Iterable, Optional

log = logging.getLogger("argos.db")
SLICE_S = 86400  # how much request history one locked read covers: small, so nothing waits long behind it
SLOW_DB_S = 1.0  # a query that waits for the lock, or runs, longer than this is logged with where the time went

RUN_COLUMNS: dict[str, str] = {
    "run_id": "TEXT NOT NULL UNIQUE",
    "launch_id": "TEXT",
    "created_at": "REAL NOT NULL",
    "base_model": "TEXT NOT NULL",
    "model_id": "TEXT",
    "profile_name": "TEXT",
    "launch_command": "TEXT NOT NULL",
    "env_json": "TEXT DEFAULT '{}'",
    "gguf_path": "TEXT",
    "mmproj_file": "TEXT",
    "draft_model": "TEXT",
    "spec_type": "TEXT",
    "port": "INTEGER",
    "backend": "TEXT",
    "gpu_layers": "TEXT",
    "n_ctx": "INTEGER",
    "n_batch": "INTEGER",
    "n_ubatch": "INTEGER",
    "n_threads": "INTEGER",
    "n_parallel": "INTEGER",
    "mmap": "INTEGER",
    "load_mode": "TEXT",
    "flash_attention": "TEXT",
    "cache_type_k": "TEXT",
    "cache_type_v": "TEXT",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "draft_tokens": "INTEGER",
    "draft_acc": "REAL",
    "prompt_per_second": "REAL",
    "tokens_per_second": "REAL",
    "prefill_high_tps": "REAL",  # the fastest and slowest stretch of the prompt, between progress reports
    "prefill_low_tps": "REAL",
    "prompt_ms": "REAL",
    "eval_ms": "REAL",
    "duration_ms": "INTEGER",
    "load_ms": "REAL",
    "layers_offloaded": "TEXT",
    "gpu_util": "REAL",
    "mem_used_gb": "REAL",
    "temp_c": "REAL",
    "note": "TEXT",
    "log_source": "TEXT",
    "success": "INTEGER DEFAULT 1",
}

PROFILE_COLUMNS: dict[str, str] = {
    "profile_id": "TEXT NOT NULL UNIQUE",
    "name": "TEXT NOT NULL",
    "base_model": "TEXT",
    "model_id": "TEXT",
    "source": "TEXT DEFAULT ''",
    "launch_command": "TEXT NOT NULL",
    "env_json": "TEXT DEFAULT '{}'",
    "notes": "TEXT DEFAULT ''",
    "favorite": "INTEGER DEFAULT 0",
    "last_used_at": "REAL",  # last launched or made a model's default: the Profiles page's Recently used
    "created_at": "REAL NOT NULL",
    "updated_at": "REAL NOT NULL",
}


def _cols(spec: dict[str, str]) -> str:
    return ",\n    ".join(f"{k} {v}" for k, v in spec.items())


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {_cols(RUN_COLUMNS)}
);
CREATE INDEX IF NOT EXISTS idx_runs_base ON runs (base_model);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {_cols(PROFILE_COLUMNS)}
);

CREATE TABLE IF NOT EXISTS catalog_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('mmproj', 'draft')),
    base_model TEXT NOT NULL,
    name TEXT NOT NULL,
    hf_repo TEXT DEFAULT '',
    file_path TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS env_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag TEXT NOT NULL UNIQUE,
    category TEXT,
    description TEXT,
    recommended TEXT,
    gpu_target TEXT,
    last_used_for TEXT,
    last_tokens_per_second REAL,
    last_prompt_per_second REAL,
    last_run_at REAL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);

CREATE TABLE IF NOT EXISTS gpu_samples (
    ts REAL NOT NULL, gpu INTEGER NOT NULL, name TEXT, util REAL, mem_used_mb REAL,
    mem_total_mb REAL, temp_c REAL, power_w REAL, source TEXT
);
CREATE INDEX IF NOT EXISTS idx_gpu_samples_ts ON gpu_samples (ts);

CREATE TABLE IF NOT EXISTS cpu_samples (
    ts REAL NOT NULL, util REAL, cores INTEGER, mem_used_mb REAL, mem_total_mb REAL, swap_used_mb REAL, load1 REAL
);
CREATE INDEX IF NOT EXISTS idx_cpu_samples_ts ON cpu_samples (ts);

CREATE TABLE IF NOT EXISTS gpu_hourly (
    hour REAL NOT NULL, gpu INTEGER NOT NULL, name TEXT, samples INTEGER,
    util_avg REAL, util_max REAL, mem_avg_mb REAL, mem_max_mb REAL, mem_total_mb REAL,
    temp_avg REAL, temp_max REAL, power_avg REAL, power_max REAL,
    PRIMARY KEY (hour, gpu)
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER, ts REAL NOT NULL, model TEXT, src TEXT, path TEXT, status INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_tokens INTEGER, draft_tokens INTEGER,
    draft_acc_tokens INTEGER, prompt_per_second REAL, tokens_per_second REAL,
    duration_ms INTEGER, error TEXT, prefill_high_tps REAL, prefill_low_tps REAL,
    UNIQUE (source_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests (ts DESC);

CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, files TEXT NOT NULL, dest TEXT NOT NULL,
    status TEXT NOT NULL, bytes_done INTEGER DEFAULT 0, bytes_total INTEGER DEFAULT 0,
    error TEXT DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS models_seen (
    model_id TEXT PRIMARY KEY, name TEXT, path TEXT, size_bytes INTEGER, quant TEXT, ftype INTEGER,
    arch TEXT, kind TEXT, origin TEXT, present INTEGER DEFAULT 1, first_seen REAL NOT NULL,
    last_seen REAL NOT NULL, extra TEXT DEFAULT '{{}}'
);

CREATE TABLE IF NOT EXISTS traits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, value TEXT NOT NULL,
    canonical TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new', note TEXT DEFAULT '',
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, count INTEGER DEFAULT 0, example TEXT DEFAULT '',
    UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS model_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, last_seen REAL NOT NULL,
    open INTEGER NOT NULL DEFAULT 1, observed_running INTEGER DEFAULT 0, source TEXT DEFAULT '',
    gpus TEXT DEFAULT '[]', size_gib REAL, measured_gib REAL, load_seconds REAL,
    quant TEXT, ftype INTEGER, arch TEXT, ctx INTEGER, cache_k TEXT, cache_v TEXT, split_mode TEXT,
    spec_type TEXT, load_mode TEXT, ngl TEXT, parallel INTEGER, mmproj INTEGER DEFAULT 0,
    flags TEXT DEFAULT '{{}}', env TEXT DEFAULT '{{}}', command TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_model ON model_sessions (model_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON model_sessions (open);

CREATE TABLE IF NOT EXISTS host_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, view TEXT NOT NULL DEFAULT '*', title TEXT DEFAULT '', body TEXT DEFAULT '',
    color TEXT DEFAULT 'yellow', created_at REAL NOT NULL, updated_at REAL NOT NULL, deleted INTEGER DEFAULT 0,
    x REAL, y REAL, w REAL, h REAL, collapsed INTEGER DEFAULT 0, is_open INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS memory_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL,
    text TEXT NOT NULL, hash TEXT NOT NULL, model TEXT DEFAULT '', dims INTEGER DEFAULT 0, vector BLOB,
    indexed_at REAL, UNIQUE (source, key)
);

CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'ssh',
    host TEXT DEFAULT '', port INTEGER DEFAULT 22, env_prefix TEXT DEFAULT '', llama_server TEXT DEFAULT '',
    gpu TEXT DEFAULT 'auto', model_dirs TEXT DEFAULT '[]', is_default INTEGER DEFAULT 0, source TEXT DEFAULT 'ui',
    status TEXT DEFAULT 'unknown', latency_ms REAL, last_check REAL, last_error TEXT DEFAULT '',
    engines TEXT DEFAULT '{{}}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
    env_kind TEXT DEFAULT 'none', env_value TEXT DEFAULT '', color TEXT DEFAULT '', download_dir TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    compacted INTEGER DEFAULT 0
);
"""

PROFILE_FIELDS = ("name", "base_model", "model_id", "source", "launch_command", "notes")
GPU_FIELDS = ("ts", "gpu", "name", "util", "mem_used_mb", "mem_total_mb", "temp_c", "power_w", "source")
REQUEST_FIELDS = ("source_id", "ts", "model", "src", "path", "status", "input_tokens", "output_tokens",
                  "cache_tokens", "draft_tokens", "draft_acc_tokens", "prompt_per_second",
                  "tokens_per_second", "duration_ms", "error", "prefill_high_tps", "prefill_low_tps")

SPEED_FIELDS = ("prompt_per_second", "tokens_per_second")


def _speed(v):
    """The engine reports -1 when a request had no such phase (an embedding has no decode): no speed, not a negative one."""
    return v if isinstance(v, (int, float)) and v > 0 else None


def new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{os.urandom(2).hex()}"


class Store:
    def __init__(self, path: str):
        self.path = path
        # Postgres when asked for, SQLite otherwise. Read once: a connection that
        # changed dialect mid-run would be worse than either.
        self.dsn = dialect.url()
        self.pg = dialect.is_postgres(self.dsn)
        if not self.pg and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()  # the one-time setup only; never held across a query
        self._open: list = []  # every connection made, so close() can reach them all
        self._settings_cache: Optional[dict[str, Any]] = None
        self._local = threading.local()
        with self._lock:
            self._migrate_legacy()
            self._conn.executescript(SCHEMA)
            self._add_missing_columns("runs", RUN_COLUMNS)
            self._add_missing_columns("profiles", PROFILE_COLUMNS)
            self._add_missing_columns("requests", {"prefill_high_tps": "REAL", "prefill_low_tps": "REAL"})
            self._add_missing_columns("servers", {"env_kind": "TEXT DEFAULT 'none'", "env_value": "TEXT DEFAULT ''",
                                                 "color": "TEXT DEFAULT ''", "download_dir": "TEXT DEFAULT ''"})
            self._add_missing_columns("notes", {"x": "REAL", "y": "REAL", "w": "REAL", "h": "REAL", "collapsed": "INTEGER DEFAULT 0"})
            self._migrate_notes()
            self._conn.commit()
        self._warn_if_empty_postgres()

    def _warn_if_empty_postgres(self) -> None:
        """Postgres with nothing in it, next to a SQLite file that has something.

        That combination is almost always a deployment that was pointed at
        Postgres without the data being moved across. It does not fail - a
        genuinely new install looks the same - but it should not be silent: the
        app comes up fast and empty, which reads as "working" until you notice
        the profiles are gone.
        """
        if not self.pg:
            return
        try:
            rows = sum(int(self._q("SELECT COUNT(*) AS n FROM " + t)[0]["n"])
                       for t in ("profiles", "runs", "settings"))
            if rows:
                return
            sibling = os.environ.get("ARGOS_BUILDER_DB") or os.path.join(
                os.path.dirname(os.path.abspath(self.path)) or "/data", "builder.db")
            if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
                log.warning(
                    "running on Postgres, which is empty, but %s exists (%d bytes). "
                    "This deployment is showing no data. Run scripts/migrate_to_postgres.py "
                    "to move it across, or unset ARGOS_DATABASE_URL to go back to the file.",
                    sibling, os.path.getsize(sibling))
        except Exception:
            pass

    def _migrate_notes(self) -> None:
        """Notes used to belong to a page, keep a history and be hidden on delete. Now there is one set of notes,
        open ones show on every page, minimized ones wait in the Notes menu, and a delete is final."""
        if "is_open" not in self._columns("notes"):
            self._conn.execute("ALTER TABLE notes ADD COLUMN is_open INTEGER DEFAULT 1")
            self._conn.execute("UPDATE notes SET is_open = CASE WHEN collapsed=1 THEN 0 ELSE 1 END")
        self._conn.execute("DELETE FROM notes WHERE deleted=1")
        self._conn.execute("DROP TABLE IF EXISTS note_versions")

    def _connect(self):
        """A connection for the calling thread.

        Postgres first: a pool would be better still, but a connection per thread
        is what the store already assumed, and psycopg's own connections are
        cheap enough at this size.

        One shared connection with a lock around it means a single slow call stops every
        other request: a query waited 6.5 hours for that lock on a container that had
        been up for ten. SQLite is happy with a connection per thread, and in WAL readers
        do not block a writer at all - so a call that gets stuck blocks its own request
        and nothing else.

        Note what is deliberately absent: locking_mode=EXCLUSIVE. It buys a little on a
        slow filesystem by holding the file's lock for good, on the assumption that only
        this process opens it. That assumption is false - a second connection, a backup,
        an admin sqlite3, a second instance - and the price is that everything else waits
        forever instead of for the busy timeout.
        """
        if self.pg:
            conn = dialect.connect(self.dsn)
            with self._lock:
                self._open.append(conn)
            return conn
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # ARGOS_SQLITE_JOURNAL forces the journal when the guess is wrong (a bind mount can
        # hide what the filesystem underneath really is). Whitelisted, never raw.
        forced = (os.environ.get("ARGOS_SQLITE_JOURNAL") or "").strip().upper()
        mode = forced if forced in ("WAL", "TRUNCATE", "DELETE", "PERSIST") else self._journal_for(os.path.abspath(self.path))
        # First, so that even choosing the journal waits out an old process still shutting down.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(f"PRAGMA journal_mode={mode}")
        if mode == "WAL":
            # A power cut can lose the last commits but never corrupts, and this is the
            # fsync per commit that every settings and telemetry write otherwise waits for.
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-65536")  # up to 64 MiB of pages stay resident
        conn.execute("PRAGMA mmap_size=268435456")  # reads come from the mapping, not a read() call each
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            # A connection per thread, and many threads are short-lived (a launch, a warm-up, a
            # refresh). Nothing closed theirs, so on Postgres they piled up until the server
            # refused new clients ("too many clients already"). Close it when the thread goes.
            weakref.finalize(threading.current_thread(), self._release, conn)
        return conn

    def _release(self, conn) -> None:
        """A thread that had a connection has ended: close it and forget it."""
        with self._lock:
            if conn in self._open:
                self._open.remove(conn)
        try:
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _journal_for(path: str) -> str:
        """The journal this database can actually rely on, given where it lives.

        WAL is faster and lets readers run while a writer works, but it coordinates
        through a -shm file and mmap. On a FUSE filesystem - Unraid's /mnt/user is shfs
        - that is what fails: the -wal stops checkpointing and grows, and reads block.
        TRUNCATE needs only ordinary file locking, which those filesystems do honour,
        at the cost of a writer excluding readers.

        So: WAL on a real filesystem, TRUNCATE on one that is not. Best effort - if the
        mounts cannot be read, WAL is the better guess for a normal disk.
        """
        best, kind = "", ""
        try:
            with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mount, fstype = parts[1], parts[2]
                    if mount == path or path.startswith(mount.rstrip("/") + "/"):
                        if len(mount) > len(best):
                            best, kind = mount, fstype.lower()
        except OSError:
            return "WAL"
        if any(word in kind for word in ("fuse", "nfs", "cifs", "smb")):
            return "TRUNCATE"
        return "WAL"

    def _abort(self) -> None:
        """Clear a failed statement.

        Postgres refuses every later statement on a connection until the failed
        one is rolled back - one bad write would otherwise poison the thread's
        connection for good. SQLite has no such state and does not mind.
        """
        if self.pg:
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _q(self, sql: str, args: Iterable = ()) -> list[dict]:
        # No lock, and this thread's own connection: a slow call holds up its own request
        # and nothing else. A shared connection behind one lock is what let a single query
        # wait six hours while the container answered nothing.
        asked = time.monotonic()
        try:
            rows = [dict(r) for r in self._conn.execute(sql, tuple(args)).fetchall()]
        except Exception:
            self._abort()
            raise
        done = time.monotonic()
        if done - asked > SLOW_DB_S:
            log.warning("slow query: ran %.1fs, %d rows: %s", done - asked, len(rows), " ".join(sql.split())[:90])
        return rows

    def _insert(self, sql: str, args: Iterable = (), commit: bool = True) -> int:
        """An INSERT that wants the id it just made.

        SQLite hands it back as ``lastrowid``; Postgres has no rowid and answers
        with RETURNING. Both are read here so the callers do not have to know
        which database they are on. Every table these write to has an id column.
        """
        if self.pg:
            cur = self._x(sql.rstrip().rstrip(";") + " RETURNING id", args, commit=commit)
            row = cur.fetchone()
            return int(row["id"]) if row else 0
        cur = self._x(sql, args, commit=commit)
        return int(cur.lastrowid or 0)

    def _x(self, sql: str, args: Iterable = (), commit: bool = True) -> sqlite3.Cursor:
        # No lock, same reason as _q: SQLite serializes writers itself, with the busy timeout.
        try:
            cur = self._conn.execute(sql, tuple(args))
        except Exception:
            self._abort()
            raise
        if commit:
            self._conn.commit()
        return cur

    def close(self) -> None:
        """Close every connection this store opened.

        SQLite takes its connections down with the process, so this never
        mattered. Postgres does not: a connection left open keeps its transaction
        and the locks in it, and anything that needs those objects - a schema
        drop, a migration, an ALTER - waits indefinitely. The suite found this
        as a teardown that never returned.
        """
        with self._lock:
            conns, self._open = self._open, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local = threading.local()

    def commit(self) -> None:
        """Finish a run of writes started with ``commit=False``. A pull writes dozens
        of rows at once, and on a slow filesystem the per-row commit is the whole cost."""
        self._conn.commit()

    # ----- migrations -----------------------------------------------------
    def _tables(self) -> dict[str, str]:
        return dialect.tables(self._conn, self.pg)

    def _columns(self, table: str) -> set[str]:
        return dialect.columns(self._conn, table, self.pg)

    def _migrate_legacy(self) -> None:
        tables = self._tables()
        if "runs" in tables:
            cols = self._columns("runs")
            for old, new in (("mmjog_file", "mmproj_file"), ("custom_env_json", "env_json"),
                             ("gpu_util_real", "gpu_util")):
                if old in cols and new not in cols:
                    self._conn.execute(f"ALTER TABLE runs RENAME COLUMN {old} TO {new}")
        if "profiles" in tables and "copied_into" in self._columns("profiles"):
            self._conn.execute("ALTER TABLE profiles RENAME TO profiles_legacy")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT INTO profiles (profile_id, name, base_model, source, launch_command, "
                "env_json, created_at, updated_at) SELECT profile_id, name, copied_into, "
                "source_model, launch_command, env_json, created_at, updated_at FROM profiles_legacy")
            self._conn.execute("DROP TABLE profiles_legacy")
        # catalog: v1 had two tables, v2 a CHECK allowing 'mtp' (not a llama.cpp file
        # type -- MTP is --spec-type draft-mtp); both become 'draft' rows now.
        if "catalog_files" in tables and "'draft'" not in tables["catalog_files"]:
            self._conn.execute("ALTER TABLE catalog_files RENAME TO catalog_v2")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT INTO catalog_files (kind, base_model, name, hf_repo, file_path, notes, created_at) "
                "SELECT CASE kind WHEN 'mtp' THEN 'draft' ELSE kind END, base_model, name, hf_repo, "
                "file_path, notes, created_at FROM catalog_v2")
            self._conn.execute("DROP TABLE catalog_v2")
        legacy_catalog = (("mtp_models", "draft"), ("vision_mmjogs", "mmproj"))
        if any(t in tables for t, _ in legacy_catalog):
            self._conn.executescript(SCHEMA)
            for table, kind in legacy_catalog:
                if table in tables:
                    self._conn.execute(
                        "INSERT INTO catalog_files (kind, base_model, name, hf_repo, file_path, notes, "
                        f"created_at) SELECT '{kind}', COALESCE(base_model, model_id), name, "
                        f"COALESCE(hf_repo, ''), COALESCE(file_path, ''), COALESCE(notes, ''), "
                        f"created_at FROM {table}")
                    self._conn.execute(f"DROP TABLE {table}")

    def _add_missing_columns(self, table: str, spec: dict[str, str]) -> None:
        have = self._columns(table)
        for name, decl in spec.items():
            if name not in have:
                decl = decl.replace("NOT NULL UNIQUE", "").replace("NOT NULL", "")
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ----- runs -----------------------------------------------------------
    def add_run(self, rec: dict[str, Any]) -> None:
        """Insert or replace (by run_id) a perf-sheet row; unknown keys are ignored."""
        row = {k: v for k, v in rec.items() if k in RUN_COLUMNS}
        if isinstance(row.get("env_json"), dict):
            row["env_json"] = json.dumps(row["env_json"])
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "run_id")
        self._x(f"INSERT INTO runs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))}) "
                f"ON CONFLICT(run_id) DO UPDATE SET {updates}", row.values())

    def get_run(self, run_id: str) -> Optional[dict]:
        rows = self._q("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return _decode(rows[0]) if rows else None

    def list_runs(self, base_model: Optional[str] = None, limit: int = 500) -> list[dict]:
        q, args = "SELECT * FROM runs", []
        if base_model:
            q, args = q + " WHERE base_model=?", [base_model]
        return [_decode(r) for r in self._q(q + " ORDER BY created_at DESC LIMIT ?", args + [limit])]

    def delete_run(self, run_id: str) -> bool:
        return self._x("DELETE FROM runs WHERE run_id=?", (run_id,)).rowcount > 0

    def count_runs(self) -> int:
        """How many runs there are, without reading them: the sidebar wants the number
        and nothing else."""
        rows = self._q("SELECT COUNT(*) AS n FROM runs")
        return int(rows[0]["n"]) if rows else 0

    def base_models(self) -> list[str]:
        q = ("SELECT base_model FROM runs UNION SELECT base_model FROM profiles "
             "UNION SELECT base_model FROM catalog_files")
        return sorted({r["base_model"] for r in self._q(q) if r["base_model"]}, key=str.lower)

    # ----- profiles -------------------------------------------------------
    def save_profile(self, d: dict[str, Any], commit: bool = True) -> dict:
        """Create, or update when ``profile_id`` matches an existing profile. No limit.

        ``commit=False`` leaves the write in the open transaction so a caller saving
        many rows pays for one commit instead of one each.
        """
        pid = d.get("profile_id") or new_id("prof")
        vals = {k: str(d.get(k) or "").strip() for k in PROFILE_FIELDS}
        now = time.time()
        if "favorite" not in d:  # a save from the Builder does not say: the star stays as it was
            old = self.get_profile(pid) if d.get("profile_id") else None
            d = {**d, "favorite": bool(old and old.get("favorite"))}
        self._x(
            f"INSERT INTO profiles (profile_id, {', '.join(PROFILE_FIELDS)}, favorite, created_at, updated_at) "
            f"VALUES (?, {', '.join('?' * len(PROFILE_FIELDS))}, ?, ?, ?) ON CONFLICT(profile_id) DO UPDATE SET "
            f"{', '.join(f'{k}=excluded.{k}' for k in PROFILE_FIELDS)}, favorite=excluded.favorite, "
            f"updated_at=excluded.updated_at",
            (pid, *vals.values(), 1 if d.get("favorite") else 0, now, now), commit=commit)
        return self.get_profile(pid)  # type: ignore[return-value]

    def get_profile(self, profile_id: str) -> Optional[dict]:
        rows = self._q("SELECT * FROM profiles WHERE profile_id=?", (profile_id,))
        return _decode(rows[0]) if rows else None

    def list_profiles(self, model_id: Optional[str] = None) -> list[dict]:
        q, args = "SELECT * FROM profiles", []
        if model_id:
            q, args = q + " WHERE model_id=?", [model_id]
        return [_decode(r) for r in self._q(q + " ORDER BY favorite DESC, updated_at DESC", args)]

    def set_profile_favorite(self, profile_id: str, on: bool) -> bool:
        """Star or unstar a profile. Not an edit: ``updated_at`` stays."""
        return self._x("UPDATE profiles SET favorite=? WHERE profile_id=?", (1 if on else 0, profile_id)).rowcount > 0

    def touch_profile(self, profile_id: str, at: Optional[float] = None) -> None:
        """The profile was just used (launched, or made its model's default)."""
        self._x("UPDATE profiles SET last_used_at=? WHERE profile_id=?", (at or time.time(), profile_id))

    def delete_profile(self, profile_id: str) -> bool:
        return self._x("DELETE FROM profiles WHERE profile_id=?", (profile_id,)).rowcount > 0

    # ----- catalog (extra mmproj / draft files) ---------------------------
    def add_file(self, kind: str, base_model: str, *, name: str = "", hf_repo: str = "",
                 file_path: str = "", notes: str = "") -> int:
        name = name or os.path.basename((file_path or hf_repo).rstrip("/")) or kind
        return self._insert("INSERT INTO catalog_files (kind, base_model, name, hf_repo, file_path, notes, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (kind, base_model, name, hf_repo, file_path, notes, time.time()))

    def list_files(self, kind: Optional[str] = None, base_model: Optional[str] = None) -> list[dict]:
        where, args = [], []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if base_model:
            where.append("base_model=?")
            args.append(base_model)
        q = "SELECT * FROM catalog_files" + (" WHERE " + " AND ".join(where) if where else "")
        return self._q(q + " ORDER BY base_model, created_at DESC", args)

    def delete_file(self, file_id: int) -> bool:
        return self._x("DELETE FROM catalog_files WHERE id=?", (file_id,)).rowcount > 0

    # ----- env-var usage --------------------------------------------------
    def list_env_flags(self) -> list[dict]:
        return self._q("SELECT * FROM env_flags ORDER BY flag")

    def set_last_used(self, flag: str, base_model: str, tps: Optional[float],
                      pps: Optional[float], run_at: float) -> None:
        self._x("INSERT INTO env_flags (flag, last_used_for, last_tokens_per_second, "
                "last_prompt_per_second, last_run_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(flag) DO UPDATE SET last_used_for=excluded.last_used_for, "
                "last_tokens_per_second=excluded.last_tokens_per_second, "
                "last_prompt_per_second=excluded.last_prompt_per_second, last_run_at=excluded.last_run_at",
                (flag, base_model, tps, pps, run_at))

    # ----- settings -------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        """Almost every request reads the settings, several times, and each read queued for the one database
        lock behind everything else. Only set_setting writes the table, so the parsed rows are kept until then."""
        cached = self._settings_cache
        if cached is None:
            cached = {r["key"]: _json(r["value"], None) for r in self._q("SELECT key, value FROM settings")}
            self._settings_cache = cached
        return copy.deepcopy(cached)

    def set_setting(self, key: str, value: Any) -> None:
        self._settings_cache = None
        if value is None:
            self._x("DELETE FROM settings WHERE key=?", (key,))
        else:
            self._x("INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # ----- inventory: models, traits, sessions, host -----------------------------------
    def upsert_model_seen(self, rec: dict[str, Any], now: Optional[float] = None) -> bool:
        """Record a model; returns True when it had never been seen. first_seen is kept."""
        now = now or time.time()
        with self._lock:
            row = self._conn.execute("SELECT present FROM models_seen WHERE model_id=?", (rec["model_id"],)).fetchone()
            fields = {k: rec.get(k) for k in ("name", "path", "size_bytes", "quant", "ftype", "arch", "kind", "origin")}
            extra = json.dumps(rec.get("extra") or {})
            if row is None:
                self._conn.execute(
                    "INSERT INTO models_seen (model_id, name, path, size_bytes, quant, ftype, arch, kind, origin, present, "
                    "first_seen, last_seen, extra) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)",
                    (rec["model_id"], *fields.values(), now, now, extra))
            else:
                self._conn.execute(
                    "UPDATE models_seen SET name=?, path=?, size_bytes=?, quant=?, ftype=?, arch=?, kind=?, origin=?, "
                    "present=1, last_seen=?, extra=? WHERE model_id=?",
                    (*fields.values(), now, extra, rec["model_id"]))
            self._conn.commit()
            return row is None

    def list_models_seen(self) -> list[dict]:
        # lower(name), not COLLATE NOCASE: Postgres has no such collation, and
        # this sorts the same on both.
        rows = self._q("SELECT * FROM models_seen ORDER BY lower(name)")
        for r in rows:
            r["extra"] = _json(r.get("extra"), {})
        return rows

    def mark_models_absent(self, origin: str, present_ids: Iterable[str]) -> list[str]:
        """Models of this origin that were present and are not any more. Returns their ids."""
        present = set(present_ids)
        gone = [r["model_id"] for r in self._q("SELECT model_id FROM models_seen WHERE origin=? AND present=1", (origin,))
                if r["model_id"] not in present]
        for mid in gone:
            self._x("UPDATE models_seen SET present=0 WHERE model_id=?", (mid,))
        return gone

    def upsert_trait(self, kind: str, value: str, example: str = "", status: str = "new",
                     now: Optional[float] = None) -> tuple[dict, bool]:
        """Count one sighting of ``value``. Never raises on odd input. Returns (row, created)."""
        now = now or time.time()
        kind, value = str(kind)[:40], str(value)[:200]
        with self._lock:
            row = self._conn.execute("SELECT * FROM traits WHERE kind=? AND value=?", (kind, value)).fetchone()
            if row is None:
                tid = self._insert(
                    "INSERT INTO traits (kind, value, status, first_seen, last_seen, count, example) VALUES (?,?,?,?,?,1,?)",
                    (kind, value, status, now, now, example[:300]))
                return dict(self._conn.execute("SELECT * FROM traits WHERE id=?", (tid,)).fetchone()), True
            self._conn.execute("UPDATE traits SET count=count+1, last_seen=? WHERE id=?", (now, row["id"]))
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM traits WHERE id=?", (row["id"],)).fetchone()), False

    def list_traits(self, kind: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        where, args = [], []
        if kind:
            where.append("kind=?"); args.append(kind)
        if status:
            where.append("status=?"); args.append(status)
        sql = "SELECT * FROM traits" + (" WHERE " + " AND ".join(where) if where else "")
        return self._q(sql + " ORDER BY kind, count DESC, value", args)

    def get_trait(self, trait_id: int) -> Optional[dict]:
        rows = self._q("SELECT * FROM traits WHERE id=?", (trait_id,))
        return rows[0] if rows else None

    def update_trait(self, trait_id: int, **fields: Any) -> Optional[dict]:
        allowed = {k: v for k, v in fields.items() if k in ("status", "canonical", "note")}
        if allowed:
            self._x(f"UPDATE traits SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?", (*allowed.values(), trait_id))
        return self.get_trait(trait_id)

    SESSION_FIELDS = ("model_id", "started_at", "last_seen", "observed_running", "source", "gpus", "size_gib", "measured_gib",
                      "load_seconds", "quant", "ftype", "arch", "ctx", "cache_k", "cache_v", "split_mode", "spec_type",
                      "load_mode", "ngl", "parallel", "mmproj", "flags", "env", "command")

    def open_session(self, rec: dict[str, Any]) -> int:
        vals = {k: rec.get(k) for k in self.SESSION_FIELDS}
        for k in ("gpus", "flags", "env"):
            vals[k] = json.dumps(vals[k] if vals[k] is not None else ([] if k == "gpus" else {}))
        cols = ", ".join(vals)
        return self._insert(f"INSERT INTO model_sessions ({cols}) VALUES ({', '.join('?' for _ in vals)})",
                            tuple(vals.values()))

    def update_session(self, session_id: int, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in self.SESSION_FIELDS or k in ("ended_at", "open")}
        for k in ("gpus", "flags", "env"):
            if k in allowed:
                allowed[k] = json.dumps(allowed[k])
        if allowed:
            self._x(f"UPDATE model_sessions SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?",
                    (*allowed.values(), session_id))

    def open_session_starts(self) -> dict[str, float]:
        """model id -> when its open session started: two small columns, not the commands and flags of every row.
        /api/launches asks every second or two, and the full rows made each answer wait seconds on the database."""
        rows = self._q("SELECT model_id, started_at FROM model_sessions WHERE open=1 ORDER BY started_at DESC")
        return {r["model_id"]: r["started_at"] for r in rows}  # the oldest start wins, as before

    def list_sessions(self, model: Optional[str] = None, since: Optional[float] = None,
                      open_only: bool = False, limit: int = 500) -> list[dict]:
        where, args = [], []
        if model:
            where.append("model_id=?"); args.append(model)
        if since:
            where.append("(started_at>=? OR ended_at IS NULL OR ended_at>=?)"); args += [since, since]
        if open_only:
            where.append("open=1")
        sql = "SELECT * FROM model_sessions" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self._q(sql + " ORDER BY started_at DESC LIMIT ?", (*args, limit))
        for r in rows:
            r["gpus"], r["flags"], r["env"] = _json(r["gpus"], []), _json(r["flags"], {}), _json(r["env"], {})
        return rows

    def renumber_session_gpus(self, card_count: int) -> int:
        """Fix loads recorded with a GPU number that does not exist, and return how many changed.

        Before the engine numbered its cards from 0 it used the DRM card numbers, which on a host whose GPUs
        are card1 and card2 recorded "1,2" for what HIP calls 0 and 1. A load that names a card past the last
        one is shifted down by the gap. A load that only ever named valid numbers cannot be told apart and is
        left as it is.
        """
        if card_count <= 0:
            return 0
        changed = 0
        for r in self._q("SELECT id, gpus FROM model_sessions"):
            ids = [int(g) for g in _json(r["gpus"], []) if str(g).isdigit()]
            if not ids or max(ids) < card_count:
                continue
            shift = max(ids) - (card_count - 1)
            fixed = sorted({str(i - shift) for i in ids if i - shift >= 0}, key=int)
            self._x("UPDATE model_sessions SET gpus=? WHERE id=?", (json.dumps(fixed), r["id"]))
            changed += 1
        return changed

    def count_sessions(self, open_only: bool = False) -> int:
        return self._q("SELECT COUNT(*) AS n FROM model_sessions" + (" WHERE open=1" if open_only else ""))[0]["n"]

    def add_host_snapshot(self, source: str, data: dict, digest: str, ts: Optional[float] = None) -> bool:
        """Keep a snapshot only when it differs from the last one from this source."""
        last = self._q("SELECT digest FROM host_snapshots WHERE source=? ORDER BY ts DESC LIMIT 1", (source,))
        if last and last[0]["digest"] == digest:
            return False
        self._x("INSERT INTO host_snapshots (ts, source, digest, data) VALUES (?,?,?,?)",
                (ts or time.time(), source, digest, json.dumps(data)))
        return True

    def list_host_snapshots(self, limit: int = 20) -> list[dict]:
        rows = self._q("SELECT * FROM host_snapshots ORDER BY ts DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = _json(r["data"], {})
        return rows

    # ----- notes ----------------------------------------------------------
    NOTE_COLORS = ("yellow", "blue", "green", "pink", "gray")
    NOTE_FIELDS = ("id", "title", "body", "color", "x", "y", "w", "h", "is_open", "created_at", "updated_at")

    def _note(self, row: dict) -> dict:
        n = {k: row.get(k) for k in self.NOTE_FIELDS}
        n["is_open"] = bool(n["is_open"])
        return n

    def add_note(self, title: str = "", body: str = "", color: str = "yellow", is_open: bool = True) -> dict:
        """A new note. Notes are not tied to a page: an open note shows on every page."""
        now = time.time()
        nid = self._insert("INSERT INTO notes (view, title, body, color, is_open, created_at, updated_at) VALUES ('*',?,?,?,?,?,?)",
                           (title[:200], body[:20000], color if color in self.NOTE_COLORS else "yellow", int(bool(is_open)), now, now))
        return self.get_note(nid)

    def get_note(self, note_id: int) -> Optional[dict]:
        rows = self._q("SELECT * FROM notes WHERE id=?", (note_id,))
        return self._note(rows[0]) if rows else None

    def list_notes(self) -> list[dict]:
        """Every note, oldest first: the open ones float over the page, the rest wait in the Notes menu."""
        return [self._note(r) for r in self._q("SELECT * FROM notes ORDER BY created_at, id")]

    def update_note(self, note_id: int, **fields: Any) -> Optional[dict]:
        """Save any of title, body, color, x, y, w, h and is_open."""
        note = self.get_note(note_id)
        if not note:
            return None
        color = fields.get("color", note["color"])
        vals = {"title": str(fields.get("title", note["title"]) or "")[:200], "body": str(fields.get("body", note["body"]) or "")[:20000],
                "color": color if color in self.NOTE_COLORS else note["color"],
                "is_open": int(bool(fields.get("is_open", note["is_open"])))}
        for k in ("x", "y", "w", "h"):
            v = fields.get(k, note[k])
            vals[k] = None if v is None else max(0.0, min(float(v), 100000.0))
        self._x("UPDATE notes SET title=?, body=?, color=?, is_open=?, x=?, y=?, w=?, h=?, updated_at=? WHERE id=?",
                (vals["title"], vals["body"], vals["color"], vals["is_open"], vals["x"], vals["y"], vals["w"], vals["h"], time.time(), note_id))
        return self.get_note(note_id)

    def delete_note(self, note_id: int) -> bool:
        """Gone for good. Minimize a note instead to keep it in the Notes menu."""
        return self._x("DELETE FROM notes WHERE id=?", (note_id,)).rowcount > 0

    # ----- servers --------------------------------------------------------
    SERVER_FIELDS = ("name", "kind", "host", "port", "env_prefix", "env_kind", "env_value", "llama_server", "gpu", "model_dirs",
                     "download_dir", "color", "source")

    def _server(self, row: dict) -> dict:
        row["model_dirs"] = _json(row.get("model_dirs"), [])
        row["engines"] = _json(row.get("engines"), {})
        row["is_default"] = bool(row.get("is_default"))
        return row

    def list_servers(self) -> list[dict]:
        return [self._server(r) for r in self._q("SELECT * FROM servers ORDER BY (kind='local') DESC, id")]

    def get_server(self, server_id: int) -> Optional[dict]:
        rows = self._q("SELECT * FROM servers WHERE id=?", (server_id,))
        return self._server(rows[0]) if rows else None

    def server_by_name(self, name: str) -> Optional[dict]:
        rows = self._q("SELECT * FROM servers WHERE name=?", (name,))
        return self._server(rows[0]) if rows else None

    def save_server(self, d: dict[str, Any], server_id: Optional[int] = None) -> dict:
        """Insert (or update ``server_id``). ``d`` holds only SERVER_FIELDS; the first server saved becomes the default."""
        now = time.time()
        vals = {k: d[k] for k in self.SERVER_FIELDS if k in d}
        if "model_dirs" in vals:
            vals["model_dirs"] = json.dumps(vals["model_dirs"])
        if server_id is None:
            vals.setdefault("name", "")
            cols = list(vals) + ["created_at", "updated_at"]
            self._x(f"INSERT INTO servers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [*vals.values(), now, now])
            server_id = self._q("SELECT id FROM servers WHERE name=?", (vals["name"],))[0]["id"]
            if not self._q("SELECT 1 FROM servers WHERE is_default=1"):
                self._x("UPDATE servers SET is_default=1 WHERE id=?", (server_id,))
        elif vals:
            sets = ", ".join(f"{k}=?" for k in vals)
            self._x(f"UPDATE servers SET {sets}, updated_at=? WHERE id=?", [*vals.values(), now, server_id])
        return self.get_server(server_id)  # type: ignore[return-value]

    def set_server_check(self, server_id: int, status: str, latency_ms: Optional[float], error: str, engines: dict) -> None:
        self._x("UPDATE servers SET status=?, latency_ms=?, last_check=?, last_error=?, engines=? WHERE id=?",
                (status, latency_ms, time.time(), error[:500], json.dumps(engines), server_id))

    def set_default_server(self, server_id: int) -> bool:
        if not self.get_server(server_id):
            return False
        with self._lock:
            self._conn.execute("UPDATE servers SET is_default=0")
            self._conn.execute("UPDATE servers SET is_default=1 WHERE id=?", (server_id,))
            self._conn.commit()
        return True

    def delete_server(self, server_id: int) -> bool:
        s = self.get_server(server_id)
        if not s or s["kind"] == "local":
            return False
        self._x("DELETE FROM servers WHERE id=?", (server_id,))
        if s["is_default"]:
            first = self._q("SELECT id FROM servers ORDER BY (kind='local') DESC, id LIMIT 1")
            if first:
                self.set_default_server(first[0]["id"])
        return True

    # ----- events ---------------------------------------------------------
    def add_event(self, kind: str, message: str, level: str = "info", data: Optional[dict] = None,
                  ts: Optional[float] = None) -> None:
        self._x("INSERT INTO events (ts, level, kind, message, data) VALUES (?, ?, ?, ?, ?)",
                (ts or time.time(), level, kind, message[:2000], json.dumps(data or {})))

    def list_events(self, kind: Optional[str] = None, level: Optional[str] = None,
                    since: Optional[float] = None, q: Optional[str] = None, limit: int = 300) -> list[dict]:
        where, args = [], []
        for col, val in (("kind", kind), ("level", level)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if since:
            where.append("ts>=?")
            args.append(since)
        if q:
            where.append("message LIKE ?")
            args.append(f"%{q}%")
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self._q(sql + " ORDER BY ts DESC, id DESC LIMIT ?", args + [limit])
        for r in rows:
            r["data"] = _json(r["data"], {})
        return rows

    # ----- GPU telemetry --------------------------------------------------
    def add_gpu_samples(self, samples: list[dict]) -> None:
        if not samples:
            return
        with self._lock:
            self._conn.executemany(
                f"INSERT INTO gpu_samples ({', '.join(GPU_FIELDS)}) VALUES ({', '.join('?' * len(GPU_FIELDS))})",
                [tuple(s.get(k) for k in GPU_FIELDS) for s in samples])
            self._conn.commit()

    def gpu_series(self, since: float, bucket: int) -> list[dict]:
        """Raw samples averaged into ``bucket``-second buckets, newer than ``since``."""
        return self._q(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS t, gpu, MAX(name) AS name, AVG(util) AS util, "
            "MAX(util) AS util_max, AVG(mem_used_mb) AS mem_used_mb, MAX(mem_total_mb) AS mem_total_mb, "
            "AVG(temp_c) AS temp_c, MAX(temp_c) AS temp_max, AVG(power_w) AS power_w, COUNT(*) AS n "
            "FROM gpu_samples WHERE ts>=? GROUP BY t, gpu ORDER BY t, gpu", (bucket, bucket, since))

    def gpu_summary(self, since: float) -> list[dict]:
        """Per-GPU aggregates over raw samples newer than ``since``."""
        return self._q(
            "SELECT gpu, MAX(name) AS name, COUNT(*) AS n, AVG(util) AS util_avg, MAX(util) AS util_max, "
            "AVG(mem_used_mb) AS mem_avg_mb, MAX(mem_used_mb) AS mem_max_mb, MAX(mem_total_mb) AS mem_total_mb, "
            "MAX(temp_c) AS temp_max, AVG(power_w) AS power_avg FROM gpu_samples WHERE ts>=? "
            "GROUP BY gpu ORDER BY gpu", (since,))

    def gpu_window(self, t0: float, t1: float) -> list[dict]:
        """Per-GPU aggregates between two times: raw samples if they are still kept, else the hourly rollups."""
        rows = self._q(
            "SELECT gpu, MAX(name) AS name, COUNT(*) AS n, AVG(util) AS util_avg, MAX(util) AS util_max, "
            "AVG(mem_used_mb) AS mem_avg_mb, MAX(mem_used_mb) AS mem_max_mb, MAX(mem_total_mb) AS mem_total_mb, "
            "MAX(temp_c) AS temp_max, AVG(power_w) AS power_avg FROM gpu_samples WHERE ts>=? AND ts<=? "
            "GROUP BY gpu ORDER BY gpu", (t0, t1))
        if rows:
            return rows
        return self._q(
            "SELECT gpu, MAX(name) AS name, SUM(samples) AS n, AVG(util_avg) AS util_avg, MAX(util_max) AS util_max, "
            "AVG(mem_avg_mb) AS mem_avg_mb, MAX(mem_max_mb) AS mem_max_mb, MAX(mem_total_mb) AS mem_total_mb, "
            "MAX(temp_max) AS temp_max, AVG(power_avg) AS power_avg FROM gpu_hourly WHERE hour>=? AND hour<=? "
            "GROUP BY gpu ORDER BY gpu", (t0 - 3600, t1))

    def gpu_latest(self) -> list[dict]:
        return self._q("SELECT * FROM gpu_samples WHERE ts=(SELECT MAX(ts) FROM gpu_samples) ORDER BY gpu")

    def gpu_rollup_series(self, since: float, bucket: int) -> list[dict]:
        """Hourly rollups regrouped into ``bucket``-second buckets (for ranges beyond raw retention)."""
        return self._q(
            "SELECT CAST(hour / ? AS INTEGER) * ? AS t, gpu, MAX(name) AS name, "
            "SUM(util_avg * samples) / SUM(samples) AS util, MAX(util_max) AS util_max, "
            "SUM(mem_avg_mb * samples) / SUM(samples) AS mem_used_mb, MAX(mem_total_mb) AS mem_total_mb, "
            "SUM(temp_avg * samples) / SUM(samples) AS temp_c, MAX(temp_max) AS temp_max, "
            "SUM(power_avg * samples) / SUM(samples) AS power_w, SUM(samples) AS n "
            "FROM gpu_hourly WHERE hour>=? GROUP BY t, gpu ORDER BY t, gpu", (bucket, bucket, since))

    def gpu_hourly(self, since: float) -> list[dict]:
        return self._q("SELECT * FROM gpu_hourly WHERE hour>=? ORDER BY hour, gpu", (since,))

    def prune_requests(self, before: float) -> None:
        """Delete the request history older than ``before``."""
        with self._lock:
            self._conn.execute("DELETE FROM requests WHERE ts < ?", (before,))
            self._conn.commit()

    # ----- CPU and RAM, beside the GPUs ------------------------------------
    def add_cpu_sample(self, s: dict) -> None:
        self._x("INSERT INTO cpu_samples (ts, util, cores, mem_used_mb, mem_total_mb, swap_used_mb, load1) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (s["ts"], s.get("util"), s.get("cores"), s.get("mem_used_mb"),
                                                 s.get("mem_total_mb"), s.get("swap_used_mb"), s.get("load1")))

    def cpu_series(self, since: float, bucket: int) -> list[dict]:
        return self._q(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS t, AVG(util) AS util, MAX(util) AS util_max, AVG(mem_used_mb) AS mem_used_mb, "
            "MAX(mem_total_mb) AS mem_total_mb, AVG(swap_used_mb) AS swap_used_mb, AVG(load1) AS load1, MAX(cores) AS cores "
            "FROM cpu_samples WHERE ts>=? GROUP BY t ORDER BY t", (bucket, bucket, since))

    def cpu_latest(self) -> Optional[dict]:
        rows = self._q("SELECT * FROM cpu_samples ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def prune_gpu(self, before: float) -> None:
        """Delete every GPU (and CPU) reading older than ``before``: the raw samples and the hourly rollups."""
        with self._lock:
            self._conn.execute("DELETE FROM gpu_samples WHERE ts < ?", (before,))
            self._conn.execute("DELETE FROM cpu_samples WHERE ts < ?", (before,))
            self._conn.execute("DELETE FROM gpu_hourly WHERE hour < ?", (before - 3600,))
            self._conn.commit()

    def rollup_gpu(self, now: Optional[float] = None, keep_raw_s: float = 14 * 86400) -> None:
        """Fold every finished hour of raw samples into gpu_hourly; drop raw beyond retention."""
        now = now or time.time()
        current_hour = int(now // 3600) * 3600
        with self._lock:
            # Only the hours not yet folded in: the last rolled hour is redone in case it was cut short, the
            # rest of the window is already in gpu_hourly. Re-aggregating every raw sample on every pass kept
            # the lock - which every page read waits for - for as long as the whole retention window took.
            # By name, not by position: sqlite3.Row answers row[0], psycopg's
            # dict_row answers row["h"] and only row["h"].
            last = self._conn.execute("SELECT MAX(hour) AS h FROM gpu_hourly").fetchone()["h"]
            floor = float(last) if last is not None else 0.0
            self._conn.execute(
                "INSERT INTO gpu_hourly (hour, gpu, name, samples, util_avg, util_max, mem_avg_mb, "
                "mem_max_mb, mem_total_mb, temp_avg, temp_max, power_avg, power_max) "
                "SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS h, gpu, MAX(name), COUNT(*), AVG(util), "
                "MAX(util), AVG(mem_used_mb), MAX(mem_used_mb), MAX(mem_total_mb), AVG(temp_c), "
                "MAX(temp_c), AVG(power_w), MAX(power_w) FROM gpu_samples WHERE ts >= ? AND ts < ? "
                "GROUP BY h, gpu ON CONFLICT(hour, gpu) DO UPDATE SET samples=excluded.samples, "
                "util_avg=excluded.util_avg, util_max=excluded.util_max, mem_avg_mb=excluded.mem_avg_mb, "
                "mem_max_mb=excluded.mem_max_mb, mem_total_mb=excluded.mem_total_mb, "
                "temp_avg=excluded.temp_avg, temp_max=excluded.temp_max, power_avg=excluded.power_avg, "
                "power_max=excluded.power_max", (floor, current_hour))
            self._conn.execute("DELETE FROM gpu_samples WHERE ts < ?", (now - keep_raw_s,))
            self._conn.commit()

    # ----- Argos request history -------------------------------------
    def add_requests(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self._lock:
            # cursor.rowcount, not sqlite3's total_changes: the latter has no
            # counterpart in Postgres, and both report the rows an INSERT took.
            cur = self._conn.executemany(
                f"INSERT OR IGNORE INTO requests ({', '.join(REQUEST_FIELDS)}) "
                f"VALUES ({', '.join('?' * len(REQUEST_FIELDS))})",
                [tuple(_speed(r.get(k)) if k in SPEED_FIELDS else r.get(k) for k in REQUEST_FIELDS) for r in rows])
            self._conn.commit()
            return int(cur.rowcount)

    def list_requests(self, model: Optional[str] = None, since: Optional[float] = None,
                      limit: int = 300) -> list[dict]:
        where, args = [], []
        if model:
            where.append("model=?")
            args.append(model)
        if since:
            where.append("ts>=?")
            args.append(since)
        sql = "SELECT * FROM requests" + (" WHERE " + " AND ".join(where) if where else "")
        return self._q(sql + " ORDER BY ts DESC LIMIT ?", args + [limit])

    def request_stats(self, since: float) -> list[dict]:
        return self._q(
            "SELECT model, COUNT(*) AS requests, AVG(CASE WHEN tokens_per_second > 0 THEN tokens_per_second END) AS tg_avg, "
            "MAX(CASE WHEN tokens_per_second > 0 THEN tokens_per_second END) AS tg_max, AVG(CASE WHEN prompt_per_second > 0 THEN prompt_per_second END) AS pp_avg, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(CASE WHEN status >= 400 OR (error IS NOT NULL AND error != '') THEN 1 ELSE 0 END) AS errors, "
            "MAX(ts) AS last_ts FROM requests WHERE ts>=? GROUP BY model ORDER BY requests DESC", (since,))

    def model_speed_summary(self, since: float, min_prompt: int, min_output: int,
                            until: Optional[float] = None) -> dict[str, dict[str, Any]]:
        """Per model since ``since``: request count, average decode speed, and the lowest / highest / count of
        prefill and decode speed (requests long enough to be meaningful, plus successful benchmark runs).

        The request history is the big table, and one scan over it holds the database lock for the whole scan,
        which stalls every other call on a slow disk. So it is read a day at a time, taking the lock per slice.
        """
        until = until or time.time()
        out: dict[str, dict[str, Any]] = {}

        def entry(model: str) -> dict[str, Any]:
            return out.setdefault(model, {"requests": 0, "tg_sum": 0.0, "tg_n": 0, "prefill": None, "decode": None})

        def stretch(d: dict[str, Any], hi: Any, lo: Any) -> None:
            """Within one prompt: its fastest stretch (the start) and its slowest (the end, the context full)."""
            if hi is None and lo is None:
                return
            cur = d.get("prefill_stretch") or {}
            d["prefill_stretch"] = {"high": max(x for x in (cur.get("high"), hi) if x is not None) if (hi or cur.get("high")) else None,
                                    "low": min(x for x in (cur.get("low"), lo) if x is not None) if (lo or cur.get("low")) else None}

        def widen(d: dict[str, Any], kind: str, lo: Any, hi: Any, n: int) -> None:
            if not n:
                return
            cur = d[kind]
            d[kind] = {"min": lo, "max": hi, "n": n} if cur is None else {
                "min": min(cur["min"], lo), "max": max(cur["max"], hi), "n": cur["n"] + n}

        sql = ("SELECT model, COUNT(*) AS requests, "
               "SUM(CASE WHEN tokens_per_second > 0 THEN tokens_per_second END) AS tg_sum, "
               "SUM(CASE WHEN tokens_per_second > 0 THEN 1 ELSE 0 END) AS tg_n, "
               "MIN(CASE WHEN prompt_per_second > 0 AND COALESCE(input_tokens, 0) >= ? THEN prompt_per_second END) AS prefill_min, "
               "MAX(CASE WHEN prompt_per_second > 0 AND COALESCE(input_tokens, 0) >= ? THEN prompt_per_second END) AS prefill_max, "
               "SUM(CASE WHEN prompt_per_second > 0 AND COALESCE(input_tokens, 0) >= ? "
               "THEN 1 ELSE 0 END) AS prefill_n, "
               "MAX(prefill_high_tps) AS stretch_high, MIN(CASE WHEN prefill_low_tps > 0 THEN prefill_low_tps END) AS stretch_low, "
               "MIN(CASE WHEN tokens_per_second > 0 AND COALESCE(output_tokens, 0) >= ? THEN tokens_per_second END) AS decode_min, "
               "MAX(CASE WHEN tokens_per_second > 0 AND COALESCE(output_tokens, 0) >= ? THEN tokens_per_second END) AS decode_max, "
               "SUM(CASE WHEN tokens_per_second > 0 AND COALESCE(output_tokens, 0) >= ? "
               "THEN 1 ELSE 0 END) AS decode_n "
               "FROM requests WHERE ts>=? AND ts<? GROUP BY model")
        t0 = since
        while t0 <= until:
            t1 = t0 + SLICE_S
            last = t1 > until
            rows = self._q(sql, (min_prompt, min_prompt, min_prompt, min_output, min_output, min_output,
                                 t0, float("inf") if last else t1))
            for r in rows:
                if r["model"] is None:
                    continue
                d = entry(r["model"])
                d["requests"] += r["requests"]
                d["tg_sum"] += r["tg_sum"] or 0.0
                d["tg_n"] += r["tg_n"] or 0
                widen(d, "prefill", r["prefill_min"], r["prefill_max"], r["prefill_n"])
                widen(d, "decode", r["decode_min"], r["decode_max"], r["decode_n"])
                stretch(d, r["stretch_high"], r["stretch_low"])
            t0 = t1
        for r in self._q(
                "SELECT model_id AS model, "
                "MAX(prefill_high_tps) AS stretch_high, MIN(CASE WHEN prefill_low_tps > 0 THEN prefill_low_tps END) AS stretch_low, "
                "MIN(CASE WHEN prompt_per_second > 0 THEN prompt_per_second END) AS prefill_min, "
                "MAX(CASE WHEN prompt_per_second > 0 THEN prompt_per_second END) AS prefill_max, "
                "SUM(CASE WHEN prompt_per_second > 0 THEN 1 ELSE 0 END) AS prefill_n, "
                "MIN(CASE WHEN tokens_per_second > 0 THEN tokens_per_second END) AS decode_min, "
                "MAX(CASE WHEN tokens_per_second > 0 THEN tokens_per_second END) AS decode_max, "
                "SUM(CASE WHEN tokens_per_second > 0 THEN 1 ELSE 0 END) AS decode_n "
                "FROM runs WHERE success=1 AND model_id IS NOT NULL AND model_id != '' AND created_at>=? GROUP BY model_id",
                (since,)):
            d = entry(r["model"])
            widen(d, "prefill", r["prefill_min"], r["prefill_max"], r["prefill_n"])
            widen(d, "decode", r["decode_min"], r["decode_max"], r["decode_n"])
            stretch(d, r["stretch_high"], r["stretch_low"])
        for d in out.values():
            d["tg_avg"] = d.pop("tg_sum") / d["tg_n"] if d["tg_n"] else None
            d.pop("tg_n")
        return out

    def request_window(self, model: str, t0: float, t1: float) -> dict:
        """What one model's requests did between two times."""
        rows = self._q(
            "SELECT COUNT(*) AS requests, AVG(CASE WHEN tokens_per_second > 0 THEN tokens_per_second END) AS tg_avg, "
            "AVG(CASE WHEN prompt_per_second > 0 THEN prompt_per_second END) AS pp_avg, SUM(output_tokens) AS output_tokens "
            "FROM requests WHERE model=? AND ts>=? AND ts<=?", (model, t0, t1))
        return rows[0] if rows else {"requests": 0, "tg_avg": None, "pp_avg": None, "output_tokens": None}

    # ----- downloads ------------------------------------------------------
    def save_download(self, d: dict) -> None:
        self._x("INSERT INTO downloads (id, repo, files, dest, status, bytes_done, bytes_total, error, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, bytes_done=excluded.bytes_done, bytes_total=excluded.bytes_total, "
                "error=excluded.error, updated_at=excluded.updated_at",
                (d["id"], d["repo"], json.dumps(d["files"]), d["dest"], d["status"], d.get("bytes_done", 0),
                 d.get("bytes_total", 0), d.get("error", ""), d.get("created_at", time.time()), time.time()))

    def list_downloads(self, limit: int = 100) -> list[dict]:
        rows = self._q("SELECT * FROM downloads ORDER BY created_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["files"] = _json(r["files"], [])
        return rows

    # ----- assistant chat -------------------------------------------------
    def add_message(self, role: str, content: str) -> int:
        return self._insert("INSERT INTO chat_messages (ts, role, content) VALUES (?, ?, ?)",
                            (time.time(), role, content))

    def list_messages(self) -> list[dict]:
        return self._q("SELECT * FROM chat_messages ORDER BY id")

    def mark_compacted(self, up_to_id: int) -> None:
        self._x("UPDATE chat_messages SET compacted=1 WHERE id<=?", (up_to_id,))

    def clear_chat(self) -> None:
        self._x("DELETE FROM chat_messages")


def _json(v: Any, default: Any) -> Any:
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def _decode(row: dict) -> dict:
    row["env_json"] = _json(row.get("env_json"), {})
    return row


__all__ = ["Store", "RUN_COLUMNS", "new_id"]
