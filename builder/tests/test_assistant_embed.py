"""The assistant's pair: a chat model and an embedding model, one idle clock each."""
import time

from src.integrations import engine_api

CHAT = "qwen3-0.6b-q8_0"
EMBED = "qwen3-embedding-0.6b-q8_0"


def engine_pair(client, app, monkeypatch, state="ready"):
    """The assistant in engine mode with both models named, and a fake engine."""
    loaded, unloaded = [], []
    client.put("/api/settings/assistant", json={"mode": "engine", "model": CHAT, "idle_unload_min": 15})
    client.put("/api/settings/memory", json={"model": EMBED, "enabled": True})
    client.put("/api/settings/integrations", json={"engine_url": "http://engine"})
    monkeypatch.setattr(engine_api, "model_state", lambda url, mid, timeout=1.5: state)
    monkeypatch.setattr(engine_api, "unload_model", lambda url, mid, timeout=60.0: unloaded.append(mid))
    monkeypatch.setattr(engine_api, "load_model", lambda url, mid, timeout=900.0: loaded.append(mid))
    return app.extensions["pb"].assistant, loaded, unloaded


def test_both_models_unload_on_their_own_clocks(client, app, monkeypatch):
    a, _loaded, unloaded = engine_pair(client, app, monkeypatch)
    now = time.time()
    a.last_used = now - 16 * 60
    a.memory.last_used = now - 20 * 60
    a.unload_idle(now)
    assert sorted(unloaded) == sorted([CHAT, EMBED]), "each model keeps its own timer"


def test_each_clock_is_independent(client, app, monkeypatch):
    a, _loaded, unloaded = engine_pair(client, app, monkeypatch)
    now = time.time()
    a.last_used = now - 16 * 60      # chat is overdue
    a.memory.last_used = now - 60    # the embedder was used a minute ago
    a.unload_idle(now)
    assert unloaded == [CHAT], "a busy embedding model must not be dragged out with the chat model"


def test_an_embedding_model_loaded_by_something_else_gets_a_clock(client, app, monkeypatch):
    """The schedule loads it, so nothing here ever stamped it: it used to be skipped forever."""
    a, _loaded, unloaded = engine_pair(client, app, monkeypatch)
    assert a.memory.last_used == 0
    now = time.time()
    a.last_used = now                     # the chat model is busy, so only the embedder is adopted

    a.unload_idle(now)                    # adopts it: the clock starts
    assert a.memory.last_used == now
    assert unloaded == [], "not unloaded on the tick that starts its clock"

    a.last_used = now + 16 * 60 - 60      # the chat model answered a minute ago
    a.unload_idle(now + 16 * 60)          # and then the usual timer applies, to it alone
    assert unloaded == [EMBED]


def test_a_ready_chat_model_is_not_reloaded(client, app, monkeypatch):
    a, loaded, _unloaded = engine_pair(client, app, monkeypatch)
    now = time.time()
    a.last_used = now

    a.unload_idle(now + 60)
    assert loaded == [] and a.last_used == now


def test_warm_embed_starts_it_and_its_clock(client, app, monkeypatch):
    a, loaded, _unloaded = engine_pair(client, app, monkeypatch, state="stopped")
    assert a.warm_embed() is True
    assert abs(a.memory.last_used - time.time()) < 5, "the clock starts from the moment it was needed"
    for _ in range(50):
        if loaded:
            break
        time.sleep(0.02)
    assert loaded == [EMBED]


def test_warm_embed_leaves_a_running_one_alone(client, app, monkeypatch):
    a, loaded, _unloaded = engine_pair(client, app, monkeypatch, state="ready")
    assert a.warm_embed() is False, "already up: nothing to start"
    assert loaded == []
    assert a.memory.last_used > 0, "but the clock still starts, so the timer covers it"


def test_warm_embed_without_an_embedding_model_is_a_no_op(client, app, monkeypatch):
    a, loaded, _unloaded = engine_pair(client, app, monkeypatch)
    client.put("/api/settings/memory", json={"model": "", "enabled": True})
    assert a.warm_embed() is False and loaded == []


def test_a_cpu_only_embedder_embeds_as_records_come_in(client, app, monkeypatch):
    from src.integrations import engine_api
    a = app.extensions["pb"].assistant
    client.put("/api/settings/integrations", json={"engine_url": "http://engine"})
    client.put("/api/settings/memory", json={"model": "embed--cpu"})
    launches = {"embed--cpu": {"cmd": "llama-server -m /m/e.gguf --embedding -ngl 99", "env": ["HIP_VISIBLE_DEVICES=-1"]},
                "embed--gpu": {"cmd": "llama-server -m /m/e.gguf --embedding -ngl 99", "env": ["HIP_VISIBLE_DEVICES=0"]}}
    pool = {"models": []}
    monkeypatch.setattr(engine_api, "model_launch", lambda url, mid, timeout=3.0: launches.get(mid, {}))
    monkeypatch.setattr(engine_api, "pool_state", lambda url, timeout=3.0: pool)
    assert a.embed_on_cpu() is True, "its GPUs are hidden"

    client.put("/api/settings/memory", json={"model": "embed--gpu"})
    assert a.embed_on_cpu() is False, "on a GPU it waits for a search, as before"
    a._embed_cpu = None
    pool["models"] = [{"id": "embed--gpu", "on_cpu": True}]
    assert a.embed_on_cpu() is True, "fallen back to the CPU while the GPUs were full"
