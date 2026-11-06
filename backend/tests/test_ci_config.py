# SPEC-2026-11-06-fase-1-fondazioni
"""AC-032 (REQ-101): CI runs verify on every PR, and verify has backend and frontend steps."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def verify_step_names() -> list[str]:
    spec = importlib.util.spec_from_file_location("verify", ROOT / "scripts" / "verify.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [name for name, _cmd, _fast in module.STEPS]


def test_AC_032_verify_has_backend_and_frontend_steps() -> None:
    names = set(verify_step_names())
    for side in ("backend", "frontend"):
        for kind in ("format", "lint", "types", "test"):
            assert f"{side}-{kind}" in names
    assert {"e2e", "secrets"} <= names


def test_AC_032_ci_runs_verify_on_pull_requests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s*pull_request:", workflow, re.M)
    assert re.search(r"^\s*- run: python scripts/verify\.py\s*$", workflow, re.M)
    assert "setup-uv" in workflow
    assert "setup-node" in workflow
