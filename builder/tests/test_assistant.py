import json
import os
import time

from src.assistant import pick_quant

from .conftest import fake_command
from .fakes import make_engine, make_hub, serve


def sse(resp) -> list[dict]:
    return [json.loads(line[5:]) for line in resp.get_data(as_text=True).splitlines() if line.startswith("data:")]


def use_fake_model(client, app, **assistant):
    p = client.post("/api/profiles", json={"name": "fake assistant",
                                           "launch_command": fake_command()}).get_json()["profile"]
    client.put("/api/settings/assistant", json={"mode": "managed", "profile_id": p["profile_id"], **assistant})
    return p


def test_pick_quant():
    qs = [{"quant": "F16", "size": 9}, {"quant": "Q4_K_M", "size": 4}, {"quant": "Q8_0", "size": 6},
          {"quant": "IQ2_XS", "size": 2}]
    assert pick_quant(qs)["quant"] == "Q8_0"
    assert pick_quant([q for q in qs if q["quant"] != "Q8_0"])["quant"] == "Q4_K_M"
    assert pick_quant([{"quant": "F16", "size": 1}]) is None


def test_chat_needs_a_model(client):
    events = sse(client.post("/api/assistant/chat", json={"message": "hi"}))
    assert events[-1]["type"] == "error" and "Pick a model" in events[-1]["error"] and "Set up Qwen3-0.6B" in events[-1]["error"]


def test_managed_chat_digest_reset(client, app):
    store = app.extensions["pb"].store
    store.add_gpu_samples([{"ts": time.time(), "gpu": 0, "name": "MI50", "util": 91, "mem_used_mb": 30000,
                            "mem_total_mb": 32768, "temp_c": 77, "power_w": 240, "source": "test"}])
    store.add_requests([{"source_id": 1, "ts": time.time(), "model": "qwen3.6-35b", "tokens_per_second": 42.0,
                         "prompt_per_second": 700.0, "status": 200}])
    use_fake_model(client, app)

    digest = client.get("/api/assistant/digest").get_json()["digest"]
    assert "GPU0 MI50: 91% busy, VRAM 29.3/32.0 GiB, 77°C, 240 W" in digest
    assert "qwen3.6-35b: 1 req, gen 42.0 t/s avg" in digest

    events = sse(client.post("/api/assistant/chat", json={"message": "how is qwen3.6-35b doing?"}))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and kinds[-1] == "done", events
    answer = "".join(e["text"] for e in events if e["type"] == "delta")
    assert answer == "You said: how is qwen3.6-35b doing?. history=1 live=yes"

    events = sse(client.post("/api/assistant/chat", json={"message": "think first"}))
    answer = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "<think>" not in answer and answer.startswith("You said: think first. history=3")

    state = client.get("/api/assistant").get_json()
    assert [m["role"] for m in state["messages"]] == ["user", "assistant"] * 2
    assert state["server"]["running"] and state["server"]["ready"]
    assert client.post("/api/assistant/reset").get_json()["ok"]
    assert client.get("/api/assistant").get_json()["messages"] == []
    assert any(e["message"] == "chat reset" for e in client.get("/api/events?kind=assistant").get_json()["events"])
    client.post("/api/assistant/stop")
    assert client.get("/api/assistant").get_json()["server"]["running"] is False


def test_compaction_keeps_recent_turns(client, app):
    use_fake_model(client, app, context_tokens=1400, keep_recent=2)
    long = "x" * 900
    compacted = False
    for i in range(4):
        events = sse(client.post("/api/assistant/chat", json={"message": f"q{i} {long}"}))
        assert events[-1]["type"] == "done", events
        compacted |= any(e["type"] == "compacted" for e in events)
    assert compacted
    state = client.get("/api/assistant").get_json()
    assert state["summary"].startswith("SUMMARY of")
    live = [m for m in state["messages"] if not m["compacted"]]
    assert len(live) <= 3 and len(state["messages"]) == 8  # nothing deleted, only marked
    events = sse(client.post("/api/assistant/chat", json={"message": "and now?"}))
    assert "summary=yes" in "".join(e["text"] for e in events if e["type"] == "delta")


def test_endpoint_mode(client, app):
    runner = app.extensions["runner"]
    run = runner.launch(fake_command())
    deadline = time.time() + 15
    while not runner.health(run) and time.time() < deadline:
        time.sleep(0.1)
    client.put("/api/settings/assistant", json={"mode": "endpoint", "endpoint": f"http://127.0.0.1:{run.port}/v1",
                                                "model": "qwen3-0.6b-q8_0"})
    events = sse(client.post("/api/assistant/chat", json={"message": "ping"}))
    assert events[-1]["type"] == "done" and not any(e["type"] == "status" for e in events)
    assert client.get("/api/assistant").get_json()["server"]["mode"] == "endpoint"


def test_setup_uses_local_file_or_downloads(client, app):
    pb = app.extensions["pb"]
    models_dir = pb.settings.get("paths")["models_dir"]
    path = os.path.join(models_dir, "Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q8_0.gguf")
    os.makedirs(os.path.dirname(path))
    open(path, "wb").write(b"x")
    client.put("/api/settings/prefab", json={"load_mode": "dio", "split_mode": "layer", "env": "HIP_VISIBLE_DEVICES=0"})
    r = client.post("/api/assistant/setup").get_json()
    assert r["download"] is None
    cmd = r["profile"]["launch_command"]
    assert cmd == (f'HIP_VISIBLE_DEVICES=0 llama-server --model "{path}" --host 127.0.0.1 --port ${{PORT}} '
                   '-ngl 99 -c 8192 --flash-attn on -lm dio --split-mode none --jinja')
    assert pb.settings.get("assistant")["profile_id"] == r["profile"]["profile_id"]
    assert pb.settings.get("assistant")["mode"] == "engine" and pb.settings.get("assistant")["model"] == r["model"],         "the assistant uses the profile through the engine, picked like any other model"
    again = client.post("/api/assistant/setup").get_json()["profile"]
    assert again["profile_id"] == r["profile"]["profile_id"]  # updated, not duplicated

    os.remove(path)
    with serve(make_hub({"Qwen3-0.6B-Q8_0.gguf": b"q" * 64, "Qwen3-0.6B-F16.gguf": b"f" * 128})) as url:
        client.put("/api/settings/downloads", json={"hf_endpoint": url})
        r = client.post("/api/assistant/setup").get_json()
        assert r["download"]["files"] == [{"name": "Qwen3-0.6B-Q8_0.gguf", "size": 64}]
        deadline = time.time() + 10
        while not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.05)
        assert os.path.getsize(path) == 64


# ----- compacting, loading and unloading ------------------------------------------------
def test_compact_button_summarises_all_but_the_last_two(client, app):
    store = app.extensions["pb"].store
    with serve(make_engine([], [], model_ids=["qwen3-0.6b"], chat_reply="Tried q8_0 cache on GPU1; 62 t/s.")) as url:
        client.put("/api/settings/integrations", json={"engine_url": url})
        client.put("/api/settings/assistant", json={"mode": "engine", "model": "qwen3-0.6b"})
        for i in range(3):
            store.add_message("user", f"question {i}")
            store.add_message("assistant", f"answer {i}")
        r = client.post("/api/assistant/compact").get_json()
        assert r["compacted"] == 4 and "62 t/s" in r["summary"]
        assert [m["content"] for m in store.list_messages() if not m["compacted"]] == ["question 2", "answer 2"]
        assert client.post("/api/assistant/compact").get_json()["compacted"] == 0, "nothing left to compact"
        assert "62 t/s" in client.get("/api/assistant").get_json()["summary"]


def test_models_load_unload_and_unload_when_idle(client, app):
    running, calls = [], []
    a = app.extensions["pb"].assistant
    with serve(make_engine([], [], model_ids=["qwen3-0.6b", "nomic-embed"], running=running, calls=calls)) as url:
        client.put("/api/settings/integrations", json={"engine_url": url})
        client.put("/api/settings/assistant", json={"mode": "engine", "model": "qwen3-0.6b", "idle_unload_min": 5})
        client.put("/api/settings/memory", json={"model": "nomic-embed", "endpoint": ""})
        m = client.get("/api/assistant").get_json()["models"]
        assert (m["chat"]["model"], m["chat"]["state"], m["chat"]["controllable"]) == ("qwen3-0.6b", "stopped", True)
        assert (m["embed"]["model"], m["embed"]["where"]) == ("nomic-embed", "engine") and m["idle_unload_min"] == 5

        assert client.post("/api/assistant/models/chat/load").get_json()["ok"]
        assert ("GET", "/upstream/qwen3-0.6b/health") in calls
        running.append({"model": "qwen3-0.6b", "state": "ready"})
        assert client.get("/api/assistant").get_json()["models"]["chat"]["state"] == "ready"

        now = time.time()
        assert a.unload_idle(now=now + 60) == [], "used a minute ago"
        assert a.unload_idle(now=now + 6 * 60) == ["chat"]
        assert ("POST", "/api/models/unload/qwen3-0.6b") in calls and running == []
        running.append({"model": "nomic-embed", "state": "ready"})
        assert a.unload_idle(now=now + 60 * 60) == [], "a model the assistant did not use is left alone"

        client.post("/api/assistant/models/embed/load")
        assert client.post("/api/assistant/models/embed/unload").get_json()["model"]["state"] == "stopped"

        client.put("/api/settings/assistant", json={"idle_unload_min": 0})
        client.post("/api/assistant/models/chat/load")
        running.append({"model": "qwen3-0.6b", "state": "ready"})
        assert a.unload_idle(now=now + 99 * 60) == [], "0 means never"

        client.put("/api/settings/assistant", json={"mode": "endpoint", "endpoint": "http://elsewhere:8080/v1", "model": "m"})
        r = client.post("/api/assistant/models/chat/load")
        assert r.status_code == 409 and "another server" in r.get_json()["error"]
    assert client.post("/api/assistant/models/other/load").status_code == 404


def test_choices_offer_profiles_library_models_and_embeddings(client, app):
    root = app.extensions["pb"].settings.get("paths")["models_dir"]
    for rel in ("qwen/Qwen3-0.6B-Q8_0.gguf", "embed/nomic-embed-text-v1.5.Q8_0.gguf", "whisper/ggml-base.bin"):
        os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
        open(os.path.join(root, rel), "wb").write(b"x")
    path = os.path.join(root, "qwen", "Qwen3-0.6B-Q8_0.gguf").replace("\\", "/")
    client.post("/api/profiles", json={"name": "Assistant fast", "launch_command": f"llama-server --model {path} -c 8192 --port 8080"})
    r = client.get("/api/assistant/choices").get_json()
    assert r["engine_connected"] is False
    profiles = [c for c in r["chat"] if c["group"] == "Profiles"]
    assert len(profiles) == 1 and profiles[0]["label"] == "Assistant fast" and profiles[0]["id"].startswith("qwen3-0.6b-q8_0")
    assert "qwen3-0.6b-q8_0" in [c["id"] for c in r["chat"]]
    assert [c["id"] for c in r["embed"]] == ["nomic-embed-text-v1.5.q8_0"], "embedding models only, and no audio models"


    # a profile of an embedding model is offered for the memory, not as a chat model
    nomic = os.path.join(root, "embed", "nomic-embed-text-v1.5.Q8_0.gguf").replace("\\", "/")
    client.post("/api/profiles", json={"name": "nomic big batch", "launch_command": f"llama-server --model {nomic} -ub 2048 --port 8080"})
    client.post("/api/profiles", json={"name": "qwen as embedder", "launch_command": f"llama-server --model {path} --embedding --pooling last --port 8080"})
    r = client.get("/api/assistant/choices").get_json()
    embed = {c["label"]: c for c in r["embed"]}
    assert embed["nomic big batch"]["group"] == "Profiles" and embed["nomic big batch"]["id"].startswith("nomic-embed-text-v1.5.q8_0--")
    assert "qwen as embedder" in embed, "--embedding in the command makes it one"
    assert {c["label"] for c in r["chat"] if c["group"] == "Profiles"} == {"Assistant fast"}


def test_model_focus_knows_a_model_that_has_never_been_run(client, app):
    """A model Argos can see is a model the assistant can talk about.

    Regression: _model_focus took its names only from runs and requests, and
    the INVENTORY line in the digest carries counts, so a model that exists but
    has never been launched appeared nowhere -- asking for a profile on one got
    "no model found" about a model sitting in Argos's own catalog.
    """
    pb = app.extensions["pb"]
    pb.store.upsert_model_seen({
        "model_id": "qwen3-coder-next-q5_k_s-00001-of-00003",
        "name": "Qwen3-Coder-Next-Q5_K_S-00001-of-00003",
        "path": "/app/models/Qwen3-Coder-Next-GGUF/Qwen3-Coder-Next-Q5_K_S-00001-of-00003.gguf",
        "origin": "engine",
        "kind": "chat",
    })

    out = pb.assistant._model_focus(
        "I want a layer dual GPU with 256K context using Qwen3-Coder-Next-Q5_K_S-00001-of-00003",
        time.time(),
    )
    text = "\n".join(out)

    assert "qwen3-coder-next-q5_k_s-00001-of-00003" in text, text
    assert "no launches or benchmarks recorded yet" in text, text
    assert "engine" in text, "the catalog row's facts should come through"


def test_model_focus_stays_quiet_about_unknown_names(client, app):
    """A name Argos has never seen must not be invented into the context."""
    pb = app.extensions["pb"]
    out = pb.assistant._model_focus("tell me about totally-made-up-model-xyz", time.time())
    assert "totally-made-up-model-xyz" not in "\n".join(out)


def test_digest_names_the_saved_profiles(client, app):
    """A count is not an answer: profile questions need the names.

    Regression: the digest said only "Profiles: N saved across M models", so
    every question about what is saved, which is fastest, or what to tweak got
    a shrug -- the assistant knew profiles existed but not one of them.
    """
    pb = app.extensions["pb"]
    pb.store.save_profile({"name": "dual-gpu-256k", "base_model": "Qwen3-Coder-Next",
                           "model_id": "qwen3-coder-next-q5_k_s-00001-of-00003",
                           "launch_command": "llama-server -c 262144"})

    digest = pb.assistant.digest("what profiles do I have?")

    assert "dual-gpu-256k" in digest, digest
    assert "Qwen3-Coder-Next" in digest, digest
    assert "Profiles: 1 saved" in digest, digest


def test_the_prompt_says_where_things_live(client, app):
    """'How do I make a profile?' must be answerable, not guessed at."""
    pb = app.extensions["pb"]
    system = pb.assistant._prompt("LIVE DATA")[0]["content"]

    for view in ("Builder", "Profiles", "Playground", "Perf"):
        assert view in system, f"{view} missing from the system prompt"
    assert "WHERE THINGS LIVE" in system


def test_actions_are_split_out_and_validated(client, app):
    """An ACTION line becomes an action, not prose -- and only for a known id."""
    pb = app.extensions["pb"]
    pb.store.upsert_model_seen({"model_id": "qwen3-8b", "name": "Qwen3 8B",
                                "origin": "engine", "kind": "chat"})

    clean, actions = pb.assistant._actions(
        "Loading it now.\n"
        "ACTION load qwen3-8b\n"
        "ACTION unload nope-not-a-model\n"
        "ACTION keep qwen3-8b off"
    )

    assert clean == "Loading it now."
    assert actions == [
        {"verb": "load", "model": "qwen3-8b"},
        {"verb": "keep", "model": "qwen3-8b", "on": False},
    ], actions


def test_an_invented_model_id_never_becomes_an_action(client, app):
    """A small model guessing a name must not produce a working button."""
    pb = app.extensions["pb"]

    clean, actions = pb.assistant._actions("Sure.\nACTION load totally-made-up-xyz")

    assert actions == []
    assert clean == "Sure."


def test_the_digest_offers_the_action_vocabulary(client, app):
    pb = app.extensions["pb"]
    assert "ACTIONS you may propose" in pb.assistant.digest()


def test_model_focus_covers_more_than_two_models(client, app):
    """A question comparing several setups must not silently lose the extras.

    Names of different lengths keep the ordering deterministic (equal lengths
    would tie and leave the slice up to set iteration order).
    """
    pb = app.extensions["pb"]
    for mid in ("model-aaaa", "model-bbb", "model-cc", "model-d"):
        pb.store.upsert_model_seen({"model_id": mid, "name": mid, "origin": "engine"})

    out = pb.assistant._model_focus("compare model-aaaa model-bbb model-cc model-d", time.time())

    named = [ln for ln in out if ln.split(":")[0] in {"model-aaaa", "model-bbb", "model-cc", "model-d"}]
    assert len(named) == 4, out
