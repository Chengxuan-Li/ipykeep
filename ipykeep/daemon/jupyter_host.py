"""Host the kernel inside a Jupyter server that ipykeep owns.

ipykeep launches a headless Jupyter server (``jupyter lab`` / ``notebook`` /
``jupyter_server``), starts a kernel *through* it, and binds that kernel to the
notebook as a session so an IDE (VS Code / JupyterLab / Notebook) can attach to
the very same kernel. ipykeep keeps driving the kernel by attaching a
``BlockingKernelClient`` to its connection file — exactly as it does for a bare
kernel — so nothing downstream of ``KernelSession`` changes.

Verified blueprint: ``scratch/server_front_prototype.py``.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from jupyter_core.paths import jupyter_runtime_dir

log = logging.getLogger("ipykeep.jupyter_host")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()

# command -> python module that launches a ServerApp
_LAUNCHERS = {"lab": "jupyterlab", "notebook": "notebook", "server": "jupyter_server"}
# command -> landing path appended to the base url for humans
_LANDING = {"lab": "lab", "notebook": "tree", "server": ""}


class JupyterServerHost:
    def __init__(self, root_dir: Path, notebook: Path, token: str,
                 command: str = "lab", port: int = 0):
        self.root_dir = Path(root_dir).resolve()
        self.notebook = Path(notebook).resolve()
        self.token = token
        self.command = command if command in _LAUNCHERS else "lab"
        self.port = port

        self.proc: Optional[subprocess.Popen] = None
        self.base_url: Optional[str] = None     # e.g. http://127.0.0.1:8888/
        self.kernel_id: Optional[str] = None
        self.connection_file: Optional[str] = None

    # --------------------------------------------------------------- lifecycle
    def start(self, log_file=None, ready_timeout: float = 90.0) -> None:
        module = _LAUNCHERS[self.command]
        port = self.port or _free_port()
        # Fully control the URL: bind 127.0.0.1 on a known port, and disable
        # port-retries so the server can't silently land on a different port
        # (which would make base_url wrong). Port 0 is unusable here -- the
        # server's reported URL then contains a literal ":0".
        self.base_url = f"http://127.0.0.1:{port}/"
        argv = [
            sys.executable, "-m", module, "--no-browser",
            "--ServerApp.open_browser=False",
            "--ServerApp.ip=127.0.0.1",
            f"--ServerApp.port={port}",
            "--ServerApp.port_retries=0",
            f"--IdentityProvider.token={self.token}",
            f"--ServerApp.root_dir={self.root_dir}",
        ]
        kwargs: dict[str, Any] = dict(stdin=subprocess.DEVNULL,
                                      stdout=log_file or subprocess.DEVNULL,
                                      stderr=log_file or subprocess.DEVNULL)
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        log.info("launching jupyter %s on %s (root_dir=%s)", self.command, self.base_url, self.root_dir)
        self.proc = subprocess.Popen(argv, **kwargs)

        self._await_ready(ready_timeout)
        log.info("jupyter server up at %s", self.base_url)

        self.kernel_id = self._api("POST", "api/kernels", {"name": "python3"})["id"]
        log.info("started kernel %s via server", self.kernel_id)
        self._bind_session()
        self.connection_file = self._await_connection_file()

    def _await_ready(self, timeout: float) -> None:
        """Poll the server's REST API until it answers (or the process dies)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"jupyter {self.command} exited early (code {self.proc.returncode}); "
                    f"is it installed? try `pip install {self._pip_hint()}`"
                )
            try:
                self._api("GET", "api/status", timeout=3)
                return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError(f"jupyter {self.command} did not become ready within {timeout}s")

    def _pip_hint(self) -> str:
        return {"lab": "jupyterlab", "notebook": "notebook"}.get(self.command, "jupyter-server")

    def _bind_session(self) -> None:
        """Bind the running kernel to the notebook path so IDEs auto-attach."""
        try:
            rel = os.path.relpath(self.notebook, self.root_dir).replace(os.sep, "/")
            if rel.startswith(".."):
                log.warning("notebook %s is outside server root_dir; skipping session bind",
                            self.notebook)
                return
            self._api("POST", "api/sessions", {
                "path": rel,
                "name": self.notebook.name,
                "type": "notebook",
                "kernel": {"id": self.kernel_id},
            })
            log.info("bound kernel to session path %s", rel)
        except Exception:  # noqa: BLE001 - best effort; kernel still usable
            log.exception("failed to bind notebook session (kernel still attachable)")

    def _await_connection_file(self, timeout: float = 15.0) -> str:
        path = Path(jupyter_runtime_dir()) / f"kernel-{self.kernel_id}.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.is_file():
                return str(path)
            time.sleep(0.2)
        raise RuntimeError(f"connection file for kernel {self.kernel_id} not found")

    def attach(self, ready_timeout: float = 60.0):
        """Return a BlockingKernelClient driving the hosted kernel."""
        from jupyter_client import BlockingKernelClient

        kc = BlockingKernelClient()
        kc.load_connection_file(self.connection_file)
        kc.start_channels()
        kc.wait_for_ready(timeout=ready_timeout)
        return kc

    # ------------------------------------------------------------------ status
    @property
    def url(self) -> Optional[str]:
        """Human/IDE-facing URL including the auth token."""
        if not self.base_url:
            return None
        landing = _LANDING.get(self.command, "")
        return f"{self.base_url}{landing}?token={self.token}"

    def is_alive(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            return False
        try:
            self._api("GET", f"api/kernels/{self.kernel_id}", timeout=5)
            return True
        except Exception:
            return False

    def shutdown(self) -> None:
        if self.kernel_id:
            try:
                self._api("DELETE", f"api/kernels/{self.kernel_id}", timeout=5)
            except Exception:
                pass
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        log.info("jupyter server shut down")

    @property
    def server_pid(self) -> Optional[int]:
        return self.proc.pid if self.proc is not None else None

    # --------------------------------------------------------------------- rest
    def _api(self, method: str, path: str, body: Optional[dict] = None,
             timeout: float = 15.0) -> Any:
        if not self.base_url:
            raise RuntimeError("server not started")
        url = self.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"token {self.token}", "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode()) if raw else None
