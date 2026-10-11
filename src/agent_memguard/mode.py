"""Environment modes shared by the launcher and command adapters."""


MODES = ("container", "wsl")


def parse_mode(value: str) -> str:
    if value not in MODES:
        raise ValueError("mode must be container or wsl")
    return value
