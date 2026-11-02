"""Data-quality issues shared by the geometry stages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    count: int = 1
