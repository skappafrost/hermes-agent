"""Contract types for the unified memory ResultMerger.

These dataclasses are pure data containers. No plugin or I/O imports are
required so the merger can remain a stateless, deterministic pure-logic
component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, List, Optional, Sequence


class MergeStrategy(Enum):
    """How to combine items from multiple sources."""

    CONCAT = auto()
    UNION = auto()
    WEIGHTED_COMBINE = auto()


class OrderKey(Enum):
    """Primary ordering dimension for merge output."""

    PROVIDER_PRIORITY = auto()
    TIMESTAMP = auto()
    CONFIDENCE = auto()


class ConflictResolution(Enum):
    """How to resolve two items that share the same identity."""

    FIRST_WINS = auto()
    LAST_WINS = auto()
    HIGHEST_CONFIDENCE = auto()
    HIGHEST_PRIORITY = auto()


@dataclass(frozen=True)
class ProviderResult:
    """A single partial result produced by one provider."""

    provider_id: str
    payload: Any
    timestamp: Optional[datetime] = None
    confidence: float = 1.0
    priority: int = 0
    order_hint: Optional[int] = None
    tags: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "confidence", float(max(0.0, min(1.0, self.confidence)))
        )


@dataclass(frozen=True)
class MergeConfig:
    """Immutable configuration for a merge operation."""

    strategy: MergeStrategy = MergeStrategy.UNION
    order_key: OrderKey = OrderKey.PROVIDER_PRIORITY
    conflict_resolution: ConflictResolution = ConflictResolution.HIGHEST_PRIORITY
    provider_priority: List[str] = field(default_factory=lambda: ["obsidian", "agentmemory"])
    max_results: Optional[int] = None
    dedup_key: Optional[str] = None


@dataclass(frozen=True)
class MergedResult:
    """Result of a merge operation."""

    items: List[Any]
    source_count: int = 0
    suppressed: int = 0


class MergeError(Exception):
    """Raised when a merge operation cannot be completed."""
