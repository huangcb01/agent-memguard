"""Small shared registry: reserve budgets and elect one pressure victim."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .processes import matches


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
    def __init__(self, directory: Path, paths: list[Path], owner: dict, limit: int):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeError("state directory must be owned by this user and not writable by others")
        namespace = os.readlink("/proc/self/ns/pid") + os.readlink("/proc/self/ns/cgroup")
        key = hashlib.sha256((namespace + repr([str(p.resolve()) for p in paths])).encode()).hexdigest()[:20]
        self.path = directory / (key + ".json")
        self.lock_path = directory / (key + ".lock")
        self.owner = owner
        self.identifier = f"{owner['pid']}:{owner['started']}"
        self.limit = limit
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
                if data.get("version") != 1 or not isinstance(data.get("tasks"), dict):
                    raise RuntimeError("invalid task registry")
            else:
                data = {"version": 1, "tasks": {}, "request": None, "cooldown_until": 0}
            for identifier, record in list(data["tasks"].items()):
                if not matches(record["owner"]):
                    del data["tasks"][identifier]
            request = data.get("request")
            if request and request["victim"] not in data["tasks"]:
                data["request"] = None
                data["cooldown_until"] = time.monotonic() + 0.75
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

    def register(self, memories, soft_fraction: float, admission: bool) -> None:
        with self.locked() as data:
            pending = sum(max(0, record["limit"] - record["rss"]) for record in data["tasks"].values())
            if admission:
                for memory in memories:
                    if memory.working_set + pending + self.limit > memory.maximum * soft_fraction:
                        raise RuntimeError("insufficient container headroom for this task and reserved budgets")
            data["tasks"][self.identifier] = {
                "owner": self.owner, "limit": self.limit, "rss": 0, "growth": 0,
                "updated": time.monotonic(), "root": None,
            }

    def update(self, rss: int, root: dict | None, pressure: str | None) -> tuple[dict | None, dict | None]:
        now = time.monotonic()
        with self.locked() as data:
            record = data["tasks"][self.identifier]
            record.update(rss=rss, growth=max(0, rss - self.previous_rss), updated=now, root=root)
            self.previous_rss = rss
            request = data.get("request")
            if pressure is None:
                # Pressure can disappear without a task being killed.
                data["request"] = request = None
            elif request:
                if pressure == "hard":
                    request["level"] = "hard"
            elif now >= data.get("cooldown_until", 0):
                candidates = [(key, value) for key, value in data["tasks"].items() if value["root"]]
                if candidates:
                    victim, _ = max(candidates, key=lambda item: item[1]["rss"] + 2 * item[1]["growth"])
                    request = {"victim": victim, "level": pressure, "created": now}
                    data["request"] = request
            own = dict(request) if request and request["victim"] == self.identifier else None
            peer = None
            if request and request["victim"] != self.identifier and request["level"] == "hard" and now - request["created"] >= 0.5:
                peer = dict(data["tasks"][request["victim"]])
            return own, peer

    def unregister(self) -> None:
        with self.locked() as data:
            data["tasks"].pop(self.identifier, None)
            if data.get("request") and data["request"]["victim"] == self.identifier:
                data["request"] = None
                data["cooldown_until"] = time.monotonic() + 0.75
