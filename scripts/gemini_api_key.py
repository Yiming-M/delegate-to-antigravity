#!/usr/bin/env python3
"""Store a Gemini API key (from Google AI Studio) with hidden input, or check a stored one."""
from __future__ import annotations

import argparse
import getpass
import os
import re
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import warnings

KEY_DIR = Path.home() / ".config/delegate-to-antigravity/keys"


def check_private(path: Path, mode: int, directory: bool = False) -> None:
    info = path.lstat()
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        raise ValueError(f"Unsafe credential path: {path}; expected owner-only {mode:o}")
    if not directory and info.st_nlink != 1:
        raise ValueError("Credential file must not have hard links")


def validate_key(value: str) -> None:
    if not 20 <= len(value) <= 4096 or not value.isascii() or not value.isprintable():
        raise ValueError("Expected one printable API key of 20-4096 characters")
    if any(c.isspace() for c in value):
        raise ValueError("The input contains whitespace; paste only the API key or one API-key assignment")


def normalize_input(value: str) -> str:
    value = value.strip()
    for prefix in ("export GEMINI_API_KEY=", "GEMINI_API_KEY="):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    if value[:1] in ("\"", "'"):
        try:
            parts = shlex.split(value)
        except ValueError:
            raise ValueError("Unbalanced quotes around the API key") from None
        if len(parts) != 1:
            raise ValueError("Expected only one API key")
        value = parts[0]
    return value


def key_profile_path(profile: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile):
        raise ValueError("Invalid API key profile name")
    path = KEY_DIR / profile
    script = Path(__file__).resolve()
    checkout = next((parent for parent in script.parents if (parent / ".git").exists()), script.parents[1])
    if path.resolve().is_relative_to(checkout):
        raise ValueError("API keys must be stored outside the skill checkout")
    return path


def read_key(profile: str) -> str:
    path = key_profile_path(profile)
    check_private(path.parent, 0o700, directory=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("API key must be an owner-only regular file with mode 600")
        value = stream.read(4097).strip()
    validate_key(value)
    return value


def store_key(path: Path, value: str) -> None:
    validate_key(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(path.parent, 0o700, directory=True)
    if path.exists() or path.is_symlink():
        check_private(path, 0o600)
    fd, temporary = tempfile.mkstemp(prefix=".api-key-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default", help="Named API key profile")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--stdin", action="store_true", help="Receive the key over a private pipe")
    modes.add_argument("--check", action="store_true", help="Check permissions and format without displaying the key")
    args = parser.parse_args()
    try:
        path = key_profile_path(args.profile)
        if args.check:
            read_key(args.profile)
            print(f"API key ready: {path}; file=600 directory=700")
            return
        if args.stdin:
            value = sys.stdin.read(4097).strip()
        else:
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass("Paste Gemini API key from Google AI Studio (input hidden): ").strip()
        store_key(path, normalize_input(value))
        del value
        print(f"Saved API key: {path}; file=600 directory=700")
    except (OSError, ValueError, getpass.GetPassWarning, EOFError) as exc:
        print(f"API key setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
