"""Continuity injects the previous run's ANSWER, not its echoed prompt.

``save_job_output`` persists a full audit record (header + the entire
assembled prompt + ``## Response`` + answer). For a skill-backed job the
echoed prompt is the whole skill text — 10-20K characters — so injecting the
document verbatim burned the 8K ``context_from`` budget on the job's own
prompt and truncated the answer away completely. These tests pin the
extraction contract.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    return hermes_home


def _saved_output(job_name, job_id, prompt_body, response_body):
    """Reproduce the exact document shape ``run_job`` hands to save_job_output."""
    return (
        f"# Cron Job: {job_name}\n\n"
        f"**Job ID:** {job_id}\n"
        f"**Run Time:** 2026-09-04 17:02:35\n"
        f"**Schedule:** 0 17 * * *\n\n"
        f"## Prompt\n\n{prompt_body}\n\n## Response\n\n{response_body}\n"
    )


class TestExtractResponse:
    def test_returns_only_the_response_body(self):
        from cron.scheduler import _extract_cron_output_response

        doc = _saved_output("dev-digest", "aaaaaaaaaaaa", "SKILL TEXT " * 500, "Report: item A")
        extracted = _extract_cron_output_response(doc)

        assert "Report: item A" in extracted
        assert "SKILL TEXT" not in extracted
        assert "## Prompt" not in extracted

    def test_keeps_run_time_for_recency(self):
        from cron.scheduler import _extract_cron_output_response

        doc = _saved_output("dev-digest", "aaaaaaaaaaaa", "prompt", "the answer")
        extracted = _extract_cron_output_response(doc)

        assert "**Run Time:** 2026-09-04 17:02:35" in extracted
        assert extracted.rstrip().endswith("the answer")

    def test_last_marker_wins_when_prompt_echoes_a_response_heading(self):
        """A skill or an earlier injected block can contain '## Response'."""
        from cron.scheduler import _extract_cron_output_response

        doc = _saved_output(
            "digest",
            "aaaaaaaaaaaa",
            "Docs example:\n\n## Response\n\nDECOY from the echoed prompt",
            "REAL answer",
        )
        extracted = _extract_cron_output_response(doc)

        assert "REAL answer" in extracted
        assert "DECOY" not in extracted

    def test_falls_back_to_whole_doc_without_a_response_section(self):
        """blocked_config / error notices have no Response — keep them intact."""
        from cron.scheduler import _extract_cron_output_response

        doc = (
            "# Cron Job: threat-monitor\n\n"
            "**Job ID:** eeeeeeeeeeee\n"
            "**Status:** BLOCKED (configuration)\n\n"
            "**Reason:** delivery platform 'discord' has no gateway credentials.\n"
        )
        assert _extract_cron_output_response(doc) == doc.strip()

    def test_falls_back_when_response_body_is_empty(self):
        from cron.scheduler import _extract_cron_output_response

        doc = _saved_output("digest", "aaaaaaaaaaaa", "prompt text", "   ")
        extracted = _extract_cron_output_response(doc)

        assert "prompt text" in extracted


class TestBuildJobPromptUsesExtraction:
    def test_self_context_carries_the_answer_past_a_huge_prompt(self, cron_env):
        """Regression: a 20K skill-backed record used to truncate the answer away."""
        from cron.jobs import create_job, OUTPUT_DIR
        from cron.scheduler import _build_job_prompt

        job = create_job(
            prompt="Report only NEW items.",
            schedule="every 6h",
            context_from=["self"],
        )
        output_dir = OUTPUT_DIR / job["id"]
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "2026-09-04_17-00-00.md").write_text(
            _saved_output(
                job.get("name") or "job",
                job["id"],
                "SKILL DOCUMENTATION LINE\n" * 900,  # ~23K chars of echoed prompt
                "Previously reported: alpha, beta, gamma.",
            ),
            encoding="utf-8",
        )

        result = _build_job_prompt(job)
        prompt = result[0] if isinstance(result, tuple) else result

        assert "## Your previous run's output" in prompt
        assert "Previously reported: alpha, beta, gamma." in prompt
        assert "SKILL DOCUMENTATION LINE" not in prompt
        assert "[... output truncated ...]" not in prompt
