"""Small shared registry: elect victims only under environment pressure."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .processes import Process, descendants, matches


def default_state_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        path = Path(runtime)
        if path.is_dir() and path.stat().st_uid == os.getuid():
            return path / "agent-memguard"
    # This registry is rewritten every sample. Prefer local ephemeral storage
    # instead of a user's cache, which may live on a slow shared filesystem.
    return Path(tempfile.gettempdir()) / f"agent-memguard-{os.getuid()}"


class Coordinator:
    def __init__(self, directory: Path, owner: dict):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeError("state directory must be owned by this user and not writable by others")
        # One lock covers overlapping cgroup hierarchies. Membership in each
        # finite scope is stored per task, rather than encoded in the file name.
        # Namespace isolation keeps both local PIDs and visible paths comparable.
        namespace = os.readlink("/proc/self/ns/pid") + os.readlink("/proc/self/ns/cgroup")
        key = hashlib.sha256((namespace + ":pressure-v3").encode()).hexdigest()[:20]
        self.path = directory / (key + ".json")
        self.lock_path = directory / (key + ".lock")
        self.owner = owner
        self.identifier = f"{owner['pid']}:{owner['started']}"
        self.previous_rss = 0

    @contextmanager
    def locked(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 0.1
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("task registry lock timed out")
                    time.sleep(0.005)
            if self.path.exists():
                data = json.loads(self.path.read_text())
                if data.get("version") != 3 or not isinstance(data.get("tasks"), dict):
                    raise RuntimeError("invalid task registry")
            else:
                data = {"version": 3, "tasks": {}, "requests": {}, "cooldowns": {}}
            for identifier, record in list(data["tasks"].items()):
                if not matches(record["owner"]):
                    del data["tasks"][identifier]
            for scope, request in list(data["requests"].items()):
                if request["victim"] not in data["tasks"]:
                    self._complete(data, scope, request)
            data["cooldowns"] = {scope: until for scope, until in data["cooldowns"].items()
                                 if until > time.monotonic()}
            yield data
            temporary_fd, temporary_name = tempfile.mkstemp(prefix=".registry-", dir=self.path.parent)
            try:
                with os.fdopen(temporary_fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(data, stream, separators=(",", ":"))
                    stream.flush()
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        finally:
            os.close(fd)

    @staticmethod
    def _refresh_scopes(data, paths, finite_scopes):
        """Apply a sample to every task sharing the observed controllers."""
        observed = set(paths)
        finite = set(finite_scopes)
        for task in data["tasks"].values():
            membership = set(task["paths"])
            task["scopes"] = sorted((set(task["scopes"]) - observed) | (membership & finite))
            task["pressure"] = {scope: level for scope, level in task["pressure"].items()
                                if scope in task["scopes"]}
        for request in data["requests"].values():
            request["domain"] = list(data["tasks"][request["victim"]]["scopes"])

    def register(self, memories, soft_fraction: float, admission: bool,
                 paths: list[Path] | None = None, table: dict[int, Process] | None = None) -> None:
        with self.locked() as data:
            self._finish_exited_tasks(data, table)
            scopes = [str(Path(memory.path).resolve()) for memory in memories]
            monitored = [str(Path(path).resolve()) for path in paths] if paths is not None else scopes
            self._refresh_scopes(data, monitored, scopes)
            if admission:
                for memory in memories:
                    if memory.working_set >= memory.maximum * soft_fraction:
                        raise RuntimeError("environment memory pressure too high to start a task")
            data["tasks"][self.identifier] = {
                "owner": self.owner, "rss": 0, "growth": 0,
                "updated": time.monotonic(), "root": None,
                "paths": monitored, "scopes": scopes, "pressure": {}, "termination": None, "released": False,
            }

    @staticmethod
    def _complete(data, scope, request):
        # The released task also relieves its ancestors. Give every scope in
        # this request's domain time to observe that release before reelecting.
        until = time.monotonic() + 0.75
        for path in request["domain"]:
            data["cooldowns"][path] = until
        del data["requests"][scope]

    @classmethod
    def _finish_exited_tasks(cls, data, table):
        """Release elections only after a sample confirms the whole task exited.

        The caller scans /proc before acquiring the lock. Keep the termination
        result for an owner that may still be stopped and unable to acknowledge.
        """
        if table is None:
            return
        for scope, request in list(data["requests"].items()):
            if not request["committed"]:
                continue
            victim = data["tasks"][request["victim"]]
            owner = table.get(victim["owner"]["pid"])
            identity = victim["root"]
            if not owner or owner.started != victim["owner"]["started"] or identity is None:
                continue
            root = table.get(identity["pid"])
            if root and root.started == identity["started"] and root.alive:
                continue
            if descendants(table, owner.pid):
                continue
            victim.update(released=True, rss=0, growth=0, pressure={})
            cls._complete(data, scope, request)

    def update(self, rss: int, root: dict | None, pressure: str | None,
               pressure_paths: list[str] | None = None, memories=None,
               table: dict[int, Process] | None = None) -> tuple[dict | None, dict | None]:
        now = time.monotonic()
        with self.locked() as data:
            self._finish_exited_tasks(data, table)
            record = data["tasks"][self.identifier]
            if memories is not None:
                finite = [str(Path(memory.path).resolve()) for memory in memories]
                self._refresh_scopes(data, record.get("paths", record["scopes"]), finite)
            record.update(rss=rss, growth=max(0, rss - self.previous_rss), updated=now, root=root)
            self.previous_rss = rss
            scopes = record["scopes"] if pressure_paths is None else [str(Path(p).resolve()) for p in pressure_paths]
            record["pressure"] = ({scope: pressure for scope in scopes if scope in record["scopes"]}
                                  if pressure and not record.get("released") else {})
            for scope, request in list(data["requests"].items()):
                # A victim also releases memory in its ancestors. Hard
                # pressure there must upgrade an existing leaf TERM request,
                # even when the victim's own launcher cannot take a sample.
                levels = [level for task in data["tasks"].values()
                          for path, level in task["pressure"].items() if path in request["domain"]]
                if not levels and not request["committed"]:
                    # Recovery must be observed by every reporting source;
                    # another launcher's higher thresholds cannot cancel it.
                    del data["requests"][scope]
                elif "hard" in levels:
                    request["level"] = "hard"
            for scope, level in record["pressure"].items():
                if scope in data["requests"] or now < data["cooldowns"].get(scope, 0):
                    continue
                # Wait only when an existing victim can relieve this exact
                # pressure scope. A sibling's victim cannot relieve our leaf,
                # even if both tasks have the same finite, healthy ancestor.
                if any(scope in request["domain"] for request in data["requests"].values()):
                    continue
                candidates = [(key, value) for key, value in data["tasks"].items()
                              if value["root"] and not value["termination"] and scope in value["scopes"]]
                if candidates:
                    victim, selected = max(candidates, key=lambda item: item[1]["rss"] + 2 * item[1]["growth"])
                    data["requests"][scope] = {
                        "victim": victim, "level": level, "created": now, "scope": scope,
                        "domain": selected["scopes"], "committed": False,
                    }
            own = None
            peer = None
            for request in data["requests"].values():
                if request["victim"] == self.identifier:
                    own = dict(request)
                elif (set(record["scopes"]) & set(request["domain"]) and request["level"] == "hard"
                      and now - request["created"] >= 0.5):
                    victim = data["tasks"][request["victim"]]
                    peer = {**victim, "request": dict(request)}
            if record["termination"]:
                own = {**(own or {}), "victim": self.identifier, "termination": dict(record["termination"])}
            return own, peer

    def commit(self, request: dict, signal: str) -> dict | None:
        """Persist the reason before signaling; reject a canceled election."""
        with self.locked() as data:
            victim = data["tasks"].get(request["victim"])
            if victim is None:
                return None
            termination = victim["termination"]
            if termination and (signal == "SIGTERM" or termination["signal"] == "SIGKILL"):
                return dict(termination)
            current = data["requests"].get(request.get("scope"))
            if not current or (current["victim"], current["created"]) != (request["victim"], request["created"]):
                return None
            current["committed"] = True
            victim["termination"] = {
                "reason": "environment_pressure", "signal": signal, "rss": victim["rss"],
                "scope": current["scope"], "created": time.monotonic(),
            }
            return dict(victim["termination"])

    def unregister(self) -> None:
        with self.locked() as data:
            data["tasks"].pop(self.identifier, None)
            for scope, request in list(data["requests"].items()):
                if request["victim"] == self.identifier:
                    self._complete(data, scope, request)
