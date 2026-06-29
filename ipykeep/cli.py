"""ipykeep command-line interface (typer).

Commands talk to the per-notebook daemon over TCP loopback JSON-RPC, reading the
connection details (port + token) from the runtime descriptor file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer

from ipykeep.client import (
    DaemonError,
    client_call as _client_call,
    ensure_started,
    resolve_notebook as _resolve_notebook,
)
from ipykeep.daemon.pid import is_alive, log_path, read_runtime

app = typer.Typer(add_completion=False, help="Persistent kernel access for AI coding agents.")


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
    try:
        st = ensure_started(nb, serve=serve, timeout=timeout)
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


_VSCODE_EXTENSION_ID = "ipykeep.ipykeep-vscode"


def _fire_vscode_uri(uri: str) -> bool:
    """Open a vscode:// deep link via the `code` CLI. Returns True on success."""
    import shutil
    import subprocess

    code = shutil.which("code") or shutil.which("code.cmd")
    if not code:
        return False
    try:
        subprocess.run([code, "--open-url", uri], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


@app.command(name="open")
def open_cmd(
    notebook: str = typer.Argument(..., help="Path to the .ipynb to open live in VS Code."),
    timeout: float = typer.Option(180.0, help="Seconds to wait for warm-up."),
) -> None:
    """Open the notebook on its warm kernel in VS Code, one click.

    Boots the daemon with a hosted Jupyter server (``--serve``) and fires the
    ipykeep VS Code extension's deep link so the notebook opens already attached
    to the warm kernel. Falls back to printing the server URL if VS Code or the
    extension is unavailable.
    """
    from urllib.parse import quote

    nb = Path(notebook).resolve()
    if not nb.is_file():
        typer.secho(f"notebook not found: {nb}", fg="red", err=True)
        raise typer.Exit(1)

    typer.echo(f"starting daemon for {nb.name} ...")
    try:
        st = ensure_started(nb, serve=True, timeout=timeout)
    except DaemonError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)

    if st.get("warming"):
        typer.secho(f"daemon up (pid {st['pid']}); kernel still warming — try again shortly.",
                    fg="yellow")
        raise typer.Exit(0)
    if st.get("warm_error"):
        typer.secho(f"daemon up but warm failed: {st['warm_error']} (see {log_path(nb)})", fg="red")
        raise typer.Exit(1)

    typer.secho(
        f"daemon ready (pid {st['pid']}): {st['tracked_cells']} cells warm, "
        f"ipyflow_loaded={st['ipyflow_loaded']}",
        fg="green",
    )

    uri = f"vscode://{_VSCODE_EXTENSION_ID}/open?notebook={quote(str(nb), safe='')}"
    if _fire_vscode_uri(uri):
        typer.secho(f"opening {nb.name} in VS Code on the warm kernel ...", fg="cyan")
    else:
        typer.secho("could not launch VS Code automatically. To open manually:", fg="yellow")
        typer.secho(f"  deep link: {uri}", fg="cyan")
        if st.get("server_url"):
            typer.secho(f"  or attach by URL: {st['server_url']}", fg="cyan")
            typer.secho(
                "    (VS Code: 'Jupyter: Connect to a Remote Jupyter Server' -> paste it, "
                "then pick the running kernel)",
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


@app.command(name="mcp-serve")
def mcp_serve(
    notebook: Optional[str] = typer.Argument(None, help="Notebook to target (else from config)."),
) -> None:
    """Run the MCP server over stdio (point your harness at this via .mcp.json)."""
    try:
        from ipykeep.mcp_server import serve_stdio
    except ImportError:
        typer.secho('MCP support not installed — run: pip install "ipykeep[mcp]"',
                    fg="red", err=True)
        raise typer.Exit(1)
    if notebook:
        # make resolve_notebook(None) in the server pick this notebook
        import os
        os.environ["IPYKEEP_NOTEBOOK"] = str(Path(notebook).resolve())
    serve_stdio()


@app.command()
def init(
    notebook: Optional[str] = typer.Argument(None, help="Default notebook for this project."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Scaffold integration files: .mcp.json, SKILL.md, AGENTS.md, ipykeep.toml."""
    from ipykeep.scaffold import run_init

    results = run_init(Path.cwd(), notebook, force=force)
    for path, action in results:
        color = "green" if action in ("created", "merged", "appended", "updated", "overwritten") else "yellow"
        typer.secho(f"  {action:<32} {path}", fg=color)
    typer.secho("ipykeep project initialized.", fg="green")


@app.command(name="_serve", hidden=True)
def _serve(notebook: str, serve: bool = typer.Option(False, "--serve")) -> None:
    """Internal: run the daemon event loop in the foreground (spawned by start)."""
    from ipykeep.daemon.server import run_daemon

    run_daemon(Path(notebook).resolve(), serve=serve)


if __name__ == "__main__":
    app()
