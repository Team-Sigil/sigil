"""Where the agent layer's source is, for the parity and end-to-end tests.

`AGENT_LAYER_SRC` points at its `src/` directory (default: a sibling `agent-layer` checkout).
Locally a missing agent layer skips those tests; with `CI` set it fails them, so a pipeline
can never pass without actually checking the contract.
"""

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

DEFAULT_SRC = Path(__file__).resolve().parents[2] / "agent-layer" / "src"


def agent_layer_src() -> Path:
    src = Path(os.environ.get("AGENT_LAYER_SRC") or DEFAULT_SRC)
    if not (src / "agent_layer").is_dir():
        message = f"agent layer not found at {src}; set AGENT_LAYER_SRC"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return src


def import_agent_layer(module: str) -> ModuleType:
    src = str(agent_layer_src())
    sys.path.insert(0, src)
    try:
        return importlib.import_module(module)
    finally:
        sys.path.remove(src)
