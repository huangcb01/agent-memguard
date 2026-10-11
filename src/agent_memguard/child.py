"""Small exec helper: configure OOM preference before the target can allocate."""

import ctypes
import os
import sys


def main() -> int:
    parent = int(sys.argv[1])
    ready_fd = int(sys.argv[2])
    oom_adj = int(sys.argv[3])
    command = sys.argv[4:]
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, 9, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG = SIGKILL
            raise OSError(ctypes.get_errno(), "cannot set parent-death signal")
        if os.getppid() != parent:
            return 125
        with open("/proc/self/oom_score_adj", "w") as stream:
            stream.write(str(oom_adj))
        with open("/proc/self/oom_score_adj") as stream:
            if int(stream.read()) != oom_adj:
                raise RuntimeError("OOM preference was not applied")
        os.write(ready_fd, b"READY\n")
        os.close(ready_fd)
    except Exception:
        # Never print the command, environment, or exception payload here.
        try:
            os.write(ready_fd, b"ERROR\n")
        except OSError:
            pass
        print("[memguard] target initialization failed; command was not executed", file=sys.stderr)
        return 125
    try:
        os.execvp(command[0], command)
    except FileNotFoundError:
        print("[memguard] target executable not found", file=sys.stderr)
        return 127
    except PermissionError:
        print("[memguard] target executable is not executable", file=sys.stderr)
        return 126
    except OSError:
        print("[memguard] target exec failed", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
