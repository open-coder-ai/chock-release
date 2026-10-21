#!/usr/bin/env python3
"""Assemble and inspect the `rc` branches of a release candidate (candidates/<version>-rc.<n>.yaml).

  assemble  For each train repo: clone it, create `rc` from origin/main only when `rc` does not
            exist (else check it out and merge origin/main in), merge each listed PR from
            refs/pull/<n>/head with a merge commit (--no-ff), and push `rc` fast-forward only.
            A PR that is not open, a conflict, or a push that is not a fast-forward stops that repo
            with the repo and PR printed; nothing is pushed for it. Other repos still run.
  pins      One commit on chock-site's `rc` setting .catalog-ref and .framework-ref to the
            catalog's and the engine's `rc` heads, pushed fast-forward only.
  status    Each repo's `rc` head, which listed PRs it contains, and whether main moved under it.

assemble and status take --only <repo> (repeatable) to cover part of the train, for a session whose
GitHub scope reaches one repo; a repo that is not in the candidate file is a usage error.
Every verb takes --dry-run: it prints the plan, reads GitHub, and touches nothing.
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
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import check

BRANCH = check.CANDIDATE_BRANCH
IDENTITY = ("Jothi Mani Rajendran", "250249270+jothimani-rajendran@users.noreply.github.com")


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

    # GitHub reads ------------------------------------------------------------------------

    def get(self, repo: str, path: str) -> object | None:
        try:
            return self.gh.get(f"/repos/{check.OWNER}/{repo}{path}")
        except check.GitHubError as exc:
            raise Refused(f"GitHub API error: {exc}") from None

    def rc_head(self, repo: str) -> str | None:
        ref = self.get(repo, f"/git/ref/heads/{BRANCH}")
        sha = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        return sha if isinstance(sha, str) else None

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
                try:
                    self.assemble_repo(repo, Path(tmp) / repo, reset)
                except Refused as exc:
                    failed += 1
                    self.out(f"REFUSED {repo}: {exc} (nothing pushed for {repo})", file=sys.stderr)
        if failed:
            self.out(f"{failed} repo(s) refused; the others are done", file=sys.stderr)
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
            self.merge(dest, repo, f"origin/{base}", f"Merge {base} into {BRANCH}", f"{base}")
        else:
            git.run(["checkout", "--quiet", "-B", BRANCH, f"origin/{base}"], dest)
        for n, sha in prs.items():
            local = f"refs/rc-pr/{n}"
            git.run(["fetch", "--quiet", "origin", f"+refs/pull/{n}/head:{local}"], dest)
            got = git.run(["rev-parse", local], dest).stdout.strip()
            if got != sha:
                raise Refused(f"PR #{n} moved while assembling (GitHub said {sha[:12]}, fetched {got[:12]})")
            self.merge(dest, repo, local, f"Merge pull request #{n} into {BRANCH}", f"PR #{n}")

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

    def merge(self, dest: Path, repo: str, ref: str, message: str, label: str) -> None:
        """Merge <ref> into HEAD with a merge commit; a conflict aborts and refuses with the repo and PR."""
        git = self.git
        if git.run(["merge-base", "--is-ancestor", ref, "HEAD"], dest, ok=True).returncode == 0:
            return  # already contained: a repeat round adds nothing
        proc = git.run(["merge", "--no-ff", "--signoff", "-m", message, ref], dest, ok=True)
        if proc.returncode == 0:
            return
        unmerged = git.run(["diff", "--name-only", "--diff-filter=U"], dest, ok=True).stdout.split()
        git.run(["merge", "--abort"], dest, ok=True)
        if unmerged:
            raise Refused(f"CONFLICT merging {label} into {BRANCH} ({', '.join(unmerged[:5])})")
        raise Refused(f"cannot merge {label} into {BRANCH}: {(proc.stdout + proc.stderr).strip()[-300:]}")

    # pins --------------------------------------------------------------------------------

    def pins(self) -> int:
        site = check.SITE
        try:
            heads = {}
            for repo in (check.CATALOG, check.ENGINE, site):
                head = self.rc_head(repo)
                if head is None:
                    raise Refused(f"{repo} has no {BRANCH} branch; run assemble first")
                heads[repo] = head
            want = {".catalog-ref": heads[check.CATALOG], ".framework-ref": heads[check.ENGINE]}
            if self.dry_run:
                for name, sha in want.items():
                    self.out(f"{site}: would set {name} on {BRANCH} to {sha}")
                self.out(f"{site}: one commit, pushed fast-forward only")
                return 0
            with tempfile.TemporaryDirectory(prefix="chock-rc-") as tmp:
                dest = Path(tmp) / site
                git = self.git
                git.run(["clone", "--quiet", "--no-tags", git.url_for(site), str(dest)])
                git.run(["checkout", "--quiet", "-B", BRANCH, f"origin/{BRANCH}"], dest)
                for name, sha in want.items():
                    (dest / name).write_text(sha + "\n", encoding="utf-8")
                git.run(["add", *want], dest)
                if git.run(["diff", "--cached", "--quiet"], dest, ok=True).returncode == 0:
                    self.out(f"{site}: {BRANCH} already pins {', '.join(want)} to the rc heads; nothing to do")
                    return 0
                message = f"rc: pin catalog {heads[check.CATALOG][:12]} and engine {heads[check.ENGINE][:12]}"
                git.run(["commit", "--quiet", "--signoff", "-m", message], dest)
                proc = git.run(["push", "--quiet", "origin", f"{BRANCH}:refs/heads/{BRANCH}"], dest, ok=True)
                if proc.returncode != 0:
                    raise Refused(
                        f"push of {BRANCH} was not a fast-forward or was refused: {proc.stderr.strip()[-300:]}"
                    )
                head = git.run(["rev-parse", "HEAD"], dest).stdout.strip()
                self.out(f"{site}: {BRANCH} {heads[site][:12]} -> {head[:12]} ({message})")
            return 0
        except Refused as exc:
            self.out(f"REFUSED: {exc} (nothing pushed)", file=sys.stderr)
            return 1

    # status ------------------------------------------------------------------------------

    def status(self) -> int:
        failed = 0
        base = self.cand.base
        for repo in self.repos():
            try:
                head = self.rc_head(repo)
                if head is None:
                    self.out(f"{repo:22} no {BRANCH} branch  (PRs: {self.listed(repo)})")
                    continue
                cmp = self.get(repo, f"/compare/{base}...{BRANCH}")
                moved = cmp.get("behind_by", 0) if isinstance(cmp, dict) else None
                main = "unreadable" if moved is None else f"yes, {moved} commit(s) not in {BRANCH}" if moved else "no"
                prs = []
                for n in self.cand.prs[repo]:
                    prs.append(f"#{n} {'in' if self.contains(repo, head, n) else 'NOT in'}")
                self.out(f"{repo:22} {BRANCH} {head[:12]}  {base} moved: {main}  PRs: {', '.join(prs) or 'none'}")
            except Refused as exc:
                failed += 1
                self.out(f"{repo:22} {exc}", file=sys.stderr)
        return 1 if failed else 0

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
    parser.add_argument("verb", choices=["assemble", "pins", "status"])
    parser.add_argument("candidate", type=Path, help="candidates/<version>-rc.<n>.yaml")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    parser.add_argument("--only", action="append", metavar="REPO", help="assemble, status: only this repo (repeatable)")
    parser.add_argument("--reset", action="store_true", help="assemble: delete and recreate rc from main (owner only)")
    parser.add_argument(
        "--i-am-the-owner", action="store_true", help="the owner's flag for --reset; an agent never passes it"
    )
    args = parser.parse_args(argv)
    if args.only and args.verb == "pins":
        parser.error("--only belongs to assemble and status")
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
    return rc.status()


if __name__ == "__main__":
    sys.exit(main())
