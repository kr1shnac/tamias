"""User configuration file and provider presets.

Tamias reads an optional TOML config file so common flags do not have to be
retyped on every ``serve``/``run``. The path is ``$TAMIAS_CONFIG`` when set,
else ``~/.tamias/config.toml``. A missing default file is not an error; a
missing file named explicitly with ``--config`` is.

Precedence for every value is **CLI flag > environment > config file >
built-in default**. Secrets (auth tokens) are deliberately not read from the
file: keep them in the environment.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_ENV_VAR = "TAMIAS_CONFIG"

# Presets map a friendly provider name to an OpenAI-compatible base URL (no
# trailing ``/v1/chat/completions``) and the request-field style used when the
# effort policy is active.
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openrouter": {
        "upstream": "https://openrouter.ai/api",
        "effort_style": "openrouter",
        "description": "OpenRouter (many models behind one key)",
    },
    "openai": {
        "upstream": "https://api.openai.com",
        "effort_style": "openai",
        "description": "OpenAI platform",
    },
    "ollama": {
        "upstream": "http://127.0.0.1:11434/v1",
        "effort_style": "openai",
        "description": "local Ollama server",
    },
}

CONFIG_KEYS = (
    "upstream",
    "provider",
    "prices",
    "db",
    "router_mode",
    "cheap_model",
    "strong_model",
    "effort_style",
    "effort_mode",
    "host",
    "port",
    "min_gap",
    "inject_usage",
)


class ConfigError(ValueError):
    """A config file could not be read, parsed or validated."""


@dataclass
class Config:
    """Values read from the config file; ``None`` means "not set here"."""

    path: Path | None = None
    upstream: str | None = None
    provider: str | None = None
    prices: str | None = None
    db: str | None = None
    router_mode: str | None = None
    cheap_model: str | None = None
    strong_model: str | None = None
    effort_style: str | None = None
    effort_mode: str | None = None
    host: str | None = None
    port: int | None = None
    min_gap: int | None = None
    inject_usage: bool | None = None

    def resolved_upstream(self) -> str | None:
        """The upstream from this file: explicit URL, else the provider preset."""
        if self.upstream:
            return self.upstream
        if self.provider:
            return preset_upstream(self.provider)
        return None


def preset_upstream(provider: str) -> str:
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        known = ", ".join(sorted(PROVIDER_PRESETS))
        raise ConfigError(f"unknown provider {provider!r} (known: {known})")
    return preset["upstream"]


def preset_names() -> tuple[str, ...]:
    return tuple(sorted(PROVIDER_PRESETS))


def default_config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tamias" / "config.toml"


def _coerce(key: str, value: object) -> object:
    if key in ("port", "min_gap"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key!r} must be an integer")
        return value
    if key == "inject_usage":
        if not isinstance(value, bool):
            raise ConfigError("'inject_usage' must be true or false")
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{key!r} must be a string")
    return value


def load_config(path: str | os.PathLike[str] | None, env: dict[str, str] | None = None) -> Config:
    """Read and validate the config file.

    ``path=None`` uses the default location and tolerates a missing file.
    An explicit path that does not exist is an error.
    """
    env = os.environ if env is None else env
    explicit = path is not None
    resolved = Path(path).expanduser() if explicit else default_config_path(env)

    if not resolved.exists():
        if explicit:
            raise ConfigError(f"config file not found: {resolved}")
        return Config(path=resolved)
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read config {resolved}: {exc}") from exc

    unknown = set(raw) - set(CONFIG_KEYS)
    if unknown:
        allowed = ", ".join(CONFIG_KEYS)
        raise ConfigError(
            f"unknown config key(s) {', '.join(sorted(unknown))} in {resolved} "
            f"(allowed: {allowed})"
        )

    cfg = Config(path=resolved)
    for key in CONFIG_KEYS:
        if key in raw:
            setattr(cfg, key, _coerce(key, raw[key]))

    if cfg.provider is not None and cfg.provider not in PROVIDER_PRESETS:
        known = ", ".join(preset_names())
        raise ConfigError(
            f"unknown provider {cfg.provider!r} in {resolved} (known: {known})"
        )
    return cfg


def pick(cli_value: object, config_value: object, default: object = None) -> object:
    """Precedence: an explicit CLI value (not ``None``/``""``) beats the config."""
    if cli_value is not None and cli_value != "":
        return cli_value
    if config_value is not None:
        return config_value
    return default
