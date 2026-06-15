"""Staleness computation.

Two independent sources, unioned:
  * Source-code change  — sha256 of a cell's source differs from the hash at its
    last execution (tracked in ``KernelSession.cells``).
  * External file change — a watched file a cell reads from was modified
    (cell ids supplied by the file watcher).

Directly-stale cells are then expanded downstream over ipyflow's dataflow graph:
each stale cell's defined symbols are looked up (kernel-side, see
``KernelSession.get_depgraph``) and every cell where those symbols are live is
marked ``upstream_stale``, recursively to a fixpoint.

The result is a structured plan; nothing executes unless ``execute_plan`` is
called.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ipykeep.daemon.kernel_manager import CellInfo, KernelSession


class StalenessTracker:
    def __init__(self, session: KernelSession):
        self.session = session

    def compute_plan(self, dirty_cells: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        session = self.session
        current = session.load_cells()
        stored = {c.cell_id: c.source_hash for c in session.cells}
        known_ids = {c.cell_id for c in current}

        # Source 1: source-code change (or never-executed cell).
        reasons: dict[str, str] = {}
        for c in current:
            old = stored.get(c.cell_id)
            if old is None or old != c.source_hash:
                reasons[c.cell_id] = "source_changed"

        # Source 2: watched-file modification.
        for cid in (dirty_cells or ()):
            if cid in known_ids:
                reasons.setdefault(cid, "file_modified")

        # Dataflow edges from ipyflow (kernel-side).
        defines, downstream = self._dataflow(known_ids)

        # Propagate downstream to a fixpoint.
        work = list(reasons)
        while work:
            cid = work.pop()
            for nxt in downstream.get(cid, ()):  # cells using a symbol cid defines
                if nxt not in reasons:
                    reasons[nxt] = "upstream_stale"
                    work.append(nxt)

        plan: list[dict[str, Any]] = []
        for c in sorted(current, key=lambda c: c.index):
            if c.cell_id in reasons:
                plan.append({
                    "cell_index": c.index,
                    "cell_id": c.cell_id,
                    "reason": reasons[c.cell_id],
                    "produces_vars": sorted(defines.get(c.cell_id, [])),
                })
        return plan

    def _dataflow(self, known_ids: set[str]) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
        """Return (cell_id -> vars it defines, cell_id -> downstream cell_ids)."""
        defines: dict[str, list[str]] = {}
        downstream: dict[str, set[str]] = {}
        for sym in self.session.get_depgraph():
            dc = sym.get("defining_cell")
            if dc not in known_ids:
                continue
            defines.setdefault(dc, []).append(sym["name"])
            for live in sym.get("live_cells", []):
                if live in known_ids and live != dc:
                    downstream.setdefault(dc, set()).add(live)
        return defines, downstream

    def execute_plan(self, plan: list[dict[str, Any]]) -> dict[str, Any]:
        cell_ids = [entry["cell_id"] for entry in plan]
        if not cell_ids:
            return {"executed": [], "outputs": [], "errors": []}
        return self.session.execute_cells(cell_ids)
