# ipykeep — VS Code Live Attach Design

**Date:** 2026-06-28
**Status:** Approved design, pending implementation plan

## Problem

Opening the notebook associated with an ipykeep-activated kernel is not a smooth
experience, especially in VS Code. Today `ipykeep start --serve` hosts the warm
kernel in an ipykeep-owned Jupyter server and prints a `server_url`, but the
"last mile" in VS Code is manual: *Jupyter: Connect to a Remote Jupyter Server →
paste URL → pick the kernel*. There is also no way for the user to watch the
agent work — the agent edits the `.ipynb` on disk and runs cells through
ipykeep's own kernel client, so neither the source edits nor the execution
outputs surface naturally in the editor the user is looking at.

## Goal

Make it a seamless experience for VS Code (the primary target):

1. **One-click open** — open the right notebook on the right (warm) kernel with
   a single action, no URL pasting, no manual kernel picking.
2. **Watch it live** — the user sees both:
   - the agent's **source edits** appear in the cells, and
   - the agent's **cell executions stream their outputs** into those cells in
     real time ("watch it run").

All of this **without regressing** the existing CLI / JupyterLab / headless
(pure-MCP) use cases.

## Non-Goals (v1)

- JupyterLab "watch-it-run" (live streaming of agent-driven executions into Lab
  cells). Lab keeps its current shared-warm-kernel behavior. Live streaming
  would need an equivalent execution-injection path we are not building for Lab
  in v1.
- Real-time collaborative editing (CRDT / Yjs / shared cursors). Source-edit
  visibility is reload-based, not character-level.
- Multiple notebooks per daemon (unchanged: one daemon per notebook).
- Changing *who decides* to execute. The agent still explicitly calls
  `run_stale --execute`; this design only changes *where* the execution runs.

## Background: why the naive approach fails (validated by spike)

The obvious design — "when VS Code is attached, just call
`notebook.cell.execute` so outputs render natively, and let ipyflow keep
tracking" — does not work out of the box. A spike on 2026-06-28 confirmed why.

**Output rendering is frontend-owned.** In the Jupyter messaging protocol, a
cell's outputs render in whichever frontend *issued* the `execute_request`
(VS Code maps its own request's `msg_id` to the cell and routes iopub output
there). When ipykeep runs a cell through its own kernel client, the outputs
carry ipykeep's `msg_id`; VS Code has no cell to attach them to and drops them.
There is no way to "forward" ipykeep-issued outputs into a VS Code cell — the
execution must be *issued by VS Code* for the user to watch it.

**VS Code's `cellId` is a different identity namespace.** VS Code sets
`execute_request` metadata `cellId = cell.document.uri.toString()` — a
`vscode-notebook-cell:/…#<handle>` URI — **not** the nbformat `cell.id` that
ipykeep keys every cell on (`kernel_manager.py`, `_load_cells`). The spike
warmed a 2-cell notebook (nbformat ids `cell-a`, `cell-b`) through ipykeep, then
re-ran cell B the way VS Code does (with a `vscode-notebook-cell:` URI as
`cellId`) and inspected ipyflow + ipykeep's depgraph:

```
BEFORE:  y  defining_cell = "cell-b"
AFTER:   y  defining_cell = "vscode-notebook-cell:/c%3A/proj/nb.ipynb#W3sZmlsZQ%3D%3D"
         x  live_cells   = ["cell-b", "vscode-notebook-cell:…"]   ← one logical cell, two identities
```

ipyflow registered the VS-Code-issued run as a *new, foreign cell*. ipykeep's
nbformat-id-keyed staleness graph split: the cell ipykeep knows as `cell-b` and
the cell VS Code ran are, to ipyflow, two different cells, so ipykeep loses
track of which notebook cell defined `y`.

**Conclusion:** delegating execution to VS Code is the right approach for
watch-it-run, but it *requires* an explicit id-reconciliation layer. That layer
is the load-bearing part of this design, not a footnote.

## Architecture

Three pieces change/are added: new daemon RPC methods, a tracker
id-reconciliation layer, and a thin VS Code companion extension. A small CLI
command wires the one-click entry point.

```
┌──────────────────────────────────────────────────────────────┐
│ VS Code  +  ipykeep-vscode extension                         │
│  • one-click open (server collection + open nb + pick kernel)│
│  • registers as a watcher; pushes cell-id map                │
│  • long-polls for run requests; runs cells via VS Code       │
│  • decorates stale cells; "Run stale (N)" button             │
└───────────────┬───────────────────────────────┬──────────────┘
                │ TCP loopback JSON-RPC          │ Jupyter server
                │ (port+token from descriptor)   │ (attach to warm kernel)
                ▼                                 ▼
┌──────────────────────────────────────────────────────────────┐
│ ipykeep daemon                                               │
│  • execution mode: direct (default) ⇄ delegated (watcher)   │
│  • watcher coordination RPC (register / map / long-poll)    │
│  • tracker id-reconciliation (alias: vscode uri → nbformat) │
│  • KernelSession (--serve host) — unchanged kernel driving  │
└──────────────────────────────────────────────────────────────┘
```

### Component 1 — Daemon watcher coordination (new RPC methods)

Added to the existing TCP loopback JSON-RPC surface (`daemon/server.py`). The
extension is an RPC *client* and reads the runtime descriptor (port + token)
exactly as the CLI does.

| Method | Params | Returns | Purpose |
|---|---|---|---|
| `register_watcher` | `{client: str}` | `{watcher_id}` | Mark a delegating frontend attached → set mode `delegated`. |
| `unregister_watcher` | `{watcher_id}` | `{ok}` | Revert to `direct`. |
| `set_cell_id_map` | `{map: [{nbformat_id, vscode_uri, index}]}` | `{ok}` | Push the alias table; called on attach and on notebook structural change. |
| `await_run_request` | `{watcher_id, timeout}` | `{run_id, cells:[nbformat_id]}` or `{run_id: null}` | Long-poll: blocks until a delegated run is pending or times out (re-poll). |
| `report_run_complete` | `{run_id, results:[{nbformat_id, error}]}` | `{ok}` | Extension reports it finished running the cells; resolves the pending `run_stale --execute`. |

**Execution-mode switch.** A single daemon flag, default `direct`.
- `direct`: `run_stale --execute` / `run_cells` execute via ipykeep's own
  `KernelSession` client — **identical to today**.
- `delegated`: `run_stale --execute` / `run_cells` compute the ordered stale
  cell list, enqueue it as a pending run request (fulfilling any waiting
  `await_run_request` long-poll), and block until `report_run_complete` — they
  do **not** run cells through ipykeep's client.

**Heartbeat / liveness.** `await_run_request` polls act as the watcher
heartbeat. If no poll arrives within a timeout (e.g. 2× the long-poll window),
the daemon auto-reverts to `direct` mode and clears the alias table, so a
crashed or closed VS Code never wedges the agent. `unregister_watcher` does the
same explicitly.

### Component 2 — Tracker id reconciliation

`staleness/tracker.py` (and the kernel-side `_DEPGRAPH_CODE` consumer in
`kernel_manager.py`) gain an **alias table** keyed `vscode_uri → nbformat_id`,
populated from `set_cell_id_map`.

- When building the depgraph / stale set, any cell id that is not a known
  nbformat id is normalized through the alias table back to its nbformat id
  before propagation. This collapses the "two identities for one cell" split the
  spike exposed.
- In `direct` mode the alias table is empty and normalization is a no-op → the
  depgraph and stale set are byte-identical to today. This is the compatibility
  guarantee at the tracker level.

### Component 3 — VS Code companion extension (`ipykeep-vscode`)

Thin TypeScript extension. Kept deliberately small so the testable logic lives
in ipykeep (Python), not in the extension host.

**One-click open** — a command `ipykeep: Open Live Notebook` and a URI handler
`vscode://ipykeep.ipykeep-vscode/open?notebook=<path>` (so the CLI/agent can
trigger it). On invocation it:
1. Ensures the daemon is running in `--serve` mode for the notebook (starts it
   if needed, or surfaces a clear error).
2. Registers ipykeep's Jupyter server with VS Code via
   `createJupyterServerCollection` (URL + token from the daemon status) so it
   appears in the picker without the user pasting anything.
3. Opens the notebook document.
4. Selects the warm kernel for that notebook via the Jupyter extension's kernel
   API / controller selection.

**Watcher loop** — after open:
1. `register_watcher`.
2. Build and `set_cell_id_map`: for each cell, map its VS Code cell URI to the
   nbformat `cell.id`. Source of the nbformat id: the cell metadata if present,
   otherwise by positional correspondence against the `.ipynb` on disk (cell
   order is preserved). Rebuild on notebook structural change (add/remove/move).
3. Loop: `await_run_request`; on a non-null request, resolve each nbformat id to
   its VS Code cell index and call
   `vscode.commands.executeCommand('notebook.cell.execute', { ranges, document })`
   so outputs **stream into the cells live**; then `report_run_complete`.

**Source-edit visibility** — the agent edits the `.ipynb` on disk; VS Code
auto-reloads a non-dirty notebook to reflect new source. The extension
additionally:
- decorates stale/changed cells (pulled via `get_stale_set`) and offers a
  "Run stale (N)" button (which drives the same delegated path), and
- on a dirty-document conflict (user has unsaved edits when an external edit
  lands) **warns rather than clobbering** — the agent's edit remains on disk for
  the user to reconcile.

### Component 4 — CLI glue

`ipykeep open <notebook>`:
1. Boots / confirms the `--serve` daemon for the notebook.
2. Fires the `vscode://ipykeep.ipykeep-vscode/open?...` URI (via `code
   --open-url` or the OS URL handler) to trigger the extension's one-click flow.
3. Falls back to printing the server URL + instructions if VS Code or the
   extension is not present (degrades to today's behavior).

## Data Flow — the watch-it-run loop

```
agent edits cell source (NotebookEdit → .ipynb on disk)
   └─▶ VS Code reflects new source (auto-reload); extension decorates it stale
agent calls  run_stale --execute  (CLI or MCP)
   └─▶ daemon, mode=delegated: compute ordered stale nbformat-ids,
       enqueue as pending run, DO NOT run via ipykeep's client; block
extension long-poll  await_run_request  ─▶ receives [cell-b, …]
   └─▶ map nbformat-id → VS Code cell index
   └─▶ notebook.cell.execute(ranges)  ─▶ VS Code issues the execute_request
       └─▶ kernel runs; OUTPUTS STREAM INTO VS CODE CELLS LIVE  ✦user watches✦
       └─▶ ipyflow records under the vscode uri id
           └─▶ ipykeep normalizes uri → nbformat id via alias table (graph stays coherent)
extension  report_run_complete(results)  ─▶ daemon resolves the blocked run_stale call
```

## Compatibility Guarantee

Restated as an invariant: **execution mode defaults to `direct`, and only
`register_watcher` flips it to `delegated`.**

- **Pure CLI / MCP / headless agent:** never registers a watcher → always
  `direct` → executes via ipykeep's own client exactly as today. Alias table
  empty → depgraph unchanged.
- **JupyterLab:** attaches to the same warm kernel via the server URL as today;
  ipykeep executes directly. No regression; it simply does not get the
  VS-Code-only watch-it-run upgrade.
- **VS Code + extension:** the only configuration that sets `delegated` mode.

The delegation logic and id-alias normalization are both gated behind the
watcher flag; with the flag off (or the extension absent), ipykeep is
behaviorally identical to its current release.

## Failure Handling

- **Watcher disconnect / crash mid-session:** missed heartbeat (no
  `await_run_request` poll within timeout) → daemon auto-reverts to `direct`,
  clears the alias table. The agent can continue executing directly.
- **Delegated run fails** (`notebook.cell.execute` errors, or a mapped cell was
  deleted so the id map is stale): extension reports the error via
  `report_run_complete`; for that run the daemon may fall back to direct
  execution and logs the discrepancy. The id map is rebuilt on the next
  structural change.
- **Dirty notebook in VS Code:** extension warns and does not clobber unsaved
  user edits; the agent's on-disk edit is preserved for manual reconciliation.

## Testing

- The core risk (id-namespace split) is already de-risked by the
  2026-06-28 spike (`scratch/` reproduction).
- **Unit:** tracker alias-normalization — a foreign `vscode-notebook-cell:` id
  resolves to the correct nbformat id in the depgraph / stale set.
- **Regression:** with no watcher registered, `run_stale` (plan + execute) and
  the depgraph are unchanged vs. the current behavior (direct mode).
- **Daemon protocol:** `register_watcher` → `await_run_request` long-poll →
  `run_stale --execute` enqueues → `report_run_complete` resolves, end to end,
  with a fake watcher client (no VS Code needed).
- **Extension:** manual test matrix (one-click open attaches warm kernel; edit a
  cell and see the source update; `run_stale --execute` and watch outputs stream
  into the cells; stale-cell decorations correct). Kept manual because extension
  host integration testing is heavy; the extension is intentionally thin.

## Scope Note

CLAUDE.md states ipykeep "does not provide a notebook editor or UI." The
companion extension is integration glue (server registration, kernel selection,
execution delegation) plus lightweight cell decorations — not an editor. This is
a deliberate, scoped expansion of that principle to deliver the one-click +
live-watch experience, and is called out here so the boundary stays explicit.
```
