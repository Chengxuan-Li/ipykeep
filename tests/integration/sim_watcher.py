"""Simulated VS Code watcher — plays the companion extension's RPC role against a
live ipykeep daemon, for headless end-to-end testing of delegated execution.

It does exactly what the real extension will do over the wire:
  * register_watcher          -> daemon flips to delegated mode
  * set_cell_id_map           -> pushes vscode-uri <-> nbformat-id aliases
  * await_run_request (poll)  -> receives the ordered stale cell ids
  * runs those cells ON THE KERNEL the way VS Code does — issuing an
    execute_request whose metadata.cellId is a "vscode-notebook-cell:" URI
    (the crux the alias table reconciles)
  * report_run_complete

Attaches via the daemon's connection_file (works for a bare kernel too, so no
Jupyter server is required for the test).

Usage (manual two-shell flow):
    ipykeep start examples/01_eda.ipynb
    python tests/integration/sim_watcher.py examples/01_eda.ipynb   # leave running
    # ... in another shell: edit a cell, then `ipykeep run-stale --execute`
"""
from __future__ import annotations

import json
import queue
import secrets
import socket
import sys
import time
from pathlib import Path

import nbformat
from jupyter_client import BlockingKernelClient

from ipykeep.daemon.pid import is_alive, read_runtime


class WatcherSim:
    def __init__(self, notebook: str):
        self.nb = Path(notebook).resolve()
        self.info = read_runtime(self.nb)
        if not self.info or not is_alive(self.info.get("pid", -1)):
            raise SystemExit(f"no live daemon for {self.nb.name}; run `ipykeep start {self.nb}` first")
        self.client_id: str | None = None
        self.kc: BlockingKernelClient | None = None
        self.uri_by_id: dict[str, str] = {}
        self.requests_handled: list[list[str]] = []

    # ----------------------------------------------------------------- rpc
    def _call(self, method: str, params: dict | None = None, timeout: float = 60.0):
        req = {"id": 1, "token": self.info["token"], "method": method, "params": params or {}}
        with socket.create_connection(("127.0.0.1", self.info["port"]), timeout=timeout) as s:
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            s.settimeout(timeout)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf)
        if resp.get("error"):
            raise RuntimeError(resp["error"].get("message", "daemon error"))
        return resp.get("result")

    # ----------------------------------------------------------- notebook io
    def _code_cells(self):
        nb = nbformat.read(self.nb, as_version=4)
        return [c for c in nb.cells if c.get("cell_type") == "code"]

    def _build_map(self) -> list[dict]:
        cell_map = []
        for i, c in enumerate(self._code_cells()):
            cid = c.get("id") or f"idx-{i}"
            uri = f"vscode-notebook-cell:/{self.nb.as_posix()}#sim{i}"
            self.uri_by_id[cid] = uri
            cell_map.append({"vscode_uri": uri, "nbformat_id": cid, "index": i})
        return cell_map

    def _source_for(self, cid: str) -> str:
        for i, c in enumerate(self._code_cells()):
            if (c.get("id") or f"idx-{i}") == cid:
                s = c.get("source", "")
                return "".join(s) if isinstance(s, list) else s
        return ""

    # ------------------------------------------------------------- lifecycle
    def attach(self) -> None:
        st = self._call("status")
        conn = st.get("connection_file")
        if not conn:
            raise SystemExit("daemon status has no connection_file; cannot attach")
        self.client_id = self._call("register_watcher", {"client_id": secrets.token_hex(4)})["client_id"]
        self._call("set_cell_id_map", {"cell_map": self._build_map()})
        self.kc = BlockingKernelClient()
        self.kc.load_connection_file(conn)
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=30)
        print(f"[sim] attached as watcher {self.client_id} ({len(self.uri_by_id)} cells mapped)")

    def _run_like_vscode(self, cid: str) -> None:
        """Issue an execute_request tagged with a vscode-notebook-cell URI."""
        src = self._source_for(cid)
        content = dict(code=src, silent=False, store_history=True,
                       user_expressions={}, allow_stdin=False, stop_on_error=True)
        msg = self.kc.session.msg("execute_request", content)
        msg["metadata"] = {"cellId": self.uri_by_id.get(cid, cid)}
        self.kc.shell_channel.send(msg)
        self._drain(msg["header"]["msg_id"])

    def _drain(self, msg_id: str, timeout: float = 120.0) -> None:
        try:
            while True:
                r = self.kc.get_shell_msg(timeout=timeout)
                if r["parent_header"].get("msg_id") == msg_id:
                    break
        except queue.Empty:
            pass
        try:
            while True:
                m = self.kc.get_iopub_msg(timeout=timeout)
                if m["parent_header"].get("msg_id") != msg_id:
                    continue
                if (m["header"]["msg_type"] == "status"
                        and m["content"].get("execution_state") == "idle"):
                    break
        except queue.Empty:
            pass

    def poll_once(self, timeout: float = 30.0) -> list[str] | None:
        req = self._call("await_run_request", {"timeout": timeout}, timeout=timeout + 15)
        if req.get("run_id") is None:
            return None
        cells = req.get("cells", [])
        print(f"[sim] run request {req['run_id'][:8]}: {cells}")
        for cid in cells:
            self._run_like_vscode(cid)
        self._call("report_run_complete",
                   {"run_id": req["run_id"],
                    "results": {"executed": cells, "outputs": [], "errors": [None] * len(cells)}})
        self.requests_handled.append(cells)
        print(f"[sim] reported complete: {cells}")
        return cells

    def loop(self, stop=None) -> None:
        while stop is None or not stop():
            try:
                self.poll_once()
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001
                print("[sim] error:", exc)
                time.sleep(1)

    def close(self) -> None:
        try:
            if self.client_id:
                self._call("unregister_watcher", {"client_id": self.client_id})
        except Exception:
            pass
        try:
            if self.kc:
                self.kc.stop_channels()
        except Exception:
            pass
        print("[sim] detached")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python tests/integration/sim_watcher.py <notebook.ipynb>")
    sim = WatcherSim(sys.argv[1])
    sim.attach()
    try:
        sim.loop()
    finally:
        sim.close()


if __name__ == "__main__":
    main()
