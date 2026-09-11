"""ResultMerger implementation for unified memory.

Combines partial results from multiple providers (Obsidian vault, AgentMemory,
...) into a single deterministic, ordered, deduplicated result list. The
class is intentionally stateless: no I/O, no plugin imports beyond the
local contract module.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional, Sequence

from plugins.memory.unified_memory.contract import (
    ConflictResolution,
    MergeConfig,
    MergeStrategy,
    MergedResult,
    OrderKey,
    ProviderResult,
)


class ResultMerger:
    """Pure-logic merger for provider results.

    Parameters
    ----------
    default_limit:
        Fallback truncation limit used when ``config.max_results`` is None.
    """

    def __init__(self, default_limit: int = 20) -> None:
        if default_limit < 0:
            raise ValueError("default_limit must be non-negative")
        self.default_limit = default_limit

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def merge(
        self,
        partials: Sequence[ProviderResult],
        config: Optional[MergeConfig] = None,
    ) -> MergedResult:
        """Merge ``partials`` into a single deterministic output."""
        config = config or MergeConfig()

        if config.strategy == MergeStrategy.CONCAT:
            return self._merge_concat(partials, config)

        if config.strategy == MergeStrategy.UNION:
            return self._merge_union(partials, config)

        if config.strategy == MergeStrategy.WEIGHTED_COMBINE:
            return self._merge_weighted_combine(partials, config)

        raise ValueError(f"Unsupported merge strategy: {config.strategy}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _limit(self, config: MergeConfig) -> int:
        return config.max_results if config.max_results is not None else self.default_limit

    @staticmethod
    def _dedup_key(pr: ProviderResult, config: MergeConfig) -> Any:
        if config.dedup_key:
            payload = pr.payload
            if isinstance(payload, dict):
                return payload.get(config.dedup_key)
            return getattr(payload, config.dedup_key, None)
        # Default: stable hashable key derived from the payload.
        payload = pr.payload
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, dict):
            return json.dumps(payload, sort_keys=True, default=str)
        return str(payload)

    def _ordered(self, partials: Sequence[ProviderResult], config: MergeConfig) -> List[ProviderResult]:
        return sorted(partials, key=lambda pr: _sort_key(pr, config, self.default_limit))

    def _resolve_conflict(self, group: Sequence[ProviderResult], config: MergeConfig) -> ProviderResult:
        if config.conflict_resolution == ConflictResolution.FIRST_WINS:
            return group[0]
        if config.conflict_resolution == ConflictResolution.LAST_WINS:
            return group[-1]
        if config.conflict_resolution == ConflictResolution.HIGHEST_CONFIDENCE:
            return max(group, key=lambda pr: (pr.confidence, pr.priority))
        if config.conflict_resolution == ConflictResolution.HIGHEST_PRIORITY:
            return max(group, key=lambda pr: (pr.priority, pr.confidence))
        return group[0]

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------
    def _merge_concat(self, partials: Sequence[ProviderResult], config: MergeConfig) -> MergedResult:
        ordered = self._ordered(partials, config)
        limit = self._limit(config)
        items = [pr.payload for pr in ordered[:limit]]
        return MergedResult(
            items=items,
            source_count=len(partials),
            suppressed=max(0, len(partials) - len(items)),
        )

    def _merge_union(self, partials: Sequence[ProviderResult], config: MergeConfig) -> MergedResult:
        ordered = self._ordered(partials, config)
        limit = self._limit(config)
        seen: dict = {}
        for pr in ordered:
            key = self._dedup_key(pr, config)
            if key is None:
                continue
            if key in seen:
                continue
            seen[key] = pr
        representatives = list(seen.values())
        representatives = self._ordered(representatives, config)
        items = [pr.payload for pr in representatives[:limit]]
        return MergedResult(
            items=items,
            source_count=len(partials),
            suppressed=max(0, len(partials) - len(items)),
        )

    def _merge_weighted_combine(self, partials: Sequence[ProviderResult], config: MergeConfig) -> MergedResult:
        # Weighted combine is implemented as union with confidence-biased ordering.
        return self._merge_union(partials, config)


# ------------------------------------------------------------------
# Sort key helpers
# ------------------------------------------------------------------
def _sort_key(pr: ProviderResult, config: MergeConfig, default_limit: int) -> tuple:
    ts = pr.timestamp or datetime.min
    priority = pr.priority
    confidence = pr.confidence
    provider_rank = _provider_rank(pr.provider_id, config.provider_priority)

    if config.order_key == OrderKey.TIMESTAMP:
        return (
            0 if ts is not datetime.min else 1,
            -ts.timestamp() if ts is not datetime.min else 0,
            -confidence,
            provider_rank,
            str(pr.payload) if pr.payload is not None else "",
        )
    if config.order_key == OrderKey.CONFIDENCE:
        return (
            -confidence,
            0 if ts is not datetime.min else 1,
            -ts.timestamp() if ts is not datetime.min else 0,
            provider_rank,
            str(pr.payload) if pr.payload is not None else "",
        )
    # PROVIDER_PRIORITY default
    return (
        provider_rank,
        -priority,
        -confidence,
        0 if ts is not datetime.min else 1,
        -ts.timestamp() if ts is not datetime.min else 0,
        str(pr.payload) if pr.payload is not None else "",
    )


def _provider_rank(provider_id: str, priority: List[str]) -> int:
    try:
        return priority.index(provider_id)
    except ValueError:
        return len(priority)
