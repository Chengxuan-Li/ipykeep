"""Inspection orchestration.

Two halves live here:

* ``emit_summary`` runs **kernel-side**: it looks the variable up in the live
  namespace, runs the matched summarizer inside a ``ThreadPoolExecutor`` with a
  timeout, and prints a one-line sentinel JSON. On timeout it emits a degraded
  summary (type + size) with ``summary_status="timeout"``; the kernel is never
  interrupted (the worker thread is simply abandoned).

* ``Inspector`` runs **daemon-side**: it asks the kernel to emit a summary for a
  named variable and parses the result. Re-exports ``registry`` and
  ``summarizer`` so users can extend inspection from a notebook via
  ``from ipykeep.inspection.inspector import summarizer``.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ipykeep.inspection.registry import SummarizerRegistry, registry, summarizer
from ipykeep.inspection.summarizers import _safe_sizeof, is_thread_sensitive, summarize

if TYPE_CHECKING:  # avoid importing jupyter-side modules when used in a kernel
    from ipykeep.daemon.kernel_manager import KernelSession

__all__ = ["emit_summary", "Inspector", "registry", "summarizer", "SummarizerRegistry"]

_JSON_SENTINEL = "__IPYKEEP_JSON__"


def _emit(payload: dict) -> None:
    print(_JSON_SENTINEL + json.dumps(payload, default=str))


def emit_summary(name: str, timeout_s: float = 5.0, sample_rows: int = 5) -> None:
    """Kernel-side entry point. Prints a sentinel-prefixed JSON summary."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    try:
        from IPython import get_ipython
        ns = get_ipython().user_ns
    except Exception:
        ns = {}

    if name not in ns:
        _emit({"name": name, "summary_status": "not_found"})
        return

    obj = ns[name]
    base = {"name": name, "type": type(obj).__name__, "size_bytes": _safe_sizeof(obj)}

    def _finish(result: dict) -> None:
        result.setdefault("name", name)
        result.setdefault("size_bytes", base["size_bytes"])
        result.setdefault("summary_status", "ok")
        _emit(result)

    def _inline() -> None:
        try:
            _finish(summarize(obj, sample_rows=sample_rows))
        except Exception as exc:  # noqa: BLE001
            base["summary_status"] = "error"
            base["error"] = repr(exc)
            _emit(base)

    # Thread-affine objects (DB connections) must be summarized on the kernel
    # execution thread; their summarizers are cheap, so run them inline.
    if is_thread_sensitive(obj):
        _inline()
        return

    # Otherwise run in a worker thread so a slow summary can time out and return
    # a degraded result promptly (the worker is then abandoned; the kernel is
    # never interrupted).
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(summarize, obj, sample_rows=sample_rows)
        try:
            _finish(future.result(timeout=timeout_s))
        except FuturesTimeout:
            base["summary_status"] = "timeout"
            _emit(base)
        except Exception:
            _inline()
    finally:
        pool.shutdown(wait=False)


class Inspector:
    def __init__(self, session: "KernelSession", timeout_s: float = 5.0, sample_rows: int = 5):
        self.session = session
        self.timeout_s = timeout_s
        self.sample_rows = sample_rows

    def inspect(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not name.isidentifier():
            return {"name": name, "summary_status": "error", "error": "invalid variable name"}
        from ipykeep.daemon.kernel_manager import _INTERNAL_CELL_ID, _extract_sentinel_json

        code = (
            "import ipykeep.inspection as _ipykeep_insp\n"
            f"_ipykeep_insp.emit_summary({name!r}, timeout_s={self.timeout_s}, "
            f"sample_rows={self.sample_rows})"
        )
        r = self.session.execute(code, cell_id=_INTERNAL_CELL_ID, timeout=self.timeout_s + 15)
        data = _extract_sentinel_json(r.stdout)
        if isinstance(data, dict):
            return data
        return {
            "name": name,
            "summary_status": "error",
            "error": (r.error or r.stderr or "no summary produced").strip()[:500],
        }
