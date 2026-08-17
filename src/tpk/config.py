"""Settings and corpus configuration.

Every non-secret setting is resolvable from BOTH the config TOML and an
environment variable, with one precedence rule everywhere (issue #54):

    environment variable  >  config-file value  >  built-in default

Secrets (TIMEPLUS_PASSWORD, ANTHROPIC_API_KEY, OPENAI_API_KEY, GITHUB_TOKEN)
are env-only and never read from the file. The config file is repos.toml (or
$TPK_CONFIG); its sections are [db], [agent], [llm], [server], and [repos.*].
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

EXTRACTION_MODES = ("code-only", "semantic")
LLM_BACKENDS = ("auto", "claude", "openai")


DB_BACKENDS = ("timeplusd", "proton")


def config_path() -> Path:
    """The config TOML: $TPK_CONFIG if set, else repos.toml at the repo root."""
    override = os.environ.get("TPK_CONFIG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "repos.toml"


def _config_data(toml_path: Path | None = None) -> dict:
    """Parse the config TOML (default: config_path()). Missing/invalid -> {}.
    Not cached: settings are resolved at startup and tests vary env/file."""
    p = toml_path or config_path()
    try:
        return tomllib.loads(p.read_text()) if p.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def setting(env_name: str, section: str, key: str, default, *,
            cast=str, toml_path: Path | None = None):
    """Resolve one setting: env var > config-file [section].key > default.

    Empty string counts as unset for both env and file. `cast` is applied to
    env/file values (which may be strings); `default` is returned as-is."""
    raw = os.environ.get(env_name)
    if raw not in (None, ""):
        return cast(raw)
    sec = _config_data(toml_path).get(section) or {}
    val = sec.get(key)
    if val not in (None, ""):
        return cast(val)
    return default


def as_bool(v) -> bool:
    """Cast an env/file value to bool: '0'/'false'/'no'/'off' -> False."""
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class Settings:
    host: str
    user: str
    password: str
    port: int = 8123
    stream_prefix: str = ""
    # DB stream-semantics mode: "timeplusd" (mutable streams; requires Timeplus
    # Enterprise) or "proton" (versioned_kv + soft-delete; runs on OSS proton
    # AND Enterprise, which supports the full proton feature set). See #50.
    backend: str = "timeplusd"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=setting("TIMEPLUS_HOST", "db", "host", "localhost"),
            user=setting("TIMEPLUS_USER", "db", "user", "default"),
            # password is a secret: env-only, never read from the config file.
            password=os.environ.get("TIMEPLUS_PASSWORD", ""),
            stream_prefix=setting("TPK_STREAM_PREFIX", "db", "stream_prefix", ""),
            backend=db_backend(),
        )


def db_backend() -> str:
    """The configured DB backend (env > [db].backend > default). Used by db.py
    helpers that need it without threading it through every signature."""
    backend = setting("TPK_DB_BACKEND", "db", "backend", "timeplusd")
    if backend not in DB_BACKENDS:
        raise ValueError(
            f"TPK_DB_BACKEND / [db].backend must be one of {DB_BACKENDS}, got {backend!r}"
        )
    return backend


def database() -> str:
    """The database all tpk streams live under (env > [db].database > 'tpk').
    tpk qualifies every stream as `<database>.<prefix><name>` (see #58) so its
    objects don't clutter the server's `default` database and can be granted /
    dropped as a unit. Read module-level (like db_backend) so db.py helpers can
    qualify names without threading it through every signature."""
    name = setting("TIMEPLUS_DATABASE", "db", "database", "tpk")
    if not name.replace("_", "").isalnum():
        raise ValueError(
            f"TIMEPLUS_DATABASE / [db].database must be alphanumeric/underscore, got {name!r}"
        )
    return name


def daily_token_limit() -> int:
    """Global fallback daily per-user token budget for non-admin chat (#62):
    env TPK_DAILY_TOKEN_LIMIT > [server].daily_token_limit > 0 (unlimited).
    Applies to users whose role does not set its own `daily_token_limit`."""
    val = setting("TPK_DAILY_TOKEN_LIMIT", "server", "daily_token_limit", 0, cast=int)
    return max(int(val), 0)


@dataclass(frozen=True)
class RepoConfig:
    name: str
    path: Path | None = None  # local checkout (dev mode)
    visibility: str = "internal"  # "internal" | "public"
    extraction: str = "code-only"  # "code-only" | "semantic"
    description: str = ""
    github: str = ""  # "org/repo" — fetched at `ref` into the checkout cache
    ref: str = ""  # tag / release / branch / SHA (required with github)
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.path is not None and not isinstance(self.path, Path):
            object.__setattr__(self, "path", Path(self.path))


def checkout_root() -> Path:
    """Cache directory for github-sourced repo checkouts (env > [server] > default)."""
    return Path(setting(
        "TPK_CHECKOUT_DIR", "server", "checkout_dir",
        str(Path.home() / ".tpk" / "checkouts"),
    )).expanduser()


def resolved_repo_path(cfg: RepoConfig) -> Path:
    """The tree ingest scans and read_source quotes from."""
    if cfg.github:
        return checkout_root() / cfg.name / cfg.ref.replace("/", "_")
    assert cfg.path is not None  # load_repos enforces path xor github
    return cfg.path


def repo_paths(repos: dict[str, RepoConfig]) -> dict[str, Path]:
    return {name: resolved_repo_path(cfg) for name, cfg in repos.items()}


def entry_key(cfg: RepoConfig) -> str:
    """Graph identity of a corpus entry: name@ref, or bare name for
    local-path entries (which have no ref)."""
    return f"{cfg.name}@{cfg.ref}" if cfg.ref else cfg.name


@dataclass(frozen=True)
class LLMConfig:
    backend: str = "auto"  # "auto" | "claude" | "openai"
    model: str = ""  # backend default when empty; else passed to graphify --model
    token_budget: int = 0  # 0 = graphify default; else --token-budget per chunk
    # (gateways like Bedrock reject oversized request bodies; ~16000 is safe)


def load_repos(toml_path: Path) -> dict[str, RepoConfig]:
    data = tomllib.loads(toml_path.read_text())
    repos: dict[str, RepoConfig] = {}
    for name, cfg in data["repos"].items():
        if "@" in name:
            raise ValueError(f"repo name {name!r} must not contain '@' (reserved for entry keys)")
        extraction = cfg.get("extraction", "code-only")
        if extraction not in EXTRACTION_MODES:
            raise ValueError(
                f"repo {name!r}: extraction must be one of {EXTRACTION_MODES}, got {extraction!r}"
            )
        github = cfg.get("github", "")
        path = cfg.get("path", "")
        if bool(github) == bool(path):
            raise ValueError(
                f"repo {name!r}: set exactly one of 'path' (local checkout) or "
                "'github' (org/repo fetched at 'ref')"
            )
        if github and not cfg.get("ref"):
            raise ValueError(f"repo {name!r}: 'github' requires a 'ref' (tag/branch)")
        repos[name] = RepoConfig(
            name=name,
            path=Path(path) if path else None,
            visibility=cfg["visibility"],
            extraction=extraction,
            description=cfg.get("description", ""),
            github=github,
            ref=cfg.get("ref", ""),
        )
    return repos


def load_llm(toml_path: Path) -> LLMConfig:
    data = tomllib.loads(toml_path.read_text())
    llm = data.get("llm", {})
    # Env overrides win over repos.toml so the extraction backend/model can be
    # set without rebuilding a baked-in repos.toml (e.g. the all-in-one image).
    # `auto` picks whichever API key is set, which is ambiguous when both are
    # exported -- set TPK_EXTRACTION_BACKEND=openai|claude to disambiguate.
    backend = os.environ.get("TPK_EXTRACTION_BACKEND") or llm.get("backend", "auto")
    if backend not in LLM_BACKENDS:
        raise ValueError(f"llm.backend must be one of {LLM_BACKENDS}, got {backend!r}")
    model = os.environ.get("TPK_EXTRACTION_MODEL") or llm.get("model", "")
    return LLMConfig(
        backend=backend,
        model=model,
        token_budget=int(llm.get("token_budget", 0)),
    )


AGENT_PROVIDERS = ("anthropic", "openai")


@dataclass(frozen=True)
class AgentConfig:
    provider: str  # "anthropic" | "openai"
    model: str

    @classmethod
    def from_env(cls) -> "AgentConfig":
        provider = setting("TPK_AGENT_PROVIDER", "agent", "provider", "")
        if provider and provider not in AGENT_PROVIDERS:
            raise ValueError(
                f"TPK_AGENT_PROVIDER / [agent].provider must be one of "
                f"{AGENT_PROVIDERS}, got {provider!r}"
            )
        if not provider:
            if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_BASE_URL"):
                provider = "anthropic"
            elif os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
                provider = "openai"
            else:
                raise ValueError(
                    "no agent LLM configured: set TPK_AGENT_PROVIDER (or "
                    "[agent].provider) plus ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL "
                    "or OPENAI_API_KEY/OPENAI_BASE_URL"
                )
        default_model = "claude-sonnet-5" if provider == "anthropic" else "gpt-5.2"
        return cls(
            provider=provider,
            model=setting("TPK_AGENT_MODEL", "agent", "model", default_model),
        )

    @classmethod
    def reasoning_effort(cls) -> str | None:
        """Agent reasoning effort (env > [agent].reasoning_effort > unset)."""
        return setting("TPK_AGENT_REASONING_EFFORT", "agent", "reasoning_effort", None)
