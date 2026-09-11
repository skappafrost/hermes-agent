"""Tests for prefetch merge + graceful AgentMemory degradation.

Standalone: python -m pytest test_prefetch_merge.py
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.memory.unified_memory import UnifiedMemoryProvider


class FastVault:
    def prefetch(self, query, *, session_id=""):
        return "[Vault] hit for " + query


class BoomAM:
    mode = "unified"

    def smart_search(self, query, limit=5, scope=None):
        raise RuntimeError("am exploded")


class TimeoutAM:
    mode = "unified"

    def smart_search(self, query, limit=5, scope=None):
        time.sleep(30)
        return []


class OkAM:
    mode = "unified"

    def smart_search(self, query, limit=5, scope=None):
        return [
            {"title": "am hit 1", "timestamp": "2026-08-26T01:00:00Z"},
            {"title": "am hit 2", "timestamp": "2026-08-26T02:00:00Z"},
        ]


def make_provider(am, vault=None, timeout=0.5):
    p = UnifiedMemoryProvider({"prefetch_timeout_s": timeout})
    p._am = am
    if vault is not None:
        p._vault_provider = vault
    return p


class TestPrefetchMerge(unittest.TestCase):
    def test_both_ok_merges_vault_and_am(self):
        p = make_provider(OkAM(), FastVault(), timeout=2.0)
        ctx = p.prefetch("query")
        self.assertIn("[Vault] hit", ctx)
        self.assertIn("[Working memory]", ctx)
        self.assertIn("am hit 1", ctx)
        self.assertIn("am hit 2", ctx)
        self.assertEqual(p._last_prefetch_sources["agentmemory"]["status"], "ok")
        self.assertEqual(p._last_prefetch_sources["vault"]["status"], "ok")

    def test_am_error_degrades_to_vault_only(self):
        p = make_provider(BoomAM(), FastVault(), timeout=2.0)
        ctx = p.prefetch("query")
        self.assertIn("[Vault] hit", ctx)
        self.assertNotIn("[Working memory]", ctx)
        self.assertEqual(p._last_prefetch_sources["agentmemory"]["status"], "error")
        self.assertEqual(p._last_prefetch_sources["vault"]["status"], "ok")

    def test_am_timeout_degrades_to_vault_only(self):
        p = make_provider(TimeoutAM(), FastVault(), timeout=0.5)
        ctx = p.prefetch("query")
        self.assertIn("[Vault] hit", ctx)
        self.assertNotIn("[Working memory]", ctx)
        self.assertEqual(p._last_prefetch_sources["agentmemory"]["status"], "timeout")
        self.assertEqual(p._last_prefetch_sources["vault"]["status"], "ok")

    def test_vault_error_uses_am_only(self):
        class BoomVault:
            def prefetch(self, query, *, session_id=""):
                raise RuntimeError("vault exploded")

        p = make_provider(OkAM(), BoomVault(), timeout=2.0)
        ctx = p.prefetch("query")
        self.assertIn("[Working memory]", ctx)
        self.assertIn("am hit 1", ctx)
        self.assertNotIn("[Vault]", ctx)
        self.assertEqual(p._last_prefetch_sources["vault"]["status"], "error")
        self.assertEqual(p._last_prefetch_sources["agentmemory"]["status"], "ok")

    def test_prefetch_never_raises(self):
        p = make_provider(BoomAM(), BoomAM(), timeout=0.5)
        ctx = p.prefetch("query")
        self.assertEqual(ctx, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
