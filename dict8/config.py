"""Config access.

CLAUDE.md invariant 3: no path, hotkey, model name, or threshold appears in code — it is
all read from `config.yml`. The corollary that matters more: a `TBD` is a *visible gap*,
never a silent default. `require()` refuses to hand back an unresolved value, so a missing
decision fails loudly at the call site instead of turning into a plausible-looking guess
three layers down.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

TBD = "TBD"

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("DICT8_CONFIG", REPO_ROOT / "config.yml"))


class ConfigGap(RuntimeError):
    """A config value the caller needs is still TBD/null.

    Deliberately not a subclass of KeyError: an unresolved decision is a different
    failure from a typo'd key, and they want different fixes.
    """


def _is_gap(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == TBD)


class Config:
    def __init__(self, data: dict, source: Path | None = None) -> None:
        self._data = data
        self.source = source

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = Path(path) if path else CONFIG_PATH
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")), p)

    def get(self, dotted: str, default: Any = None) -> Any:
        """Fetch by dotted path. Returns `default` for a missing key OR an unresolved TBD."""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if _is_gap(node) else node

    def require(self, dotted: str) -> Any:
        """Fetch by dotted path, refusing to return an unresolved value."""
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigGap(f"{dotted} is unresolved in {self.source} — resolve it, do not default it")
        return value

    def path(self, dotted: str) -> Path:
        """A required config value that names a filesystem path, with `~` expanded."""
        return Path(str(self.require(dotted))).expanduser()

    def gaps(self, prefix: str = "") -> list[str]:
        """Every dotted key still TBD/null — what the UI renders as a labeled gap."""
        found: list[str] = []

        def walk(node: Any, trail: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{trail}.{k}" if trail else str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{trail}[{i}]")
            elif _is_gap(node) and trail.startswith(prefix):
                found.append(trail)

        walk(self._data, "")
        return sorted(found)


_cached: Config | None = None


def load(path: Path | None = None) -> Config:
    """Process-wide config. Explicit `path` always reloads."""
    global _cached
    if path is not None:
        return Config.load(path)
    if _cached is None:
        _cached = Config.load()
    return _cached
