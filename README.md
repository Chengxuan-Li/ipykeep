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
> end-to-end on Windows / Python 3.13. The MCP server, `ipykeep init`
> scaffolding, packaging entry point, and polished example notebooks are planned
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

When a command needs a daemon and none is running, it says so and points you at
`ipykeep start`. With a single running daemon (or a `notebook` set in config) the
notebook argument is optional.

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
