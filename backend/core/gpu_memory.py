"""Best-effort VRAM cleanup for the FastAPI process itself.

Important: the actual diffusion/video model lives entirely inside ComfyUI's
own process, and ComfyUI already manages its VRAM budget between queue
items (that's what --lowvram / --gpu-only in scripts/run_comfyui.ps1 are
for). This module is a supplementary safety net for the rare case where the
backend process itself imports torch and touches Intel XPU tensors
directly (e.g. local thumbnailing or a future pre/post-processing step) —
it is a no-op if torch isn't installed in this venv at all, which is the
expected default (see requirements.txt).
"""

import gc
import logging

logger = logging.getLogger("manytv.gpu")


def release_gpu_memory() -> None:
    gc.collect()

    try:
        import torch
    except ImportError:
        return

    try:
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            torch.xpu.empty_cache()
            torch.xpu.synchronize()
            logger.info("Cleared Intel XPU cache in backend process.")
    except Exception:
        logger.exception("Failed to clear Intel XPU cache.")
