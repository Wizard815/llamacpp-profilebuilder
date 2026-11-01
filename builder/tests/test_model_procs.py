from src.model_procs import ModelProcs, parse_ps

PS = """  4120 1153024 3605 /usr/local/bin/llama-server --host 0.0.0.0 --port 5853 -m /app/models/e.gguf --embedding
  4188 2301952 120 /usr/local/bin/llama-server --host 0.0.0.0 --port 5842 -m /app/models/q.gguf
  9999 1000 1 bash -c something else
"""


def test_ps_lines_are_read():
    rows = parse_ps(PS)
    assert [(r["pid"], r["rss_kib"], r["cpu_s"]) for r in rows] == [(4120, 1153024, 3605.0), (4188, 2301952, 120.0)]


def test_each_model_gets_its_own_processes_ram_and_cores(monkeypatch):
    import src.model_procs as mp
    calls, clock = [], [1000.0]
    later = PS.replace(" 3605 ", " 3617 ")  # 12 more CPU seconds over 4 s: 3 cores
    runs = iter([PS, later])
    monkeypatch.setattr(mp.time, "time", lambda: clock[0])
    monkeypatch.setattr(mp, "local_ps", lambda: [])
    procs = ModelProcs(run=lambda argv: calls.append(argv) or next(runs))
    running = [{"model": "embed-cpu", "cmd": "/app/bin/remote-llama rocm --port 5853 -m /app/models/e.gguf"},
               {"model": "chat-cpu", "cmd": "/app/bin/remote-llama rocm --port 5842 -m /app/models/q.gguf"},
               {"model": "coder", "cmd": "/app/bin/remote-llama rocm --port 5848 -m /app/models/c.gguf"}]
    first = procs.sample(running)
    assert calls == [["/app/bin/remote-llama", "rocm", "--ps"]], "one call per backend"
    assert first["embed-cpu"] == {"rss_gib": 1.1, "cores": None, "pid": 4120, "where": "rocm"}, "cores need two readings"
    assert "coder" not in first
    clock[0] += 4
    second = procs.sample(running)
    assert second["embed-cpu"]["cores"] == 3.0 and second["chat-cpu"]["cores"] == 0.0
