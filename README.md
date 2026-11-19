# Delegate to Antigravity

A skill for Claude Code and Codex that hands a task to Google's Antigravity agent through the Antigravity CLI (`agy`), reviews the result, and sends feedback to the same Antigravity conversation. Give Antigravity a project directory, the access it may use, and the checks it should run. The calling agent can then inspect the changes and verify the result.

The skill calls the `agy` executable and uses its existing Google sign-in and Antigravity plan. It replaces Gemini CLI, which stopped serving personal Google accounts, including Google AI Pro and Ultra, on 18 June 2026.

For the other directions, see [delegate-to-codex](https://github.com/Yiming-M/delegate-to-codex) and [delegate-to-claude](https://github.com/Yiming-M/delegate-to-claude).

## Install

You'll need Claude Code or Codex, Python 3.10 or later, and the [Antigravity CLI](https://antigravity.google/docs/cli/reference) on macOS or Linux.

1. Install `agy` and sign in once interactively. Headless calls reuse that sign-in. [Antigravity CLI headless mode](https://antigravity.google/docs/cli/headless/)

   ```bash
   brew install --cask antigravity-cli
   agy
   ```

2. From the root of this repository, copy the skill into your personal skills directory.

   ```bash
   # Claude Code
   skill_dir="$HOME/.claude/skills/delegate-to-antigravity"
   # Codex
   # skill_dir="$HOME/.agents/skills/delegate-to-antigravity"
   mkdir -p "$skill_dir"
   cp -R SKILL.md agents scripts "$skill_dir/"
   ```

3. Ask for Gemini in plain words, with a request such as this one.

   > Ask Gemini for a second opinion on the input validation in this project. Keep it read-only and tell me where the two of you disagree.

## Run from the terminal

From the repository root, make a read-only request and save its conversation ID in a private transcript.

```bash
audit_dir=$(mktemp -d)
python3 scripts/delegate_to_antigravity.py \
  --cwd /path/to/project --access read-only \
  --prompt 'Review input validation and cite relevant files. Use your file tools; you cannot run shell commands.' \
  --transcript "$audit_dir/first.trace.json"
```

Use the reported conversation ID for a follow-up from the same working directory.

```bash
python3 scripts/delegate_to_antigravity.py \
  --cwd /path/to/project --access read-only --conversation CONVERSATION_ID \
  --prompt 'Check whether those findings are covered by existing tests.' \
  --transcript "$audit_dir/follow-up.trace.json"
```

Read-only access denies file writes, shell commands, MCP tools and browser actions for that call. agy's own modes do not stop a headless call from editing files, and an allowed command can write the workspace even inside agy's sandbox, so the helper passes per-call permission rules instead. They are never saved to your `~/.gemini/antigravity-cli/settings.json`.

For implementation, use `--access workspace-write`, which lets agy edit files inside the working directory and any `--add-dir`. Allow the specific commands the task needs with `--allow`, for example `--allow 'command(/opt/homebrew/bin/python3 -m pytest -q)'`. Every call runs in agy's terminal sandbox, which limits command writes to the workspace and temporary directories and blocks network access except to domains allowed for the call, for example `--allow 'read_url(pypi.org)'`. On macOS, Apple's `/usr/bin/git` and `/usr/bin/python3` fail inside that sandbox, so name a Homebrew or virtual environment path.

agy reports success even when it was denied a tool. The helper exits 4 in that case so the caller does not mistake a partial run for a finished one. Use `--model` and `--effort` to pass an explicit choice (`agy models` lists the model slugs, most of which already name an effort level), and `--agy` to select an executable outside `PATH`. `--dry-run` previews the command without starting agy.

Keep transcripts private, since they contain task prompts and tool output.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use a stand-in `agy` executable that replays simulated events.
