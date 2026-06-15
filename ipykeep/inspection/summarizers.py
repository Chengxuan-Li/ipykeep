"""Built-in summarizers + the dispatch/emit entry points (run kernel-side).

Each summarizer returns a JSON-serialisable dict. Optional third-party libs are
detected by duck-typing / module name so importing this module never requires
geopandas, sqlalchemy or psycopg2 to be installed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from ipykeep.inspection.registry import registry

_REPR_LIMIT = 500
_STR_LIMIT = 200
_SAMPLE_ITEMS = 10


def _safe_sizeof(obj: Any) -> int:
    try:
        return int(sys.getsizeof(obj))
    except Exception:
        return -1


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit} chars)"


def _modclass(obj: Any) -> tuple[str, str]:
    cls = type(obj)
    return getattr(cls, "__module__", ""), cls.__name__


# --------------------------------------------------------------- predicates
def _is_geodataframe(obj: Any) -> bool:
    mod, name = _modclass(obj)
    return mod.startswith("geopandas") or (name == "GeoDataFrame")


def _is_dataframe(obj: Any) -> bool:
    mod, name = _modclass(obj)
    return mod.startswith("pandas") and name == "DataFrame"


def _is_ndarray(obj: Any) -> bool:
    mod, name = _modclass(obj)
    return mod.startswith("numpy") and name == "ndarray"


def _is_sqlite(obj: Any) -> bool:
    return isinstance(obj, sqlite3.Connection)


def _is_sqlalchemy_engine(obj: Any) -> bool:
    mod, name = _modclass(obj)
    return mod.startswith("sqlalchemy") and name in ("Engine", "Connection")


def _is_psycopg2(obj: Any) -> bool:
    mod, _ = _modclass(obj)
    return mod.startswith("psycopg2")


def is_thread_sensitive(obj: Any) -> bool:
    """True for objects bound to their creating thread (DB connections).

    Such objects must be summarized on the kernel execution thread, not a worker
    thread, so the inspector runs their (cheap) summarizers inline.
    """
    try:
        return _is_sqlite(obj) or _is_sqlalchemy_engine(obj) or _is_psycopg2(obj)
    except Exception:
        return False


# --------------------------------------------------------------- summarizers
def summarize_dataframe(obj: Any, sample_rows: int = 5) -> dict:
    null_counts = {str(c): int(n) for c, n in obj.isnull().sum().items()}
    numeric = obj.select_dtypes("number")
    describe = json.loads(numeric.describe().to_json()) if numeric.shape[1] else {}
    return {
        "type": "pandas.DataFrame",
        "shape": list(obj.shape),
        "columns": {str(c): str(dt) for c, dt in obj.dtypes.items()},
        "null_counts": null_counts,
        "describe": describe,
        "sample": json.loads(obj.head(sample_rows).to_json(orient="records")),
    }


def summarize_geodataframe(obj: Any, sample_rows: int = 5) -> dict:
    out = summarize_dataframe(obj, sample_rows=sample_rows)
    out["type"] = "geopandas.GeoDataFrame"
    try:
        out["geometry_type"] = sorted({str(t) for t in obj.geometry.geom_type.dropna().unique()})
    except Exception:
        out["geometry_type"] = None
    try:
        out["crs"] = str(obj.crs)
    except Exception:
        out["crs"] = None
    try:
        out["bbox"] = [float(x) for x in obj.total_bounds]
    except Exception:
        out["bbox"] = None
    return out


def summarize_ndarray(obj: Any, **_: Any) -> dict:
    out = {
        "type": "numpy.ndarray",
        "shape": list(obj.shape),
        "dtype": str(obj.dtype),
        "memory_bytes": int(getattr(obj, "nbytes", _safe_sizeof(obj))),
    }
    try:
        if obj.size and obj.dtype.kind in "iufc":
            out.update(min=float(obj.min()), max=float(obj.max()),
                       mean=float(obj.mean()), std=float(obj.std()))
    except Exception:
        pass
    return out


def summarize_dict(obj: dict, **_: Any) -> dict:
    items = list(obj.items())[:_SAMPLE_ITEMS]
    return {
        "type": "dict",
        "len": len(obj),
        "key_types": sorted({type(k).__name__ for k, _ in items}),
        "value_types": sorted({type(v).__name__ for _, v in items}),
        "sample": {repr(k): _truncate(repr(v), 120) for k, v in items},
    }


def summarize_sequence(obj: Any, **_: Any) -> dict:
    items = list(obj[:_SAMPLE_ITEMS])
    return {
        "type": type(obj).__name__,
        "len": len(obj),
        "element_types": sorted({type(x).__name__ for x in items}),
        "sample": [_truncate(repr(x), 120) for x in items],
    }


def summarize_str(obj: str, **_: Any) -> dict:
    return {"type": "str", "len": len(obj), "preview": _truncate(obj, _STR_LIMIT)}


def summarize_sqlite(obj: sqlite3.Connection, **_: Any) -> dict:
    out: dict[str, Any] = {"type": "sqlite3.Connection"}
    try:
        dbs = obj.execute("PRAGMA database_list").fetchall()
        out["database"] = [row[2] for row in dbs] or [":memory:"]
    except Exception:
        out["database"] = None
    try:
        names = [r[0] for r in obj.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        tables = {}
        for n in names:
            try:
                tables[n] = int(obj.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0])
            except Exception:
                tables[n] = None
        out["tables"] = tables
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def _redact_url(url: str) -> str:
    import re
    return re.sub(r"://([^:/@]+):([^@/]+)@", r"://\1:***@", url)


def summarize_sqlalchemy(obj: Any, **_: Any) -> dict:
    out: dict[str, Any] = {"type": "sqlalchemy.Engine"}
    try:
        out["dialect"] = obj.dialect.name
    except Exception:
        out["dialect"] = None
    try:
        out["url"] = _redact_url(str(obj.url))
    except Exception:
        out["url"] = None
    try:
        from sqlalchemy import inspect as sa_inspect
        out["tables"] = sorted(sa_inspect(obj).get_table_names())
    except Exception:
        out["tables"] = None
    return out


def summarize_psycopg2(obj: Any, **_: Any) -> dict:
    out: dict[str, Any] = {"type": "psycopg2.connection"}
    try:
        out["dsn"] = _redact_url(getattr(obj, "dsn", "") or "")
    except Exception:
        out["dsn"] = None
    try:
        out["closed"] = bool(obj.closed)
    except Exception:
        out["closed"] = None
    return out


def summarize_fallback(obj: Any, **_: Any) -> dict:
    return {
        "type": type(obj).__name__,
        "module": getattr(type(obj), "__module__", None),
        "size_bytes": _safe_sizeof(obj),
        "repr": _truncate(repr(obj), _REPR_LIMIT),
    }


# Register built-ins (order matters: most specific first).
registry.register_builtin(_is_geodataframe, summarize_geodataframe)
registry.register_builtin(_is_dataframe, summarize_dataframe)
registry.register_builtin(_is_ndarray, summarize_ndarray)
registry.register_builtin(_is_sqlite, summarize_sqlite)
registry.register_builtin(_is_sqlalchemy_engine, summarize_sqlalchemy)
registry.register_builtin(_is_psycopg2, summarize_psycopg2)
registry.register_builtin(lambda o: isinstance(o, dict), summarize_dict)
registry.register_builtin(lambda o: isinstance(o, (list, tuple)), summarize_sequence)
registry.register_builtin(lambda o: isinstance(o, str), summarize_str)


def summarize(obj: Any, sample_rows: int = 5) -> dict:
    fn = registry.lookup(obj)
    if fn is None:
        return summarize_fallback(obj)
    try:
        return fn(obj, sample_rows=sample_rows)
    except TypeError:
        # summarizer that doesn't accept sample_rows
        return fn(obj)
