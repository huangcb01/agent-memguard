import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_memguard.hook import rewrite


class HookTests(unittest.TestCase):
    def test_shell_quoting_preserves_command(self):
        command = "printf '%s\\n' 'a b' '$literal'; exit 7"
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        rewritten = rewrite(event, str(ROOT / "bin/memguard"))["hookSpecificOutput"]["updatedInput"]["command"]
        with tempfile.TemporaryDirectory() as directory:
            cgroup = Path(directory) / "cgroup"
            cgroup.mkdir()
            (cgroup / "memory.max").write_text(str(1024**3))
            (cgroup / "memory.current").write_text(str(100 * 1024**2))
            (cgroup / "memory.stat").write_text("inactive_file 0\n")
            result = subprocess.run(["/bin/bash", "-c", rewritten], capture_output=True, text=True, timeout=10,
                                    env={**os.environ,
                                         "MEMGUARD_CGROUP_DIR": str(cgroup),
                                         "MEMGUARD_STATE_DIR": str(Path(directory) / "state")})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, "a b\n$literal\n")

    def test_invalid_payload_denies_instead_of_hook_failure(self):
        result = subprocess.run([str(ROOT / "bin/memguard-codex-hook")], input="{bad json", text=True,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_missing_guard_denies(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}}
        result = subprocess.run([str(ROOT / "bin/memguard-codex-hook")], input=json.dumps(event), text=True,
                                capture_output=True, timeout=5, env={**os.environ, "MEMGUARD_BIN": "/not/a/guard"})
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
