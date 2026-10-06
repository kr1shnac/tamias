"""Load router tunables from a TOML file.

Everything here is plain data: the file is parsed with ``tomllib`` and never
evaluated, so a config file cannot run code.  Unknown keys and mistyped values
are rejected with a message naming the file, the key and the problem.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tamias.router import EASY_TOOLS, RouterConfig

__all__ = ["ROUTER_CONFIG_KEYS", "load_router_config"]

ROUTER_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "big_output_chars",
        "easy_tools",
        "edit_tools",
        "error_markers",
        "min_gap",
        "shell_tools",
    }
)

_STRING_LIST_KEYS = ("easy_tools", "edit_tools", "shell_tools", "error_markers")


def load_router_config(path: str | Path) -> RouterConfig:
    """Read ``path`` and return the :class:`~tamias.router.RouterConfig` it describes.

    Only the keys in :data:`ROUTER_CONFIG_KEYS` are accepted.  ``easy_tools``
    adds exact names to v1's six; ``edit_tools`` and ``shell_tools`` add exact
    names to those families; ``error_markers``, ``big_output_chars`` and
    ``min_gap`` tune the rules.  Raises ``FileNotFoundError`` for a missing
    file and ``ValueError`` for anything else wrong with the file.
    """
    config_path = Path(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{config_path}: invalid TOML: {exc}") from exc

    unknown = sorted(set(raw) - ROUTER_CONFIG_KEYS)
    if unknown:
        allowed = ", ".join(sorted(ROUTER_CONFIG_KEYS))
        names = ", ".join(repr(key) for key in unknown)
        raise ValueError(f"{config_path}: unknown key(s) {names}; allowed keys: {allowed}")

    lists = {key: _string_list(raw, key, config_path) for key in _STRING_LIST_KEYS}
    return RouterConfig(
        easy_tools=EASY_TOOLS.union(lists["easy_tools"]),
        edit_tools=frozenset(lists["edit_tools"]),
        shell_tools=frozenset(lists["shell_tools"]),
        error_markers=tuple(lists["error_markers"]) if "error_markers" in raw else None,
        big_output_chars=_integer(raw, "big_output_chars", config_path, minimum=1, default=None),
        min_gap=_integer(raw, "min_gap", config_path, minimum=0, default=3),
    )


def _string_list(raw: dict[str, object], key: str, path: Path) -> list[str]:
    if key not in raw:
        return []
    value = raw[key]
    if not isinstance(value, list):
        raise ValueError(f"{path}: {key} must be an array of strings, got {type(value).__name__}")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"{path}: {key}[{index}] must be a string, got {type(item).__name__}"
            )
        if not item.strip():
            raise ValueError(f"{path}: {key}[{index}] must not be empty")
        items.append(item)
    return items


def _integer(
    raw: dict[str, object],
    key: str,
    path: Path,
    *,
    minimum: int,
    default: int | None,
) -> int | None:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path}: {key} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{path}: {key} must be at least {minimum}, got {value}")
    return value
