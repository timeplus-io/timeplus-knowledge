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
    path: Path
    visibility: str  # "internal" | "public"
    extraction: str = "code-only"  # "code-only" | "semantic"
    description: str = ""


@dataclass(frozen=True)
class LLMConfig:
    backend: str = "auto"  # "auto" | "claude" | "openai"
    model: str = ""  # backend default when empty; else passed to graphify --model


def load_repos(toml_path: Path) -> dict[str, RepoConfig]:
    data = tomllib.loads(toml_path.read_text())
    repos: dict[str, RepoConfig] = {}
    for name, cfg in data["repos"].items():
        extraction = cfg.get("extraction", "code-only")
        if extraction not in EXTRACTION_MODES:
            raise ValueError(
                f"repo {name!r}: extraction must be one of {EXTRACTION_MODES}, got {extraction!r}"
            )
        repos[name] = RepoConfig(
            name=name,
            path=Path(cfg["path"]),
            visibility=cfg["visibility"],
            extraction=extraction,
            description=cfg.get("description", ""),
        )
    return repos


def load_llm(toml_path: Path) -> LLMConfig:
    data = tomllib.loads(toml_path.read_text())
    llm = data.get("llm", {})
    backend = llm.get("backend", "auto")
    if backend not in LLM_BACKENDS:
        raise ValueError(f"llm.backend must be one of {LLM_BACKENDS}, got {backend!r}")
    return LLMConfig(backend=backend, model=llm.get("model", ""))


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
