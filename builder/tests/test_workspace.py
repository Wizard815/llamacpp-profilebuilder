"""Always-set env and flags, notes with history, layout, the file browser, extra-file downloads,
profile change review and apply, short log ranges, and asking the assistant about log lines."""
import json
import os
import time

import pytest

from src import dialect
from src.always import apply_always
from src.routes.workspace import diff_commands

from .conftest import fake_command
from .fakes import make_hub, make_engine, serve
from .test_inventory import fake_openai, point_assistant_at


# ----- always-set -------------------------------------------------------------------------------
ALWAYS = {"env": "HSA_OVERRIDE_GFX_VERSION=9.0.6\n# comment\nbad line\nHIP_VISIBLE_DEVICES=0", "flags": "-lm dio --no-warmup"}


def test_always_adds_only_what_the_command_lacks():
    cmd, added = apply_always("HIP_VISIBLE_DEVICES=1 llama-server -m /m/a.gguf -lm mmap", ALWAYS)
    assert added == ["HSA_OVERRIDE_GFX_VERSION=9.0.6", "--no-warmup"], "the profile's own GPU and load mode win"
    assert "HIP_VISIBLE_DEVICES=1" in cmd and "-lm mmap" in cmd and "--no-warmup" in cmd
    assert apply_always("llama-server 'broken", ALWAYS) == ("llama-server 'broken", [])
    assert apply_always("llama-server -m a.gguf", {"env": "", "flags": ""}) == ("llama-server -m a.gguf", [])


def test_always_reaches_the_builder_launches_and_export(app, client, tmp_path):
    client.put("/api/settings/always", json=ALWAYS)
    j = client.post("/api/command/parse", json={"command": "llama-server -m /m/a.gguf", "format": "plain"}).get_json()
    assert j["always_added"] == ["HSA_OVERRIDE_GFX_VERSION=9.0.6", "HIP_VISIBLE_DEVICES=0", "-lm dio", "--no-warmup"]
    assert "--no-warmup" not in j["command"], "the builder shows them, it does not write them into the profile"
    assert j["always_env"] == {"HSA_OVERRIDE_GFX_VERSION": "9.0.6", "HIP_VISIBLE_DEVICES": "0"}
    assert [x["flag"] for x in j["always_args"]] == ["-lm", "--no-warmup"] and j["always_args"][0]["value"] == "dio"
    assert j["run_command"].startswith("HSA_OVERRIDE_GFX_VERSION=9.0.6 HIP_VISIBLE_DEVICES=0 llama-server")
    assert j["run_command"].endswith("--no-warmup") and "--no-warmup" in j["run_multiline"], "Copy gives what runs"
    own = client.post("/api/command/build", json={"command": "HIP_VISIBLE_DEVICES=1 llama-server -m /m/a.gguf -lm mmap",
                                                   "format": "plain"}).get_json()
    assert own["always_env"] == {"HSA_OVERRIDE_GFX_VERSION": "9.0.6"} and [x["flag"] for x in own["always_args"]] == ["--no-warmup"]

    client.post("/api/profiles", json={"name": "p", "base_model": "a", "launch_command": "llama-server --model /m/a.gguf --port 8080"})
    state = lambda: client.get("/api/cookbook/state").get_json()["presets"]  # noqa: E731 - what the engine reads
    ours = [x for x in state() if (x.get("fields") or {}).get("source") == "argos"]
    cmd = ours[0]["cmd"]
    assert "HSA_OVERRIDE_GFX_VERSION=9.0.6" in cmd and "--no-warmup" in cmd, "the engine gets them"

    r = client.post("/api/launches", json={"command": fake_command()}).get_json()
    assert "--no-warmup" in r["launch"]["command"] and r["launch"]["command"].startswith("HSA_OVERRIDE")
    client.post(f"/api/launches/{r['launch']['launch_id']}/stop")

    client.put("/api/settings/always", json={"on_export": False})
    client.post("/api/profiles", json={"name": "q", "base_model": "a", "launch_command": "llama-server --model /m/a.gguf --port 8080"})
    assert all("--no-warmup" not in p["cmd"] for p in state() if (p.get("fields") or {}).get("source") == "argos")


# ----- notes ------------------------------------------------------------------------------------
def test_notes_are_one_set_for_every_page(app, client):
    n = client.post("/api/notes", json={"title": "try", "body": "v1", "color": "nope"}).get_json()["note"]
    assert n["color"] == "yellow" and n["is_open"] is True, "a new note is open, so it shows on every page"
    assert "view" not in n and "versions" not in n
    other = client.post("/api/notes", json={"body": "second"}).get_json()["note"]
    assert [x["id"] for x in client.get("/api/notes").get_json()["notes"]] == [n["id"], other["id"]]
    assert [x["id"] for x in client.get("/api/notes?view=logs").get_json()["notes"]] == [n["id"], other["id"]], "no per-page notes"

    r = client.put(f"/api/notes/{n['id']}", json={"body": "v2", "x": 40, "y": 60, "w": 300, "h": 220}).get_json()["note"]
    assert (r["body"], r["x"], r["y"], r["w"], r["h"]) == ("v2", 40, 60, 300, 220)
    assert client.put(f"/api/notes/{n['id']}", json={"x": "left"}).status_code == 400
    assert client.put("/api/notes/999999", json={"body": "x"}).status_code == 404


def test_minimize_keeps_a_note_in_the_menu_and_open_brings_it_back(client):
    n = client.post("/api/notes", json={"title": "t", "body": "b"}).get_json()["note"]
    m = client.put(f"/api/notes/{n['id']}", json={"is_open": False}).get_json()["note"]
    assert m["is_open"] is False and m["body"] == "b"
    listed = client.get("/api/notes").get_json()["notes"]
    assert [(x["id"], x["is_open"]) for x in listed] == [(n["id"], False)], "minimized notes are still listed, for the menu"
    assert client.put(f"/api/notes/{n['id']}", json={"is_open": True}).get_json()["note"]["is_open"] is True
    hidden = client.post("/api/notes", json={"body": "start minimized", "is_open": False}).get_json()["note"]
    assert hidden["is_open"] is False


def test_deleting_a_note_is_final(app, client):
    n = client.post("/api/notes", json={"body": "gone soon"}).get_json()["note"]
    assert client.delete(f"/api/notes/{n['id']}").get_json()["ok"]
    assert client.get("/api/notes").get_json()["notes"] == []
    assert client.delete(f"/api/notes/{n['id']}").status_code == 404
    assert app.extensions["pb"].store.get_note(n["id"]) is None
    assert client.get(f"/api/notes/{n['id']}/history").status_code in (404, 405), "there is no history to bring it back from"


@pytest.mark.skipif(dialect.is_postgres(dialect.url()),
                    reason="there is no old SQLite file to upgrade inside a Postgres database")
def test_old_notes_are_migrated(tmp_path):
    import sqlite3
    from src.db import Store
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, view TEXT NOT NULL, title TEXT DEFAULT '', body TEXT DEFAULT '',
            color TEXT DEFAULT 'yellow', created_at REAL NOT NULL, updated_at REAL NOT NULL, deleted INTEGER DEFAULT 0,
            x REAL, y REAL, w REAL, h REAL, collapsed INTEGER DEFAULT 0);
        CREATE TABLE note_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER NOT NULL, ts REAL NOT NULL, title TEXT, body TEXT);
        INSERT INTO notes (view, title, body, created_at, updated_at, collapsed) VALUES ('builder', 'open', 'a', 1, 1, 0);
        INSERT INTO notes (view, title, body, created_at, updated_at, collapsed) VALUES ('logs', 'small', 'b', 2, 2, 1);
        INSERT INTO notes (view, title, body, created_at, updated_at, deleted) VALUES ('perf', 'deleted', 'c', 3, 3, 1);
    """)
    con.commit()
    con.close()
    notes = Store(path).list_notes()
    assert [(n["title"], n["is_open"]) for n in notes] == [("open", True), ("small", False)], "deleted ones are purged, minimized stay in the menu"


# ----- layout -----------------------------------------------------------------------------------
def test_layout_round_trip(client):
    assert client.get("/api/layout").get_json()["layout"] == {}
    client.put("/api/layout", json={"builder/main": ["b", "a"]})
    client.put("/api/layout", json={"logs/gpu": ["x"]})
    assert client.get("/api/layout").get_json()["layout"] == {"builder/main": ["b", "a"], "logs/gpu": ["x"]}
    assert client.put("/api/layout", json={"k": "not a list"}).status_code == 400


# ----- the models folder ------------------------------------------------------------------------
def test_file_tree_and_search_stay_inside_the_models_folder(app, client):
    root = app.config["MODELS_DIR"]
    os.makedirs(os.path.join(root, "qwen", "sub"), exist_ok=True)
    for p in ("qwen/q-Q4_K_M.gguf", "qwen/mmproj-F16.gguf", "qwen/readme.md", "top.gguf", ".hidden.gguf"):
        open(os.path.join(root, p), "wb").write(b"1234")
    j = client.get("/api/files").get_json()
    assert [(e["name"], e["dir"]) for e in j["entries"]] == [("qwen", True), ("top.gguf", False)] and j["parent"] is None
    j = client.get("/api/files?dir=qwen").get_json()
    assert [e["name"] for e in j["entries"]] == ["sub", "mmproj-F16.gguf", "q-Q4_K_M.gguf"] and j["parent"] == "."
    assert j["entries"][1]["size"] == 4 and j["entries"][1]["abs"].endswith("qwen/mmproj-F16.gguf")
    assert client.get("/api/files?dir=../..").status_code == 400
    assert client.get("/api/files?dir=nope").status_code == 404
    assert [r["path"] for r in client.get("/api/files/search?q=mmproj").get_json()["results"]] == ["qwen/mmproj-F16.gguf"]
    assert client.get("/api/files/search?q=x").status_code == 400


def test_extra_file_downloads_into_the_models_own_folder(app, client):
    root = app.config["MODELS_DIR"]
    os.makedirs(os.path.join(root, "gemma"), exist_ok=True)
    open(os.path.join(root, "gemma", "gemma-3-12b-Q4_K_M.gguf"), "wb").write(b"GGUF")
    files = {"mmproj-F16.gguf": b"x" * 2048}
    with serve(make_hub(files, repo="org/gemma-GGUF")) as url:
        client.put("/api/settings/downloads", json={"hf_endpoint": url})
        assert client.post("/api/catalog/download", json={"kind": "mmproj"}).status_code == 400
        assert client.post("/api/catalog/download", json={"kind": "mmproj", "base_model": "gemma-3-12b-q4_k_m",
                                                           "repo": "org/gemma-GGUF", "file": "nope.gguf"}).status_code == 400
        r = client.post("/api/catalog/download", json={"kind": "mmproj", "base_model": "gemma-3-12b-q4_k_m",
                                                        "repo": "org/gemma-GGUF", "file": "mmproj-F16.gguf"}).get_json()
        assert r["ok"] and r["file_path"] == os.path.join(root, "gemma", "mmproj-F16.gguf")
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(r["file_path"]):
            time.sleep(0.05)
    assert open(r["file_path"], "rb").read() == files["mmproj-F16.gguf"]
    entry = [f for f in client.get("/api/catalog?kind=mmproj").get_json()["files"] if f["id"] == r["id"]][0]
    assert entry["hf_repo"] == "org/gemma-GGUF" and entry["base_model"] == "gemma-3-12b-q4_k_m"


# ----- changing a profile -----------------------------------------------------------------------
def test_diff_is_flag_by_flag_and_ignores_spelling():
    got = diff_commands("A=1 llama-server -m /m/a.gguf -c 4096 -ngl 99 --jinja",
                        "A=2 B=3 llama-server --model /m/a.gguf --ctx-size 8192 -ngl 99 -fa on")
    by = {(c["name"], c["change"]): c for c in got}
    assert by[("A", "changed")]["before"] == "1" and by[("A", "changed")]["after"] == "2" and by[("A", "changed")]["env"]
    assert ("B", "added") in by and ("--ctx-size", "changed") in by and ("-fa", "added") in by and ("--jinja", "removed") in by
    assert not any(c["name"] in ("--model", "-m", "-ngl") for c in got), "same value in another spelling is no change"
    assert diff_commands("llama-server -c 1", "llama-server -c 1") == []


def test_review_then_apply_reloads_only_that_model(app, client):
    p = client.post("/api/profiles", json={"name": "Fast", "base_model": "a",
                                           "launch_command": "llama-server --model /m/a.gguf --port 8080 -c 4096"}).get_json()["profile"]
    calls = []
    running = [{"model": "a--fast", "state": "ready", "cmd": "x"}, {"model": "other", "state": "ready", "cmd": "y"}]
    with serve(make_engine([], [], running=running, calls=calls)) as url:
        client.put("/api/settings/integrations", json={"engine_url": url})
        d = client.post("/api/profiles/diff", json={"profile_id": p["profile_id"], "name": "Fast",
                                                    "launch_command": "llama-server --model /m/a.gguf --port 8080 -c 8192"}).get_json()
        assert d["changes"] == [{"name": "-c", "before": "4096", "after": "8192", "change": "changed", "env": False}]
        assert d["variant_id"] == "a--fast" and d["running"] is True and d["existing"]

        new = client.post("/api/profiles/diff", json={"launch_command": "llama-server -m /m/b.gguf"}).get_json()
        assert new["existing"] is False and new["variant_id"] == "" and new["running"] is None

        client.post("/api/profiles", json={"profile_id": p["profile_id"], "name": "Fast", "base_model": "a",
                                           "launch_command": "llama-server --model /m/a.gguf --port 8080 -c 8192"})
        r = client.post(f"/api/profiles/{p['profile_id']}/apply", json={"load": True, "delay_s": 0}).get_json()
        assert r["refresh"] == "ok" and r["load"] == "started" and r["variant_id"] == "a--fast"
        deadline = time.time() + 5
        while time.time() < deadline and ("GET", "/upstream/a--fast/health") not in calls:
            time.sleep(0.05)
    assert ("POST", "/api/odysseus/refresh") in calls and ("GET", "/upstream/a--fast/health") in calls
    assert not any("other" in path for _, path in calls), "nothing is done to the other model"
    assert client.post("/api/profiles/nope/apply", json={}).status_code == 404


def test_apply_without_engine(client):
    p = client.post("/api/profiles", json={"name": "x", "base_model": "a", "launch_command": "llama-server --model /m/a.gguf"}).get_json()["profile"]
    client.put("/api/settings/integrations", json={"engine_url": ""})
    r = client.post(f"/api/profiles/{p['profile_id']}/apply", json={"load": True}).get_json()
    assert r["exported"] and r["refresh"] == "no Argos URL set" and r["load"] == "skipped"


# ----- logs -------------------------------------------------------------------------------------
def test_short_log_ranges(app, client):
    now = time.time()
    app.extensions["pb"].store.add_gpu_samples([{"ts": now - 20, "gpu": 0, "name": "g", "util": 50, "mem_used_mb": 1, "mem_total_mb": 2,
                                                 "temp_c": 1, "power_w": 1, "source": "t"}])
    app.extensions["pb"].store.add_requests([{"source_id": 1, "ts": now - 200, "model": "m", "status": 200},
                                             {"source_id": 2, "ts": now - 20, "model": "m", "status": 500}])
    for rng in ("30s", "5m", "10m", "30m", "1h"):
        assert client.get(f"/api/gpu/series?range={rng}").get_json()["ok"]
    j = client.get("/api/gpu/series?range=30s").get_json()
    assert j["bucket"] == 2 and len(j["rows"]) == 1
    assert [r["status"] for r in client.get("/api/requests?range=30s").get_json()["requests"]] == [500]
    assert len(client.get("/api/requests?range=5m").get_json()["requests"]) == 2


def test_ask_the_assistant_about_log_lines(app, client):
    assert client.post("/api/assistant/ask", json={}).status_code == 400
    handler, calls = fake_openai(["The loads failed because GPU 1 ran out of memory."])
    with serve(handler) as url:
        point_assistant_at(client, url)
        r = client.post("/api/assistant/ask", json={"question": "why did it fail?", "title": "Events",
                                                    "lines": ["12:00 error a exited: out of memory"] * 3}).get_json()
    assert r["answer"].startswith("The loads failed")
    sent = calls[0]["messages"]
    assert "LIVE DATA" in sent[0]["content"] and "out of memory" in sent[1]["content"] and "QUESTION: why did it fail?" in sent[1]["content"]
    assert client.get("/api/assistant").get_json()["messages"] == [], "a one-off question stays out of the chat"

    client.put("/api/settings/assistant", json={"mode": "endpoint", "endpoint": "http://127.0.0.1:9/v1", "model": "m"})
    assert client.post("/api/assistant/ask", json={"question": "x"}).status_code == 502


def test_digest_compares_models(app, client):
    now = time.time()
    app.extensions["pb"].store.add_requests([{"source_id": i, "ts": now - 100 - i, "model": m, "tokens_per_second": tg, "prompt_per_second": 500}
                                             for i, (m, tg) in enumerate([("a", 40.0), ("a", 42.0), ("b", 60.0)])])
    d = client.get("/api/assistant/digest").get_json()["digest"]
    assert "Model speeds last 2 h (compare models): b: gen 60.0 t/s" in d and "a: gen 41.0 t/s" in d


def test_benchmark_runs_of_engine_models_are_named_by_their_profile(app, client):
    store = app.extensions["pb"].store
    client.put("/api/settings/always", json={"env": "HSA_OVERRIDE_GFX_VERSION=9.0.6", "flags": "--no-warmup"})
    client.post("/api/profiles", json={"name": "fast 32k", "base_model": "a",
                                       "launch_command": "llama-server -m /m/a.gguf -c 32768 -fa on --port 8080"})
    run = {"created_at": 1.0, "base_model": "a", "profile_name": "", "success": 1, "tokens_per_second": 9.0}
    # the engine ran the profile with Always set added and its own port: still that profile
    store.add_run({**run, "run_id": "r1", "launch_id": "engine:a",
                   "launch_command": "HSA_OVERRIDE_GFX_VERSION=9.0.6 llama-server -m /m/a.gguf -c 32768 -fa on --port 5801 --no-warmup"})
    store.add_run({**run, "run_id": "r2", "launch_id": "engine:a--long", "launch_command": "llama-server -m /m/a.gguf -c 131072"})
    store.add_run({**run, "run_id": "r3", "launch_id": "engine:a", "launch_command": "llama-server -m /m/a.gguf -c 4096"})
    names = {r["run_id"]: r["profile_name"] for r in client.get("/api/runs").get_json()["runs"]}
    assert names == {"r1": "fast 32k", "r2": "long", "r3": ""}, "r3 is the model's own command"


def test_env_presets_are_linked_not_copied(app, client):
    from src.always import apply_always

    presets = client.get("/api/env-presets").get_json()["presets"]
    assert [p["name"] for p in presets] == ["Tensor Environment"], "the starter preset is there"
    assert "GGML_CUDA_ALLREDUCE=internal" in presets[0]["env"]

    # a profile links to it; its own value wins over the preset's
    cmd = "ARGOS_PRESETS=tensor-environment HSA_ENABLE_SDMA=1 llama-server -m /m/a.gguf"
    full, added = apply_always(cmd, {})
    assert "ARGOS_PRESETS" not in full and "HIP_FORCE_DEV_KERNARG=1" in full and "HSA_ENABLE_SDMA=1" in full
    assert "HSA_ENABLE_SDMA=0" not in full and "HSA_ENABLE_SDMA=0" not in added

    j = client.post("/api/command/parse", json={"command": cmd, "format": "plain"}).get_json()
    assert j["presets_linked"] == [{"id": "tensor-environment", "name": "Tensor Environment", "missing": False}]
    assert j["preset_env"] == {"HIP_FORCE_DEV_KERNARG": "1", "HSA_FORCE_FINE_GRAIN_PCIE": "1", "GGML_CUDA_ALLREDUCE": "internal"}
    assert "ARGOS_PRESETS=tensor-environment" in j["command"], "the profile keeps the link"
    assert "HIP_FORCE_DEV_KERNARG=1" in j["run_command"] and "ARGOS_PRESETS" not in j["run_command"]

    # editing the preset changes what every linked profile runs, the engine's commands included
    client.post("/api/profiles", json={"name": "tp", "base_model": "a", "launch_command": cmd + " --port 8080"})
    assert client.put("/api/env-presets/tensor-environment", json={"name": "Tensor Environment", "env": "HIP_FORCE_DEV_KERNARG=1",
                                                                  "flags": "-sm tensor"}).status_code == 200
    state = client.get("/api/cookbook/state").get_json()["presets"]
    ours = next(x for x in state if (x.get("fields") or {}).get("source") == "argos")["cmd"]
    assert "-sm tensor" in ours and "HSA_FORCE_FINE_GRAIN_PCIE" not in ours and "ARGOS_PRESETS" not in ours
    assert client.get("/api/env-presets").get_json()["presets"][0]["profiles"] == ["tp"]

    assert client.put("/api/env-presets/Bad Id", json={"name": "x", "env": "A=1"}).status_code == 400
    assert client.put("/api/env-presets/empty", json={"name": "x", "env": ""}).status_code == 400
    assert client.delete("/api/env-presets/tensor-environment").status_code == 200
    gone = client.post("/api/command/parse", json={"command": cmd, "format": "plain"}).get_json()
    assert gone["presets_linked"][0]["missing"] is True and gone["preset_env"] == {}
    assert client.get("/api/env-presets").get_json()["presets"] == [], "deleted stays deleted: the starter is offered once"


def test_an_env_preset_is_applied_as_editable_values(client):
    cmd = "HSA_ENABLE_SDMA=1 llama-server -m /m/a.gguf -ngl 99"
    r = client.post("/api/env-presets/tensor-environment/apply", json={"command": cmd, "part": "env"}).get_json()
    assert "HIP_FORCE_DEV_KERNARG=1" in r["command"] and "HSA_ENABLE_SDMA=1" in r["command"], "what the profile sets stays"
    assert "HSA_ENABLE_SDMA=1" not in r["added"] and "GGML_CUDA_ALLREDUCE=internal" in r["added"]
    assert "ARGOS_PRESETS" not in r["command"], "the values are the profile's own now, not a link"
    j = client.post("/api/command/parse", json={"command": r["command"], "format": "plain"}).get_json()
    assert j["env"]["GGML_CUDA_ALLREDUCE"] == "internal" and j["preset_env"] == {}

    client.put("/api/env-presets/split", json={"name": "Split", "env": "A_B=1", "flags": "-sm tensor --no-warmup"})
    only_flags = client.post("/api/env-presets/split/apply", json={"command": cmd, "part": "flags"}).get_json()
    assert "-sm tensor" in only_flags["command"] and "A_B=1" not in only_flags["command"]

    # an older profile that links to a preset: Make editable brings its values in and drops the link
    linked = "ARGOS_PRESETS=tensor-environment,split llama-server -m /m/a.gguf"
    out = client.post("/api/env-presets/tensor-environment/apply", json={"command": linked, "part": "all"}).get_json()["command"]
    assert "ARGOS_PRESETS=split" in out and "HIP_FORCE_DEV_KERNARG=1" in out
    assert client.post("/api/env-presets/nope/apply", json={"command": cmd}).status_code == 404
