"""A fake GitHub API for the check. No test touches the network.

World.consistent() builds a train where every check passes; each test breaks one fact.
The version used here is a test value, never a real release version.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import check  # noqa: E402

VERSION = "9.8.7"


def sha(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()


class World(check.GitHub):
    def __init__(self) -> None:
        super().__init__(token=None)
        self.commits = {repo: sha(repo) for repo in (*check.TRAIN, *check.INPUTS)}
        self.default_branch: dict[str, str] = {}
        self.missing_repos: set[str] = set()
        self.compare: dict[str, str | None] = {}  # repo -> status, None = commit unknown
        self.files: dict[tuple[str, str], str] = {}
        self.check_runs: dict[str, list[dict]] = {}
        self.statuses: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}  # repo -> git ref object
        self.tag_objects: dict[str, dict] = {}
        self.errors: dict[str, str] = {}  # path prefix -> raise GitHubError
        self.calls: list[str] = []

    @classmethod
    def consistent(cls) -> World:
        w = cls()
        c = w.commits
        w.put(check.ENGINE, "pyproject.toml", f'[project]\nname = "chock"\nversion = "{VERSION}"\n')
        w.put(check.CATALOG, ".framework-ref", c[check.ENGINE] + "\n")
        w.put(check.CATALOG, "VERSION", VERSION + "\n")
        for repo in check.PLUGINS:
            w.put(
                repo,
                "generated_from.yaml",
                yaml.safe_dump({"generated_from": {"catalog": c[check.CATALOG], "engine": c[check.ENGINE]}}),
            )
        w.put(check.SITE, ".framework-ref", c[check.ENGINE] + "\n")
        w.put(check.SITE, ".catalog-ref", c[check.CATALOG] + "\n")
        w.put(check.SITE, "data/version.json", json.dumps({"version": VERSION}))
        w.put(
            "chock-quickstart",
            "README.md",
            f'pip install "chock @ git+https://github.com/open-coder-ai/chock@{c[check.ENGINE]}"\n',
        )
        w.put("chock-example", "requirements.txt", f"chock=={VERSION}\n")
        for repo in check.TRAIN:
            w.check_runs[repo] = [{"name": "test", "status": "completed", "conclusion": "success"}]
        return w

    def put(self, repo: str, path: str, text: str) -> None:
        self.files[(repo, path)] = text

    def manifest(self, **overrides) -> dict:
        data = {
            "version": VERSION,
            "repos": {r: {"commit": self.commits[r]} for r in check.TRAIN},
            "inputs": {r: {"commit": self.commits[r]} for r in check.INPUTS},
        }
        data.update(overrides)
        return data

    def get(self, path: str) -> object | None:
        self.calls.append(path)
        for prefix, msg in self.errors.items():
            if path.startswith(prefix):
                raise check.GitHubError(msg)
        prefix = f"/repos/{check.OWNER}/"
        assert path.startswith(prefix), path
        repo, _, rest = path[len(prefix) :].partition("/")
        if repo in self.missing_repos:
            return None
        if rest == "":
            return {"default_branch": self.default_branch.get(repo, "main")}
        if rest.startswith("compare/"):
            status = self.compare.get(repo, "behind")
            return None if status is None else {"status": status}
        if rest.startswith("contents/"):
            file, _, _query = rest[len("contents/") :].partition("?")
            text = self.files.get((repo, file))
            if text is None:
                return None
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if rest.startswith("commits/") and "/check-runs" in rest:
            runs = self.check_runs.get(repo, [])
            return {"total_count": len(runs), "check_runs": runs}
        if rest.startswith("commits/") and rest.endswith("/status"):
            return self.statuses.get(repo, {"state": "pending", "statuses": []})
        if rest.startswith("git/ref/tags/"):
            return self.tags.get(repo)
        if rest.startswith("git/tags/"):
            return self.tag_objects.get(rest[len("git/tags/") :])
        raise AssertionError(f"unexpected GET {path}")


@pytest.fixture
def world() -> World:
    return World.consistent()


@pytest.fixture
def run(tmp_path):
    """run(world, manifest_dict=None, name=None, version=VERSION) -> (manifest, result)."""

    def _run(
        world: World, data: dict | None = None, name: str | None = None, version: str | None = VERSION, ci: bool = True
    ):
        data = world.manifest() if data is None else data
        path = tmp_path / (name or f"{data.get('version', 'x')}.yaml")
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return check.check(path, world, version, ci=ci)

    return _run
