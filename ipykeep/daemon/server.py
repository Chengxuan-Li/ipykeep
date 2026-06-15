"""Daemon: asyncio JSON-RPC server over TCP loopback owning the kernel.

Protocol: newline-delimited JSON. Each request is one JSON object
``{"id", "token", "method", "params"}`` and each response is one JSON object
``{"id", "result"}`` or ``{"id", "error": {"message", ...}}``.

All kernel I/O is serialized onto a single worker thread (``kpool``) so ZMQ
sockets are only ever touched from one thread. The kernel is warmed in the
background so the daemon is reachable (status/ping) while warming proceeds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import traceback
from functools import partial
from pathlib import Path
from typing import Any, Callable

from ipykeep.config import Config, load_config
from ipykeep.daemon.kernel_manager import KernelSession
from ipykeep.daemon.pid import clear_runtime, log_path, write_runtime
from ipykeep.inspection.inspector import Inspector
from ipykeep.staleness.tracker import StalenessTracker
from ipykeep.staleness.watcher import FileWatcher

log = logging.getLogger("ipykeep.server")

_NON_KERNEL_METHODS = {"ping", "status", "shutdown"}


class IpykeepServer:
    def __init__(self, notebook: Path, config: Config):
        self.notebook = Path(notebook)
        self.config = config
        self.session = KernelSession(self.notebook)
        self.token = secrets.token_hex(16)
        self.warming = True
        self.warm_error: str | None = None
        self.tracker: StalenessTracker | None = None
        self.watcher: FileWatcher | None = None
        self.inspector: Inspector | None = None
        self.dirty_cells: set[str] = set()
        self._stop: asyncio.Event | None = None
        self._kpool = None
        self._server: asyncio.AbstractServer | None = None

    # ------------------------------------------------------------- kernel pool
    async def _k(self, fn: Callable, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._kpool, partial(fn, *args))

    # -------------------------------------------------------------------- run
    async def run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._kpool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kernel")

        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        write_runtime(self.notebook, port=port, token=self.token, pid=os.getpid())
        log.info("listening on 127.0.0.1:%d (notebook=%s)", port, self.notebook)

        warm_task = asyncio.create_task(self._warm())
        try:
            async with self._server:
                await self._stop.wait()
        finally:
            warm_task.cancel()
            if self.watcher is not None:
                await self.watcher.stop()
            # Remove the descriptor first so clients stop targeting us before the
            # (slower) kernel shutdown, narrowing the post-stop race window.
            clear_runtime(self.notebook)
            try:
                await self._k(self.session.shutdown)
            except Exception:
                log.exception("error shutting down kernel")
            log.info("daemon stopped")

    async def _warm(self) -> None:
        try:
            await self._k(self.session.start)
            await self._k(self.session.warm)
            self.tracker = StalenessTracker(self.session)
            self.inspector = Inspector(self.session, self.config.inspect_timeout_s,
                                       self.config.inspect_sample_rows)
            if self.config.summarizers:
                await self._k(self._load_summarizers)
            self.watcher = FileWatcher(Path.cwd(), self._on_dirty, self.config.watch_debounce_ms)
            self.watcher.configure(self.session.cells, self.config.watch_debounce_ms)
            await self.watcher.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.warm_error = repr(exc)
            log.exception("kernel warm failed")
        finally:
            self.warming = False

    def _on_dirty(self, cells: set[str]) -> None:
        # Called from the watcher task (same event loop thread) -> safe.
        self.dirty_cells |= set(cells)

    def _load_summarizers(self) -> None:
        """Load user summarizer entry points kernel-side (best effort)."""
        from ipykeep.daemon.kernel_manager import _INTERNAL_CELL_ID

        specs = list(self.config.summarizers)
        code = (
            "import ipykeep.inspection as _k\n"
            f"_errs = _k.registry.load_entrypoints({specs!r})\n"
            "print('ipykeep summarizer load errors:', _errs) if _errs else None\n"
        )
        self.session.execute(code, cell_id=_INTERNAL_CELL_ID)

    # ---------------------------------------------------------------- handler
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                resp = await self._dispatch(line)
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("error handling client %s", peer)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _dispatch(self, line: bytes) -> dict[str, Any]:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return {"id": None, "error": {"message": "invalid JSON"}}

        rid = req.get("id")
        if req.get("token") != self.token:
            return {"id": rid, "error": {"message": "unauthorized"}}

        method = req.get("method")
        params = req.get("params") or {}
        if method not in _NON_KERNEL_METHODS and self.warming:
            return {"id": rid, "error": {"message": "kernel is still warming", "warming": True}}

        try:
            result = await self._call(method, params)
            return {"id": rid, "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"id": rid, "error": {"message": repr(exc), "traceback": traceback.format_exc()}}

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"pong": True, "warming": self.warming}
        if method == "status":
            return await self._status()
        if method == "shutdown":
            asyncio.get_running_loop().call_later(0.1, self._stop.set)  # type: ignore[union-attr]
            return {"stopping": True}
        if method in ("execute_cells", "run_cells"):
            return await self._k(self.session.execute_cells, params.get("cell_ids", []))
        if method in ("get_namespace_raw", "get_namespace"):
            return await self._k(self.session.get_namespace_raw)
        if method == "inspect":
            return await self._k(self.inspector.inspect, str(params.get("var_name", "")))
        if method == "get_depgraph":
            return await self._k(self.session.get_depgraph)
        if method == "get_stale_set":
            return await self._stale_set()
        if method == "run_stale":
            return await self._run_stale(bool(params.get("execute", False)))
        if method == "watch_file":
            return await self._watch_file(str(params.get("path", "")))
        raise ValueError(f"unknown method: {method}")

    async def _stale_set(self) -> list[dict[str, Any]]:
        plan = await self._k(self.tracker.compute_plan, set(self.dirty_cells))
        return [{"cell_id": p["cell_id"], "cell_index": p["cell_index"],
                 "reason": p["reason"]} for p in plan]

    async def _run_stale(self, execute: bool) -> dict[str, Any]:
        plan = await self._k(self.tracker.compute_plan, set(self.dirty_cells))
        result: dict[str, Any] = {"stale_cells": plan, "will_execute": execute}
        if execute and plan:
            result["executed"] = await self._k(self.tracker.execute_plan, plan)
            self.dirty_cells -= {p["cell_id"] for p in plan}
        elif execute:
            result["executed"] = {"executed": [], "outputs": [], "errors": []}
        return result

    async def _watch_file(self, path: str) -> dict[str, Any]:
        if self.watcher is None:
            return {"watching": False, "associated_cells": []}
        cells = self.session.load_cells()
        base = Path(path).name
        assoc = {c.cell_id for c in cells if base and (base in c.source or path in c.source)}
        resolved, cell_list = self.watcher.add_manual(path, assoc)
        return {"watching": True, "path": str(resolved), "associated_cells": cell_list}

    async def _status(self) -> dict[str, Any]:
        st = await self._k(self.session.status)
        stale_set: list[dict[str, Any]] = []
        if not self.warming and self.tracker is not None:
            try:
                stale_set = await self._stale_set()
            except Exception:  # noqa: BLE001
                log.exception("status stale-set computation failed")
        st.update({
            "pid": os.getpid(),
            "notebook": str(self.notebook),
            "warming": self.warming,
            "warm_error": self.warm_error,
            "tracked_cells": len(self.session.cells),
            "watched_files": len(self.watcher.path_to_cells) if self.watcher else 0,
            "stale_set": stale_set,
        })
        return st


def run_daemon(notebook: Path) -> None:
    notebook = Path(notebook).resolve()
    config = load_config()
    logging.basicConfig(
        filename=str(log_path(notebook)),
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("daemon starting for %s", notebook)
    try:
        asyncio.run(IpykeepServer(notebook, config).run())
    except Exception:
        log.exception("daemon crashed")
        raise
