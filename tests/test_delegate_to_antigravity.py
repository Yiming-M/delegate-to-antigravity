from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "delegate_to_antigravity.py"


def json_lines(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


# Event shapes below follow agy 1.5.12's stream-json output.
def init_event(conversation_id: str = "conv-1", **init: object) -> dict[str, object]:
    return {"event": "init", "conversation_id": conversation_id,
            "init": {"cwd": "/repo", "permission_mode": "request-review", **init}}


def tool_event(name: str, state: str = "DONE", *, error: str | None = None) -> dict[str, object]:
    info: dict[str, object] = {"name": name, "parameters": {"Path": "/repo/calc.py"}}
    if error:
        info["error"] = {"type": "TOOL_ERROR", "message": error}
    return {"event": "step_update", "step_update": {
        "conversation_id": "conv-1", "state": state, "step_type": "tool",
        "tool_name": name, "tool_info": info}}


def result_event(response: str = "Done.", *, status: str = "SUCCESS",
                 conversation_id: str = "conv-1", **extra: object) -> dict[str, object]:
    return {"event": "result", "result": {
        "conversation_id": conversation_id, "status": status, "response": response,
        "duration_seconds": 2.5, "num_turns": 1, "usage": {"total_tokens": 42}, **extra}}


def conversation_log(home: Path, conversation_id: str = "conv-1") -> Path:
    return home / ".gemini/antigravity-cli/brain" / conversation_id / ".system_generated/logs/transcript_full.jsonl"


def model_calls(*models: str) -> str:
    # Shape of agy 1.5.15's conversation log entries for model calls.
    return json_lines(*({"type": "PLANNER_RESPONSE", "model": model, "cache_read_tokens": 0} for model in models))


class FakeAgy:
    """An executable that records its arguments, then replays canned output."""

    def __init__(self, root: Path, stdout: str, *, stderr: str = "", code: int = 0,
                 served: tuple[str, ...] = ()) -> None:
        self.path = root / "agy"
        self.record = root / "record.json"
        (root / "stdout.txt").write_text(stdout, encoding="utf-8")
        (root / "stderr.txt").write_text(stderr, encoding="utf-8")
        log = conversation_log(root)
        self.path.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, sys
            from pathlib import Path
            root = Path({str(root)!r})
            Path({str(self.record)!r}).write_text(json.dumps(sys.argv[1:]))
            served = {model_calls(*served)!r}
            if served:
                log = Path({str(log)!r})
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a") as stream:
                    stream.write(served)
            sys.stdout.write((root / "stdout.txt").read_text())
            sys.stderr.write((root / "stderr.txt").read_text())
            sys.exit({code})
            """), encoding="utf-8")
        self.path.chmod(0o755)

    def argv(self) -> list[str]:
        return json.loads(self.record.read_text(encoding="utf-8"))


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


class DelegateToAntigravityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transcript = self.root / "call.trace.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_helper(self, fake: FakeAgy, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--agy", str(fake.path), "--cwd", str(self.root),
             "--prompt", "/review this", "--transcript", str(self.transcript), *extra],
            # A temporary home keeps the user's own route file out of these native-mode tests.
            env={**os.environ, "HOME": str(self.root)}, capture_output=True, text=True, check=False)

    def transcript_data(self) -> dict[str, object]:
        return json.loads(self.transcript.read_text(encoding="utf-8"))

    def test_read_only_call_denies_mutating_tools_and_returns_response(self) -> None:
        fake = FakeAgy(self.root, json_lines(init_event(), tool_event("read_file"), result_event("Looks fine.")))
        completed = self.run_helper(fake)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "Looks fine.\n")
        argv = fake.argv()
        self.assertEqual(argv[-1], "--print=/review this")
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn("--sandbox", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        deny = json.loads(option(argv, "--settings"))["permissions"]["deny"]
        self.assertEqual(set(deny), {"write_file(*)", "command(*)", "mcp(*)", "execute_url(*)"})
        data = self.transcript_data()
        self.assertEqual(data["conversation_id"], "conv-1")
        self.assertEqual(data["tool_activity"][0]["name"], "read_file")
        self.assertEqual(self.transcript.stat().st_mode & 0o777, 0o600)

    def test_workspace_write_passes_only_requested_allow_rules(self) -> None:
        fake = FakeAgy(self.root, json_lines(result_event()))
        completed = self.run_helper(fake, "--access", "workspace-write", "--allow", "command(pytest -q)")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        settings = json.loads(option(fake.argv(), "--settings"))
        self.assertEqual(settings, {"permissions": {"allow": ["command(pytest -q)"]}})

        fake = FakeAgy(self.root, json_lines(result_event()))
        self.run_helper(fake, "--access", "workspace-write")
        self.assertNotIn("--settings", fake.argv())

    def test_allow_rules_are_refused_for_read_only_calls(self) -> None:
        fake = FakeAgy(self.root, "")
        completed = self.run_helper(fake, "--allow", "command(ls)")
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(fake.record.exists())

    def test_resume_passes_conversation_and_rejects_a_different_one(self) -> None:
        fake = FakeAgy(self.root, json_lines(result_event(conversation_id="conv-2")))
        completed = self.run_helper(fake, "--conversation", "conv-1")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("instead of conv-1", completed.stderr)
        self.assertEqual(option(fake.argv(), "--conversation"), "conv-1")

    def resume_from(self, *models: str) -> None:
        log = conversation_log(self.root)
        log.parent.mkdir(parents=True)
        log.write_text(model_calls(*models), encoding="utf-8")

    def test_resume_warns_before_a_model_change(self) -> None:
        self.resume_from("gemini-3.8-flash-low")
        fake = FakeAgy(self.root, json_lines(result_event()), served=("gemini-3.8-flash-medium",))
        completed = self.run_helper(fake, "--conversation", "conv-1", "--model", "gemini-3.8-flash-medium")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr.count("warning: resumed conversation drift"), 1)
        self.assertIn("differs from the conversation's model 'gemini-3.8-flash-low'", completed.stderr)
        data = self.transcript_data()
        self.assertEqual(data["served_models"], ["gemini-3.8-flash-medium"])
        self.assertEqual(data["prior_model"], "gemini-3.8-flash-low")

    def test_resume_without_model_reports_agy_default_switch(self) -> None:
        self.resume_from("gemini-3.8-flash-low", "gemini-3.8-flash-low")
        fake = FakeAgy(self.root, json_lines(result_event()), served=("gemini-4-neon-high", "gemini-4-neon-high"))
        completed = self.run_helper(fake, "--conversation", "conv-1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("agy served gemini-4-neon-high instead of the conversation's model", completed.stderr)
        self.assertIn("served_model=gemini-4-neon-high ", completed.stderr)
        self.assertEqual(len(self.transcript_data()["resume_warnings"]), 1)

    def test_resume_with_same_model_reports_served_model_and_cache_without_warning(self) -> None:
        self.resume_from("gemini-3.8-flash-low")
        usage = {"input_tokens": 2267, "cache_read_tokens": 9885}
        fake = FakeAgy(self.root, json_lines(
            {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE", "usage": usage}},
            result_event()), served=("gemini-3.8-flash-low",))
        completed = self.run_helper(fake, "--conversation", "conv-1", "--model", "gemini-3.8-flash-low",
                                    "--access", "workspace-write", "--allow", "command(pytest -q)")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("warning", completed.stderr)
        self.assertIn("served_model=gemini-3.8-flash-low ", completed.stderr)
        self.assertIn("first_call_cache_read=9885", completed.stderr)
        self.assertEqual(self.transcript_data()["first_call_usage"], usage)

    def test_denied_actions_exit_4_although_agy_reports_success(self) -> None:
        fake = FakeAgy(self.root, json_lines(result_event(
            "I'll start by listing files.", denied_actions=[{"action": "command", "display_name": "Bash"}])))
        completed = self.run_helper(fake)
        self.assertEqual(completed.returncode, 4)
        self.assertIn("do not claim full completion", completed.stderr)
        self.assertEqual(self.transcript_data()["denials"], [{"action": "command", "display_name": "Bash"}])

    def test_tool_blocked_by_deny_rule_exits_4(self) -> None:
        fake = FakeAgy(self.root, json_lines(
            tool_event("edit_file", "ERROR", error="permission check failed for write_file \"/repo/calc.py\": "
                                                   "Permission denied for write_file(/repo/calc.py)."),
            tool_event("read_file", "ERROR", error="file not found"),
            result_event("Neither edit succeeded.")))
        completed = self.run_helper(fake)
        self.assertEqual(completed.returncode, 4)
        denials = self.transcript_data()["denials"]
        self.assertEqual([denial["name"] for denial in denials], ["edit_file"])

    def test_error_status_fails_and_reports_agy_error(self) -> None:
        fake = FakeAgy(self.root, json_lines(result_event(
            "", status="ERROR", conversation_id="", error="invalid model selection")))
        completed = self.run_helper(fake, "--model", "no-such-model")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("agy error: invalid model selection", completed.stderr)

    def test_missing_result_event_is_an_error(self) -> None:
        fake = FakeAgy(self.root, json_lines(init_event()))
        completed = self.run_helper(fake)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("no final result event", completed.stderr)

    def test_authentication_failure_points_to_interactive_sign_in(self) -> None:
        fake = FakeAgy(self.root, "", stderr="Error: authentication required\n", code=1)
        completed = self.run_helper(fake)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("run `agy` once in a terminal", completed.stderr)

    def test_dry_run_does_not_start_agy(self) -> None:
        fake = FakeAgy(self.root, "")
        completed = self.run_helper(fake, "--dry-run", "--model", "gemini-3.8-flash-low", "--effort", "low")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        command = json.loads(completed.stdout)["command"]
        self.assertEqual(option(command, "--model"), "gemini-3.8-flash-low")
        self.assertEqual(option(command, "--effort"), "low")
        self.assertFalse(fake.record.exists())


if __name__ == "__main__":
    unittest.main()
