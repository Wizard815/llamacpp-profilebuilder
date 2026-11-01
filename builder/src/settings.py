"""App settings: defaults from the environment, overrides saved from the UI.

Each top-level key is one group. A group saved from the Settings view is
stored whole in the ``settings`` table and merged over its default, so new
default keys still appear for older saved groups.
"""
from __future__ import annotations

import copy
import os
from typing import Any

from .db import Store

SECRET_KEYS = {("downloads", "hf_token"), ("odysseus", "token")}


def env_defaults(cfg: dict) -> dict:
    """Default settings built from app config (which itself defaults to env vars)."""
    root = cfg.get("ROOT", ".")
    return {
        "paths": {
            "models_dir": cfg.get("MODELS_DIR") or os.path.join(root, "models"),
            # local runners (Servers -> Local): whisper.cpp, Kokoro, custom servers and add-ons, one folder each
            "runners_dir": cfg.get("RUNNERS_DIR") or os.path.join(os.path.dirname(os.path.abspath(cfg.get("DB") or root)), "runners"),
        },
        "integrations": {
            # the engine is the Argos container's own scheduler; the builder reads GPUs, loads and speeds from it
            "engine_url": cfg.get("ENGINE_URL", ""),
        },
        "always": {
            # added to every command when it does not set them already; a profile's own value wins
            "env": "",
            "flags": "",
            "on_launch": True,   # launches from this app (playground, measure)
            "on_export": True,   # every model the engine starts
        },
        "odysseus": {
            "url": "",                # e.g. http://odysseus:7000
            "token": "",              # an ody_… API token from Odysseus → Settings → API tokens
            "brain_in_memory": True,  # let the assistant's memory include Odysseus' Brain
            "use_presets": True,      # Odysseus' saved Cookbook presets become models here
        },
        "memory": {
            # the assistant's memory: every record gets an embedding from this OpenAI-compatible endpoint
            "enabled": True,
            "endpoint": "",   # blank = the engine's /v1 (another OpenAI-compatible server only if you need one)
            "model": "",      # an embedding model id, e.g. nomic-embed-text-v2-q8; blank = search by words only
            "model_cpu": "",  # its CPU alternative for when the GPUs are full: best another profile of the same model
            "batch": 16,
            "results": 8,     # records the assistant sees per question
        },
        "gpu": {
            # where the GPU toolkits and tools are (Settings -> GPU tools); blank = whatever is on PATH.
            # A custom ROCm or CUDA on the host is mounted into Argos and its mounted folder goes here.
            "rocm_path": cfg.get("ROCM_PATH", ""),     # e.g. /opt/rocm-gfx906
            "cuda_path": cfg.get("CUDA_PATH", ""),     # e.g. /usr/local/cuda-12.4
            "rocm_smi": "",                            # e.g. /opt/rocm-gfx906/bin/rocm-smi
            "nvidia_smi": "",
        },
        "kv": {
            # KV cache types the estimator does not know, as name=bits per value, e.g. "turbo5=3.0, mykv=4.25".
            # bits per value = bytes per block * 8 / values per block (q8_0: 34 bytes per 32 values = 8.5)
            "custom_types": "",
        },
        "builder": {
            # "odysseus" rewrites commands into the Cookbook's shape on every edit
            "command_format": "odysseus",
        },
        "prefab": {
            # what a freshly scanned model's prefab command starts from
            # The program a prefab launches. Empty means llama-server on PATH.
            # A build that keeps llama.cpp in another container has no
            # llama-server to run at all, so point this at whatever launches it
            # there - e.g. "/app/bin/remote-llama rocm", the wrapper the image
            # ships. Everything after the backend word is passed through.
            "program": "",
            "host": "0.0.0.0",
            # ${PORT}, not a number: the runner swaps it for a free port on
            # every launch. A fixed port collides with whatever already holds
            # it - in a single-port build that is the builder's own 8080, so
            # the launch fails and the health check answers from Argos itself.
            "port": "${PORT}",
            "ngl": "99",
            "ctx": "32768",
            "flash_attn": "on",
            "cache_type": "q8_0",
            "load_mode": "",
            "split_mode": "",
            "jinja": True,
            "env": "",
            "extra": "",
        },
        "playground": {
            "llama_bench": True,  # the llama-bench tab in the Playground
        },
        "downloads": {
            "hf_endpoint": cfg.get("HF_ENDPOINT") or "https://huggingface.co",
            "hf_token": cfg.get("HF_TOKEN", ""),
            "vram_gb": 0.0,  # 0 = detect
        },
        "telemetry": {
            "enabled": bool(cfg.get("TELEMETRY", True)),
            "interval_s": 2,  # seconds between GPU readings: two hours of them is a few thousand rows
            "keep_minutes": 120,  # GPU readings and request history older than this are deleted
            "source": "auto",  # auto | engine | local | off
        },
        "assistant": {
            "mode": "engine",        # engine: a model in Argos; endpoint: another OpenAI-compatible server; managed: a profile launched beside the engine
            "profile_id": "",
            "endpoint": "",          # only for mode endpoint, e.g. http://other-box:8080/v1
            "model": "",
            "context_tokens": 8192,  # what the assistant's server was launched with
            "keep_recent": 8,        # messages always kept verbatim
            "temperature": 0.7,
            "idle_unload_min": 15,   # unload the assistant's chat and embedding models after this long unused; 0 = never
            # the assistant's chat and embedding models run on the CPU when the GPUs are full, instead of unloading
            # a model you are using to make room (needs Schedule's pool router)
            "cpu_fallback": True,
            "quant": "Q8_0",         # what to download the assistant's own model at
            "escalate_model": "",    # a bigger model for questions beyond the small one; empty = it answers everything
            # each model may have a CPU alternative, used when the GPU one would have to wait for VRAM or unload
            # something (model_choice): blank = the GPU one always
            "model_cpu": "",
            "escalate_model_cpu": "",
            # a hard question goes to a bigger chat model that is already loaded and idle (the coder between
            # prompts, say) before loading the escalation model: no load, nothing unloaded
            "escalate_to_loaded": True,
            "escalate_quant": "Q4_K_M",  # what to download that one at: it may run on CPU, where smaller is faster
        },
    }


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in out or not isinstance(base, dict):
            out[k] = v
    return out


class Settings:
    def __init__(self, store: Store, defaults: dict):
        self.store = store
        self.defaults = defaults

    def all(self) -> dict:
        saved = self.store.get_settings()
        return {group: _merge(vals, saved.get(group) or {}) for group, vals in self.defaults.items()}

    def get(self, group: str) -> dict:
        return self.all()[group]

    def update(self, group: str, values: dict) -> dict:
        """Save ``values`` over ``group``; unknown keys are rejected, types coerced to the default's."""
        if group not in self.defaults:
            raise KeyError(f"unknown settings group {group!r}")
        current = self.get(group)
        for key, val in values.items():
            if key not in self.defaults[group]:
                raise KeyError(f"unknown setting {group}.{key}")
            if (group, key) in SECRET_KEYS and isinstance(val, str) and set(val) == {"•"}:
                continue  # masked value sent back unchanged
            current[key] = _coerce(self.defaults[group][key], val)
        self.store.set_setting(group, current)
        return current

    def public(self) -> dict:
        """All settings with secrets masked, for the UI."""
        out = self.all()
        for group, key in SECRET_KEYS:
            if out[group].get(key):
                out[group][key] = "•" * 8
        return out


def _coerce(default: Any, value: Any) -> Any:
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    return "" if value is None else str(value)


__all__ = ["Settings", "env_defaults"]
