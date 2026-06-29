"""`ipykeep init` — scaffold a project's integration files.

Generates (non-destructively unless force=True):
  .mcp.json                         MCP server registration (merged if present)
  .claude/skills/ipykeep/SKILL.md   Claude skill: when/how to use ipykeep
  AGENTS.md                         harness-agnostic agent instructions (appended)
  ipykeep.toml                      ipykeep's own config ([tool.ipykeep])
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_AGENTS_MARKER = "<!-- ipykeep:begin -->"

MCP_ENTRY = {"command": "ipykeep", "args": ["mcp-serve"], "cwd": "${workspaceFolder}"}

VSCODE_EXTENSION_ID = "ipykeep.ipykeep-vscode"

SKILL_MD = """\
---
name: ipykeep
description: Use when creating, editing, executing, or exploring (EDA) Jupyter notebooks (.ipynb), especially notebooks with expensive cells (data loading, DB queries, model training). ipykeep keeps a live kernel warm between edits so only stale cells re-run — never restart the kernel or re-run the whole notebook. Trigger whenever you edit notebook cells and need to re-run them, inspect notebook/runtime variables, or check what is warm in the kernel.
---

# ipykeep — persistent kernel for notebook work

ipykeep owns a long-lived Jupyter kernel for a notebook and reports exactly which
cells are stale after an edit, so expensive upstream steps are not re-run. Edit
cells with normal tools; use ipykeep to decide what to execute and to inspect
results.

**Prefer the ipykeep MCP tools if they are available** (`start_session`,
`run_stale`, `inspect`, `get_namespace`, …). If not, use the equivalent CLI
commands shown below.

## Start of session
- MCP: call `start_session` (warms the kernel). CLI: `ipykeep start <nb>`.
- Then `get_namespace` to see what is already warm.

## The loop (every time you change a cell)
1. Edit the cell with your normal editing tools.
2. `run_stale` (plan only) — see which cells are stale and why.
3. `run_stale(execute=true)` — re-run ONLY the stale cells; expensive up-to-date
   cells are skipped.
4. `inspect <var>` — verify a key output (shape, dtypes, nulls, sample).

## Rules
- NEVER restart the kernel or re-run the whole notebook to try something.
- NEVER re-run expensive cells (data loads, DB queries, model fits) unless
  `run_stale` lists them as stale.
- Prefer `inspect` / `get_namespace` over adding print-heavy cells.
- If a cell reads a data file the scan may have missed, call `watch_file <path>`.

## CLI equivalents
`ipykeep start <nb>` · `ipykeep run-stale [--execute] <nb>` ·
`ipykeep inspect <var> -n <nb>` · `ipykeep namespace <nb>` ·
`ipykeep run <ids> -n <nb>` · `ipykeep watch <path> -n <nb>` · `ipykeep stop <nb>`

## Letting a human watch you work live (VS Code)
For one-click open of the notebook on the warm kernel with your edits AND cell
executions streaming live into the user's editor, install the **ipykeep VS Code
extension** and have the human run `ipykeep open <nb>` (boots a hosted server and
opens VS Code attached to the warm kernel). While the extension is attached, your
`run_stale --execute` is delegated to VS Code so outputs render in the cells the
human is watching — keep using `run_stale` exactly as normal.

Without the extension: `serve=true` (MCP) / `ipykeep start --serve <nb>` still
hosts the kernel so VS Code / JupyterLab can attach for inspection; you keep
owning execution (outputs won't stream into their cells in that mode).
"""

AGENTS_SECTION = """\
{marker}
## Working with notebooks (ipykeep)

This project uses **ipykeep** to keep a notebook's Jupyter kernel warm between
edits. Do not restart the kernel or re-run the whole notebook.

- Start a session: `ipykeep start {notebook}` (or the `start_session` MCP tool).
- After editing a cell, BEFORE re-running: `ipykeep run-stale {notebook}` to see
  what is stale, then `ipykeep run-stale --execute {notebook}` to run only those.
- Never re-run expensive cells (data loads, DB queries, model fits) unless
  run-stale lists them as stale.
- Inspect outputs with `ipykeep inspect <var> -n {notebook}` instead of adding
  print cells. Register missed data files with `ipykeep watch <path>`.
<!-- ipykeep:end -->
"""

IPYKEEP_TOML = """\
[tool.ipykeep]
notebook = "{notebook}"          # default notebook for this project
inspect_timeout_s = 5            # per-summary timeout
inspect_sample_rows = 5          # rows returned by the DataFrame summarizer
watch_debounce_ms = 500          # file watcher debounce
log_level = "INFO"
serve = false                    # host the kernel in a Jupyter server (IDE attach)
server_command = "lab"           # "lab" | "notebook" | "server"
server_port = 0                  # 0 = pick a free port
delegated_timeout_s = 300        # wait for the VS Code watcher before direct fallback

[tool.ipykeep.inspection]
summarizers = []                 # "module.path:callable" custom summarizers
"""


def _resolve_notebook(target_dir: Path, notebook: Optional[str]) -> str:
    if notebook:
        nb = Path(notebook)
        try:
            return nb.resolve().relative_to(target_dir.resolve()).as_posix()
        except ValueError:
            return nb.as_posix()
    candidates = [p for p in target_dir.glob("*.ipynb")
                  if ".ipynb_checkpoints" not in p.parts]
    if len(candidates) == 1:
        return candidates[0].name
    return "analysis.ipynb"


def _write(path: Path, content: str, force: bool, results: list[tuple[str, str]]) -> None:
    rel = str(path)
    existed = path.exists()
    if existed and not force:
        results.append((rel, "skipped (exists)"))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    results.append((rel, "overwritten" if existed else "created"))


def _merge_mcp_json(path: Path, force: bool, results: list[tuple[str, str]]) -> None:
    existed = path.exists()
    data: dict = {}
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    servers = data.setdefault("mcpServers", {})
    if "ipykeep" in servers and not force:
        results.append((str(path), "skipped (ipykeep already registered)"))
        return
    servers["ipykeep"] = MCP_ENTRY
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    results.append((str(path), "merged" if existed else "created"))


def _merge_agents(path: Path, notebook: str, force: bool, results: list[tuple[str, str]]) -> None:
    section = AGENTS_SECTION.format(marker=_AGENTS_MARKER, notebook=notebook)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if _AGENTS_MARKER in existing and not force:
            results.append((str(path), "skipped (ipykeep section present)"))
            return
        if _AGENTS_MARKER in existing and force:
            # replace the marked block
            head = existing.split(_AGENTS_MARKER)[0].rstrip()
            tail = existing.split("<!-- ipykeep:end -->", 1)
            rest = tail[1] if len(tail) > 1 else ""
            path.write_text(head + "\n\n" + section + rest, encoding="utf-8")
            results.append((str(path), "updated"))
            return
        path.write_text(existing.rstrip() + "\n\n" + section, encoding="utf-8")
        results.append((str(path), "appended"))
        return
    path.write_text("# Agent guide\n\n" + section, encoding="utf-8")
    results.append((str(path), "created"))


def _merge_vscode_extensions(path: Path, force: bool, results: list[tuple[str, str]]) -> None:
    """Recommend the ipykeep VS Code companion in .vscode/extensions.json."""
    existed = path.exists()
    data: dict = {}
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    recs = data.setdefault("recommendations", [])
    if not isinstance(recs, list):
        recs = data["recommendations"] = []
    if VSCODE_EXTENSION_ID in recs and not force:
        results.append((str(path), "skipped (extension already recommended)"))
        return
    if VSCODE_EXTENSION_ID not in recs:
        recs.append(VSCODE_EXTENSION_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    results.append((str(path), "merged" if existed else "created"))


def run_init(target_dir: Path, notebook: Optional[str] = None,
             force: bool = False) -> list[tuple[str, str]]:
    target_dir = Path(target_dir)
    nb = _resolve_notebook(target_dir, notebook)
    results: list[tuple[str, str]] = []

    _merge_mcp_json(target_dir / ".mcp.json", force, results)
    _write(target_dir / ".claude" / "skills" / "ipykeep" / "SKILL.md", SKILL_MD, force, results)
    _merge_agents(target_dir / "AGENTS.md", nb, force, results)
    _write(target_dir / "ipykeep.toml", IPYKEEP_TOML.format(notebook=nb), force, results)
    _merge_vscode_extensions(target_dir / ".vscode" / "extensions.json", force, results)
    return results
