# ipykeep — Build Plan

## Project Overview

**ipykeep** is a lightweight CLI + MCP server that gives AI coding agents
(Claude Code, Cursor, Codex CLI, and any MCP-compatible harness) persistent,
stateful access to a live Jupyter kernel.

Its two core capabilities are:

1. **Staleness tracking**: after an agent edits one or more notebook cells (or
   an external file changes on disk), ipykeep tells the agent exactly which
   cells need to re-execute and which do not — without requiring a full
   kernel restart or re-run of expensive upstream steps.

2. **Variable inspection**: the agent can query the live runtime namespace for
   structured summaries of variables (shape, dtypes, statistics, sample rows,
   memory size) without executing additional notebook code or parsing raw stdout.

ipykeep does **not** replace the notebook editor, does **not** require
migration to a reactive notebook format, and does **not** manage its own UI.
It is purely a backend daemon + CLI + MCP surface that agents call into.

---

## Design Principles

- The persistent kernel is the single source of truth for runtime state.
- Staleness is determined from two sources: notebook cell source-code diffs
  (via ipyflow's dataflow graph) and external file modification times (via a
  filesystem watcher). Both produce the same output: a set of stale cell IDs.
- `run_stale` defaults to **plan mode** — it returns a structured plan for the
  agent to inspect before execution. The agent calls `run_stale --execute`
  explicitly to apply the plan.
- Variable inspection never transmits whole objects — only typed JSON summaries.
  All inspection calls have a configurable timeout; on timeout, a degraded
  summary (type + size only) is returned. The kernel is never killed.
- Inspection behavior for unknown types is user-extensible via a simple
  registry API.
- pin/restore is explicitly out of scope for v1.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Agent (Claude Code / Cursor / Codex CLI / etc.)        │
│  — edits .ipynb via its normal file editing tools       │
│  — calls ipykeep tools via CLI or MCP                   │
└────────────┬──────────────────────────┬─────────────────┘
             │ CLI (bash, universal)    │ MCP (structured)
             ▼                          ▼
┌─────────────────────────────────────────────────────────┐
│  ipykeep daemon  (per-project, long-running process)    │
│                                                         │
│  ┌─────────────────┐  ┌──────────────────────────────┐  │
│  │ Kernel Manager  │  │ Staleness Tracker            │  │
│  │                 │  │                              │  │
│  │ jupyter_client  │  │ ipyflow State API            │  │
│  │ wrapping a      │  │ deps() / users() /           │  │
│  │ long-lived      │  │ timestamp() /                │  │
│  │ ipykernel with  │  │ cells().slice()              │  │
│  │ ipyflow loaded  │  │                              │  │
│  └────────┬────────┘  │ + cell source-code hash      │  │
│           │           │   diff table (local state)   │  │
│           │           └──────────────────────────────┘  │
│           │                                             │
│           │           ┌──────────────────────────────┐  │
│           │           │ File Watcher                 │  │
│           │           │                              │  │
│           │           │ watchfiles / watchdog        │  │
│           │           │ tracks mtime of files read   │  │
│           │           │ by notebook cells            │  │
│           │           │ → marks reader cells stale   │  │
│           │           └──────────────────────────────┘  │
│           │                                             │
│           │           ┌──────────────────────────────┐  │
│           └──────────►│ Variable Inspector           │  │
│                       │                              │  │
│                       │ type registry with           │  │
│                       │ per-type summarizer fns      │  │
│                       │ + timeout wrapper            │  │
│                       │ + user-extensible registry   │  │
│                       └──────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
             │
             ▼
    .ipynb file on disk
    external data files (CSV, parquet, DB, etc.)
```

The daemon is started once per project session. It owns the kernel process for
the duration of the session. All CLI calls and MCP tool calls communicate with
the daemon over a local Unix socket (JSON-RPC). The daemon is stateless across
restarts except for the ipyflow-managed dependency graph, which ipyflow
persists into notebook metadata automatically.

---

## Module Structure

```
ipykeep/
├── __init__.py
├── cli.py                  # typer-based CLI entry point
├── daemon/
│   ├── __init__.py
│   ├── server.py           # Unix socket JSON-RPC server, process lifecycle
│   ├── kernel_manager.py   # jupyter_client wrapper; start/stop/execute kernel
│   └── pid.py              # PID file management, daemon detection
├── staleness/
│   ├── __init__.py
│   ├── tracker.py          # cell hash table, ipyflow API queries, stale set computation
│   └── watcher.py          # filesystem watcher; maps file paths -> cell IDs
├── inspection/
│   ├── __init__.py
│   ├── inspector.py        # dispatch to type registry; timeout wrapper
│   ├── summarizers.py      # built-in summarizers: DataFrame, ndarray, dict,
│   │                       # list, str, DB connections (sqlite3, sqlalchemy,
│   │                       # psycopg2), GeoDataFrame
│   └── registry.py         # SummarizerRegistry: register(), lookup(), fallback chain
├── mcp_server.py           # MCP tool definitions wrapping daemon JSON-RPC calls
├── config.py               # config file (pyproject.toml [tool.ipykeep] section)
└── py.typed
```

---

## CLI Reference (target interface)

All commands communicate with the daemon over the local socket.
If no daemon is running, commands that require one print a clear error and
suggest `ipykeep start`.

```
ipykeep start <notebook.ipynb>   Start daemon for this notebook.
                                  Boots kernel, loads ipyflow, runs all cells
                                  once (so upstream state is warm), watches
                                  the notebook file and any files read by cells.

ipykeep stop                     Gracefully shut down daemon and kernel.

ipykeep status                   Print daemon PID, kernel status, number of
                                  tracked cells, number of watched files,
                                  current stale set.

ipykeep run-stale [--execute]    Default (no flag): print a structured plan
                                  showing which cells are stale, why (source
                                  changed / upstream dependency stale / watched
                                  file modified), and what variables each cell
                                  produces. Does not execute anything.
                                  --execute: apply the plan — execute only the
                                  stale cells in notebook order.

ipykeep run <cell_ids>           Execute specific cells by index or ID,
                                  regardless of staleness state. Useful when
                                  agent wants to override the plan.

ipykeep inspect <var_name>       Return a JSON summary of a runtime variable.
                                  Includes: type, shape/len, dtypes (if
                                  applicable), null_counts, basic statistics,
                                  sample rows/items, memory size in bytes.
                                  Respects inspection timeout (default 5s).

ipykeep namespace                Return a JSON list of all top-level variables
                                  in the kernel namespace with type, size, and
                                  last-updated cell index.

ipykeep watch <file_path>        Manually register an external file as a
                                  dependency of the current session. ipykeep
                                  attempts to detect file reads automatically
                                  from cell source; this command handles cases
                                  it misses.

ipykeep init                     Scaffold project config and integration files
                                  (see Integration Files section below).
```

---

## MCP Tool Surface

When running as an MCP server (`ipykeep mcp-serve`), the following tools are
exposed. Parameter schemas follow MCP JSON Schema conventions.

| Tool | Key Parameters | Returns |
|---|---|---|
| `run_stale` | `execute: bool = false` | Plan object: `{stale_cells: [{id, reason, produces_vars}], will_execute: bool}` |
| `run_cells` | `cell_ids: list[int]` | `{executed: list[int], outputs: list[str], errors: list[str \| null]}` |
| `inspect` | `var_name: str` | Type-specific summary JSON (see Inspection section) |
| `get_namespace` | — | `[{name, type, size_bytes, last_updated_cell}]` |
| `get_stale_set` | — | `[{cell_id, reason}]` — staleness without execution |
| `watch_file` | `path: str` | `{watching: bool, associated_cells: list[int]}` |
| `kernel_status` | — | `{alive: bool, execution_count: int, ipyflow_loaded: bool}` |

---

## Staleness Computation

Staleness has two independent sources that are unioned together:

**Source 1 — Cell source code change.**
On each `run_stale` call, ipykeep re-reads the `.ipynb` file and computes a
hash of each cell's source. Cells whose hash differs from the stored hash at
last execution are marked directly stale. Their downstream dependents are then
computed via ipyflow's `users()` API recursively and also marked stale.

**Source 2 — External file modification.**
The file watcher tracks mtime of files that notebook cells read from disk.
ipykeep detects these paths through two mechanisms:
- Static analysis: scan cell source for common read patterns
  (`pd.read_csv(...)`, `open(...)`, `read_parquet(...)`, `connect(...)`, etc.)
  and extract string literal path arguments.
- Manual registration: `ipykeep watch <path>` for cases static analysis misses
  (dynamically constructed paths, paths in config dicts, etc.).
When a watched file's mtime changes, all cells that read it are marked stale,
and their downstream dependents propagate as in Source 1.

The union of both stale sets is what `run_stale` reports and executes.

**ipyflow exec mode.**
The daemon starts the kernel with `ipyflow exec_mode = "lazy"`. This means
ipyflow does not reactively auto-execute cells on change — ipykeep owns the
execution decision and delegates it only when the agent explicitly calls
`run_stale --execute` or `run_cells`. This is intentional: agents need to
inspect the plan before committing to execution.

---

## Variable Inspection

### Built-in Summarizers

The following types have built-in summarizers. All run inside a
`ThreadPoolExecutor` with a configurable timeout (default `INSPECT_TIMEOUT_S =
5`). On timeout, the summarizer returns a degraded response with type, size,
and a `"summary_status": "timeout"` field. The kernel is never interrupted.

| Type | Summary fields |
|---|---|
| `pandas.DataFrame` | shape, columns+dtypes, null_counts per column, describe() output (numeric cols), first 5 rows as records |
| `geopandas.GeoDataFrame` | all DataFrame fields + geometry type, CRS, bbox |
| `numpy.ndarray` | shape, dtype, min/max/mean/std, memory bytes |
| `dict` | len, key types (sampled), value types (sampled), first 10 key-value pairs with value repr truncated |
| `list` / `tuple` | len, element types (sampled), first 10 elements repr |
| `str` | len in chars, first 200 chars |
| `sqlite3.Connection` | database path, list of table names + row counts |
| `sqlalchemy.Engine` | dialect, database URL (credentials redacted), table names |
| `psycopg2.connection` | dsn (credentials redacted), open/closed status |
| Any other type | `type.__name__`, `sys.getsizeof`, `repr()` truncated to 500 chars |

### User-Extensible Registry

Users can register custom summarizers in `pyproject.toml` or via Python:

**pyproject.toml:**
```toml
[tool.ipykeep.inspection]
summarizers = [
    "myproject.ipykeep_ext:GeoDataFrameSummarizer",
    "myproject.ipykeep_ext:RasterSummarizer",
]
```

**Python (in notebook or init cell):**
```python
from ipykeep.inspection.registry import registry
from ipykeep.inspection.inspector import summarizer

@summarizer(match=lambda obj: hasattr(obj, 'rio'))  # rasterio DataArray
def rioxarray_summary(obj):
    return {
        "type": "rioxarray.DataArray",
        "shape": obj.shape,
        "crs": str(obj.rio.crs),
        "bounds": obj.rio.bounds(),
        "dtype": str(obj.dtype),
    }

registry.register(rioxarray_summary)
```

The registry lookup order is: user-registered summarizers (in registration
order, first match wins) → built-in type-specific summarizers → fallback
(type + size + truncated repr).

---

## Integration Files

`ipykeep init` generates the following in the project directory:

**.mcp.json (fragment)**
```json
{
  "mcpServers": {
    "ipykeep": {
      "command": "ipykeep",
      "args": ["mcp-serve"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```
This fragment is compatible with Claude Code (`.claude/settings.json`),
Cursor (`.cursor/mcp.json`), and Codex CLI (`codex.json`). Append it to the
appropriate file for your harness.

**SKILL.md / AGENTS.md**
Generated at `.claude/skills/ipykeep/SKILL.md` and `AGENTS.md` in the project
root. Content instructs the agent:

- Always call `run_stale` (plan mode) before deciding whether to re-execute
  cells after editing a notebook.
- After executing stale cells, call `inspect` on key output variables to
  verify the result makes sense before proceeding.
- Use `get_namespace` at the start of a session to understand what is already
  warm in the kernel.
- If a cell reads from an external file and static analysis may have missed it,
  call `watch_file` explicitly.
- Do not call `run_cells` on cells that produce expensive upstream state
  (data loading, DB queries, network fetches) unless `run_stale` specifically
  identifies them as stale.

**ipykeep.toml (or pyproject.toml section)**
```toml
[tool.ipykeep]
notebook = "analysis.ipynb"          # default notebook for this project
inspect_timeout_s = 5                # per-summarizer timeout
inspect_sample_rows = 5              # rows returned by DataFrame summarizer
watch_debounce_ms = 500              # file watcher debounce
log_level = "INFO"

[tool.ipykeep.inspection]
summarizers = []                     # user-registered summarizer entry points
```

---

## Example Notebooks

The `examples/` directory should contain four notebooks covering the primary
use cases that motivate ipykeep. Do not generate their final content now —
they will be developed iteratively. The scenarios they should cover are:

1. **General EDA pipeline** — loading a dataset from a local file (CSV or
   parquet), multi-step cleaning and transformation, exploratory statistics,
   and plotting. Each stage has a realistic sleep to simulate I/O or compute
   cost. The notebook should be domain-neutral (no UBEM-specific terminology).
   Demonstrate: cell source change causing selective downstream re-execution.

2. **Database + network fetch pipeline** — establish a DB connection (use
   sqlite3 as a stand-in), run a slow query, separately fetch data from a
   simulated HTTP endpoint (use sleep). Join the two sources and run
   aggregations. Demonstrate: the DB connection as a non-serializable runtime
   object that survives across agent iterations; changing aggregation logic
   does not re-trigger the query.

3. **External file dependency** — read a data file whose path is tracked by
   the watcher. Demonstrate: modifying the source file on disk (simulated by
   overwriting it in a setup cell with different content) causes the reader
   cell to become stale even though the reader cell's source code did not
   change. Also demonstrate: the file write in one notebook producing a parquet
   that a downstream cell reads — cross-notebook dependency mediated through
   the filesystem.

4. **Iterative parameter search** — a pipeline with expensive data preparation
   followed by a parameterized processing step (clustering, model training, or
   similar) that the agent is expected to iterate on by changing a small number
   of scalar parameters. Demonstrate: only the parameterized cell and its
   downstream summary/visualization cells re-run; data prep is untouched.

Each notebook should have clear markdown cells labeling each stage, comments
indicating which cells are "expensive" (and why), and comments marking cells
that the agent is expected to modify during iteration.

---

## Implementation Phases

### Phase 0 — Environment Validation (do this before writing any code)

1. In a clean Python environment, run:
   ```
   pip install ipyflow jupyter_client ipykernel watchfiles typer
   ```
2. Start a kernel manually via `jupyter_client`, execute `%load_ext ipyflow`
   in it, then run a few cells and call `from ipyflow import deps, users,
   timestamp, cells` to verify the State API returns usable data structures.
   Print the raw return values. Document the actual types and shapes returned.
3. If the API shapes differ from the README, adjust the staleness tracker
   design accordingly before proceeding to Phase 1.

### Phase 1 — Daemon + Kernel Manager

Implement `daemon/server.py`, `daemon/kernel_manager.py`, `daemon/pid.py`.
The daemon should:
- Accept a notebook path as its only required argument.
- Start a kernel via `jupyter_client.KernelManager`.
- Execute `%load_ext ipyflow` and `%flow mode lazy` in the kernel on startup.
- Execute all cells in the notebook in order on startup (warming the state).
- Listen on a Unix socket at `$XDG_RUNTIME_DIR/ipykeep/<project_hash>.sock`
  (fallback: `/tmp/ipykeep/<project_hash>.sock`).
- Handle JSON-RPC requests: `status`, `execute_cells`, `get_namespace_raw`.
- Write a PID file alongside the socket.

CLI commands for this phase: `start`, `stop`, `status`.

Acceptance criterion: `ipykeep start examples/01_eda.ipynb` warms the kernel,
`ipykeep status` shows it alive, `ipykeep stop` shuts it down cleanly.

### Phase 2 — Staleness Tracker

Implement `staleness/tracker.py`.
- On `run_stale` call: read `.ipynb`, hash each cell's source, diff against
  stored hashes.
- Query ipyflow `users()` recursively to expand the stale set to downstream
  dependents.
- Return a structured plan: `[{cell_index, cell_id, reason, produces_vars}]`.

Implement `staleness/watcher.py`.
- On daemon start: scan all cell sources for common file-read patterns
  (regex-based, not AST; prioritise common pandas/numpy/builtin patterns).
- Register discovered paths with `watchfiles.awatch`.
- On file change event: look up which cells read that path, add them to the
  stale set, propagate downstream via ipyflow `users()`.

CLI commands for this phase: `run-stale` (plan mode), `run-stale --execute`,
`run`, `watch`.

Acceptance criterion: edit a mid-notebook cell source in the example notebook,
call `run_stale`, verify the plan correctly identifies that cell and its
downstream dependents as stale and the expensive upstream cells as clean.
Then verify `run_stale --execute` only re-runs the stale cells.

### Phase 3 — Variable Inspector

Implement `inspection/registry.py`, `inspection/summarizers.py`,
`inspection/inspector.py`.
- Implement built-in summarizers for all types listed in the Inspection section.
- Wrap each summarizer call in `concurrent.futures.ThreadPoolExecutor` with
  `future.result(timeout=INSPECT_TIMEOUT_S)`. On `TimeoutError`: return
  degraded summary with `summary_status: "timeout"`.
- Implement the registry lookup order described above.
- Expose `registry.register()` as a public API.

CLI commands for this phase: `inspect <var_name>`, `namespace`.

Acceptance criterion: after warming the kernel with the EDA example notebook,
`ipykeep inspect df_clean` returns a valid JSON summary with shape, dtypes,
null counts, and sample rows. `ipykeep inspect conn` (sqlite3 connection)
returns table names and row counts. Calling inspect on a custom object with
no registered summarizer falls back gracefully.

### Phase 4 — MCP Server

Implement `mcp_server.py` using the MCP Python SDK.
- Wrap all daemon JSON-RPC calls as MCP tools with proper JSON Schema parameter
  definitions.
- Implement `ipykeep mcp-serve` subcommand.
- Implement `ipykeep init` to scaffold `.mcp.json`, `SKILL.md`, `AGENTS.md`,
  and `ipykeep.toml`.

Acceptance criterion: add ipykeep to Claude Code's MCP config, open the
example notebook project, and complete a full agent iteration loop:
`get_namespace` → edit a cell → `run_stale` (plan) → `run_stale --execute`
→ `inspect` key variable → verify correctness.

### Phase 5 — Packaging + Example Notebooks

- `pyproject.toml` with entry point `ipykeep = ipykeep.cli:app`.
- `README.md` with quickstart (5 commands from install to first agent loop).
- Four example notebooks as described in the Example Notebooks section.
- `CONTRIBUTING.md` with instructions for registering custom summarizers.

---

## Key Dependencies

| Package | Role |
|---|---|
| `ipyflow` | Dependency graph, staleness tracking via State API |
| `jupyter_client` | Kernel process management and ZMQ communication |
| `ipykernel` | The kernel itself |
| `watchfiles` | Filesystem watcher for external file dependencies |
| `typer` | CLI |
| `mcp` | MCP Python SDK for the server |
| `pyproject-parser` or `tomllib` | Config file parsing |

All are pure Python or have well-maintained binary wheels. No C extensions
beyond what ipykernel already requires.

---

## What ipykeep Does Not Do

- Does not provide a notebook editor or UI.
- Does not require notebooks to be in any special format (standard `.ipynb`
  only).
- Does not manage multiple notebooks simultaneously in v1 (one daemon per
  notebook; cross-notebook dependencies are handled via the file watcher).
- Does not serialize or restore runtime variables (pin/restore out of scope
  for v1).
- Does not make staleness decisions automatically — the agent always decides
  whether to execute the returned plan.
- Does not kill or restart the kernel under any normal operating condition,
  including inspection timeouts.