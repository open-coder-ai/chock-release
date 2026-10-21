"""check.py --candidate: the release check against each repo's `rc` head, and the refusals around it."""

from __future__ import annotations

import pytest
import yaml

import check
import release
from conftest import ROOT, VERSION, World, sha

# The one candidate file the repo ships.
SHIPPED = next(iter(sorted((ROOT / "candidates").glob("*-rc.*.yaml"))))
RC = "rc"


class CandidateWorld(World):
    """A train whose rc heads are consistent, with its main heads separate and its CI on `rc`."""

    def __init__(self) -> None:
        super().__init__()
        self.main_heads = {repo: sha(f"main:{repo}") for repo in (*check.TRAIN, *check.INPUTS)}
        self.commits = {repo: sha(f"rc:{repo}") for repo in check.TRAIN} | {check.AGENTSEAM: sha("agentseam")}
        self.main_heads[check.AGENTSEAM] = self.commits[check.AGENTSEAM]
        self.no_rc: set[str] = set()
        self.compare = {repo: "ahead" for repo in check.TRAIN}

    @classmethod
    def consistent(cls) -> CandidateWorld:
        return super().consistent()  # type: ignore[return-value]

    def add_run(self, repo, workflow, created_at="2026-01-01T00:00:00Z", **fields):
        if repo in check.TRAIN:
            fields.setdefault("head_branch", RC)
        return super().add_run(repo, workflow, created_at, **fields)

    def red_run(self, repo: str, branch: str, commit: str, failing: tuple[str, ...]) -> None:
        """A completed, failed run of the required workflow with these jobs failed and the rest green."""
        workflow, required = check.REQUIRED_WORKFLOWS[repo]
        run = super().add_run(repo, workflow, "2026-01-02T00:00:00Z", head_sha=commit, head_branch=branch)
        run["conclusion"] = "failure"
        self.jobs[run["id"]] = [
            {"name": job, "status": "completed", "conclusion": "failure" if job in failing else "success"}
            for job in required
        ]

    def get(self, path: str):
        prefix = f"/repos/{check.OWNER}/"
        repo, _, rest = path[len(prefix) :].partition("/")
        if rest.startswith("git/ref/heads/"):
            self.calls.append(path)
            branch = rest[len("git/ref/heads/") :]
            if branch == RC and repo in check.TRAIN:
                return None if repo in self.no_rc else {"object": {"sha": self.commits[repo]}}
            return {"object": {"sha": self.main_heads[repo]}}
        return super().get(path)


@pytest.fixture
def cworld() -> CandidateWorld:
    return CandidateWorld.consistent()


@pytest.fixture
def candidate_file(tmp_path):
    def _write(data: dict | None = None, name: str = f"{VERSION}-rc.1.yaml"):
        data = (
            {"version": VERSION, "base": "main", "repos": {repo: [] for repo in check.TRAIN}} if data is None else data
        )
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write


def run_candidate(world, path):
    _, manifest, result = check.check_candidate(path, world)
    return manifest, result


def errors(result, needle):
    return [e for e in result.errors if needle in e]


def warnings(result, needle):
    return [w for w in result.warnings if needle in w]


# The same code paths, against the rc heads -------------------------------------------------


def test_consistent_candidate_passes_with_no_tag_reads(cworld, candidate_file):
    manifest, result = run_candidate(cworld, candidate_file())
    assert result.ok, result.errors
    assert manifest.repos == {repo: cworld.commits[repo] for repo in check.TRAIN}
    assert result.tags == []
    assert not [c for c in cworld.calls if "/git/ref/tags/" in c or "/git/tags/" in c]  # checks 8 and 9 are skipped


def test_check_one_reads_rc_not_the_default_branch(cworld, candidate_file):
    cworld.no_rc.add("chock-site")
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "check 1: chock-site: no rc branch")
    # CI is read on rc: a run on main at the rc commit does not count
    cworld.workflows["chock"]["ci.yml"].clear()
    cworld.add_run("chock", "ci.yml", head_branch="main")
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "check 2: chock: ci.yml never ran on") and errors(result, "on rc")


def test_ci_must_be_green_on_rc(cworld, candidate_file):
    cworld.workflows["chock-catalog"]["ci.yml"].clear()
    cworld.green("chock-catalog", conclusion="failure")
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "check 2: chock-catalog:") and not result.ok


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (
            lambda w: w.put("chock-catalog", ".framework-ref", sha("other") + "\n"),
            "check 4: chock-catalog: .framework-ref",
        ),
        (
            lambda w: w.put("chock-codex-plugins", "generated_from.yaml", "generated_from: {catalog: x, engine: y}"),
            "check 5: chock-codex-plugins",
        ),
        (lambda w: w.put("chock-site", ".catalog-ref", sha("other") + "\n"), "check 6: chock-site: .catalog-ref"),
        (lambda w: w.put("chock-example", "requirements.txt", "chock==1.2.3\n"), "check 7: chock-example"),
    ],
)
def test_checks_4a_5_6a_7_refuse(cworld, candidate_file, mutate, needle):
    mutate(cworld)
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, needle)


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (
            lambda w: w.put(
                "chock",
                "pyproject.toml",
                '[project]\nname="chock"\nversion="0.0.0"\ndependencies=["agentseam==7.6.5"]\n',
            ),
            "check 3: chock: __version__ is '0.0.0'",
        ),
        (lambda w: w.put("chock-catalog", "VERSION", "0.0.0\n"), "check 4: chock-catalog: VERSION is '0.0.0'"),
        (
            lambda w: w.put("chock-site", "data/version.json", '{"version": "0.0.0"}'),
            "check 6: chock-site: data/version.json version is '0.0.0'",
        ),
        (lambda w: w.files.pop(("chock-catalog", "VERSION")), "chock-catalog: VERSION not found"),
        (lambda w: w.files.pop(("chock-site", "data/version.json")), "chock-site: data/version.json not found"),
    ],
)
def test_version_file_checks_3_4b_6b_only_warn(cworld, candidate_file, mutate, needle):
    mutate(cworld)
    _, result = run_candidate(cworld, candidate_file())
    assert result.ok, result.errors
    assert warnings(result, needle), result.warnings


def test_the_release_check_still_refuses_the_same_version_files(cworld, tmp_path):
    """The same facts refuse in a release manifest: candidate mode softens them, nothing else does."""
    w = World.consistent()
    w.put("chock-catalog", "VERSION", "0.0.0\n")
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(yaml.safe_dump(w.manifest()), encoding="utf-8")
    _, result = check.check(path, w, VERSION)
    assert errors(result, "check 4: chock-catalog: VERSION is '0.0.0'") and result.warnings == []


# Known red ---------------------------------------------------------------------------------


def test_job_red_on_rc_and_on_the_default_branch_is_known_red(cworld, candidate_file):
    repo = "chock-devin-plugins"
    cworld.workflows[repo]["generated-only.yml"].clear()
    cworld.red_run(repo, "rc", cworld.commits[repo], ("verify",))
    cworld.red_run(repo, "main", cworld.main_heads[repo], ("verify",))
    _, result = run_candidate(cworld, candidate_file())
    assert result.ok, result.errors
    assert warnings(result, "known red: check 2: chock-devin-plugins: required job 'verify' concluded failure")


def test_job_red_only_on_rc_is_a_refusal(cworld, candidate_file):
    repo = "chock-devin-plugins"
    cworld.workflows[repo]["generated-only.yml"].clear()
    cworld.red_run(repo, "rc", cworld.commits[repo], ("verify",))
    main_run = cworld.green(repo)  # main's head is green
    main_run.update(head_sha=cworld.main_heads[repo], head_branch="main")
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "required job 'verify' concluded failure")
    assert warnings(result, "known red") == []


def test_a_different_job_red_on_rc_is_not_excused_by_main(cworld, candidate_file):
    repo = "chock"
    cworld.workflows[repo]["ci.yml"].clear()
    cworld.red_run(repo, "rc", cworld.commits[repo], ("validate",))
    cworld.red_run(repo, "main", cworld.main_heads[repo], ("acceptance (tier 1)",))
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "required job 'validate' concluded failure")
    assert errors(result, "ci.yml run") and warnings(result, "known red") == []


def test_a_run_that_never_ran_is_never_known_red(cworld, candidate_file):
    repo = "chock-site"
    cworld.workflows[repo]["ci.yml"].clear()
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "ci.yml never ran") and not warnings(result, "known red")


# Warning when main moves, and the plan -----------------------------------------------------


def test_main_moving_under_rc_is_a_warning(cworld, candidate_file):
    cworld.compare["chock"] = "diverged"
    _, result = run_candidate(cworld, candidate_file())
    assert result.ok and warnings(result, "check 1: chock: main has moved under rc (compare: diverged)")


def test_plan_says_nothing_is_tagged_published_or_moved(cworld, candidate_file, monkeypatch, capsys):
    path = candidate_file()
    monkeypatch.setattr(check, "GitHub", lambda token=None: cworld)
    assert check.main(["--candidate", str(path), "--plan"]) == 0
    out = capsys.readouterr().out
    assert "candidate: nothing is tagged, published or moved" in out
    assert out.startswith("OK:") and "rc ->" in out
    assert not [c for c in cworld.calls if "/git/ref/tags/" in c]


def test_candidate_mode_never_tags_and_cannot_be_asked_to(cworld, candidate_file):
    manifest, result = run_candidate(cworld, candidate_file())
    assert result.tags == []  # release.step_tag has nothing to create
    assert not hasattr(check.Checker, "create_tag")
    assert "Release plan" not in check.render_candidate_plan(
        check.load_candidate(candidate_file(), check.Result()), manifest
    )


def test_main_refuses_both_or_neither_argument(candidate_file, capsys):
    path = candidate_file()
    for argv in ([], [str(path), "--candidate", str(path)], ["--candidate", str(path), "--version", VERSION]):
        with pytest.raises(SystemExit) as exc:
            check.main(argv)
        assert exc.value.code == 2


def test_main_prints_warnings_and_exits_one_on_a_refusal(cworld, candidate_file, monkeypatch, capsys):
    cworld.put("chock-catalog", "VERSION", "0.0.0\n")
    cworld.put("chock-site", ".catalog-ref", sha("other") + "\n")
    monkeypatch.setattr(check, "GitHub", lambda token=None: cworld)
    assert check.main(["--candidate", str(candidate_file())]) == 1
    captured = capsys.readouterr()
    assert "WARNING: check 4: chock-catalog: VERSION" in captured.out
    assert "REFUSED" in captured.err and "check 6: chock-site: .catalog-ref" in captured.err


# The candidate file itself -----------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d.update(base="dev"), "base must be 'main'"),
        (lambda d: d.update(version="1.0"), "version must be a MAJOR.MINOR.PATCH"),
        (lambda d: d.update(version="1.2.3"), "does not match version"),
        (lambda d: d.update(extra=1), "unknown top-level key 'extra'"),
        (lambda d: d["repos"].pop("chock-site"), "repos.chock-site is missing"),
        (lambda d: d["repos"].update(**{"context-report": []}), "repos.context-report is not in the train"),
        (lambda d: d["repos"].update(chock=[1, 1]), "lists a PR twice"),
        (lambda d: d["repos"].update(chock=["12"]), "must be a list of PR numbers"),
        (lambda d: d["repos"].update(chock=[True]), "must be a list of PR numbers"),
        (lambda d: d["repos"].update(chock=[0]), "must be a list of PR numbers"),
        (lambda d: d["repos"].update(chock={"commit": sha("x")}), "must be a list of PR numbers"),
    ],
)
def test_candidate_shape_refusals(candidate_file, mutate, needle):
    data = {"version": VERSION, "base": "main", "repos": {repo: [] for repo in check.TRAIN}}
    mutate(data)
    result = check.Result()
    assert check.load_candidate(candidate_file(data), result) is None
    assert errors(result, needle), result.errors


def test_candidate_needs_its_name_and_a_candidate_shape(candidate_file, cworld):
    result = check.Result()
    assert check.load_candidate(candidate_file(name="candidate.yaml"), result) is None
    assert errors(result, "must be <MAJOR.MINOR.PATCH>-rc.<n>.yaml")
    manifest_shaped = cworld.manifest()
    result = check.Result()
    assert check.load_candidate(candidate_file(manifest_shaped, name=f"{VERSION}.yaml"), result) is None
    _, bad = run_candidate(cworld, candidate_file(manifest_shaped, name=f"{VERSION}.yaml"))
    assert not bad.ok


def test_the_shipped_candidate_passes_the_shape_check():
    result = check.Result()
    assert check.load_candidate(SHIPPED, result) is not None, result.errors


# release.py and the release check refuse a candidate ---------------------------------------


@pytest.mark.parametrize("step", ["build", "tag", "pypi", "site", "verify", "summary"])
def test_release_py_refuses_a_candidate_file_outright(step, candidate_file, monkeypatch, capsys):
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: pytest.fail("a candidate must not reach GitHub"))
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: pytest.fail("a candidate must not build"))
    assert release.main([step, str(candidate_file()), "--version", VERSION]) == 1
    assert "is a release candidate, not a manifest" in capsys.readouterr().err


def test_release_py_refuses_a_candidate_shaped_file_with_any_name(candidate_file, monkeypatch, capsys):
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: pytest.fail("a candidate must not reach GitHub"))
    assert release.main(["tag", str(candidate_file(name=f"{VERSION}.yaml")), "--version", VERSION]) == 1
    assert "release candidate" in capsys.readouterr().err


def test_check_py_given_a_candidate_as_a_manifest_refuses(candidate_file, cworld):
    _, result = check.check(candidate_file(), cworld, VERSION)
    assert errors(result, "is a release candidate, not a manifest")
    assert cworld.calls == []
