"""Paths and user configuration for the chronx store (~/.chronx)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

ENV_HOME = "CHRONX_HOME"

DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".chronx",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".cache",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".eggs",
        "target",
    }
)

DEFAULT_IGNORE_GLOBS: tuple[str, ...] = (
    "*.pyc",
    "*.pyo",
    "*.o",
    "*.so",
    "*.swp",
    "*.swx",
    "*~",
    ".DS_Store",
    "*.lock.tmp",
)

DEFAULT_CONFIG_TOML = """\
# chronx configuration (TOML). Reloaded when the daemon starts.

# Files larger than this (bytes) are never snapshotted.
max_file_size = 8388608

# How long (ms) the daemon waits after a command finishes before it
# collects that command's filesystem changes (lets fs events settle).
settle_ms = 200

# Refuse to track a working directory containing more than this many files.
max_files = 50000

# Refuse to track $HOME or / directly (subdirectories are fine).
allow_home_root = false

# Extra directory names to ignore anywhere in the tree.
extra_ignore_dirs = []

# Extra glob patterns (matched against file basenames) to ignore.
extra_ignore_globs = []
"""


@dataclass(frozen=True)
class Paths:
    """Filesystem layout of the chronx store."""

    home: Path

    @property
    def objects(self) -> Path:
        return self.home / "objects"

    @property
    def db(self) -> Path:
        return self.home / "chronx.db"

    @property
    def fifo(self) -> Path:
        return self.home / "daemon.fifo"

    @property
    def pidfile(self) -> Path:
        return self.home / "daemon.pid"

    @property
    def log(self) -> Path:
        return self.home / "daemon.log"

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(home=Path(os.environ.get(ENV_HOME, "~/.chronx")).expanduser())

    def ensure(self) -> None:
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.objects.mkdir(mode=0o700, exist_ok=True)


@dataclass(frozen=True)
class Config:
    """User-tunable behaviour, loaded from ~/.chronx/config.toml."""

    max_file_size: int = 8 * 1024 * 1024
    settle_ms: int = 200
    max_files: int = 50_000
    allow_home_root: bool = False
    ignore_dirs: frozenset[str] = field(default_factory=lambda: DEFAULT_IGNORE_DIRS)
    ignore_globs: tuple[str, ...] = field(default_factory=lambda: DEFAULT_IGNORE_GLOBS)

    @property
    def settle_seconds(self) -> float:
        return max(self.settle_ms, 0) / 1000.0

    def with_extra(
        self, dirs: frozenset[str], globs: tuple[str, ...]
    ) -> "Config":
        """A copy of this config with additional ignore rules merged in."""
        if not dirs and not globs:
            return self
        return replace(
            self,
            ignore_dirs=self.ignore_dirs | dirs,
            ignore_globs=self.ignore_globs + globs,
        )

    @classmethod
    def load(cls, paths: Paths) -> "Config":
        try:
            raw = tomllib.loads(paths.config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, tomllib.TOMLDecodeError):
            return cls()
        return cls(
            max_file_size=int(raw.get("max_file_size", cls.max_file_size)),
            settle_ms=int(raw.get("settle_ms", cls.settle_ms)),
            max_files=int(raw.get("max_files", cls.max_files)),
            allow_home_root=bool(raw.get("allow_home_root", cls.allow_home_root)),
            ignore_dirs=DEFAULT_IGNORE_DIRS
            | frozenset(str(d) for d in raw.get("extra_ignore_dirs", [])),
            ignore_globs=DEFAULT_IGNORE_GLOBS
            + tuple(str(g) for g in raw.get("extra_ignore_globs", [])),
        )


def load_root_ignore(root: Path) -> tuple[frozenset[str], tuple[str, ...]]:
    """Parse `<root>/.chronxignore` into (dir names, basename globs).

    One pattern per line; `#` starts a comment. Lines ending with `/` name
    directories to prune anywhere in the tree; anything else is a glob
    matched against file basenames.
    """
    dirs: set[str] = set()
    globs: list[str] = []
    try:
        text = (root / ".chronxignore").read_text(encoding="utf-8")
    except OSError:
        return frozenset(), ()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("/"):
            dirs.add(line.rstrip("/").strip())
        else:
            globs.append(line)
    return frozenset(d for d in dirs if d), tuple(globs)
