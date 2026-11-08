"""Provider catalogue: where a model runs, and how to authenticate there.

Tamias sits *on top of* providers; it is not one.  It ships no model of its own
and issues no credential of its own.  A client points at tamias and names a
model.  Tamias decides which configured provider serves that model, then
presents *that provider's* credential on the way out.  Adding a provider is a
config entry, not a code change.

Credentials are never stored by this package.  A provider entry names either an
environment variable (``api_key_env``) or a command that writes the secret to
stdout (``api_key_command``) -- the shape a real secret manager already offers::

    [[providers]]
    name = "openrouter"
    api_key_env = "OPENROUTER_API_KEY"

    [[providers]]
    name = "nvidia"
    api_key_command = "op read op://Private/NVIDIA/api-key"

Give both and the environment wins when it is set, with the command as the
fallback: a developer exports the variable, a service manager calls the vault,
and the same file serves both.

The secret is read once at start-up into a :class:`Credential`, whose ``repr``
and ``str`` are deliberately opaque, so a credential cannot reach a log line, an
error body or a decision reason.  A provider whose secret cannot be read is
reported as *unconfigured* and is skipped by routing rather than being tried and
failed -- the same rule the router applies to a rate-limited model.

``base_url`` and ``effort_style`` default from the built-in preset for a known
provider name; override either to point at something the preset does not know.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

PROVIDERS_ENV_VAR = "TAMIAS_PROVIDERS"

# The trailing version segment of an OpenAI-compatible root.  Tamias builds every
# wire URL from a *root* plus the wire's own versioned path, so a base URL that
# already carries ``/v1`` (``https://integrate.api.nvidia.com/v1``) and one that
# does not (``https://openrouter.ai/api``) both work.
_VERSION_SUFFIXES = ("/v1", "/api/v1")

# How a provider expects its secret to arrive.  ``bearer`` is the OpenAI-compatible
# default (``Authorization: Bearer <key>``); ``none`` is for a local runtime that
# authenticates nothing.
AUTH_STYLES = ("bearer", "none")

#: Headers that carry the *caller's* credential to tamias.  They are never a
#: credential for the downstream provider, so they are dropped before a
#: provider's own credential is applied.  ``authorization`` is the
#: OpenAI-compatible form; ``x-api-key`` is the Anthropic wire's form; the
#: others are common provider-specific spellings.
CLIENT_AUTH_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
    }
)

# How much of a provider's ``/v1/models`` answer to trust.  ``all`` (the default)
# takes every id the provider lists; ``listed`` takes only the ids the operator
# wrote in ``models``, because a catalogue sometimes advertises far more than a
# key can actually invoke -- NVIDIA's public list names 85 models and a dev key
# can call 14, so discovery alone would offer a routing pool that is mostly 404.
DISCOVERY_MODES = ("all", "listed")

# Presets cover the providers a normal deployment already has a key for.  They
# carry no credential and no model list: only where the API lives and which
# reasoning vocabulary it speaks.
BUILTIN_PROVIDERS: dict[str, dict[str, str]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api",
        "effort_style": "openrouter",
        "description": "OpenRouter (many models behind one key)",
    },
    "openai": {
        "base_url": "https://api.openai.com",
        "effort_style": "openai",
        "description": "OpenAI platform",
    },
    "opencode": {
        "base_url": "https://opencode.ai/zen",
        "effort_style": "openai",
        "description": "OpenCode Zen (free models; paid rates are not published)",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "effort_style": "openai",
        "description": "NVIDIA NIM / build.nvidia.com",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai",
        "effort_style": "openai",
        "description": "Groq",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "effort_style": "openai",
        "description": "DeepSeek platform",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai",
        "effort_style": "openai",
        "description": "Mistral platform",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "effort_style": "openai",
        "description": "local Ollama server (no credential)",
    },
}

_TOP_LEVEL_KEYS = ("providers",)
_PROVIDER_KEYS = (
    "name",
    "base_url",
    "api_key_env",
    "api_key_command",
    "auth",
    "effort_style",
    "models",
    "discover",
)


class ProviderError(ValueError):
    """The provider catalogue could not be read, parsed or validated."""


class Credential:
    """A provider secret that refuses to describe itself.

    The value is reachable only through :meth:`header`, so a credential cannot
    be interpolated into a log line, a decision reason or an error body by
    accident.  Both ``repr`` and ``str`` are opaque on purpose.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value.strip()

    def header(self) -> str:
        return f"Bearer {self._value}"

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Credential(<redacted>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        # Constant-time-ish identity comparison would be overkill here: this
        # compares two in-process objects, never a user-supplied guess.
        return isinstance(other, Credential) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class Provider:
    """One upstream a model can run on.  Never carries the secret itself."""

    name: str
    base_url: str
    effort_style: str = "openai"
    auth: str = "bearer"
    key_source: str = ""
    """Where the secret came from: ``env:VAR``, ``command``, or empty."""

    discover: str = "all"
    """``all`` takes every discovered id; ``listed`` takes only ``models``."""

    models: tuple[str, ...] = ()
    """Ids this provider claims, in the operator's order.  Also the allowlist
    when :attr:`discover` is ``listed``."""

    @property
    def root(self) -> str:
        """The base URL with any trailing version segment removed.

        Every wire URL is built as ``root + "/v1/<wire>"``, which keeps one
        convention for providers that publish ``.../v1`` and those that publish
        ``.../api``.
        """
        url = self.base_url.rstrip("/")
        for suffix in _VERSION_SUFFIXES:
            if url.endswith(suffix):
                return url[: -len(suffix)]
        return url

    @property
    def configured(self) -> bool:
        return self.auth == "none" or bool(self.key_source)

    def __repr__(self) -> str:
        # No secret is a field, so the dataclass repr is already safe; the
        # explicit form keeps ``key_source`` (env var *name*, not value) visible.
        return (
            f"Provider(name={self.name!r}, base_url={self.base_url!r}, "
            f"auth={self.auth!r}, key_source={self.key_source!r})"
        )


def default_providers_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    override = environ.get(PROVIDERS_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tamias" / "providers.toml"


def _run_command(command: str, *, timeout: float = 10.0) -> str:
    """Run a credential helper and return its stdout, stripped.

    A helper that fails, times out, or prints nothing yields ``""`` so the
    provider is treated as unconfigured.  Called once at start-up, never per
    request: a secret manager round-trip does not belong on the request path.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - the operator wrote the command
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


class ProviderCatalogue:
    """The configured providers, the credentials read for them, and model lookup."""

    def __init__(
        self,
        providers: Mapping[str, Provider] | None = None,
        credentials: Mapping[str, Credential] | None = None,
        model_providers: Mapping[str, str] | None = None,
        default: str | None = None,
    ) -> None:
        self._providers = dict(providers or {})
        self._credentials = dict(credentials or {})
        self._model_providers = {k: v for k, v in (model_providers or {}).items() if v}
        self._default = default

    def __len__(self) -> int:
        return len(self._providers)

    def __contains__(self, name: object) -> bool:
        return name in self._providers

    def get(self, name: str) -> Provider | None:
        return self._providers.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def all(self) -> tuple[Provider, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    def unconfigured(self) -> tuple[str, ...]:
        """Providers whose credential could not be read, in name order."""
        return tuple(
            name for name in self.names() if not self._providers[name].configured
        )

    def credential(self, name: str) -> Credential | None:
        return self._credentials.get(name)

    def provider_for(self, model: str) -> Provider | None:
        """The provider that should serve ``model``, or ``None``.

        Resolution is explicit, never a guess from the model id: an id like
        ``nvidia/nemotron`` names a *vendor* namespace on OpenRouter far more
        often than it names the NVIDIA platform, so prefix matching would
        silently bill the wrong account.  The order is an explicit mapping --
        the provider's own ``models`` list or the registry's ``provider``
        column -- then the single configured provider when there is exactly one.
        """
        mapped = self._model_providers.get(model)
        if mapped is not None:
            return self._providers.get(mapped)
        if self._default is not None:
            return self._providers.get(self._default)
        if len(self._providers) == 1:
            return next(iter(self._providers.values()))
        return None

    def provider_name_for(self, model: str) -> str:
        provider = self.provider_for(model)
        return provider.name if provider is not None else ""

    def apply_auth(self, provider_name: str, headers: dict[str, str]) -> dict[str, str]:
        """Return ``headers`` with the provider's credential in place.

        The caller's own credential is always dropped first.  That includes
        ``authorization`` and the Anthropic wire's ``x-api-key`` (plus the
        other common API-key spellings in :data:`CLIENT_AUTH_HEADERS`):
        whatever the client used to reach tamias is not a credential for the
        downstream provider, and forwarding it would leak the client's token
        to a third party.  A provider with no credential still gets the client
        credential stripped.
        """
        cleaned = {
            key: value
            for key, value in headers.items()
            if key.lower() not in CLIENT_AUTH_HEADERS
        }
        provider = self._providers.get(provider_name)
        if provider is None or provider.auth == "none":
            return cleaned
        credential = self._credentials.get(provider_name)
        if credential is None:
            return cleaned
        cleaned["authorization"] = credential.header()
        return cleaned


    def with_model_providers(self, mapping: Mapping[str, str]) -> ProviderCatalogue:
        """A copy whose model->provider map also carries ``mapping``."""
        merged = dict(self._model_providers)
        merged.update(mapping)
        return ProviderCatalogue(
            providers=self._providers,
            credentials=self._credentials,
            model_providers=merged,
            default=self._default,
        )


def empty_catalogue() -> ProviderCatalogue:
    return ProviderCatalogue()


def unknown_model_message(model: str, providers: tuple[str, ...]) -> str:
    """The one explanation for a model no configured provider serves.

    Every metered wire refuses this request, so the sentence is written once
    and wrapped in each wire's own error envelope.
    """
    known = ", ".join(providers) or "none"
    return (
        f"no configured provider serves model {model!r} "
        f"(providers: {known}). Add the provider to ~/.tamias/providers.toml "
        "or the model to ~/.tamias/models.toml."
    )


def _require_str(entry: dict, key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"{where}: {key!r} must be a non-empty string")
    return value.strip()


def _parse_provider(
    entry: object, index: int, seen: set[str]
) -> tuple[Provider, tuple[str, ...], str]:
    where = f"providers[{index}]"
    if not isinstance(entry, dict):
        raise ProviderError(f"{where}: each provider must be a table")

    for key in entry:
        if key not in _PROVIDER_KEYS:
            known = ", ".join(_PROVIDER_KEYS)
            raise ProviderError(f"{where}: unknown key {key!r} (known: {known})")

    name = _require_str(entry, "name", where)
    if name in seen:
        raise ProviderError(f"{where}: duplicate provider name {name!r}")

    preset = BUILTIN_PROVIDERS.get(name, {})
    base_url = entry.get("base_url", preset.get("base_url"))
    if not isinstance(base_url, str) or not base_url.strip():
        known = ", ".join(sorted(BUILTIN_PROVIDERS))
        raise ProviderError(
            f"{where}: provider {name!r} needs 'base_url' (known presets: {known})"
        )
    effort_style = entry.get("effort_style", preset.get("effort_style", "openai"))
    if not isinstance(effort_style, str) or not effort_style.strip():
        raise ProviderError(f"{where}: 'effort_style' must be a non-empty string")

    auth = entry.get("auth", "none" if name == "ollama" else "bearer")
    if auth not in AUTH_STYLES:
        known = ", ".join(AUTH_STYLES)
        raise ProviderError(f"{where}: unknown auth {auth!r} (known: {known})")

    api_key_env = entry.get("api_key_env", "")
    api_key_command = entry.get("api_key_command", "")
    for key, value in (("api_key_env", api_key_env), ("api_key_command", api_key_command)):
        if not isinstance(value, str):
            raise ProviderError(f"{where}: {key!r} must be a string")
    if auth == "bearer" and not (api_key_env or api_key_command):
        raise ProviderError(
            f"{where}: provider {name!r} authenticates with a bearer token, so it "
            "needs 'api_key_env' or 'api_key_command'"
        )

    models = entry.get("models", [])
    if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
        raise ProviderError(f"{where}: 'models' must be an array of strings")

    discover = entry.get("discover", "all")
    if discover not in DISCOVERY_MODES:
        known = ", ".join(DISCOVERY_MODES)
        raise ProviderError(f"{where}: unknown discover {discover!r} (known: {known})")
    if discover == "listed" and not models:
        raise ProviderError(
            f"{where}: discover = 'listed' needs a non-empty 'models' list, "
            "otherwise the provider would offer no models at all"
        )

    seen.add(name)
    provider = Provider(
        name=name,
        base_url=base_url.strip(),
        effort_style=effort_style.strip(),
        auth=auth,
        key_source="",
        discover=discover,
        models=tuple(model.strip() for model in models),
    )
    return provider, tuple(model.strip() for model in models), (api_key_env or api_key_command)


def load_providers(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    runner: Callable[[str], str] | None = None,
    missing_ok: bool = True,
) -> ProviderCatalogue:
    """Read a provider catalogue and resolve each credential once.

    A missing default file yields an empty catalogue (a single-upstream
    deployment is still valid); a missing file named explicitly raises.
    ``runner`` is injected by tests so no test ever shells out.
    """
    environ = os.environ if env is None else env
    run = _run_command if runner is None else runner
    target = Path(path) if path is not None else default_providers_path(environ)

    if not target.exists():
        if missing_ok:
            return empty_catalogue()
        raise ProviderError(f"provider file not found: {target}")

    try:
        with open(target, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ProviderError(f"could not parse providers {target}: {error}") from error
    except OSError as error:
        raise ProviderError(f"could not read providers {target}: {error}") from error

    if not isinstance(data, dict):
        raise ProviderError(f"{target}: providers document must be a table")
    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            raise ProviderError(f"{target}: unknown top-level key {key!r}")

    raw = data.get("providers", [])
    if not isinstance(raw, list):
        raise ProviderError(f"{target}: 'providers' must be an array of tables")

    providers: dict[str, Provider] = {}
    credentials: dict[str, Credential] = {}
    model_providers: dict[str, str] = {}
    seen: set[str] = set()

    for index, entry in enumerate(raw):
        parsed, models, key_source = _parse_provider(entry, index, seen)
        providers[parsed.name] = parsed
        for model in models:
            # First provider to claim a model wins: file order is the operator's
            # own precedence statement.
            model_providers.setdefault(model, parsed.name)
        if parsed.auth == "none":
            continue
        value = ""
        source = ""
        if entry.get("api_key_env"):
            variable = str(entry["api_key_env"]).strip()
            value = environ.get(variable, "").strip()
            if value:
                source = f"env:{variable}"
        if not value and entry.get("api_key_command"):
            value = run(str(entry["api_key_command"]).strip())
            if value:
                source = "command"
        if value:
            credentials[parsed.name] = Credential(value)
            providers[parsed.name] = Provider(
                name=parsed.name,
                base_url=parsed.base_url,
                effort_style=parsed.effort_style,
                auth=parsed.auth,
                key_source=source,
                discover=parsed.discover,
                models=parsed.models,
            )

    default = next(iter(providers), None) if len(providers) == 1 else None
    return ProviderCatalogue(
        providers=providers,
        credentials=credentials,
        model_providers=model_providers,
        default=default,
    )


def catalogue_from_registry(
    catalogue: ProviderCatalogue, registry: object
) -> ProviderCatalogue:
    """Merge a capability registry's ``provider`` column into the catalogue.

    The registry is the authority on which provider serves a model id that does
    not carry the provider as its own prefix (``deepseek/deepseek-v4.1-flash``
    served by ``openrouter``).  Registry facts win over prefix guessing.
    """
    mapping: dict[str, str] = {}
    getter = getattr(registry, "all", None)
    if callable(getter):
        for info in getter():
            provider = getattr(info, "provider", "")
            if isinstance(provider, str) and provider in catalogue:
                model = getattr(info, "id", "")
                if isinstance(model, str) and model:
                    mapping[model] = provider
    return catalogue.with_model_providers(mapping)
