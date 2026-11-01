import time

from src.db import Store
from src.telemetry import Collector, fetch_activity, parse_nvidia_csv, read_amd_sysfs, read_engine

from .fakes import make_engine, serve


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_amd_sysfs(tmp_path):
    for card, busy, used in (("card0", "37", str(6 * 2**30)), ("card1", "0", str(1 * 2**30))):
        dev = tmp_path / card / "device"
        write(dev / "mem_info_vram_total", str(16 * 2**30))
        write(dev / "mem_info_vram_used", used)
        write(dev / "gpu_busy_percent", busy)
        write(dev / "hwmon" / "hwmon3" / "temp1_input", "61000")
        write(dev / "hwmon" / "hwmon3" / "power1_average", "182000000")
    write(tmp_path / "card0-DP-1" / "status", "connected")  # connectors are not GPUs
    write(tmp_path / "card2" / "device" / "vendor", "0x8086")  # no VRAM files: skipped
    gpus = read_amd_sysfs(str(tmp_path))
    assert [g["gpu"] for g in gpus] == [0, 1]
    assert gpus[0] == {"gpu": 0, "name": "AMD GPU (card0)", "util": 37.0, "mem_used_mb": 6144.0,
                       "mem_total_mb": 16384.0, "temp_c": 61.0, "power_w": 182.0, "source": "sysfs"}


def test_nvidia_csv():
    rows = parse_nvidia_csv("0, NVIDIA GeForce RTX 4090, 87, 20123, 24564, 71, 301.55\n1, x, [N/A], 1, 2, 3\n")
    assert rows == [{"gpu": 0, "name": "NVIDIA GeForce RTX 4090", "util": 87.0, "mem_used_mb": 20123.0,
                     "mem_total_mb": 24564.0, "temp_c": 71.0, "power_w": 301.55, "source": "nvidia-smi"}]


GPU_STATS = [
    {"timestamp": "2026-10-11T10:00:00Z", "id": 0, "name": "MI50", "temp_c": 50, "gpu_util_pct": 10,
     "mem_used_mb": 1000, "mem_total_mb": 32768, "power_draw_w": 90},
    {"timestamp": "2026-10-11T10:00:05Z", "id": 0, "name": "MI50", "temp_c": 55, "gpu_util_pct": 99,
     "mem_used_mb": 30000, "mem_total_mb": 32768, "power_draw_w": 250},
    {"timestamp": "2026-10-11T10:00:05Z", "id": 1, "name": "MI50", "temp_c": 40, "gpu_util_pct": 0,
     "mem_used_mb": 10, "mem_total_mb": 32768, "power_draw_w": 20},
]


def activity(i, model="qwen3.6-35b", tps=45.0, hour=10):
    return {"id": i, "timestamp": f"2026-10-11T{hour}:0{i % 10}:00Z", "src": "proxy", "model": model,
            "req_path": "/v1/chat/completions", "resp_status_code": 200 if i != 3 else 400,
            "tokens": {"input_tokens": 100, "output_tokens": 50, "cache_tokens": 0, "draft_tokens": 0,
                       "draft_acc_tokens": 0, "prompt_per_second": 800.0, "tokens_per_second": tps},
            "duration_ms": 1200, "error_msg": "too long" if i == 3 else ""}


def test_engine_readers():
    with serve(make_engine(GPU_STATS, [activity(i) for i in range(1, 5)])) as url:
        gpus = read_engine(url)
        assert [(g["gpu"], g["util"], g["mem_used_mb"]) for g in gpus] == [(0, 99, 30000), (1, 0, 10)]
        rows = fetch_activity(url, 2)
        assert [r["source_id"] for r in rows] == [3, 4] and rows[0]["error"] == "too long"
    acts = [activity(1), {**activity(2), "metadata": {"prefill_high_tps": "2048.0", "prefill_low_tps": "1024.5"}}]
    with serve(make_engine(GPU_STATS, acts)) as url:
        rows = fetch_activity(url, 0)
        assert (rows[0]["prefill_high_tps"], rows[1]["prefill_high_tps"], rows[1]["prefill_low_tps"]) == (None, 2048.0, 1024.5)


def test_the_prompt_stretch_reaches_the_model_summary(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    now = time.time()
    store.add_requests([{"source_id": i, "ts": now - i, "model": "m", "input_tokens": 9000, "prompt_per_second": 1300.0,
                         "prefill_high_tps": hi, "prefill_low_tps": lo} for i, hi, lo in ((1, 2000.0, 900.0), (2, 2400.0, 1100.0), (3, None, None))])
    s = store.model_speed_summary(now - 3600, 1000, 1)["m"]
    assert s["prefill_stretch"] == {"high": 2400.0, "low": 900.0}
    assert store.list_requests()[0]["prefill_high_tps"] in (2000.0, 2400.0, None)


def settings_for(url, source="auto", keep=120):
    s = {"telemetry": {"enabled": True, "interval_s": 15, "keep_minutes": keep, "source": source},
         "integrations": {"engine_url": url}}
    get = lambda: s  # noqa: E731
    get.values = s
    return get


def test_collector_stores_samples_activity_and_rollups(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    events = []
    acts = [activity(i) for i in range(1, 4)]
    with serve(make_engine(GPU_STATS, acts)) as url:
        settings = settings_for(url, keep=10 ** 8)  # the sample requests are dated weeks back
        col = Collector(store, settings, lambda *a, **k: events.append(a))
        now = time.time()
        col.tick(now=now - 7200)
        col.tick(now=now)
        assert col.last_source == "engine"
        assert len(store.list_requests()) == 3
        acts.append(activity(4, tps=50.0))  # new request on Argos
        assert col.sync_activity() == 1 and len(store.list_requests()) == 4
        acts[:] = [activity(1, model="fresh", hour=12)]  # Argos's database was recreated: ids restart
        assert col.sync_activity() == 1
        assert {r["model"] for r in store.list_requests()} == {"qwen3.6-35b", "fresh"}
    stats = {r["model"]: r for r in store.request_stats(0)}
    assert stats["qwen3.6-35b"]["requests"] == 4 and stats["qwen3.6-35b"]["errors"] == 1
    assert {r["gpu"] for r in store.gpu_latest()} == {0, 1}
    settings.values["telemetry"]["keep_minutes"] = 120
    col.tick(now=now + 3 * 3600)  # three hours on: everything before the last two is gone
    assert all(r["t"] >= now + 3600 - 1 for r in store.gpu_series(0, 1)) and store.gpu_hourly(0) == []
    assert all(r["ts"] >= now + 3600 for r in store.list_requests()), "request history too"


def test_collector_reports_each_failure_once(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    events = []
    col = Collector(store, settings_for("http://127.0.0.1:9", source="engine"),
                    lambda kind, msg, **k: events.append(msg))
    col.tick()
    col.tick()
    assert len([e for e in events if e.startswith("GPU sampling")]) == 1


def test_old_gpu_readings_are_deleted(tmp_path):
    store = Store(str(tmp_path / "p.db"))
    now = time.time()
    store.add_gpu_samples([{"ts": now - 3 * 3600, "gpu": 0, "name": "g", "util": 1, "source": "t"},
                           {"ts": now - 60, "gpu": 0, "name": "g", "util": 2, "source": "t"}])
    store.rollup_gpu(now=now)
    store.prune_gpu(now - 2 * 3600)
    assert [r["util"] for r in store.gpu_series(0, 1)] == [2]
    assert all(h["hour"] >= now - 3 * 3600 for h in store.gpu_hourly(0)), "rollups older than the window go too"


def test_log_routes(client, app):
    store = app.extensions["pb"].store
    now = time.time()
    store.add_gpu_samples([{"ts": now - 60, "gpu": 0, "name": "g", "util": 50, "mem_used_mb": 100,
                            "mem_total_mb": 1000, "temp_c": 60, "power_w": 100, "source": "test"}])
    store.add_event("launch", "boom", level="error")
    store.add_requests([{"source_id": 1, "ts": now, "model": "m", "tokens_per_second": 10.0}])
    s = client.get("/api/gpu/series?range=1h").get_json()
    assert s["source"] == "raw" and s["rows"][0]["util"] == 50 and s["latest"][0]["gpu"] == 0
    # the page asks for 2 s buckets, then only what is newer than what it has
    fine = client.get("/api/gpu/series?range=1h&bucket=2").get_json()
    assert fine["bucket"] == 2 and len(fine["rows"]) == 1
    assert client.get(f"/api/gpu/series?range=1h&bucket=2&since={now}").get_json()["rows"] == []
    assert client.get("/api/gpu/series?range=2w").status_code == 400
    assert client.get("/api/events?level=error").get_json()["events"][0]["message"] == "boom"
    assert client.get("/api/events?q=nothing").get_json()["events"] == []
    r = client.get("/api/requests?range=24h").get_json()
    assert r["requests"][0]["model"] == "m" and r["stats"][0]["requests"] == 1


def test_Collector_live_page_samples_every_two_seconds(tmp_path, monkeypatch):
    """Usage Live polls with live=1: the GPUs are read every 2 s while it does, not every 15 s, and the
    background hooks keep their own pace."""
    from src import telemetry
    store = Store(str(tmp_path / "t.db"))
    col = Collector(store, settings_for("", source="local"), lambda *a, **k: None, hooks=[lambda: hooks.append(1)])
    hooks, ticks = [], []
    monkeypatch.setattr(col, "tick", lambda: ticks.append(time.time()))
    monkeypatch.setattr(telemetry, "LIVE_INTERVAL_S", 0.05)
    col.start()
    time.sleep(0.3)
    assert len(ticks) == 1, "15 s apart when nobody is watching live"
    col.watch_live(seconds=0.5)
    time.sleep(0.4)
    col.stop()
    assert len(ticks) >= 4, "a live page wakes the loop and it samples fast"
    assert len(hooks) == 1, "the hooks do not speed up with it"


def test_Collector_series_with_live_turns_it_on(client, app):
    col = app.extensions["pb"].collector
    assert not col.live
    client.get("/api/gpu/series?range=5m")
    assert not col.live
    client.get("/api/gpu/series?range=5m&live=1")
    assert col.live


def test_the_cpu_and_ram_are_read_beside_the_gpus(tmp_path, monkeypatch):
    import src.telemetry as tel
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    perf = {"enabled": True, "gpu_stats": GPU_STATS, "sys_stats": [
        {"timestamp": stamp, "cpu_util_per_core": [10, 30, 50, 70], "mem_total_mb": 262144, "mem_used_mb": 65536,
         "swap_used_mb": 0, "load_avg_1": 3.5}]}
    asked = []
    monkeypatch.setattr(tel, "_get_json", lambda url, *a, **k: asked.append(url) or perf)
    rows, cpu = tel.read_engine_full("http://engine")
    assert "after=" in asked[0], "only the last half minute, not the whole history"
    assert cpu == {"util": 40.0, "cores": 4, "mem_used_mb": 65536, "mem_total_mb": 262144, "swap_used_mb": 0, "load1": 3.5}
    assert [r["gpu"] for r in rows] == [0, 1]

    store = Store(str(tmp_path / "c.db"))
    col = Collector(store, settings_for("http://engine", source="engine"), lambda *a, **k: None)
    monkeypatch.setattr(col, "sync_activity", lambda: 0)
    col.tick(now=now)
    assert store.cpu_latest()["util"] == 40.0 and store.cpu_series(0, 2)[0]["mem_used_mb"] == 65536
    store.prune_gpu(now + 1)
    assert store.cpu_latest() is None, "pruned with the GPU readings"


def test_this_machines_cpu_when_the_engine_has_none():
    import src.telemetry as tel
    first, counters = tel.read_local_cpu()
    if first is None:  # not Linux
        return
    assert first["util"] is None and first["mem_total_mb"] > 0, "busy needs two readings"
    second, _ = tel.read_local_cpu(counters)
    assert second["util"] is None or 0 <= second["util"] <= 100
