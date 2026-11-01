"""The hand-off: the small model declares ESCALATE and the bigger one answers."""
from src.assistant import ESCALATE_RE

SMALL = "qwen3-0.6b-q8_0"
BIG = "qwen3.5-2b-q4_k_m"


def test_the_marker_must_be_its_own_line():
    assert ESCALATE_RE.search("ESCALATE")
    assert ESCALATE_RE.search("  escalate.  ")
    assert ESCALATE_RE.search("Try this.\nESCALATE\n")
    # saying the word is not declaring it
    assert ESCALATE_RE.search("I will not escalate") is None
    assert ESCALATE_RE.search("ESCALATE because it is hard") is None


def test_the_marker_is_stripped_before_the_user_sees_it():
    assert ESCALATE_RE.sub("", "Try this.\nESCALATE\n").strip() == "Try this."


def test_no_escalation_model_means_the_small_one_answers_everything(client, app):
    a = app.extensions["pb"].assistant
    client.put("/api/settings/assistant", json={"mode": "engine", "model": SMALL, "escalate_model": ""})
    client.put("/api/settings/integrations", json={"engine_url": "http://engine"})
    assert a.escalate_model() == ""
    a._escalated = True          # even when a turn says it escalated
    _base, model = a.endpoint()
    assert model == SMALL, "with nothing configured there is nowhere to hand off to"


def test_a_handed_over_turn_is_answered_by_the_bigger_model(client, app):
    a = app.extensions["pb"].assistant
    client.put("/api/settings/assistant", json={"mode": "engine", "model": SMALL, "escalate_model": BIG})
    client.put("/api/settings/integrations", json={"engine_url": "http://engine"})
    assert a.escalate_model() == BIG

    a._escalated = False
    assert a.endpoint()[1] == SMALL, "the small model answers first"

    a._escalated = True
    assert a.endpoint()[1] == BIG, "after the hand-off the bigger model answers"


def test_each_model_keeps_its_own_quant_default(client, app):
    """The assistant's model lives on the GPU; the escalation model may not."""
    cfg = app.extensions["pb"].settings.get("assistant")
    assert cfg["quant"] == "Q8_0"
    assert cfg["escalate_quant"] == "Q4_K_M"


def test_a_local_file_is_picked_by_the_quant_that_was_asked_for(tmp_path):
    from src.routes.assistant import _local_model

    d = tmp_path / "models"
    d.mkdir()
    for name in ("Qwen3-0.6B-Q8_0.gguf", "Qwen3-0.6B-Q4_K_M.gguf"):
        (d / name).write_bytes(b"x" * 10)
    assert _local_model(str(d), r"qwen3-0\.6b", "Q8_0")["quant"] == "Q8_0"
    assert _local_model(str(d), r"qwen3-0\.6b", "Q4_K_M")["quant"] == "Q4_K_M"


def test_the_escalation_model_has_its_own_repo():
    from src.assistant import ESCALATE_REPOS

    assert ESCALATE_REPOS and all("2B" in r or "2b" in r for r in ESCALATE_REPOS), ESCALATE_REPOS


POOL = {"cards": [{"id": "0", "free_gib": 1.0}, {"id": "1", "free_gib": 0.5}], "models": [
    {"id": "coder", "state": "ready", "size_gib": 62.7, "cards": ["0", "1"], "in_flight": 1, "slots": [{"id": 0}]},
    {"id": SMALL, "state": "ready", "size_gib": 0.8, "on_cpu": True},
    {"id": "big-gpu", "state": "stopped", "size_gib": 6.0}, {"id": "big-cpu", "state": "stopped", "size_gib": 6.0}]}


def test_each_model_has_a_gpu_and_a_cpu_choice():
    from src.model_choice import choose
    assert choose("g", "", POOL)[0] == "g", "no CPU model: the GPU one, as before"
    assert choose("big-gpu", "big-cpu", POOL) == ("big-cpu", "the CPU model: the GPU one needs 6.0 GiB and only 1.5 GiB is free")
    roomy = {**POOL, "cards": [{"id": "0", "free_gib": 20}]}
    assert choose("big-gpu", "big-cpu", roomy)[0] == "big-gpu", "it fits: the GPU one"
    loaded = {**roomy, "models": POOL["models"][:3] + [{"id": "big-cpu", "state": "ready", "size_gib": 6.0}]}
    assert choose("big-gpu", "big-cpu", loaded)[0] == "big-cpu", "one already loaded is kept, never both"


def test_a_hard_question_goes_to_a_loaded_idle_model_then_the_escalation_pair(client, app, monkeypatch):
    a = app.extensions["pb"].assistant
    client.put("/api/settings/integrations", json={"engine_url": "http://engine"})
    client.put("/api/settings/assistant", json={"mode": "engine", "model": SMALL, "escalate_model": "big-gpu", "escalate_model_cpu": "big-cpu"})
    pool = {k: v for k, v in POOL.items()}
    monkeypatch.setattr(a, "_pool", lambda: pool)
    assert a.bigger_model()[0] == "big-cpu", "the coder is busy and the GPUs are full: the CPU escalation model"

    pool["models"] = [{**m, "in_flight": 0, "slots": []} if m["id"] == "coder" else m for m in POOL["models"]]
    assert a.bigger_model() == ("coder", "coder, already loaded and idle"), "an idle loaded big model: no load"
    client.put("/api/settings/assistant", json={"escalate_to_loaded": False})
    assert a.bigger_model()[0] == "big-cpu"

    client.put("/api/settings/assistant", json={"escalate_model_cpu": ""})
    model, why = a.bigger_model()
    assert model == "" and "needs 6.0 GiB" in why and "unload a loaded model" in why, "never a load that would wait or unload"


def test_numbers_the_data_never_gave_are_caught():
    from src.assistant import unsupported_numbers
    data = "- coder (ready, on GPU 0,1): last request: prefill 1077 t/s, decode 38.1 t/s; 62.70 GiB"
    assert unsupported_numbers("Prefill 1,077 t/s and decode 38 t/s, 62.7 GiB.", data) == []
    assert unsupported_numbers("decode speed of 79.0 t/s and prefill speed of 49.3 t/s", data) == ["79.0 t/s", "49.3 t/s"]


def test_a_made_up_answer_is_replaced_by_the_bigger_models(client, app, monkeypatch):
    a = app.extensions["pb"].assistant
    client.put("/api/settings/integrations", json={"engine_url": "http://engine"})
    client.put("/api/settings/assistant", json={"mode": "engine", "model": SMALL, "escalate_model": "big-gpu"})
    monkeypatch.setattr(a, "bigger_model", lambda: ("big-gpu", "the escalation model"))
    monkeypatch.setattr(a, "endpoint", lambda status=None, wait_s=300: ("http://engine/v1", a._big if a._escalated else SMALL))
    monkeypatch.setattr(a, "compact", lambda *x: False)
    replies = {SMALL: ["It runs at ", "79.0 t/s."], "big-gpu": ["The data shows no speed for that yet."]}
    monkeypatch.setattr(a, "_stream", lambda base, model, msgs: iter(replies[model]))
    events = list(a.chat("gpu temperature?"))
    kinds = [e["type"] for e in events]
    assert "reset" in kinds and kinds.index("reset") > kinds.index("delta"), "the small answer is shown, then cleared"
    after = "".join(e["text"] for e in events[kinds.index("reset"):] if e["type"] == "delta")
    assert after == "The data shows no speed for that yet."
    assert any(e["type"] == "status" and "79.0 t/s" in e["text"] for e in events)


def test_profiles_of_one_embedding_model_share_the_vectors(client, app):
    mem = app.extensions["pb"].memory
    client.put("/api/settings/memory", json={"enabled": True, "model": "qembed--gpu-8k", "model_cpu": "qembed--cpuonly-8k"})
    mem.pick = lambda: "qembed--cpuonly-8k"
    assert mem.vector_key() == "qembed"
    st = mem.store
    st._x("INSERT INTO memory_records (source, key, ts, text, hash, vector, model, dims) VALUES ('note', 'a', 1, 't', 'h', ?, ?, 2)",
          (b"\\x00" * 8, "qembed--gpu-8k"))
    st._x("INSERT INTO memory_records (source, key, ts, text, hash, vector, model, dims) VALUES ('note', 'b', 1, 't', 'h2', ?, ?, 2)",
          (b"\\x00" * 8, "other-model"))
    assert mem.status()["embedded"] == 1, "the GPU profile's vector counts for the CPU profile"
