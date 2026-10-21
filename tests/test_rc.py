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
        self.rc_prs: dict[str, dict] = {}  # repo -> the open rc -> main PR
        self.sent: list[tuple[str, str, dict]] = []  # every write: (method, path, body)

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
            return {
                "state": self.states.get((repo, n), "open"),
                "base": {"ref": self.bases.get((repo, n), "main")},
                "head": {"sha": sha},
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


def candidate(**prs: list[int]) -> check.Candidate:
    table = {repo: [] for repo in check.TRAIN}
    table.update({repo.replace("_", "-"): numbers for repo, numbers in prs.items()})
    return check.Candidate(version=VERSION, rc=1, base="main", prs=table)


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
    assert show(".catalog-ref") == remote.head("chock-catalog", "rc")
    assert show(".framework-ref") == remote.head("chock", "rc")
    body = git("-C", str(remote.path("chock-site")), "log", "-1", "--format=%an <%ae>%n%B", "rc")
    assert (
        f"Jothi Mani Rajendran <{OWNER_EMAIL}>" in body
        and f"Signed-off-by: Jothi Mani Rajendran <{OWNER_EMAIL}>" in body
    )
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 0
    assert len(remote.log("chock-site")) == before + 1
    assert any("nothing to do" in line for line in lines.out)


def test_pins_refuses_when_a_repo_has_no_rc(remote, gh):
    r, lines = make(remote, gh, candidate())
    assert r.pins() == 1
    assert any("has no rc branch" in e for e in lines.err)


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
    assert cand.prs["chock-site"] == [11, 78] and cand.prs["chock"] == [314, 315]
    assert cand.prs["chock-quickstart"] == [] and cand.prs["chock-example"] == []
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


# prs --------------------------------------------------------------------------------------

TITLE = f"Release candidate {VERSION}-rc.1: do not merge"


def test_prs_creates_a_draft_pr_from_rc_to_main_for_each_repo_with_an_rc(remote, gh):
    remote.pr("chock", 1, {"one.txt": "1\n"})
    remote.pr("chock", 2, {"two.txt": "2\n"})
    cand = candidate(chock=[1, 2])
    make(remote, gh, cand)[0].assemble(reset=False)
    r, lines = make(remote, gh, cand)
    assert r.prs() == 0, lines.err
    assert {repo for repo in gh.rc_prs} == set(check.TRAIN)
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
    assert all(line.endswith("no rc branch, skipped") for line in skipped)
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
