"""Inspection subpackage.

Importable kernel-side as ``import ipykeep.inspection as _k`` to call
``_k.emit_summary(...)``. Importing this package registers the built-in
summarizers.
"""
from ipykeep.inspection.registry import SummarizerRegistry, registry, summarizer
from ipykeep.inspection.summarizers import summarize  # registers built-ins
from ipykeep.inspection.inspector import Inspector, emit_summary

__all__ = ["registry", "summarizer", "SummarizerRegistry", "summarize",
           "emit_summary", "Inspector"]
