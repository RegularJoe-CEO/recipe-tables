"""
Push a saved recipe to the user's GitHub repository.

Configuration (any of these sources, in order of precedence):
  1. Explicit arguments to push_recipe()
  2. Environment variables:
       GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH, GITHUB_PATH_PREFIX
  3. Values from a local .env file (python-dotenv)
  4. Settings saved in data/github_settings.json (written by the UI)

What gets pushed for each recipe:
  recipes/<slug>/recipe.md
  recipes/<slug>/recipe.json
  recipes/<slug>/recipe.pdf   (if available)

Uses PyGithub (Contents API) so no local git clone is required.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Load .env from project root if present
_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

_SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "data", "github_settings.json")


@dataclass
class GitHubConfig:
    token: str = ""
    repo: str = ""  # owner/name
    branch: str = "main"
    path_prefix: str = "recipes/"

    def is_configured(self) -> bool:
        return bool(self.token and self.repo and "/" in self.repo)


def load_settings() -> GitHubConfig:
    """Merge env + optional settings file."""
    file_cfg = {}
    if os.path.isfile(_SETTINGS_PATH):
        try:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        except Exception:
            file_cfg = {}

    return GitHubConfig(
        token=os.environ.get("GITHUB_TOKEN")
        or file_cfg.get("token")
        or "",
        repo=os.environ.get("GITHUB_REPO")
        or file_cfg.get("repo")
        or "your-username/recipe-tables-library",  # placeholder
        branch=os.environ.get("GITHUB_BRANCH")
        or file_cfg.get("branch")
        or "main",
        path_prefix=os.environ.get("GITHUB_PATH_PREFIX")
        or file_cfg.get("path_prefix")
        or "recipes/",
    )


def save_settings(cfg: GitHubConfig, persist_token: bool = False) -> None:
    """
    Save non-secret settings (and optionally the token) to data/github_settings.json.
    Prefer env vars / .env for the token in production use.
    """
    data = {
        "repo": cfg.repo,
        "branch": cfg.branch,
        "path_prefix": cfg.path_prefix,
    }
    if persist_token and cfg.token:
        data["token"] = cfg.token
    os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
    return s or "recipe"


@dataclass
class PushResult:
    ok: bool
    message: str
    urls: list[str]


def push_recipe_folder(
    folder_path: str,
    config: Optional[GitHubConfig] = None,
    commit_message: Optional[str] = None,
) -> PushResult:
    """
    Upload recipe.md / recipe.json / recipe.pdf from a library folder to GitHub.
    """
    cfg = config or load_settings()
    if not cfg.token or cfg.token.startswith("ghp_your_"):
        return PushResult(
            False,
            "GitHub token not configured. Set GITHUB_TOKEN in .env or the Settings panel.",
            [],
        )
    if not cfg.repo or "your-username" in cfg.repo:
        return PushResult(
            False,
            "GitHub repository not configured. Set GITHUB_REPO to owner/repo "
            "(e.g. janedoe/my-recipes) in .env or Settings.",
            [],
        )

    try:
        from github import Github, GithubException, InputGitTreeElement
    except ImportError:
        return PushResult(False, "PyGithub is not installed. Run: pip install PyGithub", [])

    # Read local files
    files = {}
    for name in ("recipe.md", "recipe.json", "recipe.pdf"):
        p = os.path.join(folder_path, name)
        if os.path.isfile(p):
            mode = "rb" if name.endswith(".pdf") else "r"
            with open(p, mode, encoding=None if mode == "rb" else "utf-8") as f:
                files[name] = f.read()

    if not files:
        return PushResult(False, f"No recipe files found in {folder_path}", [])

    # Determine remote path
    title = "recipe"
    meta_path = os.path.join(folder_path, "meta.json")
    if os.path.isfile(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
            title = meta.get("title") or title
    folder = os.path.basename(folder_path.rstrip("/\\"))
    prefix = cfg.path_prefix.strip("/")
    remote_dir = f"{prefix}/{folder}" if prefix else folder

    try:
        gh = Github(cfg.token)
        repo = gh.get_repo(cfg.repo)
        branch = cfg.branch or "main"
        msg = commit_message or f"Add recipe: {title}"
        urls = []

        for name, content in files.items():
            remote_path = f"{remote_dir}/{name}"
            if isinstance(content, str):
                content_bytes = content.encode("utf-8")
                is_binary = False
            else:
                content_bytes = content
                is_binary = True

            # Create or update
            try:
                existing = repo.get_contents(remote_path, ref=branch)
                if is_binary:
                    repo.update_file(
                        remote_path,
                        msg,
                        content_bytes,
                        existing.sha,
                        branch=branch,
                    )
                else:
                    repo.update_file(
                        remote_path,
                        msg,
                        content if isinstance(content, str) else content.decode("utf-8"),
                        existing.sha,
                        branch=branch,
                    )
            except GithubException as e:
                if e.status == 404:
                    if is_binary:
                        # PyGithub create_file expects str; for binary use base64 via API
                        repo.create_file(
                            remote_path,
                            msg,
                            content_bytes,
                            branch=branch,
                        )
                    else:
                        repo.create_file(
                            remote_path,
                            msg,
                            content if isinstance(content, str) else content.decode("utf-8"),
                            branch=branch,
                        )
                else:
                    raise

            urls.append(f"https://github.com/{cfg.repo}/blob/{branch}/{remote_path}")

        return PushResult(
            True,
            f"Pushed {len(files)} file(s) to {cfg.repo}@{branch}:{remote_dir}/",
            urls,
        )
    except Exception as e:
        return PushResult(False, f"GitHub push failed: {e}", [])


def test_connection(config: Optional[GitHubConfig] = None) -> PushResult:
    cfg = config or load_settings()
    if not cfg.token or not cfg.repo:
        return PushResult(False, "Token and repo are required.", [])
    try:
        from github import Github

        gh = Github(cfg.token)
        repo = gh.get_repo(cfg.repo)
        return PushResult(
            True,
            f"Connected to {repo.full_name} (default branch: {repo.default_branch})",
            [repo.html_url],
        )
    except Exception as e:
        return PushResult(False, f"Connection failed: {e}", [])
