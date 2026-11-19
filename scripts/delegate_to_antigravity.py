#!/usr/bin/env python3
"""Delegate a task to the Antigravity CLI (agy) and resume it by conversation ID."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Where Antigravity's installer puts agy when it is not on PATH.
INSTALLER_BIN = Path.home() / ".local/bin/agy"

# agy's modes do not stop headless edits, and an allowed command can write the
# workspace even inside the terminal sandbox, so read-only access denies every
# tool that can change something.
READ_ONLY_DENY = ["write_file(*)", "command(*)", "mcp(*)", "execute_url(*)"]


@dataclass(frozen=True)
class ParsedAgyOutput:
    init: dict[str, Any]
    conversation_id: str | None
    status: str | None
    response: str
    error: Any
    usage: dict[str, Any] | None
    num_turns: int | None
    duration_seconds: float | None
    tool_activity: list[dict[str, Any]]
    denials: list[Any]


def find_agy(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    candidate = os.environ.get("DELEGATE_AGY_BIN") or shutil.which("agy")
    if candidate:
        return candidate
    return str(INSTALLER_BIN) if INSTALLER_BIN.is_file() else None


def session_settings(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.access == "read-only":
        return {"permissions": {"deny": READ_ONLY_DENY}}
    if args.allow:
        return {"permissions": {"allow": args.allow}}
    return None


def build_command(args: argparse.Namespace, agy: str, prompt: str) -> list[str]:
    command = [
        agy, "--output-format", "stream-json",
        # The prompt is task data: never expand a leading "/" into a slash command or skill.
        "--disable-slash-commands",
        # Any shell command that is allowed to run stays in the terminal sandbox.
        "--sandbox",
    ]
    settings = session_settings(args)
    if settings:
        # Applies to this call only; agy never writes it to the user's settings.json.
        command.extend(["--settings", json.dumps(settings)])
    for name in ("model", "effort", "conversation"):
        value = getattr(args, name)
        if value is not None:
            command.extend(["--" + name, value])
    for directory in args.add_dir:
        command.extend(["--add-dir", directory])
    # agy reads the prompt only from the --print value, never from stdin.
    command.append("--print=" + prompt)
    return command


def parse_agy_stream(stdout: str) -> ParsedAgyOutput:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"stream-json line {line_number} is invalid JSON: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"stream-json line {line_number} is not a JSON object")
        events.append(event)
    if not events:
        raise ValueError("agy returned an empty stream-json response")
    results = [event["result"] for event in events
               if event.get("event") == "result" and isinstance(event.get("result"), dict)]
    if not results:
        raise ValueError("agy stream-json response has no final result event")
    result = results[-1]
    response = result.get("response")
    if response is None:
        response = ""
    elif not isinstance(response, str):
        raise ValueError("agy final response field is not text")
    init_event = next((event for event in events if event.get("event") == "init"), {})
    tools = [event["step_update"] for event in events
             if event.get("event") == "step_update" and isinstance(event.get("step_update"), dict)
             and event["step_update"].get("step_type") == "tool"
             and event["step_update"].get("state") in ("DONE", "ERROR")]
    tool_activity = [{"state": step["state"], **(step.get("tool_info") or {"name": step.get("tool_name")})}
                     for step in tools]
    # A tool that asked for approval is listed in denied_actions; one a deny rule
    # blocked only shows up as a failed tool step.
    denials: list[Any] = list(result.get("denied_actions") or [])
    denials += [tool for tool in tool_activity if tool["state"] == "ERROR"
                and "permission check failed" in json.dumps(tool.get("error"))]
    return ParsedAgyOutput(
        init=init_event.get("init") if isinstance(init_event.get("init"), dict) else {},
        conversation_id=result.get("conversation_id") or init_event.get("conversation_id") or None,
        status=result.get("status") if isinstance(result.get("status"), str) else None,
        response=response,
        error=result.get("error") or None,
        usage=result.get("usage") if isinstance(result.get("usage"), dict) else None,
        num_turns=result.get("num_turns"),
        duration_seconds=result.get("duration_seconds"),
        tool_activity=tool_activity,
        denials=denials,
    )


def write_text(path_value: str | None, text: str) -> None:
    if path_value:
        path = Path(path_value).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Prompts, responses and tool output stay readable by the owner only.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)


def run_agy(command: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    # A tool command may outlive agy. Terminate the whole process group on timeout.
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, cwd=cwd,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            return 124, stdout, stderr + f"\nagy timed out after {timeout}s."
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    prompt_source = parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt", help="Task or follow-up text; otherwise read stdin")
    prompt_source.add_argument("--prompt-file", help="UTF-8 task or follow-up file")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--agy", help="agy executable; default: $DELEGATE_AGY_BIN, PATH, then ~/.local/bin/agy")
    parser.add_argument("--model", help="Exact model slug from `agy models`; omit to use agy's default")
    parser.add_argument("--effort", help="Exact reasoning effort; omit to use agy's default")
    parser.add_argument("--conversation", help="Conversation ID returned by an earlier call, to resume it")
    parser.add_argument("--access", choices=["read-only", "workspace-write"], default="read-only",
                        help="read-only (default) denies edits, commands, MCP tools and browser actions")
    parser.add_argument("--allow", action="append", default=[], metavar="RULE",
                        help="agy permission rule allowed for this call, e.g. 'command(pytest -q)'; "
                             "workspace-write only; repeatable")
    parser.add_argument("--add-dir", action="append", default=[], help="Additional workspace directory; repeatable")
    parser.add_argument("--out", help="Write successful response text")
    parser.add_argument("--transcript", help="Write conversation ID, audit, response and raw agy events")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true", help="Preview without starting agy")
    args = parser.parse_args()

    agy = find_agy(args.agy)
    if not agy:
        parser.error("agy was not found. Install the Antigravity CLI or pass --agy.")
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        parser.error(f"cwd is not a directory: {cwd}")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if args.conversation is not None and not args.conversation.strip():
        parser.error("--conversation requires an explicit conversation ID")
    if args.allow and args.access == "read-only":
        parser.error("--allow requires --access workspace-write: an allowed command can write the workspace")
    try:
        prompt = (Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
                  if args.prompt_file else args.prompt if args.prompt is not None else sys.stdin.read())
    except OSError as exc:
        parser.error(str(exc))
    if not prompt.strip():
        parser.error("prompt is empty")
    command = build_command(args, agy, prompt)
    if args.dry_run:
        print(json.dumps({"cwd": str(cwd), "command": command[:-1] + ["--print=<prompt>"],
                          "prompt_chars": len(prompt)}, indent=2))
        return
    if not os.access(agy, os.X_OK):
        parser.error(f"agy is missing or not executable: {agy}")

    try:
        returncode, stdout, stderr = run_agy(command, cwd, args.timeout)
    except OSError as exc:
        returncode, stdout, stderr = 1, "", str(exc)
    parsed = None
    parse_error = None
    try:
        parsed = parse_agy_stream(stdout)
    except ValueError as exc:
        parse_error = str(exc)
    conversation_id = parsed.conversation_id if parsed else None
    resume_mismatch = bool(args.conversation and conversation_id and conversation_id != args.conversation)

    # A zero exit is only transport success; the caller must verify the task.
    if returncode:
        exit_code = returncode if 1 <= returncode <= 255 else 1
    elif parsed is None or parsed.status != "SUCCESS" or parsed.error or resume_mismatch:
        exit_code = 1
    elif parsed.denials:
        exit_code = 4
    elif not parsed.response:
        exit_code = 1
    else:
        exit_code = 0

    # agy names the model only when one is requested, and never reports the served model.
    reported_model = parsed.init.get("model") if parsed else None
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "cwd": str(cwd),
        "command": command[:-1], "prompt": prompt, "conversation_id": conversation_id,
        "resumed_from": args.conversation, "access": args.access, "allow": args.allow,
        "requested_model": args.model, "reported_model": reported_model, "requested_effort": args.effort,
        "returncode": exit_code, "agy_returncode": returncode,
        "status": parsed.status if parsed else None, "response": parsed.response if parsed else None,
        "error": parsed.error if parsed else None, "denials": parsed.denials if parsed else [],
        "usage": parsed.usage if parsed else None, "num_turns": parsed.num_turns if parsed else None,
        "duration_seconds": parsed.duration_seconds if parsed else None,
        "tool_activity": parsed.tool_activity if parsed else [],
        "stderr": stderr, "parse_error": parse_error, "raw_stdout": stdout,
    }
    write_text(args.transcript, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if stderr:
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
    usage = parsed.usage if parsed and parsed.usage else {}
    print(
        f"Antigravity audit: requested_model={args.model or 'agy default'} "
        f"reported_model={reported_model or 'unknown'} effort={args.effort or 'agy default'} "
        f"access={args.access} conversation={conversation_id or 'unknown'} "
        f"status={parsed.status if parsed else 'unknown'} turns={parsed.num_turns if parsed else 'unknown'} "
        f"tokens={usage.get('total_tokens', 'unknown')}", file=sys.stderr,
    )
    if exit_code:
        if parse_error:
            print(f"error: unable to parse agy stream-json output: {parse_error}", file=sys.stderr)
        if re.search(r"authentication required", stderr + json.dumps(parsed.error if parsed else None),
                     re.IGNORECASE):
            print("error: agy is not signed in; run `agy` once in a terminal to sign in.", file=sys.stderr)
        if resume_mismatch:
            print(f"error: agy returned conversation {conversation_id} instead of {args.conversation}.",
                  file=sys.stderr)
        if parsed and parsed.error:
            print(f"agy error: {parsed.error}", file=sys.stderr)
        if parsed and parsed.denials:
            print("needs review: agy was denied tool permissions; do not claim full completion.", file=sys.stderr)
            print(json.dumps(parsed.denials, ensure_ascii=False), file=sys.stderr)
        if parsed and parsed.response:
            print(f"agy error/partial response: {parsed.response}", file=sys.stderr)
        print(f"error: agy call incomplete (agy exit={returncode}, wrapper exit={exit_code}).", file=sys.stderr)
        raise SystemExit(exit_code)
    write_text(args.out, parsed.response)
    print(parsed.response, end="" if parsed.response.endswith("\n") else "\n")


if __name__ == "__main__":
    main()
