"""Tracker id-reconciliation: VS-Code-issued executions tag ipyflow's dependency
graph with a foreign ``vscode-notebook-cell:`` URI instead of the nbformat cell
id. The alias table folds those back so downstream staleness stays coherent.

These are pure-logic tests over ``StalenessTracker`` with a fake session — no
kernel required.
"""
from __future__ import annotations

import hashlib

from ipykeep.daemon.kernel_manager import CellInfo
from ipykeep.staleness.tracker import StalenessTracker

URI_B = "vscode-notebook-cell:/c%3A/proj/nb.ipynb#W3sZmlsZQ%3D%3D"


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _cell(index: int, cid: str, source: str) -> CellInfo:
    return CellInfo(index=index, cell_id=cid, source=source, source_hash=_hash(source))


class FakeSession:
    """Minimal stand-in exposing what StalenessTracker.compute_plan needs."""

    def __init__(self, current: list[CellInfo], warmed: list[CellInfo],
                 depgraph: list[dict]):
        self._current = current
        self.cells = warmed
        self._depgraph = depgraph

    def load_cells(self) -> list[CellInfo]:
        return self._current

    def get_depgraph(self) -> list[dict]:
        return self._depgraph


def _scenario():
    """cell-a defines x; cell-b (run by VS Code, so tracked under URI_B) defines y
    from x; cell-c defines z from y. cell-a's source was edited since warm."""
    current = [
        _cell(0, "cell-a", "x = 2"),          # edited
        _cell(1, "cell-b", "y = x + 1"),
        _cell(2, "cell-c", "z = y + 1"),
    ]
    warmed = [
        _cell(0, "cell-a", "x = 1"),          # old hash -> cell-a is source_changed
        _cell(1, "cell-b", "y = x + 1"),
        _cell(2, "cell-c", "z = y + 1"),
    ]
    # ipyflow records cell-b's run under the VS Code URI, not "cell-b".
    depgraph = [
        {"name": "x", "defining_cell": "cell-a", "live_cells": [URI_B]},
        {"name": "y", "defining_cell": URI_B, "live_cells": ["cell-c"]},
        {"name": "z", "defining_cell": "cell-c", "live_cells": []},
    ]
    return current, warmed, depgraph


def test_alias_normalizes_foreign_ids_so_downstream_propagates():
    current, warmed, depgraph = _scenario()
    tracker = StalenessTracker(FakeSession(current, warmed, depgraph))
    tracker.set_alias_map([{"vscode_uri": URI_B, "nbformat_id": "cell-b", "index": 1}])

    plan = tracker.compute_plan()
    stale = {p["cell_id"] for p in plan}

    # Editing cell-a must cascade through cell-b (tracked under URI_B) to cell-c.
    assert stale == {"cell-a", "cell-b", "cell-c"}, stale
    reasons = {p["cell_id"]: p["reason"] for p in plan}
    assert reasons["cell-a"] == "source_changed"
    assert reasons["cell-b"] == "upstream_stale"
    assert reasons["cell-c"] == "upstream_stale"


def test_without_alias_the_graph_splits_demonstrating_the_bug():
    current, warmed, depgraph = _scenario()
    tracker = StalenessTracker(FakeSession(current, warmed, depgraph))
    # No alias map (direct mode): foreign URI ids are dropped by _dataflow.

    plan = tracker.compute_plan()
    stale = {p["cell_id"] for p in plan}

    # Downstream propagation is lost: only the directly-edited cell is flagged.
    assert stale == {"cell-a"}, stale


def test_empty_alias_is_a_noop_for_pure_nbformat_graphs():
    """Direct mode (no foreign ids anywhere) is unchanged by the alias machinery."""
    current = [_cell(0, "cell-a", "x = 2"), _cell(1, "cell-b", "y = x + 1")]
    warmed = [_cell(0, "cell-a", "x = 1"), _cell(1, "cell-b", "y = x + 1")]
    depgraph = [
        {"name": "x", "defining_cell": "cell-a", "live_cells": ["cell-b"]},
        {"name": "y", "defining_cell": "cell-b", "live_cells": []},
    ]
    tracker = StalenessTracker(FakeSession(current, warmed, depgraph))

    plan = tracker.compute_plan()
    stale = {p["cell_id"] for p in plan}
    assert stale == {"cell-a", "cell-b"}, stale
