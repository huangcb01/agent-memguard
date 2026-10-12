"""Read scoped cgroup memory or explicitly selected WSL VM memory."""

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
    source: str = "cgroup"
    swap_current: int | None = None
    swap_maximum: int | None = None
    slab_reclaimable: int = 0
    slab_unreclaimable: int = 0

    @property
    def working_set(self) -> int:
        # Dirty/writeback pages cannot be assumed immediately reclaimable.
        reclaimable_file = max(0, self.inactive_file - self.dirty - self.writeback)
        # Slab reclaim can be slow or incomplete (e.g. referenced dentries).
        # Credit only half, retaining the other half as a safety margin. WSL
        # already uses MemAvailable, so must not receive a second deduction.
        reclaimable_slab = self.slab_reclaimable_credit
        return max(0, self.current - reclaimable_file - reclaimable_slab)

    @property
    def slab_reclaimable_credit(self) -> int:
        return max(0, self.slab_reclaimable) // 2 if self.source == "cgroup" else 0

    @property
    def available(self) -> int:
        return max(0, self.maximum - self.working_set)

    def metrics(self) -> dict:
        result = {"source": self.source, "path": self.path, "maximum": self.maximum,
                  "current": self.current, "working_set": self.working_set, "available": self.available,
                  "swap_current": self.swap_current, "swap_maximum": self.swap_maximum}
        if self.source == "cgroup":
            result.update(inactive_file=self.inactive_file, file_dirty=self.dirty, file_writeback=self.writeback,
                          slab_reclaimable=self.slab_reclaimable, slab_unreclaimable=self.slab_unreclaimable,
                          slab_reclaimable_credit=self.slab_reclaimable_credit)
        return result


def read_memory(path: Path) -> Memory | None:
    maximum = (path / "memory.max").read_text().strip()
    if maximum == "max":
        return None
    maximum_int = int(maximum)
    if maximum_int <= 0:
        raise ValueError("invalid cgroup memory.max")
    stats = dict(line.split() for line in (path / "memory.stat").read_text().splitlines())
    current = int((path / "memory.current").read_text())
    swap_current = (path / "memory.swap.current").read_text().strip() if (path / "memory.swap.current").exists() else None
    swap_maximum = (path / "memory.swap.max").read_text().strip() if (path / "memory.swap.max").exists() else None
    return Memory(str(path), maximum_int, current, int(stats.get("inactive_file", 0)),
                  int(stats.get("file_dirty", 0)), int(stats.get("file_writeback", 0)),
                  swap_current=int(swap_current) if swap_current is not None else None,
                  swap_maximum=int(swap_maximum) if swap_maximum not in (None, "max") else None,
                  slab_reclaimable=int(stats.get("slab_reclaimable", 0)),
                  slab_unreclaimable=int(stats.get("slab_unreclaimable", 0)))


def read_wsl_memory(path: Path) -> Memory:
    # A container's meminfo may describe its host. Only read the native file
    # in explicitly selected WSL mode on a WSL kernel. Overrides are fixtures.
    if path == Path("/proc/meminfo") and "microsoft" not in Path("/proc/sys/kernel/osrelease").read_text().lower():
        raise RuntimeError("WSL mode requires a WSL kernel; container mode uses cgroup memory")
    required = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    values = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields or fields[0].rstrip(":") not in required:
            continue
        if len(fields) != 3 or fields[2] != "kB":
            raise ValueError("invalid WSL memory metrics")
        values[fields[0].rstrip(":")] = int(fields[1]) * 1024
    if values.keys() != required:
        raise ValueError("missing WSL memory metrics")
    total, available = values["MemTotal"], values["MemAvailable"]
    swap_total, swap_free = values["SwapTotal"], values["SwapFree"]
    if total <= 0 or not 0 <= available <= total or not 0 <= swap_free <= swap_total:
        raise ValueError("invalid WSL memory metrics")
    # MemAvailable already estimates reclaimable memory. Swap is reported
    # separately, and is not added to the usable physical-memory capacity.
    return Memory(str(path), total, total - available, 0, 0, 0, source="wsl",
                  swap_current=swap_total - swap_free, swap_maximum=swap_total)


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
