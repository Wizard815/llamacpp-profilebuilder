"""The Builder panel: the assistant sees what is on screen, and can propose a flag edit."""
from src.assistant import _builder_line


def test_the_builder_line_says_what_is_on_screen():
    line = _builder_line({
        "command": "llama-server -m /m/x.gguf -ngl 99 -c 32768",
        "flags": {"model": "/m/x.gguf", "n-gpu-layers": "99", "flash-attn": "on", "HIP_VISIBLE_DEVICES": "0"},
        "env": "HIP_VISIBLE_DEVICES=0",
        "models": ["qwen3-0.6b-q8_0"],
    })
    assert "command: llama-server -m /m/x.gguf" in line
    assert "n-gpu-layers=99" in line and "flash-attn=on" in line
    assert "env: HIP_VISIBLE_DEVICES=0" in line
    assert "comparing against: qwen3-0.6b-q8_0" in line


def test_the_line_carries_everything_the_page_holds():
    """The panel is meant to help build, so it sees the program, the loose flags and
    what is applied at launch, not only the ones with a field of their own."""
    line = _builder_line({"program": "/app/bin/remote-llama",
                          "other": ["--n-cpu-moe 4", "--no-warmup"],
                          "always": ["LLAMA_CACHE=/data/cache"]})
    assert "program: /app/bin/remote-llama" in line
    assert "other flags: --n-cpu-moe 4, --no-warmup" in line
    assert "always applied: LLAMA_CACHE=/data/cache" in line


def test_an_env_var_is_not_offered_as_a_flag():
    line = _builder_line({"flags": {"HIP_VISIBLE_DEVICES": "0", "n-gpu-layers": "99"}})
    assert "n-gpu-layers=99" in line and "HIP_VISIBLE_DEVICES=0" not in line


def test_an_empty_builder_says_so():
    assert _builder_line({}) == "(nothing set yet)"
    assert _builder_line({"command": "   "}) == "(nothing set yet)"


def test_a_flag_edit_is_offered(client, app):
    text, actions = app.extensions["pb"].assistant._actions("Try this.\nACTION set --flash-attn off")
    assert actions == [{"verb": "set", "flag": "--flash-attn", "value": "off", "kind": "flag", "model": ""}]
    assert "ACTION" not in text and "Try this." in text


def test_an_unset_needs_no_value(client, app):
    _text, actions = app.extensions["pb"].assistant._actions("ACTION unset --cache-type-k")
    assert actions[0]["verb"] == "unset" and actions[0]["flag"] == "--cache-type-k"


def test_a_flag_that_is_not_a_flag_is_never_a_button(client, app):
    """A flag edit is written into a command line, so only a flag's shape is offered."""
    for bad in ["ACTION set $(curl http://x) y", "ACTION set rm -rf /", "ACTION set `whoami` x",
                "ACTION set --x;ls y", "ACTION set ../../etc/passwd x"]:
        assert app.extensions["pb"].assistant._actions(bad)[1] == [], bad


def test_the_digest_carries_the_builder_for_the_turn(client, app):
    a = app.extensions["pb"].assistant
    assert "BUILDER" not in a.digest()
    a._builder = {"command": "llama-server -m /m/x.gguf"}
    try:
        assert "BUILDER" in a.digest() and "llama-server -m /m/x.gguf" in a.digest()
    finally:
        a._builder = None
    assert "BUILDER" not in a.digest()


def test_an_env_var_can_be_set_unset_and_described(client, app):
    a = app.extensions["pb"].assistant
    _t, actions = a._actions("ACTION set HSA_ENABLE_SDMA 0\nACTION unset GGML_SCHED_N_COPIES\n"
                             "ACTION describe HIP_FORCE_DEV_KERNARG Puts kernel arguments in device memory.\n"
                             "ACTION describe --no-warmup Skips the warm-up run.")
    assert [(x["verb"], x["flag"], x["kind"]) for x in actions] == [
        ("set", "HSA_ENABLE_SDMA", "env"), ("unset", "GGML_SCHED_N_COPIES", "env"),
        ("describe", "HIP_FORCE_DEV_KERNARG", "env"), ("describe", "--no-warmup", "flag")]
    assert actions[2]["value"] == "Puts kernel arguments in device memory."
    for bad in ["ACTION set HSA_X $(id)", "ACTION set HSA_X 'a b'", "ACTION describe HSA_X", "ACTION set lower_case 1"]:
        assert a._actions(bad)[1] == [], bad


def test_the_digest_carries_the_flags_page_notes(client, app):
    a = app.extensions["pb"].assistant
    client.post("/api/flags/env", json={"name": "MY_TUNING", "description": "my own note"})
    a._builder = {"command": "HSA_ENABLE_SDMA=0 MY_TUNING=1 NOT_LISTED=2 llama-server -m /m/x.gguf -fa on"}
    try:
        d = a.digest("what does HIP_FORCE_DEV_KERNARG do?")
    finally:
        a._builder = None
    assert "FLAGS PAGE" in d and "- MY_TUNING: my own note" in d and "- NOT_LISTED: not on the Flags page" in d
    assert "- HSA_ENABLE_SDMA: Use the SDMA copy engines" in d and "- HIP_FORCE_DEV_KERNARG: Put HIP kernel arguments" in d
    assert "--flash-attn" in d


def test_the_flags_page_takes_a_pasted_line_and_descriptions(client):
    r = client.post("/api/flags/import", json={"text": "GGML_SCHED_N_COPIES=1 HIP_VISIBLE_DEVICES=0,1 NEW_ONE=x "
                                                       "llama-server --brand-new-flag 3 -ngl 99"}).get_json()
    assert r["added"] == ["NEW_ONE", "--brand-new-flag"] and set(r["known"]) == {"GGML_SCHED_N_COPIES", "HIP_VISIBLE_DEVICES", "-ngl"}
    assert client.post("/api/flags/env/describe", json={"name": "NEW_ONE", "description": "first"}).get_json()["added"] is False
    assert client.post("/api/flags/env/describe", json={"name": "OTHER_ONE", "description": "second"}).get_json()["added"] is True
    assert client.post("/api/flags/flag/describe", json={"name": "-ngl", "description": "layers on the GPU"}).get_json()["name"] == "n-gpu-layers"
    env = {r["flag"]: r for r in client.get("/api/env-flags").get_json()["rows"]}
    assert env["NEW_ONE"]["description"] == "first" and env["OTHER_ONE"]["description"] == "second"
    flags = {r["flag"]: r for r in client.get("/api/flags").get_json()["rows"]}
    assert flags["n-gpu-layers"]["description"] == "layers on the GPU"


def test_what_is_running_names_the_working_model_with_its_own_speeds(client, app):
    import time

    from src.assistant_tools import loaded_now

    from .fakes import make_engine, serve
    running = [{"model": "qwen3-0.6b-cpu", "state": "ready", "cmd": "x"}, {"model": "coder", "state": "ready", "cmd": "y"},
               {"model": "embed-cpu", "state": "ready", "cmd": "z"}]
    pool = {"router": "pool", "enabled": True, "cards": [], "queue": [], "swaps": [], "models": [
        {"id": "coder", "state": "ready", "size_gib": 62.8, "cards": ["0", "1"], "in_flight": 1,
         "slots": [{"id": 0, "phase": "prefill", "tokens": 30000, "total": 47668, "tokens_per_second": 1155.3}]},
        {"id": "qwen3-0.6b-cpu", "state": "ready", "size_gib": 0.8, "on_cpu": True},
        {"id": "embed-cpu", "state": "ready", "size_gib": 0.7, "on_cpu": True}]}
    a = app.extensions["pb"].assistant
    store = app.extensions["pb"].store
    store.add_requests([{"source_id": 1, "ts": time.time(), "model": "coder", "input_tokens": 47672, "output_tokens": 188,
                         "prompt_per_second": 1076.96, "tokens_per_second": 38.11, "prefill_high_tps": 1907.0, "prefill_low_tps": 1004.0}])
    client.put("/api/settings/assistant", json={"model": "qwen3-0.6b-cpu"})
    client.put("/api/settings/memory", json={"model": "embed-cpu"})
    with serve(make_engine([], [], running=running, pool=pool)) as url:
        client.put("/api/settings/integrations", json={"engine_url": url})
        lines = loaded_now(a._tool_ctx(), "what model is running right now")
    assert lines[1].startswith("- coder (ready, on GPU 0,1)"), "the working model first, not the assistant's own"
    assert "reading a prompt (30000 of 47668 tokens) at 1155 t/s prefill" in lines[1]
    assert "prefill 1077 t/s, fastest 1907 slowest 1004 over 47672 tokens, decode 38.1 t/s" in lines[1]
    assert "[the assistant's own model]" in lines[2] and "on the CPU" in lines[2] and "now: idle" in lines[2]
    assert "about 62.80 GiB" in lines[-1], "CPU models take no VRAM"


def test_a_model_is_found_by_the_words_of_its_name():
    from src.assistant_tools import _fuzzy
    name = "Qwen3-Coder-Next-Q5_K_S-00001-of-00003 qwen3-coder-next-q5_k_s-00001-of-00003"
    assert _fuzzy("list qwen-coder", name) and _fuzzy("qwen coder q5", name) and _fuzzy("show me coder-next", name)
    assert not _fuzzy("list gemma", name) and not _fuzzy("list q5", name), "one short word does not name a model"
    assert not _fuzzy("?", name)


def test_what_is_running_and_a_models_details_are_answered_in_code(client, app, monkeypatch):
    from src import assistant_tools as t
    monkeypatch.setattr(t, "loaded_now", lambda ctx, q: ["LOADED NOW (1 model(s)):", "- coder (ready, on GPU 0,1): now: idle"])
    ctx = app.extensions["pb"].assistant._tool_ctx()
    for q in ("what model is running right now and decode and prefill speeds?", "what's loaded?", "which models are on the cpu"):
        assert t.direct_answer(ctx, q) == "**LOADED NOW (1 model(s))**\n- coder (ready, on GPU 0,1): now: idle", q

    coder = {"model_id": "qwen3-coder-next", "name": "Qwen3-Coder-Next-Q5_K_S", "loaded_seconds": 60, "origin": "file"}
    monkeypatch.setattr(t, "model_details", lambda ctx, q: ["MODEL Qwen3-Coder-Next-Q5_K_S:", "- decode speed 38 to 42 t/s"])
    monkeypatch.setattr(ctx["inventory"], "models_summary", lambda: [coder])
    client.post("/api/profiles", json={"name": "qwencoder-512-2chat", "base_model": "Qwen3-Coder-Next-Q5_K_S",
                                       "launch_command": "llama-server -m /m/Qwen3-Coder-Next-Q5_K_S-00001-of-00003.gguf --port 8080"})
    answer = t.direct_answer(ctx, "list qwen-coder")
    assert answer.startswith("**MODEL Qwen3-Coder-Next-Q5_K_S**") and "38 to 42 t/s" in answer
    assert "profiles of Qwen3-Coder-Next-Q5_K_S: qwencoder-512-2chat" in answer
    assert t.direct_answer(ctx, "?") is None and t.direct_answer(ctx, "why is my prefill slow at 100k tokens?") is None


def test_the_small_model_reads_live_data_and_the_bigger_one_gets_the_memory(client, app, monkeypatch):
    from src.assistant import NEEDS_BIG
    a = app.extensions["pb"].assistant
    monkeypatch.setattr(a, "related", lambda q: ["RELATED RECORDS (closest to the question, from the database):", "- [event] x"])
    assert "RELATED RECORDS" in a.digest("anything", memory=True)
    assert "RELATED RECORDS" not in a.digest("anything", memory=False), "no embedding search for the small model"

    for q in ("why was prefill slow yesterday?", "what settings should I use for the coder?", "compare gemma vs qwen",
              "when did the coder last crash", "recommend a context size"):
        assert NEEDS_BIG.search(q), q
    for q in ("what is loaded", "gpu temperature now", "how much vram is free"):
        assert not NEEDS_BIG.search(q), q
