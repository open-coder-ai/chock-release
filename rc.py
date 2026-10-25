#!/usr/bin/env python3
"""Assemble and inspect the `rc` branches of a release candidate (candidates/<version>-rc.<n>.yaml).

  assemble  For each train repo: clone it, create `rc` from origin/main only when `rc` does not
            exist (else check it out and merge origin/main in), merge each listed PR from
            refs/pull/<n>/head with a merge commit (--no-ff), and push `rc` fast-forward only.
            A PR that is not open, a conflict, or a push that is not a fast-forward stops that repo
            with the repo and PR printed; nothing is pushed for it. Other repos still run. The one
            exception: a conflict in a PR merge confined to paths the candidate file lists under
            `regenerated:` for that repo (rewritten by pins and the site's regeneration right after)
            is resolved by taking the PR's version and noted in the merge commit and the output.
  pins      One commit on chock-site's `rc` setting .catalog-ref to the catalog's base-branch (main) head
            and .framework-ref to the engine's `rc` head, pushed fast-forward only. The same commit also
            replaces the engine commit the site last pinned (read from data/kinds/engine-commit.txt
            and the old .framework-ref) with the new one in data/kinds/engine-commit.txt and in the
            `"chock"` entry of the `pins` object of data/architecture/*.json, by string replacement
            so the formatting survives; a diagram pinned at some other commit is left alone.
  status    Each repo's `rc` head, which listed PRs it contains, whether main moved under it, and
            the number of its rc draft PR (or "none"). For the catalog: its main head and, per listed PR,
            "merged" or "NOT merged (open|closed)".
  prs       For each repo whose `rc` exists: find the open PR from `rc` to main and create it as a
            draft when absent ("Release candidate <version>-rc.<n>: do not merge"), so CI runs on
            the `rc` head; when it exists, rewrite its body only. It never marks a PR ready, merges
            or closes one, and leaves an existing title and draft state alone. These PRs are never
            merged: promotion merges the listed PRs to main.

The catalog is merge-first: the engine refuses a catalog commit that is not on the catalog's main branch, so
the candidate pins the catalog's main head, and the PRs the candidate lists for it are merged to main (gated by
its own CI) before `pins` runs. assemble and prs skip it (no `rc` branch, no draft PR) and --only cannot name it
for them; status and check.py --candidate report whether its listed PRs are merged.

assemble, prs and status take --only <repo> (repeatable) to cover part of the train, for a session whose
GitHub scope reaches one repo; a repo that is not in the candidate file is a usage error.
Every verb takes --dry-run: it prints the plan, reads GitHub, and touches nothing.
`prs` is the only verb that writes to the GitHub API (creating or updating those draft PRs).
`assemble --reset` deletes and recreates `rc` from main. That is the owner's: it is refused unless
--i-am-the-owner is also given, and an agent never passes that flag.

Commits are authored as the owner, with a sign-off. GitHub only, over https. The token comes from
GITHUB_TOKEN when set (and is handed to git through its environment, never argv); reads work without
one. Nothing here tags, releases, publishes or moves any branch but `rc`.

Exit codes: 0 done, 1 refused or failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import check

BRANCH = check.CANDIDATE_BRANCH
IDENTITY = ("Jothi Mani Rajendran", "250249270+jothimani-rajendran@users.noreply.github.com")


REGENERATE = (
    "reminder: the site records engine and catalog snapshots that its CI checks against these pins. "
    "On chock-site's rc, in this order, regenerate them (commands from chock-site's module docstrings and "
    ".github/workflows/ci.yml): "
    "1. `python -m sitegen.docsource --chock <chock checkout at .framework-ref>` (data/docs-source/); "
    "2. `python -m sitegen.writedoc --record` (data/write-source/, chock installed at .framework-ref); "
    "3. `python -m sitegen.catalogdoc --catalog <catalog checkout at .catalog-ref>` "
    "(data/catalog-source/, chock installed at .framework-ref; the adopt run fetches from GitHub); "
    "4. `python -m sitegen.cliexamples --record` (data/cli-examples/, chock installed at .framework-ref); "
    "5. `python -m sitegen.agentseamdoc snapshot --agentseam <agentseam checkout>` "
    "(data/agentseam-source/) and `python -m sitegen.agentseamdoc run --record` (data/agentseam-run.json); "
    "6. check the architecture diagrams: "
    "`python -m sitegen.architecture --check --fetch --clones <folder> --repo chock-site=.` "
    "(pins now follow the engine; this only reads). "
    "Then run each of the above with --check (agentseamdoc: `snapshot ... --check` and `run --check`). "
    "The regeneration commit goes on rc only, never on main."
)

ENGINE_COMMIT_FILE = "data/kinds/engine-commit.txt"
ARCHITECTURE_DIR = "data/architecture"
SHA_RE = re.compile(r"[0-9a-f]{40}")


def read_sha(path: Path) -> str | None:
    """The 40-hex commit in a one-line pin file, or None when the file is missing or holds anything else."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if SHA_RE.fullmatch(text) else None


def refuse_symlink(root: Path, path: Path) -> None:
    """Refuse a write target that is a symlink or resolves outside the clone: a write would follow it."""
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise Refused(f"{path.relative_to(root).as_posix()} is a symlink or resolves outside the clone; not writing it")


def rewrite_engine_pins(site: Path, old_shas: set[str], new: str) -> list[str]:
    """Replace each old engine commit by `new` where the site hand-keeps a copy of the engine pin.

    Those copies are data/kinds/engine-commit.txt (the whole file) and the `"chock"` entry of the `pins`
    object of each data/architecture/*.json. A plain textual replacement of the old sha, so key order,
    indentation and the final newline survive. Returns the repo-relative paths changed.
    """
    changed = []
    paths = [site / ENGINE_COMMIT_FILE, *sorted((site / ARCHITECTURE_DIR).glob("*.json"))]
    for path in paths:
        refuse_symlink(site, path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        after = text
        for old in old_shas - {new}:
            if path.suffix == ".json":
                after = re.sub(rf'("chock"\s*:\s*"){old}(")', rf"\g<1>{new}\g<2>", after)
            else:
                after = after.replace(old, new)
        if after != text:
            path.write_text(after, encoding="utf-8")
            changed.append(path.relative_to(site).as_posix())
    return changed


def glob_match(pattern: str, path: str) -> bool:
    """Path glob: `**` crosses directories, `*` and `?` stay inside one; the whole path must match."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None


class Refused(Exception):
    """A step that must stop this repo; the message says why."""


def https_url(repo: str) -> str:
    return f"https://github.com/{check.OWNER}/{repo}.git"


class Git:
    """git over https, with the token (if any) passed in the environment and never printed."""

    def __init__(self, url_for: Callable[[str], str] = https_url, token: str | None = None, protocols: str = "https"):
        self.url_for = url_for
        self._token = token
        self._protocols = protocols

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(GIT_ALLOW_PROTOCOL=self._protocols, GIT_TERMINAL_PROMPT="0")
        if self._token:
            basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            n = int(env.get("GIT_CONFIG_COUNT") or 0)  # append: the environment may already carry git config
            env.update(
                GIT_CONFIG_COUNT=str(n + 1),
                **{
                    f"GIT_CONFIG_KEY_{n}": "http.https://github.com/.extraheader",
                    f"GIT_CONFIG_VALUE_{n}": f"Authorization: Basic {basic}",
                },
            )
        return env

    def run(self, args: list[str], cwd: Path | None = None, ok: bool = False) -> subprocess.CompletedProcess:
        """Run git; raise Refused on a non-zero exit unless ok (the caller reads returncode)."""
        argv = [
            "git",
            "-c",
            f"user.name={IDENTITY[0]}",
            "-c",
            f"user.email={IDENTITY[1]}",
            "-c",
            "commit.gpgsign=false",
        ]
        proc = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            [*argv, *args], cwd=cwd, env=self.env(), capture_output=True, text=True, check=False
        )
        if proc.returncode != 0 and not ok:
            tail = (proc.stdout + proc.stderr).strip()[-600:]
            if self._token:
                tail = tail.replace(self._token, "***")
            raise Refused(f"git {args[0]} failed: {tail}")
        return proc


class Rc:
    def __init__(
        self, gh: check.GitHub, git: Git, cand: check.Candidate, dry_run: bool, out=print, only: list[str] | None = None
    ):
        self.gh = gh
        self.git = git
        self.cand = cand
        self.dry_run = dry_run
        self.out = out
        self.only = only

    def repos(self) -> list[str]:
        """The train repos this run covers: all of them, or those named with --only."""
        return [repo for repo in check.TRAIN if not self.only or repo in self.only]

    def skip_merge_first(self, repo: str) -> bool:
        """Say so and return True for a merge-first repo: it has no `rc` branch and no rc draft PR."""
        if repo not in check.MERGE_FIRST:
            return False
        self.out(
            f"{repo}: merge-first, skipped (its PRs are merged to {self.cand.base} first; "
            f"no {BRANCH} branch is created, merged or pushed for it, and no draft PR)"
        )
        return True

    # GitHub reads ------------------------------------------------------------------------

    def get(self, repo: str, path: str) -> object | None:
        try:
            return self.gh.get(f"/repos/{check.OWNER}/{repo}{path}")
        except check.GitHubError as exc:
            raise Refused(f"GitHub API error: {exc}") from None

    def rc_head(self, repo: str) -> str | None:
        ref = self.get(repo, f"/git/ref/heads/{BRANCH}")
        sha = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        return sha if isinstance(sha, str) and check.SHA.fullmatch(sha) else None

    def base_head(self, repo: str) -> str | None:
        ref = self.get(repo, f"/git/ref/heads/{self.cand.base}")
        sha = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        return sha if isinstance(sha, str) and check.SHA.fullmatch(sha) else None

    def open_prs(self, repo: str) -> dict[int, str]:
        """PR number -> head sha, refusing a PR that is missing, not open, or not against the base."""
        heads: dict[int, str] = {}
        for n in self.cand.prs[repo]:
            pr = self.get(repo, f"/pulls/{n}")
            if not isinstance(pr, dict):
                raise Refused(f"{repo} PR #{n}: not found")
            if pr.get("state") != "open":
                raise Refused(f"{repo} PR #{n}: not open (state: {pr.get('state')}, merged: {pr.get('merged')})")
            base = pr.get("base", {}).get("ref")
            if base != self.cand.base:
                raise Refused(f"{repo} PR #{n}: targets {base!r}, the candidate's base is {self.cand.base!r}")
            sha = pr.get("head", {}).get("sha")
            if not isinstance(sha, str) or not check.SHA.fullmatch(sha):
                raise Refused(f"{repo} PR #{n}: no head commit")
            heads[n] = sha
        return heads

    # assemble ----------------------------------------------------------------------------

    def assemble(self, reset: bool) -> int:
        failed = 0
        with tempfile.TemporaryDirectory(prefix="chock-rc-") as tmp:
            for repo in self.repos():
                if self.skip_merge_first(repo):
                    continue
                try:
                    self.assemble_repo(repo, Path(tmp) / repo, reset)
                except Refused as exc:
                    failed += 1
                    self.out(f"REFUSED {repo}: {exc} (nothing pushed for {repo})", file=sys.stderr)
        if failed:
            self.out(f"{failed} repo(s) refused; the others are done", file=sys.stderr)
        self.out("next: rc.py prs <file> so CI runs on rc")
        return 1 if failed else 0

    def assemble_repo(self, repo: str, dest: Path, reset: bool) -> None:
        prs = self.open_prs(repo)
        existing = self.rc_head(repo)
        base = self.cand.base
        listed = ", ".join(f"#{n}" for n in prs) or "no PRs"
        if reset:
            how = f"delete and recreate {BRANCH} from {base}" if existing else f"create {BRANCH} from {base}"
        else:
            how = f"check out {BRANCH}, merge origin/{base}" if existing else f"create {BRANCH} from origin/{base}"
        if self.dry_run:
            self.out(f"{repo}: would clone, {how}, merge {listed} (--no-ff), push {BRANCH} fast-forward only")
            return

        git = self.git
        git.run(["clone", "--quiet", "--no-tags", git.url_for(repo), str(dest)])
        if existing and not reset:
            git.run(["checkout", "--quiet", "-B", BRANCH, f"origin/{BRANCH}"], dest)
            self.merge(dest, repo, f"origin/{base}", f"Merge {base} into {BRANCH}", f"{base}")  # never takes a side
        else:
            git.run(["checkout", "--quiet", "-B", BRANCH, f"origin/{base}"], dest)
        for n, sha in prs.items():
            local = f"refs/rc-pr/{n}"
            git.run(["fetch", "--quiet", "origin", f"+refs/pull/{n}/head:{local}"], dest)
            got = git.run(["rev-parse", local], dest).stdout.strip()
            if got != sha:
                raise Refused(f"PR #{n} moved while assembling (GitHub said {sha[:12]}, fetched {got[:12]})")
            self.merge(dest, repo, local, f"Merge pull request #{n} into {BRANCH}", f"PR #{n}", pr=n)

        head = git.run(["rev-parse", "HEAD"], dest).stdout.strip()
        if head == existing:
            self.out(f"{repo}: {BRANCH} already at {head[:12]}, nothing to push")
            return
        if reset and existing:
            push = [
                "push",
                "--quiet",
                f"--force-with-lease={BRANCH}:{existing}",
                "origin",
                f"{BRANCH}:refs/heads/{BRANCH}",
            ]
        else:
            push = ["push", "--quiet", "origin", f"{BRANCH}:refs/heads/{BRANCH}"]
        proc = git.run(push, dest, ok=True)
        if proc.returncode != 0:
            raise Refused(
                f"push of {BRANCH} was not a fast-forward or was refused: {(proc.stderr or '').strip()[-300:]}"
            )
        self.out(f"{repo}: {BRANCH} {(existing or 'new')[:12]} -> {head[:12]} (merged {listed})")

    def merge(self, dest: Path, repo: str, ref: str, message: str, label: str, pr: int | None = None) -> None:
        """Merge <ref> into HEAD with a merge commit; a conflict aborts and refuses with the repo and PR.

        For a PR merge (pr given) a conflict confined to paths the candidate declares `regenerated` for
        this repo is resolved by taking the PR's version: the next pins and regeneration step rewrites
        those paths anyway. Any other conflict still refuses, naming the paths outside the list.
        """
        git = self.git
        if git.run(["merge-base", "--is-ancestor", ref, "HEAD"], dest, ok=True).returncode == 0:
            return  # already contained: a repeat round adds nothing
        # No rename detection: with it a deletion paired with a similar new file shows as one unmerged path
        # and the deletion would be resolved without being listed (-X no-renames alone is a no-op under ort).
        proc = git.run(
            ["merge", "--no-ff", "-s", "recursive", "-X", "no-renames", "--signoff", "-m", message, ref], dest, ok=True
        )
        if proc.returncode == 0:
            return
        listing = git.run(["diff", "--name-only", "-z", "--diff-filter=U"], dest, ok=True).stdout
        unmerged = [p for p in listing.split("\0") if p]
        globs = self.cand.regenerated.get(repo, [])
        if pr is not None and unmerged:
            outside = [p for p in unmerged if not any(glob_match(g, p) for g in globs)]
            if not outside:
                self.take_pr_side(dest, repo, message, pr, unmerged)
                return
            if globs:
                git.run(["merge", "--abort"], dest, ok=True)
                raise Refused(
                    f"CONFLICT merging {label} into {BRANCH} ({', '.join(unmerged[:5])}); "
                    f"outside the regenerated paths: {', '.join(outside)}"
                )
        git.run(["merge", "--abort"], dest, ok=True)
        if unmerged:
            raise Refused(f"CONFLICT merging {label} into {BRANCH} ({', '.join(unmerged[:5])})")
        raise Refused(f"cannot merge {label} into {BRANCH}: {(proc.stdout + proc.stderr).strip()[-300:]}")

    def take_pr_side(self, dest: Path, repo: str, message: str, pr: int, paths: list[str]) -> None:
        """Resolve each conflicting path to the PR's version (its deletion too) and complete the merge commit."""
        git = self.git
        try:
            for path in paths:
                if git.run(["ls-files", "-u", "--", path], dest).stdout.count(" 3\t"):
                    git.run(["checkout", "--theirs", "--", path], dest)
                    git.run(["add", "--", path], dest)
                else:  # the PR deleted it
                    git.run(["rm", "--quiet", "--", path], dest)
            line = f"conflicts in regenerated paths taken from PR #{pr}: {', '.join(paths)}"
            git.run(["commit", "--quiet", "--signoff", "-m", message, "-m", line], dest)
        except Refused:
            git.run(["merge", "--abort"], dest, ok=True)
            raise
        self.out(f"{repo}: {line}")

    # pins --------------------------------------------------------------------------------

    def pins(self) -> int:
        site = check.SITE
        try:
            heads = {}
            for repo in (check.CATALOG, check.ENGINE, site):
                if repo in check.MERGE_FIRST:  # no rc: the engine installs only a catalog commit on main
                    head = self.base_head(repo)
                    if head is None:
                        raise Refused(f"{repo} has no {self.cand.base} branch")
                else:
                    head = self.rc_head(repo)
                    if head is None:
                        raise Refused(f"{repo} has no {BRANCH} branch; run assemble first")
                heads[repo] = head
            want = {".catalog-ref": heads[check.CATALOG], ".framework-ref": heads[check.ENGINE]}
            if self.dry_run:
                for name, sha in want.items():
                    which = f"{self.cand.base} head" if name == ".catalog-ref" else f"{BRANCH} head"
                    self.out(f"{site}: would set {name} on {BRANCH} to {sha} (the {which})")
                self.out(
                    f"{site}: and the old engine commit in {ENGINE_COMMIT_FILE} and the chock pin of "
                    f"{ARCHITECTURE_DIR}/*.json, where the site still holds it"
                )
                self.out(f"{site}: one commit, pushed fast-forward only")
                return 0
            with tempfile.TemporaryDirectory(prefix="chock-rc-") as tmp:
                dest = Path(tmp) / site
                git = self.git
                git.run(["clone", "--quiet", "--no-tags", git.url_for(site), str(dest)])
                git.run(["checkout", "--quiet", "-B", BRANCH, f"origin/{BRANCH}"], dest)
                old_engine = {
                    sha for sha in (read_sha(dest / ENGINE_COMMIT_FILE), read_sha(dest / ".framework-ref")) if sha
                }
                for name, sha in want.items():
                    refuse_symlink(dest, dest / name)
                    (dest / name).write_text(sha + "\n", encoding="utf-8")
                copies = rewrite_engine_pins(dest, old_engine, heads[check.ENGINE])
                git.run(["add", *want, *copies], dest)
                if git.run(["diff", "--cached", "--quiet"], dest, ok=True).returncode == 0:
                    self.out(
                        f"{site}: {BRANCH} already pins the catalog's {self.cand.base} head "
                        f"and the engine's {BRANCH} head; nothing to do"
                    )
                    return 0
                message = (
                    f"rc: pin catalog {heads[check.CATALOG][:12]} ({self.cand.base} head) "
                    f"and engine {heads[check.ENGINE][:12]} ({BRANCH} head)"
                )
                git.run(["commit", "--quiet", "--signoff", "-m", message], dest)
                proc = git.run(["push", "--quiet", "origin", f"{BRANCH}:refs/heads/{BRANCH}"], dest, ok=True)
                if proc.returncode != 0:
                    raise Refused(
                        f"push of {BRANCH} was not a fast-forward or was refused: {proc.stderr.strip()[-300:]}"
                    )
                head = git.run(["rev-parse", "HEAD"], dest).stdout.strip()
                self.out(f"{site}: {BRANCH} {heads[site][:12]} -> {head[:12]} ({message})")
                if copies:
                    self.out(f"{site}: also moved the engine commit in {', '.join(copies)}")
                self.out(REGENERATE)
            return 0
        except Refused as exc:
            self.out(f"REFUSED: {exc} (nothing pushed)", file=sys.stderr)
            return 1

    # prs ---------------------------------------------------------------------------------

    def rc_pr(self, repo: str) -> dict | None:
        """The open PR from `rc` to the base, or None."""
        query = urllib.parse.urlencode({"head": f"{check.OWNER}:{BRANCH}", "base": self.cand.base, "state": "open"})
        found = self.get(repo, f"/pulls?{query}")
        prs = [p for p in found if isinstance(p, dict)] if isinstance(found, list) else []
        return prs[0] if prs else None

    def pr_title(self) -> str:
        return f"Release candidate {self.cand.version}-rc.{self.cand.rc}: do not merge"

    def pin_lines(self, repo: str) -> list[str]:
        """The site's two pins as they stand on its rc head (empty for any other repo)."""
        if repo != check.SITE:
            return []
        lines = []
        for name in (".catalog-ref", ".framework-ref"):
            data = self.get(repo, f"/contents/{name}?ref={BRANCH}")
            try:
                value = (
                    base64.b64decode(data.get("content", "")).decode("utf-8").strip() if isinstance(data, dict) else ""
                )
            except (ValueError, UnicodeDecodeError):
                value = ""
            lines.append(f"- {name}: {value if SHA_RE.fullmatch(value) else 'unreadable'}")
        return lines

    def pr_body(self, repo: str) -> str:
        carried = [f"- #{n}" for n in self.cand.prs[repo]] or ["- no PRs (this repo's main as is)"]
        parts = [
            "The staging candidate from org-plan plan/internal-release.md, never to be merged "
            "(promotion merges the listed PRs to main).",
            "Carries:\n" + "\n".join(carried),
        ]
        pins = self.pin_lines(repo)
        if pins:
            parts.append("Pins:\n" + "\n".join(pins))
        parts.append("Generated by rc.py")
        return "\n\n".join(parts)

    def prs(self) -> int:
        failed = 0
        for repo in self.repos():
            if self.skip_merge_first(repo):
                continue
            try:
                self.pr_repo(repo)
            except (Refused, check.GitHubError) as exc:
                failed += 1
                self.out(f"REFUSED {repo}: {exc}", file=sys.stderr)
        if failed:
            self.out(f"{failed} repo(s) refused; the others are done", file=sys.stderr)
        return 1 if failed else 0

    def pr_repo(self, repo: str) -> None:
        if self.rc_head(repo) is None:
            self.out(f"{repo}: no {BRANCH} branch, skipped")
            return
        if self.rc_head(repo) == self.base_head(repo):
            self.out(f"{repo}: {BRANCH} equals {self.cand.base}, no PR needed")
            return
        body = self.pr_body(repo)
        existing = self.rc_pr(repo)
        if existing is None:
            if self.dry_run:
                self.out(f"{repo}: would create draft PR {BRANCH} -> {self.cand.base}: {self.pr_title()}")
                return
            made = self.send(
                repo,
                "POST",
                "/pulls",
                {"title": self.pr_title(), "head": BRANCH, "base": self.cand.base, "body": body, "draft": True},
            )
            number = made.get("number") if isinstance(made, dict) else None
            self.out(f"{repo}: created draft PR #{number} {BRANCH} -> {self.cand.base}")
            return
        number = existing.get("number")
        if existing.get("body") == body:
            self.out(f"{repo}: PR #{number} body already current")
        elif self.dry_run:
            self.out(f"{repo}: would update the body of PR #{number} (title and draft state untouched)")
        else:
            self.send(repo, "PATCH", f"/pulls/{number}", {"body": body})  # the body only
            self.out(f"{repo}: updated the body of PR #{number} (title and draft state untouched)")

    def send(self, repo: str, method: str, path: str, body: dict) -> object | None:
        try:
            return self.gh.send(method, f"/repos/{check.OWNER}/{repo}{path}", body)
        except check.GitHubError as exc:
            raise Refused(f"GitHub API error: {exc}") from None

    # status ------------------------------------------------------------------------------

    def status(self) -> int:
        failed = 0
        base = self.cand.base
        for repo in self.repos():
            try:
                if repo in check.MERGE_FIRST:
                    self.status_merge_first(repo)
                    continue
                head = self.rc_head(repo)
                if head is None:
                    self.out(f"{repo:22} no {BRANCH} branch  rc PR: none  (PRs: {self.listed(repo)})")
                    continue
                cmp = self.get(repo, f"/compare/{base}...{BRANCH}")
                moved = cmp.get("behind_by", 0) if isinstance(cmp, dict) else None
                main = "unreadable" if moved is None else f"yes, {moved} commit(s) not in {BRANCH}" if moved else "no"
                prs = []
                for n in self.cand.prs[repo]:
                    prs.append(f"#{n} {'in' if self.contains(repo, head, n) else 'NOT in'}")
                pr = self.rc_pr(repo)
                shown = f"#{pr.get('number')}" if pr else "none"
                ci = f"  {self.ci_summary(repo, head)}" if pr else ""
                self.out(
                    f"{repo:22} {BRANCH} {head[:12]}  rc PR: {shown}  {base} moved: {main}  "
                    f"PRs: {', '.join(prs) or 'none'}{ci}"
                )
            except Refused as exc:
                failed += 1
                self.out(f"{repo:22} {exc}", file=sys.stderr)
        return 1 if failed else 0

    def status_merge_first(self, repo: str) -> None:
        """A merge-first repo: its base-branch head and whether each listed PR is merged (no rc, no rc PR)."""
        head = self.base_head(repo)
        if head is None:
            raise Refused(f"no {self.cand.base} branch")
        prs = []
        for n in self.cand.prs[repo]:
            pr = self.get(repo, f"/pulls/{n}")
            if not isinstance(pr, dict):
                prs.append(f"#{n} NOT merged (not found)")
            else:
                prs.append(f"#{n} merged" if pr.get("merged") else f"#{n} NOT merged ({pr.get('state')})")
        self.out(f"{repo:22} {self.cand.base} {head[:12]}  merge-first  PRs: {', '.join(prs) or 'none'}")

    def ci_summary(self, repo: str, head: str) -> str:
        """The line "ci: N success, M failure" from the check runs of the rc head: one API call."""
        try:
            data = self.get(repo, f"/commits/{head}/check-runs?per_page=100")
        except Refused:
            return "ci: unreadable"
        runs = data.get("check_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            return "ci: unreadable"
        done = [r.get("conclusion") for r in runs if isinstance(r, dict)]
        text = f"ci: {done.count('success')} success, {sum(c in ('failure', 'timed_out') for c in done)} failure"
        other = len(done) - done.count("success") - sum(c in ("failure", "timed_out") for c in done)
        return text + (f", {other} other" if other else "")

    def listed(self, repo: str) -> str:
        return ", ".join(f"#{n}" for n in self.cand.prs[repo]) or "none"

    def contains(self, repo: str, head: str, n: int) -> bool:
        """True when PR <n>'s head commit is an ancestor of the rc head."""
        pr = self.get(repo, f"/pulls/{n}")
        sha = pr.get("head", {}).get("sha") if isinstance(pr, dict) else None
        if not isinstance(sha, str):
            return False
        cmp = self.get(repo, f"/compare/{head}...{sha}")
        return isinstance(cmp, dict) and cmp.get("status") in ("behind", "identical")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=["assemble", "pins", "prs", "status"])
    parser.add_argument("candidate", type=Path, help="candidates/<version>-rc.<n>.yaml")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    parser.add_argument(
        "--only", action="append", metavar="REPO", help="assemble, prs, status: only this repo (repeatable)"
    )
    parser.add_argument("--reset", action="store_true", help="assemble: delete and recreate rc from main (owner only)")
    parser.add_argument(
        "--i-am-the-owner", action="store_true", help="the owner's flag for --reset; an agent never passes it"
    )
    args = parser.parse_args(argv)
    if args.only and args.verb == "pins":
        parser.error("--only belongs to assemble, prs and status")
    if args.reset and args.verb != "assemble":
        parser.error("--reset belongs to assemble")
    if args.reset and not args.i_am_the_owner:
        print(
            "REFUSED: --reset deletes and recreates rc; it needs --i-am-the-owner, which is the owner's alone",
            file=sys.stderr,
        )
        return 1

    result = check.Result()
    cand = check.load_candidate(args.candidate, result)
    if cand is None:
        print(f"REFUSED: {args.candidate.name} is not a candidate file:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    unknown = [name for name in args.only or [] if name not in cand.prs]
    if unknown:
        parser.error(f"--only names {', '.join(unknown)}, which is not in {args.candidate.name}")

    merge_first = [name for name in args.only or [] if name in check.MERGE_FIRST]
    if merge_first and args.verb in ("assemble", "prs"):
        parser.error(
            f"--only names {', '.join(merge_first)}, which is merge-first: its PRs are merged to {cand.base} "
            f"(gated by its own CI) before the candidate pins it, so {args.verb} creates no {BRANCH} branch "
            "and no draft PR for it"
        )

    token = os.environ.get("GITHUB_TOKEN") or None
    rc = Rc(
        check.GitHub(token=token),
        Git(token=token),
        cand,
        args.dry_run,
        out=lambda *a, **k: print(*a, **k),
        only=args.only,
    )
    if args.verb == "assemble":
        if args.dry_run:
            print(f"Dry run: assemble {args.candidate.name}{' --reset' if args.reset else ''} (nothing is touched)")
        return rc.assemble(args.reset)
    if args.verb == "pins":
        return rc.pins()
    if args.verb == "prs":
        return rc.prs()
    return rc.status()


if __name__ == "__main__":
    sys.exit(main())
