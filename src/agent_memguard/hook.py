"""Codex PreToolUse/Bash adapter, with no eval and no command logging."""

import json
import os
from pathlib import Path
import shlex
import shutil
import sys


MAX_INPUT = 1024 * 1024


def find_guard() -> str:
    explicit = os.environ.get("MEMGUARD_BIN")
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError("MEMGUARD_BIN must be an absolute executable path")
        return str(candidate)
    sibling = Path(sys.argv[0]).resolve().with_name("memguard")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("memguard")
    if found:
        return found
    raise RuntimeError("memguard executable not found")


def rewrite(event: dict, guard: str) -> dict:
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "Bash":
        return {}
    command = event.get("tool_input", {}).get("command")
    if not isinstance(command, str):
        raise ValueError("Bash hook requires a string command")
    shell = os.environ.get("MEMGUARD_REAL_SHELL", "/bin/bash")
    if not Path(shell).is_absolute() or not Path(shell).is_file() or not os.access(shell, os.X_OK):
        raise RuntimeError("MEMGUARD_REAL_SHELL must be an absolute executable path")
    wrapped = shlex.join([guard, "--", shell, "-c", command])
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "updatedInput": {"command": wrapped}}}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("hook input exceeds 1 MiB")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        if event.get("tool_name") != "Bash" or event.get("hook_event_name") != "PreToolUse":
            output = {}
        else:
            output = rewrite(event, find_guard())
    except Exception:
        # Hook errors themselves may be ignored by Codex. Return a valid deny
        # decision rather than exiting nonzero and letting a command through.
        output = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                         "permissionDecisionReason": "memguard hook could not safely wrap this command"}}
    print(json.dumps(output))
    return 0
