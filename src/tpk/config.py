"""Environment settings and corpus (repos.toml) configuration."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


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


def load_repos(toml_path: Path) -> dict[str, RepoConfig]:
    data = tomllib.loads(toml_path.read_text())
    return {
        name: RepoConfig(name=name, path=Path(cfg["path"]), visibility=cfg["visibility"])
        for name, cfg in data["repos"].items()
    }
