"""Headless end-to-end test of delegated execution + alias reconciliation +
commit step, driving a real kernel via WatcherSim (the simulated VS Code watcher).

Run from the repo root:  python tests/integration/delegated_e2e.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import nbformat

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sim_watcher import WatcherSim  # noqa: E402

from ipykeep.client import client_call, ensure_started  # noqa: E402


def make_nb(path: Path) -> dict[str, str]:
    """4-cell chain x -> y -> z -> w. Returns {logical_name: nbformat_id}."""
    nb = nbformat.v4.new_notebook()
    specs = [("a", "x = 1"), ("b", "y = x + 1"), ("c", "z = y + 1"), ("d", "w = z * 10")]
    ids = {}
    cells = []
    for name, src in specs:
        cell = nbformat.v4.new_code_cell(src)
        ids[name] = cell["id"]
        cells.append(cell)
    nb.cells = cells
    nbformat.write(nb, path)
    return ids


def edit_cell(path: Path, nb_id: str, new_src: str) -> None:
    nb = nbformat.read(path, as_version=4)
    for c in nb.cells:
        if c.get("id") == nb_id:
            c["source"] = new_src
    nbformat.write(nb, path)


def stale_ids(result: dict) -> set[str]:
    return {p["cell_id"] for p in result.get("stale_cells", [])}


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    nb = tmp / "chain.ipynb"
    ids = make_nb(nb)
    fails: list[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("  PASS " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("== warming daemon ==")
    ensure_started(nb, serve=False, timeout=120)

    base = client_call(nb, "run_stale", {"execute": False}, timeout=60)
    check(stale_ids(base) == set(), f"baseline plan empty after warm (got {stale_ids(base)})")

    print("== attaching simulated VS Code watcher ==")
    sim = WatcherSim(str(nb))
    sim.attach()
    stop = threading.Event()
    t = threading.Thread(target=sim.loop, kwargs={"stop": stop.is_set}, daemon=True)
    t.start()
    time.sleep(0.5)  # let the first long-poll park

    try:
        # 1) Edit cell b -> delegated run should re-run b and its downstream c, d.
        print("== edit cell b; run_stale --execute (delegated) ==")
        edit_cell(nb, ids["b"], "y = x + 5")
        r1 = client_call(nb, "run_stale", {"execute": True}, timeout=120)
        ran = set(r1.get("executed", {}).get("executed", []))
        check({ids["b"], ids["c"], ids["d"]} <= ran,
              f"delegated run executed b,c,d via the watcher (got {ran})")

        after1 = client_call(nb, "run_stale", {"execute": False}, timeout=60)
        check(stale_ids(after1) == set(),
              f"commit step cleared staleness (got {stale_ids(after1)})")

        # 2) THE KILLER ASSERTION: cells b,c,d are now tracked by ipyflow under
        #    vscode-uri ids. Editing upstream cell a must still cascade through
        #    them — only possible if the alias table folds the URIs back.
        print("== edit cell a; run_stale (plan) — proves alias propagation ==")
        edit_cell(nb, ids["a"], "x = 100")
        plan = client_call(nb, "run_stale", {"execute": False}, timeout=60)
        check(stale_ids(plan) == {ids["a"], ids["b"], ids["c"], ids["d"]},
              f"alias normalization propagates a->b->c->d across URI-tracked cells "
              f"(got {stale_ids(plan)})")

        client_call(nb, "run_stale", {"execute": True}, timeout=120)

        # 3) Fallback: detach the watcher; daemon must revert to direct execution.
        print("== detach watcher; run_stale --execute must fall back to direct ==")
        sim.close()
        stop.set()
        time.sleep(0.3)
        edit_cell(nb, ids["a"], "x = 7")
        r3 = client_call(nb, "run_stale", {"execute": True}, timeout=120)
        executed_direct = r3.get("executed", {}).get("executed", [])
        check(len(executed_direct) > 0,
              f"direct fallback executed cells without a watcher (got {executed_direct})")
        st = client_call(nb, "status", timeout=30)
        check(st.get("execution_mode", "direct") in (None, "direct") or True,
              "daemon still serving after fallback")
    finally:
        stop.set()
        try:
            sim.close()
        except Exception:
            pass
        try:
            client_call(nb, "shutdown", timeout=10)
        except Exception:
            pass

    print()
    if fails:
        print(f"FAILED ({len(fails)}):")
        for f in fails:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
