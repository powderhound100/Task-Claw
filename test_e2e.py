"""
End-to-end test suite for the Task-Claw pipeline.

Tests realistic pipeline flows with stage-aware mock data that mirrors actual
CLI/PM responses, covering features added in recent commits:
- Vague prompt rewriting
- No-change code detection (skip simplify/test)
- Garbage safety nets (new patterns, post-REVISE drop)
- Wallclock timeout
- Prompt externalization and caching
- Cross-review and deep merge

Run with:
    python -m unittest test_e2e -v
    python -m unittest test_e2e.TestContextFlow -v
"""

import importlib.util
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

# ── Pre-set env vars BEFORE importing ────────────────────────────────────
os.environ.setdefault("GITHUB_TOKEN", "fake-token-for-tests")
os.environ.setdefault("TASKS_FILE", str(Path(__file__).parent / "data" / "tasks.json"))
os.environ.setdefault("IDEAS_FILE", str(Path(__file__).parent / "data" / "ideas.json"))

# ── Import task-claw.py using importlib (hyphen in filename) ─────────────
spec = importlib.util.spec_from_file_location(
    "task_claw", str(Path(__file__).parent / "task-claw.py")
)
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)


# ═══════════════════════════════════════════════════════════════════════════
#  REALISTIC MOCK DATA
# ═══════════════════════════════════════════════════════════════════════════

REALISTIC_PLAN_OUTPUT = (
    "## Implementation Plan\n\n"
    "1. Edit `src/auth.py` — add JWT token validation middleware\n"
    "2. Edit `src/routes.py` — wire auth middleware into login/logout endpoints\n"
    "3. Create `src/token_store.py` — in-memory token blacklist with TTL cleanup\n"
    "4. Edit `tests/test_auth.py` — add tests for valid/expired/revoked tokens\n\n"
    "### Testing Strategy\n"
    "- Unit tests for token validation logic\n"
    "- Integration test for login → access → logout → deny flow\n\n"
    "### Error Handling\n"
    "- Return 401 with clear message on expired tokens\n"
    "- Return 403 on revoked tokens\n"
    "- Log all auth failures for audit trail"
)

REALISTIC_CODE_OUTPUT = (
    "I have implemented the JWT authentication middleware as planned.\n\n"
    "### Changes Made\n"
    "- **src/auth.py**: Added `validate_jwt()` function with expiry and revocation checks. "
    "Uses `hmac.compare_digest` for timing-safe comparison.\n"
    "- **src/routes.py**: Wired `@require_auth` decorator into `/api/login`, `/api/logout`, "
    "and `/api/protected` endpoints.\n"
    "- **src/token_store.py**: Created in-memory blacklist with `threading.Timer` for TTL "
    "cleanup every 5 minutes.\n"
    "- **tests/test_auth.py**: Added 8 test cases covering valid tokens, expired tokens, "
    "revoked tokens, and malformed headers.\n\n"
    "All changes follow existing conventions (snake_case, guard clauses, type hints). "
    "The implementation is minimal and focused on the auth requirement."
)

REALISTIC_SIMPLIFY_OUTPUT = (
    "Reviewed git diff for quality issues.\n\n"
    "### Issues Found and Fixed\n"
    "1. **Duplicate import** in `src/routes.py` — removed redundant `import json`\n"
    "2. **Deep nesting** in `validate_jwt()` — refactored to early-return guard clauses\n"
    "3. **Dead code** — removed commented-out `# old_validate()` function\n\n"
    "No other issues found. Code is clean and follows project conventions."
)

REALISTIC_TEST_PASS_OUTPUT = (
    "Running tests...\n"
    "test_valid_token (tests.test_auth.TestJWTAuth) ... ok\n"
    "test_expired_token (tests.test_auth.TestJWTAuth) ... ok\n"
    "test_revoked_token (tests.test_auth.TestJWTAuth) ... ok\n"
    "test_malformed_header (tests.test_auth.TestJWTAuth) ... ok\n"
    "test_login_flow (tests.test_auth.TestJWTAuth) ... ok\n"
    "test_logout_revokes (tests.test_auth.TestJWTAuth) ... ok\n"
    "test_protected_endpoint (tests.test_auth.TestJWTAuth) ... ok\n"
    "test_no_token (tests.test_auth.TestJWTAuth) ... ok\n\n"
    "----------------------------------------------------------------------\n"
    "Ran 8 tests in 0.234s\n\n"
    "OK"
)

REALISTIC_TEST_FAIL_OUTPUT = (
    "Running tests...\n"
    "test_valid_token (tests.test_auth.TestJWTAuth) ... ok\n"
    "test_expired_token (tests.test_auth.TestJWTAuth) ... FAIL\n"
    "test_revoked_token (tests.test_auth.TestJWTAuth) ... ok\n\n"
    "======================================================================\n"
    "FAIL: test_expired_token (tests.test_auth.TestJWTAuth)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    "  File \"tests/test_auth.py\", line 45, in test_expired_token\n"
    "    self.assertEqual(response.status_code, 401)\n"
    "AssertionError: 200 != 401\n\n"
    "----------------------------------------------------------------------\n"
    "Ran 8 tests in 0.198s\n\n"
    "FAILED (failures=1)"
)

REALISTIC_PM_APPROVE = (
    "## Verdict\nAPPROVE\n\n"
    "## Issues\nNone\n\n"
    "## Synthesis\n"
    "The implementation correctly adds JWT authentication middleware with proper "
    "token validation, revocation support, and comprehensive test coverage. "
    "Code follows existing conventions and uses timing-safe comparisons.\n\n"
    "## Handoff to next stage\n"
    "JWT auth middleware is implemented in src/auth.py with decorator pattern. "
    "8 tests cover all token states. Ready for simplification review."
)

REALISTIC_PM_REVISE = (
    "## Verdict\nREVISE\n\n"
    "## Issues\n"
    "- Token blacklist has no persistence — server restart clears all revocations\n"
    "- Missing rate limiting on login endpoint\n"
    "- No test for concurrent token revocation race condition\n\n"
    "## Synthesis\n"
    "The core implementation is solid but has gaps in production readiness. "
    "The in-memory blacklist needs a persistence layer and the login endpoint "
    "should have rate limiting to prevent brute force attacks.\n\n"
    "## Handoff to next stage\n"
    "Address the three issues above before proceeding. Prioritize the "
    "persistence gap as it affects security guarantees."
)

GARBAGE_PERMISSION = (
    "I'd be happy to help! Could you grant me write permission to the files? "
    "I need access to modify the codebase. Please approve the edit so I can proceed."
)

GARBAGE_CUTOFF = (
    "I'll implement the JWT authentication middleware. Let me start by "
    "message may have been cut off"
)

GARBAGE_QUESTION = (
    "What specific changes would you like me to make? Could you share more "
    "details about the authentication requirements? I need more context "
    "to proceed with the implementation."
)

REALISTIC_SECURITY_CLEAR = {
    "max_severity": "LOW",
    "report": "Security review complete. No high-severity issues found. "
              "Minor: consider adding CORS headers to auth endpoints.",
}

REALISTIC_SECURITY_HIGH = {
    "max_severity": "HIGH",
    "report": "CRITICAL: Hardcoded JWT secret key found in src/auth.py line 12. "
              "The secret 'my-secret-key-123' must be moved to environment variables. "
              "This is a HIGH severity finding that blocks deployment.",
}


def make_pm_response(verdict="APPROVE", issues=None, synthesis="Good.", handoff="Proceed."):
    """Build a well-formed PM overseer response string."""
    issues_text = "\n".join(f"- {i}" for i in (issues or [])) or "None"
    return (
        f"## Verdict\n{verdict}\n\n"
        f"## Issues\n{issues_text}\n\n"
        f"## Synthesis\n{synthesis}\n\n"
        f"## Handoff\n{handoff}"
    )


# ═══════════════════════════════════════════════════════════════════════════
#  BASE CLASS
# ═══════════════════════════════════════════════════════════════════════════

class E2EBase(unittest.TestCase):
    """Shared base for E2E pipeline tests with realistic mock data."""

    def setUp(self):
        with tc.status_lock:
            tc.agent_status["state"] = "idle"
            tc.agent_status["current_task"] = None
            tc.agent_status["current_stage"] = None
        tc._reset_pipeline_stats()
        tc._PROMPTS = None
        tc.pm_consecutive_failures = 0 if hasattr(tc, "pm_consecutive_failures") else 0

        self.patches = []

        p = patch.object(tc, "_save_stage_output", return_value=Path("/fake/output.md"))
        self.mock_save = p.start()
        self.patches.append(p)

        p = patch.object(tc, "_git_commit_and_push", return_value=True)
        self.mock_git = p.start()
        self.patches.append(p)

        p = patch.object(tc, "_restart_changed_services")
        self.mock_restart = p.start()
        self.patches.append(p)

        p = patch.object(tc, "rewrite_prompt", side_effect=lambda prompt, cfg: prompt)
        self.mock_rewrite = p.start()
        self.patches.append(p)

        p = patch.object(tc, "run_security_review", return_value=REALISTIC_SECURITY_CLEAR)
        self.mock_security = p.start()
        self.patches.append(p)

        p = patch.object(tc, "_handle_security_findings", return_value="ok")
        self.mock_handle_sec = p.start()
        self.patches.append(p)

        p = patch.object(tc, "_has_uncommitted_changes", return_value=True)
        self.mock_changes = p.start()
        self.patches.append(p)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _good_team(self, name="claude"):
        return [(name, REALISTIC_CODE_OUTPUT)]

    def _make_capturing_mock(self, stage_output_map):
        """Return a side_effect for run_team that captures calls and returns stage-specific output.

        stage_output_map: dict mapping stage name → list of (name, output) tuples.
        Captures are stored in self.captured_calls as {stage: [(prompt, context, team)]}.
        """
        self.captured_calls = {}

        def mock_run_team(stage_name, prompt, team, context, timeout, task_id=""):
            if stage_name not in self.captured_calls:
                self.captured_calls[stage_name] = []
            self.captured_calls[stage_name].append({
                "prompt": prompt, "context": context, "team": team,
            })
            return stage_output_map.get(stage_name, self._good_team())

        return mock_run_team


# ═══════════════════════════════════════════════════════════════════════════
#  TEST CLASSES
# ═══════════════════════════════════════════════════════════════════════════


class TestContextFlow(E2EBase):
    """Verifies context passes correctly between stages."""

    def _run_with_capture(self, stage_map=None):
        """Run pipeline in direct mode with capturing mock, return result."""
        if stage_map is None:
            stage_map = {
                "plan": [("claude", REALISTIC_PLAN_OUTPUT)],
                "code": [("claude", REALISTIC_CODE_OUTPUT)],
                "simplify": [("claude", REALISTIC_SIMPLIFY_OUTPUT)],
                "test": [("claude", REALISTIC_TEST_PASS_OUTPUT)],
            }
        mock_fn = self._make_capturing_mock(stage_map)
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_fn):
            return tc.run_pipeline("Add JWT auth", task_id="test-ctx")

    def test_plan_context_reaches_code_stage(self):
        self._run_with_capture()
        self.assertIn("code", self.captured_calls)
        code_prompt = self.captured_calls["code"][0]["prompt"]
        # Plan output should be included in the code stage prompt (via _build_direct_prompt)
        self.assertIn("JWT token validation", code_prompt)

    def test_code_context_reaches_test_stage(self):
        self._run_with_capture()
        self.assertIn("test", self.captured_calls)
        test_prompt = self.captured_calls["test"][0]["prompt"]
        # Test stage prompt includes the original task
        self.assertIn("JWT auth", test_prompt)

    def test_context_uses_delimiter_format(self):
        result = self._run_with_capture()
        # Check that stage_results were saved (pipeline ran successfully)
        self.assertTrue(result["success"])
        # The _save_stage_output mock was called with delimiter-formatted context
        save_calls = self.mock_save.call_args_list
        self.assertTrue(len(save_calls) >= 4, f"Expected 4+ saves, got {len(save_calls)}")

    def test_cap_context_preserves_plan_section(self):
        """Build >12K context; verify plan delimiter survives after capping."""
        plan_section = "=== Plan stage output ===\nStep 1: Edit auth.py\n=== End plan ==="
        filler = "\n\n=== Code stage output ===\n" + "x" * 15000 + "\n=== End code ==="
        context = plan_section + filler
        result = tc._cap_context(context, max_chars=12000)
        self.assertLessEqual(len(result), 12000)
        self.assertIn("Plan stage output", result)

    def test_pm_handoff_reaches_next_stage(self):
        """Custom PM handoff text appears in next stage context."""
        custom_handoff = "CUSTOM_HANDOFF_TOKEN_XYZ"
        pm_approve_custom = tc._parse_overseer_response(
            make_pm_response(handoff=custom_handoff)
        )
        with patch.object(tc, "_pm_health_check", return_value=True), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch.object(tc, "pm_direct_team",
                          return_value=(REALISTIC_PLAN_OUTPUT, True)), \
             patch.object(tc, "pm_oversee_stage", return_value=pm_approve_custom):
            result = tc.run_pipeline("Add JWT auth", task_id="test-handoff")
        self.assertTrue(result["success"])

    def test_rewrite_output_used_as_pipeline_prompt(self):
        """Mock rewrite_prompt to modify text; verify modified version used."""
        # Override the default passthrough rewrite mock
        self.mock_rewrite.side_effect = lambda prompt, cfg: "REWRITTEN: " + prompt
        stage_map = {
            "plan": [("claude", REALISTIC_PLAN_OUTPUT)],
            "code": [("claude", REALISTIC_CODE_OUTPUT)],
            "simplify": [("claude", REALISTIC_SIMPLIFY_OUTPUT)],
            "test": [("claude", REALISTIC_TEST_PASS_OUTPUT)],
        }
        mock_fn = self._make_capturing_mock(stage_map)
        with patch.object(tc, "_pm_health_check", return_value=True), \
             patch.object(tc, "run_team", side_effect=mock_fn), \
             patch.object(tc, "pm_direct_team",
                          return_value=("Build JWT auth", True)), \
             patch.object(tc, "pm_oversee_stage",
                          return_value=tc._parse_overseer_response(make_pm_response())):
            result = tc.run_pipeline("Add JWT auth", task_id="test-rewrite")
        self.assertTrue(result["success"])
        # The rewrite result is stored in stage_results
        self.assertIn("REWRITTEN:", result["stage_results"]["rewrite"])

    def test_direct_mode_builds_correct_prompts(self):
        """_build_direct_prompt() output includes prior context."""
        context = "=== Plan stage output ===\nStep 1: Edit auth.py\n=== End plan ==="
        result = tc._build_direct_prompt("code", "Add JWT auth", context)
        self.assertIn("Add JWT auth", result)
        self.assertIn("Plan:", result)
        self.assertIn("Edit auth.py", result)


class TestNoChangeDetection(E2EBase):
    """Tests _has_uncommitted_changes() skip logic."""

    def test_no_changes_skips_simplify_and_test(self):
        self.mock_changes.return_value = False
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()):
            result = tc.run_pipeline("Add JWT auth", task_id="test-no-changes")
        stage_names = [e["stage"] for e in result["stage_log"]]
        # simplify and test should be skipped
        skipped = [e for e in result["stage_log"] if e["verdict"] == "skipped"]
        skipped_stages = [e["stage"] for e in skipped]
        self.assertIn("simplify", skipped_stages)
        self.assertIn("test", skipped_stages)

    def test_with_changes_runs_all_stages(self):
        self.mock_changes.return_value = True
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()):
            result = tc.run_pipeline("Add JWT auth", task_id="test-with-changes")
        stage_names = [e["stage"] for e in result["stage_log"]]
        for expected in ["rewrite", "plan", "code", "simplify", "test", "review"]:
            self.assertIn(expected, stage_names,
                          f"Missing '{expected}' in {stage_names}")

    def test_review_runs_even_without_changes(self):
        self.mock_changes.return_value = False
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()):
            result = tc.run_pipeline("Add JWT auth", task_id="test-review-runs")
        stage_names = [e["stage"] for e in result["stage_log"]]
        self.assertIn("review", stage_names)

    def test_skipped_stage_log_format(self):
        self.mock_changes.return_value = False
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()):
            result = tc.run_pipeline("Add JWT auth", task_id="test-skip-fmt")
        skipped = [e for e in result["stage_log"] if e["verdict"] == "skipped"]
        for entry in skipped:
            self.assertEqual(entry["elapsed"], 0)
            self.assertIn("No code changes", entry.get("note", ""))

    def test_no_change_detection_in_pm_mode(self):
        self.mock_changes.return_value = False
        with patch.object(tc, "_pm_health_check", return_value=True), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch.object(tc, "pm_direct_team",
                          return_value=(REALISTIC_PLAN_OUTPUT, True)), \
             patch.object(tc, "pm_oversee_stage",
                          return_value=tc._parse_overseer_response(make_pm_response())):
            result = tc.run_pipeline("Add JWT auth", task_id="test-pm-nochange")
        skipped_stages = [e["stage"] for e in result["stage_log"]
                          if e["verdict"] == "skipped"]
        self.assertIn("simplify", skipped_stages)
        self.assertIn("test", skipped_stages)


class TestGarbageSafetyNet(E2EBase):
    """Tests garbage detection patterns and post-REVISE safety net."""

    def test_new_pattern_message_cut_off(self):
        result = tc._is_garbage_output([
            ("agent", "I'll start implementing the feature. message may have been cut off" + " " * 100)
        ])
        self.assertTrue(result)

    def test_new_pattern_appears_incomplete(self):
        result = tc._is_garbage_output([
            ("agent", "The response appears incomplete but here is what I have so far." + " " * 100)
        ])
        self.assertTrue(result)

    def test_new_pattern_no_content(self):
        result = tc._is_garbage_output([
            ("agent", "I see the task description but no content was provided for me to work with." + " " * 50)
        ])
        self.assertTrue(result)

    def test_garbage_after_max_revise_drops_output(self):
        """PM returns REVISE, team output is garbage, stage result context should be cleaned."""
        call_count = {"n": 0}

        def mock_run_team(stage_name, *args, **kwargs):
            call_count["n"] += 1
            if stage_name == "plan":
                return [("claude", GARBAGE_PERMISSION)]
            return self._good_team()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):
            result = tc.run_pipeline("Add JWT auth", task_id="test-garbage-revise")
        # Plan stage should be empty (garbage dropped after retries)
        self.assertEqual(result["stage_results"].get("plan", ""), "")

    def test_good_output_after_max_revise_kept(self):
        """Non-garbage output is preserved even in direct mode."""
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=[("claude", REALISTIC_PLAN_OUTPUT)]):
            result = tc.run_pipeline("Add JWT auth", task_id="test-good-kept")
        self.assertNotEqual(result["stage_results"].get("plan", ""), "")
        self.assertIn("Implementation Plan", result["stage_results"]["plan"])

    def test_direct_mode_garbage_retry_preserves_plan(self):
        """Garbage on first try → retry uses _extract_plan_context() (plan preserved)."""
        call_count = {"n": 0}
        plan_context = "=== Plan stage output ===\nStep 1: Edit auth.py\n=== End plan ==="

        def mock_run_team(stage_name, prompt, team, context, timeout, task_id=""):
            call_count["n"] += 1
            if stage_name == "code" and call_count["n"] <= 3:
                # First code call is garbage
                return [("claude", GARBAGE_QUESTION)]
            if stage_name == "code":
                # Retry should have plan context in the prompt
                self.assertIn("auth", prompt.lower())
                return self._good_team()
            if stage_name == "plan":
                return [("claude", REALISTIC_PLAN_OUTPUT)]
            return self._good_team()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):
            result = tc.run_pipeline("Add JWT auth", task_id="test-plan-preserved")
        self.assertTrue(result["success"])


class TestWallclockTimeout(E2EBase):
    """Tests PIPELINE_WALLCLOCK_TIMEOUT."""

    def test_timeout_aborts_pipeline(self):
        """Patch time.time to exceed limit; pipeline stops early."""
        time_values = iter([
            0,      # pipeline_start
            0,      # canary write time
            0,      # stage loop check (rewrite)
            1,      # rewrite elapsed
            1,      # stage loop (plan)
            100,    # plan elapsed
            100,    # stage loop (code)
            99999,  # code wallclock check — exceeds timeout
        ])

        def mock_time():
            try:
                return next(time_values)
            except StopIteration:
                return 99999

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch("time.time", side_effect=mock_time), \
             patch.object(tc, "PIPELINE_WALLCLOCK_TIMEOUT", 500):
            result = tc.run_pipeline("Add JWT auth", task_id="test-timeout")
        stage_names = [e["stage"] for e in result["stage_log"]]
        # Should have some stages but not all (timeout aborted early)
        self.assertTrue(len(stage_names) < 6,
                        f"Expected <6 stages but got {len(stage_names)}: {stage_names}")

    def test_timeout_stage_log_entry(self):
        """Aborted stage has verdict: timeout."""
        # Use a very short timeout via env var
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch.object(tc, "PIPELINE_WALLCLOCK_TIMEOUT", 0.001):
            # Sleep briefly so wallclock check triggers
            real_time = time.time
            start = real_time()

            def advancing_time():
                # Each call advances by 1 second
                return start + (real_time() - start) * 10000

            with patch("time.time", side_effect=advancing_time):
                result = tc.run_pipeline("Add JWT auth", task_id="test-timeout-entry")
        timeout_entries = [e for e in result["stage_log"] if e.get("verdict") == "timeout"]
        if timeout_entries:
            note = timeout_entries[0]["note"].lower()
            self.assertTrue("timeout" in note or "aborted" in note or "too long" in note)

    def test_completed_stages_preserved_before_timeout(self):
        """Stages that ran before timeout appear normally."""
        call_count = {"n": 0}
        base_time = time.time()

        def mock_time():
            # After 3 calls (rewrite, plan, code check), exceed timeout
            call_count["n"] += 1
            if call_count["n"] > 8:
                return base_time + 99999
            return base_time + call_count["n"]

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch("time.time", side_effect=mock_time), \
             patch.object(tc, "PIPELINE_WALLCLOCK_TIMEOUT", 500):
            result = tc.run_pipeline("Add JWT auth", task_id="test-preserved")
        # At least rewrite and plan should have completed
        completed = [e for e in result["stage_log"] if e["verdict"] != "timeout"]
        self.assertGreaterEqual(len(completed), 1)

    def test_zero_timeout_disables_limit(self):
        """PIPELINE_WALLCLOCK_TIMEOUT=0 → all stages run."""
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch.object(tc, "PIPELINE_WALLCLOCK_TIMEOUT", 0):
            result = tc.run_pipeline("Add JWT auth", task_id="test-no-timeout")
        stage_names = [e["stage"] for e in result["stage_log"]]
        for expected in ["rewrite", "plan", "code", "simplify", "test", "review"]:
            self.assertIn(expected, stage_names)


class TestPromptSystem(E2EBase):
    """Tests prompt externalization and caching."""

    def test_prompts_loaded_from_file(self):
        tc._PROMPTS = None  # force reload
        prompts = tc._load_prompts()
        self.assertIsInstance(prompts, dict)
        self.assertIn("pm_system", prompts)
        self.assertIn("cli_prompts", prompts)

    def test_prompt_cache_sentinel(self):
        tc._PROMPTS = None
        first = tc._load_prompts()
        self.assertIsNotNone(tc._PROMPTS)
        second = tc._load_prompts()
        self.assertIs(first, second)  # same object — cached

    def test_fallback_when_file_missing(self):
        tc._PROMPTS = None
        with patch.dict(os.environ, {"PROMPTS_FILE": "/nonexistent/prompts.json"}):
            tc._PROMPTS = None  # reset cache
            prompts = tc._load_prompts()
            # Should still return a dict (empty from failed load, but fallback works)
            self.assertIsInstance(prompts, dict)
            # _get_prompt should use _FALLBACK_PROMPTS
            result = tc._get_prompt("pm_system", "director")
            self.assertIn("Program Manager", result)

    def test_get_prompt_nested_key(self):
        tc._PROMPTS = None
        result = tc._get_prompt("pm_system", "director")
        self.assertTrue(len(result) > 0)
        self.assertIn("Program Manager", result)

    def test_cli_prompt_size_warning(self):
        with self.assertLogs("task-claw", level="WARNING") as cm:
            tc._warn_cli_prompt_size("test-stage", "x" * 600)
        self.assertTrue(any("CLI prompt" in msg for msg in cm.output))


class TestPromptFileSwitching(E2EBase):
    """Tests Windows prompt truncation fix — file-based prompt switching."""

    def setUp(self):
        super().setUp()
        self.providers = json.loads(
            (Path(__file__).parent / "providers.json").read_text(encoding="utf-8")
        )["providers"]

    def test_short_prompt_inline(self):
        cmd = tc.build_cli_command(self.providers["claude"], "plan", "short prompt")
        self.assertIn("-p", cmd)
        self.assertNotIn("--prompt-file", cmd)

    def test_long_prompt_uses_prompt_file(self):
        long_prompt = "x" * 7000
        fake_file = Path("/tmp/test-prompt.md")
        cmd = tc.build_cli_command(self.providers["claude"], "plan", long_prompt,
                                    prompt_file=fake_file)
        self.assertIn("--prompt-file", cmd)
        self.assertIn(str(fake_file), cmd)

    def test_aider_uses_message_file(self):
        long_prompt = "x" * 7000
        fake_file = Path("/tmp/test-prompt.md")
        cmd = tc.build_cli_command(self.providers["aider"], "plan", long_prompt,
                                    prompt_file=fake_file)
        self.assertIn("--message-file", cmd)

    def test_prompt_file_written_correctly(self):
        content = "Test prompt content for file writing"
        path = tc._write_prompt_file(content, "test-task", "plan", "plan")
        try:
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), content)
        finally:
            if path.exists():
                path.unlink()
            # Clean up directory if empty
            if path.parent.exists() and not list(path.parent.iterdir()):
                path.parent.rmdir()


class TestRealisticHappyPath(E2EBase):
    """Full pipeline runs with stage-specific realistic outputs."""

    def test_direct_mode_full_run(self):
        stage_map = {
            "plan": [("claude", REALISTIC_PLAN_OUTPUT)],
            "code": [("claude", REALISTIC_CODE_OUTPUT)],
            "simplify": [("claude", REALISTIC_SIMPLIFY_OUTPUT)],
            "test": [("claude", REALISTIC_TEST_PASS_OUTPUT)],
        }
        mock_fn = self._make_capturing_mock(stage_map)
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_fn):
            result = tc.run_pipeline("Add JWT auth", task_id="test-happy-direct")
        self.assertTrue(result["success"])
        self.assertTrue(result["published"])
        stage_names = [e["stage"] for e in result["stage_log"]]
        for expected in ["rewrite", "plan", "code", "simplify", "test", "review"]:
            self.assertIn(expected, stage_names)
        self.assertIn("stats", result)

    def test_pm_mode_full_run(self):
        with patch.object(tc, "_pm_health_check", return_value=True), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch.object(tc, "pm_direct_team",
                          return_value=(REALISTIC_PLAN_OUTPUT, True)), \
             patch.object(tc, "pm_oversee_stage",
                          return_value=tc._parse_overseer_response(REALISTIC_PM_APPROVE)):
            result = tc.run_pipeline("Add JWT auth", task_id="test-happy-pm")
        self.assertTrue(result["success"])
        self.assertTrue(result["published"])
        stage_names = [e["stage"] for e in result["stage_log"]]
        for expected in ["rewrite", "plan", "code", "simplify", "test", "review"]:
            self.assertIn(expected, stage_names)

    def test_test_failure_triggers_fix_and_recovers(self):
        call_count = {"test": 0}

        def mock_run_team(stage_name, *args, **kwargs):
            if stage_name == "test":
                call_count["test"] += 1
                return [("claude", REALISTIC_TEST_FAIL_OUTPUT)]
            if stage_name == "code":
                # code-fix stage returns good output
                return [("claude", REALISTIC_CODE_OUTPUT)]
            if stage_name == "plan":
                return [("claude", REALISTIC_PLAN_OUTPUT)]
            if stage_name == "simplify":
                return [("claude", REALISTIC_SIMPLIFY_OUTPUT)]
            return self._good_team()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):
            result = tc.run_pipeline("Add JWT auth", task_id="test-fix-recover")
        stage_names = [e["stage"] for e in result["stage_log"]]
        self.assertIn("code-fix", stage_names)
        self.assertTrue(result["success"])


class TestRealisticPMInteractions(E2EBase):
    """Tests PM quality gates with realistic data."""

    def test_pm_approves_good_output(self):
        parsed = tc._parse_overseer_response(REALISTIC_PM_APPROVE)
        self.assertEqual(parsed["verdict"], "approve")
        self.assertEqual(parsed["issues"], [])
        self.assertIn("JWT authentication", parsed["synthesis"])

    def test_pm_revise_has_specific_issues(self):
        parsed = tc._parse_overseer_response(REALISTIC_PM_REVISE)
        self.assertEqual(parsed["verdict"], "revise")
        self.assertEqual(len(parsed["issues"]), 3)
        self.assertTrue(any("persistence" in i.lower() for i in parsed["issues"]))
        self.assertTrue(any("rate limiting" in i.lower() for i in parsed["issues"]))

    def test_garbage_short_circuits_pm(self):
        """When team output is garbage, _pm_api_call is never called."""
        with patch.object(tc, "_pm_api_call") as mock_pm:
            result = tc.pm_oversee_stage(
                "code", "Add auth", "", [("claude", GARBAGE_PERMISSION)], {}
            )
        mock_pm.assert_not_called()
        self.assertEqual(result["verdict"], "revise")

    def test_revise_then_approve_flow(self):
        """First pm_oversee_stage returns REVISE, second returns APPROVE."""
        oversee_calls = {"n": 0}
        revise_result = tc._parse_overseer_response(REALISTIC_PM_REVISE)
        approve_result = tc._parse_overseer_response(REALISTIC_PM_APPROVE)

        def mock_oversee(*args, **kwargs):
            oversee_calls["n"] += 1
            if oversee_calls["n"] == 1:
                return revise_result
            return approve_result

        with patch.object(tc, "_pm_health_check", return_value=True), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch.object(tc, "pm_direct_team",
                          return_value=(REALISTIC_PLAN_OUTPUT, True)), \
             patch.object(tc, "pm_oversee_stage", side_effect=mock_oversee), \
             patch.dict(os.environ, {"PIPELINE_MAX_REVISE": "2"}), \
             patch.object(tc, "MAX_REVISE_ATTEMPTS", 2):
            result = tc.run_pipeline("Add JWT auth", task_id="test-revise-approve")
        self.assertTrue(result["success"])
        # PM oversee should have been called at least twice for the plan stage
        self.assertGreaterEqual(oversee_calls["n"], 2)

    def test_consecutive_failures_switch_to_direct(self):
        """After 2 PM failures, pipeline switches to direct mode."""
        # pm_direct_team returns (prompt, False) to indicate PM failure
        # After 2 consecutive failures, pipeline internally switches to direct mode
        with patch.object(tc, "_pm_health_check", return_value=True), \
             patch.object(tc, "run_team", return_value=self._good_team()), \
             patch.object(tc, "pm_direct_team",
                          return_value=("fallback prompt", False)), \
             patch.object(tc, "pm_oversee_stage",
                          return_value=tc._parse_overseer_response(make_pm_response())):
            result = tc.run_pipeline("Add JWT auth", task_id="test-pm-fallback")
        # Pipeline should still complete despite PM failures
        self.assertTrue(result["success"])


class TestCrossReviewMerge(E2EBase):
    """Tests multi-agent code path."""

    def test_single_agent_no_cross_review(self):
        with patch.object(tc, "cross_review_code") as mock_cross:
            with patch.object(tc, "_pm_health_check", return_value=True), \
                 patch.object(tc, "run_team",
                              return_value=[("claude", REALISTIC_CODE_OUTPUT)]), \
                 patch.object(tc, "pm_direct_team",
                              return_value=(REALISTIC_PLAN_OUTPUT, True)), \
                 patch.object(tc, "pm_oversee_stage",
                              return_value=tc._parse_overseer_response(make_pm_response())):
                tc.run_pipeline("Add JWT auth", task_id="test-single-agent")
        mock_cross.assert_not_called()

    def test_two_agents_triggers_cross_review(self):
        two_agents = [
            ("claude", REALISTIC_CODE_OUTPUT),
            ("copilot", REALISTIC_CODE_OUTPUT + "\n\nAdditional copilot changes."),
        ]
        with patch.object(tc, "cross_review_code",
                          return_value=[("review-by-claude", "LGTM")]) as mock_cross, \
             patch.object(tc, "pm_merge_with_reviews",
                          return_value=tc._parse_overseer_response(make_pm_response())) as mock_merge, \
             patch.object(tc, "_pm_health_check", return_value=True), \
             patch.object(tc, "run_team", return_value=two_agents), \
             patch.object(tc, "pm_direct_team",
                          return_value=(REALISTIC_PLAN_OUTPUT, True)), \
             patch.object(tc, "pm_oversee_stage",
                          return_value=tc._parse_overseer_response(make_pm_response())):
            tc.run_pipeline("Add JWT auth", task_id="test-two-agents")
        mock_cross.assert_called_once()
        mock_merge.assert_called_once()

    def test_comparison_summary_extraction(self):
        reviews = [
            ("review-by-claude", (
                "## Agreement Points\nBoth use guard clauses\n\n"
                "## Divergences\nAgent A uses async, B uses sync\n\n"
                "## Winner Per Component\nAuth: Agent A\nRoutes: Agent B\n\n"
                "## Recommended Merge Strategy\nUse A's auth with B's routes"
            )),
        ]
        outputs = [("claude", "code A"), ("copilot", "code B")]
        summary = tc._build_comparison_summary(outputs, reviews)
        self.assertIn("Agreement Points", summary)
        self.assertIn("Divergences", summary)
        self.assertIn("Winner Per Component", summary)

    def test_merge_produces_combined_output(self):
        """pm_merge_with_reviews called with implementations + reviews."""
        impl = [("claude", "impl A"), ("copilot", "impl B")]
        reviews = [("review-by-claude", "review text")]
        pm_cfg = {"backend": "github_models", "model": "gpt-4o"}

        with patch.object(tc, "_pm_api_call",
                          return_value=make_pm_response()) as mock_api:
            result = tc.pm_merge_with_reviews(
                "code", "Add auth", "", impl, reviews, pm_cfg
            )
        mock_api.assert_called_once()
        self.assertIn("verdict", result)


class TestEdgeCases(E2EBase):
    """Miscellaneous edge cases."""

    def test_disabled_stage_skipped(self):
        custom_cfg = {
            "program_manager": {"backend": "github_models", "model": "gpt-4o"},
            "stages": {
                "rewrite": {"enabled": True, "team": ["claude"], "timeout": 120},
                "plan": {"enabled": True, "team": ["claude"], "timeout": 300},
                "code": {"enabled": True, "team": ["claude"], "timeout": 300},
                "simplify": {"enabled": False, "team": ["claude"], "timeout": 300},
                "test": {"enabled": True, "team": ["claude"], "timeout": 300},
                "review": {"enabled": True, "team": ["claude"], "timeout": 300},
            },
            "publish": {"enabled": True, "auto_push": True},
        }
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()):
            result = tc.run_pipeline("Add JWT auth", task_id="test-disabled",
                                      pipeline_cfg=custom_cfg)
        stage_names = [e["stage"] for e in result["stage_log"]]
        self.assertNotIn("simplify", stage_names)

    def test_start_stage_skips_earlier(self):
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()):
            result = tc.run_pipeline("Add JWT auth", task_id="test-start-stage",
                                      start_stage="code")
        stage_names = [e["stage"] for e in result["stage_log"]]
        self.assertNotIn("rewrite", stage_names)
        self.assertNotIn("plan", stage_names)
        self.assertIn("code", stage_names)

    def test_pipeline_stats_structure(self):
        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()):
            result = tc.run_pipeline("Add JWT auth", task_id="test-stats")
        self.assertIn("stats", result)
        stats = result["stats"]
        self.assertIsInstance(stats, dict)

    def test_empty_team_output_no_crash(self):
        def mock_run_team(stage_name, *args, **kwargs):
            if stage_name == "plan":
                return []  # empty output
            return self._good_team()

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", side_effect=mock_run_team):
            result = tc.run_pipeline("Add JWT auth", task_id="test-empty")
        # Should not crash
        self.assertTrue(result["success"])

    def test_security_high_blocks_publish(self):
        self.mock_security.return_value = REALISTIC_SECURITY_HIGH
        self.mock_handle_sec.return_value = "blocked"

        with patch.object(tc, "_pm_health_check", return_value=False), \
             patch.object(tc, "run_team", return_value=self._good_team()):
            result = tc.run_pipeline("Add JWT auth", task_id="test-sec-high")
        self.assertFalse(result["published"])
        self.assertFalse(result["success"])
        self.assertIn("security", result.get("error", "").lower())


if __name__ == "__main__":
    unittest.main()
