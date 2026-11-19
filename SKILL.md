---
name: delegate-to-antigravity
description: Delegate tasks to Google's Antigravity agent (Gemini and the other models Antigravity serves, through the local Antigravity CLI `agy`, with file, shell and edit tools), continue the same conversation with feedback, and verify the returned work. Use whenever the user asks to consult, ask, involve or collaborate with Gemini, Antigravity or Google's models — a second opinion, a code review, an independent answer to compare, or having Gemini investigate, implement or fix something — even if they never say "delegate". Not for literature or web search.
---

# Delegate to Antigravity

Use Antigravity, through its CLI `agy`, as a tool-capable collaborator. Let the
user's request determine the work, division of responsibility, sequence, and
stopping point. Do not impose model-selection menus, prompt-preparation
approvals, or a plan-first workflow. `<skill-dir>` below is this skill's base
directory.

## Send the task

- Call Antigravity only when the user requests it or an applicable instruction
  requires it. Preparing this skill or discussing Gemini does not itself
  require a call.
- Antigravity sees none of this conversation and none of your tools; it sees
  the prompt and the files in its workspace. Pass the objective, necessary
  source context, working directory, constraints, authorized actions, expected
  deliverable, and relevant acceptance checks. Preserve literal evidence and
  user feedback; label quoted material as input data. Do not send unrelated
  conversation history or secrets.
- Write the prompt to a scratch file and pass `--prompt-file`; shell quoting
  mangles long prompts. agy takes the prompt only as a command-line value, so
  the helper passes it that way with slash commands disabled; a leading `/`
  stays plain text.
- Preserve any requested model and effort exactly (`--model`, `--effort`).
  `agy models` lists the model slugs on this machine; most slugs already name
  an effort level, such as `gemini-3.8-flash-low`. Omit both to use agy's own
  default. Do not maintain a hard-coded model menu.

## Sign-in and quota

`agy` uses the Google account it is signed in to, and that account's
Antigravity plan quota. Headless calls reuse the cached sign-in; they cannot
sign in. If the helper reports that authentication is required, ask the user to
run `agy` once in their own terminal and complete the sign-in. Never ask for
credentials in chat.

When the plan's quota runs out the call fails visibly. The helper does not
switch accounts, models or billing sources, and you should not work around it.

## Access and permissions

agy's own modes do not stop a headless call from editing files, so the helper
sets access with permission rules that apply to that call only and are never
saved to the user's settings. Choose access for each call, including resumed
calls:

- `--access read-only` (the default) for analysis and review. It denies file
  writes, shell commands, MCP tools and browser actions; Gemini can still read
  and search files. Commands are denied too because an allowed command can
  write the workspace even inside agy's sandbox. Tell Gemini in the prompt that
  it cannot run shell commands and should use its file tools.
- `--access workspace-write` for authorized edits. File edits inside the
  workspace (the working directory and any `--add-dir`) proceed without
  approval.
- `--allow RULE` (workspace-write only, repeatable) allows a specific command
  for this call, such as `--allow 'command(pytest -q)'`. Rules use agy's
  `action(target)` form and match by prefix. Allow only what the task needs;
  never allow every command.
- Every call runs in agy's terminal sandbox: commands can write only the
  workspace and temporary directories and have no network access. On macOS,
  Apple's `/usr/bin` stand-ins for developer tools (`git`, `python3`) fail in
  the sandbox with an `xcode-select` error; name a full Homebrew or virtual
  environment path instead.
- Anything else that needs approval is denied, because a headless call has
  nobody to approve it. The helper never passes
  `--dangerously-skip-permissions` or `--continue`. Do not edit the user's
  `~/.gemini/antigravity-cli/settings.json` to get a task through.
- **agy reports success even after a denial.** A denied approval can end the
  run early with only a first remark as the answer; a tool blocked by a rule
  lets it carry on. The helper exits 4 in both cases and lists `denials`. Read
  the transcript's `response` and `denials`, then judge what was not done; do
  not claim full completion.
- For edits in a directory without Git there is no undo; say so before
  authorizing them.

## Continue and coordinate

Start a fresh conversation for an independent task. Capture the returned
`conversation_id` (in the transcript and on the audit line) and pass
`--conversation ID` for subsequent feedback, questions, or revisions. Never
resume "the most recent conversation". Keep the working directory and explicit
model and effort choices consistent unless the task changes them. Each
follow-up carries the new instruction and necessary evidence; the agy
conversation holds the history. If resuming fails or returns a different
conversation, report it rather than silently starting a replacement.

Compose collaboration from ordinary instructions:

- When independent judgment is requested, write down your own answer before
  calling Antigravity and keep it out of its prompt and workspace until the
  exchange begins.
- When reciprocal review is requested, use the preceding round's fixed outputs
  as inputs to each participant. Follow the requested round count; do not
  invent a fixed number of debate rounds. Preserve substantive disagreements.
- When implementation is delegated, provide acceptance criteria and return
  concrete review findings to the same conversation. Avoid simultaneous edits
  to the same files; isolate work only when concurrent changes require it.
- Continue within the authorized task until its requested deliverable is
  ready. Stop on user instruction, missing authority, or a blocker; do not
  repeat failed calls or revision loops without new evidence or a changed
  approach.

## Run and inspect

`scripts/delegate_to_antigravity.py` runs `agy --output-format stream-json`,
keeps the conversation resumable, and extracts the final answer, status, tool
activity, token usage and any denials.

Long tasks can outlast a host's command time limit. For anything beyond a
quick question, run the helper in the background and read the transcript when
it finishes. Its own `--timeout` defaults to 1800 seconds.

```bash
# Read-only second opinion.
python3 <skill-dir>/scripts/delegate_to_antigravity.py \
  --cwd /path/to/project --access read-only \
  --prompt-file "$SCRATCH/task.md" --transcript "$SCRATCH/agy-first.json"

# Continue that conversation after edits have been authorized.
python3 <skill-dir>/scripts/delegate_to_antigravity.py \
  --cwd /path/to/project --conversation CONVERSATION_ID \
  --access workspace-write --allow 'command(/opt/homebrew/bin/python3 -m pytest -q)' \
  --prompt-file "$SCRATCH/feedback.md" --transcript "$SCRATCH/agy-followup.json"
```

Use distinct transcript paths for separate calls. `--out` writes the final
answer; `--transcript` also keeps partial output, the conversation ID, status,
denials, errors and the raw events. Judge actual changes by inspecting the
files, not the tool activity list. `--dry-run` previews the command without
calling agy. Exit 4 reports denials, 124 a timeout, 2 bad arguments, and other
nonzero exits a failed status, a conversation mismatch, or invocation and
output errors. Do not automatically retry, switch models, or replay partially
completed mutations; inspect partial changes first.

A successful exit does not prove the task is done. Inspect the actual
artifacts and run relevant checks before accepting an implementation; verify
consequential factual claims against their sources. Return the useful result,
any remaining disagreement or blocker, and what you actually verified. Add a
compact audit from the helper's stderr: requested model and effort, access,
conversation ID, status and token use. agy names a model only when one was
requested and never reports which model served the call, so mark the served
model unknown.

## Setup problems

- The helper looks for agy in this order: `--agy`, `$DELEGATE_AGY_BIN`, `agy`
  on PATH, then `~/.local/bin/agy`. If none is found, tell the user; do not
  install it yourself.
- agy writes its conversations and logs under `~/.gemini/antigravity-cli`. If
  your host's sandbox blocks agy or that storage, follow the host's escalation
  procedure and say why. Do not loosen agy's permissions to compensate, and do
  not mistake a sandbox denial for a sign-in failure. Inspect completed actions
  before retrying; never replay mutations.
