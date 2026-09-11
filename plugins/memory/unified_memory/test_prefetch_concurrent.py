"""Concurrent dual-source prefetch tests (kanban t_e058bffd).

Standalone: python test_prefetch_concurrent.py
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.memory.unified_memory import UnifiedMemoryProvider


class SlowVault:
    """Test double: vault prefetch that hangs longer than the timeout."""
    def prefetch(self, query, *, session_id=""):
        time.sleep(30)
        return "SHOULD NOT APPEAR"


class FastVault:
    def prefetch(self, query, *, session_id=""):
        return "[Vault] hit for " + query


class BoomAM:
    """Circuit-breaker double raising immediately."""
    mode = "unified"
    def smart_search(self, query, limit=5, scope=None):
        raise RuntimeError("am exploded")


class OkAM:
    mode = "unified"
    def smart_search(self, query, limit=5, scope=None):
        return [{"title": "am hit", "timestamp": "2026-08-26T01:00:00Z"}]


def make_provider(am, vault=None, timeout=0.5):
    p = UnifiedMemoryProvider({"prefetch_timeout_s": timeout})
    p._am = am
    if vault is not None:
        p._vault_provider = vault
    return p


class TestConcurrentPrefetch(unittest.TestCase):
    def test_hanging_vault_times_out_healthy_am_wins(self):
        """Hanging source times out without delaying the healthy one."""
        p = make_provider(OkAM(), SlowVault(), timeout=0.5)
        t0 = time.monotonic()
        ctx = p.prefetch("query")
        dt = time.monotonic() - t0
        self.assertIn("[Working memory]", ctx)
        self.assertIn("am hit", ctx)
        self.assertNotIn("SHOULD NOT APPEAR", ctx)
        self.assertLess(dt, 3.0, f"healthy source blocked by hanging one ({dt:.1f}s)")
        self.assertEqual(p._last_prefetch_sources["vault"]["status"], "timeout")
        self.assertEqual(p._last_prefetch_sources["agentmemory"]["status"], "ok")

    def test_exploding_source_captured_not_raised(self):
        p = make_provider(BoomAM(), FastVault(), timeout=2.0)
        ctx = p.prefetch("query")  # must not raise
        self.assertIn("[Vault] hit", ctx)
        self.assertNotIn("[Working memory]", ctx)
        self.assertEqual(p._last_prefetch_sources["agentmemory"]["status"], "error")
        self.assertEqual(p._last_prefetch_sources["vault"]["status"], "ok")

    def test_both_sources_ok(self):
        p = make_provider(OkAM(), FastVault(), timeout=2.0)
        ctx = p.prefetch("query")
        self.assertIn("[Vault] hit", ctx)
        self.assertIn("am hit", ctx)
        self.assertEqual(
            {k: v["status"] for k, v in p._last_prefetch_sources.items()},
            {"agentmemory": "ok", "vault": "ok"})

    def test_timeout_is_configurable(self):
        p = UnifiedMemoryProvider({})
        self.assertEqual(p._prefetch_timeout_s, 5.0)
        p2 = UnifiedMemoryProvider({"prefetch_timeout_s": 1.25})
        self.assertEqual(p2._prefetch_timeout_s, 1.25)

    def test_sources_fetched_concurrently(self):
        """Total wall time ~= slowest leg, not the sum."""
        class Sleepy:
            def __init__(self, s): self.s = s
            def smart_search(self, q, limit=5, scope=None):
                time.sleep(self.s); return []
            def prefetch(self, q, *, session_id=""):
                time.sleep(self.s); return ""
        am, vault = Sleepy(0.8), FastVault()
        vault.__class__ = type("V", (FastVault,), {"prefetch": lambda self, q, session_id="": (time.sleep(0.8), "[Vault] x")[1]})
        p = make_provider(am, vault, timeout=5.0)
        t0 = time.monotonic()
        p.prefetch("q")
        dt = time.monotonic() - t0
        self.assertLess(dt, 1.6, f"sequential behaviour detected ({dt:.1f}s)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
