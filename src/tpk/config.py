"""Environment settings and corpus (repos.toml) configuration."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

EXTRACTION_MODES = ("code-only", "semantic")
LLM_BACKENDS = ("auto", "claude", "openai")


@dataclass(frozen=True)
class Settings:
    host: str
    user: str
    password: str
    port: int = 8123
    stream_prefix: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("TIMEPLUS_HOST", "localhost"),
            user=os.environ.get("TIMEPLUS_USER", "default"),
            password=os.environ.get("TIMEPLUS_PASSWORD", ""),
        )


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
    """Cache directory for github-sourced repo checkouts."""
    return Path(
        os.environ.get("TPK_CHECKOUT_DIR", str(Path.home() / ".tpk" / "checkouts"))
    )


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
    backend = llm.get("backend", "auto")
    if backend not in LLM_BACKENDS:
        raise ValueError(f"llm.backend must be one of {LLM_BACKENDS}, got {backend!r}")
    return LLMConfig(
        backend=backend,
        model=llm.get("model", ""),
        token_budget=int(llm.get("token_budget", 0)),
    )


AGENT_PROVIDERS = ("anthropic", "openai")


@dataclass(frozen=True)
class AgentConfig:
    provider: str  # "anthropic" | "openai"
    model: str

    @classmethod
    def from_env(cls) -> "AgentConfig":
        provider = os.environ.get("TPK_AGENT_PROVIDER", "")
        if provider and provider not in AGENT_PROVIDERS:
            raise ValueError(
                f"TPK_AGENT_PROVIDER must be one of {AGENT_PROVIDERS}, got {provider!r}"
            )
        if not provider:
            if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_BASE_URL"):
                provider = "anthropic"
            elif os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
                provider = "openai"
            else:
                raise ValueError(
                    "no agent LLM configured: set TPK_AGENT_PROVIDER plus "
                    "ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL or "
                    "OPENAI_API_KEY/OPENAI_BASE_URL"
                )
        default_model = "claude-sonnet-5" if provider == "anthropic" else "gpt-5.2"
        return cls(provider=provider, model=os.environ.get("TPK_AGENT_MODEL") or default_model)
