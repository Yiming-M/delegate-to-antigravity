# Configure authentication and priority

Without `--auth-config`, the helper uses `~/.config/delegate-to-antigravity/routes.json` when that file exists, and otherwise only agy's own sign-in. Use a route file to add Gemini API keys and choose the order.

## Sources

- **subscription**: the Google account agy is signed in to, with its Antigravity plan quota. agy keeps one saved sign-in, so a route file has at most one subscription source. Sign in by running `agy` once in a terminal.
- **api**: a Gemini API key created in Google AI Studio. API calls are billed to the key's Google Cloud project, including prepaid credit, and do not use the plan quota. They serve Gemini models only, and the model list differs from the sign-in's (`agy models` shows each). `/usage` and Remote Control are not available with a key; see the key's usage in AI Studio.

## Add API keys

Run the key tool from the skill directory and paste the key at its hidden prompt. Profile names use lowercase letters, digits, hyphens and underscores, with a letter or digit first. `--check` checks a profile without printing the key.

```bash
python3 scripts/gemini_api_key.py --profile api1
python3 scripts/gemini_api_key.py --profile api1 --check
```

Keys are stored in owner-only files under `~/.config/delegate-to-antigravity/keys/`. For an API call the helper passes the key to agy in the `GEMINI_API_KEY` environment variable together with the per-call setting `modelProvider: "gemini"`; agy ignores the variable without that setting. Neither is written to agy's `settings.json`. Route files contain only profile names. Keep key values out of the repository, route files, prompts, command arguments and transcripts.

agy's terminal sandbox blocks commands from reading `~/.config`, so delegated commands cannot read the key files. Whether agy removes `GEMINI_API_KEY` from the environment of commands it runs is not documented; with an API source, allow only the commands a task needs.

## Choose the order

Copy the [example](../examples/plan-then-two-keys.routes.json) and edit `order` to choose a sequence or subset of the named sources. The example uses the plan first, then two keys.

```bash
mkdir -p "$HOME/.config/delegate-to-antigravity"
cp examples/plan-then-two-keys.routes.json "$HOME/.config/delegate-to-antigravity/routes.json"
```

Every call starts at the first source. Before the task is sent, the helper runs `agy -p /usage` with that source's environment: a sign-in lists the plan's remaining quota, and a key makes it exit 2. If agy is not using the expected credential, the call stops before any model request.

The call moves to the next source only when:

- on the subscription, the turn fails with a quota error and `/usage` then shows 0% remaining for the model's family (`Gemini Models`, or `Claude and GPT models`);
- on an API key, the turn fails because prepaid credit is depleted, a daily quota is exceeded, or a spend cap is reached.

The next source resumes the same conversation with a continuation prompt, the same model, effort and access. agy stores conversations locally, and a conversation started with one credential can be resumed with the other (checked with agy 1.5.16). A Claude or GPT model is never handed to an API key. Per-minute rate limits, model overload (503), authentication failures, network errors and timeouts end the call. `--auth-source NAME` uses one named source.
