"""A fake GitHub API for the check. No test touches the network.

World.consistent() builds a train where every check passes; each test breaks one fact.
The versions used here are test values, never real release versions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import urllib.parse
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import check  # noqa: E402

VERSION = "9.8.7"
AGENTSEAM_VERSION = "7.6.5"
ENGINE_PYPROJECT = (
    '[project]\nname = "chock"\nversion = "{version}"\n'
    'dependencies = ["pyyaml>=6.0", "agentseam=={agentseam}"]\n'
    "[project.optional-dependencies]\n"
    'dev = ["pytest>=7.0"]\n'
)


def sha(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()


def run_id(repo: str) -> int:
    return int(sha(repo)[:8], 16)


class World(check.GitHub):
    def __init__(self) -> None:
        super().__init__(token=None)
        self.commits = {repo: sha(repo) for repo in (*check.TRAIN, *check.INPUTS)}
        self.default_branch: dict[str, str] = {}
        self.missing_repos: set[str] = set()
        self.compare: dict[str, str | None] = {}  # repo -> status, None = commit unknown
        self.files: dict[tuple[str, str], str] = {}
        self.workflows: dict[str, dict[str, list[dict]]] = {}  # repo -> workflow file -> runs
        self.jobs: dict[int, list[dict]] = {}  # run id -> jobs
        self.statuses: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}  # repo -> git ref object
        self.tag_objects: dict[str, dict] = {}
        self.errors: dict[str, str] = {}  # path prefix -> raise GitHubError
        self.calls: list[str] = []

    @classmethod
    def consistent(cls) -> World:
        w = cls()
        c = w.commits
        w.put(check.ENGINE, "pyproject.toml", ENGINE_PYPROJECT.format(version=VERSION, agentseam=AGENTSEAM_VERSION))
        w.put(check.AGENTSEAM, "pyproject.toml", f'[project]\nname = "agentseam"\nversion = "{AGENTSEAM_VERSION}"\n')
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
        w.put(check.SITE, "data/agentseam-source/source.json", json.dumps({"agentseam_ref": c[check.AGENTSEAM]}))
        w.put(
            "chock-quickstart",
            "README.md",
            f'pip install "chock @ git+https://github.com/open-coder-ai/chock@{c[check.ENGINE]}"\n',
        )
        w.put("chock-example", "requirements.txt", f"chock=={VERSION}\n")
        for repo in (*check.TRAIN, *check.INPUTS):
            w.green(repo)
        return w

    def put(self, repo: str, path: str, text: str) -> None:
        self.files[(repo, path)] = text

    def green(self, repo: str, conclusion: str = "success", status: str = "completed") -> dict:
        """One completed run of the repo's required workflow on its commit, every required job green."""
        workflow, required = check.REQUIRED_WORKFLOWS[repo]
        run = self.add_run(repo, workflow, conclusion=conclusion, status=status)
        self.jobs[run["id"]] = [{"name": name, "status": status, "conclusion": conclusion} for name in required]
        return run

    def add_run(self, repo: str, workflow: str, created_at: str = "2026-01-01T00:00:00Z", **fields) -> dict:
        runs = self.workflows.setdefault(repo, {}).setdefault(workflow, [])
        run = {
            "id": run_id(repo) + len(runs),
            "run_number": len(runs) + 1,
            "head_sha": self.commits[repo],
            "head_branch": self.default_branch.get(repo, "main"),
            "status": "completed",
            "conclusion": "success",
            "created_at": created_at,
            "event": "push",
        }
        run.update(fields)
        runs.append(run)
        return run

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
            text = self.files.get((repo, urllib.parse.unquote(file)))
            if text is None:
                return None
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if rest.startswith("actions/workflows/"):
            workflow, _, query = rest[len("actions/workflows/") :].partition("/runs?")
            params = urllib.parse.parse_qs(query)
            runs = self.workflows.get(repo, {}).get(urllib.parse.unquote(workflow))
            if runs is None:
                return None
            runs = [
                r for r in runs if r["head_sha"] == params["head_sha"][0] and r["head_branch"] == params["branch"][0]
            ]
            return {"total_count": len(runs), "workflow_runs": list(reversed(runs))}
        if rest.startswith("actions/runs/") and "/jobs" in rest:
            jobs = self.jobs.get(int(rest.split("/")[2]), [])
            return {"total_count": len(jobs), "jobs": jobs}
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

    def _run(world: World, data: dict | None = None, name: str | None = None, version: str | None = VERSION):
        data = world.manifest() if data is None else data
        path = tmp_path / (name or f"{data.get('version', 'x')}.yaml")
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return check.check(path, world, version)

    return _run
