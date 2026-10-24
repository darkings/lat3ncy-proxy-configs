#!/usr/bin/env python3
"""Shared conversion/build diagnostics types.

A diagnostic is either blocking (the target/bundle must not be published) or a
warning. Keeping the type in one tiny module avoids circular imports between
the converter, the validator and the build orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str = ""
    blocking: bool = True
    severity: str = "error"
    source: str = "converter"

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "blocking": self.blocking,
            "severity": self.severity,
            "source": self.source,
        }

    def __str__(self) -> str:
        location = f" [{self.path}]" if self.path else ""
        return f"{self.code}{location}: {self.message}"


class DiagnosticCollection(list):
    """List of :class:`Diagnostic` with convenience accessors."""

    def __init__(self, items: Iterable[Diagnostic] = ()) -> None:
        super().__init__(items)

    @property
    def blocking_errors(self) -> list[Diagnostic]:
        return [d for d in self if d.blocking]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self if not d.blocking]

    @property
    def has_blocking(self) -> bool:
        return any(d.blocking for d in self)

    def by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for diagnostic in self:
            counts[diagnostic.code] = counts.get(diagnostic.code, 0) + 1
        return dict(sorted(counts.items()))

    def extend_blocking(self, other: Iterable[Diagnostic]) -> None:
        self.extend(other)


def blocking_errors(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    return [d for d in diagnostics if d.blocking]


def summarize(diagnostics: Iterable[Diagnostic]) -> dict:
    items = list(diagnostics)
    blocking = [d for d in items if d.blocking]
    return {
        "total": len(items),
        "blocking": len(blocking),
        "warnings": len(items) - len(blocking),
        "codes": DiagnosticCollection(items).by_code(),
    }
