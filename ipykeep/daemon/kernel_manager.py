"""Kernel lifecycle + execution via jupyter_client.

Owns a single long-lived ipykernel with ipyflow loaded in lazy mode. All methods
here are blocking and MUST be invoked from the daemon's single kernel worker
thread (see server.py) so that all ZMQ traffic stays on one thread.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import nbformat

log = logging.getLogger("ipykeep.kernel")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_JSON_SENTINEL = "__IPYKEEP_JSON__"
# Dedicated cell id for the daemon's own helper executions. Without an explicit
# id, ipyflow assigns such executions the *last active* notebook cell id, which
# remaps that cell's counter and breaks symbol->cell resolution. Isolating them
# under one sentinel id keeps every real notebook cell's mapping intact.
_INTERNAL_CELL_ID = "__ipykeep_internal__"


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass
class CellInfo:
    index: int          # position among code cells (0-based)
    cell_id: str        # stable id passed to ipyflow via metadata.cellId
    source: str
    source_hash: str
    execution_count: Optional[int] = None


@dataclass
class ExecResult:
    cell_id: Optional[str]
    execution_count: Optional[int]
    stdout: str
    stderr: str
    results: list[str]
    error: Optional[str]

    def text_output(self) -> str:
        parts = [self.stdout]
        if self.results:
            parts.append("\n".join(self.results))
        return "".join(p for p in parts if p)


# Kernel-side snippet: enumerate the user namespace as JSON on one line.
_NS_CODE = r"""
def _ipykeep_namespace():
    import json as _json, sys as _sys, types as _types
    ip = get_ipython()
    user_ns = ip.user_ns
    hidden = set(getattr(ip, "user_ns_hidden", {}))
    skip = {"In", "Out", "exit", "quit", "get_ipython", "open"}
    try:
        from ipyflow.singletons import flow as _flow
        gs = _flow().global_scope
    except Exception:
        gs = None
    out = []
    for name, val in list(user_ns.items()):
        if name.startswith("_") or name in hidden or name in skip:
            continue
        if isinstance(val, _types.ModuleType):
            continue
        try:
            size = int(_sys.getsizeof(val))
        except Exception:
            size = -1
        cellnum = None
        if gs is not None:
            try:
                s = gs.get(name)
                if s is not None:
                    cellnum = s.timestamp.cell_num
            except Exception:
                cellnum = None
        out.append({"name": name, "type": type(val).__name__,
                    "size_bytes": size, "last_updated_cell_num": cellnum})
    print("__IPYKEEP_JSON__" + _json.dumps(out))
_ipykeep_namespace()
"""


# Kernel-side snippet: per user-namespace symbol, its defining cell id and the
# cell ids where it is live (used). Drives downstream staleness propagation.
_DEPGRAPH_CODE = r"""
def _ipykeep_depgraph():
    import json as _json, types as _types
    ip = get_ipython()
    user_ns = ip.user_ns
    hidden = set(getattr(ip, "user_ns_hidden", {}))
    skip = {"In", "Out", "exit", "quit", "get_ipython", "open"}
    try:
        from ipyflow.singletons import flow as _flow
        from ipyflow import cells as _cells
        fl = _flow()
        gs = fl.global_scope
    except Exception:
        print("__IPYKEEP_JSON__[]")
        return
    ctr_to_id = {}
    for cid in _cells().all_executed_cell_ids():
        c = _cells().from_id_nullable(cid)
        if c is not None:
            ctr_to_id[c.cell_ctr] = c.cell_id
    out = []
    for name, val in list(user_ns.items()):
        if name.startswith("_") or name in hidden or name in skip:
            continue
        if isinstance(val, _types.ModuleType):
            continue
        try:
            s = gs.get(name)
        except Exception:
            s = None
        if s is None:
            continue
        try:
            def_id = ctr_to_id.get(s.timestamp.cell_num)
        except Exception:
            def_id = None
        live = sorted({getattr(c, "cell_id", None) for c in s.cells_where_live} - {None},
                      key=lambda x: str(x))
        out.append({"name": name, "defining_cell": def_id, "live_cells": live})
    print("__IPYKEEP_JSON__" + _json.dumps(out))
_ipykeep_depgraph()
"""


class KernelSession:
    def __init__(self, notebook: Path, serve: bool = False,
                 server_command: str = "lab", server_port: int = 0,
                 log_file=None):
        self.notebook = Path(notebook)
        self.serve = serve
        self.server_command = server_command
        self.server_port = server_port
        self.log_file = log_file
        self.km = None
        self.kc = None
        self.host = None  # JupyterServerHost when serving
        self.cells: list[CellInfo] = []
        self._last_exec_count: Optional[int] = None
        self._ipyflow_loaded = False

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        if self.serve:
            self._start_served()
        else:
            from jupyter_client.manager import start_new_kernel

            log.info("starting bare kernel")
            self.km, self.kc = start_new_kernel(startup_timeout=60)
        r = self.execute("%load_ext ipyflow")
        self._ipyflow_loaded = r.error is None
        if r.error:
            log.error("failed to load ipyflow:\n%s", r.error)
        self.execute("%flow mode lazy")
        self._ensure_importable()
        log.info("kernel ready (ipyflow_loaded=%s, serve=%s)", self._ipyflow_loaded, self.serve)

    def _start_served(self) -> None:
        import secrets

        from pathlib import Path as _Path

        from ipykeep.daemon.jupyter_host import JupyterServerHost

        log.info("starting server-hosted kernel (command=%s)", self.server_command)
        self.host = JupyterServerHost(
            root_dir=_Path.cwd(), notebook=self.notebook, token=secrets.token_hex(16),
            command=self.server_command, port=self.server_port,
        )
        self.host.start(log_file=self.log_file)
        self.kc = self.host.attach()
        self.km = None

    def _ensure_importable(self) -> None:
        """Make `import ipykeep.inspection` work kernel-side regardless of cwd."""
        import ipykeep

        pkg_parent = str(Path(ipykeep.__file__).resolve().parent.parent)
        code = (
            "import sys as _sys\n"
            f"if {pkg_parent!r} not in _sys.path:\n"
            f"    _sys.path.insert(0, {pkg_parent!r})\n"
        )
        self.execute(code, cell_id=_INTERNAL_CELL_ID)

    # ---------------------------------------------------------------- execute
    def execute(self, code: str, cell_id: Optional[str] = None, timeout: float = 300) -> ExecResult:
        kc = self.kc
        if kc is None:
            raise RuntimeError("kernel not started")
        if cell_id is None:
            msg_id = kc.execute(code)
        else:
            content = dict(code=code, silent=False, store_history=True,
                           user_expressions={}, allow_stdin=False, stop_on_error=True)
            msg = kc.session.msg("execute_request", content)
            msg["metadata"] = {"cellId": str(cell_id)}
            kc.shell_channel.send(msg)
            msg_id = msg["header"]["msg_id"]
        return self._collect(msg_id, cell_id, timeout)

    def _collect(self, msg_id: str, cell_id: Optional[str], timeout: float) -> ExecResult:
        kc = self.kc
        stdout: list[str] = []
        stderr: list[str] = []
        results: list[str] = []
        error: Optional[str] = None
        exec_count: Optional[int] = None

        # shell reply (carries execution_count + error status)
        while True:
            try:
                reply = kc.get_shell_msg(timeout=timeout)
            except queue.Empty:
                break
            if reply["parent_header"].get("msg_id") != msg_id:
                continue
            c = reply["content"]
            exec_count = c.get("execution_count", exec_count)
            if c.get("status") == "error" and error is None:
                tb = c.get("traceback") or []
                error = strip_ansi("\n".join(tb)) or f"{c.get('ename')}: {c.get('evalue')}"
            break

        # iopub stream until idle for our msg
        while True:
            try:
                msg = kc.get_iopub_msg(timeout=timeout)
            except queue.Empty:
                break
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            t = msg["header"]["msg_type"]
            c = msg["content"]
            if t == "stream":
                (stdout if c.get("name") == "stdout" else stderr).append(c.get("text", ""))
            elif t in ("execute_result", "display_data"):
                results.append(c.get("data", {}).get("text/plain", ""))
            elif t == "execute_input":
                exec_count = c.get("execution_count", exec_count)
            elif t == "error":
                error = strip_ansi("\n".join(c.get("traceback", [])))
            elif t == "status" and c.get("execution_state") == "idle":
                break

        if exec_count is not None:
            self._last_exec_count = exec_count
        return ExecResult(cell_id=cell_id, execution_count=exec_count,
                          stdout="".join(stdout), stderr="".join(stderr),
                          results=results, error=error)

    # ------------------------------------------------------------- notebook io
    def _load_cells(self) -> list[CellInfo]:
        nb = nbformat.read(self.notebook, as_version=4)
        out: list[CellInfo] = []
        idx = 0
        for cell in nb.cells:
            if cell.get("cell_type") != "code":
                continue
            src = cell.get("source", "")
            if isinstance(src, list):
                src = "".join(src)
            cid = cell.get("id") or f"idx-{idx}"
            out.append(CellInfo(index=idx, cell_id=str(cid), source=src, source_hash=_hash(src)))
            idx += 1
        return out

    def warm(self) -> None:
        self.cells = self._load_cells()
        log.info("warming %d code cells", len(self.cells))
        for c in self.cells:
            r = self.execute(c.source, c.cell_id)
            c.execution_count = r.execution_count
            if r.error:
                log.warning("cell %s (index %d) raised during warm:\n%s",
                            c.cell_id, c.index, r.error)
        self._set_positions()
        log.info("warm complete")

    def _set_positions(self) -> None:
        if not self.cells:
            return
        mapping = {c.cell_id: c.index for c in self.cells}
        code = (
            "try:\n"
            "    from ipyflow import cells as _ipykeep_cells\n"
            f"    _ipykeep_cells().set_cell_positions({mapping!r})\n"
            "except Exception as _e:\n"
            "    pass\n"
        )
        self.execute(code, cell_id=_INTERNAL_CELL_ID)

    # --------------------------------------------------------------- run cells
    def execute_cells(self, cell_ids: list[Any]) -> dict[str, Any]:
        current = self._load_cells()
        # carry over execution counts we already know
        prev = {c.cell_id: c.execution_count for c in self.cells}
        for c in current:
            c.execution_count = prev.get(c.cell_id)
        by_id = {c.cell_id: c for c in current}
        by_idx = {c.index: c for c in current}

        selected: list[CellInfo] = []
        for cid in cell_ids:
            c: Optional[CellInfo] = None
            if isinstance(cid, int):
                c = by_idx.get(cid)
            else:
                c = by_id.get(str(cid))
                if c is None and str(cid).lstrip("-").isdigit():
                    c = by_idx.get(int(cid))
            if c is not None and c not in selected:
                selected.append(c)
        selected.sort(key=lambda c: c.index)

        executed: list[int] = []
        outputs: list[str] = []
        errors: list[Optional[str]] = []
        for c in selected:
            r = self.execute(c.source, c.cell_id)
            c.execution_count = r.execution_count
            c.source_hash = _hash(c.source)
            executed.append(c.index)
            outputs.append(r.text_output())
            errors.append(r.error)
        self.cells = current
        self._set_positions()
        return {"executed": executed, "outputs": outputs, "errors": errors}

    # --------------------------------------------------------------- namespace
    def get_namespace_raw(self) -> list[dict[str, Any]]:
        r = self.execute(_NS_CODE, cell_id=_INTERNAL_CELL_ID)
        data = _extract_sentinel_json(r.stdout)
        if data is None:
            return []
        ctr_to_index = {c.execution_count: c.index for c in self.cells if c.execution_count is not None}
        for item in data:
            num = item.pop("last_updated_cell_num", None)
            item["last_updated_cell"] = ctr_to_index.get(num, num)
        return data

    def get_depgraph(self) -> list[dict[str, Any]]:
        r = self.execute(_DEPGRAPH_CODE, cell_id=_INTERNAL_CELL_ID)
        data = _extract_sentinel_json(r.stdout)
        return data if isinstance(data, list) else []

    def load_cells(self) -> list[CellInfo]:
        """Public: current code cells freshly read from disk."""
        return self._load_cells()

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        connection_file = None
        server_url = None
        server_token = None
        if self.host is not None:
            alive = self.host.is_alive()
            connection_file = self.host.connection_file
            server_url = self.host.url
            server_token = self.host.token
        else:
            alive = bool(self.km and self.km.is_alive())
            try:
                connection_file = self.km.connection_file if self.km is not None else None
            except Exception:
                connection_file = None
        return {
            "alive": alive,
            "execution_count": self._last_exec_count,
            "ipyflow_loaded": self._ipyflow_loaded,
            "connection_file": connection_file,
            "server_url": server_url,
            "server_token": server_token,
        }

    def server_pid(self) -> Optional[int]:
        return self.host.server_pid if self.host is not None else None

    def shutdown(self) -> None:
        try:
            if self.kc is not None:
                self.kc.stop_channels()
        except Exception:
            pass
        if self.host is not None:
            self.host.shutdown()
        else:
            try:
                if self.km is not None:
                    self.km.shutdown_kernel(now=False)
            except Exception:
                pass
        log.info("kernel shut down")


def _extract_sentinel_json(stdout: str) -> Optional[Any]:
    for line in stdout.splitlines():
        if line.startswith(_JSON_SENTINEL):
            try:
                return json.loads(line[len(_JSON_SENTINEL):])
            except json.JSONDecodeError:
                return None
    return None
