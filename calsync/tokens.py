"""Refresh-token files: one 0600 JSON file per calendar under the tokens directory (0700)."""
from __future__ import annotations

import json
import os
from pathlib import Path


def token_path(tokens_dir: Path, name: str) -> Path:
    return Path(tokens_dir) / f"{name}.json"


def load(tokens_dir: Path, name: str) -> dict | None:
    p = token_path(tokens_dir, name)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save(tokens_dir: Path, name: str, data: dict) -> Path:
    d = Path(tokens_dir)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = token_path(d, name)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p
