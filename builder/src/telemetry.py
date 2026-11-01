"""Long-term GPU and request history.

A background collector samples the GPUs every few seconds and stores them in
``gpu_samples``; finished hours are folded into ``gpu_hourly`` (kept forever)
and raw rows are dropped after the retention window. GPU readings come from,
in ``auto`` order:

1. Argos's ``GET /api/performance`` -- the container that owns the GPUs
   already samples them (Argos reads LACT, rocm-smi, sysfs or
   nvidia-smi), so this app needs no device access at all;
2. AMD sysfs under /sys/class/drm (gpu_busy_percent, mem_info_vram_*, hwmon);
3. ``nvidia-smi``.

With an Argos URL configured it also copies Argos's per-request
activity (``GET /api/metrics/activity``) into ``requests``, so speeds per
model survive Argos restarts and its own retention.
"""
from __future__ import annotations

import glob
import json
import os
import shutil

from . import gputools
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Optional

from .gpu_pick import note_cards
from .db import Store

DRM = "/sys/class/drm"


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _num(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def read_amd_sysfs(root: str = DRM) -> list[dict]:
    out = []
    cards = sorted((c for c in glob.glob(os.path.join(root, "card*")) if os.path.basename(c)[4:].isdigit()),
                   key=lambda c: int(os.path.basename(c)[4:]))
    for card in cards:
        dev = os.path.join(card, "device")
        total = _num(_read(os.path.join(dev, "mem_info_vram_total")))
        if not total:
            continue
        hwmon = (glob.glob(os.path.join(dev, "hwmon", "hwmon*")) or [""])[0]
        temp = _num(_read(os.path.join(hwmon, "temp1_input"))) if hwmon else None
        power = (_num(_read(os.path.join(hwmon, "power1_average"))) or
                 _num(_read(os.path.join(hwmon, "power1_input")))) if hwmon else None
        out.append({
            "gpu": len(out),
            "name": _read(os.path.join(dev, "product_name")) or f"AMD GPU ({os.path.basename(card)})",
            "util": _num(_read(os.path.join(dev, "gpu_busy_percent"))),
            "mem_used_mb": round((_num(_read(os.path.join(dev, "mem_info_vram_used"))) or 0) / 2**20, 1),
            "mem_total_mb": round(total / 2**20, 1),
            "temp_c": temp / 1000 if temp is not None else None,
            "power_w": round(power / 1e6, 1) if power is not None else None,
            "source": "sysfs",
        })
    return out


NVIDIA_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"


def parse_nvidia_csv(text: str) -> list[dict]:
    out = []
    for line in text.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) != 7:
            continue
        nums = [_num(x) for x in p[2:]]
        out.append({"gpu": int(_num(p[0]) or 0), "name": p[1], "util": nums[0], "mem_used_mb": nums[1],
                    "mem_total_mb": nums[2], "temp_c": nums[3], "power_w": nums[4], "source": "nvidia-smi"})
    return out


def read_nvidia_smi() -> list[dict]:
    exe = gputools.tool("nvidia-smi")
    if not exe:
        return []
    try:
        res = subprocess.run([exe, f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_nvidia_csv(res.stdout)


def _get_json(url: str, timeout: float = 5):
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def read_engine(base_url: str) -> list[dict]:
    """Latest reading per GPU from Argos's /api/performance."""
    return read_engine_full(base_url)[0]


def read_engine_full(base_url: str) -> tuple[list[dict], Optional[dict]]:
    """(the latest reading per GPU, the latest CPU and RAM reading) from the engine's /api/performance, the last
    half minute of it. The CPU reading is the machine's: models running on the CPU in another container on the
    same host count in it."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
    data = _get_json(base_url.rstrip("/") + "/api/performance?after=" + urllib.parse.quote(since))
    if not data.get("enabled"):
        return [], None
    sys_rows = data.get("sys_stats") or data.get("system_stats") or []
    cpu = None
    if sys_rows:
        s = max(sys_rows, key=lambda r: _ts(r.get("timestamp")))
        cores = [float(x) for x in s.get("cpu_util_per_core") or [] if x is not None]
        cpu = {"util": round(sum(cores) / len(cores), 1) if cores else None, "cores": len(cores),
               "mem_used_mb": s.get("mem_used_mb"), "mem_total_mb": s.get("mem_total_mb"),
               "swap_used_mb": s.get("swap_used_mb"), "load1": s.get("load_avg_1")}
    latest: dict[int, dict] = {}
    for g in data.get("gpu_stats") or []:
        gid = int(g.get("id", 0))
        if gid not in latest or _ts(g.get("timestamp")) >= _ts(latest[gid].get("timestamp")):
            latest[gid] = g
    return [{"gpu": gid, "name": g.get("name") or f"GPU {gid}", "util": g.get("gpu_util_pct"),
             "mem_used_mb": g.get("mem_used_mb"), "mem_total_mb": g.get("mem_total_mb"),
             "temp_c": g.get("temp_c"), "power_w": g.get("power_draw_w"), "source": "engine"}
            for gid, g in sorted(latest.items())], cpu


def read_local_cpu(prev: Optional[tuple] = None) -> tuple[Optional[dict], Optional[tuple]]:
    """CPU and RAM of this machine from /proc (Linux), for when the engine does not report them. CPU busy is
    worked out between two readings, so the first one has none. Returns (reading, the counters to pass next time)."""
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            first = f.readline().split()[1:]
        with open("/proc/meminfo", encoding="utf-8") as f:
            mem = {line.split(":")[0]: int(line.split()[1]) for line in f if line.split()[1:2] and line.split()[1].isdigit()}
    except (OSError, ValueError, IndexError):
        return None, prev
    nums = [int(x) for x in first]
    idle, total = nums[3] + (nums[4] if len(nums) > 4 else 0), sum(nums)
    util = None
    if prev and total > prev[1]:
        util = round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)
    total_kb, avail_kb = mem.get("MemTotal", 0), mem.get("MemAvailable", mem.get("MemFree", 0))
    return ({"util": util, "cores": os.cpu_count() or 0, "mem_used_mb": round((total_kb - avail_kb) / 1024, 1),
             "mem_total_mb": round(total_kb / 1024, 1),
             "swap_used_mb": round((mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 1024, 1),
             "load1": os.getloadavg()[0] if hasattr(os, "getloadavg") else None}, (idle, total))


def fetch_activity(base_url: str, after_id: int, limit: int = 999, order: str = "asc") -> list[dict]:
    """Argos activity rows with id > after_id as ``requests`` rows (oldest first by default)."""
    qs = urllib.parse.urlencode({"min_id": after_id + 1, "limit": limit, "sort": "id", "order": order})
    page = _get_json(f"{base_url.rstrip('/')}/api/metrics/activity?{qs}")
    rows = []
    for e in page.get("data") or []:
        t = e.get("tokens") or {}
        rows.append({
            "source_id": e.get("id"), "ts": _ts(e.get("timestamp")), "model": e.get("model"),
            "src": e.get("src"), "path": e.get("req_path"), "status": e.get("resp_status_code"),
            "input_tokens": t.get("input_tokens"), "output_tokens": t.get("output_tokens"),
            "cache_tokens": t.get("cache_tokens"), "draft_tokens": t.get("draft_tokens"),
            "draft_acc_tokens": t.get("draft_acc_tokens"), "prompt_per_second": t.get("prompt_per_second"),
            "tokens_per_second": t.get("tokens_per_second"), "duration_ms": e.get("duration_ms"),
            "error": e.get("error_msg") or "",
            # the engine reads these from the model's log: the fastest and slowest stretch of the prompt
            "prefill_high_tps": _num_or_none((e.get("metadata") or {}).get("prefill_high_tps")),
            "prefill_low_tps": _num_or_none((e.get("metadata") or {}).get("prefill_low_tps")),
        })
    return rows


def _num_or_none(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def recent_requests(base_url: str, limit: int = 25) -> list[dict]:
    """The engine's newest requests, newest first, read straight from it: the Live feed cannot wait for the
    collector's next sync. Each row is a ``requests`` row; unknown numbers come back as None, not -1."""
    rows = fetch_activity(base_url, 0, limit=limit, order="desc")
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, (int, float)) and v < 0:
                r[k] = None
    return rows


LIVE_INTERVAL_S = 1  # how often the GPUs are read while a Usage page is live
HOOKS_EVERY_S = 15  # the background hooks keep this pace however often the GPUs are read
LIVE_HOLD_S = 10  # live sampling stops this long after the page's last poll


class Collector:
    """Background loop: GPU samples, activity sync, rollups and periodic hooks."""

    def __init__(self, store: Store, settings: Callable[[], dict], on_event: Callable[..., None],
                 hooks: Optional[list[Callable[[], None]]] = None):
        self.store = store
        self.settings = settings  # -> {"telemetry": {...}, "integrations": {...}}
        self.on_event = on_event
        self.hooks = hooks or []
        self._stop = threading.Event()
        self._nudge = threading.Event()  # wakes the loop early: a Live page just opened
        self._live_until = 0.0
        self._last_hooks = 0.0
        self._thread: Optional[threading.Thread] = None
        self._last_rollup = 0.0
        self.last_cpu: Optional[dict] = None
        self._cpu_prev: Optional[tuple] = None
        self._reported: dict[str, float] = {}
        self.last_source = ""

    def start(self) -> None:
        # Readings used to come every 15 s by default, which left a short range two points to draw. A setting
        # still at that old default moves to the new one, once; one changed by hand stays.
        try:
            saved = self.store.get_settings()
            if not saved.get("_telemetry_2s") and (saved.get("telemetry") or {}).get("interval_s") == 15:
                self.store.set_setting("telemetry", {**saved["telemetry"], "interval_s": 2})
            self.store.set_setting("_telemetry_2s", True)
        except Exception:  # noqa: BLE001 - the old pace still works
            pass
        if not self._thread:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="telemetry")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._nudge.set()

    def watch_live(self, seconds: float = LIVE_HOLD_S) -> None:
        """Someone is watching Usage live: sample every LIVE_INTERVAL_S for the next ``seconds``, not every
        telemetry interval (15 s by default), so the charts move while they look. Each poll renews it."""
        was = self.live
        self._live_until = time.time() + seconds
        if not was:
            self._nudge.set()

    @property
    def live(self) -> bool:
        return time.time() < self._live_until

    def _loop(self) -> None:
        while not self._stop.is_set():
            t = self.settings()["telemetry"]
            interval = max(1, int(t.get("interval_s") or 2))
            if t.get("enabled") and t.get("source") != "off":
                self.tick()
            if time.time() - self._last_hooks >= max(interval, HOOKS_EVERY_S):  # the hooks keep their own pace
                self._last_hooks = time.time()
                for hook in self.hooks:
                    try:
                        hook()
                    except Exception as exc:  # noqa: BLE001
                        self._report(f"background task failed: {exc}")
            self._nudge.wait(min(interval, LIVE_INTERVAL_S) if self.live else interval)
            self._nudge.clear()

    def sample(self) -> list[dict]:
        s = self.settings()
        source = s["telemetry"].get("source") or "auto"
        url = s["integrations"].get("engine_url") or ""
        readers: list[tuple[str, Callable[[], list[dict]]]] = []
        if source in ("auto", "engine") and url:
            def engine_reader():
                rows, cpu = read_engine_full(url)
                self.last_cpu = cpu
                return rows
            readers.append(("engine", engine_reader))
        if source in ("auto", "local"):
            readers += [("sysfs", read_amd_sysfs), ("nvidia-smi", read_nvidia_smi)]
        errors = []
        for name, reader in readers:
            try:
                rows = reader()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
                continue
            if rows:
                self.last_source = name
                return rows
        if errors:
            self._report("GPU sampling: " + "; ".join(errors))
        return []

    def tick(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        self.last_cpu = None
        rows = self.sample()
        for r in rows:
            r["ts"] = now
        self.store.add_gpu_samples(rows)
        # CPU and RAM beside the GPUs: from the engine's reading, else this machine's own
        cpu = self.last_cpu
        if cpu is None:
            cpu, self._cpu_prev = read_local_cpu(self._cpu_prev)
        if cpu and (cpu.get("util") is not None or cpu.get("mem_total_mb")):
            self.store.add_cpu_sample({**cpu, "ts": now})
        note_cards(rows)  # AMD or NVIDIA, for the Builder's GPU pick
        self.sync_activity()
        if now - self._last_rollup > 60:
            minutes = self.settings()["telemetry"].get("keep_minutes") or 120
            self.store.prune_gpu(now - minutes * 60)
            self.store.prune_requests(now - minutes * 60)
            self._last_rollup = now

    def sync_activity(self) -> int:
        url = self.settings()["integrations"].get("engine_url") or ""
        if not url:
            return 0
        cursor = int(self.store.get_settings().get("_activity_cursor") or 0)
        try:
            rows = fetch_activity(url, cursor)
            if not rows and cursor:
                # Argos's ids restart when its database is recreated: resync from 0
                latest = fetch_activity(url, 0, limit=1, order="desc")
                if latest and int(latest[0]["source_id"] or 0) < cursor:
                    rows = fetch_activity(url, 0)
                    self.store.set_setting("_activity_cursor", 0)
        except Exception as exc:  # noqa: BLE001
            self._report(f"Argos activity: {exc}")
            return 0
        if rows:
            self.store.add_requests(rows)
            self.store.set_setting("_activity_cursor", max(int(r["source_id"] or 0) for r in rows))
        return len(rows)

    def _report(self, message: str) -> None:
        """Log each distinct failure at most once an hour instead of every tick."""
        now = time.time()
        if now - self._reported.get(message, 0) > 3600:
            self._reported[message] = now
            self.on_event("telemetry", message, level="warn")


__all__ = ["Collector", "read_amd_sysfs", "read_nvidia_smi", "parse_nvidia_csv", "read_engine",
           "fetch_activity"]
