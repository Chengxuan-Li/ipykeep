# Integration tests (manual)

These exercise the **delegated execution** path against a *real* kernel, so they
are not part of the automated `pytest` suite (they spin up a daemon and a live
ipykernel). pytest does not collect them — the filenames don't match `test_*.py`.

## `delegated_e2e.py` — headless end-to-end

Self-contained: creates a temporary notebook, starts a bare-kernel daemon,
attaches a simulated watcher in a thread, and asserts the full delegated cycle —
including alias reconciliation, the commit step, and the heartbeat fallback.

```bash
python tests/integration/delegated_e2e.py        # prints PASS/FAIL per check
```

## `sim_watcher.py` — simulated VS Code watcher

Plays the companion extension's RPC role (register → set-cell-map → long-poll →
run-cells-as-VS-Code → report) against a daemon you started yourself. Use it to
drive the delegated path without VS Code:

```bash
ipykeep start examples/01_eda.ipynb               # in one shell
python tests/integration/sim_watcher.py examples/01_eda.ipynb   # in another; leave running
# then, elsewhere: edit a cell and run `ipykeep run-stale --execute`
```

It reproduces the crux the alias table reconciles: it issues each `execute_request`
with a `vscode-notebook-cell:` `metadata.cellId`, exactly as VS Code does.
