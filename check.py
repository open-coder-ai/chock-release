#!/usr/bin/env python3
"""Consistency check for one release train manifest (releases/<version>.yaml).

It refuses on any mismatch and lists every mismatch it finds, not only the first.
The numbered checks follow org-plan plan/release-1.md:

1. Every repo in the train is named, with a 40-hex commit on its default branch.
2. CI is green on every named commit, inputs included: the repo's required workflow
   ran on that commit on the default branch, concluded success, and every required job
   concluded success. A skipped required job is not green. Other check runs on the
   commit (scheduled sweeps, tag-triggered releases, code scanning) are not read.
3. The engine at its commit reports __version__ == version (pyproject.toml), none of
   its dependencies is a direct reference (PyPI rejects those), and its agentseam pin
   is the version agentseam's pyproject.toml reports at inputs.agentseam.
4. The catalog's .framework-ref equals the engine commit; its VERSION equals version.
5. Each plugin repo records the catalog and engine commits it was generated from.
6. The site's .framework-ref and .catalog-ref equal the manifest's; its version data
   equals version; its agentseam snapshot names inputs.agentseam.
7. quickstart and example pin the engine commit, or chock==version, and carry no
   other chock reference (a range, an unpinned install, a branch archive).
8. No v<version> tag exists yet in any train repo, except as allowed by 9.
9. An existing v<version> tag at the manifest commit passes; at any other commit, refuses.

Candidate mode (--candidate candidates/<version>-rc.<n>.yaml) runs the same code paths
against each repo's `rc` head instead of a manifest commit. Checks 1 (on `rc`), 2 and 6a
refuse; 3, 4a, 4b, 5, 6b and 7 warn: the version files until REL-5 lands, and the catalog's, the
plugins' and the quickstart and example engine pins because the owner's adopt-framework run,
REL-4's regeneration and REL-5 set them at release prep, never rc.py.
8 and 9 are skipped, because nothing is tagged, published or moved.
The catalog is merge-first (the engine refuses a catalog commit that is not on the catalog's main): in candidate
mode its "rc" is its base-branch head, every PR the candidate lists for it must be merged (refused with the PR
number otherwise), and the site's .catalog-ref must equal that head.
A required job that is also red on the repo's default branch is a warning ("known red"), not a refusal.
A GitHub API error reading an input repo (agentseam: recorded, never tagged) is a warning here, a
refusal in manifest mode, so a session whose scope lacks that repo can still read the candidate's verdict.

Credentials come only from the environment (GITHUB_TOKEN). They are never printed.

Exit codes: 0 consistent, 1 mismatches found, 2 usage error.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

OWNER = "open-coder-ai"

ENGINE = "chock"
CATALOG = "chock-catalog"
SITE = "chock-site"
PLUGINS = (
    "chock-claude-plugins",
    "chock-cursor-plugins",
    "chock-codex-plugins",
    "chock-copilot-plugins",
    "chock-devin-plugins",
)
PINNERS = ("chock-quickstart", "chock-example")
TRAIN = (ENGINE, CATALOG, *PLUGINS, SITE, *PINNERS)
# Repos with no `rc` branch: their candidate PRs are merged to the base branch first and the candidate pins the
# base head. The engine refuses a catalog commit that is not on the catalog's main ("commit <sha> is not on
# <url>'s main branch. Nothing was installed."), so a pin on a catalog `rc` head could never be installed.
MERGE_FIRST = (CATALOG,)

# Recorded by commit, never tagged: separate products with their own versions.
AGENTSEAM = "agentseam"
INPUTS = (AGENTSEAM,)

# Check 2 reads one workflow per repo, by file name, and the jobs in it that must conclude
# success on the manifest commit. It does not read every check run on the commit: the
# catalog's nightly sweep and drift jobs fail on main's head by design when derived files
# lag, a v<version> tag starts chock's own release.yml on the tagged commit, and either
# would block a release or a re-run. A matrix job matches its name prefix ("validate" matches
# "validate (ubuntu-latest, 3.12)"). A job the run skipped is not green.
#
# The plugin repos: publish.yml pushes with the built-in token, which starts no workflow run,
# so the owner dispatches generated-only.yml on main after a publish (RUNBOOK.md).
# quickstart and example: ci.yml with a `check` job is REL-1c's; until it lands they have no
# CI and the check refuses them, which is the fail-closed answer for an untested commit.
REQUIRED_WORKFLOWS: dict[str, tuple[str, tuple[str, ...]]] = {
    ENGINE: ("ci.yml", ("validate", "acceptance (tier 1)")),
    CATALOG: ("ci.yml", ("policies",)),
    **{repo: ("generated-only.yml", ("verify",)) for repo in PLUGINS},
    SITE: ("ci.yml", ("lint", "unit", "e2e")),
    **{repo: ("ci.yml", ("check",)) for repo in PINNERS},
    AGENTSEAM: ("ci.yml", ("lint", "test")),
}

# Where each repo keeps the facts the check reads. The prepare phase (REL-3 to REL-5)
# creates the files that do not exist yet; a missing file is a mismatch, not a pass.
ENGINE_VERSION_FILE = "pyproject.toml"  # [project].version is chock.__version__
AGENTSEAM_VERSION_FILE = "pyproject.toml"  # [project].version of agentseam
CATALOG_VERSION_FILE = "VERSION"  # one line: the train version
FRAMEWORK_REF_FILE = ".framework-ref"
CATALOG_REF_FILE = ".catalog-ref"
PLUGIN_PROVENANCE_FILE = "generated_from.yaml"  # generated_from: {catalog, engine}
SITE_VERSION_FILE = "data/version.json"  # {"version": ...}
SITE_AGENTSEAM_FILE = "data/agentseam-source/source.json"  # {"agentseam_ref": <commit>, ...}
PIN_FILES = ("README.md", "docs/README.md", "requirements.txt", "pyproject.toml")

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
CANDIDATE_NAME = re.compile(r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-rc\.([1-9]\d*)\.yaml")
CANDIDATE_BRANCH = "rc"
CANDIDATE_BASE = "main"
NOTHING_MOVED = "candidate: nothing is tagged, published or moved"
# A required job that concluded like this is red; anything else not green (queued, skipped) is not "known red".
RED_CONCLUSIONS = ("failure", "cancelled", "timed_out", "startup_failure")
SHA = re.compile(r"[0-9a-f]{40}")
PLACEHOLDER_SHA = "0" * 40
GIT_PIN = re.compile(rf"chock\s*@\s*git\+https://github\.com/{OWNER}/chock(?:\.git)?@([0-9A-Za-z._/-]+)")
ARCHIVE_PIN = re.compile(rf"chock\s*@\s*https://github\.com/{OWNER}/chock/archive/([0-9A-Za-z._/-]+)\.tar\.gz")
PYPI_PIN = re.compile(r"(?<![\w/-])chock(?:\[[^\]]*\])?\s*==\s*([0-9A-Za-z.+!*-]+)")
# A chock requirement with any operator but ==: a range floats to whatever PyPI serves later.
RANGE_PIN = re.compile(r"(?<![\w/-])chock(?:\[[^\]]*\])?\s*(?:===|~=|>=|<=|!=|<|>)\s*\d[^\s\"',;]*")
# `pip install chock` with no version and no URL. Best-effort: it reads pip's spelling only.
BARE_INSTALL = re.compile(r"pip\s+install\s+(?:-[-\w]+\s+)*[\"'`]?chock(?:\[[^\]]*\])?[\"'`]?(?!\s*@)(?=\s|$)", re.M)

# PEP 508 shapes in chock's own dependency list.
REQ_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
DIRECT_REF = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*(?:\[[^\]]*\])?\s*@")
AGENTSEAM_PIN = re.compile(r"^\s*agentseam\s*(?:\[[^\]]*\])?\s*==\s*([0-9A-Za-z.+!*-]+)\s*(?:;.*)?$", re.IGNORECASE)


def canonical_name(requirement: str) -> str | None:
    """The PEP 503 normalised project name of a requirement string, or None."""
    m = REQ_NAME.match(requirement)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else None


class GitHubError(Exception):
    """An API call failed for a reason other than 404."""


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another https:// URL. Anything else stays an HTTP error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            return None  # urllib then raises the 3xx as an HTTPError
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(HttpsOnlyRedirects())

# Everything that can escape urlopen() or the body read after it: URLError, TimeoutError and
# ConnectionResetError are OSErrors; IncompleteRead and BadStatusLine are HTTPExceptions.
NETWORK_ERRORS = (OSError, http.client.HTTPException, json.JSONDecodeError)


class GitHub:
    """Minimal GitHub REST client. get() returns parsed JSON, or None on 404; send() writes (rc.py prs, plugins.py)."""

    def __init__(self, token: str | None = None, api: str = "https://api.github.com"):
        self._token = token
        if not api.startswith("https://"):
            raise ValueError("the GitHub API base must be https")
        self._api = api.rstrip("/")

    def get(self, path: str) -> object | None:
        req = urllib.request.Request(self._api + path)  # noqa: S310 (https base, set in __init__)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "chock-release-check")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with OPENER.open(req, timeout=30) as resp:  # noqa: S310 (fixed https base)
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            # The message names the path and status only, never the request headers.
            raise GitHubError(f"GET {path}: HTTP {exc.code}") from None
        except NETWORK_ERRORS as exc:
            raise GitHubError(f"GET {path}: {type(exc).__name__}") from None

    def send(self, method: str, path: str, body: dict) -> object | None:
        """POST or PATCH <body> as JSON: parsed JSON, None for an empty body; HTTP errors name path and status."""
        if method not in ("POST", "PATCH"):
            raise ValueError("send takes POST or PATCH")
        req = urllib.request.Request(  # noqa: S310 (https base, set in __init__)
            self._api + path, data=json.dumps(body).encode(), method=method
        )
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "chock-release-check")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with OPENER.open(req, timeout=30) as resp:  # noqa: S310 (fixed https base)
                raw = resp.read()
                return json.loads(raw) if raw.strip() else None  # a workflow dispatch answers 204, empty
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"{method} {path}: HTTP {exc.code}") from None
        except NETWORK_ERRORS as exc:
            raise GitHubError(f"{method} {path}: {type(exc).__name__}") from None


class StrictLoader(yaml.SafeLoader):
    """yaml.SafeLoader that refuses a duplicate mapping key instead of keeping the last one."""

    def construct_mapping(self, node, deep=False):
        seen: set = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                continue  # unhashable: SafeLoader refuses it below
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml(text: str) -> object:
    return yaml.load(text, Loader=StrictLoader)  # noqa: S506 (StrictLoader is a SafeLoader)


@dataclass
class Manifest:
    version: str
    repos: dict[str, str]
    inputs: dict[str, str]

    def commit_of(self, repo: str) -> str | None:
        return self.repos.get(repo) or self.inputs.get(repo)


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # (repo, commit, state): state is "create" or "exists" for the v<version> tag.
    tags: list[tuple[str, str, str]] = field(default_factory=list)

    def refuse(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Candidate:
    version: str
    rc: int
    base: str
    prs: dict[str, list[int]]
    # repo -> path globs the candidate rewrites on `rc` after assembly (rc.py assemble may take a PR's side there)
    regenerated: dict[str, list[str]] = field(default_factory=dict)


def looks_like_candidate(path: Path) -> bool:
    """True for a candidate file by name or by shape (a top-level `base`); never raises."""
    if CANDIDATE_NAME.fullmatch(path.name):
        return True
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and "base" in data


def load_regenerated(raw: object, result: Result) -> dict[str, list[str]]:
    """The optional `regenerated:` mapping, repo to path globs; shape errors go to result."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        result.refuse("candidate: 'regenerated' must be a mapping of repo to a list of path globs")
        return {}
    globs: dict[str, list[str]] = {}
    for name, patterns in raw.items():
        if name not in TRAIN:
            result.refuse(f"candidate: regenerated.{name} is not in the train")
        elif not isinstance(patterns, list) or not all(isinstance(g, str) and g.strip() for g in patterns):
            result.refuse(f"candidate: regenerated.{name} must be a list of non-empty path globs, got {patterns!r}")
        else:
            globs[name] = patterns
    return globs


def load_candidate(path: Path, result: Result) -> Candidate | None:
    """Parse and validate candidates/<version>-rc.<n>.yaml. Shape errors go to result."""
    named = CANDIDATE_NAME.fullmatch(path.name)
    if named is None:
        result.refuse(f"candidate: file name {path.name} must be <MAJOR.MINOR.PATCH>-rc.<n>.yaml")
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        detail = f": {exc.problem}" if isinstance(exc, yaml.MarkedYAMLError) and exc.problem else ""
        result.refuse(f"candidate: cannot read {path.name}: {type(exc).__name__}{detail}")
        return None
    if not isinstance(data, dict):
        result.refuse("candidate: top level must be a mapping")
        return None
    for key in sorted(set(data) - {"version", "base", "repos", "regenerated"}, key=str):
        result.refuse(f"candidate: unknown top-level key '{key}'")
    version = data.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        result.refuse(f"candidate: version must be a MAJOR.MINOR.PATCH string, got {version!r}")
        version = ""
    elif named is not None and named.group(1) != version:
        result.refuse(f"candidate: file {path.name} does not match version {version!r}")
    if data.get("base") != CANDIDATE_BASE:
        result.refuse(f"candidate: base must be {CANDIDATE_BASE!r}, got {data.get('base')!r}")
    raw = data.get("repos")
    prs: dict[str, list[int]] = {}
    if not isinstance(raw, dict):
        result.refuse("candidate: 'repos' must be a mapping")
        raw = {}
    for name in TRAIN:
        if name not in raw:
            result.refuse(f"candidate: repos.{name} is missing (use [] for no PRs)")
            continue
        numbers = raw[name]
        if not isinstance(numbers, list) or not all(type(n) is int and n > 0 for n in numbers):
            result.refuse(f"candidate: repos.{name} must be a list of PR numbers, got {numbers!r}")
        elif len(set(numbers)) != len(numbers):
            result.refuse(f"candidate: repos.{name} lists a PR twice: {numbers!r}")
        else:
            prs[name] = numbers
    for name in sorted(set(raw) - set(TRAIN), key=str):
        result.refuse(f"candidate: repos.{name} is not in the train")
    regenerated = load_regenerated(data.get("regenerated"), result)
    if not result.ok or named is None:
        return None
    return Candidate(version=version, rc=int(named.group(2)), base=CANDIDATE_BASE, prs=prs, regenerated=regenerated)


def load_manifest(path: Path, result: Result) -> Manifest | None:
    """Parse and validate the manifest's shape. Shape errors go to result."""
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        detail = f": {exc.problem}" if isinstance(exc, yaml.MarkedYAMLError) and exc.problem else ""
        result.refuse(f"manifest: cannot read {path.name}: {type(exc).__name__}{detail}")
        return None
    if not isinstance(data, dict):
        result.refuse("manifest: top level must be a mapping")
        return None

    version = data.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        result.refuse(f"manifest: version must be a MAJOR.MINOR.PATCH string, got {version!r}")
        version = ""

    def commits(section: str, expected: tuple[str, ...]) -> dict[str, str]:
        raw = data.get(section)
        if not isinstance(raw, dict):
            result.refuse(f"manifest: '{section}' must be a mapping")
            return {}
        out: dict[str, str] = {}
        for name in expected:
            if name not in raw:
                result.refuse(f"check 1: {section}.{name} is missing from the manifest")
                continue
            entry = raw[name]
            if not isinstance(entry, dict):
                result.refuse(f"check 1: {section}.{name} must be a mapping with a commit, got {entry!r}")
                continue
            for key in sorted(set(entry) - {"commit"}):
                result.refuse(f"check 1: {section}.{name} has an unknown key '{key}'")
            commit = entry.get("commit")
            if not isinstance(commit, str) or not SHA.fullmatch(commit):
                result.refuse(f"check 1: {section}.{name}.commit must be 40 lowercase hex, got {commit!r}")
            elif commit == PLACEHOLDER_SHA:
                result.refuse(f"check 1: {section}.{name}.commit is the placeholder {PLACEHOLDER_SHA}")
            else:
                out[name] = commit
        for name in sorted(set(raw) - set(expected)):
            result.refuse(f"check 1: {section}.{name} is not in the train")
        return out

    repos = commits("repos", TRAIN)
    inputs = commits("inputs", INPUTS)
    for key in sorted(set(data) - {"version", "repos", "inputs"}):
        result.refuse(f"manifest: unknown top-level key '{key}'")
    return Manifest(version=version, repos=repos, inputs=inputs)


# Returned by Checker._get when the API call failed: the failure is already recorded, so the
# caller says nothing more. Distinct from None, which is a real 404.
API_ERROR = object()


class Checker:
    def __init__(
        self,
        gh: GitHub,
        manifest: Manifest,
        result: Result,
        candidate: bool = False,
        merge_first: dict[str, list[int]] | None = None,
    ):
        self.gh = gh
        self.m = manifest
        self.r = result
        self.candidate = candidate
        self.merge_first = merge_first or {}  # repo -> PR numbers that must already be merged (candidate mode)

    def _refuse(self, msg: str, soft: bool = False) -> None:
        """Refuse; in candidate mode a soft finding (the version files and release-prep pins) is a warning."""
        if soft and self.candidate:
            self.r.warn(msg)
        else:
            self.r.refuse(msg)

    def _get(self, repo: str, path: str) -> object | None:
        """GET /repos/OWNER/<repo><path>: parsed JSON, None on 404, API_ERROR (recorded) on failure."""
        try:
            return self.gh.get(f"/repos/{OWNER}/{repo}{path}")
        except GitHubError as exc:
            if self.candidate and repo in INPUTS:  # recorded, never tagged: a session may lack the scope to read it
                self.r.warn(f"{repo}: GitHub API error: {exc}")
            else:
                self.r.refuse(f"{repo}: GitHub API error: {exc}")
            return API_ERROR

    def _pages(self, repo: str, path: str, key: str) -> list[dict] | None:
        """Every item of a paginated list endpoint, or None (recorded) if it cannot be read."""
        items: list[dict] = []
        page = 1
        while True:
            data = self._get(repo, f"{path}?per_page=100&page={page}")
            if data is API_ERROR:
                return None
            if not isinstance(data, dict) or not isinstance(data.get(key), list):
                self.r.refuse(f"{repo}: cannot read {path}")
                return None
            batch = data[key]
            items.extend(batch)
            if len(items) >= int(data.get("total_count", 0)) or not batch:
                return items
            page += 1

    def _file(self, repo: str, path: str, soft: bool = False) -> str | None:
        """Text of <path> at the manifest commit of <repo>, or None (recorded) if absent."""
        commit = self.m.commit_of(repo)
        if commit is None:
            return None  # check 1 already refused the missing commit
        quoted = urllib.parse.quote(path)
        data = self._get(repo, f"/contents/{quoted}?ref={commit}")
        if data is API_ERROR:
            return None
        if data is None:
            self._refuse(f"{repo}: {path} not found at {commit[:12]}", soft)
            return None
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            self._refuse(f"{repo}: {path} at {commit[:12]} is not a file", soft)
            return None
        try:
            return base64.b64decode(data.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            self._refuse(f"{repo}: {path} at {commit[:12]} is not UTF-8 text", soft)
            return None

    def _ref(self, repo: str, path: str, soft: bool = False) -> str | None:
        text = self._file(repo, path, soft)
        return None if text is None else text.strip()

    def _project(self, repo: str, path: str, soft: bool = False) -> dict | None:
        """The [project] table of a pyproject.toml, or None (recorded)."""
        text = self._file(repo, path, soft)
        if text is None:
            return None
        try:
            project = tomllib.loads(text)["project"]
        except (tomllib.TOMLDecodeError, KeyError, TypeError):
            self._refuse(f"check 3: {repo}: {path} has no [project] table", soft)
            return None
        if not isinstance(project, dict):
            self._refuse(f"check 3: {repo}: {path} has no [project] table", soft)
            return None
        return project

    # 1
    def default_branch(self, repo: str) -> str | None:
        """The repo's default branch name, or None (recorded)."""
        info = self._get(repo, "")
        if info is API_ERROR:
            return None
        if not isinstance(info, dict) or not isinstance(info.get("default_branch"), str):
            self.r.refuse(f"check 1: {repo}: repository not found")
            return None
        return info["default_branch"]

    def on_default_branch(self, repo: str, commit: str) -> str | None:
        """The default branch name when <commit> is on it, else None (recorded)."""
        branch = self.default_branch(repo)
        if branch is None:
            return None
        cmp = self._get(repo, f"/compare/{urllib.parse.quote(branch)}...{commit}")
        if cmp is API_ERROR:
            return None
        if cmp is None:
            self.r.refuse(f"check 1: {repo}: commit {commit} does not exist")
            return None
        # base...head: "behind" or "identical" means head is an ancestor of base.
        if not isinstance(cmp, dict) or cmp.get("status") not in ("behind", "identical"):
            status = cmp.get("status") if isinstance(cmp, dict) else None
            self.r.refuse(f"check 1: {repo}: commit {commit[:12]} is not on {branch} (compare: {status})")
            return None
        return branch

    # 2
    def ci_problems(self, repo: str, commit: str, branch: str) -> list[tuple[str, frozenset[str] | None]]:
        """Why CI is not green on <commit> on <branch>, as (message, red): `red` names the failed jobs
        (or "status"), and is None for a problem that no known-red excuse can cover (never ran, queued)."""
        workflow, required = REQUIRED_WORKFLOWS[repo]
        query = urllib.parse.urlencode({"head_sha": commit, "branch": branch, "per_page": 100})
        data = self._get(repo, f"/actions/workflows/{urllib.parse.quote(workflow)}/runs?{query}")
        if data is API_ERROR:
            return []
        if data is None:
            return [(f"check 2: {repo}: workflow {workflow} does not exist", None)]
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            return [(f"check 2: {repo}: cannot read runs of {workflow}", None)]
        runs = [
            r for r in runs if isinstance(r, dict) and r.get("head_sha") == commit and r.get("head_branch") == branch
        ]
        if not runs:
            hint = " (open the rc draft PR: rc.py prs)" if self.candidate and branch == CANDIDATE_BRANCH else ""
            return [(f"check 2: {repo}: {workflow} never ran on {commit[:12]} on {branch}{hint}", None)]
        # The newest run is the one that counts: a re-run after a fix supersedes the failure.
        run = max(runs, key=lambda r: (str(r.get("created_at", "")), int(r.get("run_number") or 0)))
        run_id = run.get("id")
        where = f"{workflow} run {run_id} on {commit[:12]}"
        if run.get("status") != "completed":
            return [(f"check 2: {repo}: {where} is {run.get('status')}", None)]
        problems: list[tuple[str, frozenset[str] | None]] = []
        run_bad = run.get("conclusion") != "success"
        concluded = f"check 2: {repo}: {where} concluded {run.get('conclusion')}"
        jobs = self._pages(repo, f"/actions/runs/{run_id}/jobs", "jobs") if isinstance(run_id, int) else None
        if jobs is None:
            if run_bad:
                problems.append((concluded, None))
            if not isinstance(run_id, int):
                problems.append((f"check 2: {repo}: {where} has no id", None))
            return problems
        if run_bad:
            failed = frozenset(str(j.get("name")) for j in jobs if j.get("conclusion") in RED_CONCLUSIONS)
            problems.append((concluded, failed or None))
        for name in required:
            matching = [j for j in jobs if j.get("name") == name or str(j.get("name", "")).startswith(name + " (")]
            if not matching:
                problems.append((f"check 2: {repo}: required job '{name}' is not in {where}", None))
            for job in matching:
                job_name = job.get("name")
                if job.get("status") != "completed":
                    problems.append(
                        (f"check 2: {repo}: required job '{job_name}' is {job.get('status')} in {where}", None)
                    )
                elif job.get("conclusion") != "success":
                    verdict = job.get("conclusion")
                    red = frozenset({str(job_name)}) if verdict in RED_CONCLUSIONS else None
                    problems.append((f"check 2: {repo}: required job '{job_name}' concluded {verdict} in {where}", red))
        status = self._get(repo, f"/commits/{commit}/status")
        if isinstance(status, dict) and status.get("statuses") and status.get("state") != "success":
            problems.append(
                (f"check 2: {repo}: commit status is {status.get('state')} on {commit[:12]}", frozenset({"status"}))
            )
        return problems

    def ci_green(self, repo: str, commit: str, branch: str) -> None:
        problems = self.ci_problems(repo, commit, branch)
        known: frozenset[str] = frozenset()
        if self.candidate and branch == CANDIDATE_BRANCH and any(red for _, red in problems):
            known = self.red_on_default_branch(repo)
        for msg, red in problems:
            if red is not None and red <= known:
                self.r.warn(f"known red: {msg} (the same job is red on the default branch)")
            else:
                self.r.refuse(msg)

    def red_on_default_branch(self, repo: str) -> frozenset[str]:
        """Every job (or "status") that is red on the repo's default branch head; empty if unreadable."""
        scratch = Checker(self.gh, self.m, Result())  # its own errors are not the candidate's
        branch = scratch.default_branch(repo)
        if branch is None:
            return frozenset()
        ref = scratch._get(repo, f"/git/ref/heads/{urllib.parse.quote(branch)}")
        head = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        if not isinstance(head, str):
            return frozenset()
        return frozenset().union(*(red for _, red in scratch.ci_problems(repo, head, branch) if red))

    def rc_equals_default(self, repo: str, commit: str) -> str | None:
        """The default branch name when <commit> is that branch's head too (a repo with no candidate PRs), else None."""
        scratch = Checker(self.gh, self.m, Result())  # its own errors are not the candidate's
        branch = scratch.default_branch(repo)
        if branch is None:
            return None
        ref = scratch._get(repo, f"/git/ref/heads/{urllib.parse.quote(branch)}")
        head = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        return branch if head == commit else None

    # 3
    def engine_version(self) -> None:
        project = self._project(ENGINE, ENGINE_VERSION_FILE, soft=True)
        if project is None:
            return
        got = project.get("version")
        if not isinstance(got, str):
            self._refuse(f"check 3: {ENGINE}: {ENGINE_VERSION_FILE} has no [project].version", soft=True)
        elif got != self.m.version:
            self._refuse(f"check 3: {ENGINE}: __version__ is {got!r}, manifest says {self.m.version!r}", soft=True)
        self.engine_dependencies(project)

    def engine_dependencies(self, project: dict) -> None:
        """No direct reference anywhere, and agentseam pinned to the version at inputs.agentseam."""
        deps: list[object] = list(project.get("dependencies") or [])
        extras = project.get("optional-dependencies") or {}
        for group in extras.values() if isinstance(extras, dict) else []:
            deps.extend(group or [])
        pins: list[str] = []
        for dep in deps:
            if not isinstance(dep, str):
                self._refuse(f"check 3: {ENGINE}: dependency {dep!r} is not a string", soft=True)
                continue
            if DIRECT_REF.match(dep):
                self._refuse(
                    f"check 3: {ENGINE}: dependency {dep!r} is a direct reference, which PyPI rejects", soft=True
                )
                continue
            if canonical_name(dep) != AGENTSEAM:
                continue
            m = AGENTSEAM_PIN.match(dep)
            if m is None:
                self._refuse(f"check 3: {ENGINE}: dependency {dep!r} does not pin agentseam==<version>", soft=True)
            else:
                pins.append(m.group(1))
        if not pins:
            self._refuse(f"check 3: {ENGINE}: {ENGINE_VERSION_FILE} pins no agentseam==<version>", soft=True)
            return
        if len(set(pins)) > 1:
            self._refuse(f"check 3: {ENGINE}: {ENGINE_VERSION_FILE} pins agentseam to {sorted(set(pins))}", soft=True)
            return
        pin = pins[0]
        if AGENTSEAM not in self.m.inputs:
            return  # check 1 already refused the missing input
        upstream = self._project(AGENTSEAM, AGENTSEAM_VERSION_FILE, soft=True)
        if upstream is None:
            return
        got = upstream.get("version")
        if got != pin:
            commit = self.m.inputs[AGENTSEAM][:12]
            self._refuse(
                f"check 3: {AGENTSEAM}: version is {got!r} at {commit}, {ENGINE} pins agentseam=={pin}", soft=True
            )

    # 4
    def catalog(self) -> None:
        engine = self.m.repos.get(ENGINE)
        ref = self._ref(CATALOG, FRAMEWORK_REF_FILE, soft=True)
        if ref is not None and ref != engine:
            self._refuse(f"check 4: {CATALOG}: {FRAMEWORK_REF_FILE} is {ref!r}, engine commit is {engine!r}", soft=True)
        version = self._ref(CATALOG, CATALOG_VERSION_FILE, soft=True)
        if version is not None and version != self.m.version:
            self._refuse(
                f"check 4: {CATALOG}: {CATALOG_VERSION_FILE} is {version!r}, manifest says {self.m.version!r}",
                soft=True,
            )

    # 5
    def plugin(self, repo: str) -> None:
        text = self._file(repo, PLUGIN_PROVENANCE_FILE, soft=True)
        if text is None:
            return
        try:
            data = load_yaml(text)
            gen = data["generated_from"]
            recorded = {"catalog": gen.get("catalog"), "engine": gen.get("engine")}
        except (yaml.YAMLError, KeyError, TypeError, AttributeError):
            self._refuse(f"check 5: {repo}: {PLUGIN_PROVENANCE_FILE} has no generated_from mapping", soft=True)
            return
        for key, source in (("catalog", CATALOG), ("engine", ENGINE)):
            want = self.m.repos.get(source)
            if recorded[key] != want:
                self._refuse(
                    f"check 5: {repo}: generated_from.{key} is {recorded[key]!r}, manifest {source} is {want!r}",
                    soft=True,
                )

    # 6
    def site(self) -> None:
        for path, source in ((FRAMEWORK_REF_FILE, ENGINE), (CATALOG_REF_FILE, CATALOG)):
            ref = self._ref(SITE, path)
            want = self.m.repos.get(source)
            if ref is not None and ref != want:
                self.r.refuse(f"check 6: {SITE}: {path} is {ref!r}, manifest {source} is {want!r}")
        for path, key, want, label in (
            (SITE_VERSION_FILE, "version", self.m.version, "version"),
            (SITE_AGENTSEAM_FILE, "agentseam_ref", self.m.inputs.get(AGENTSEAM), f"manifest {AGENTSEAM}"),
        ):
            if want is None and key == "agentseam_ref":
                continue  # no recorded input to compare with: check 1 already reported why
            text = self._file(SITE, path, soft=True)
            if text is None:
                continue
            try:
                got = json.loads(text)[key]
            except (json.JSONDecodeError, KeyError, TypeError):
                self._refuse(f"check 6: {SITE}: {path} has no {key}", soft=True)
                continue
            if got != want:
                self._refuse(f"check 6: {SITE}: {path} {key} is {got!r}, {label} is {want!r}", soft=True)

    # 7
    def pins(self, repo: str) -> None:
        engine = self.m.repos.get(ENGINE)
        commit = self.m.repos[repo]
        found = 0
        unread = 0
        for path in PIN_FILES:
            data = self._get(repo, f"/contents/{urllib.parse.quote(path)}?ref={commit}")
            if data is API_ERROR:
                unread += 1
                continue
            if not isinstance(data, dict) or data.get("encoding") != "base64":
                continue
            try:
                text = base64.b64decode(data.get("content", "")).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                self._refuse(f"check 7: {repo}: {path} is not UTF-8 text", soft=True)
                continue
            for pattern, kind in ((GIT_PIN, "commit"), (ARCHIVE_PIN, "commit"), (PYPI_PIN, "version")):
                for match in pattern.finditer(text):
                    found += 1
                    got = match.group(1)
                    want = engine if kind == "commit" else self.m.version
                    if got != want:
                        self._refuse(f"check 7: {repo}: {path} pins chock {kind} {got!r}, want {want!r}", soft=True)
            for match in RANGE_PIN.finditer(text):
                found += 1
                self._refuse(f"check 7: {repo}: {path} has {match.group(0)!r}, want chock=={self.m.version}", soft=True)
            for match in BARE_INSTALL.finditer(text):
                found += 1
                self._refuse(f"check 7: {repo}: {path} has {match.group(0)!r}, which is unpinned", soft=True)
        if found == 0 and unread == 0:
            self._refuse(f"check 7: {repo}: no chock pin found in {', '.join(PIN_FILES)}", soft=True)

    # 8, 9
    def tag(self, repo: str, commit: str) -> None:
        name = f"v{self.m.version}"
        ref = self._get(repo, f"/git/ref/tags/{name}")
        if ref is API_ERROR:
            return
        if ref is None:
            self.r.tags.append((repo, commit, "create"))
            return
        obj = ref.get("object", {}) if isinstance(ref, dict) else {}
        target = obj.get("sha")
        if obj.get("type") == "tag":  # annotated: peel to the commit
            tag = self._get(repo, f"/git/tags/{target}")
            if tag is API_ERROR:
                return
            target = tag.get("object", {}).get("sha") if isinstance(tag, dict) else None
        if target == commit:
            self.r.tags.append((repo, commit, "exists"))
        else:
            self.r.refuse(f"check 9: {repo}: tag {name} already exists at {target!r}, manifest commit is {commit!r}")

    # 1, candidate mode: the commit is the head of `rc` by construction; main moving under it is a warning.
    def main_moved(self, repo: str, commit: str) -> None:
        branch = self.default_branch(repo)
        if branch is None:
            return
        cmp = self._get(repo, f"/compare/{urllib.parse.quote(branch)}...{commit}")
        status = cmp.get("status") if isinstance(cmp, dict) else None
        if cmp is not API_ERROR and status not in ("ahead", "identical"):
            self.r.warn(f"check 1: {repo}: {branch} has moved under {CANDIDATE_BRANCH} (compare: {status})")

    def merged(self, repo: str, numbers: list[int]) -> None:
        """Check 1, merge-first repo: each listed PR is merged into the base branch the commit was read from."""
        base = self.default_branch(repo)
        for n in numbers:
            pr = self._get(repo, f"/pulls/{n}")
            if pr is API_ERROR:
                continue
            if not isinstance(pr, dict):
                self.r.refuse(f"check 1: {repo}: PR #{n} not found")
            elif not pr.get("merged"):
                self.r.refuse(f"check 1: {repo}: PR #{n} is not merged ({pr.get('state')}); {repo} is merge-first")
            elif base is not None and pr.get("base", {}).get("ref") != base:
                self.r.refuse(f"check 1: {repo}: PR #{n} was merged into {pr.get('base', {}).get('ref')!r}, not {base}")

    def run(self) -> None:
        for repo, commit in {**self.m.repos, **self.m.inputs}.items():
            if self.candidate and repo in self.merge_first:
                branch = self.on_default_branch(repo, commit)  # its candidate head is its base head: no rc to read
                self.merged(repo, self.merge_first[repo])
            elif self.candidate and repo in self.m.repos:
                self.main_moved(repo, commit)
                branch: str | None = CANDIDATE_BRANCH
                same = self.rc_equals_default(repo, commit)
                if same is not None:  # no candidate PR: no rc PR can run CI, so read the runs on the default branch
                    branch = same
                    self.r.warn(
                        f"check 2: {repo}: {CANDIDATE_BRANCH} equals {same} at {commit[:12]}, CI read on {same}"
                    )
            else:
                branch = self.on_default_branch(repo, commit)
            if branch is not None:
                self.ci_green(repo, commit, branch)
        if not self.m.version:
            return  # every remaining check compares against the version
        if ENGINE in self.m.repos:
            self.engine_version()
        if CATALOG in self.m.repos:
            self.catalog()
        for repo in PLUGINS:
            if repo in self.m.repos:
                self.plugin(repo)
        if SITE in self.m.repos:
            self.site()
        for repo in PINNERS:
            if repo in self.m.repos:
                self.pins(repo)
        if self.candidate:
            return  # checks 8 and 9: nothing is tagged, so there is no tag to read
        for repo, commit in self.m.repos.items():
            self.tag(repo, commit)


def ref_head(gh: GitHub, repo: str, branch: str, result: Result, soft: bool = False) -> str | None:
    """The commit a branch points at, or None (recorded; a warning when soft)."""
    try:
        ref = gh.get(f"/repos/{OWNER}/{repo}/git/ref/heads/{urllib.parse.quote(branch)}")
    except GitHubError as exc:
        (result.warn if soft else result.refuse)(f"{repo}: GitHub API error: {exc}")
        return None
    sha = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        result.refuse(f"check 1: {repo}: no {branch} branch")
        return None
    return sha


def check_candidate(path: Path, gh: GitHub) -> tuple[Candidate | None, Manifest | None, Result]:
    """The release check against each repo's `rc` head. Reads only; nothing is tagged or moved."""
    result = Result()
    candidate = load_candidate(path, result)
    if candidate is None:
        return None, None, result
    repos: dict[str, str] = {}
    for repo in TRAIN:
        head = ref_head(gh, repo, candidate.base if repo in MERGE_FIRST else CANDIDATE_BRANCH, result)
        if head is not None:
            repos[repo] = head
    inputs: dict[str, str] = {}
    for name in INPUTS:  # not a candidate repo: its default branch head stands in for the recorded commit
        scratch = Checker(gh, Manifest(candidate.version, {}, {}), result, candidate=True)
        branch = scratch.default_branch(name)
        head = ref_head(gh, name, branch, result, soft=True) if branch else None
        if head is not None:
            inputs[name] = head
    manifest = Manifest(version=candidate.version, repos=repos, inputs=inputs)
    merge_first = {repo: candidate.prs[repo] for repo in MERGE_FIRST}
    Checker(gh, manifest, result, candidate=True, merge_first=merge_first).run()
    return candidate, manifest, result


def check(manifest_path: Path, gh: GitHub, expect_version: str | None = None) -> tuple[Manifest | None, Result]:
    """Run every check. Every release step runs this again before it writes anything."""
    result = Result()
    if looks_like_candidate(manifest_path):
        result.refuse(f"manifest: {manifest_path.name} is a release candidate, not a manifest (check.py --candidate)")
        return None, result
    if expect_version is not None and not SEMVER.fullmatch(expect_version):
        result.refuse(f"input: version must be MAJOR.MINOR.PATCH, got {expect_version!r}")
        return None, result
    if manifest_path.name.endswith(".example.yaml"):
        result.refuse(f"manifest: {manifest_path.name} is an example, not a release manifest")
    manifest = load_manifest(manifest_path, result)
    if manifest is None:
        return None, result
    if expect_version is not None and manifest.version != expect_version:
        result.refuse(f"manifest: version is {manifest.version!r}, the requested release is {expect_version!r}")
    if manifest.version and manifest_path.name not in (f"{manifest.version}.yaml", f"{manifest.version}.example.yaml"):
        result.refuse(f"manifest: file {manifest_path.name} does not match version {manifest.version!r}")
    Checker(gh, manifest, result).run()
    return manifest, result


def render_plan(manifest: Manifest, result: Result) -> str:
    v = manifest.version
    lines = [f"Release plan for v{v} (dry run prints this and changes nothing):"]
    lines.append(f"  1. Dry-run build of {OWNER}/{ENGINE} at {manifest.repos.get(ENGINE)} (release.py build)")
    lines.append("  2. Tag each train repo:")
    for repo, commit, state in result.tags:
        verb = "create" if state == "create" else "already tagged, skip"
        lines.append(f"       {OWNER}/{repo}  v{v} -> {commit}  ({verb})")
    lines.append(f"  3. Wait for {OWNER}/{ENGINE} release.yml pypi job; verify PyPI serves chock=={v}")
    lines.append(f"  4. Move {OWNER}/{SITE} branch 'release' -> {manifest.repos.get(SITE)}")
    lines.append(f"  5. Verify the site's /version.json reports version {v}")
    lines.append(f"  6. Create GitHub release v{v} in {OWNER}/chock-release with this manifest")
    for name, commit in manifest.inputs.items():
        lines.append(f"  Recorded input (not tagged): {name} @ {commit}")
    return "\n".join(lines)


def render_candidate_plan(candidate: Candidate, manifest: Manifest) -> str:
    lines = [f"Candidate plan for v{candidate.version}-rc.{candidate.rc} (prints this and changes nothing):"]
    for repo in TRAIN:
        prs = ", ".join(f"#{n}" for n in candidate.prs[repo]) or "no PRs"
        if repo in MERGE_FIRST:
            head = manifest.repos.get(repo, "unresolved")
            lines.append(f"  {OWNER}/{repo}  {candidate.base} -> {head}  (merge-first; merged: {prs})")
            continue
        lines.append(f"  {OWNER}/{repo}  {CANDIDATE_BRANCH} -> {manifest.repos.get(repo, 'unresolved')}  ({prs})")
    for name, commit in manifest.inputs.items():
        lines.append(f"  Recorded input (default branch head, not tagged): {name} @ {commit}")
    lines.append(f"  {NOTHING_MOVED}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, nargs="?", help="releases/<version>.yaml")
    parser.add_argument("--candidate", type=Path, help="check candidates/<version>-rc.<n>.yaml against the rc heads")
    parser.add_argument("--version", dest="expect_version", help="refuse unless the manifest is for this version")
    parser.add_argument("--plan", action="store_true", help="print the release plan when the check passes")
    args = parser.parse_args(argv)
    if (args.manifest is None) == (args.candidate is None):
        parser.error("give a manifest, or --candidate <file>, not both")
    if args.candidate is not None and args.expect_version is not None:
        parser.error("--version is for a manifest; a candidate carries its own version")

    gh = GitHub(token=os.environ.get("GITHUB_TOKEN") or None)
    path = args.candidate or args.manifest
    candidate = None
    if args.candidate is not None:
        candidate, manifest, result = check_candidate(path, gh)
    else:
        manifest, result = check(path, gh, args.expect_version)
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    if not result.ok:
        print(f"REFUSED: {len(result.errors)} mismatch(es) in {path.name}:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    if candidate is not None and manifest is not None:
        print(f"OK: {path.name} is consistent ({len(manifest.repos)} repos, {len(result.warnings)} warning(s)).")
        if args.plan:
            print(render_candidate_plan(candidate, manifest))
        return 0
    print(f"OK: {path.name} is consistent ({len(result.tags)} repos).")
    if args.plan and manifest is not None:
        print(render_plan(manifest, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
