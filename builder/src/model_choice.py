"""Which of a GPU model and its CPU alternative the assistant uses, for each of its models (chat, hard questions,
embedding).

The GPU model is the one wanted. The CPU one is for when the GPUs are full, say a large model is loaded and
busy: loading the GPU one then would wait for it or unload it. One of the pair already loaded is used as it is, so
the two are never loaded side by side and a conversation does not jump between them; with neither loaded, the
GPU model is used when it fits in the VRAM that is free.
"""
from __future__ import annotations


def choose(gpu: str, cpu: str, pool: dict) -> tuple[str, str]:
    """(model id, why) from the pair and the engine's /api/pool."""
    gpu, cpu = (gpu or "").strip(), (cpu or "").strip()
    if not cpu or cpu == gpu:
        return gpu, "the model set"
    if not gpu:
        return cpu, "the CPU model (no GPU model is set)"
    models = {m["id"]: m for m in (pool or {}).get("models") or [] if m.get("id")}
    loaded = {mid for mid, m in models.items() if m.get("state") in ("ready", "starting")}
    if gpu in loaded:
        return gpu, "the GPU model, already loaded"
    if cpu in loaded:
        return cpu, "the CPU model, already loaded"
    need = (models.get(gpu) or {}).get("size_gib") or 0
    cards = (pool or {}).get("cards") or []
    free = sum(c.get("free_gib") or 0 for c in cards)
    if not cards or not need or need <= free:
        return gpu, "the GPU model: it fits in the free VRAM"
    return cpu, f"the CPU model: the GPU one needs {need:.1f} GiB and only {free:.1f} GiB is free"


__all__ = ["choose"]
