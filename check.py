#!/usr/bin/env python3
"""Consistency check for one release train manifest (releases/<version>.yaml).

It refuses on any mismatch and lists every mismatch it finds, not only the first.
The numbered checks follow org-plan plan/release-1.md:

1. Every repo in the train is named, with a 40-hex commit on its default branch.
2. CI is green on every named commit.
3. The engine at its commit reports __version__ == version (pyproject.toml).
4. The catalog's .framework-ref equals the engine commit; its VERSION equals version.
5. Each plugin repo records the catalog and engine commits it was generated from.
6. The site's .framework-ref and .catalog-ref equal the manifest's; its version data
   equals version.
7. quickstart and example pin the engine commit, or chock==version.
8. No v<version> tag exists yet in any train repo, except as allowed by 9.
9. An existing v<version> tag at the manifest commit passes; at any other commit, refuses.

Credentials come only from the environment (GITHUB_TOKEN). They are never printed.

Exit codes: 0 consistent, 1 mismatches found, 2 usage error.
"""

from __future__ import annotations

import argparse
import base64
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

# Recorded by commit, never tagged: separate products with their own versions.
INPUTS = ("agentseam",)

# Where each repo keeps the facts the check reads. The prepare phase (REL-3 to REL-5)
# creates the files that do not exist yet; a missing file is a mismatch, not a pass.
ENGINE_VERSION_FILE = "pyproject.toml"  # [project].version is chock.__version__
CATALOG_VERSION_FILE = "VERSION"  # one line: the train version
FRAMEWORK_REF_FILE = ".framework-ref"
CATALOG_REF_FILE = ".catalog-ref"
PLUGIN_PROVENANCE_FILE = "generated_from.yaml"  # generated_from: {catalog, engine}
SITE_VERSION_FILE = "data/version.json"  # {"version": ...}
PIN_FILES = ("README.md", "requirements.txt", "pyproject.toml")

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SHA = re.compile(r"[0-9a-f]{40}")
PLACEHOLDER_SHA = "0" * 40
GIT_PIN = re.compile(rf"chock\s*@\s*git\+https://github\.com/{OWNER}/chock(?:\.git)?@([0-9A-Za-z._/-]+)")
ARCHIVE_PIN = re.compile(rf"chock\s*@\s*https://github\.com/{OWNER}/chock/archive/([0-9A-Za-z._-]+)\.tar\.gz")
PYPI_PIN = re.compile(r"(?<![\w-])chock\s*==\s*([0-9A-Za-z.+-]+)")

GREEN_CONCLUSIONS = {"success", "neutral", "skipped"}


class GitHubError(Exception):
    """An API call failed for a reason other than 404."""


class GitHub:
    """Minimal read-only GitHub REST client. get() returns parsed JSON, or None on 404."""

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
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https base)
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            # The message names the path and status only, never the request headers.
            raise GitHubError(f"GET {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GitHubError(f"GET {path}: {type(exc).__name__}") from None


@dataclass
class Manifest:
    version: str
    repos: dict[str, str]
    inputs: dict[str, str]


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    # (repo, commit, state): state is "create" or "exists" for the v<version> tag.
    tags: list[tuple[str, str, str]] = field(default_factory=list)

    def refuse(self, msg: str) -> None:
        self.errors.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_manifest(path: Path, result: Result) -> Manifest | None:
    """Parse and validate the manifest's shape. Shape errors go to result."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        result.refuse(f"manifest: cannot read {path.name}: {type(exc).__name__}")
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
            commit = entry.get("commit") if isinstance(entry, dict) else None
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


class Checker:
    def __init__(self, gh: GitHub, manifest: Manifest, result: Result, ci: bool = True):
        self.gh = gh
        self.ci = ci
        self.m = manifest
        self.r = result

    def _get(self, repo: str, path: str) -> object | None:
        """GET /repos/OWNER/<repo><path>; an API failure is recorded and returns None."""
        try:
            return self.gh.get(f"/repos/{OWNER}/{repo}{path}")
        except GitHubError as exc:
            self.r.refuse(f"{repo}: GitHub API error: {exc}")
            return None

    def _file(self, repo: str, path: str) -> str | None:
        """Text of <path> at the manifest commit of <repo>, or None (recorded) if absent."""
        commit = self.m.repos[repo]
        quoted = urllib.parse.quote(path)
        data = self._get(repo, f"/contents/{quoted}?ref={commit}")
        if data is None:
            self.r.refuse(f"{repo}: {path} not found at {commit[:12]}")
            return None
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            self.r.refuse(f"{repo}: {path} at {commit[:12]} is not a file")
            return None
        try:
            return base64.b64decode(data.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            self.r.refuse(f"{repo}: {path} at {commit[:12]} is not UTF-8 text")
            return None

    def _ref(self, repo: str, path: str) -> str | None:
        text = self._file(repo, path)
        return None if text is None else text.strip()

    # 1
    def on_default_branch(self, repo: str, commit: str) -> bool:
        info = self._get(repo, "")
        if not isinstance(info, dict) or "default_branch" not in info:
            self.r.refuse(f"check 1: {repo}: repository not found")
            return False
        branch = info["default_branch"]
        cmp = self._get(repo, f"/compare/{urllib.parse.quote(branch)}...{commit}")
        if cmp is None:
            self.r.refuse(f"check 1: {repo}: commit {commit} does not exist")
            return False
        # base...head: "behind" or "identical" means head is an ancestor of base.
        if not isinstance(cmp, dict) or cmp.get("status") not in ("behind", "identical"):
            status = cmp.get("status") if isinstance(cmp, dict) else None
            self.r.refuse(f"check 1: {repo}: commit {commit[:12]} is not on {branch} (compare: {status})")
            return False
        return True

    # 2
    def ci_green(self, repo: str, commit: str) -> None:
        runs: list[dict] = []
        page = 1
        while True:
            data = self._get(repo, f"/commits/{commit}/check-runs?per_page=100&page={page}")
            if not isinstance(data, dict):
                self.r.refuse(f"check 2: {repo}: cannot read check runs for {commit[:12]}")
                return
            batch = data.get("check_runs") or []
            runs.extend(batch)
            if len(runs) >= int(data.get("total_count", 0)) or not batch:
                break
            page += 1
        status = self._get(repo, f"/commits/{commit}/status")
        statuses = status.get("statuses", []) if isinstance(status, dict) else []

        if not runs and not statuses:
            self.r.refuse(f"check 2: {repo}: no CI ran on {commit[:12]}")
            return
        for run in runs:
            name = run.get("name", "?")
            if run.get("status") != "completed":
                self.r.refuse(f"check 2: {repo}: check '{name}' is {run.get('status')} on {commit[:12]}")
            elif run.get("conclusion") not in GREEN_CONCLUSIONS:
                self.r.refuse(f"check 2: {repo}: check '{name}' concluded {run.get('conclusion')} on {commit[:12]}")
        if statuses and status.get("state") != "success":
            self.r.refuse(f"check 2: {repo}: commit status is {status.get('state')} on {commit[:12]}")

    # 3
    def engine_version(self) -> None:
        text = self._file(ENGINE, ENGINE_VERSION_FILE)
        if text is None:
            return
        try:
            got = tomllib.loads(text)["project"]["version"]
        except (tomllib.TOMLDecodeError, KeyError, TypeError):
            self.r.refuse(f"check 3: {ENGINE}: {ENGINE_VERSION_FILE} has no [project].version")
            return
        if got != self.m.version:
            self.r.refuse(f"check 3: {ENGINE}: __version__ is {got!r}, manifest says {self.m.version!r}")

    # 4
    def catalog(self) -> None:
        engine = self.m.repos.get(ENGINE)
        ref = self._ref(CATALOG, FRAMEWORK_REF_FILE)
        if ref is not None and ref != engine:
            self.r.refuse(f"check 4: {CATALOG}: {FRAMEWORK_REF_FILE} is {ref!r}, engine commit is {engine!r}")
        version = self._ref(CATALOG, CATALOG_VERSION_FILE)
        if version is not None and version != self.m.version:
            self.r.refuse(
                f"check 4: {CATALOG}: {CATALOG_VERSION_FILE} is {version!r}, manifest says {self.m.version!r}"
            )

    # 5
    def plugin(self, repo: str) -> None:
        text = self._file(repo, PLUGIN_PROVENANCE_FILE)
        if text is None:
            return
        try:
            data = yaml.safe_load(text)
            gen = data["generated_from"]
            recorded = {"catalog": gen.get("catalog"), "engine": gen.get("engine")}
        except (yaml.YAMLError, KeyError, TypeError, AttributeError):
            self.r.refuse(f"check 5: {repo}: {PLUGIN_PROVENANCE_FILE} has no generated_from mapping")
            return
        for key, source in (("catalog", CATALOG), ("engine", ENGINE)):
            want = self.m.repos.get(source)
            if recorded[key] != want:
                self.r.refuse(
                    f"check 5: {repo}: generated_from.{key} is {recorded[key]!r}, manifest {source} is {want!r}"
                )

    # 6
    def site(self) -> None:
        for path, source in ((FRAMEWORK_REF_FILE, ENGINE), (CATALOG_REF_FILE, CATALOG)):
            ref = self._ref(SITE, path)
            want = self.m.repos.get(source)
            if ref is not None and ref != want:
                self.r.refuse(f"check 6: {SITE}: {path} is {ref!r}, manifest {source} is {want!r}")
        text = self._file(SITE, SITE_VERSION_FILE)
        if text is None:
            return
        try:
            got = json.loads(text)["version"]
        except (json.JSONDecodeError, KeyError, TypeError):
            self.r.refuse(f"check 6: {SITE}: {SITE_VERSION_FILE} has no version")
            return
        if got != self.m.version:
            self.r.refuse(f"check 6: {SITE}: {SITE_VERSION_FILE} version is {got!r}, manifest says {self.m.version!r}")

    # 7
    def pins(self, repo: str) -> None:
        engine = self.m.repos.get(ENGINE)
        commit = self.m.repos[repo]
        found = 0
        for path in PIN_FILES:
            data = self._get(repo, f"/contents/{path}?ref={commit}")
            if not isinstance(data, dict) or data.get("encoding") != "base64":
                continue
            try:
                text = base64.b64decode(data.get("content", "")).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                self.r.refuse(f"check 7: {repo}: {path} is not UTF-8 text")
                continue
            for pattern, kind in ((GIT_PIN, "commit"), (ARCHIVE_PIN, "commit"), (PYPI_PIN, "version")):
                for match in pattern.finditer(text):
                    found += 1
                    got = match.group(1)
                    want = engine if kind == "commit" else self.m.version
                    if got != want:
                        self.r.refuse(f"check 7: {repo}: {path} pins chock {kind} {got!r}, want {want!r}")
        if found == 0:
            self.r.refuse(f"check 7: {repo}: no chock pin found in {', '.join(PIN_FILES)}")

    # 8, 9
    def tag(self, repo: str, commit: str) -> None:
        name = f"v{self.m.version}"
        ref = self._get(repo, f"/git/ref/tags/{name}")
        if ref is None:
            self.r.tags.append((repo, commit, "create"))
            return
        obj = ref.get("object", {}) if isinstance(ref, dict) else {}
        target = obj.get("sha")
        if obj.get("type") == "tag":  # annotated: peel to the commit
            tag = self._get(repo, f"/git/tags/{target}")
            target = tag.get("object", {}).get("sha") if isinstance(tag, dict) else None
        if target == commit:
            self.r.tags.append((repo, commit, "exists"))
        else:
            self.r.refuse(f"check 9: {repo}: tag {name} already exists at {target!r}, manifest commit is {commit!r}")

    def run(self) -> None:
        on_branch = {}
        for repo, commit in {**self.m.repos, **self.m.inputs}.items():
            on_branch[repo] = self.on_default_branch(repo, commit)
        for repo, commit in self.m.repos.items():
            if on_branch[repo] and self.ci:
                self.ci_green(repo, commit)
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
        for repo, commit in self.m.repos.items():
            self.tag(repo, commit)


def check(
    manifest_path: Path, gh: GitHub, expect_version: str | None = None, ci: bool = True
) -> tuple[Manifest | None, Result]:
    """Run every check. ci=False skips check 2 only: release.py passes it after tagging,
    because the tag pushes start new check runs on the same commits."""
    result = Result()
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
    Checker(gh, manifest, result, ci=ci).run()
    return manifest, result


def render_plan(manifest: Manifest, result: Result) -> str:
    v = manifest.version
    lines = [f"Release plan for v{v} (dry run prints this and changes nothing):"]
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, help="releases/<version>.yaml")
    parser.add_argument("--version", dest="expect_version", help="refuse unless the manifest is for this version")
    parser.add_argument("--plan", action="store_true", help="print the release plan when the check passes")
    args = parser.parse_args(argv)

    gh = GitHub(token=os.environ.get("GITHUB_TOKEN") or None)
    manifest, result = check(args.manifest, gh, args.expect_version)
    if not result.ok:
        print(f"REFUSED: {len(result.errors)} mismatch(es) in {args.manifest.name}:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"OK: {args.manifest.name} is consistent ({len(result.tags)} repos).")
    if args.plan and manifest is not None:
        print(render_plan(manifest, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
