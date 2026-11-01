"""Fixes from the first live run: sizes of sliding-window models, measured sizes, an embedder that can go idle,
engine models in the Playground and the Logs view, and a rescan that reaches the engine."""
import time

from src.gguf import read_arch
from src.scheduler import estimate_gib

from .fakes import make_engine, serve
from .ggufs import kv_str, kv_str_array, kv_u32, write_gguf
from .test_schedule_api import hook, pool_state


def test_sliding_window_layers_do_not_grow_with_the_context(tmp_path):
    path = str(tmp_path / "g.gguf")
    write_gguf(path, [
        kv_str("general.architecture", "gemma3"), kv_u32("gemma3.block_count", 48),
        kv_u32("gemma3.attention.head_count", 16), kv_u32("gemma3.attention.head_count_kv", 8),
        kv_u32("gemma3.attention.key_length", 256), kv_u32("gemma3.attention.value_length", 256),
        kv_u32("gemma3.embedding_length", 3840), kv_u32("gemma3.context_length", 131072),
        kv_u32("gemma3.attention.sliding_window", 1024), kv_str_array("tokenizer.ggml.tokens", ["a"]),
    ])
    arch = read_arch(path)
    assert arch["swa_window"] == 1024 and arch["swa_layers"] == 40, "one layer in six is global"
    swa = estimate_gib(13 * 2 ** 30, arch, 131072)["kv_gib"]
    full = estimate_gib(13 * 2 ** 30, {**arch, "swa_layers": 0}, 131072)["kv_gib"]
    assert swa < full / 5


def test_a_load_is_measured_from_what_the_gpu_gained(app, client):
    watch, store = hook(app, "watch_pool"), app.extensions["pb"].store
    state = pool_state(a=("stopped", []), b=("ready", ["1"]))

    def gpus(mb0, mb1):
        now = time.time()
        store.add_gpu_samples([{"ts": now, "gpu": 0, "name": "MI50", "mem_used_mb": mb0, "mem_total_mb": 32768, "source": "t"},
                               {"ts": now, "gpu": 1, "name": "MI50", "mem_used_mb": mb1, "mem_total_mb": 32768, "source": "t"}])
        time.sleep(0.01)

    with serve(make_engine([], [], pool=state)) as url:
        client.put("/api/settings/integrations", json={"engine_url": url})
        gpus(500, 3000); watch()
        state.update(pool_state(a=("starting", ["0"]), b=("ready", ["1"])))
        gpus(4000, 3000); watch()
        state.update(pool_state(a=("ready", ["0"]), b=("ready", ["1"])))
        gpus(14000, 3000); watch()
        assert "a" not in app.extensions["pb"].plans.get()["measured"], "measured only once it has been ready a whole tick"
        gpus(14500, 3000); watch()
    m = app.extensions["pb"].plans.get()["measured"]["a"]
    assert abs(m["gib"] - 14000 / 1024) < 0.01 and len(m["per_card"]) == 1
    assert "b" not in app.extensions["pb"].plans.get()["measured"]


def test_background_memory_sync_never_wakes_the_embedder(app, monkeypatch):
    mem = app.extensions["pb"].memory
    app.extensions["pb"].settings.update("memory", {"enabled": True, "model": "embed", "endpoint": "http://127.0.0.1:9/v1"})
    calls = []
    monkeypatch.setattr(mem, "embed", lambda texts: calls.append(len(texts)) or [[1.0, 0.0]] * len(texts))
    app.extensions["pb"].store.add_event("launch", "something happened")
    hook(app, "watch_memory")()
    assert calls == [], "the tick only collects records"
    mem.search("something", 3)
    assert calls, "a search embeds what is pending"


def test_engine_models_are_playground_launches(app, client):
    running = [{"model": "gemma", "state": "ready", "cmd": "llama-server -m /app/models/gemma.gguf -c 16384 --port 5800"}]
    with serve(make_engine([], [], model_ids=["gemma"], running=running)) as url:
        client.put("/api/settings/integrations", json={"engine_url": url})
        ls = client.get("/api/launches").get_json()["launches"]
        eng = next(x for x in ls if x.get("engine"))
        assert eng["launch_id"] == "engine:gemma" and eng["ready"] is True
        assert "log of gemma" in client.get("/api/launches/engine:gemma/log").get_data(as_text=True)
        run = client.post("/api/launches/engine:gemma/bench", json={"prompt_words": 100, "n_predict": 64}).get_json()["run"]
        assert run["tokens_per_second"] == 42.5 and run["base_model"] == "gemma"
        assert run["launch_command"].startswith("llama-server")
        assert client.post("/api/launches/engine:gemma/stop").get_json()["launch"]["stopped"] is True
        assert client.get("/api/launches/engine:nope/log").status_code == 404


def test_rescan_reaches_the_engine(app, client):
    with serve(make_engine([], [], model_ids=["a", "b"])) as url:
        client.put("/api/settings/integrations", json={"engine_url": url})
        j = client.post("/api/library/rescan").get_json()
    assert j["engine"] == {"ok": True, "models": 2, "changed": False}


def test_a_model_that_failed_to_start_stays_listed_with_why(app, client):
    crash = {"at": "2026-11-01T10:00:00Z", "line": "/app/bin/remote-llama: line 31: -LM_SSH_HOST: invalid variable name",
             "recent": 1, "blocked": False, "tail": ["loading", "/app/bin/remote-llama: line 31: -LM_SSH_HOST: invalid variable name"]}
    pool = {"router": "pool", "enabled": True, "cards": [], "queue": [], "swaps": [],
            "models": [{"id": "gemma-cpu", "state": "stopped", "crash": crash}, {"id": "fine", "state": "stopped"}]}
    with serve(make_engine([], [], model_ids=["gemma-cpu", "fine"], running=[], pool=pool)) as url:
        client.put("/api/settings/integrations", json={"engine_url": url})
        ls = [x for x in client.get("/api/launches").get_json()["launches"] if x.get("engine")]
    assert [x["launch_id"] for x in ls] == ["engine:gemma-cpu"]
    assert ls[0]["state"] == "failed" and ls[0]["running"] is False and "invalid variable name" in ls[0]["error"]
    assert ls[0]["tail"][-1].endswith("invalid variable name")
