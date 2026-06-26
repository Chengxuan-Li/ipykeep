"""Shared client helpers for talking to the per-notebook ipykeep daemon.

Used by both the CLI (`ipykeep.cli`) and the MCP server (`ipykeep.mcp_server`)
so neither owns the transport. Communication is newline-delimited JSON-RPC over
TCP loopback; connection details come from the runtime descriptor file.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from ipykeep.config import load_config
from ipykeep.daemon.pid import is_alive, list_runtimes, log_path, read_runtime


class DaemonError(RuntimeError):
    pass


def resolve_notebook(notebook: Optional[str]) -> Path:
    """Resolve which notebook/daemon a command targets.

    Explicit path wins; else `[tool.ipykeep].notebook` from cwd config; else the
    sole running daemon. Raises DaemonError when ambiguous or absent.
    """
    if notebook:
        return Path(notebook).resolve()
    env_nb = os.environ.get("IPYKEEP_NOTEBOOK")
    if env_nb:
        return Path(env_nb).resolve()
    cfg = load_config()
    if cfg.notebook:
        return Path(cfg.notebook).resolve()
    alive = [r for r in list_runtimes() if is_alive(r.get("pid", -1))]
    if len(alive) == 1:
        return Path(alive[0]["notebook"]).resolve()
    if not alive:
        raise DaemonError("no running daemon found; start one with `ipykeep start <notebook.ipynb>`")
    raise DaemonError("multiple daemons running; pass the notebook path explicitly")


def client_call(notebook: Path, method: str, params: Optional[dict[str, Any]] = None,
                timeout: float = 60.0) -> Any:
    info = read_runtime(notebook)
    if not info or not is_alive(info.get("pid", -1)):
        raise DaemonError(f"no live daemon for {notebook.name}; run `ipykeep start {notebook}`")
    req = {"id": 1, "token": info["token"], "method": method, "params": params or {}}
    try:
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout) as sock:
            sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            sock.settimeout(timeout)
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        raise DaemonError(f"could not reach daemon for {notebook.name}: {exc}") from exc
    if not buf:
        raise DaemonError("empty response from daemon")
    resp = json.loads(buf)
    if resp.get("error"):
        raise DaemonError(resp["error"].get("message", "unknown daemon error"))
    return resp.get("result")


def spawn_daemon(notebook: Path, serve: bool = False) -> None:
    logf = open(log_path(notebook), "ab")
    kwargs: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, close_fds=True)
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    argv = [sys.executable, "-m", "ipykeep", "_serve", str(notebook)]
    if serve:
        argv.append("--serve")
    subprocess.Popen(argv, **kwargs)


def is_running(notebook: Path) -> bool:
    info = read_runtime(notebook)
    return bool(info and is_alive(info.get("pid", -1)))


def ensure_started(notebook: Path, serve: bool = False, timeout: float = 180.0) -> dict[str, Any]:
    """Idempotently ensure a warmed daemon exists for the notebook; return status.

    If one is already running, returns its status without spawning a duplicate.
    Otherwise spawns the daemon and polls until warm-up finishes. Raises
    DaemonError if it never becomes reachable.
    """
    if is_running(notebook):
        return client_call(notebook, "status", timeout=10)

    spawn_daemon(notebook, serve=serve)
    deadline = time.time() + timeout
    reachable = False
    while time.time() < deadline:
        time.sleep(0.25)
        if not is_running(notebook):
            continue
        try:
            pong = client_call(notebook, "ping", timeout=5)
            reachable = True
            if not pong.get("warming"):
                break
        except (DaemonError, OSError):
            continue
    if not reachable:
        raise DaemonError(f"daemon failed to start for {notebook.name}; see {log_path(notebook)}")
    return client_call(notebook, "status", timeout=10)
