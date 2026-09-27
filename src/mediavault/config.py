"""Per-machine configuration.

Every machine running mediavault has its own local config.yaml (git-ignored)
pointing at its own folder paths. Only config.example.yaml is shared via git.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_VIDEO_EXTENSIONS = [".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts"]


class ConfigError(RuntimeError):
    pass


@dataclass
class RootConfig:
    label: str
    path: Path
    role: str = "primary"  # primary | backup | icloud | inbox
    mode: str = "mirror"  # mirror | subset | watch

    @property
    def is_deletion_source(self) -> bool:
        """Whether a file missing from this root can be treated as evidence
        the file was deleted (as opposed to just not-yet-present/subset)."""
        return self.mode == "mirror"

    @classmethod
    def from_row(cls, row) -> "RootConfig":
        return cls(label=row["label"], path=Path(row["path"]), role=row["role"], mode=row["mode"])


@dataclass
class AppConfig:
    """Machine-level settings only. Which folders (roots) are tracked, and
    which one is the primary basis the others sync against, is *runtime*
    state stored in SQLite (see db.py) so it can be managed live from the
    CLI or the web/desktop UI without editing YAML by hand — config.yaml
    just bootstraps a fresh machine and holds settings that are inherently
    local to it (where the index db lives, which hasher to use, the TMDB
    key's env var name)."""

    machine: str
    db_path: Path
    hash_algo: str
    tmdb_api_key_env: str
    video_extensions: list[str]
    scan_interval_minutes: int
    seed_roots: list[RootConfig] = field(default_factory=list)
    config_path: Path | None = None

    @property
    def tmdb_api_key(self) -> str | None:
        return os.environ.get(self.tmdb_api_key_env) or None


def _expand(path_str: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path_str))).resolve()


def default_config_path() -> Path:
    env = os.environ.get("MEDIAVAULT_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd() / "config.yaml"


def load_config(path: Path | None = None) -> AppConfig:
    path = path or default_config_path()
    if not path.exists():
        raise ConfigError(
            f"Config file not found at {path}. Copy config.example.yaml to "
            f"config.yaml and edit it for this machine, or run `mediavault init`."
        )
    raw = yaml.safe_load(path.read_text()) or {}

    # `roots:` in config.yaml is only ever used as a one-time seed (see
    # db.seed_roots_from_config) — after that, roots live in SQLite and are
    # managed via the CLI/UI. Kept optional so a fresh machine can bootstrap
    # by copying config.example.yaml and filling in its own paths.
    seed_roots = [
        RootConfig(
            label=entry["label"],
            path=_expand(entry["path"]),
            role=entry.get("role", "primary"),
            mode=entry.get("mode", "mirror"),
        )
        for entry in raw.get("roots", [])
    ]

    db_path_str = raw.get("db_path", "~/.local/share/mediavault/mediavault.db")
    db_path = _expand(db_path_str)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        machine=raw.get("machine", os.uname().nodename if hasattr(os, "uname") else "unknown"),
        db_path=db_path,
        hash_algo=raw.get("hash_algo", "blake3"),
        tmdb_api_key_env=raw.get("tmdb_api_key_env", "TMDB_API_KEY"),
        video_extensions=raw.get("video_extensions", DEFAULT_VIDEO_EXTENSIONS),
        scan_interval_minutes=int(raw.get("scan_interval_minutes", 15)),
        seed_roots=seed_roots,
        config_path=path,
    )
