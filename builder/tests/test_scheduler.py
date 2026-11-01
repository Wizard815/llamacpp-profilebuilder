"""Plan, estimate, the engine's copy, and the pool rules. The engine tests repeat the Go tests
in Argos's internal/router/pool_test.go with the same numbers."""
import os

import pytest
import yaml

from src.db import Store
from src.gguf import read_arch
from .ggufs import kv_str, kv_str_array, kv_u32, kv_u32_array, write_gguf
from src.scheduler import (DEFAULT_PLAN, Engine, PlanError, Plans, State, clean_plan, effective_size,
                           estimate_gib, render_fragment, simulate, write_fragment)


def plan(**kw):
    p = {**DEFAULT_PLAN, **kw}
    p["placement"] = kw.get("placement", "auto")
    return p


def engine(sizes, cards=(31, 31), **kw):
    """Cards of exactly this many GiB each (reserve 0), so the numbers read directly."""
    return Engine(plan(cards=list(cards), reserve_gib=0, **kw), sizes)


# ----- plan -----------------------------------------------------------------------
def test_clean_plan_normalises_and_rejects():
    got = clean_plan({"cards": "32, 32", "persistent": "whisper, embeds\nwhisper", "sizes": {"a": "26.5"},
                      "protect_seconds": "90", "enabled": "yes"})
    assert got == {"cards": [32.0, 32.0], "persistent": ["whisper", "embeds"], "sizes": {"a": 26.5},
                   "protect_seconds": 90, "enabled": True}
    for bad in ({"cards": [32, 0]}, {"reserve_gib": -1}, {"placement": "sideways"}, {"nope": 1},
                {"sizes": {"a": 0}}, {"sizes": []}, {"cards": "x"}):
        with pytest.raises(PlanError):
            clean_plan(bad)


def test_plans_store_round_trip(tmp_path):
    plans = Plans(Store(str(tmp_path / "t.db")))
    assert plans.get() == DEFAULT_PLAN
    plans.update({"cards": [32, 32], "persistent": ["whisper"]})
    plans.update({"reserve_gib": 3})
    got = plans.get()
    assert got["cards"] == [32.0, 32.0] and got["persistent"] == ["whisper"] and got["reserve_gib"] == 3.0
    plans.set_measured("a", 25.456, [12.7, 12.75], load_seconds=41.2)
    assert plans.get()["measured"]["a"]["gib"] == 25.46


def test_effective_size_precedence():
    p = plan(sizes={"a": 20}, measured={"a": {"gib": 25}, "b": {"gib": 8}})
    assert effective_size(p, "a", 30) == (20.0, "plan")
    assert effective_size(p, "b", 30) == (8.0, "measured")
    assert effective_size(p, "c", 30) == (30.0, "estimate")
    assert effective_size(p, "d") == (0.0, "")


# ----- estimate -------------------------------------------------------------------
ARCH = {"arch": "qwen3", "layers": 28, "heads": 16, "kv_heads": 8, "key_len": 128, "value_len": 128,
        "attn_layers": 28, "train_ctx": 40960}


def test_estimate_counts_weights_kv_and_buffers():
    got = estimate_gib(int(0.6 * 2 ** 30), ARCH, 32768, "f16", "f16")
    assert got["kv_gib"] == pytest.approx(3.5)  # 28 layers * 8 heads * 256 * 2 B * 32768 tokens
    assert got["weights_gib"] == pytest.approx(0.6, abs=0.01)
    assert got["gib"] == pytest.approx(0.6 + 3.5 + 1.06, abs=0.02)

    q8 = estimate_gib(0, ARCH, 32768, "q8_0", "q8_0")["kv_gib"]
    assert q8 == pytest.approx(3.5 * 34 / 64, rel=0.01)


def test_estimate_unknowns_are_flagged_not_hidden():
    odd = estimate_gib(0, ARCH, 4096, "q3_zz", "q3_zz")
    assert any("q3_zz" in n for n in odd["notes"])
    assert odd["kv_gib"] > 0
    # the TurboQuant types are known now: smaller than q8_0, and marked approximate
    t3, q8 = estimate_gib(0, ARCH, 4096, "turbo3", "turbo3"), estimate_gib(0, ARCH, 4096, "q8_0", "q8_0")
    assert 0 < t3["kv_gib"] < q8["kv_gib"] and any("approximate" in n for n in t3["notes"])
    assert any("header" in n for n in estimate_gib(10 * 2 ** 30, None, 4096)["notes"])


# ----- gguf reader ----------------------------------------------------------------
def test_gguf_reads_arch_and_stops_before_the_tokenizer(tmp_path):
    path = str(tmp_path / "m.gguf")
    write_gguf(path, [
        kv_str("general.architecture", "llama"), kv_u32("llama.block_count", 32),
        kv_u32("llama.attention.head_count", 32), kv_u32("llama.attention.head_count_kv", 8),
        kv_u32("llama.embedding_length", 4096), kv_u32("llama.context_length", 131072),
        kv_str_array("tokenizer.ggml.tokens", ["a", "b", "c"]),
    ])
    assert read_arch(path) == {"arch": "llama", "layers": 32, "heads": 32, "kv_heads": 8, "key_len": 128,
                               "value_len": 128, "attn_layers": 32, "train_ctx": 131072, "ftype": None,
                               "swa_window": 0, "swa_layers": 0}


def test_gguf_reads_the_file_type(tmp_path):
    path = str(tmp_path / "q.gguf")
    write_gguf(path, [kv_str("general.architecture", "llama"), kv_u32("general.file_type", 15),
                      kv_u32("llama.block_count", 4), kv_u32("llama.attention.head_count", 8),
                      kv_u32("llama.embedding_length", 512), kv_str_array("tokenizer.ggml.tokens", ["a"])])
    assert read_arch(path)["ftype"] == 15


def test_gguf_hybrid_models_count_only_attention_layers(tmp_path):
    path = str(tmp_path / "h.gguf")
    kv_heads = [0, 0, 0, 2] * 10 + [0]  # 41 layers, 10 with a KV cache
    write_gguf(path, [
        kv_str("general.architecture", "qwen35moe"), kv_u32("qwen35moe.block_count", 41),
        kv_u32("qwen35moe.attention.head_count", 16), kv_u32_array("qwen35moe.attention.head_count_kv", kv_heads),
        kv_u32("qwen35moe.attention.key_length", 256), kv_u32("qwen35moe.embedding_length", 2048),
    ])
    got = read_arch(path)
    assert got["attn_layers"] == 10 and got["kv_heads"] == 2 and got["key_len"] == 256

    path2 = str(tmp_path / "i.gguf")
    write_gguf(path2, [
        kv_str("general.architecture", "qwen3next"), kv_u32("qwen3next.block_count", 48),
        kv_u32("qwen3next.attention.head_count", 16), kv_u32("qwen3next.attention.head_count_kv", 2),
        kv_u32("qwen3next.embedding_length", 2048), kv_u32("qwen3next.full_attention_interval", 4),
    ])
    assert read_arch(path2)["attn_layers"] == 12


def test_gguf_bad_files_return_none(tmp_path):
    for name, data in (("empty.gguf", b""), ("junk.gguf", b"NOPE" + b"\0" * 64), ("short.gguf", b"GGUF\x03\0\0\0")):
        (tmp_path / name).write_bytes(data)
        assert read_arch(str(tmp_path / name)) is None
    assert read_arch(str(tmp_path / "missing.gguf")) is None


# ----- the engine's copy ---------------------------------------------------------
def test_fragment_is_the_routing_block_engine_reads():
    p = plan(enabled=True, cards=[32, 32], persistent=["whisper"], protect_seconds=300)
    text, skipped = render_fragment(p, {"a": 26.0, "whisper": 4.0})
    doc = yaml.safe_load(text)
    pool = doc["routing"]["router"]["settings"]["pool"]
    assert doc["routing"]["router"]["use"] == "pool" and skipped == []
    assert pool == {"cards": [32.0, 32.0], "reserveGiB": 2.0, "protectSeconds": 300, "protectWaits": True, "placement": "auto",
                    "persistent": ["whisper"], "sizes": {"a": 26.0, "whisper": 4.0}}
    assert text.startswith("# Written by Argos")


def test_fragment_skips_models_engine_does_not_know():
    p = plan(enabled=True, persistent=["whisper", "ghost"])
    text, skipped = render_fragment(p, {"a": 10.0, "ghost": 3.0}, known={"a", "whisper"})
    pool = yaml.safe_load(text)["routing"]["router"]["settings"]["pool"]
    assert pool["persistent"] == ["whisper"] and pool["sizes"] == {"a": 10.0}
    assert skipped == ["ghost"]


def test_disabled_plan_writes_no_routing():
    text, skipped = render_fragment(plan(enabled=False), {"a": 1.0})
    assert yaml.safe_load(text) is None, "comments only"
    assert "turned off" in text and skipped == []


def test_write_fragment_is_atomic_and_idempotent(tmp_path):
    path = str(tmp_path / "config.d" / "scheduler.generated.yaml")
    assert write_fragment(path, "a: 1\n") is True
    assert write_fragment(path, "a: 1\n") is False, "unchanged content is not rewritten"
    assert write_fragment(path, "a: 2\n") is True
    assert open(path).read() == "a: 2\n"
    assert os.listdir(os.path.dirname(path)) == ["scheduler.generated.yaml"], "no temp files left"


@pytest.mark.skipif(not os.environ.get("ARGOS_BIN"), reason="set ARGOS_BIN to an Argos build")
def test_engine_accepts_the_generated_fragment(tmp_path):
    """The YAML the engine reads, merged with a real config, passes the engine's own validation."""
    import subprocess

    main = tmp_path / "config.yaml"
    main.write_text("models:\n  a:\n    cmd: echo a\n    proxy: http://localhost:9000\n"
                    "  whisper:\n    cmd: echo w\n    proxy: http://localhost:9001\n")
    confd = tmp_path / "config.d"
    confd.mkdir()
    text, _ = render_fragment(plan(enabled=True, cards=[32, 32], persistent=["whisper"]),
                              {"a": 26.0, "whisper": 4.0})
    (confd / "scheduler.generated.yaml").write_text(text)
    res = subprocess.run([os.environ["ARGOS_BIN"], "-config", str(main), "-config-dir", str(confd), "-validate"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


# ----- engine: the Go pool tests, same numbers ----------------------------------------
def start(eng, st, target, *running):
    """Commit a swap like the router does and mark the target running."""
    evict = eng.eviction_for(target, list(running) + [target], st)
    left = [r for r in running if r not in evict]
    ok, cards = eng.fits(target, left, st)
    st.placed[target] = cards or []
    for e in evict:
        st.placed.pop(e, None)
    return st.placed[target]


def test_small_model_loads_next_to_big_model():
    eng = Engine(plan(cards=[], reserve_gib=0), {"big": 50, "tiny": 1}, detected_gib=[31, 31])
    eng.cards = []  # total-only pool, like placement off
    eng.budget = 62
    assert eng.eviction_for("tiny", ["big"], State()) == []


def test_evicts_only_what_is_needed_and_prefers_idle():
    eng = Engine(plan(cards=[31, 31], reserve_gib=0, placement="off"), {"a": 20, "b": 20, "c": 20, "new": 20})
    st = State()
    st.now = 4 * 3600
    st.last_used = {"a": st.now - 3600, "b": st.now - 7200, "c": st.now - 10800}
    assert eng.eviction_for("new", ["a", "b", "c"], st) == ["c"]


def test_busy_last_and_persistent_never():
    eng = Engine(plan(cards=[20, 20], reserve_gib=0, placement="off", persistent=["whisper"]),
                 {"busy": 20, "recent": 20, "new": 20, "whisper": 2, "llm": 30})
    st = State()
    st.now = 7200
    st.busy["busy"] = 1
    st.last_used = {"busy": 0, "recent": st.now - 60}
    assert eng.eviction_for("new", ["busy", "recent"], st) == ["recent"]

    eng2 = Engine(plan(cards=[20, 20], reserve_gib=0, placement="off", persistent=["whisper"]),
                  {"whisper": 2, "llm": 30, "new": 30})
    assert eng2.eviction_for("new", ["llm", "whisper"], State()) == ["llm"]


def test_prefers_cheap_reload_and_puts_back_unneeded():
    eng = Engine(plan(cards=[20, 20], reserve_gib=0, placement="off"), {"slow": 20, "fast": 20, "new": 20})
    st = State()
    st.now = 7200
    st.last_used = {"slow": 3600, "fast": 3600}
    st.load_time = {"slow": 300, "fast": 5}
    assert eng.eviction_for("new", ["fast", "slow"], st) == ["fast"]

    eng2 = Engine(plan(cards=[20, 20], reserve_gib=0, placement="off"), {"small": 4, "large": 30, "new": 30})
    st2 = State()
    st2.now = 7200
    assert eng2.eviction_for("new", ["large", "small"], st2) == ["large"]


def test_unknown_size_runs_alone():
    eng = Engine(plan(cards=[31, 31], reserve_gib=0, placement="off"), {"a": 4, "b": 4, "mystery": 0})
    assert eng.eviction_for("mystery", ["a", "b"], State()) == ["a", "b"]
    assert eng.eviction_for("a", ["mystery"], State()) == ["mystery"]


def test_places_models_on_separate_cards():
    eng, st = engine({"a": 26, "s": 6, "b": 20}), State()
    assert start(eng, st, "a") == ["0"]
    assert start(eng, st, "s", "a") == ["1"], "the empty card wins over sharing with a"
    assert start(eng, st, "b", "a", "s") == ["1"]
    assert eng.eviction_for("b", ["a", "s"], st) == []


def test_spreads_a_model_bigger_than_a_card():
    eng, st = engine({"huge": 50, "tiny": 1}), State()
    assert start(eng, st, "huge") == ["0", "1"]
    assert eng.eviction_for("tiny", ["huge"], st) == []
    assert start(eng, st, "tiny", "huge") == ["0"]


def test_fragmentation_evicts_for_one_card():
    eng, st = engine({"a": 20, "b": 20, "c": 28}), State()
    start(eng, st, "a")
    start(eng, st, "b", "a")
    st.now = 4 * 3600
    st.last_used = {"a": st.now - 7200, "b": st.now - 3600}
    assert eng.eviction_for("c", ["a", "b"], st) == ["a"], "22 GiB free in total but 11 per card"


def test_unknown_size_takes_all_cards():
    eng, st = engine({"a": 4, "mystery": 0}), State()
    start(eng, st, "a")
    assert eng.eviction_for("mystery", ["a"], st) == ["a"]
    assert start(eng, st, "mystery") == ["0", "1"]


# ----- the simulator: the worked example from the design discussion ----------------------
def test_simulation_queues_b_behind_a_then_evicts_only_a():
    """A(26) B(20) S1(6) S2(6) in a 40 GiB pool: B waits for A, the small ones fit beside A."""
    eng = Engine(plan(cards=[20, 20], reserve_gib=0, placement="off", persistent=["s1", "s2"]),
                 {"a": 26, "b": 20, "s1": 6, "s2": 6})
    steps = [{"op": "request", "model": m} for m in ("a", "b", "s1", "s2")]
    steps += [{"op": "finish", "model": "a"}]
    res = simulate(eng, steps)

    assert [r["result"] for r in res[:4]] == ["loaded", "queued", "loaded", "loaded"]
    assert "waits for a" in res[1]["note"]
    assert res[1]["state"]["queue"] == ["b"]

    last = res[4]
    assert last["result"] == "finished"
    assert last["drained"][0].startswith("b: loaded") and "evicting a" in last["drained"][0]
    assert sorted(r["id"] for r in last["state"]["running"]) == ["b", "s1", "s2"]
    assert last["state"]["queue"] == []


def test_simulation_places_each_request_on_a_card():
    res = simulate(engine({"a": 26, "b": 20, "s": 6}), [{"op": "request", "model": m} for m in ("a", "b", "s")])
    assert [r["cards"] for r in res] == [["0"], ["1"], ["1"]]
    cards = {c["id"]: c for c in res[2]["state"]["cards"]}
    assert cards["0"]["models"] == ["a"] and cards["1"]["models"] == ["b", "s"]
    assert cards["1"]["used_gib"] == 26.0 and cards["1"]["free_gib"] == 5.0


def test_simulation_protect_window_and_errors():
    eng = engine({"a": 20, "b": 20, "c": 28}, cards=(30, 30), protect_seconds=600)
    res = simulate(eng, [
        {"op": "request", "model": "a"}, {"op": "finish", "model": "a"},
        {"op": "request", "model": "b"}, {"op": "finish", "model": "b"},
        {"op": "wait", "minutes": 30},
        {"op": "request", "model": "c"},
        {"op": "request", "model": "ghost"}, {"op": "finish", "model": "a"}, {"op": "bogus"},
    ])
    assert res[5]["result"] == "loaded" and res[5]["evicted"] == ["a"], "both idle: the cheaper one goes first"
    assert [r["result"] for r in res[6:]] == ["error", "error", "error"]


def test_simulation_serves_a_loaded_model_without_loading_again():
    eng = engine({"a": 10})
    res = simulate(eng, [{"op": "request", "model": "a"}, {"op": "request", "model": "a"}])
    assert [r["result"] for r in res] == ["loaded", "served"]
    assert res[1]["state"]["running"][0]["busy"] == 2


def test_custom_cache_types_are_bits_per_value_and_override_the_table():
    from src import scheduler as S
    assert S.parse_cache_types("turbo5=3.0, mykv=4.25; junk, x=abc, big=99") == {"turbo5": 0.375, "mykv": 0.53125}
    try:
        S.set_custom_cache_types("q3_zz=3")
        known = estimate_gib(0, ARCH, 4096, "q3_zz", "q3_zz")
        assert not any("unknown" in n for n in known["notes"])
        assert known["kv_gib"] < estimate_gib(0, ARCH, 4096, "q8_0", "q8_0")["kv_gib"]
    finally:
        S.set_custom_cache_types("")


def test_protected_models_stay_loaded_unless_turned_off():
    from src.scheduler import DEFAULT_PLAN, clean_plan
    assert DEFAULT_PLAN["protect_waits"] is True
    assert clean_plan({"protect_waits": "false"})["protect_waits"] is False
