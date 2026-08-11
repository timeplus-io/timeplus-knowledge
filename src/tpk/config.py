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
