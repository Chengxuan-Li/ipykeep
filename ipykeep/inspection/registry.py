"""Summarizer registry (kernel-side, stdlib-only so it imports anywhere).

Lookup order: user-registered summarizers (registration order, first match wins)
-> built-in type-specific summarizers -> ``None`` (caller applies the fallback).
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Optional

Summarizer = Callable[..., dict]
Matcher = Callable[[Any], bool]


class SummarizerRegistry:
    def __init__(self) -> None:
        self._user: list[tuple[Optional[Matcher], Summarizer]] = []
        self._builtin: list[tuple[Matcher, Summarizer]] = []

    def register(self, fn: Summarizer, match: Optional[Matcher] = None) -> Summarizer:
        """Register a user summarizer. ``match(obj) -> bool`` selects it."""
        m = match if match is not None else getattr(fn, "_ipykeep_match", None)
        self._user.append((m, fn))
        return fn

    def register_builtin(self, match: Matcher, fn: Summarizer) -> Summarizer:
        self._builtin.append((match, fn))
        return fn

    def lookup(self, obj: Any) -> Optional[Summarizer]:
        for m, fn in self._user:
            try:
                if m is None or m(obj):
                    return fn
            except Exception:
                continue
        for m, fn in self._builtin:
            try:
                if m(obj):
                    return fn
            except Exception:
                continue
        return None

    def load_entrypoints(self, specs: list[str]) -> list[str]:
        """Load ``"module.path:attr"`` summarizers (from config). Returns errors."""
        errors: list[str] = []
        for spec in specs:
            try:
                mod_name, _, attr = spec.partition(":")
                obj = getattr(importlib.import_module(mod_name), attr)
                if callable(obj):
                    self.register(obj)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{spec}: {exc!r}")
        return errors


registry = SummarizerRegistry()


def summarizer(match: Optional[Matcher] = None) -> Callable[[Summarizer], Summarizer]:
    """Decorator registering a user summarizer: ``@summarizer(match=lambda o: ...)``."""
    def deco(fn: Summarizer) -> Summarizer:
        fn._ipykeep_match = match  # type: ignore[attr-defined]
        registry.register(fn, match)
        return fn
    return deco
