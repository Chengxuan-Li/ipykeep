# ipykeep — VS Code companion

One-click open of a notebook on its **ipykeep warm kernel**, with the agent's
source edits and cell executions visible **live** in your editor.

## What it does

- **One-click attach.** Registers ipykeep's hosted Jupyter server in VS Code's
  kernel picker (no URL pasting) and opens the notebook on the warm kernel.
- **Watch it run.** When the agent runs `ipykeep run-stale --execute`, the daemon
  *delegates* execution to this extension, which runs those cells through VS
  Code's own pipeline — so outputs stream into your cells in real time instead of
  disappearing into a headless kernel.
- **Live staleness.** A status-bar item shows how many cells are stale and runs
  them on click.

It talks to the local ipykeep daemon over the same loopback JSON-RPC the CLI uses
(reading `{port, token}` from the runtime descriptor), and flips the daemon into
*delegated* execution mode only while attached — headless / CLI / JupyterLab
usage is unaffected.

### How the delegated handshake works

```
agent edits a cell on disk ─▶ VS Code reloads it ─▶ extension marks it stale
agent: ipykeep run-stale --execute
   └▶ daemon (delegated): compute stale set, enqueue ordered cell ids, DON'T run itself
extension long-polls await_run_request ─▶ receives the cell ids
   └▶ runs them via notebook.cell.execute  ─▶ OUTPUTS STREAM INTO YOUR CELLS
   └▶ ipyflow records the run under a VS Code "vscode-notebook-cell:" id
      └▶ daemon's alias table folds that back to the nbformat id (graph stays coherent)
   └▶ extension auto-saves the notebook (keeps it clean for the next edit)
   └▶ reports completion ─▶ daemon resolves the blocked run-stale call
```

The daemon also keeps a **heartbeat**: if the editor disconnects or a run times
out, it falls back to executing directly, so a closed VS Code never wedges the
agent.

---

## Setup on a fresh machine

End-to-end from nothing installed. Commands assume Windows PowerShell or a POSIX
shell; adjust paths accordingly.

### 1. Python side — ipykeep + a Jupyter server

```bash
# Python 3.10+ (developed on 3.13). In a fresh virtualenv:
pip install ipyflow jupyter_client ipykernel watchfiles typer pandas numpy

# A Jupyter server package is REQUIRED for one-click attach (ipykeep --serve):
pip install jupyterlab        # or: pip install notebook  / pip install jupyter-server

# Install ipykeep itself (from the repo root, editable):
pip install -e .
# Verify:
ipykeep --help                # or: python -m ipykeep --help
```

> Without a Jupyter server package, `ipykeep --serve` can't host the kernel and
> the extension has no server to register — the one-click flow won't work.

### 2. Node side — build the extension

Requires [Node.js](https://nodejs.org/) 18+ (ships with `npm`).

```bash
cd editors/vscode
npm install          # installs esbuild, typescript, @types/vscode
npm run build        # bundles src/ -> dist/extension.js
# optional sanity:
npm run compile      # type-check only (tsc --noEmit)
```

### 3. The Jupyter extension dependency

This extension declares a hard dependency on Microsoft's Jupyter extension. In
the VS Code instance you'll run it in, install **`ms-toolsai.jupyter`** from the
Marketplace (VS Code will also prompt for it on first activation).

### 4. Load the extension

Two options:

**A. Run from source (recommended while developing).** Open the **repo root** in
VS Code (a ready-made launch config lives at [`.vscode/launch.json`](../../.vscode/launch.json))
and press **F5**. This builds the extension and opens a second window — the
**Extension Development Host** — with the extension loaded live from source.
Equivalently, open the `editors/vscode/` folder directly and press F5.

**B. Package + install a `.vsix`.**

```bash
cd editors/vscode
npx @vscode/vsce package          # produces ipykeep-vscode-<version>.vsix
code --install-extension ipykeep-vscode-*.vsix
```

> The Extension Development Host is **not** an "installed" copy — it runs your
> source live. After editing the TypeScript, rebuild and press **Ctrl+R**
> (Developer: Reload Window) in that window, or run `npm run watch` for
> continuous rebuilds.

### 5. Try it

```bash
# From the repo root, start the warm daemon with a hosted server:
ipykeep start examples/01_eda.ipynb --serve
```

Then, in the window where the extension is loaded:

1. Open `examples/01_eda.ipynb` (File → Open, or open the repo folder).
2. Command Palette (**Ctrl+Shift+P**) → **"ipykeep: Open Live Notebook"**.
   - Use the command, **not** the `vscode://` deep link / `ipykeep open`, when
     testing in the Extension Development Host: the OS routes `vscode://` links to
     your *installed* VS Code, not the dev host. (`ipykeep open` is for real
     installs.)
3. If prompted, pick the **"ipykeep: …"** kernel.
4. Edit a cell (or let the agent edit it) and run `ipykeep run-stale --execute`
   from a terminal — watch the stale cells re-run with output streaming into the
   editor. View → Output → **"ipykeep"** shows `[watcher]` logs.

> **Keep the notebook clean.** Executing cells leaves the notebook dirty
> (unsaved outputs); the extension auto-saves after each delegated run to avoid
> this. If you ever hit *"notebook is dirty"* and an agent edit isn't reflecting,
> run **File: Revert File** (discards the unsaved buffer and reloads the agent's
> on-disk edit — do **not** Save, which would overwrite the edit).

---

## Known limitation: staleness propagation under heavy iterative editing

This is the most important thing to understand before relying on the live loop.

**Symptom.** After many *single-cell* edit→run cycles, editing an **upstream**
cell may re-run only that cell and **miss its downstream consumers** — so derived
cells silently hold stale values while `run-stale` reports "clean."

**Why it happens.** ipykeep propagates downstream staleness by walking ipyflow's
`cells_where_live` dataflow edges. ipyflow only lists a cell as a *live reader* of
a symbol if that cell's **last execution referenced the symbol's current
version**. When you run cells in *fragmented batches* (a different single cell per
`run-stale`, which is exactly what an iterating agent produces), those live sets
go sparse — a reader that last ran two edits ago is dropped — so the downstream
edges to walk simply aren't there.

**Concrete example** (observed in a 28-action live run): after ~24 single-cell
edits, an upstream edit to `df_clean` re-ran *only* that cell; `summary`, the
sqlite `conn`, `result`, `by_group`, and several added cells were **not** flagged,
even though they consume `df_clean`.

**When it does *not* bite.** A single `run-stale --execute` runs the *entire*
current stale set together, so within one cascade the live sets stay consistent.
A full re-run (e.g. `ipykeep run 0 1 2 … N`) resyncs everything. The gap is
specific to *accumulated* fragmented run history.

**Workaround today.** If you suspect under-propagation after a long editing
session, resync once: `ipykeep run <all cell indices>` (or restart the daemon),
then continue.

---

## Option 2 — robust source-scan propagation (planned)

The durable fix, not yet implemented.

**Necessity.** The live "watch the agent work" loop *is* the fragmented-edit
workload that triggers the limitation above. Relying on ipyflow's dynamic
liveness alone is therefore insufficient for exactly the use case this extension
exists to serve.

**The idea.** Augment `compute_plan` (in
[`ipykeep/staleness/tracker.py`](../../ipykeep/staleness/tracker.py)) with a
**static source-scan fallback**: for each stale cell's `produces_vars`, also mark
any cell whose **source text references that name** (word-boundary match) as
downstream-stale — unioned with the existing ipyflow dataflow edges.

```text
stale set  =  source-hash changes  ∪  watched-file changes
              ∪  ipyflow dataflow downstream            (dynamic, precise)
              ∪  cells whose source references a stale cell's vars   ← NEW (static, robust)
```

**Why it's robust.** Source references don't decay with run history — if a cell's
code contains `df_clean`, it depends on `df_clean` regardless of when it last ran.
So the upstream-edit cascade catches every consumer even after arbitrarily
fragmented editing.

**Trade-off.** Slightly less precise than pure dataflow (a name in a comment or
string could over-mark), mitigated by word-boundary matching and scanning only
real references. Over-marking errs safe — it re-runs a cell that didn't strictly
need it, never the reverse.

**Status.** To be implemented test-first (extend
[`tests/test_alias_normalization.py`](../../tests/test_alias_normalization.py)
with a fragmented-history case, plus the real-kernel e2e in
[`tests/integration/`](../../tests/integration/)). It's a daemon-side change, so
it needs a daemon restart to take effect — the extension is unaffected.

---

## Configuration

VS Code settings (`ipykeep.*`):

| Setting | Default | Meaning |
|---|---|---|
| `ipykeep.pollIntervalSeconds` | `30` | Long-poll window for the delegated-run request loop. |
| `ipykeep.staleRefreshSeconds` | `4` | How often the status bar refreshes the stale count. |

Daemon-side, `delegated_timeout_s` (in `ipykeep.toml`) controls how long the
daemon waits for the watcher before falling back to direct execution.

---

## Validation points (version-sensitive)

These rely on `ms-toolsai.jupyter` APIs whose exact shape varies by version;
confirm them in your environment (see [`src/jupyterApi.ts`](src/jupyterApi.ts)):

- **`createJupyterServerCollection`** provider contract (server registration).
- Whether the warm kernel can be **auto-selected**, or the user confirms it once
  in the picker.

---

## Project layout

| File | Responsibility |
|---|---|
| `src/extension.ts` | Activation, commands, the `vscode://` URI handler, attachment lifecycle. |
| `src/daemonClient.ts` | TCP JSON-RPC client mirroring `ipykeep/client.py`. |
| `src/runtimeDescriptor.ts` / `src/projectHash.ts` | Locate `{port,token}` the same way the CLI does. |
| `src/jupyterApi.ts` | Register the warm server + open/select the notebook kernel. |
| `src/watcher.ts` | Register as watcher, push the cell-id map, long-poll, run cells, auto-save. |
| `src/statusBar.ts` | Stale-count status-bar item + run-stale action. |
