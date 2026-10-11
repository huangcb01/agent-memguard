"""Read container memory, never the host's /proc/meminfo."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re


def parse_size(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*", text, re.I)
    if not match:
        raise ValueError(f"invalid memory size: {text!r}")
    result = int(float(match[1]) * 1024 ** (" KMGT".index(match[2].upper()) if match[2] else 0))
    if result <= 0:
        raise ValueError("memory size must be positive")
    return result


def format_size(value: int) -> str:
    return f"{value / 1024**3:.2f} GiB"


@dataclass(frozen=True)
class Memory:
    path: str
    maximum: int
    current: int
    inactive_file: int
    dirty: int
    writeback: int

    @property
    def working_set(self) -> int:
        # Dirty/writeback pages cannot be assumed immediately reclaimable.
        reclaimable = max(0, self.inactive_file - self.dirty - self.writeback)
        return max(0, self.current - reclaimable)


def read_memory(path: Path) -> Memory | None:
    maximum = (path / "memory.max").read_text().strip()
    if maximum == "max":
        return None
    maximum_int = int(maximum)
    if maximum_int <= 0:
        raise ValueError("invalid cgroup memory.max")
    stats = dict(line.split() for line in (path / "memory.stat").read_text().splitlines())
    current = int((path / "memory.current").read_text())
    return Memory(str(path), maximum_int, current, int(stats.get("inactive_file", 0)),
                  int(stats.get("file_dirty", 0)), int(stats.get("file_writeback", 0)))


def _unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), text)


def discover_cgroups() -> list[Path]:
    relative = next((line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines()
                     if line.startswith("0::")), None)
    if relative is None:
        raise RuntimeError("cgroup v2 is required")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        if right.split()[0] != "cgroup2":
            continue
        fields = left.split()
        mount = Path(_unescape_mount(fields[4]))
        mount_root = PurePosixPath(_unescape_mount(fields[3]))
        try:
            suffix = PurePosixPath(relative).relative_to(mount_root)
        except ValueError:
            # A private cgroup namespace can report / although mountinfo has
            # the host-side root. Only use the mounted root in that case.
            if relative != "/":
                continue
            suffix = PurePosixPath(".")
        leaf = mount / str(suffix)
        if not (leaf / "memory.current").is_file():
            continue
        result = []
        current = leaf
        while True:
            if (current / "memory.max").is_file():
                result.append(current)
            if current == mount:
                return result
            current = current.parent
    raise RuntimeError("cannot locate the current cgroup v2 memory controller")


def snapshots(paths: list[Path]) -> list[Memory]:
    return [value for path in paths if (value := read_memory(path)) is not None]
