"""rc.py against local bare repositories standing in for GitHub, and a fake REST API over them.

Real git does the merging and pushing, so a conflict or a non-fast-forward push is the real
thing; only the network is replaced. No test reaches GitHub.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import check
import rc
from conftest import ROOT, VERSION

OWNER_EMAIL = "250249270+jothimani-rajendran@users.noreply.github.com"

# The one candidate file the repo ships.
SHIPPED = next(iter(sorted((ROOT / "candidates").glob("*-rc.*.yaml"))))


def git(*args: str, cwd: Path | None = None) -> str:
    argv = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]
    proc = subprocess.run([*argv, *args], cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr}")
    return proc.stdout.strip()


class Remote:
    """One bare repository per train repo, each seeded with a main branch."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for repo in check.TRAIN:
            bare = self.path(repo)
            git("init", "--quiet", "--bare", "-b", "main", str(bare))
            self.commit(repo, "main", {"README.md": f"{repo}\n", "app.txt": "line1\nline2\nline3\n"})

    def path(self, repo: str) -> Path:
        return self.root / f"{repo}.git"

    def url(self, repo: str) -> str:
        return str(self.path(repo))

    def commit(self, repo: str, ref: str, files: dict[str, str], parent: str | None = None) -> str:
        """Commit <files> on top of <parent> (default: the branch ref) and push it to <ref> (a branch or full ref)."""
        work = self.root / f"work-{repo}-{len(list(self.root.glob('work-*')))}"
        git("init", "--quiet", "-b", "tmp", str(work))
        git("remote", "add", "origin", str(self.path(repo)), cwd=work)
        base = parent or (ref if "/" not in ref else "main")
        if self.head(repo, base):
            git("fetch", "--quiet", "origin", base, cwd=work)
            git("checkout", "--quiet", "-B", "tmp", "FETCH_HEAD", cwd=work)
        for name, text in files.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(text, encoding="utf-8")
        git("add", ".", cwd=work)
        git("commit", "--quiet", "-m", f"fixture {sorted(files)}", cwd=work)
        target = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
        git("push", "--quiet", "origin", f"HEAD:{target}", cwd=work)
        return git("rev-parse", "HEAD", cwd=work)

    def pr(self, repo: str, n: int, files: dict[str, str]) -> str:
        return self.commit(repo, f"refs/pull/{n}/head", files, parent="main")

    def head(self, repo: str, ref: str) -> str | None:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.path(repo)), "rev-parse", "--verify", "--quiet", ref],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() or None

    def log(self, repo: str, ref: str = "rc", fmt: str = "%H") -> list[str]:
        return git("-C", str(self.path(repo)), "log", f"--format={fmt}", ref).splitlines()


class LocalGH(check.GitHub):
    """The slice of the REST API rc.py reads, answered from the bare repositories."""

    def __init__(self, remote: Remote) -> None:
        super().__init__(token=None)
        self.remote = remote
        self.states: dict[tuple[str, int], str] = {}
        self.bases: dict[tuple[str, int], str] = {}
        self.head_repos: dict[tuple[str, int], str | None] = {}  # (repo, PR) -> head repo name; None = deleted
        self.merged: set[tuple[str, int]] = set()  # PRs GitHub reports as merged (state closed)
        self.rc_prs: dict[str, dict] = {}  # repo -> the open rc -> main PR
        self.sent: list[tuple[str, str, dict]] = []  # every write: (method, path, body)
        self.check_runs: dict[str, list[str | None]] = {}  # repo -> conclusions of the rc head's check runs
        self.api_calls: list[str] = []

    def send(self, method: str, path: str, body: dict) -> object | None:
        self.sent.append((method, path, body))
        repo, _, rest = path[len(f"/repos/{check.OWNER}/") :].partition("/")
        if method == "POST" and rest == "pulls":
            pr = {
                "number": 100 + len(self.rc_prs),
                "title": body["title"],
                "draft": body["draft"],
                "body": body["body"],
            }
            self.rc_prs[repo] = pr
            return pr
        if method == "PATCH" and rest == f"pulls/{self.rc_prs[repo]['number']}":
            self.rc_prs[repo]["body"] = body["body"]
            return self.rc_prs[repo]
        raise AssertionError(f"unexpected {method} {path}")

    def get(self, path: str) -> object | None:
        prefix = f"/repos/{check.OWNER}/"
        repo, _, rest = path[len(prefix) :].partition("/")
        self.api_calls.append(path)
        if rest.startswith("commits/") and "/check-runs" in rest:
            runs = self.check_runs.get(repo, [])
            return {"total_count": len(runs), "check_runs": [{"conclusion": c} for c in runs]}
        if rest.startswith("pulls?"):
            assert "head=open-coder-ai%3Arc" in rest and "base=main" in rest and "state=open" in rest
            return [self.rc_prs[repo]] if repo in self.rc_prs else []
        if rest.startswith("contents/"):
            name = rest[len("contents/") :].split("?")[0]
            blob = subprocess.run(  # noqa: S603
                ["git", "-C", str(self.remote.path(repo)), "show", f"rc:{name}"],  # noqa: S607
                capture_output=True,
                check=False,
            )
            if blob.returncode != 0:
                return None
            return {"encoding": "base64", "content": base64.b64encode(blob.stdout).decode()}
        if rest.startswith("git/ref/heads/"):
            sha = self.remote.head(repo, f"refs/heads/{rest[len('git/ref/heads/') :]}")
            return {"object": {"sha": sha}} if sha else None
        if rest.startswith("pulls/"):
            n = int(rest.split("/")[1])
            sha = self.remote.head(repo, f"refs/pull/{n}/head")
            if sha is None:
                return None
            merged = (repo, n) in self.merged
            return {
                "state": "closed" if merged else self.states.get((repo, n), "open"),
                "merged": merged,
                "base": {"ref": self.bases.get((repo, n), "main")},
                "head": {
                    "sha": sha,
                    "repo": (
                        {"full_name": self.head_repos.get((repo, n), f"{check.OWNER}/{repo}")}
                        if self.head_repos.get((repo, n), "") is not None
                        else None
                    ),
                },
            }
        if rest.startswith("compare/"):
            a, _, b = rest[len("compare/") :].partition("...")
            bare = str(self.remote.path(repo))
            try:
                ahead = int(git("-C", bare, "rev-list", "--count", f"{a}..{b}"))
                behind = int(git("-C", bare, "rev-list", "--count", f"{b}..{a}"))
            except AssertionError:
                return None
            status = {(0, 0): "identical"}.get((ahead, behind)) or (
                "ahead" if behind == 0 else "behind" if ahead == 0 else "diverged"
            )
            return {"status": status, "ahead_by": ahead, "behind_by": behind}
        raise AssertionError(f"unexpected GET {path}")


def candidate(
    regenerated: dict[str, list[str]] | None = None,
    forks: dict[str, dict[int, str]] | None = None,
    **prs: list[int],
) -> check.Candidate:
    table = {repo: [] for repo in check.TRAIN}
    table.update({repo.replace("_", "-"): numbers for repo, numbers in prs.items()})
    return check.Candidate(
        version=VERSION, rc=1, base="main", prs=table, regenerated=regenerated or {}, forks=forks or {}
    )


class Lines:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []

    def __call__(self, *args, file=None, **_kw) -> None:
        (self.err if file is not None else self.out).append(" ".join(str(a) for a in args))


@pytest.fixture(scope="session")
def seeded(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("seed")
    Remote(root)
    return root


@pytest.fixture
def remote(seeded, tmp_path) -> Remote:
    copy = tmp_path / "remote"
    shutil.copytree(seeded, copy)
    fresh = Remote.__new__(Remote)
    fresh.root = copy
    return fresh


@pytest.fixture
def gh(remote) -> LocalGH:
    return LocalGH(remote)


def make(remote, gh, cand, dry_run=False, git_cls=rc.Git) -> tuple[rc.Rc, Lines]:
    lines = Lines()
    return rc.Rc(gh, git_cls(url_for=remote.url, protocols="file"), cand, dry_run, out=lines), lines


CATALOG = check.CATALOG

# assemble ---------------------------------------------------------------------------------


def test_assemble_creates_rc_from_main_and_merges_each_pr_with_a_merge_commit(remote, gh):
    one = remote.pr("chock", 1, {"one.txt": "1\n"})
    two = remote.pr("chock", 2, {"two.txt": "2\n"})
    main_head = remote.head("chock", "refs/heads/main")
    r, lines = make(remote, gh, candidate(chock=[1, 2]))
    assert r.assemble(reset=False) == 0, lines.err
    parents = remote.log("chock", fmt="%P")
    assert [len(p.split()) for p in parents[:2]] == [2, 2]  # two merge commits on top of main
    assert remote.head("chock", "rc^1^1") == main_head
    assert remote.head("chock", "rc^2") == two and remote.head("chock", "rc^^2") == one
    assert remote.head("chock", "refs/heads/main") == main_head  # main never moves
    # every other repo gets an rc equal to its main
    assert remote.head("chock-site", "rc") == remote.head("chock-site", "main")


def test_commits_are_the_owner_with_a_sign_off(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    r, _ = make(remote, gh, candidate(chock=[1]))
    r.assemble(reset=False)
    who = remote.log("chock", fmt="%an <%ae>|%cn <%ce>")[0]
    assert who == f"Jothi Mani Rajendran <{OWNER_EMAIL}>|Jothi Mani Rajendran <{OWNER_EMAIL}>"
    body = git("-C", str(remote.path("chock")), "log", "-1", "--format=%B", "rc")
    assert f"Signed-off-by: Jothi Mani Rajendran <{OWNER_EMAIL}>" in body


def test_second_round_adds_to_the_existing_rc_and_merges_main_in(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    r, _ = make(remote, gh, candidate(chock=[1]))
    r.assemble(reset=False)
    first = remote.head("chock", "rc")
    remote.pr("chock", 2, {"two.txt": "2\n"})
    remote.commit("chock", "main", {"news.txt": "main moved\n"})
    r, lines = make(remote, gh, candidate(chock=[1, 2]))
    assert r.assemble(reset=False) == 0, lines.err
    assert first in remote.log("chock")  # the old rc is history, not rewritten
    tree = git("-C", str(remote.path("chock")), "ls-tree", "--name-only", "rc")
    assert {"one.txt", "two.txt", "news.txt"} <= set(tree.split())
    # a repeat round with nothing new pushes nothing
    before = remote.head("chock", "rc")
    r, lines = make(remote, gh, candidate(chock=[1, 2]))
    assert r.assemble(reset=False) == 0
    assert remote.head("chock", "rc") == before
    assert any("nothing to push" in line for line in lines.out)


def test_conflict_stops_that_repo_prints_repo_and_pr_and_pushes_nothing(remote, gh):
    remote.pr("chock", 1, {"app.txt": "line1\nfrom one\nline3\n"})
    remote.pr("chock", 2, {"app.txt": "line1\nfrom two\nline3\n"})
    remote.pr("chock-site", 5, {"site.txt": "ok\n"})
    r, lines = make(remote, gh, candidate(chock=[1, 2], chock_site=[5]))
    assert r.assemble(reset=False) == 1
    refusal = [e for e in lines.err if e.startswith("REFUSED chock:")]
    assert refusal and "CONFLICT" in refusal[0] and "PR #2" in refusal[0] and "nothing pushed for chock" in refusal[0]
    assert remote.head("chock", "refs/heads/rc") is None  # not even PR #1 reached the remote
    assert remote.head("chock-site", "refs/heads/rc")  # the other repos still ran


SITE_GLOBS = {"chock-site": [".framework-ref", "data/snap/**"]}


def conflicting_site(remote, extra: dict[str, str] | None = None) -> None:
    """rc holds PR 1's pins and snapshot; PR 2 rewrites the same files (and `extra`) differently."""
    remote.pr("chock-site", 1, {".framework-ref": "rc-pin\n", "data/snap/a.json": "rc\n"})
    remote.pr("chock-site", 2, {".framework-ref": "pr-pin\n", "data/snap/a.json": "pr\n", **(extra or {})})


def test_conflict_confined_to_regenerated_paths_takes_the_pr_side(remote, gh):
    conflicting_site(remote)
    r, lines = make(remote, gh, candidate(SITE_GLOBS, chock_site=[1, 2]))
    assert r.assemble(reset=False) == 0, lines.err
    show = lambda path: git("-C", str(remote.path("chock-site")), "show", f"rc:{path}")  # noqa: E731
    assert show(".framework-ref") == "pr-pin" and show("data/snap/a.json") == "pr"
    body = git("-C", str(remote.path("chock-site")), "log", "-1", "--format=%B", "rc")
    line = "conflicts in regenerated paths taken from PR #2: .framework-ref, data/snap/a.json"
    assert line in body and "Signed-off-by: Jothi Mani Rajendran" in body
    assert len(remote.log("chock-site", fmt="%P")[0].split()) == 2  # a merge commit
    assert f"chock-site: {line}" in lines.out


def test_conflict_partly_outside_the_regenerated_paths_refuses_naming_them(remote, gh):
    remote.pr("chock-site", 1, {".framework-ref": "rc-pin\n", "app.txt": "line1\nfrom one\nline3\n"})
    remote.pr("chock-site", 2, {".framework-ref": "pr-pin\n", "app.txt": "line1\nfrom two\nline3\n"})
    r, lines = make(remote, gh, candidate(SITE_GLOBS, chock_site=[1, 2]))
    assert r.assemble(reset=False) == 1
    refusal = [e for e in lines.err if e.startswith("REFUSED chock-site:")]
    assert refusal and "outside the regenerated paths: app.txt" in refusal[0] and "PR #2" in refusal[0]
    assert ".framework-ref" not in refusal[0].split("outside the regenerated paths:")[1]
    assert remote.head("chock-site", "refs/heads/rc") is None  # nothing pushed


def test_a_repo_without_a_regenerated_entry_refuses_a_conflict_as_before(remote, gh):
    conflicting_site(remote)
    r, lines = make(remote, gh, candidate({"chock": ["*.txt"]}, chock_site=[1, 2]))
    assert r.assemble(reset=False) == 1
    refusal = [e for e in lines.err if e.startswith("REFUSED chock-site:")]
    assert refusal and "CONFLICT" in refusal[0] and "outside" not in refusal[0]
    assert remote.head("chock-site", "refs/heads/rc") is None


def test_regenerated_paths_never_excuse_a_conflict_with_main(remote, gh):
    remote.pr("chock-site", 1, {".framework-ref": "pr-pin\n"})
    r, _ = make(remote, gh, candidate(SITE_GLOBS, chock_site=[1]))
    assert r.assemble(reset=False) == 0
    remote.commit("chock-site", "main", {".framework-ref": "main-pin\n"})
    remote.commit("chock-site", "rc", {".framework-ref": "rc-pin\n"})
    r, lines = make(remote, gh, candidate(SITE_GLOBS, chock_site=[1]))
    assert r.assemble(reset=False) == 1
    assert any(e.startswith("REFUSED chock-site:") and "CONFLICT merging main" in e for e in lines.err)


def test_a_path_the_pr_deleted_is_taken_as_deleted(remote, gh):
    remote.commit("chock-site", "main", {"data/snap/a.json": "main\n"})
    remote.pr("chock-site", 1, {"data/snap/a.json": "rc\n"})
    work = remote.root / "work-delete"
    git("clone", "--quiet", str(remote.path("chock-site")), str(work))
    git("rm", "--quiet", "data/snap/a.json", cwd=work)
    git("commit", "--quiet", "-m", "delete", cwd=work)
    git("push", "--quiet", "-f", "origin", "HEAD:refs/pull/2/head", cwd=work)
    r, lines = make(remote, gh, candidate(SITE_GLOBS, chock_site=[1, 2]))
    assert r.assemble(reset=False) == 0, lines.err
    assert "data/snap/a.json" not in git("-C", str(remote.path("chock-site")), "ls-tree", "-r", "--name-only", "rc")


@pytest.mark.parametrize(
    ("pattern", "path", "hit"),
    [
        (".framework-ref", ".framework-ref", True),
        (".framework-ref", "sub/.framework-ref", False),
        ("data/x/**", "data/x/a/b.json", True),
        ("data/x/*", "data/x/a/b.json", False),
        ("data/x/**", "data/xy/a.json", False),
        ("data/?.txt", "data/a.txt", True),
    ],
)
def test_glob_match(pattern, path, hit):
    assert rc.glob_match(pattern, path) is hit


def test_pr_that_is_not_open_is_refused_before_anything_is_cloned(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    gh.states[("chock", 1)] = "closed"

    class NoGit(rc.Git):
        def run(self, *a, **k):
            raise AssertionError("git must not run for a refused repo")

    r, lines = make(remote, gh, candidate(chock=[1]), git_cls=NoGit)
    with pytest.raises(AssertionError):  # the other repos are fine and do clone; only chock is refused
        r.assemble(reset=False)
    assert any(e.startswith("REFUSED chock:") and "PR #1: not open" in e for e in lines.err)
    assert remote.head("chock", "refs/heads/rc") is None


def test_pr_against_another_base_or_missing_is_refused(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    gh.bases[("chock", 1)] = "dev"
    r, _ = make(remote, gh, candidate(chock=[1]))
    with pytest.raises(rc.Refused, match="targets 'dev'"):
        r.open_prs("chock")
    r, _ = make(remote, gh, candidate(chock=[404]))
    with pytest.raises(rc.Refused, match="PR #404: not found"):
        r.open_prs("chock")


def test_fork_pr_is_refused_before_anything_is_fetched(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    gh.head_repos[("chock", 1)] = "outsider/chock"

    class NoGit(rc.Git):
        def run(self, *a, **k):
            raise AssertionError("git must not run for a refused repo")

    r, lines = make(remote, gh, candidate(chock=[1]), git_cls=NoGit)
    with pytest.raises(rc.Refused, match=r"PR #1: head is outsider/chock, not open-coder-ai/chock"):
        r.open_prs("chock")
    with pytest.raises(AssertionError):  # the other repos clone; only chock is refused
        r.assemble(reset=False)
    assert any(e.startswith("REFUSED chock:") and "outsider/chock" in e for e in lines.err)
    assert remote.head("chock", "refs/heads/rc") is None


def test_pr_whose_head_repository_is_gone_is_refused(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    gh.head_repos[("chock", 1)] = None
    r, _ = make(remote, gh, candidate(chock=[1]))
    with pytest.raises(rc.Refused, match="PR #1: no head repository"):
        r.open_prs("chock")


def test_fork_pr_the_candidate_names_is_allowed_and_only_that_fork(remote, gh):
    sha = remote.pr("chock", 1, {"one.txt": "1\n"})
    gh.head_repos[("chock", 1)] = "friend/chock"
    r, _ = make(remote, gh, candidate(chock=[1], forks={"chock": {1: "friend/chock"}}))
    assert r.open_prs("chock") == {1: sha}
    gh.head_repos[("chock", 1)] = "outsider/chock"  # the PR now comes from another fork
    with pytest.raises(
        rc.Refused, match=r"head is outsider/chock, not open-coder-ai/chock \(allowed fork: friend/chock\)"
    ):
        r.open_prs("chock")


def test_fork_allowance_for_one_pr_does_not_cover_another(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    remote.pr("chock", 2, {"two.txt": "2\n"})
    gh.head_repos[("chock", 1)] = "friend/chock"
    gh.head_repos[("chock", 2)] = "friend/chock"
    r, _ = make(remote, gh, candidate(chock=[1, 2], forks={"chock": {1: "friend/chock"}}))
    with pytest.raises(rc.Refused, match="PR #2: head is friend/chock"):
        r.open_prs("chock")


def test_push_that_is_not_a_fast_forward_is_refused_and_never_forced(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    remote.commit("chock", "rc", {"seed.txt": "x\n"}, parent="main")  # an rc exists to be raced
    raced: list[str] = []

    class Racing(rc.Git):
        def run(self, args, cwd=None, ok=False):
            if args[0] == "push" and not raced:
                raced.append("x")
                remote.commit("chock", "rc", {"other.txt": "someone else\n"})  # rc moves after our clone
            return super().run(args, cwd, ok)

    r, lines = make(remote, gh, candidate(chock=[1]), git_cls=Racing)
    assert r.assemble(reset=False) == 1
    assert any(e.startswith("REFUSED chock:") and "not a fast-forward" in e for e in lines.err)
    assert "other.txt" in git("-C", str(remote.path("chock")), "ls-tree", "--name-only", "rc")  # their commit survives


def test_reset_without_the_owner_flag_is_refused_before_any_work(monkeypatch, capsys):
    monkeypatch.setattr(check, "GitHub", lambda token=None: pytest.fail("no GitHub call may precede the refusal"))
    code = rc.main(["assemble", str(SHIPPED), "--reset"])
    assert code == 1
    assert "needs --i-am-the-owner" in capsys.readouterr().err
    # --dry-run does not soften it
    assert rc.main(["assemble", str(SHIPPED), "--reset", "--dry-run"]) == 1


def test_reset_with_the_owner_flag_recreates_rc_from_main(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    r, _ = make(remote, gh, candidate(chock=[1]))
    r.assemble(reset=False)
    old = remote.head("chock", "rc")
    r, lines = make(remote, gh, candidate())
    assert r.assemble(reset=True) == 0, lines.err
    assert remote.head("chock", "rc") == remote.head("chock", "main") != old


def test_dry_run_prints_the_plan_and_touches_nothing(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})

    class NoGit(rc.Git):
        def run(self, *a, **k):
            raise AssertionError("dry run must not run git")

    r, lines = make(remote, gh, candidate(chock=[1]), dry_run=True, git_cls=NoGit)
    assert r.assemble(reset=False) == 0
    assert any(
        line.startswith("chock: would clone, create rc from origin/main, merge #1 (--no-ff)") for line in lines.out
    )
    assert remote.head("chock", "refs/heads/rc") is None
    r, lines = make(remote, gh, candidate(chock=[1]), dry_run=True, git_cls=NoGit)
    assert r.pins() == 1  # no rc exists yet: refused, still nothing touched
    assert remote.head("chock-site", "refs/heads/rc") is None


def test_candidate_file_with_an_unknown_verb_or_missing_file_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        rc.main(["tag", str(tmp_path / "x.yaml")])
    assert exc.value.code == 2
    assert rc.main(["status", str(tmp_path / f"{VERSION}-rc.1.yaml")]) == 1


# pins -------------------------------------------------------------------------------------


def test_pins_makes_one_signed_commit_on_site_rc_and_is_idempotent(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    r, _ = make(remote, gh, candidate(chock=[1]))
    r.assemble(reset=False)
    before = len(remote.log("chock-site"))
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 0, lines.err
    assert len(remote.log("chock-site")) == before + 1
    show = lambda name: git("-C", str(remote.path("chock-site")), "show", f"rc:{name}")  # noqa: E731
    assert show(".catalog-ref") == remote.head("chock-catalog", "main")  # merge-first: main, there is no rc
    assert show(".framework-ref") == remote.head("chock", "rc")
    assert remote.head("chock-catalog", "refs/heads/rc") is None
    body = git("-C", str(remote.path("chock-site")), "log", "-1", "--format=%an <%ae>%n%B", "rc")
    assert (
        f"Jothi Mani Rajendran <{OWNER_EMAIL}>" in body
        and f"Signed-off-by: Jothi Mani Rajendran <{OWNER_EMAIL}>" in body
    )
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 0
    assert len(remote.log("chock-site")) == before + 1
    assert any("nothing to do" in line for line in lines.out)


OLD_ENGINE = "e5e05a0317b8589361f423605829ba368e16e5bd"
OTHER_ENGINE = "39df00bbf7646a08481247f31d5091d09850f96f"


def diagram(chock: str) -> str:
    pins = f'"pins": {{\n    "chock": "{chock}",\n    "agentseam": "{OLD_ENGINE}"\n  }}'
    return f'{{\n  "id": "d",\n  {pins},\n  "n": 1\n}}\n'


def seed_site_with_old_engine(remote) -> None:
    remote.commit(
        "chock-site",
        "rc",
        {
            ".framework-ref": OLD_ENGINE + "\n",
            "data/kinds/engine-commit.txt": OLD_ENGINE + "\n",
            "data/architecture/lifecycle.json": diagram(OLD_ENGINE),
            "data/architecture/whole-train.json": diagram(OTHER_ENGINE),
        },
        parent="main",
    )


def test_pins_moves_the_sites_hand_kept_engine_pins_in_the_same_commit(remote, gh):
    seed_site_with_old_engine(remote)
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")
    new = remote.head("chock", "rc")
    before = len(remote.log("chock-site"))
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 0, lines.err
    assert len(remote.log("chock-site")) == before + 1
    show = lambda name: git("-C", str(remote.path("chock-site")), "show", f"rc:{name}")  # noqa: E731
    assert show(".framework-ref") == new
    assert show("data/kinds/engine-commit.txt") == new
    # only the chock pin that held the old engine commit moves; formatting and other fields stay
    assert show("data/architecture/lifecycle.json") == diagram(new).rstrip("\n")
    assert show("data/architecture/whole-train.json") == diagram(OTHER_ENGINE).rstrip("\n")
    assert any(
        "also moved the engine commit in" in line and "data/architecture/lifecycle.json" in line for line in lines.out
    )
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 0
    assert len(remote.log("chock-site")) == before + 1
    assert any("nothing to do" in line for line in lines.out)


def test_pins_finds_the_old_engine_commit_in_engine_commit_txt_when_framework_ref_already_moved(remote, gh):
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")
    new = remote.head("chock", "rc")
    remote.commit(
        "chock-site",
        "rc",
        {".framework-ref": new + "\n", "data/kinds/engine-commit.txt": OLD_ENGINE + "\n"},
        parent="main",
    )
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 0, lines.err
    assert git("-C", str(remote.path("chock-site")), "show", "rc:data/kinds/engine-commit.txt") == new


def test_pins_dry_run_names_the_engine_pin_copies(remote, gh):
    make(remote, gh, candidate())[0].assemble(reset=False)
    r, lines = make(remote, gh, candidate(), dry_run=True)
    assert r.pins() == 0
    assert any("data/kinds/engine-commit.txt" in line and "data/architecture/*.json" in line for line in lines.out)


def test_pins_refuses_when_a_repo_has_no_rc(remote, gh):
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 1
    assert any("has no rc branch" in e for e in lines.err)


def test_pins_pins_the_catalog_main_head_even_when_a_stray_catalog_rc_exists(remote, gh):
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")
    remote.commit("chock-site", "rc", {"y.txt": "y\n"}, parent="main")
    stray = remote.commit("chock-catalog", "rc", {"z.txt": "z\n"}, parent="main")  # never read
    main = remote.commit("chock-catalog", "main", {"merged.txt": "m\n"})
    r, lines = make(remote, gh, candidate(chock_catalog=[293]))
    assert r.pins() == 0, lines.err
    show = lambda name: git("-C", str(remote.path("chock-site")), "show", f"rc:{name}")  # noqa: E731
    assert show(".catalog-ref") == main != stray
    message = git("-C", str(remote.path("chock-site")), "log", "-1", "--format=%s", "rc")
    assert message == f"rc: pin catalog {main[:12]} (main head) and engine {remote.head('chock', 'rc')[:12]} (rc head)"
    assert not [c for c in gh.api_calls if c.endswith("/chock-catalog/git/ref/heads/rc")]


def test_pins_dry_run_says_the_catalog_pin_is_its_main_head(remote, gh):
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")
    remote.commit("chock-site", "rc", {"y.txt": "y\n"}, parent="main")
    r, lines = make(remote, gh, candidate(), dry_run=True)
    assert r.pins() == 0, lines.err
    main = remote.head("chock-catalog", "main")
    assert f"chock-site: would set .catalog-ref on rc to {main} (the main head)" in lines.out
    assert any(".framework-ref on rc to" in line and "(the rc head)" in line for line in lines.out)


# status -----------------------------------------------------------------------------------


def test_status_reports_head_contained_prs_and_main_moving(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    remote.pr("chock", 2, {"two.txt": "2\n"})
    r, _ = make(remote, gh, candidate(chock=[1]))
    r.assemble(reset=False)
    remote.commit("chock", "main", {"news.txt": "moved\n"})
    r, lines = make(remote, gh, candidate(chock=[1, 2]))
    assert r.status() == 0
    chock = next(line for line in lines.out if line.startswith("chock "))
    assert remote.head("chock", "rc")[:12] in chock
    assert "#1 in" in chock and "#2 NOT in" in chock and "yes, 1 commit(s)" in chock
    site = next(line for line in lines.out if line.startswith("chock-site "))
    assert "moved: no" in site


def test_status_says_when_there_is_no_rc(remote, gh):
    r, lines = make(remote, gh, candidate(chock=[1]))
    assert r.status() == 0
    assert any(line.startswith("chock ") and "no rc branch" in line for line in lines.out)


# transport --------------------------------------------------------------------------------


def test_github_only_over_https_and_token_never_in_argv_or_errors(remote):
    assert rc.https_url("chock") == "https://github.com/open-coder-ai/chock.git"
    g = rc.Git(token="s3cret-token")
    env = g.env()
    assert env["GIT_ALLOW_PROTOCOL"] == "https" and env["GIT_TERMINAL_PROMPT"] == "0"
    n = int(env["GIT_CONFIG_COUNT"]) - 1  # appended after whatever the environment already carried
    assert env[f"GIT_CONFIG_KEY_{n}"] == "http.https://github.com/.extraheader"
    assert "s3cret-token" not in env[f"GIT_CONFIG_VALUE_{n}"]  # base64-encoded, in the environment only
    assert int(rc.Git(token=None).env().get("GIT_CONFIG_COUNT") or 0) == n
    bad = rc.Git(url_for=lambda r: "http://example.invalid/x.git", token="s3cret-token")
    with pytest.raises(rc.Refused) as exc:
        bad.run(["clone", "--quiet", bad.url_for("x"), str(remote.root / "nope")])
    assert "s3cret-token" not in str(exc.value)


def test_the_shipped_candidate_file_is_valid_and_matches_ir_3():
    result = check.Result()
    cand = check.load_candidate(SHIPPED, result)
    assert cand is not None, result.errors
    assert cand.prs["chock-site"] == [78, 80] and cand.prs["chock"] == [314, 315, 318, 319, 320, 321]
    assert set(cand.regenerated) == {"chock", "chock-site"} and ".framework-ref" in cand.regenerated["chock-site"]
    assert cand.regenerated["chock"] == [".chock/bin/**", "tests/fixtures/runtime_goldens/**"]
    assert cand.prs["chock-quickstart"] == [] and cand.prs["chock-example"] == []
    assert cand.prs[CATALOG] == [293, 295, 297]  # merged by the owner first, never assembled
    assert yaml.safe_load((SHIPPED).read_text())["base"] == "main"


# --only -----------------------------------------------------------------------------------


def test_only_limits_assemble_and_status_to_the_named_repos(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    remote.pr("chock-site", 5, {"site.txt": "ok\n"})
    lines = Lines()
    r = rc.Rc(
        gh,
        rc.Git(url_for=remote.url, protocols="file"),
        candidate(chock=[1], chock_site=[5]),
        False,
        out=lines,
        only=["chock-site"],
    )
    assert r.assemble(reset=False) == 0, lines.err
    assert remote.head("chock-site", "refs/heads/rc") and remote.head("chock", "refs/heads/rc") is None
    assert r.status() == 0
    rows = [line for line in lines.out if line.split()[0] in check.TRAIN and "rc " in line]
    assert [line.split()[0] for line in rows] == ["chock-site"]


def test_only_is_repeatable_and_a_repo_outside_the_candidate_is_a_usage_error(capsys):
    cand = str(SHIPPED)
    for argv in (
        ["status", cand, "--only", "context-report"],
        ["assemble", cand, "--only", "chock", "--only", "nope", "--dry-run"],
        ["pins", cand, "--only", "chock-site"],
    ):
        with pytest.raises(SystemExit) as exc:
            rc.main(argv)
        assert exc.value.code == 2
    assert "not in" in capsys.readouterr().err


# hardening --------------------------------------------------------------------------------


def test_a_rename_that_hides_a_deletion_refuses(remote, gh):
    # PR 1 edits app.txt; PR 2 deletes it and adds a similar file under a regenerated glob. Rename
    # detection would report only the new file as unmerged and the deletion would be taken silently.
    remote.pr("chock-site", 1, {"app.txt": "line1\nline2 changed\nline3\n"})
    work = remote.root / "work-delete"
    git("clone", "--quiet", str(remote.path("chock-site")), str(work))
    git("checkout", "--quiet", "-B", "tmp", "origin/main", cwd=work)
    (work / "data/snap").mkdir(parents=True)
    git("mv", "app.txt", "data/snap/moved.txt", cwd=work)
    git("commit", "--quiet", "-m", "move", cwd=work)
    git("push", "--quiet", "origin", "HEAD:refs/pull/2/head", cwd=work)
    r, lines = make(remote, gh, candidate({"chock-site": ["data/snap/**"]}, chock_site=[1, 2]))
    assert r.assemble(reset=False) == 1
    assert remote.head("chock-site", "refs/heads/rc") is None
    assert any(e.startswith("REFUSED chock-site:") and "CONFLICT" in e for e in lines.err), lines.err


def commit_symlink(remote, repo: str, ref: str, links: dict[str, str]) -> None:
    work = remote.root / f"work-link-{repo}-{len(list(remote.root.glob('work-*')))}"
    git("clone", "--quiet", str(remote.path(repo)), str(work))
    git("checkout", "--quiet", "-B", "tmp", "origin/main", cwd=work)
    for name, target in links.items():
        (work / name).unlink(missing_ok=True)
        (work / name).symlink_to(target)
    git("add", ".", cwd=work)
    git("commit", "--quiet", "-m", "links", cwd=work)
    git("push", "--quiet", "origin", f"HEAD:refs/heads/{ref}", cwd=work)


def test_pins_refuses_a_symlink_at_a_path_it_writes(remote, gh, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    remote.pr("chock", 1, {"one.txt": "1\n"})
    make(remote, gh, candidate(chock=[1]))[0].assemble(reset=False)
    commit_symlink(remote, "chock-site", "rc", {".framework-ref": str(outside)})
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 1
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert any("symlink" in e for e in lines.err), lines.err


def test_pin_lines_show_only_a_40_hex_sha(remote, gh):
    remote.commit(
        "chock-site", "rc", {".catalog-ref": "x\n## Injected\n@owner Closes #1\n", ".framework-ref": "a" * 40 + "\n"}
    )
    r, _ = make(remote, gh, candidate())
    assert r.pin_lines("chock-site") == ["- .catalog-ref: unreadable", f"- .framework-ref: {'a' * 40}"]


def test_a_malformed_ref_answer_is_not_a_head(remote, gh):
    r, _ = make(remote, gh, candidate())
    gh_get = gh.get
    gh.get = lambda path: (
        {"object": {"sha": "not-a-sha; --upload-pack=x"}} if "git/ref/heads/" in path else gh_get(path)
    )
    assert r.rc_head("chock") is None and r.base_head("chock") is None


# prs --------------------------------------------------------------------------------------

TITLE = f"Release candidate {VERSION}-rc.1: do not merge"


def test_prs_creates_a_draft_pr_from_rc_to_main_for_each_repo_with_an_rc(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    remote.pr("chock", 2, {"two.txt": "2\n"})
    cand = candidate(chock=[1, 2])
    make(remote, gh, cand)[0].assemble(reset=False)
    r, lines = make(remote, gh, cand)
    assert r.prs() == 0, lines.err
    assert set(gh.rc_prs) == {"chock"}  # the other repos' rc equals main: no PR needed
    for method, path, body in gh.sent:
        assert method == "POST" and path.endswith("/pulls")
        assert body["draft"] is True and body["head"] == "rc" and body["base"] == "main" and body["title"] == TITLE
    body = gh.rc_prs["chock"]["body"]
    lines_ = body.splitlines()
    assert "never to be merged" in lines_[0] and "plan/internal-release.md" in lines_[0]
    assert "- #1" in lines_ and "- #2" in lines_ and lines_[-1] == "Generated by rc.py"
    assert "Pins:" not in body  # the site's pins only on chock-site
    assert any(line.startswith("chock: created draft PR #") for line in lines.out)


def test_prs_body_of_chock_site_carries_its_two_pins(remote, gh):
    remote.commit(
        "chock-site", "rc", {".catalog-ref": "c" * 40 + "\n", ".framework-ref": "e" * 40 + "\n"}, parent="main"
    )
    r, lines = make(remote, gh, candidate(chock_site=[11]))
    assert r.prs() == 0, lines.err
    body = gh.rc_prs["chock-site"]["body"]
    assert f"- .catalog-ref: {'c' * 40}" in body and f"- .framework-ref: {'e' * 40}" in body
    assert body.splitlines()[-1] == "Generated by rc.py"


def test_prs_updates_only_the_body_when_the_pr_exists(remote, gh):
    cand = candidate(chock=[1])
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")
    gh.rc_prs["chock"] = {"number": 7, "title": "renamed by hand", "draft": False, "body": "old"}
    r, lines = make(remote, gh, cand)
    only = rc.Rc(gh, rc.Git(url_for=remote.url, protocols="file"), cand, False, out=lines, only=["chock"])
    assert only.prs() == 0, lines.err
    assert gh.sent == [("PATCH", f"/repos/{check.OWNER}/chock/pulls/7", {"body": gh.rc_prs["chock"]["body"]})]
    assert gh.rc_prs["chock"]["title"] == "renamed by hand" and gh.rc_prs["chock"]["draft"] is False
    assert "- #1" in gh.rc_prs["chock"]["body"]
    gh.sent.clear()
    assert only.prs() == 0  # a repeat round with the same list writes nothing
    assert gh.sent == [] and any("already current" in line for line in lines.out)


def test_prs_skips_a_repo_without_rc_with_one_line(remote, gh):
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")
    r, lines = make(remote, gh, candidate())
    assert r.prs() == 0
    skipped = [line for line in lines.out if "skipped" in line]
    assert len(skipped) == len(check.TRAIN) - 1 and "chock: " not in " ".join(skipped)
    assert all(line.endswith("no rc branch, skipped") for line in skipped if not line.startswith(CATALOG))
    assert set(gh.rc_prs) == {"chock"}


def test_prs_dry_run_touches_nothing(remote, gh):
    cand = candidate(chock=[1])
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")
    remote.commit("chock-site", "rc", {"y.txt": "y\n"}, parent="main")
    gh.rc_prs["chock-site"] = {"number": 9, "title": "t", "draft": True, "body": "old"}
    r, lines = make(remote, gh, cand, dry_run=True)
    assert r.prs() == 0, lines.err
    assert gh.sent == [] and gh.rc_prs["chock-site"]["body"] == "old" and "chock" not in gh.rc_prs
    assert any("chock: would create draft PR rc -> main" in line for line in lines.out)
    assert any("chock-site: would update the body of PR #9" in line for line in lines.out)


def test_prs_never_marks_ready_merges_or_closes(remote, gh):
    cand = candidate(chock=[1])
    for repo in check.TRAIN:
        remote.commit(repo, "rc", {"x.txt": "x\n"}, parent="main")
    gh.rc_prs["chock"] = {"number": 7, "title": TITLE, "draft": True, "body": "old"}
    r, _ = make(remote, gh, cand)
    r.prs()
    r.prs()
    assert gh.sent
    for method, path, body in gh.sent:
        assert method in ("POST", "PATCH") and "merge" not in path and not path.endswith("/ready_for_review")
        if method == "PATCH":
            assert set(body) == {"body"}  # no state, draft, title or base
        else:
            assert body["draft"] is True
    assert gh.rc_prs["chock"]["draft"] is True


def test_prs_reports_a_github_failure_per_repo_and_exits_1(remote, gh):
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")

    class Forbidden(LocalGH):
        def send(self, method, path, body):
            raise check.GitHubError(f"{method} {path}: HTTP 403")

    forbidden = Forbidden(remote)
    r, lines = make(remote, forbidden, candidate())
    assert r.prs() == 1
    assert any(e.startswith("REFUSED chock:") and "HTTP 403" in e for e in lines.err)


def test_assemble_ends_with_the_next_step_and_status_shows_the_rc_pr(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    cand = candidate(chock=[1])
    r, lines = make(remote, gh, cand)
    r.assemble(reset=False)
    assert lines.out[-1] == "next: rc.py prs <file> so CI runs on rc"
    gh.rc_prs["chock"] = {"number": 42, "title": TITLE, "draft": True, "body": ""}
    r.out = lines = Lines()
    assert r.status() == 0
    rows = {line.split()[0]: line for line in lines.out}
    assert "rc PR: #42" in rows["chock"] and "rc PR: none" in rows["chock-site"]


def test_prs_only_and_usage(capsys):
    cand = str(SHIPPED)
    with pytest.raises(SystemExit) as exc:
        rc.main(["prs", cand, "--only", "nope"])
    assert exc.value.code == 2


def test_prs_skips_a_repo_whose_rc_equals_main_before_asking_for_its_pr(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    make(remote, gh, candidate(chock=[1]))[0].assemble(reset=False)
    assert remote.head("chock-site", "rc") == remote.head("chock-site", "main")  # no candidate PRs: rc is main
    r, lines = make(remote, gh, candidate(chock=[1]))
    assert r.prs() == 0, lines.err
    assert "chock-site: rc equals main, no PR needed" in lines.out
    assert "chock-site" not in gh.rc_prs and "chock" in gh.rc_prs
    assert not [p for p in gh.api_calls if "/chock-site/pulls" in p]  # the 422 call was never made


def test_pins_prints_the_regeneration_reminder_after_its_commit_only(remote, gh):
    make(remote, gh, candidate())[0].assemble(reset=False)
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 0, lines.err
    text = lines.out[-1]
    assert lines.out[-2].startswith("chock-site: rc ")
    for needle in (
        "python -m sitegen.docsource --chock <chock checkout at .framework-ref>",
        "python -m sitegen.writedoc --record",
        "python -m sitegen.catalogdoc --catalog <catalog checkout at .catalog-ref>",
        "python -m sitegen.cliexamples --record",
        "python -m sitegen.agentseamdoc snapshot --agentseam <agentseam checkout>",
        "python -m sitegen.agentseamdoc run --record",
        "python -m sitegen.architecture --check --fetch --clones <folder> --repo chock-site=.",
        "rc only",
    ):
        assert needle in text
    r, lines = make(remote, gh, candidate())
    r.pins()  # nothing to do the second time: no reminder
    assert not any("reminder" in line for line in lines.out)


def test_status_shows_the_ci_summary_of_the_rc_pr_with_one_call_per_repo(remote, gh):
    remote.commit("chock", "rc", {"x.txt": "x\n"}, parent="main")
    remote.commit("chock-site", "rc", {"y.txt": "y\n"}, parent="main")
    gh.rc_prs["chock"] = {"number": 5, "title": TITLE, "draft": True, "body": ""}
    gh.check_runs["chock"] = ["success", "success", "failure", None]
    r, lines = make(remote, gh, candidate())
    assert r.status() == 0, lines.err
    rows = {line.split()[0]: line for line in lines.out}
    assert rows["chock"].endswith("ci: 2 success, 1 failure, 1 other")
    assert "ci:" not in rows["chock-site"]  # no rc PR: no summary
    assert len([p for p in gh.api_calls if "/chock/commits/" in p]) == 1


# the catalog is merge-first ---------------------------------------------------------------


def test_assemble_skips_the_catalog_with_one_line_and_creates_no_rc_for_it(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    r, lines = make(remote, gh, candidate(chock=[1], chock_catalog=[293]))  # #293 does not even exist as a ref
    assert r.assemble(reset=False) == 0, lines.err
    skipped = [line for line in lines.out if line.startswith(f"{CATALOG}:")]
    assert len(skipped) == 1 and "merge-first, skipped" in skipped[0] and "no rc branch is created" in skipped[0]
    assert remote.head(CATALOG, "refs/heads/rc") is None and remote.head("chock", "refs/heads/rc")
    assert not [c for c in gh.api_calls if f"/{CATALOG}/" in c]  # nothing is read for it either


def test_assemble_dry_run_skips_the_catalog_too(remote, gh):
    r, lines = make(remote, gh, candidate(chock_catalog=[293]), dry_run=True)
    assert r.assemble(reset=False) == 0, lines.err
    assert any(line.startswith(f"{CATALOG}: merge-first, skipped") for line in lines.out)
    assert not any(line.startswith(f"{CATALOG}: would clone") for line in lines.out)


def test_assemble_reset_leaves_the_catalog_alone(remote, gh):
    main = remote.head(CATALOG, "refs/heads/main")
    r, _ = make(remote, gh, candidate(chock_catalog=[293]))
    assert r.assemble(reset=True) == 0
    assert remote.head(CATALOG, "refs/heads/rc") is None and remote.head(CATALOG, "refs/heads/main") == main


def test_prs_skips_the_catalog_even_when_a_stray_catalog_rc_exists(remote, gh):
    remote.commit(CATALOG, "rc", {"x.txt": "x\n"}, parent="main")  # an old branch: still no draft PR for it
    remote.commit("chock", "rc", {"y.txt": "y\n"}, parent="main")
    r, lines = make(remote, gh, candidate(chock_catalog=[293]))
    assert r.prs() == 0, lines.err
    assert any(line.startswith(f"{CATALOG}: merge-first, skipped") and "no draft PR" in line for line in lines.out)
    assert CATALOG not in gh.rc_prs and "chock" in gh.rc_prs
    assert not [m for m, path, _ in gh.sent if f"/{CATALOG}/" in path]
    assert not [c for c in gh.api_calls if f"/{CATALOG}/" in c]


@pytest.mark.parametrize("verb", ["assemble", "prs"])
def test_only_the_catalog_is_a_usage_error_for_assemble_and_prs(verb, capsys):
    for only in (["chock-catalog"], ["chock", "chock-catalog"]):
        argv = [verb, str(SHIPPED), "--dry-run"]
        for name in only:
            argv += ["--only", name]
        with pytest.raises(SystemExit) as exc:
            rc.main(argv)
        assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "merge-first" in err and f"{verb} creates no rc branch and no draft PR for it" in err


def test_only_the_catalog_is_fine_for_status(remote, gh):
    lines = Lines()
    r = rc.Rc(
        gh,
        rc.Git(url_for=remote.url, protocols="file"),
        candidate(chock_catalog=[293]),
        False,
        out=lines,
        only=[CATALOG],
    )
    assert r.status() == 0, lines.err
    assert [line.split()[0] for line in lines.out] == [CATALOG]


def test_status_shows_the_catalog_main_head_and_merged_or_not_per_pr(remote, gh):
    for n in (293, 294, 295):
        remote.pr(CATALOG, n, {f"{n}.txt": "x\n"})
    gh.merged.add((CATALOG, 293))
    gh.states[(CATALOG, 295)] = "closed"
    gh.rc_prs[CATALOG] = {"number": 9, "title": TITLE, "draft": True, "body": ""}  # a stray: never shown
    r, lines = make(remote, gh, candidate(chock_catalog=[293, 294, 295, 296]))
    assert r.status() == 0, lines.err
    row = next(line for line in lines.out if line.startswith(f"{CATALOG} "))
    assert remote.head(CATALOG, "refs/heads/main")[:12] in row and "merge-first" in row
    assert "#293 merged" in row and "#294 NOT merged (open)" in row and "#295 NOT merged (closed)" in row
    assert "#296 NOT merged (not found)" in row
    assert "NOT in" not in row and "rc PR" not in row and "ci:" not in row
    assert not [c for c in gh.api_calls if f"/{CATALOG}/" in c and ("compare" in c or c.endswith("heads/rc"))]


def test_status_with_no_catalog_prs_says_none(remote, gh):
    r, lines = make(remote, gh, candidate())
    assert r.status() == 0
    assert next(line for line in lines.out if line.startswith(f"{CATALOG} ")).endswith("PRs: none")
