"""Tests for plugins/memory/unified_memory/merger.py.

Standalone: python -m pytest test_merger.py   or   python -m unittest test_merger
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from plugins.memory.unified_memory.contract import (
        MergeConfig,
        MergeStrategy,
        OrderKey,
        ProviderResult,
    )
    from plugins.memory.unified_memory.merger import ResultMerger
except ImportError:  # repo-root run: package path differs
    from contract import MergeConfig, MergeStrategy, OrderKey, ProviderResult  # type: ignore
    from merger import ResultMerger  # type: ignore


class TestResultMergerConcat(unittest.TestCase):
    def test_concat_preserves_order(self):
        merger = ResultMerger(default_limit=10)
        partials = [
            ProviderResult(provider_id="obsidian", payload="vault-a"),
            ProviderResult(provider_id="agentmemory", payload="am-a"),
        ]
        config = MergeConfig(strategy=MergeStrategy.CONCAT)
        result = merger.merge(partials, config)
        self.assertEqual(result.items, ["vault-a", "am-a"])

    def test_concat_truncates_to_limit(self):
        merger = ResultMerger(default_limit=2)
        partials = [
            ProviderResult(provider_id="obsidian", payload="vault-a"),
            ProviderResult(provider_id="obsidian", payload="vault-b"),
            ProviderResult(provider_id="agentmemory", payload="am-a"),
        ]
        config = MergeConfig(strategy=MergeStrategy.CONCAT)
        result = merger.merge(partials, config)
        self.assertEqual(result.items, ["vault-a", "vault-b"])
        self.assertEqual(result.suppressed, 1)


class TestResultMergerUnion(unittest.TestCase):
    def test_union_deduplicates_same_payload(self):
        merger = ResultMerger(default_limit=10)
        partials = [
            ProviderResult(provider_id="obsidian", payload="same"),
            ProviderResult(provider_id="agentmemory", payload="same"),
        ]
        config = MergeConfig(strategy=MergeStrategy.UNION)
        result = merger.merge(partials, config)
        self.assertEqual(result.items, ["same"])

    def test_union_keeps_distinct_items(self):
        merger = ResultMerger(default_limit=10)
        partials = [
            ProviderResult(provider_id="obsidian", payload="vault-a"),
            ProviderResult(provider_id="agentmemory", payload="am-a"),
        ]
        config = MergeConfig(strategy=MergeStrategy.UNION)
        result = merger.merge(partials, config)
        self.assertEqual(result.items, ["vault-a", "am-a"])

    def test_union_respects_provider_priority(self):
        merger = ResultMerger(default_limit=10)
        partials = [
            ProviderResult(provider_id="agentmemory", payload="am-a"),
            ProviderResult(provider_id="obsidian", payload="vault-a"),
        ]
        config = MergeConfig(
            strategy=MergeStrategy.UNION,
            order_key=OrderKey.PROVIDER_PRIORITY,
            provider_priority=["obsidian", "agentmemory"],
        )
        result = merger.merge(partials, config)
        self.assertEqual(result.items, ["vault-a", "am-a"])

    def test_union_respects_timestamp_order(self):
        from datetime import datetime, timezone

        merger = ResultMerger(default_limit=10)
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        partials = [
            ProviderResult(provider_id="obsidian", payload="older", timestamp=t1),
            ProviderResult(provider_id="agentmemory", payload="newer", timestamp=t2),
        ]
        config = MergeConfig(
            strategy=MergeStrategy.UNION,
            order_key=OrderKey.TIMESTAMP,
        )
        result = merger.merge(partials, config)
        self.assertEqual(result.items, ["newer", "older"])

    def test_union_limits_results(self):
        merger = ResultMerger(default_limit=1)
        partials = [
            ProviderResult(provider_id="obsidian", payload="vault-a"),
            ProviderResult(provider_id="agentmemory", payload="am-a"),
        ]
        config = MergeConfig(strategy=MergeStrategy.UNION)
        result = merger.merge(partials, config)
        self.assertEqual(len(result.items), 1)


class TestResultMergerEdgeCases(unittest.TestCase):
    def test_empty_partials(self):
        merger = ResultMerger(default_limit=10)
        result = merger.merge([], MergeConfig())
        self.assertEqual(result.items, [])
        self.assertEqual(result.source_count, 0)

    def test_default_strategy_is_union(self):
        merger = ResultMerger(default_limit=10)
        partials = [
            ProviderResult(provider_id="obsidian", payload="dup"),
            ProviderResult(provider_id="agentmemory", payload="dup"),
        ]
        result = merger.merge(partials)
        self.assertEqual(result.items, ["dup"])

    def test_confidence_clamped(self):
        pr = ProviderResult(provider_id="x", payload="y", confidence=2.0)
        self.assertEqual(pr.confidence, 1.0)

    def test_negative_default_limit_rejected(self):
        with self.assertRaises(ValueError):
            ResultMerger(default_limit=-1)


if __name__ == "__main__":
    unittest.main()
