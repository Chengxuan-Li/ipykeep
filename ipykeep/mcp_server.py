"""MCP server exposing ipykeep as native tools (`ipykeep mcp-serve`).

A thin stdio wrapper over the per-notebook daemon's JSON-RPC surface. The agent
calls these tools instead of shelling out to the CLI. The notebook is resolved
from `[tool.ipykeep].notebook` (or the sole running daemon); call `start_session`
first to warm a kernel.

IMPORTANT: stdout is the MCP protocol channel — nothing here may print to stdout.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ipykeep.client import DaemonError, client_call, ensure_started, resolve_notebook

log = logging.getLogger("ipykeep.mcp")

mcp = FastMCP("ipykeep")

_STATUS_KEYS = ("alive", "execution_count", "ipyflow_loaded", "tracked_cells",
                "watched_files", "warming", "warm_error", "server_url")


def _call(method: str, params: Optional[dict] = None, *, notebook: Optional[str] = None,
          timeout: float = 60.0) -> Any:
    """Resolve the target notebook and call the daemon, turning failures into a
    structured error the agent can act on instead of an exception."""
    try:
        nb = resolve_notebook(notebook)
    except DaemonError as exc:
        return {"error": str(exc)}
    try:
        return client_call(nb, method, params or {}, timeout=timeout)
    except DaemonError as exc:
        return {"error": str(exc),
                "hint": "if no kernel is running, call start_session first"}


@mcp.tool()
def start_session(notebook: Optional[str] = None, serve: bool = False) -> dict:
    """Start (or reuse) the ipykeep daemon and warm the kernel for the notebook.

    Call this once at the start of a session. `serve=true` also hosts the kernel
    in a Jupyter server so a human can attach an IDE (VS Code / JupyterLab).
    """
    try:
        nb = resolve_notebook(notebook)
    except DaemonError as exc:
        return {"error": str(exc)}
    try:
        st = ensure_started(nb, serve=serve)
    except DaemonError as exc:
        return {"error": str(exc)}
    out: dict[str, Any] = {"started": True, "notebook": str(nb)}
    out.update({k: st.get(k) for k in _STATUS_KEYS})
    return out


@mcp.tool()
def stop_session(notebook: Optional[str] = None) -> dict:
    """Shut down the ipykeep daemon and kernel for the notebook."""
    return _call("shutdown", notebook=notebook, timeout=15)


@mcp.tool()
def run_stale(execute: bool = False) -> dict:
    """Show which cells are stale (plan), or re-run only the stale ones.

    Default (execute=false) returns a plan: {stale_cells:[{cell_index, cell_id,
    reason, produces_vars}], will_execute}. execute=true re-runs only the stale
    cells in order — expensive up-to-date cells are skipped.
    """
    return _call("run_stale", {"execute": execute}, timeout=900)


@mcp.tool()
def run_cells(cell_ids: list[int]) -> dict:
    """Execute specific cells by index, regardless of staleness."""
    return _call("run_cells", {"cell_ids": cell_ids}, timeout=900)


@mcp.tool()
def inspect(var_name: str) -> dict:
    """Return a typed JSON summary of a runtime variable (shape, dtypes, nulls,
    stats, sample, size) without transmitting the whole object."""
    return _call("inspect", {"var_name": var_name})


@mcp.tool()
def get_namespace() -> Any:
    """List top-level variables in the kernel: name, type, size, last-updated cell."""
    return _call("get_namespace")


@mcp.tool()
def get_stale_set() -> Any:
    """Staleness without executing: a list of {cell_id, cell_index, reason}."""
    return _call("get_stale_set")


@mcp.tool()
def watch_file(path: str) -> dict:
    """Register an external file dependency the automatic scan may have missed."""
    return _call("watch_file", {"path": path})


@mcp.tool()
def kernel_status() -> dict:
    """Kernel/daemon status: alive, execution_count, ipyflow_loaded, server_url, …."""
    st = _call("status")
    if isinstance(st, dict) and "error" not in st:
        return {k: st.get(k) for k in _STATUS_KEYS}
    return st


def serve_stdio() -> None:
    """Run the MCP server over stdio (entry point for `ipykeep mcp-serve`)."""
    mcp.run(transport="stdio")
