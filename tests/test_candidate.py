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


CATALOG = check.CATALOG


class CandidateWorld(World):
    """A train whose rc heads are consistent, with its main heads separate and its CI on `rc`.

    The catalog is merge-first: it has no `rc`, the candidate's commit for it is its main head, its CI is read on
    main, and its listed PRs (`pulls`, by number) are merged unless a test says otherwise.
    """

    def __init__(self) -> None:
        super().__init__()
        self.main_heads = {repo: sha(f"main:{repo}") for repo in (*check.TRAIN, *check.INPUTS)}
        self.commits = {repo: sha(f"rc:{repo}") for repo in check.TRAIN} | {check.AGENTSEAM: sha("agentseam")}
        self.main_heads[check.AGENTSEAM] = self.commits[check.AGENTSEAM]
        self.commits[CATALOG] = self.main_heads[CATALOG]
        self.no_rc: set[str] = set()
        self.compare = {repo: "ahead" for repo in check.TRAIN} | {CATALOG: "identical"}
        self.pulls: dict[int, dict] = {}  # catalog PR number -> API object; a number not here is merged into main

    @classmethod
    def consistent(cls) -> CandidateWorld:
        return super().consistent()  # type: ignore[return-value]

    def add_run(self, repo, workflow, created_at="2026-01-01T00:00:00Z", **fields):
        if repo in check.TRAIN and repo != CATALOG:
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
            if branch == RC and repo == CATALOG:
                raise AssertionError("the catalog has no rc: nothing may read it")
            if branch == RC and repo in check.TRAIN:
                return None if repo in self.no_rc else {"object": {"sha": self.commits[repo]}}
            return {"object": {"sha": self.main_heads[repo]}}
        if rest.startswith("pulls/") and repo == CATALOG:
            self.calls.append(path)
            n = int(rest.split("/")[1])
            default = {"state": "closed", "merged": True, "base": {"ref": "main"}}
            return self.pulls.get(n, default)
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
    assert errors(result, "on rc (open the rc draft PR: rc.py prs)")  # the hint, in candidate mode only


def test_ci_must_be_green_on_rc(cworld, candidate_file):
    cworld.workflows["chock-catalog"]["ci.yml"].clear()
    cworld.green("chock-catalog", conclusion="failure")
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "check 2: chock-catalog:") and not result.ok


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda w: w.put("chock-site", ".catalog-ref", sha("other") + "\n"), "check 6: chock-site: .catalog-ref"),
    ],
)
def test_check_6a_refuses(cworld, candidate_file, mutate, needle):
    mutate(cworld)
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, needle)


PIN_BREAKS = [
    (lambda w: w.put("chock-example", "requirements.txt", "chock==1.2.3\n"), "check 7: chock-example"),
    (lambda w: w.put("chock-catalog", ".framework-ref", sha("other") + "\n"), "check 4: chock-catalog: .framework-ref"),
    (
        lambda w: w.put("chock-codex-plugins", "generated_from.yaml", "generated_from: {catalog: x, engine: y}"),
        "check 5: chock-codex-plugins",
    ),
]


@pytest.mark.parametrize("mutate, needle", PIN_BREAKS)
def test_checks_4a_5_and_7_only_warn_in_candidate_mode(cworld, candidate_file, mutate, needle):
    mutate(cworld)
    _, result = run_candidate(cworld, candidate_file())
    assert result.ok, result.errors
    assert warnings(result, needle), result.warnings


@pytest.mark.parametrize("mutate, needle", PIN_BREAKS)
def test_checks_4a_5_and_7_still_refuse_in_manifest_mode(tmp_path, mutate, needle):
    w = World.consistent()
    mutate(w)
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(yaml.safe_dump(w.manifest()), encoding="utf-8")
    _, result = check.check(path, w, VERSION)
    assert errors(result, needle) and result.warnings == []


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


# The catalog is merge-first ------------------------------------------------------------------


def catalog_candidate(candidate_file, *numbers: int):
    data = {"version": VERSION, "base": "main", "repos": {repo: [] for repo in check.TRAIN}}
    data["repos"][CATALOG] = list(numbers)
    return candidate_file(data)


def test_the_catalog_is_read_on_main_never_on_rc(cworld, candidate_file):
    manifest, result = run_candidate(cworld, catalog_candidate(candidate_file, 293))
    assert result.ok, result.errors
    assert manifest.repos[CATALOG] == cworld.main_heads[CATALOG]
    assert not warnings(result, f"{CATALOG}: rc equals")  # not the "rc is main" fallback: it never had an rc
    assert not [c for c in cworld.calls if c.endswith(f"/{CATALOG}/git/ref/heads/rc")]
    assert f"/repos/{check.OWNER}/{CATALOG}/pulls/293" in cworld.calls


def test_a_catalog_with_no_rc_branch_is_not_a_refusal(cworld, candidate_file):
    cworld.no_rc.add(CATALOG)
    _, result = run_candidate(cworld, catalog_candidate(candidate_file, 293))
    assert result.ok, result.errors


@pytest.mark.parametrize("state", ["open", "closed"])
def test_a_listed_catalog_pr_that_is_not_merged_is_a_refusal_naming_the_pr(cworld, candidate_file, state):
    cworld.pulls[293] = {"state": state, "merged": False, "base": {"ref": "main"}}
    _, result = run_candidate(cworld, catalog_candidate(candidate_file, 293, 294))
    assert errors(result, f"check 1: {CATALOG}: PR #293 is not merged ({state}); {CATALOG} is merge-first")
    assert not errors(result, "#294")  # the other listed PR is merged


def test_a_catalog_pr_merged_into_another_branch_or_missing_is_a_refusal(cworld, candidate_file):
    cworld.pulls[293] = {"state": "closed", "merged": True, "base": {"ref": "dev"}}
    cworld.pulls[294] = None
    _, result = run_candidate(cworld, catalog_candidate(candidate_file, 293, 294))
    assert errors(result, "PR #293 was merged into 'dev', not main")
    assert errors(result, "PR #294 not found")


def test_catalog_prs_are_only_read_for_the_catalog(cworld, candidate_file):
    data = {"version": VERSION, "base": "main", "repos": {repo: [] for repo in check.TRAIN}}
    data["repos"]["chock"] = [314]
    data["repos"]["chock-site"] = [11]
    run_candidate(cworld, candidate_file(data))
    assert not [c for c in cworld.calls if "/pulls/" in c]  # other repos stay on the rc train, unread


def test_the_site_catalog_pin_must_equal_the_catalog_main_head(cworld, candidate_file):
    cworld.put("chock-site", ".catalog-ref", sha("rc:chock-catalog") + "\n")  # an rc head: not installable
    _, result = run_candidate(cworld, catalog_candidate(candidate_file, 293))
    assert errors(result, "check 6: chock-site: .catalog-ref")
    cworld.put("chock-site", ".catalog-ref", cworld.main_heads[CATALOG] + "\n")
    _, result = run_candidate(cworld, catalog_candidate(candidate_file, 293))
    assert result.ok, result.errors


def test_catalog_ci_must_be_green_on_main(cworld, candidate_file):
    cworld.workflows[CATALOG]["ci.yml"].clear()
    cworld.green(CATALOG, conclusion="failure")
    _, result = run_candidate(cworld, catalog_candidate(candidate_file, 293))
    assert errors(result, f"check 2: {CATALOG}:") and not warnings(result, "known red")
    cworld.workflows[CATALOG]["ci.yml"].clear()
    _, result = run_candidate(cworld, catalog_candidate(candidate_file, 293))
    assert errors(result, "never ran") and not errors(result, "rc.py prs")  # no rc PR to open for it


def test_a_catalog_head_not_on_main_is_a_refusal(cworld, candidate_file):
    cworld.compare[CATALOG] = "ahead"
    _, result = run_candidate(cworld, catalog_candidate(candidate_file, 293))
    assert errors(result, f"check 1: {CATALOG}: commit {cworld.commits[CATALOG][:12]} is not on main")


def test_the_candidate_plan_names_the_catalog_as_merge_first(cworld, candidate_file):
    path = catalog_candidate(candidate_file, 293)
    candidate, manifest, _ = check.check_candidate(path, cworld)
    line = next(ln for ln in check.render_candidate_plan(candidate, manifest).splitlines() if CATALOG in ln)
    assert f"main -> {cworld.main_heads[CATALOG]}" in line and "merge-first" in line and "rc ->" not in line


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


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("chock-site", "'regenerated' must be a mapping"),
        ({"context-report": ["a"]}, "regenerated.context-report is not in the train"),
        ({"chock-site": ".catalog-ref"}, "regenerated.chock-site must be a list of non-empty path globs"),
        ({"chock-site": [""]}, "regenerated.chock-site must be a list of non-empty path globs"),
        ({"chock-site": [3]}, "regenerated.chock-site must be a list of non-empty path globs"),
    ],
)
def test_regenerated_shape_refusals(candidate_file, value, needle):
    data = {"version": VERSION, "base": "main", "repos": {repo: [] for repo in check.TRAIN}, "regenerated": value}
    result = check.Result()
    assert check.load_candidate(candidate_file(data), result) is None
    assert errors(result, needle), result.errors


def test_regenerated_is_optional_and_loaded(candidate_file):
    data = {"version": VERSION, "base": "main", "repos": {repo: [] for repo in check.TRAIN}}
    result = check.Result()
    assert check.load_candidate(candidate_file(data), result).regenerated == {}
    data["regenerated"] = {"chock-site": [".catalog-ref", "data/**"]}
    assert check.load_candidate(candidate_file(data), result).regenerated == data["regenerated"]
    assert result.ok


AGENTSEAM_DOWN = {f"/repos/{check.OWNER}/{check.AGENTSEAM}": "GET agentseam: HTTP 403"}


def test_agentseam_api_error_is_a_warning_in_candidate_mode(cworld, candidate_file):
    cworld.errors.update(AGENTSEAM_DOWN)
    manifest, result = run_candidate(cworld, candidate_file())
    assert result.ok, result.errors
    assert warnings(result, f"{check.AGENTSEAM}: GitHub API error: GET agentseam: HTTP 403")
    assert check.AGENTSEAM not in manifest.inputs


def test_agentseam_api_error_still_refuses_in_manifest_mode(tmp_path):
    world = World.consistent()
    world.errors.update(AGENTSEAM_DOWN)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(world.manifest(), sort_keys=False), encoding="utf-8")
    _, result = check.check(path, world)
    assert errors(result, f"{check.AGENTSEAM}: GitHub API error")


def test_a_train_repo_api_error_still_refuses_in_candidate_mode(cworld, candidate_file):
    cworld.errors[f"/repos/{check.OWNER}/chock-catalog/contents/"] = "GET x: HTTP 500"
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "chock-catalog: GitHub API error")


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


def test_rc_equal_to_main_reads_the_runs_on_the_default_branch(cworld, candidate_file):
    cworld.main_heads["chock-quickstart"] = cworld.commits["chock-quickstart"]  # no candidate PRs: rc is main
    cworld.workflows["chock-quickstart"].clear()
    cworld.green("chock-quickstart")  # CI ran on main, never on rc
    cworld.workflows["chock-quickstart"]["ci.yml"][-1]["head_branch"] = "main"
    _, result = run_candidate(cworld, candidate_file())
    assert result.ok, result.errors
    assert warnings(result, "chock-quickstart: rc equals main")


def test_rc_equal_to_main_with_red_main_ci_is_refused_not_excused(cworld, candidate_file):
    cworld.main_heads["chock-quickstart"] = cworld.commits["chock-quickstart"]
    cworld.workflows["chock-quickstart"].clear()
    cworld.green("chock-quickstart", conclusion="failure")
    cworld.workflows["chock-quickstart"]["ci.yml"][-1]["head_branch"] = "main"
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "check 2: chock-quickstart:") and not warnings(result, "known red")
    cworld.workflows["chock-quickstart"][
        "ci.yml"
    ].clear()  # and no run on main at all: the hint to open an rc PR is not offered
    _, result = run_candidate(cworld, candidate_file())
    assert errors(result, "never ran") and not errors(result, "rc.py prs")
