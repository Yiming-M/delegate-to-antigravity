from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
HELPER = SCRIPTS / "delegate_to_antigravity.py"
KEY_TOOL = SCRIPTS / "gemini_api_key.py"
KEY = "AIzaTEST-not-a-real-key-0123456789abcd"
PLAN_OK = "Gemini Models\tWeekly Limit Remaining\t94%\t2026-11-30T20:05:55Z\n"
PLAN_EMPTY = "Gemini Models\tWeekly Limit Remaining\t0%\t2026-11-30T20:05:55Z\n"
SUB_QUOTA = "Error 429, Message: quota exhausted, Status: RESOURCE_EXHAUSTED"
# Observed from agy 1.5.16 with a prepaid Gemini API project.
API_DEPLETED = ("Error 402, Message: Your prepayment credits are depleted. Please go to AI Studio, "
                "Status: RESOURCE_EXHAUSTED")
API_PER_MINUTE = ("Error 429, Message: You exceeded your current quota. Generate content requests per "
                  "minute limit exceeded on Free Tier., Status: RESOURCE_EXHAUSTED")


class FakeAgy:
    """agy stand-in: `/usage` reports the credential in use; a task replays the scenario for it.

    scenario maps "subscription" or the API key value to {"error": ...} or {"response": ...}.
    """

    def __init__(self, root: Path, scenario: dict[str, dict[str, str]], plan: str = PLAN_OK,
                 honour_key: bool = True) -> None:
        self.path = root / "agy"
        self.log = root / "calls.jsonl"
        (root / "scenario.json").write_text(json.dumps({"scenario": scenario, "plan": plan,
                                                         "honour_key": honour_key}))
        self.path.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, sys
            from pathlib import Path
            config = json.loads(Path({str(root / "scenario.json")!r}).read_text())
            argv = sys.argv[1:]
            settings = json.loads(argv[argv.index("--settings") + 1]) if "--settings" in argv else {{}}
            key = os.environ.get("GEMINI_API_KEY")
            mode = key if key and settings.get("modelProvider") == "gemini" and config["honour_key"] \\
                else "subscription"
            prompt = next((a[len("--print="):] for a in argv if a.startswith("--print=")), None)
            conversation = argv[argv.index("--conversation") + 1] if "--conversation" in argv else "conv-1"
            with open({str(self.log)!r}, "a") as log:
                log.write(json.dumps({{"mode": "api" if mode != "subscription" else "subscription",
                                       "key_seen": key, "settings": settings, "usage": "/usage" in argv,
                                       "prompt": prompt, "conversation": conversation}}) + "\\n")
            if "/usage" in argv:
                if mode != "subscription":
                    print("error: /usage failed: not available with a Gemini API key", file=sys.stderr)
                    sys.exit(2)
                sys.stdout.write(config["plan"])
                sys.exit(0)
            outcome = config["scenario"][mode]
            result = {{"conversation_id": conversation, "num_turns": 1, "usage": {{"total_tokens": 9}}}}
            if "error" in outcome:
                result.update(status="ERROR", response="", error=outcome["error"])
            else:
                result.update(status="SUCCESS", response=outcome["response"])
            print(json.dumps({{"event": "init", "conversation_id": conversation, "init": {{}}}}))
            print(json.dumps({{"event": "result", "result": result}}))
            sys.exit(3 if "error" in outcome else 0)
            """), encoding="utf-8")
        self.path.chmod(0o755)

    def calls(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class AuthRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {**os.environ, "HOME": str(self.home)}
        self.env.pop("GEMINI_API_KEY", None)
        self.transcript = self.root / "call.trace.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def store_key(self, profile: str, value: str = KEY) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(KEY_TOOL), "--profile", profile, "--stdin"],
                              input=value, env=self.env, capture_output=True, text=True)

    def routes(self, sources: dict[str, object], order: list[str]) -> Path:
        path = self.root / "routes.json"
        path.write_text(json.dumps({"sources": sources, "order": order}))
        return path

    def run_helper(self, fake: FakeAgy, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), "--agy", str(fake.path), "--cwd", str(self.root),
             "--prompt", "Fix the bug.", "--transcript", str(self.transcript), *extra],
            env=self.env, capture_output=True, text=True, timeout=60)

    def subscription_then_api(self) -> Path:
        self.assertEqual(self.store_key("api1").returncode, 0)
        return self.routes({"plan": {"type": "subscription"}, "api1": {"type": "api", "key_profile": "api1"}},
                           ["plan", "api1"])

    def test_key_tool_stores_owner_only_and_strips_assignment(self) -> None:
        result = self.store_key("api1", f"export GEMINI_API_KEY='{KEY}'")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.home / ".config/delegate-to-antigravity/keys/api1"
        self.assertEqual(path.read_text().strip(), KEY)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        check = subprocess.run([sys.executable, str(KEY_TOOL), "--profile", "api1", "--check"],
                               env=self.env, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0)
        self.assertNotIn(KEY, check.stdout + check.stderr)

    def test_exhausted_plan_resumes_same_conversation_on_api_key(self) -> None:
        fake = FakeAgy(self.root, {"subscription": {"error": SUB_QUOTA}, KEY: {"response": "Fixed."}},
                       plan=PLAN_EMPTY)
        result = self.run_helper(fake, "--auth-config", str(self.subscription_then_api()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Fixed.")
        tasks = [call for call in fake.calls() if not call["usage"]]
        self.assertEqual([call["mode"] for call in tasks], ["subscription", "api"])
        self.assertIsNone(tasks[0]["key_seen"])
        self.assertEqual(tasks[1]["settings"]["modelProvider"], "gemini")
        self.assertEqual(tasks[1]["conversation"], "conv-1")
        self.assertIn("plan quota or API credit was exhausted", tasks[1]["prompt"])
        report = self.transcript.read_text()
        self.assertNotIn(KEY, report + result.stderr)
        audit = json.loads(report)["auth_audit"]
        self.assertEqual((audit["final"], audit["fallback"], audit["reason"]),
                         ("api1", True, "subscription_quota_exhausted"))

    def test_quota_error_with_plan_remaining_does_not_spend_api_credit(self) -> None:
        fake = FakeAgy(self.root, {"subscription": {"error": SUB_QUOTA}, KEY: {"response": "Fixed."}})
        result = self.run_helper(fake, "--auth-config", str(self.subscription_then_api()))
        self.assertEqual(result.returncode, 3)
        self.assertEqual([c["mode"] for c in fake.calls() if not c["usage"]], ["subscription"])

    def test_depleted_api_credit_moves_to_next_key_but_per_minute_limit_does_not(self) -> None:
        second = KEY.replace("TEST", "TWO2")
        self.store_key("api1")
        self.store_key("api2", second)
        routes = self.routes({"api1": {"type": "api", "key_profile": "api1"},
                              "api2": {"type": "api", "key_profile": "api2"}}, ["api1", "api2"])
        fake = FakeAgy(self.root, {KEY: {"error": API_DEPLETED}, second: {"response": "Fixed."}})
        self.assertEqual(self.run_helper(fake, "--auth-config", str(routes)).returncode, 0)
        fake.log.unlink()
        fake = FakeAgy(self.root, {KEY: {"error": API_PER_MINUTE}, second: {"response": "Fixed."}})
        self.assertEqual(self.run_helper(fake, "--auth-config", str(routes)).returncode, 3)
        self.assertEqual(len([c for c in fake.calls() if not c["usage"]]), 1)

    def test_non_gemini_model_is_not_handed_to_an_api_key(self) -> None:
        fake = FakeAgy(self.root, {"subscription": {"error": SUB_QUOTA}, KEY: {"response": "Fixed."}},
                       plan="Claude and GPT models\tWeekly Limit Remaining\t0%\t2026-12-01T17:09:41Z\n")
        result = self.run_helper(fake, "--auth-config", str(self.subscription_then_api()),
                                 "--model", "claude-opus-5-5")
        self.assertEqual(result.returncode, 3)
        audit = json.loads(self.transcript.read_text())["auth_audit"]
        self.assertEqual(audit["reason"], "subscription_quota_exhausted_model_not_served_by_api")

    def test_api_source_stops_before_the_task_when_agy_keeps_the_sign_in(self) -> None:
        fake = FakeAgy(self.root, {"subscription": {"response": "Fixed."}}, honour_key=False)
        routes = self.subscription_then_api()
        result = self.run_helper(fake, "--auth-config", str(routes), "--auth-source", "api1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("agy uses subscription", result.stderr)
        self.assertEqual([c for c in fake.calls() if not c["usage"]], [])

    def test_default_route_file_and_dry_run(self) -> None:
        routes = self.subscription_then_api()
        default = self.home / ".config/delegate-to-antigravity/routes.json"
        default.write_text(routes.read_text())
        fake = FakeAgy(self.root, {})
        result = self.run_helper(fake, "--dry-run")
        self.assertEqual(json.loads(result.stdout)["auth"], ["plan", "api1"])
        self.assertFalse(fake.log.exists())

    def test_route_file_validation(self) -> None:
        fake = FakeAgy(self.root, {})
        for sources, order in (
            ({"a": {"type": "subscription"}, "b": {"type": "subscription"}}, ["a", "b"]),
            ({"a": {"type": "api"}}, ["a"]),
            ({"a": {"type": "subscription", "config_dir": "/x"}}, ["a"]),
            ({"a": {"type": "subscription"}}, ["a", "a"]),
        ):
            result = self.run_helper(fake, "--auth-config", str(self.routes(sources, order)), "--dry-run")
            self.assertEqual(result.returncode, 2, (sources, order))


if __name__ == "__main__":
    unittest.main()
