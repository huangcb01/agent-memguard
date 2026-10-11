"""Track descendants, including orphans adopted by a subreaper."""

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import signal


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    group: int
    started: int
    rss: int
    state: str

    @property
    def alive(self) -> bool:
        return self.state not in ("Z", "X", "x")

    def identity(self) -> dict:
        return {"pid": self.pid, "started": self.started}


def read_process(pid: int) -> Process | None:
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text()
        # comm can contain whitespace and parentheses; fields start after
        # the final closing parenthesis, not after a naive split().
        fields = text[text.rfind(")") + 2:].split()
        return Process(pid, int(fields[1]), int(fields[2]), int(fields[19]),
                       max(0, int(fields[21])) * PAGE_SIZE, fields[0])
    except (FileNotFoundError, ProcessLookupError):
        return None


def process_table() -> dict[int, Process]:
    result = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            process = read_process(int(path.name))
        except (PermissionError, ValueError, IndexError):
            continue
        if process:
            result[process.pid] = process
    return result


def descendants(table: dict[int, Process], parent: int) -> list[Process]:
    children: dict[int, list[int]] = {}
    for process in table.values():
        children.setdefault(process.parent, []).append(process.pid)
    pending = children.get(parent, []).copy()
    seen = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        pending.extend(children.get(pid, []))
    return [table[pid] for pid in seen if table[pid].alive]


def set_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable child subreaper")


def matches(identity: dict) -> bool:
    process = read_process(identity["pid"])
    return bool(process and process.alive and process.started == identity["started"])


def signal_identity(identity: dict, sig: int) -> None:
    """Use a pidfd where available to avoid signaling a recycled PID."""
    pid = identity["pid"]
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            fd = os.pidfd_open(pid)
            try:
                if matches(identity):
                    signal.pidfd_send_signal(fd, sig)
            finally:
                os.close(fd)
        elif matches(identity):
            os.kill(pid, sig)
    except ProcessLookupError:
        pass


def signal_task(members: list[Process], root: Process | None, sig: int) -> None:
    # Capture the original task group too, covering normal workers forked
    # between /proc scans. Never signal the guard's own group.
    if root and root.group == root.pid and root.group != os.getpgrp():
        current = read_process(root.pid)
        if current and current.started == root.started:
            try:
                os.killpg(root.group, sig)
            except ProcessLookupError:
                pass
    for process in members:
        signal_identity(process.identity(), sig)


def reap_orphans(root_pid: int) -> None:
    # Do not steal Popen's wait status for its direct child.
    table = process_table()
    for process in table.values():
        if process.parent == os.getpid() and process.pid != root_pid and not process.alive:
            try:
                os.waitpid(process.pid, os.WNOHANG)
            except ChildProcessError:
                pass
