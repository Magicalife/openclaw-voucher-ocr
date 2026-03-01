"""Tiny shim to call OpenClaw tools from within a skill runner.

OpenClaw injects tool functions into the Python runtime when running skills.
This module provides a stable import path for scripts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def image(*, model: str, image: str, prompt: str) -> Dict[str, Any]:  # type: ignore
    # OpenClaw will override this function at runtime. If it does not, fail fast.
    raise RuntimeError(
        "OpenClaw tool injection not present: image(). Use run_with_vision.py for gateway-driven execution."
    )
