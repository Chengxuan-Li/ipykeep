# ipykeep

**Persistent, stateful access to a live Jupyter kernel for AI coding agents.**

ipykeep is a lightweight CLI + (planned) MCP server that gives AI coding agents
— Claude Code, Cursor, Codex CLI, and any MCP-compatible harness — a long-lived
Jupyter kernel they can reason about between edits. It does two things:

1. **Staleness tracking** — after an agent edits notebook cells (or an external
   data file changes on disk), ipykeep reports exactly which cells need to
   re-execute and which do not, so expensive upstream steps (data loads, DB
   queries) are never needlessly re-run.
2. **Variable inspection** — the agent queries the live namespace for typed JSON
   summaries of variables (shape, dtypes, stats, sample rows, memory size)
   without executing extra code or scraping stdout.

It is purely a backend daemon + CLI + tool surface. It does **not** replace the
notebook editor, require a special notebook format, or manage a UI.

> **Status:** validated vertical slice. The daemon, kernel manager, staleness
> tracker, file watcher, and variable inspector are implemented and tested
> end-to-end on Windows / Python 3.13 — including the MCP server (`ipykeep
> mcp-serve`) and `ipykeep init` scaffolding. A **VS Code companion extension**
> (one-click open + watch-the-agent-run-live) lives in
> [`editors/vscode/`](editors/vscode/) — see
> [`ipykeep open`](#watch-the-agent-work-live-in-vs-code) below. A
> `CONTRIBUTING.md` and the four polished example notebooks are the remaining
> follow-ups. See [`CLAUDE.md`](CLAUDE.md) for the full design and roadmap.

---

## How it works

A per-notebook daemon owns one long-lived `ipykernel` with
[`ipyflow`](https://github.com/ipyflow/ipyflow) loaded in lazy mode. The kernel
is the single source of truth for runtime state. The CLI talks to the daemon
over a local TCP-loopback JSON-RPC channel (port + auth token are written to a
per-project runtime descriptor file).

- **Staleness** is the union of two sources: a SHA-256 diff of each cell's source
  against its last execution, and modification times of external files that cells
  read. Both expand downstream over ipyflow's dataflow graph.
- **Inspection** runs typed summarizers *inside* the kernel — whole objects are
  never transmitted, only JSON summaries. Each summary has a timeout; on timeout
  a degraded summary (type + size) is returned and the kernel is never killed.

`run-stale` defaults to **plan mode**: it prints what it would do. The agent runs
`run-stale --execute` explicitly to apply the plan — ipykeep never re-executes on
its own.

---

## Requirements

- Python 3.10+ (developed and tested on 3.13)
- `ipyflow`, `jupyter_client`, `ipykernel`, `watchfiles`, `typer`
  (and `pandas` / `numpy` for the built-in DataFrame / ndarray summarizers)

```bash
pip install ipyflow jupyter_client ipykernel watchfiles typer pandas numpy
```

> Installing `mcp` (for the planned MCP server) upgrades `starlette`; use a
> dedicated virtualenv if your environment also depends on `fastapi`.

---

## Quickstart

From the project root (the package runs via `python -m ipykeep`; a console-script
entry point is a planned follow-up):

```bash
# 1. Start the daemon: boots the kernel, loads ipyflow, warms every cell once.
python -m ipykeep start examples/01_eda.ipynb

# 2. See what's already warm in the kernel.
python -m ipykeep namespace

# 3. After editing a cell, see which cells are stale (plan only).
python -m ipykeep run-stale

# 4. Re-execute just the stale cells.
python -m ipykeep run-stale --execute

# 5. Inspect a key variable to verify the result.
python -m ipykeep inspect df_clean

# Stop the daemon (and kernel) when done.
python -m ipykeep stop
```

---

## CLI reference

| Command | Description |
|---|---|
| `start <notebook.ipynb>` | Start the daemon, boot the kernel, load ipyflow, warm all cells, watch the notebook and files it reads. |
| `stop [notebook]` | Gracefully shut down the daemon and kernel. |
| `status [notebook]` | Daemon PID, kernel status, tracked cells, watched files, current stale set. |
| `run-stale [--execute]` | Plan mode by default: show stale cells, why, and what they produce. `--execute` runs only the stale cells in order. |
| `run <cell_ids...>` | Execute specific cells by index or id, regardless of staleness. |
| `inspect <var_name>` | JSON summary of a runtime variable (type, shape, dtypes, nulls, stats, sample, size). |
| `namespace` | JSON list of top-level variables with type, size, and last-updated cell. |
| `watch <file_path>` | Manually register an external file dependency that static analysis missed. |
| `open <notebook.ipynb>` | Boot the daemon with a hosted server and open the notebook on the warm kernel in VS Code (one click, via the companion extension). |

When a command needs a daemon and none is running, it says so and points you at
`ipykeep start`. With a single running daemon (or a `notebook` set in config) the
notebook argument is optional.

---

## Use from an AI agent (MCP) + `ipykeep init`

Onboard a project in one command:

```bash
ipykeep init [notebook.ipynb]   # scaffolds .mcp.json, a Claude SKILL.md,
                                # AGENTS.md, and ipykeep.toml (non-destructive)
```

That registers an MCP server so agents call ipykeep as **native tools** instead of
shelling out:

```bash
pip install "ipykeep[mcp]"      # MCP SDK
ipykeep mcp-serve               # stdio server (your harness launches this via .mcp.json)
```

Tools exposed: `start_session`, `stop_session`, `run_stale`, `run_cells`,
`inspect`, `get_namespace`, `get_stale_set`, `watch_file`, `kernel_status`. The
agent calls `start_session` first to warm the kernel, then runs the
edit → `run_stale` → `run_stale(execute=true)` → `inspect` loop. The generated
`SKILL.md`/`AGENTS.md` teach the agent this workflow.

---

## Connecting an IDE to the live kernel (`--serve`)

By default ipykeep runs a **bare** kernel — great for headless agents, but IDEs
(VS Code, JupyterLab/Notebook) connect to a Jupyter *server*, not a bare kernel.
Start with `--serve` and ipykeep hosts the kernel inside a Jupyter server **it
owns**, so you can attach an IDE to the very same warm kernel the agent drives:

```bash
ipykeep start --serve analysis.ipynb        # (requires: pip install "ipykeep[serve]")
# -> prints:  IDE: open http://127.0.0.1:<port>/lab?token=<token>
```

- **Browser**: open that URL — JupyterLab opens the notebook already bound to the
  running kernel.
- **VS Code**: *Jupyter: Connect to a Remote Jupyter Server* → paste the URL →
  open the notebook → pick the running kernel.

`ipykeep status` reports `server_url` and `server_token`. The launcher is
configurable (`server_command = "lab" | "notebook" | "server"`). ipykeep keeps
driving the kernel exactly as in bare mode; the IDE is just another client of the
same kernel.

**Caveats:** it's one shared namespace, so (1) let the agent own cell execution
via `run-stale --execute` and use the IDE for inspection/scratch — manually
running tracked cells from the IDE can lag ipykeep's stale set until the next
`run-stale`; and (2) only one side should edit/save the `.ipynb` file at a time.
Upside: with an IDE attached, outputs are persisted back to the notebook (the
CLI-only path does not write outputs).

---

## Watch the agent work live in VS Code

The [`editors/vscode/`](editors/vscode/) companion extension turns the manual
`--serve` attach into a one-click, watch-it-run experience:

```bash
ipykeep open examples/01_eda.ipynb     # boots --serve + opens VS Code on the warm kernel
```

With the extension installed, the notebook opens already bound to the warm
kernel (no URL pasting, no manual kernel pick), and — crucially — when the agent
runs `run-stale --execute`, the daemon **delegates** execution to VS Code so the
cells re-run and their **outputs stream into your editor live**, instead of
disappearing into a headless kernel. Source edits the agent makes on disk show
up too, and a status-bar item shows the current stale count.

This is purely additive and watcher-gated: with no extension attached, the
daemon behaves exactly as the CLI/headless path described above.

See **[`editors/vscode/README.md`](editors/vscode/README.md)** for fresh-machine
setup, how the delegated-execution handshake works, and a **known
staleness-propagation limitation** under heavy iterative editing (plus the
planned fix).

---

## Configuration

Optional `[tool.ipykeep]` table in `ipykeep.toml` or `pyproject.toml`:

```toml
[tool.ipykeep]
notebook = "examples/01_eda.ipynb"   # default notebook for this project
inspect_timeout_s = 5                 # per-summary timeout
inspect_sample_rows = 5               # rows returned by the DataFrame summarizer
watch_debounce_ms = 500               # file watcher debounce
log_level = "INFO"
serve = false                         # host the kernel in a Jupyter server (IDE attach)
server_command = "lab"                # "lab" | "notebook" | "server"
server_port = 0                       # 0 = pick a free port

[tool.ipykeep.inspection]
summarizers = []                      # "module.path:callable" custom summarizers
```

## Extending inspection

Register a custom summarizer from a notebook or init cell:

```python
from ipykeep.inspection.inspector import summarizer, registry

@summarizer(match=lambda obj: hasattr(obj, "rio"))   # e.g. a rioxarray DataArray
def rioxarray_summary(obj):
    return {"type": "rioxarray.DataArray", "shape": list(obj.shape),
            "crs": str(obj.rio.crs), "dtype": str(obj.dtype)}
```

Lookup order: user-registered summarizers (first match wins) → built-in
type-specific summarizers → generic fallback (type + size + truncated repr).

---

## License

TBD.
