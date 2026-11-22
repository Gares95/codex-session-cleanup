#!/usr/bin/env python3
"""Run the canonical synthetic suite against the extracted Agensi script."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "tools" / "build_agensi.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("codex_agensi_builder", BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Agensi package builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    builder = load_builder()
    with tempfile.TemporaryDirectory(prefix="codex-agensi-suite-") as temp_dir:
        root = Path(temp_dir)
        archive_path = builder.build(root / "out")
        extracted = root / "extracted"
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                    raise RuntimeError(f"unsafe archive member: {info.filename}")
            archive.extractall(extracted)
        script = extracted / builder.TOP / builder.SCRIPT
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        print(
            f"Testing extracted Agensi script: {script.stat().st_size} bytes, sha256 {digest}",
            file=sys.stderr,
        )
        environment = dict(os.environ)
        environment.update(
            {
                "CODEX_SESSION_CLEANUP_SCRIPT": str(script),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_cleanup_sessions.py",
                "-v",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
