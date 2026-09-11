"""Tests for plugins/memory/unified_memory/scope.py (ScopeManager).

Standalone: python -m pytest test_scope.py   or   python -m unittest test_scope
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from plugins.memory.unified_memory.scope import (
        DEFAULT_SCOPE,
        TEAM_ID,
        ScopeManager,
    )
except ImportError:  # repo-root run: package path differs
    from scope import DEFAULT_SCOPE, TEAM_ID, ScopeManager  # type: ignore


class TestDefaultIsolation(unittest.TestCase):
    def test_default_isolation_between_two_agents(self):
        sm = ScopeManager({})  # no config at all
        self.assertFalse(
            sm.can_read("agent-b", "agent-a", "notes"),
            "untagged category must stay private between agents",
        )
        self.assertEqual(sm.tag_scope("notes"), DEFAULT_SCOPE)
        self.assertIsNone(sm.scope_tag("agent-a", "notes")["team"])


class TestSharedCategory(unittest.TestCase):
    CFG = {"shared_categories": ["team-notes"]}

    def test_tagged_category_visible_cross_agent(self):
        sm = ScopeManager(self.CFG)
        self.assertTrue(sm.can_read("agent-b", "agent-a", "team-notes"))
        tag = sm.scope_tag("agent-a", "team-notes")
        self.assertEqual(tag["scope"], "shared")
        self.assertEqual(tag["team"], TEAM_ID)

    def test_untagged_stays_private_even_when_another_is_shared(self):
        sm = ScopeManager(self.CFG)
        self.assertFalse(sm.can_read("agent-b", "agent-a", "private-log"))
        self.assertEqual(sm.tag_scope("private-log"), DEFAULT_SCOPE)


class TestConfigToggle(unittest.TestCase):
    def test_toggle_changes_behavior_without_code_edits(self):
        sm = ScopeManager({"shared_categories": []})
        self.assertFalse(sm.can_read("b", "a", "reports"))
        sm.reconfigure({"shared_categories": ["reports"]})
        self.assertTrue(sm.can_read("b", "a", "reports"))
        # and back off again via from_config
        off = ScopeManager.from_config({"shared_categories": ["other"]})
        self.assertFalse(off.can_read("b", "a", "reports"))

    def test_malformed_config_is_tolerated(self):
        sm = ScopeManager(None)
        self.assertFalse(sm.can_read("b", "a", "x"))
        sm.reconfigure({"shared_categories": [123, None, "ok"]})
        self.assertTrue(sm.is_shared("ok"))  # non-strings dropped, no crash
        self.assertTrue(sm.can_read("b", "a", "ok"))

    def test_same_agent_always_reads_own_data(self):
        for cfg in (None, {}, {"shared_categories": ["c"]}):
            with self.subTest(cfg=cfg):
                self.assertTrue(ScopeManager(cfg).can_read("a", "a", "c"))


if __name__ == "__main__":
    unittest.main()
