"""CLI and shell-compatible entry points."""

import argparse
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from .coordinator import Coordinator, default_state_dir
from .memory import discover_cgroups, format_size, parse_size, snapshots
from .processes import (descendants, matches, process_table, read_process,
                        reap_orphans, set_subreaper, signal_task)


GUARD_EXIT = 125


def report(event: str, **fields) -> None:
    print("[memguard] " + json.dumps({"event": event, **fields}, ensure_ascii=False), file=sys.stderr, flush=True)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Run a Linux command under a task and container memory guard.")
    parser.add_argument("--limit", type=parse_size, default=os.environ.get("MEMGUARD_LIMIT", "1G"),
                        help="task RSS budget, shared pages counted per process (default: 1G)")
    parser.add_argument("--soft-fraction", type=float, default=os.environ.get("MEMGUARD_SOFT_FRACTION", "0.8125"))
    parser.add_argument("--hard-fraction", type=float, default=os.environ.get("MEMGUARD_HARD_FRACTION", "0.875"))
    parser.add_argument("--interval", type=float, default=os.environ.get("MEMGUARD_INTERVAL", "0.25"))
    parser.add_argument("--soft-duration", type=float, default=os.environ.get("MEMGUARD_SOFT_DURATION", "0.5"))
    parser.add_argument("--term-grace", type=float, default=os.environ.get("MEMGUARD_TERM_GRACE", "0.5"))
    parser.add_argument("--oom-score-adj", type=int, default=os.environ.get("MEMGUARD_OOM_SCORE_ADJ", "500"))
    parser.add_argument("--state-dir", type=Path, default=os.environ.get("MEMGUARD_STATE_DIR"))
    parser.add_argument("--cgroup-dir", type=Path, default=os.environ.get("MEMGUARD_CGROUP_DIR"),
                        help="override cgroup discovery; useful for integration tests")
    parser.add_argument("--no-admission-check", action="store_true", help="do not reserve task budgets before starting")
    parser.add_argument("--allow-unlimited", action="store_true", help="allow RSS-only protection when there is no finite cgroup limit")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--check", action="store_true", help="print effective settings without executing a command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    if options.command[:1] == ["--"]:
        options.command = options.command[1:]
    if not 0 < options.soft_fraction < options.hard_fraction < 1:
        parser.error("require 0 < soft-fraction < hard-fraction < 1")
    if not 0.05 <= options.interval <= 2:
        parser.error("interval must be between 0.05 and 2 seconds")
    if not 0 <= options.soft_duration <= 5 or not 0 <= options.term_grace <= 2:
        parser.error("soft-duration must be 0..5 and term-grace must be 0..2 seconds")
    if not 0 <= options.oom_score_adj <= 1000:
        parser.error("target oom-score-adj must be between 0 and 1000")
    if not options.command and not options.check:
        parser.error("provide a command after --")
    return options


def pressure_level(memories, options, soft_since, now):
    if any(m.working_set >= m.maximum * options.hard_fraction for m in memories):
        return "hard", soft_since
    if any(m.working_set >= m.maximum * options.soft_fraction for m in memories):
        if soft_since is None:
            soft_since = now
        return ("soft" if now - soft_since >= options.soft_duration else None), soft_since
    return None, None


def _spawn(options):
    reader, writer = os.pipe()
    child = None
    try:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("child.py")), str(os.getpid()),
             str(writer), str(options.oom_score_adj), *options.command],
            pass_fds=(writer,), start_new_session=True,
        )
        os.close(writer)
        writer = -1
        if not select.select([reader], [], [], 5)[0] or os.read(reader, 64) != b"READY\n":
            raise RuntimeError("target initialization did not succeed")
        root = read_process(child.pid)
        if root is None:
            # A fast command can already be reaped by the time we sample.
            return child, None
        return child, root
    except BaseException:
        if child:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        raise
    finally:
        os.close(reader)
        if writer != -1:
            os.close(writer)


def _cleanup(child, root):
    if child is None:
        return
    # Repeated scans also catch children orphaned during the first kill.
    deadline = time.monotonic() + 2
    while True:
        members = descendants(process_table(), os.getpid())
        signal_task(members, root, signal.SIGKILL)
        if not members or time.monotonic() >= deadline:
            break
        child.poll()
        reap_orphans(child.pid)
        time.sleep(0.02)
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        report("cleanup_incomplete", reason="task did not exit after SIGKILL")
    reap_orphans(child.pid)


def run(options) -> int:
    child = root = coordinator = None
    registered = False
    previous_handlers = {}
    received_signal = [None]
    final_reported = False
    try:
        paths = [options.cgroup_dir] if options.cgroup_dir else discover_cgroups()
        memories = snapshots(paths)
        if not memories and not options.allow_unlimited:
            raise RuntimeError("no finite cgroup limit; use --allow-unlimited explicitly for RSS-only protection")
        if options.check:
            print(json.dumps({
                "limit": options.limit, "oom_score_adj": options.oom_score_adj,
                "state_dir": str(options.state_dir or default_state_dir()),
                "cgroups": [{"path": m.path, "maximum": m.maximum, "current": m.current,
                             "working_set": m.working_set, "soft": int(m.maximum * options.soft_fraction),
                             "hard": int(m.maximum * options.hard_fraction)} for m in memories],
            }, indent=2))
            return 0
        set_subreaper()
        owner = read_process(os.getpid())
        if owner is None:
            raise RuntimeError("cannot identify the guard process")
        coordinator = Coordinator(options.state_dir or default_state_dir(), paths, owner.identity(), options.limit)
        coordinator.register(memories, options.soft_fraction, not options.no_admission_check)
        registered = True
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            previous_handlers[sig] = signal.signal(sig, lambda signum, frame: received_signal.__setitem__(0, signum))
        child, root = _spawn(options)
        if options.verbose:
            report("started", pid=child.pid, limit=options.limit, oom_score_adj=options.oom_score_adj)
        soft_since = stopping_at = None
        reason = None
        last_rss = 0
        peak_rss = trigger_rss = 0
        trigger_memories = []
        killed = False
        while True:
            now = time.monotonic()
            members = descendants(process_table(), os.getpid())
            last_rss = sum(p.rss for p in members)
            peak_rss = max(peak_rss, last_rss)
            memories = snapshots(paths)  # Read failures terminate the task.
            if not memories and not options.allow_unlimited:
                raise RuntimeError("finite cgroup memory limit disappeared")
            level, soft_since = pressure_level(memories, options, soft_since, now)
            own, peer = coordinator.update(last_rss, root.identity() if root else None, level)
            if peer and matches(peer["owner"]):
                table = process_table()
                peer_root = table.get(peer["root"]["pid"]) if peer.get("root") else None
                if peer_root and peer_root.started != peer["root"]["started"]:
                    peer_root = None
                signal_task(descendants(table, peer["owner"]["pid"]), peer_root, signal.SIGKILL)
            if reason is None:
                if received_signal[0] is not None:
                    reason = "interrupted"
                elif last_rss >= options.limit:
                    reason = "task_rss_limit"
                elif own:
                    reason = "container_pressure"
                if reason:
                    stopping_at = now
                    trigger_rss = last_rss
                    trigger_memories = memories
                    if reason == "interrupted":
                        signal_task(members, root, received_signal[0])
                    else:
                        signal_task(members, root, signal.SIGTERM)
            hard = level == "hard" and reason is not None
            if reason and (hard or now - stopping_at >= options.term_grace) and not killed:
                signal_task(members, root, signal.SIGKILL)
                killed = True
            code = child.poll()
            if code is not None:
                if reason:
                    report("terminated", reason=reason, pid=child.pid, task_rss=trigger_rss, peak_rss=peak_rss,
                           limit=options.limit, signal="SIGKILL" if killed else (
                               signal.Signals(received_signal[0]).name if reason == "interrupted" else "SIGTERM"),
                           cgroups=[{"maximum": m.maximum, "current": m.current,
                                     "inactive_file": m.inactive_file, "working_set": m.working_set}
                                    for m in trigger_memories],
                           suggestion="reduce workers, process in chunks, or use a cluster job")
                    final_reported = True
                    return 128 + received_signal[0] if reason == "interrupted" else GUARD_EXIT
                if code < 0:
                    report("target_signaled", signal=-code, pid=child.pid,
                           reason="target received a signal; this alone does not prove kernel OOM")
                    final_reported = True
                    return 128 - code
                return code
            if reason and now - stopping_at >= options.term_grace + 2:
                report("terminated", reason=reason, pid=child.pid, task_rss=trigger_rss, peak_rss=peak_rss,
                       limit=options.limit, signal="SIGKILL", cleanup_pending=True)
                final_reported = True
                return 128 + received_signal[0] if reason == "interrupted" else GUARD_EXIT
            reap_orphans(child.pid)
            time.sleep(options.interval)
    except (Exception, KeyboardInterrupt) as error:
        if not final_reported:
            # Our own exception strings can describe configuration, but never
            # dump arbitrary exception contents, command argv, or environment.
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            report("guard_error", reason=detail, exit_code=GUARD_EXIT)
        return GUARD_EXIT
    finally:
        _cleanup(child, root)
        if coordinator and registered:
            try:
                coordinator.unregister()
            except Exception:
                report("registry_cleanup_failed", reason="stale record will be pruned on the next run")
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main(argv=None) -> int:
    return run(arguments(argv))


def shell_main(argv=None) -> int:
    shell = Path(os.environ.get("MEMGUARD_REAL_SHELL", "/bin/bash"))
    try:
        if not shell.is_absolute() or not shell.is_file() or not os.access(shell, os.X_OK):
            raise RuntimeError("MEMGUARD_REAL_SHELL must be an absolute executable path")
        if shell.resolve() == Path(sys.argv[0]).resolve():
            raise RuntimeError("MEMGUARD_REAL_SHELL cannot point back to memguard-shell")
        return main(["--", str(shell), *(sys.argv[1:] if argv is None else argv)])
    except RuntimeError as error:
        report("guard_error", reason=str(error), exit_code=GUARD_EXIT)
        return GUARD_EXIT
