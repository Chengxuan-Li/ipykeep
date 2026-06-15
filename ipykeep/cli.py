"""ipykeep command-line interface (typer).

Commands talk to the per-notebook daemon over TCP loopback JSON-RPC, reading the
connection details (port + token) from the runtime descriptor file.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import typer

from ipykeep.config import load_config
from ipykeep.daemon.pid import (
    descriptor_path,
    is_alive,
    list_runtimes,
    log_path,
    read_runtime,
)

app = typer.Typer(add_completion=False, help="Persistent kernel access for AI coding agents.")
err = typer.style


class DaemonError(RuntimeError):
    pass


# --------------------------------------------------------------------- helpers
def _resolve_notebook(notebook: Optional[str]) -> Path:
    if notebook:
        return Path(notebook).resolve()
    cfg = load_config()
    if cfg.notebook:
        return Path(cfg.notebook).resolve()
    alive = [r for r in list_runtimes() if is_alive(r.get("pid", -1))]
    if len(alive) == 1:
        return Path(alive[0]["notebook"]).resolve()
    if not alive:
        raise DaemonError("no running daemon found; start one with `ipykeep start <notebook.ipynb>`")
    raise DaemonError("multiple daemons running; pass the notebook path explicitly")


def _client_call(notebook: Path, method: str, params: Optional[dict[str, Any]] = None,
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


def _spawn_daemon(notebook: Path, serve: bool = False) -> None:
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


# --------------------------------------------------------------------- commands
@app.command()
def start(
    notebook: str = typer.Argument(..., help="Path to the .ipynb to serve."),
    serve: bool = typer.Option(
        False, "--serve/--no-serve",
        help="Host the kernel in a Jupyter server so an IDE (VS Code / Lab) can attach."),
    timeout: float = typer.Option(180.0, help="Seconds to wait for warm-up."),
) -> None:
    """Start the daemon: boot kernel, load ipyflow, warm all cells."""
    nb = Path(notebook).resolve()
    if not nb.is_file():
        typer.secho(f"notebook not found: {nb}", fg="red", err=True)
        raise typer.Exit(1)

    info = read_runtime(nb)
    if info and is_alive(info.get("pid", -1)):
        typer.secho(f"daemon already running for {nb.name} (pid {info['pid']})", fg="yellow")
        raise typer.Exit(0)

    typer.echo(f"starting daemon for {nb.name} ...")
    _spawn_daemon(nb, serve=serve)

    deadline = time.time() + timeout
    reachable = False
    while time.time() < deadline:
        time.sleep(0.25)
        info = read_runtime(nb)
        if not (info and is_alive(info.get("pid", -1))):
            continue
        try:
            pong = _client_call(nb, "ping", timeout=5)
            reachable = True
            if not pong.get("warming"):
                break
        except (DaemonError, OSError):
            continue

    if not reachable:
        typer.secho(f"daemon failed to start; see {log_path(nb)}", fg="red", err=True)
        raise typer.Exit(1)

    try:
        st = _client_call(nb, "status", timeout=10)
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)

    if st.get("warming"):
        typer.secho(f"daemon up (pid {st['pid']}); kernel still warming in background.", fg="yellow")
    elif st.get("warm_error"):
        typer.secho(f"daemon up but warm failed: {st['warm_error']} (see {log_path(nb)})", fg="red")
        raise typer.Exit(1)
    else:
        typer.secho(
            f"daemon ready (pid {st['pid']}): {st['tracked_cells']} cells warm, "
            f"ipyflow_loaded={st['ipyflow_loaded']}",
            fg="green",
        )
        if st.get("server_url"):
            typer.secho(f"IDE: open {st['server_url']}", fg="cyan")
            typer.secho(
                "     (browser: open the URL; VS Code: 'Jupyter: Connect to a Remote "
                "Jupyter Server' -> paste it, then pick the running kernel)",
                fg="cyan",
            )


@app.command()
def stop(notebook: Optional[str] = typer.Argument(None, help="Notebook path (optional).")) -> None:
    """Gracefully shut down the daemon and kernel."""
    try:
        nb = _resolve_notebook(notebook)
        _client_call(nb, "shutdown", timeout=10)
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"stopped daemon for {nb.name}", fg="green")


@app.command()
def status(notebook: Optional[str] = typer.Argument(None, help="Notebook path (optional).")) -> None:
    """Show daemon and kernel status."""
    try:
        nb = _resolve_notebook(notebook)
        st = _client_call(nb, "status", timeout=10)
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(st, indent=2))


def _print_plan(plan: list[dict[str, Any]]) -> None:
    if not plan:
        typer.secho("no stale cells — kernel is up to date.", fg="green")
        return
    typer.secho(f"{len(plan)} stale cell(s):", fg="yellow")
    for p in plan:
        vars_ = ", ".join(p.get("produces_vars", [])) or "—"
        typer.echo(f"  [{p['cell_index']:>2}] {p['cell_id']:<14} {p['reason']:<14} produces: {vars_}")


@app.command(name="run-stale")
def run_stale(
    execute: bool = typer.Option(False, "--execute", help="Execute the stale cells (default: plan only)."),
    notebook: Optional[str] = typer.Argument(None, help="Notebook path (optional)."),
) -> None:
    """Show (plan) or execute the set of stale cells."""
    try:
        nb = _resolve_notebook(notebook)
        result = _client_call(nb, "run_stale", {"execute": execute}, timeout=600)
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    _print_plan(result.get("stale_cells", []))
    if execute:
        ex = result.get("executed", {})
        executed = ex.get("executed", [])
        errors = [e for e in ex.get("errors", []) if e]
        typer.secho(f"executed cells {executed}", fg="green")
        for e in errors:
            typer.secho(e, fg="red")


@app.command()
def run(
    cell_ids: list[str] = typer.Argument(..., help="Cell indices or ids to execute."),
    notebook: Optional[str] = typer.Option(None, "--notebook", "-n", help="Notebook path (optional)."),
) -> None:
    """Execute specific cells by index or id, regardless of staleness."""
    ids: list[Any] = [int(c) if c.lstrip("-").isdigit() else c for c in cell_ids]
    try:
        nb = _resolve_notebook(notebook)
        ex = _client_call(nb, "run_cells", {"cell_ids": ids}, timeout=600)
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"executed cells {ex.get('executed', [])}", fg="green")
    for e in (e for e in ex.get("errors", []) if e):
        typer.secho(e, fg="red")


@app.command()
def watch(
    path: str = typer.Argument(..., help="External file to register as a dependency."),
    notebook: Optional[str] = typer.Option(None, "--notebook", "-n", help="Notebook path (optional)."),
) -> None:
    """Manually register an external file dependency."""
    try:
        nb = _resolve_notebook(notebook)
        res = _client_call(nb, "watch_file", {"path": path})
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    cells = ", ".join(res.get("associated_cells", [])) or "—"
    typer.secho(f"watching {res.get('path', path)} (cells: {cells})", fg="green")


@app.command()
def inspect(
    var_name: str = typer.Argument(..., help="Variable name to summarize."),
    notebook: Optional[str] = typer.Option(None, "--notebook", "-n", help="Notebook path (optional)."),
) -> None:
    """Return a JSON summary of a runtime variable."""
    try:
        nb = _resolve_notebook(notebook)
        summary = _client_call(nb, "inspect", {"var_name": var_name}, timeout=60)
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(summary, indent=2))


@app.command(name="namespace")
def namespace(notebook: Optional[str] = typer.Argument(None, help="Notebook path (optional).")) -> None:
    """List top-level variables in the kernel namespace."""
    try:
        nb = _resolve_notebook(notebook)
        ns = _client_call(nb, "get_namespace_raw")
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(ns, indent=2))


@app.command(name="_serve", hidden=True)
def _serve(notebook: str, serve: bool = typer.Option(False, "--serve")) -> None:
    """Internal: run the daemon event loop in the foreground (spawned by start)."""
    from ipykeep.daemon.server import run_daemon

    run_daemon(Path(notebook).resolve(), serve=serve)


if __name__ == "__main__":
    app()
