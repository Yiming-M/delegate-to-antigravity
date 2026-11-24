"""Named agy authentication sources, their order, and the checks that decide when to move on."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gemini_api_key import key_profile_path, read_key

# Variables that would make agy use a credential other than the one a source names.
FOREIGN_AUTH_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL", "AGY_ADC_AUTH",
                    "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_APPLICATION_CREDENTIALS")


@dataclass(frozen=True)
class AuthSource:
    name: str
    key_profile: str | None = None

    @property
    def kind(self) -> str:
        return "api" if self.key_profile else "subscription"

    def launch(self) -> tuple[dict[str, Any], dict[str, str]]:
        """Settings to merge into this call's --settings, and the environment agy runs with."""
        env = {name: value for name, value in os.environ.items()
               if name not in FOREIGN_AUTH_ENV and not name.startswith("AGY_LLM_GATEWAY_")}
        if not self.key_profile:
            return {}, env
        # Without modelProvider "gemini", agy ignores the key and keeps using the saved sign-in.
        env["GEMINI_API_KEY"] = read_key(self.key_profile)
        return {"modelProvider": "gemini"}, env


def load_auth_config(path: Path) -> tuple[dict[str, AuthSource], list[str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read the authentication configuration as JSON") from exc
    if not isinstance(document, dict) or document.keys() != {"sources", "order"}:
        raise ValueError("Authentication configuration must contain sources and order")
    sources, order = document["sources"], document["order"]
    if not isinstance(sources, dict) or not sources or not isinstance(order, list) or not order:
        raise ValueError("Authentication configuration requires named sources and a nonempty order")
    parsed = {}
    for name, fields in sources.items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise ValueError("Invalid authentication source name")
        if not isinstance(fields, dict) or fields.keys() - {"type", "key_profile"}:
            raise ValueError(f"Invalid fields for authentication source {name}")
        profile = fields.get("key_profile")
        if fields.get("type") == "api" and isinstance(profile, str):
            key_profile_path(profile)
        elif fields.get("type") != "subscription" or "key_profile" in fields:
            raise ValueError(f"Authentication source {name} needs type subscription, or type api with key_profile")
        parsed[name] = AuthSource(name, profile)
    if sum(source.kind == "subscription" for source in parsed.values()) > 1:
        raise ValueError("agy keeps one saved sign-in, so configure at most one subscription source")
    if any(not isinstance(name, str) or name not in parsed for name in order) or len(set(order)) != len(order):
        raise ValueError("Authentication order must list distinct configured source names")
    return parsed, order


def check_auth(agy: str, settings: dict[str, Any], env: dict[str, str], cwd: Path,
               timeout: int) -> tuple[str, list[dict[str, str]]]:
    """Ask agy which credential it would use, without a model request.

    `/usage` lists the plan's remaining quota for a sign-in and exits 2 with a Gemini API key.
    """
    command = [agy]
    if settings:
        command += ["--settings", json.dumps(settings)]
    result = subprocess.run(command + ["-p", "/usage"], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=timeout)
    output = result.stdout + result.stderr
    if result.returncode == 2 and "not available with a Gemini API key" in output:
        return "api", []
    rows = [dict(zip(("models", "window", "remaining", "resets"), line.split("\t")))
            for line in result.stdout.splitlines() if line.count("\t") == 3]
    if result.returncode == 0 and rows:
        return "subscription", rows
    if re.search(r"authentication required|not signed in|sign in", output, re.IGNORECASE):
        return "none", []
    return "unknown", []


def model_family(model: str | None) -> str:
    return "Claude and GPT models" if model and model.lower().startswith(("claude", "gpt")) else "Gemini Models"


def quota_error(error: Any) -> bool:
    return bool(re.search(r"RESOURCE_EXHAUSTED|QUOTA_EXHAUSTED|\b429\b|quota", json.dumps(error), re.IGNORECASE))


def plan_exhausted(rows: list[dict[str, str]], model: str | None) -> bool:
    family = model_family(model)
    return any(row["models"] == family and row["remaining"].strip() == "0%" for row in rows)


def api_exhausted(error: Any) -> bool:
    # Per-minute limits are temporary and agy retries them; these end the key's allowance.
    return bool(re.search(r"prepayment credits are depleted|per day limit exceeded|PerDay"
                          r"|spend(?:ing)? cap|credits? (?:are |is )?(?:depleted|exhausted)",
                          json.dumps(error), re.IGNORECASE))
