"""Runtime path and log rotation helpers for the macOS deliverable."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
STATE_DIR = RUNTIME / "state"
CONFIG_DIR = RUNTIME / "config"
CACHE_DIR = RUNTIME / "cache"
LOG_DIR = RUNTIME / "logs"


def ensure_dirs() -> None:
    for path in (STATE_DIR, CONFIG_DIR, CACHE_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
    RUNTIME.chmod(0o700)
    for path in (STATE_DIR, CONFIG_DIR, CACHE_DIR, LOG_DIR):
        path.chmod(0o700)


def secure_runtime_files() -> None:
    for directory in (STATE_DIR, CONFIG_DIR):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if path.is_file():
                path.chmod(0o600)


def state_path(name: str) -> Path:
    ensure_dirs()
    return STATE_DIR / name


def config_path(name: str) -> Path:
    ensure_dirs()
    return CONFIG_DIR / name


def cache_path(name: str) -> Path:
    ensure_dirs()
    return CACHE_DIR / name


def log_path(name: str) -> Path:
    ensure_dirs()
    return LOG_DIR / name


def rotate_log(path: Path, *, max_bytes: int, keep_lines: int) -> None:
    """Keep a bounded tail; launchd writes with append semantics."""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            kept = path.read_text(errors="replace").splitlines()[-keep_lines:]
            path.write_text("\n".join(kept) + "\n")
            path.chmod(0o600)
    except OSError:
        pass
