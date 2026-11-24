"""Refuse a test run next to a real backend/.env.

backend/.env holds the real Alpaca/Tastytrade credentials, the app token and the order
routing switch. A test run there can read real credentials and, through the app's own
settings writers, overwrite real settings (on 2026-11-24 a test run through a symlinked
.env switched order routing off for the live server). So tests refuse to run when:
- backend/.env is a symlink (a worktree pointing at the real file), or
- backend/.env sits in the repository's MAIN checkout (where .git is a directory).
A linked worktree (.git is a file) with its own standalone .env, or with none, is fine.
"""
from __future__ import annotations

from pathlib import Path


def env_problem(backend: Path) -> str | None:
    env = backend / ".env"
    if env.is_symlink():
        return (f"{env} is a symlink (to {env.resolve()}); tests could read or overwrite "
                "the real settings. Run tests in a worktree with its own .env, or none.")
    if env.exists() and (backend.parent / ".git").is_dir():
        return (f"{env} is the main checkout's real settings file. Run tests from a git "
                "worktree (git worktree add …) with its own .env, or none.")
    return None
