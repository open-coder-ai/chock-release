#!/usr/bin/env python3
"""Keep the five per-policy plugin repos in sync with a release (IR-18, D9 A2).

  publish --catalog <sha> --engine <sha> [--dry-run] [--only <repo>]

For the catalog commit and the engine commit a release is built from:

  1. Refuses unless both are full 40-hex commits and the catalog commit's `.framework-ref`
     is the engine commit (the plugin repos build with the framework the catalog declares).
     Also refuses, before dispatching anything, when a plugin repo's `.github/requirements/framework.txt`
     pins an agentseam version other than the one the engine commit's pyproject.toml requires (`agentseam==X`;
     a direct `@ git+` reference is a refusal too): the build would crash. It prints repo and both versions.
     It does not edit the plugin repos' requirements.
  2. Dispatches `publish.yml` in each plugin repo on `main` (catalog_ref = the catalog sha,
     framework_ref = auto, dry_run as given) and waits for that run to finish green.
  3. Real run only: dispatches `generated-only.yml` on `main` and waits for its `verify` job
     (the push the publish job makes with the built-in token starts no workflow, so nothing
     else would run it).
  4. Real run only: prints the repo's new `main` head and its `generated_from.yaml`, and refuses
     if the file names a catalog or engine commit other than the two given.

--dry-run passes dry_run=true to publish.yml: the run builds the packages and prints the diff,
and pushes nothing. Steps 3 and 4 are skipped, because `main` did not move. It still dispatches
a workflow run in each plugin repo, so it needs GITHUB_TOKEN with Actions: write on them.
A real run publishes; it is the owner's. An agent runs --dry-run only.

--only <repo> (repeatable) covers part of the five; a name that is not a plugin repo is a usage error.
One repo failing does not stop the others (a re-run publishes nothing twice: "No change to publish").

`verify` regenerates from the catalog's `main`, so it is only green when the catalog commit is the
head of the catalog's `main`; the command warns when it is not.

The token comes from GITHUB_TOKEN and is never printed. GitHub only, over https.
Exit codes: 0 done, 1 refused or failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
import tomllib
import urllib.parse
from collections.abc import Callable

import check

PUBLISH = "publish.yml"
VERIFY = "generated-only.yml"
VERIFY_JOB = "verify"
FRAMEWORK_LOCK = ".github/requirements/framework.txt"
LOCK_AGENTSEAM = re.compile(r"^\s*agentseam\s*(?:\[[^\]]*\])?\s*==\s*([0-9A-Za-z.+!*-]+)", re.IGNORECASE | re.MULTILINE)
BRANCH = "main"
POLL_SECONDS = 15
TIMEOUT_SECONDS = 45 * 60


class Refused(Exception):
    """The command stops: nothing it printed after this point is true."""


def provenance(text: str) -> tuple[str | None, str | None] | None:
    """(catalog sha, engine sha) a plugin repo's generated_from.yaml names, or None when unreadable.

    Two layouts exist: what publish.yml writes (`catalog: {sha}`, `framework: {sha}`) and the
    `generated_from: {catalog, engine}` mapping check.py's check 5 reads.
    """
    try:
        data = check.load_yaml(text)
    except check.yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    flat = data.get("generated_from")
    if isinstance(flat, dict):
        return flat.get("catalog"), flat.get("engine")
    catalog, framework = data.get("catalog"), data.get("framework")
    if isinstance(catalog, dict) and isinstance(framework, dict):
        return catalog.get("sha"), framework.get("sha")
    return None


class Plugins:
    def __init__(
        self,
        gh: check.GitHub,
        catalog: str,
        engine: str,
        dry_run: bool,
        only: list[str] | None = None,
        out: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        timeout: float = TIMEOUT_SECONDS,
    ):
        self.gh = gh
        self.catalog = catalog
        self.engine = engine
        self.dry_run = dry_run
        self.only = only
        self.out = out
        self.sleep = sleep
        self.now = now
        self.timeout = timeout

    def repos(self) -> list[str]:
        return [repo for repo in check.PLUGINS if not self.only or repo in self.only]

    def get(self, repo: str, path: str) -> object | None:
        try:
            return self.gh.get(f"/repos/{check.OWNER}/{repo}{path}")
        except check.GitHubError as exc:
            raise Refused(f"{repo}: GitHub API error: {exc}") from None

    def preflight(self) -> None:
        for name, value in (("--catalog", self.catalog), ("--engine", self.engine)):
            if not check.SHA.fullmatch(value):
                raise Refused(f"{name} must be a full 40-character lowercase commit sha, got {value!r}")
        data = self.get(check.CATALOG, f"/contents/{check.FRAMEWORK_REF_FILE}?ref={self.catalog}")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise Refused(f"{check.CATALOG}: no {check.FRAMEWORK_REF_FILE} at {self.catalog}")
        declared = base64.b64decode(data.get("content", "")).decode("utf-8", "replace").strip()
        if declared != self.engine:
            raise Refused(
                f"{check.CATALOG} {self.catalog[:12]} declares framework {declared!r} in {check.FRAMEWORK_REF_FILE}, "
                f"not the engine commit {self.engine}: run adopt-framework in the catalog first"
            )
        head = self.get(check.CATALOG, f"/git/ref/heads/{BRANCH}")
        main = head.get("object", {}).get("sha") if isinstance(head, dict) else None
        if main != self.catalog:
            self.out(
                f"WARNING: {check.CATALOG} {BRANCH} is {main}, not {self.catalog}; the plugin repos' "
                f"{VERIFY_JOB} job regenerates from {BRANCH} and will be red until they agree"
            )
        self.agentseam_pins()

    def file(self, repo: str, path: str, ref: str) -> str | None:
        data = self.get(repo, f"/contents/{path}?ref={ref}")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        return base64.b64decode(data.get("content", "")).decode("utf-8", "replace")

    def engine_agentseam(self) -> str:
        """The agentseam version the engine commit's pyproject.toml pins with `==`."""
        text = self.file(check.ENGINE, "pyproject.toml", self.engine)
        if text is None:
            raise Refused(f"{check.ENGINE}: no pyproject.toml at {self.engine}")
        try:
            project = tomllib.loads(text)["project"]
        except (tomllib.TOMLDecodeError, KeyError, TypeError):
            raise Refused(f"{check.ENGINE}: pyproject.toml at {self.engine} has no [project] table") from None
        deps: list[object] = list(project.get("dependencies") or [])
        for group in (project.get("optional-dependencies") or {}).values():
            deps.extend(group or [])
        pins: set[str] = set()
        for dep in deps:
            if not isinstance(dep, str) or check.canonical_name(dep) != check.AGENTSEAM:
                continue
            if check.DIRECT_REF.match(dep):
                raise Refused(
                    f"{check.ENGINE} {self.engine[:12]} requires {dep!r}, a direct reference: "
                    "release-week check 3 refuses it, and PyPI rejects it; pin agentseam==<version> first"
                )
            m = check.AGENTSEAM_PIN.match(dep)
            if m is None:
                raise Refused(f"{check.ENGINE} {self.engine[:12]} requires {dep!r}, which is not agentseam==<version>")
            pins.add(m.group(1))
        if len(pins) != 1:
            raise Refused(
                f"{check.ENGINE} {self.engine[:12]} pins agentseam to {sorted(pins) or 'nothing'}, not one version"
            )
        return pins.pop()

    def agentseam_pins(self) -> None:
        """Every plugin repo hash-pins the agentseam version the engine needs, else its build crashes."""
        want = self.engine_agentseam()
        problems = []
        for repo in self.repos():
            text = self.file(repo, FRAMEWORK_LOCK, BRANCH)
            if text is None:
                problems.append(f"{repo}: no {FRAMEWORK_LOCK} on {BRANCH}")
                continue
            found = sorted(set(LOCK_AGENTSEAM.findall(text)))
            if found != [want]:
                problems.append(
                    f"{repo}: {FRAMEWORK_LOCK} pins agentseam {', '.join(found) or 'nothing'}, "
                    f"engine {self.engine[:12]} requires {want}"
                )
        if problems:
            raise Refused(
                "agentseam pin differs, so the plugin build would crash (update the plugin repos' requirements "
                "first; this command does not):\n  " + "\n  ".join(problems)
            )

    def run_ids(self, repo: str, workflow: str) -> set[int]:
        query = urllib.parse.urlencode({"event": "workflow_dispatch", "branch": BRANCH, "per_page": 50})
        data = self.get(repo, f"/actions/workflows/{workflow}/runs?{query}")
        if not isinstance(data, dict):
            raise Refused(f"{repo}: workflow {workflow} not found")
        return {r["id"] for r in data.get("workflow_runs", [])}

    def dispatch(self, repo: str, workflow: str, inputs: dict[str, str]) -> int:
        """Dispatch <workflow> on main and return the id of the run it started."""
        before = self.run_ids(repo, workflow)
        body: dict = {"ref": BRANCH}
        if inputs:
            body["inputs"] = inputs
        try:
            self.gh.send("POST", f"/repos/{check.OWNER}/{repo}/actions/workflows/{workflow}/dispatches", body)
        except check.GitHubError as exc:
            raise Refused(f"{repo}: could not dispatch {workflow}: {exc}") from None
        deadline = self.now() + self.timeout
        while True:
            new = self.run_ids(repo, workflow) - before
            if new:
                return max(new)
            if self.now() >= deadline:
                raise Refused(f"{repo}: {workflow} was dispatched but no run appeared in {self.timeout:.0f}s")
            self.sleep(POLL_SECONDS)

    def wait(self, repo: str, run_id: int, workflow: str) -> dict:
        deadline = self.now() + self.timeout
        while True:
            run = self.get(repo, f"/actions/runs/{run_id}")
            if not isinstance(run, dict):
                raise Refused(f"{repo}: run {run_id} of {workflow} disappeared")
            if run.get("status") == "completed":
                return run
            if self.now() >= deadline:
                raise Refused(f"{repo}: {workflow} run {run_id} still {run.get('status')} after {self.timeout:.0f}s")
            self.sleep(POLL_SECONDS)

    def run_workflow(self, repo: str, workflow: str, inputs: dict[str, str]) -> dict:
        run_id = self.dispatch(repo, workflow, inputs)
        self.out(f"{repo}: {workflow} run {run_id} dispatched")
        run = self.wait(repo, run_id, workflow)
        if run.get("conclusion") != "success":
            raise Refused(f"{repo}: {workflow} run {run_id} concluded {run.get('conclusion')!r}")
        return run

    def verify_job(self, repo: str, run: dict) -> None:
        data = self.get(repo, f"/actions/runs/{run['id']}/jobs?per_page=100")
        all_jobs = data.get("jobs", []) if isinstance(data, dict) else []
        jobs = [j for j in all_jobs if j.get("name") == VERIFY_JOB]
        if not jobs or any(j.get("conclusion") != "success" for j in jobs):
            seen = ", ".join(f"{j.get('name')}={j.get('conclusion')}" for j in all_jobs) or "no jobs"
            raise Refused(f"{repo}: {VERIFY} has no green {VERIFY_JOB} job ({seen})")

    def head(self, repo: str) -> str:
        ref = self.get(repo, f"/git/ref/heads/{BRANCH}")
        sha = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        if not isinstance(sha, str) or not check.SHA.fullmatch(sha):
            raise Refused(f"{repo}: no {BRANCH} branch")
        return sha

    def provenance_of(self, repo: str, head: str) -> None:
        data = self.get(repo, f"/contents/{check.PLUGIN_PROVENANCE_FILE}?ref={head}")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise Refused(f"{repo}: no {check.PLUGIN_PROVENANCE_FILE} at {head}")
        text = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
        got = provenance(text)
        if got is None:
            raise Refused(f"{repo}: {check.PLUGIN_PROVENANCE_FILE} at {head} names no catalog and engine commit")
        self.out(f"{repo}: {BRANCH} is now {head}")
        self.out(f"{repo}: {check.PLUGIN_PROVENANCE_FILE}: catalog {got[0]}, engine {got[1]}")
        for label, have, want in (("catalog", got[0], self.catalog), ("engine", got[1], self.engine)):
            if have != want:
                raise Refused(f"{repo}: {check.PLUGIN_PROVENANCE_FILE} names {label} {have!r}, not {want}")

    def sync(self, repo: str) -> None:
        self.run_workflow(
            repo,
            PUBLISH,
            {"catalog_ref": self.catalog, "framework_ref": "auto", "dry_run": "true" if self.dry_run else "false"},
        )
        if self.dry_run:
            self.out(f"{repo}: dry run built green, nothing pushed ({VERIFY} and provenance skipped)")
            return
        run = self.run_workflow(repo, VERIFY, {})
        self.verify_job(repo, run)
        self.provenance_of(repo, self.head(repo))
        self.out(f"{repo}: in sync with catalog {self.catalog[:12]} and engine {self.engine[:12]}")

    def publish(self) -> int:
        try:
            self.preflight()
        except Refused as exc:
            self.out(f"REFUSED: {exc}")
            return 1
        self.out(
            f"{'Dry run: ' if self.dry_run else ''}publish {len(self.repos())} plugin repo(s) from catalog "
            f"{self.catalog} and engine {self.engine}"
        )
        failed: list[str] = []
        for repo in self.repos():
            try:
                self.sync(repo)
            except Refused as exc:
                self.out(f"REFUSED: {exc}")
                failed.append(repo)
        if failed:
            self.out(f"FAILED: {', '.join(failed)}; the others are done. Fix the cause and re-run with --only")
            return 1
        self.out("OK: every plugin repo is " + ("built green (dry run)" if self.dry_run else "published and verified"))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=["publish"])
    parser.add_argument("--catalog", required=True, metavar="SHA", help="chock-catalog commit to publish")
    parser.add_argument("--engine", required=True, metavar="SHA", help="chock commit the catalog declares")
    parser.add_argument("--dry-run", action="store_true", help="dispatch publish.yml with dry_run=true; push nothing")
    parser.add_argument("--only", action="append", metavar="REPO", help="only this plugin repo (repeatable)")
    args = parser.parse_args(argv)
    unknown = [name for name in args.only or [] if name not in check.PLUGINS]
    if unknown:
        parser.error(f"--only names {', '.join(unknown)}, which is not one of: {', '.join(check.PLUGINS)}")
    token = os.environ.get("GITHUB_TOKEN") or None
    if token is None:
        print(
            "REFUSED: GITHUB_TOKEN is not set; dispatching a workflow needs a token with Actions: write",
            file=sys.stderr,
        )
        return 1
    return Plugins(check.GitHub(token=token), args.catalog, args.engine, args.dry_run, args.only).publish()


if __name__ == "__main__":
    sys.exit(main())
