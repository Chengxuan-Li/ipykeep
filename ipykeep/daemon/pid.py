"""Runtime descriptor management.

Replaces the spec's POSIX socket + PID file with a single cross-platform JSON
descriptor recording the daemon ``pid``, the TCP loopback ``port``, an auth
``token``, and the notebook it serves. One descriptor per notebook, keyed by a
hash of the notebook's absolute path.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    d = Path(base) / "ipykeep"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_hash(notebook: Path) -> str:
    abspath = str(Path(notebook).resolve()).lower() if os.name == "nt" else str(Path(notebook).resolve())
    return hashlib.sha1(abspath.encode("utf-8")).hexdigest()[:16]


def descriptor_path(notebook: Path) -> Path:
    return runtime_dir() / f"{project_hash(notebook)}.json"


def log_path(notebook: Path) -> Path:
    return runtime_dir() / f"{project_hash(notebook)}.log"


def write_runtime(notebook: Path, *, port: int, token: str, pid: int,
                  server_pid: Optional[int] = None) -> Path:
    info = {
        "pid": pid,
        "port": port,
        "token": token,
        "notebook": str(Path(notebook).resolve()),
        "server_pid": server_pid,
        "started_at": time.time(),
    }
    path = descriptor_path(notebook)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_runtime(notebook: Path) -> Optional[dict[str, Any]]:
    path = descriptor_path(notebook)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_runtime(notebook: Path) -> None:
    for p in (descriptor_path(notebook),):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def list_runtimes() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in runtime_dir().glob("*.json"):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def is_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
