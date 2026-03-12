"""
Comprehensive test harness for the Task-Claw pipeline.

Run with:
    python -m unittest test_pipeline.py -v
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# ── Pre-set env vars BEFORE importing to prevent side effects ─────────────
os.environ.setdefault("GITHUB_TOKEN", "fake-token-for-tests")
os.environ.setdefault("TASKS_FILE", str(Path(__file__).parent / "data" / "tasks.json"))
os.environ.setdefault("IDEAS_FILE", str(Path(__file__).parent / "data" / "ideas.json"))

# ── Import task-claw.py using importlib (hyphen in filename) ──────────────
spec = importlib.util.spec_from_file_location(
    "task_claw", str(Path(__file__).parent / "task-claw.py")
)
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)


# ── Helper ────────────────────────────────────────────────────────────────

def make_pm_response(verdict="APPROVE", issues=None, synthesis="Good.", handoff="Proceed."):
    """Build a well-formed PM overseer response string."""
    issues_text = "\n".join(f"- {i}" for i in (issues or [])) or "None"
    return (
        f"## Verdict\n{verdict}\n\n"
        f"## Issues\n{issues_text}\n\n"
        f"## Synthesis\n{synthesis}\n\n"
        f"## Handoff\n{handoff}"
    )


GOOD_OUTPUT = (
    "I have analyzed the codebase and implemented the requested changes. "
    "The following files were modified to add the new feature as specified. "
    "All edge cases have been handled and the code follows existing patterns. "
    "Here is a summary of what was done including detailed explanations. "
    "The implementation covers input validation, error handling, and tests. "
    "Everything looks correct and production-ready."
)


# ═══════════════════════════════════════════════════════════════════════════
#  PURE FUNCTION TESTS — no mocking needed
# ═══════════════════════════════════════════════════════════════════════════


class TestIsGarbageOutput(unittest.TestCase):
    """Tests for _is_garbage_output()."""

    def test_short_output_is_garbage(self):
        result = tc._is_garbage_output([("agent1", "ok")])
        self.assertTrue(result)

    def test_permission_request_is_garbage(self):
        result = tc._is_garbage_output([
            ("agent1", "I'm sorry, could you grant permission to write files? " * 5)
        ])
        self.assertTrue(result)

    def test_normal_code_output_not_garbage(self):
        result = tc._is_garbage_output([("agent1", GOOD_OUTPUT)])
        self.assertFalse(result)

    def test_case_insensitivity(self):
        # The function lowercases before matching
        result = tc._is_garbage_output([
            ("agent1", "COULD YOU SHARE the details of what you want? " * 5)
        ])
        self.assertTrue(result)

    def test_mixed_agents_one_garbage(self):
        # Join behavior: all output is combined, so one garbage pattern poisons it
        result = tc._is_garbage_output([
            ("agent1", GOOD_OUTPUT),
            ("agent2", "could you share the codebase details please? " * 5),
        ])
        self.assertTrue(result)


class TestTestFoundFailures(unittest.TestCase):
    """Tests for _test_found_failures()."""

    def test_all_tests_passed(self):
        self.assertFalse(tc._test_found_failures("All tests passed successfully."))

    def test_zero_failures(self):
        self.assertFalse(tc._test_found_failures("Test results: 0 failures, 5 passed."))

    def test_traceback_detected(self):
        self.assertTrue(tc._test_found_failures(
            "Running tests...\nTraceback (most recent call last):\n  File 'test.py'"
        ))

    def test_syntax_error(self):
        self.assertTrue(tc._test_found_failures(
            "SyntaxError: unexpected token on line 42"
        ))

    def test_pass_overrides_fail(self):
        # "no error" is a pass pattern and should override "error" keyword
        self.assertFalse(tc._test_found_failures(
            "Checked all files. No errors found in the implementation."
        ))

    def test_clean_output(self):
        # No failure or pass keywords at all
        self.assertFalse(tc._test_found_failures(
            "The code looks clean and follows best practices."
        ))


class TestParseOverseerResponse(unittest.TestCase):
    """Tests for _parse_overseer_response()."""

    def test_well_formed_approve(self):
        text = make_pm_response(verdict="APPROVE")
        result = tc._parse_overseer_response(text)
        self.assertEqual(result["verdict"], "approve")
        self.assertEqual(result["issues"], [])
        self.assertTrue(len(result["synthesis"]) > 0)

    def test_well_formed_revise(self):
        text = make_pm_response(
            verdict="REVISE",
            issues=["Bug found in parser", "Missing test coverage"]
        )
        result = tc._parse_overseer_response(text)
        self.assertEqual(result["verdict"], "revise")
        self.assertEqual(len(result["issues"]), 2)
        self.assertIn("Bug found in parser", result["issues"])

    def test_missing_verdict_defaults_approve(self):
        text = "## Synthesis\nLooks good overall.\n\n## Issues\nNone"
        result = tc._parse_overseer_response(text)
        self.assertEqual(result["verdict"], "approve")

    def test_issues_none_string(self):
        text = make_pm_response(verdict="APPROVE")
        result = tc._parse_overseer_response(text)
        self.assertEqual(result["issues"], [])

    def test_empty_string(self):
        # Empty string has no ## Verdict and no approval signals, so defaults to "revise"
        result = tc._parse_overseer_response("")
        self.assertEqual(result["verdict"], "revise")
        self.assertIsInstance(result["issues"], list)


class TestCapContext(unittest.TestCase):
    """Tests for _cap_context()."""

    def test_under_limit_unchanged(self):
        text = "Short context here."
        self.assertEqual(tc._cap_context(text, max_chars=1000), text)

    def test_over_limit_drops_oldest(self):
        # Build text with multiple ## PLAN sections > 12000 chars
        sections = []
        for i in range(10):
            sections.append(f"## PLAN Section {i}\n" + "x" * 2000)
        text = "\n\n".join(sections)
        self.assertGreater(len(text), 12000)
        result = tc._cap_context(text, max_chars=12000)
        self.assertLessEqual(len(result), 12000)
        # Oldest sections should be dropped; newest should remain
        self.assertIn("Section 9", result)

    def test_single_section_preserved(self):
        text = "## PLAN\n" + "x" * 15000
        result = tc._cap_context(text, max_chars=12000)
        # Single section is kept even if over limit
        self.assertIn("## PLAN", result)


class TestBuildDirectPrompt(unittest.TestCase):
    """Tests for _build_direct_prompt()."""

    def test_plan_stage(self):
        result = tc._build_direct_prompt("plan", "Add logging", "")
        self.assertIn("step-by-step plan", result)

    def test_code_stage_with_context(self):
        # Context must be >50 chars after cleaning to be appended
        long_context = "## PLAN\n1. Edit main.py to add logging module\n2. Add logger configuration\n3. Wire up handlers and formatters throughout"
        result = tc._build_direct_prompt("code", "Add logging", long_context)
        self.assertIn("Add logging", result)
        self.assertIn("Plan:", result)

    def test_code_stage_filters_garbage(self):
        garbage_context = (
            "## PLAN\n"
            "1. Edit main.py\n"
            "could you share the codebase?\n"
            "2. Add logger\n"
        )
        result = tc._build_direct_prompt("code", "Add logging", garbage_context)
        self.assertNotIn("could you share", result)

    def test_test_stage(self):
        result = tc._build_direct_prompt("test", "Add logging", "")
        self.assertIn("git diff", result)


class TestBuildCliCommand(unittest.TestCase):
    """Tests for build_cli_command() and get_timeout()."""

    def setUp(self):
        self.providers_cfg = json.loads(
            (Path(__file__).parent / "providers.json").read_text(encoding="utf-8")
        )
        self.providers = self.providers_cfg["providers"]

    def test_claude_plan_command(self):
        cmd = tc.build_cli_command(self.providers["claude"], "plan", "Test prompt")
        cmd_str = " ".join(cmd)
        self.assertIn("-p", cmd)
        self.assertIn("Test prompt", cmd)
        self.assertIn("--permission-mode", cmd_str)
        self.assertIn("plan", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)

    def test_claude_implement_command(self):
        cmd = tc.build_cli_command(self.providers["claude"], "implement", "Test prompt")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)

    def test_copilot_plan_command(self):
        cmd = tc.build_cli_command(self.providers["copilot"], "plan", "Test prompt")
        # Should start with resolved binary or "gh"
        self.assertTrue(cmd[0].endswith("gh") or cmd[0].endswith("gh.exe") or "gh" in cmd[0])
        self.assertIn("copilot", cmd)
        self.assertIn("-p", cmd)

    def test_aider_plan_command(self):
        cmd = tc.build_cli_command(self.providers["aider"], "plan", "Test prompt")
        self.assertTrue("aider" in cmd[0].lower())
        self.assertIn("--message", cmd)
        self.assertIn("--dry-run", cmd)

    def test_codex_implement_command(self):
        cmd = tc.build_cli_command(self.providers["codex"], "implement", "Test prompt")
        self.assertTrue("codex" in cmd[0].lower())
        self.assertIn("--full-auto", cmd)

    def test_all_providers_all_phases(self):
        phases = ["plan", "implement", "security", "test", "review"]
        for pname, prov in self.providers.items():
            for phase in phases:
                cmd = tc.build_cli_command(prov, phase, "Test prompt for all")
                self.assertTrue(len(cmd) > 0, f"{pname}/{phase}: empty command")
                # Binary should be first element
                self.assertTrue(
                    prov["binary"] in cmd[0].lower() or cmd[0].endswith(prov["binary"]),
                    f"{pname}/{phase}: binary mismatch — got {cmd[0]}"
                )
                # No literal {prompt} placeholders should remain
                for arg in cmd:
                    self.assertNotIn("{prompt}", arg,
                                     f"{pname}/{phase}: unresolved {{prompt}} in {arg}")

    def test_simplify_fallback(self):
        # Providers without simplify_args should fall back to implement_args
        prov = dict(self.providers["copilot"])
        # copilot has no simplify_args
        self.assertNotIn("simplify_args", prov)
        cmd = tc.build_cli_command(prov, "simplify", "Test prompt")
        # Should use implement_args (which has --yolo for copilot)
        self.assertIn("--yolo", cmd)

    def test_prompt_file_substitution(self):
        prov = {
            "binary": "test-cli",
            "subcommand": [],
            "plan_args": ["--file", "{prompt_file}", "-p", "{prompt}"],
        }
        fake_path = Path("/tmp/prompt.md")
        cmd = tc.build_cli_command(prov, "plan", "Hello", prompt_file=fake_path)
        self.assertIn(str(fake_path), cmd)
        self.assertNotIn("{prompt_file}", " ".join(cmd))


class TestGetProviderForPhase(unittest.TestCase):
    """Tests for get_provider_for_phase()."""

    def test_default_provider(self):
        # With no env overrides, should return default from providers.json
        with patch.dict(os.environ, {}, clear=False):
            # Remove any env overrides that might exist
            env_keys = ["CLI_PROVIDER", "CLI_PLAN_PROVIDER", "CLI_IMPLEMENT_PROVIDER",
                        "CLI_SECURITY_PROVIDER", "CLI_TEST_PROVIDER", "CLI_REVIEW_PROVIDER"]
            clean_env = {k: v for k, v in os.environ.items() if k not in env_keys}
            with patch.dict(os.environ, clean_env, clear=True):
                provider = tc.get_provider_for_phase("plan")
                # Default is "claude" per providers.json
                self.assertEqual(provider["name"], "Claude Code")

    def test_env_override(self):
        with patch.dict(os.environ, {"CLI_PROVIDER": "copilot"}):
            provider = tc.get_provider_for_phase("plan")
            self.assertEqual(provider["name"], "GitHub Copilot CLI")

    def test_phase_specific_override(self):
        with patch.dict(os.environ, {"CLI_IMPLEMENT_PROVIDER": "copilot"}):
            impl_provider = tc.get_provider_for_phase("implement")
            self.assertEqual(impl_provider["name"], "GitHub Copilot CLI")
            # Plan should still use default (no CLI_PLAN_PROVIDER set)
            # Need to clear CLI_PROVIDER to get true default
            env_clean = {k: v for k, v in os.environ.items()
                         if k not in ("CLI_PROVIDER", "CLI_PLAN_PROVIDER")}
            env_clean["CLI_IMPLEMENT_PROVIDER"] = "copilot"
            with patch.dict(os.environ, env_clean, clear=True):
                plan_provider = tc.get_provider_for_phase("plan")
                self.assertEqual(plan_provider["name"], "Claude Code")


class TestParseClaudeJsonOutput(unittest.TestCase):
    """Tests for _parse_claude_json_output()."""

    def test_valid_json_output(self):
        data = json.dumps([
            {"role": "assistant", "content": [
                {"type": "text", "text": "Here is the implementation."},
                {"type": "tool_use", "name": "Edit", "input": {}},
                {"type": "text", "text": "Done with changes."},
            ]}
        ])
        text, subagents, tools = tc._parse_claude_json_output(data)
        self.assertIn("Here is the implementation", text)
        self.assertIn("Done with changes", text)
        self.assertEqual(tools.get("Edit", 0), 1)
        self.assertEqual(subagents, 0)

    def test_plain_text_passthrough(self):
        raw = "This is just plain text output, not JSON."
        text, subagents, tools = tc._parse_claude_json_output(raw)
        self.assertEqual(text, raw)
        self.assertEqual(subagents, 0)
        self.assertEqual(tools, {})

    def test_malformed_json(self):
        raw = '[{"role": "assistant", "content": [{"type": "text"'
        text, subagents, tools = tc._parse_claude_json_output(raw)
        self.assertEqual(text, raw)
        self.assertEqual(subagents, 0)

    def test_subagent_counted(self):
        data = json.dumps([
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Agent", "input": {}},
                {"type": "text", "text": "Delegated work to subagent."},
            ]}
        ])
        text, subagents, tools = tc._parse_claude_json_output(data)
        self.assertEqual(subagents, 1)
        self.assertEqual(tools.get("Agent", 0), 1)


# ═══════════════════════════════════════════════════════════════════════════
#  PIPELINE FLOW TESTS — mocked
# ═══════════════════════════════════════════════════════════════════════════

class PipelineTestBase(unittest.TestCase):
    """Base class for pipeline flow tests with common setUp/tearDown."""

    def setUp(self):
        """Reset agent state and set up common patches."""
        with tc.status_lock:
            tc.agent_status["state"] = "idle"
            tc.agent_status["current_task"] = None
            tc.agent_status["current_stage"] = None
        tc._reset_pipeline_stats()

        # Common patches
        self.patches = []

        # Mock _save_stage_output to avoid creating real directories
        p = patch.object(tc, "_save_stage_output", return_value=Path("/fake/output.md"))
        self.mock_save = p.start()
        self.patches.append(p)

        # Mock _git_commit_and_push
        p = patch.object(tc, "_git_commit_and_push", return_value=True)
        self.mock_git = p.start()
        self.patches.append(p)

        # Mock _restart_changed_services
        p = patch.object(tc, "_restart_changed_services")
        self.mock_restart = p.start()
        self.patches.append(p)

        # Mock rewrite_prompt to passthrough
        p = patch.object(tc, "rewrite_prompt", side_effect=lambda prompt, cfg: prompt)
        self.mock_rewrite = p.start()
        self.patches.append(p)

        # Mock run_security_review
        p = patch.object(tc, "run_security_review",
                         return_value={"max_severity": "LOW", "report": "All clear. No issues found."})
        self.mock_security = p.start()
        self.patches.append(p)

        # Mock _handle_security_findings
        p = patch.object(tc, "_handle_security_findings", return_value="ok")
        self.mock_handle_sec = p.start()
        self.patches.append(p)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _good_team_output(self, name="claude"):
        return [(name, GOOD_OUTPUT)]


class TestPipelineHappyPath(PipelineTestBase):
    """Test that a fully-approved pipeline completes successfully."""

    def test_all_stages_pass(self):
        with patch.object(tc, "_pm_health_check", return_value=True), \
             patch.object(tc, "run_team", return_value=self._good_team_output()), \
             patch.object(tc, "_pm_api_call", return_value=make_pm_response()), \
             patch.object(tc, "pm_direct_team", return_value=(GOOD_OUTPUT, True)), \
             patch.object(tc, "pm_oversee_stage", return_value=tc._parse_overseer_response(make_pm_response())):

            result = tc.run_pipeline("Add a login feature", task_id="test-happy")

        self.assertTrue(result["success"])
        self.assertTrue(result["published"])
        # Should have entries for all stages: rewrite, plan, code, simplify, test, review
        stage_names = [entry["stage"] for entry in result["stage_log"]]
        for expected in ["rewrite", "plan", "code", "simplify", "test", "review"]:
            self.assertIn(expected, stage_names,
                          f"Missing stage '{expected}' in stage_log: {stage_names}")
        self.assertIsNone(result["error"])


class TestPipelinePMFailover(PipelineTestBase):
    """Test that PM failure gracefully falls back to direct mode."""

    def test_pm_failure_falls_to_direct(self):
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team_output()):

            result = tc.run_pipeline("Add a login feature", task_id="test-failover")

        self.assertTrue(result["success"])
        # In direct mode, stages should still complete
        stage_names = [entry["stage"] for entry in result["stage_log"]]
        self.assertIn("plan", stage_names)
        self.assertIn("code", stage_names)


class TestPipelineGarbageRetry(PipelineTestBase):
    """Test garbage detection and retry behavior."""

    def test_garbage_triggers_retry(self):
        call_count = {"n": 0}

        def mock_run_team(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                # First call per stage is garbage for the plan stage
                stage = args[0] if args else kwargs.get("stage_name", "")
                if stage == "plan" and call_count["n"] == 1:
                    return [("claude", "I don't have access to the files.")]
                return self._good_team_output()
            return self._good_team_output()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):

            result = tc.run_pipeline("Add a login feature", task_id="test-garbage-retry")

        self.assertTrue(result["success"])

    def test_double_garbage_skips_stage(self):
        def mock_run_team_garbage(*args, **kwargs):
            stage = args[0] if args else ""
            if stage == "plan":
                return [("claude", "I don't have access")]
            return self._good_team_output()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team_garbage):

            result = tc.run_pipeline("Add a login feature", task_id="test-double-garbage")

        self.assertTrue(result["success"])
        # Plan stage result should be empty
        self.assertEqual(result["stage_results"].get("plan", ""), "")


class TestPipelineTestCodeLoopback(PipelineTestBase):
    """Test that test failures trigger a code-fix loopback."""

    def test_failures_trigger_fix(self):
        call_count = {"n": 0}

        def mock_run_team(stage_name, *args, **kwargs):
            call_count["n"] += 1
            if stage_name == "test":
                return [("claude",
                         "Running tests... SyntaxError found in code at line 42. "
                         "The function parse_data has an unclosed parenthesis. "
                         "This needs to be fixed before the code can run. " * 3)]
            return self._good_team_output()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):

            result = tc.run_pipeline("Add a login feature", task_id="test-loopback")

        self.assertTrue(result["success"])
        stage_names = [entry["stage"] for entry in result["stage_log"]]
        self.assertIn("code-fix", stage_names)

    def test_passing_tests_no_loopback(self):
        def mock_run_team(stage_name, *args, **kwargs):
            if stage_name == "test":
                return [("claude",
                         "All tests passed, everything looks good. "
                         "The implementation is correct and handles all edge cases properly. "
                         "No issues were found during testing. " * 3)]
            return self._good_team_output()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):

            result = tc.run_pipeline("Add a login feature", task_id="test-no-loopback")

        self.assertTrue(result["success"])
        stage_names = [entry["stage"] for entry in result["stage_log"]]
        self.assertNotIn("code-fix", stage_names)


class TestPipelinePublishGating(PipelineTestBase):
    """Validates Phase 1a — test_passed flag gating publish.

    NOTE: Currently publish is NOT gated on test results.
    These tests document DESIRED behavior after Phase 1 changes.
    """

    @unittest.skip("Pending Phase 1a implementation")
    def test_test_failure_blocks_publish(self):
        def mock_run_team(stage_name, *args, **kwargs):
            if stage_name == "test":
                return [("claude",
                         "FAIL: test_login failed with AssertionError. "
                         "Expected 200 but got 500. The server crashes on login. " * 3)]
            return self._good_team_output()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):

            result = tc.run_pipeline("Add a login feature", task_id="test-block-pub")

        self.assertFalse(result["published"])

    @unittest.skip("Pending Phase 1a implementation")
    def test_test_pass_allows_publish(self):
        def mock_run_team(stage_name, *args, **kwargs):
            if stage_name == "test":
                return [("claude",
                         "All tests passed. 12 tests ran, 0 failures. "
                         "The implementation is correct. " * 3)]
            return self._good_team_output()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):

            result = tc.run_pipeline("Add a login feature", task_id="test-allow-pub")

        self.assertTrue(result["published"])


class TestPipelineSecurityBlock(PipelineTestBase):
    """Test that HIGH severity security findings block publishing."""

    def test_high_severity_blocks(self):
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team_output()), \
             patch.object(tc, "run_security_review",
                          return_value={"max_severity": "HIGH",
                                        "report": "Critical SQL injection vulnerability found."}), \
             patch.object(tc, "_handle_security_findings", return_value="blocked"), \
             patch.object(tc, "pm_oversee_stage",
                          return_value=tc._parse_overseer_response(make_pm_response())):

            result = tc.run_pipeline("Add a login feature", task_id="test-sec-block")

        self.assertFalse(result["published"])
        # Pipeline should report failure when blocked
        self.assertFalse(result["success"])
        self.assertIn("security", result.get("error", "").lower())


if __name__ == "__main__":
    unittest.main()
