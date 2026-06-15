"""Filesystem watcher: map file paths a notebook reads -> reader cell ids.

Read paths are discovered with regex over cell source (not AST), prioritising
common pandas / numpy / builtin patterns. Paths missed by static analysis (e.g.
dynamically built) are added via ``watch_file`` / the ``watch`` command.

Watching is done with ``watchfiles.awatch`` over the parent directories of the
tracked files. A modification to a tracked path reports its reader cell ids back
to the daemon via the ``on_dirty`` callback; the daemon unions them into the
stale set computed by the tracker.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger("ipykeep.watcher")

_READ_FUNCS = (
    "read_csv", "read_parquet", "read_excel", "read_json", "read_table",
    "read_feather", "read_pickle", "read_hdf", "read_orc", "read_stata",
    "loadtxt", "genfromtxt", "load", "open", "connect", "imread",
)
_READ_RE = re.compile(
    r"\b(?:" + "|".join(_READ_FUNCS) + r")\s*\(\s*[rbfRBF]*(['\"])(?P<path>.*?)\1"
)


def extract_read_paths(source: str) -> list[str]:
    out: list[str] = []
    for m in _READ_RE.finditer(source):
        p = m.group("path")
        if not p or p.startswith(":") or "://" in p:
            continue
        out.append(p)
    return out


def resolve_path(path: str, base: Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(base) / p
    try:
        return p.resolve()
    except OSError:
        return p


def build_path_map(cells, base: Path) -> dict[Path, set[str]]:
    mapping: dict[Path, set[str]] = {}
    for c in cells:
        for raw in extract_read_paths(c.source):
            mapping.setdefault(resolve_path(raw, base), set()).add(c.cell_id)
    return mapping


class FileWatcher:
    def __init__(self, base_dir: Path, on_dirty: Callable[[set[str]], None],
                 debounce_ms: int = 500):
        self.base_dir = Path(base_dir)
        self.on_dirty = on_dirty
        self.debounce_ms = debounce_ms
        self.path_to_cells: dict[Path, set[str]] = {}
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def configure(self, cells, debounce_ms: Optional[int] = None) -> None:
        if debounce_ms is not None:
            self.debounce_ms = debounce_ms
        self.path_to_cells = build_path_map(cells, self.base_dir)

    def watched_dirs(self) -> list[Path]:
        return sorted({p.parent for p in self.path_to_cells if p.parent.is_dir()})

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._spawn()

    def _spawn(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        dirs = self.watched_dirs()
        if dirs:
            self._task = asyncio.create_task(self._watch(dirs))
            log.info("watching %d dir(s) for %d file(s)", len(dirs), len(self.path_to_cells))

    async def _watch(self, dirs: list[Path]) -> None:
        from watchfiles import awatch

        try:
            async for changes in awatch(*[str(d) for d in dirs], debounce=self.debounce_ms):
                hit: set[str] = set()
                for _change, raw in changes:
                    cells = self.path_to_cells.get(resolve_path(raw, self.base_dir))
                    if cells:
                        hit |= cells
                if hit:
                    log.info("file change -> stale cells %s", sorted(hit))
                    self.on_dirty(hit)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("file watcher stopped on error")

    def add_manual(self, path: str, cells: Iterable[str]) -> tuple[Path, list[str]]:
        p = resolve_path(path, self.base_dir)
        known_dirs = set(self.watched_dirs())
        self.path_to_cells.setdefault(p, set()).update(cells)
        if p.parent.is_dir() and p.parent not in known_dirs and self._loop is not None:
            self._spawn()
        return p, sorted(self.path_to_cells[p])

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
