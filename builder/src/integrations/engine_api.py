"""Talk to the Argos engine running in this container: its loaded models, GPUs, pool and model list."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


def _get(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def running(base_url: str, timeout: float = 3.0) -> list[dict]:
    """The engine's ``/running``: every model that is not stopped, with its state and command. Raises when unreachable."""
    data = _get(base_url.rstrip("/") + "/running", timeout)
    if not isinstance(data, dict) or not isinstance(data.get("running"), list):
        raise ValueError("the engine did not return a running list")
    return [m for m in data["running"] if isinstance(m, dict) and m.get("model")]


def hardware(base_url: str, timeout: float = 5.0) -> dict:
    """The engine's ``/api/hardware`` snapshot (GPUs, CPU, memory, environment)."""
    data = _get(base_url.rstrip("/") + "/api/hardware", timeout)
    if not isinstance(data, dict):
        raise ValueError("the engine did not return JSON from /api/hardware")
    return data


def version(base_url: str, timeout: float = 3.0) -> dict:
    data = _get(base_url.rstrip("/") + "/api/version", timeout)
    return data if isinstance(data, dict) else {}


def _post(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        text = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def odysseus_refresh(base_url: str, timeout: float = 30.0) -> Any:
    """Ask the engine to re-read the profiles now instead of at its next refresh."""
    return _post(base_url.rstrip("/") + "/api/odysseus/refresh", timeout)


def model_profile(base_url: str, model_id: str, timeout: float = 3.0) -> dict:
    """Which saved profile the engine runs for a model's own name: ``{"labels": [...], "chosen": "..."}``."""
    return _get(base_url.rstrip("/") + f"/api/odysseus/model/{urllib.parse.quote(model_id, safe='')}/profile", timeout)


def set_model_profile(base_url: str, model_id: str, label: Optional[str], timeout: float = 10.0) -> Any:
    """Make requests for ``model_id`` run the variant ``label`` (None: the model's own command again)."""
    req = urllib.request.Request(base_url.rstrip("/") + f"/api/odysseus/model/{urllib.parse.quote(model_id, safe='')}/profile",
                                 data=json.dumps({"label": label}).encode(), method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def load_model(base_url: str, model_id: str, timeout: float = 900.0) -> None:
    """Make the engine load one model (its /upstream route starts it); other models are not touched."""
    _get(base_url.rstrip("/") + f"/upstream/{urllib.parse.quote(model_id, safe='')}/health", timeout)


def request_next(base_url: str, inflight_id: str, timeout: float = 5.0) -> None:
    """Send a waiting request on first: to the front of its model's queue, or out of an answer-first hold."""
    _post(base_url.rstrip("/") + f"/api/inflight/{urllib.parse.quote(inflight_id, safe='')}/next", timeout)


def request_cancel(base_url: str, inflight_id: str, timeout: float = 5.0) -> None:
    """Cancel a running or waiting request: the client gets an error, the model stops working on it."""
    _post(base_url.rstrip("/") + f"/api/inflight/{urllib.parse.quote(inflight_id, safe='')}/cancel", timeout)


def request_hold(base_url: str, inflight_id: str, hold: bool, timeout: float = 5.0) -> None:
    """Hold a waiting request so it makes no model unload, or let it go on (hold False)."""
    req = urllib.request.Request(base_url.rstrip("/") + f"/api/inflight/{urllib.parse.quote(inflight_id, safe='')}/hold",
                                 data=b"", method="POST" if hold else "DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        resp.read()


def clear_crashes(base_url: str, model_id: str, timeout: float = 5.0) -> None:
    """Forget a model's crashes, so the engine starts it again on a request. An older engine has none to forget."""
    try:
        _post(base_url.rstrip("/") + f"/api/crashes/clear/{urllib.parse.quote(model_id, safe='')}", timeout)
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 405):
            raise


def unload_model(base_url: str, model_id: str, timeout: float = 60.0) -> None:
    """Make the engine stop one model; other models are not touched."""
    _post(base_url.rstrip("/") + f"/api/models/unload/{urllib.parse.quote(model_id, safe='')}", timeout)


def rescan(base_url: str, timeout: float = 60.0) -> dict:
    """Make the engine scan its models folder now. ``{"ok", "models", "changed"}``; raises when unreachable."""
    req = urllib.request.Request(base_url.rstrip("/") + "/api/models/rescan", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:  # 503 when modelScan is off, 500 on a scan error: both say why
        try:
            return json.loads(exc.read().decode("utf-8", errors="replace"))
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}"}


def model_log(base_url: str, model_id: str, wait: float = 1.5, max_bytes: int = 200_000) -> str:
    """The recent output of one model's process (``model_id`` may also be "proxy" or "upstream").

    Uses the engine's one-shot ``/logs/<id>``; an older engine only streams (history first, then live), so then
    this reads the stream for ``wait`` seconds and stops."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + f"/logs/{urllib.parse.quote(model_id, safe='')}",  # noqa: S310
                                    timeout=max(wait, 3.0)) as resp:
            return resp.read().decode("utf-8", errors="replace")[-max_bytes:]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    import socket
    import time as _time
    url = base_url.rstrip("/") + f"/logs/stream/{urllib.parse.quote(model_id, safe='')}"
    chunks, size, deadline = [], 0, _time.time() + wait
    try:
        with urllib.request.urlopen(url, timeout=wait) as resp:  # noqa: S310
            while _time.time() < deadline and size < max_bytes:
                try:
                    part = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                except (socket.timeout, TimeoutError):
                    break
                if not part:
                    break
                chunks.append(part)
                size += len(part)
    except (socket.timeout, TimeoutError):
        pass
    return b"".join(chunks).decode("utf-8", errors="replace")[-max_bytes:]


def completion(base_url: str, model_id: str, body: dict, timeout: float = 900.0) -> dict:
    """POST llama-server's native ``/completion`` to one model through the engine (it loads the model if needed)."""
    req = urllib.request.Request(base_url.rstrip("/") + f"/upstream/{urllib.parse.quote(model_id, safe='')}/completion",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def model_state(base_url: str, model_id: str, timeout: float = 1.5) -> str:
    """"ready", "starting", "stopping" or "stopped" for one model in the engine. Raises when unreachable."""
    for m in running(base_url, timeout):
        if m.get("model") == model_id:
            return str(m.get("state") or "ready")
    return "stopped"


def pool_state(base_url: str, timeout: float = 3.0) -> dict:
    """The engine's ``GET /api/pool``: cards, placement, queue and loading models.

    ``{"enabled": false}`` means the engine is running another router. Raises on a connection or JSON error.
    """
    state = _get(base_url.rstrip("/") + "/api/pool", timeout)
    if not isinstance(state, dict):
        raise ValueError("the engine did not return JSON from /api/pool")
    return state


def inflight(base_url: str, timeout: float = 3.0) -> list[dict]:
    """The engine's ``GET /api/inflight``: requests still running, oldest first, each with ``elapsed_ms``.
    An engine without the endpoint has none to show."""
    try:
        data = _get(base_url.rstrip("/") + "/api/inflight", timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    rows = data.get("requests") if isinstance(data, dict) else None
    return [{"id": r.get("id"), "model": r.get("model") or "", "path": r.get("req_path") or "", "src": r.get("remote_ip") or "",
             "elapsed_ms": r.get("elapsed_ms") or 0, "resp_bytes": r.get("resp_bytes") or 0,
             "held": (r.get("metadata") or {}).get("held") or ""}
            for r in rows or [] if isinstance(r, dict)]


def model_launch(base_url: str, model_id: str, timeout: float = 3.0) -> dict:
    """{"cmd", "env"} the engine starts ``model_id`` with, from its /v1/models listing; {} when it is not listed."""
    data = _get(base_url.rstrip("/") + "/v1/models", timeout)
    for m in (data.get("data") if isinstance(data, dict) else None) or []:
        if m.get("id") == model_id:
            argos = ((m.get("meta") or {}).get("argos") or {})
            return {"cmd": argos.get("cmd") or "", "env": argos.get("env") or []}
    return {}


def model_ids(base_url: str, timeout: float = 3.0) -> set[str]:
    """Ids the engine lists at ``/v1/models`` (variants included, unlisted models not)."""
    data = _get(base_url.rstrip("/") + "/v1/models", timeout)
    return {str(m["id"]) for m in (data.get("data") if isinstance(data, dict) else None) or [] if "id" in m}


__all__ = ["model_profile", "set_model_profile", "pool_state", "model_ids", "running", "hardware", "version", "odysseus_refresh", "load_model", "unload_model",
           "model_state", "model_log"]
