# Phase 0 Findings — ipyflow State API validation

Environment: Windows 11, Python 3.13.9 (Anaconda), `ipyflow` 0.0.229 /
`ipyflow-core` 0.0.229 / `pyccolo` 0.0.86, `jupyter_client` 8.6.3,
`ipykernel` 6.31.0. Driven headlessly via `jupyter_client.start_new_kernel()`
with `%load_ext ipyflow` (extension mode) + `%flow mode lazy`.

**Gate result: PASSED.** ipyflow loads and tracks dataflow correctly on 3.13.
Probe scripts: `scratch/phase0_probe*.py`.

## 1. The public State API is tracer-dependent — do NOT call it from the daemon

`deps`, `users`, `timestamp`, `lift`, `code` in `ipyflow/api/lift.py` are
**stubs**. The real work is done by ipyflow's AST tracer "argument handler",
which rewrites the *literal argument expression* (`deps(y)`) into its `Symbol`
metadata before the function body runs. Consequences:

- They only work when called as a **direct literal expression in traced cell
  code**. Wrapping in a lambda / passing via indirection raises
  `ValueError: unable to lookup metadata for symbol`. (This caused a false alarm
  in probe #1.)
- The daemon runs *outside* the kernel, so it cannot call these at all.

**Decision:** the daemon never uses the lift-based API. It works directly with
`Symbol` objects obtained from the flow singleton (below). This is fully
confirmed working.

## 2. Daemon-side access pattern (all run inside the kernel via `execute`)

```python
from ipyflow.singletons import flow      # flow() -> NotebookFlow singleton
fl = flow()
gs = fl.global_scope                      # Scope
sym = gs.get("df")                        # Optional[Symbol]  (by var name; robust)
```

`Symbol` (`ipyflow/data_model/symbol.py`) exposes everything we need:
- `sym.name` -> str
- `sym.timestamp` -> `Timestamp(cell_num, stmt_num)`; `sym.timestamp.cell_num`
  is the **cell counter** of the cell that last updated the symbol.
- `sym.children` -> `Dict[Symbol, ...]` (downstream symbols; = `users`)
- `sym.parents` -> `Dict[Symbol, ...]` (upstream symbols; = `deps`)
- `sym.cells_where_live` -> `Set[Cell]` (cells that USE the symbol) — the key
  primitive for cell→cell propagation. Also `cells_where_deep_live` /
  `cells_where_shallow_live`.

**Do NOT use `flow().all_symbols()`** — iterating it touches the internal
`aliases` dict and raises `RuntimeError: dictionary changed size during
iteration`. Instead enumerate the kernel user-namespace variable names (the
daemon already needs this for `namespace`) and call `gs.get(name)` per name.

## 3. Cell identity & ordering

- `cells()` returns the `Cell` class (`ipyflow/data_model/cell.py`), not a list.
  Classmethods: `cells().from_id_nullable(cell_id)`, `from_position(pos)`,
  `all_executed_cell_ids()`, `set_cell_positions({cell_id: order_index})`.
- A `Cell` has `.cell_id`, `.cell_ctr` (counter), `.position`, `.current_content`.
- **`metadata.cellId` on the `execute_request` IS adopted** as the cell id.
  `kernel_client.execute()` does NOT accept metadata, so the daemon must build
  the message manually:
  ```python
  content = dict(code=code, silent=False, store_history=True,
                 user_expressions={}, allow_stdin=False, stop_on_error=True)
  msg = kc.session.msg("execute_request", content)
  msg["metadata"] = {"cellId": cell_id}
  kc.shell_channel.send(msg)
  ```
- `.position` defaults to **-1** until the daemon calls
  `cells().set_cell_positions({id: index})` to declare notebook order. The
  daemon must do this on warm and whenever the notebook structure changes.
- Maintain `cell_id <-> cell_ctr` mapping from the `Cell` objects so symbol
  `timestamp.cell_num` can be mapped back to a notebook cell id.

## 4. Staleness strategy (confirmed)

ipyflow's own `FrontendCheckerResult` (`ipyflow/frontend.py`,
`flow().check_and_link_multiple_cells()`) is a NamedTuple with `waiting_cells`,
`ready_cells`, `new_ready_cells`, `stale_parents: Dict[cell_id, Set[cell_id]]`
and `.to_json()`. It drives the JupyterLab orange/purple highlighting and is
based on **execution** timestamps. Injecting an *unexecuted* edit to recompute
it requires the frontend comm content-update protocol (`create_and_track` with
`bump_cell_counter=False` asserts the cell is new), so it is **not** a clean fit
for edit-based staleness.

**Adopted design (matches the plan's symbol-graph strategy):**
1. ipykeep owns **directly-stale** detection: `sha256` source-hash diff per cell
   (edited-but-not-run) ∪ watched-file mtime changes.
2. ipykeep propagates **downstream** over the ipyflow graph:
   ```
   stale = set(directly_stale_cell_ids); work = list(stale)
   while work:
       cid = work.pop()
       for name in vars_defined_in(cid):       # timestamp.cell_num == ctr(cid)
           sym = global_scope.get(name)
           for cell in sym.cells_where_live:    # cells that use the symbol
               if cell.cell_id not in stale:
                   stale.add(cell.cell_id); work.append(cell.cell_id)
   ```
   Verified: editing `c2` yields stale `{c2, c3}` with `c0/c1` clean; editing
   `c1` recursively yields `{c1, c2, c3}`.
3. `produces_vars(cid)` = namespace names whose `timestamp.cell_num == ctr(cid)`.
4. `waiting_cells` from `FrontendCheckerResult` may be surfaced as an extra
   signal (cells stale due to a prior partial re-run) but is not the primary
   mechanism.

## 5. Kernel-driving mechanics for Phase 1

- `jupyter_client.manager.start_new_kernel()` returns `(km, kc)` ready to use.
- Execute + collect: send `execute_request`, drain `iopub` for the matching
  `parent_header.msg_id` until `status == idle`, gathering `stream` (stdout),
  `execute_result`/`display_data`, and `error`. Working collector in
  `scratch/phase0_probe2.py::collect`.
- `%load_ext ipyflow` then `%flow mode lazy` on startup (both return cleanly).

## 6. Environment caveat

Installing `mcp` upgraded `starlette` to 1.3.1, which conflicts with a
pre-existing `fastapi` 0.115.6 in this Anaconda base env (`fastapi` requires
`starlette<0.42`). Unrelated to ipykeep runtime, but flag before packaging;
consider a dedicated venv for ipykeep to avoid disturbing the base env.
