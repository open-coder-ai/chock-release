"""One test per refusal in check.py, plus the passing and reporting paths."""

from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path

import pytest
import yaml

import check
from conftest import ROOT, VERSION, World, sha


def errors_matching(result, needle: str) -> list[str]:
    return [e for e in result.errors if needle in e]


def assert_refused(result, needle: str) -> None:
    assert not result.ok
    assert errors_matching(result, needle), f"no error containing {needle!r} in {result.errors}"


# Passing paths ---------------------------------------------------------------------


def test_consistent_train_passes(world, run):
    manifest, result = run(world)
    assert result.ok, result.errors
    assert {state for _, _, state in result.tags} == {"create"}
    assert len(result.tags) == len(check.TRAIN)


def test_plan_lists_every_tag_and_the_site_move(world, run):
    manifest, result = run(world)
    plan = check.render_plan(manifest, result)
    for repo in check.TRAIN:
        assert f"{check.OWNER}/{repo}  v{VERSION} -> {world.commits[repo]}" in plan
    assert f"branch 'release' -> {world.commits[check.SITE]}" in plan
    assert "agentseam" in plan


def test_main_dry_run_prints_plan_and_exits_zero(world, tmp_path, monkeypatch, capsys):
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(yaml.safe_dump(world.manifest()), encoding="utf-8")
    monkeypatch.setattr(check, "GitHub", lambda token=None: world)
    assert check.main([str(path), "--version", VERSION, "--plan"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK:")
    assert "Release plan" in out


def test_ci_false_skips_only_check_2(world, run):
    world.check_runs[check.ENGINE] = [{"name": "pypi", "status": "in_progress", "conclusion": None}]
    _, result = run(world, ci=False)
    assert result.ok, result.errors


# Manifest shape and check 1 --------------------------------------------------------


def test_example_manifest_is_refused():
    path = ROOT / "releases" / "1.0.0.example.yaml"
    world = World()  # nothing exists: the check must refuse before trusting anything
    _, result = check.check(path, world, "1.0.0")
    assert_refused(result, "is an example")
    placeholders = errors_matching(result, "is the placeholder")
    assert len(placeholders) == len(check.TRAIN) + len(check.INPUTS)


def test_example_manifest_main_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(check, "GitHub", lambda token=None: World())
    assert check.main([str(ROOT / "releases" / "1.0.0.example.yaml"), "--version", "1.0.0"]) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_releases_holds_no_real_manifest():
    names = sorted(p.name for p in (ROOT / "releases").glob("*.yaml"))
    assert names == ["1.0.0.example.yaml"]


def test_refuses_missing_repo(world, run):
    data = world.manifest()
    del data["repos"]["chock-devin-plugins"]
    _, result = run(world, data)
    assert_refused(result, "repos.chock-devin-plugins is missing")


def test_refuses_missing_input(world, run):
    data = world.manifest(inputs={})
    _, result = run(world, data)
    assert_refused(result, "inputs.agentseam is missing")


def test_refuses_repo_not_in_train(world, run):
    data = world.manifest()
    data["repos"]["context-report"] = {"commit": sha("x")}
    _, result = run(world, data)
    assert_refused(result, "repos.context-report is not in the train")


@pytest.mark.parametrize("bad", ["abc123", sha("x").upper(), sha("x")[:39], None, 12])
def test_refuses_commit_not_40_lowercase_hex(world, run, bad):
    data = world.manifest()
    data["repos"]["chock"] = {"commit": bad}
    _, result = run(world, data)
    assert_refused(result, "repos.chock.commit must be 40 lowercase hex")


def test_refuses_unknown_top_level_key(world, run):
    _, result = run(world, world.manifest(extra=1))
    assert_refused(result, "unknown top-level key 'extra'")


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "01.0.0", 1])
def test_refuses_bad_manifest_version(world, run, bad):
    _, result = run(world, world.manifest(version=bad), name="m.yaml", version=None)
    assert_refused(result, "version must be a MAJOR.MINOR.PATCH")


def test_refuses_bad_requested_version(world, run):
    _, result = run(world, version="1.0.0; rm")
    assert_refused(result, "input: version must be MAJOR.MINOR.PATCH")


def test_refuses_requested_version_mismatch(world, run):
    _, result = run(world, version="9.8.8")
    assert_refused(result, "the requested release is '9.8.8'")


def test_refuses_filename_not_matching_version(world, run):
    _, result = run(world, name="other.yaml")
    assert_refused(result, "does not match version")


def test_refuses_unreadable_manifest(world, run, tmp_path):
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text("version: [unclosed", encoding="utf-8")
    _, result = check.check(path, world, VERSION)
    assert_refused(result, "cannot read")


def test_refuses_repo_not_found(world, run):
    world.missing_repos.add("chock-example")
    _, result = run(world)
    assert_refused(result, "check 1: chock-example: repository not found")


def test_refuses_commit_that_does_not_exist(world, run):
    world.compare["chock-catalog"] = None
    _, result = run(world)
    assert_refused(result, "check 1: chock-catalog: commit")
    assert_refused(result, "does not exist")


@pytest.mark.parametrize("status", ["ahead", "diverged"])
def test_refuses_commit_not_on_default_branch(world, run, status):
    world.default_branch["chock-site"] = "trunk"
    world.compare["chock-site"] = status
    _, result = run(world)
    assert_refused(result, f"is not on trunk (compare: {status})")


def test_refuses_input_commit_not_on_default_branch(world, run):
    world.compare["agentseam"] = "ahead"
    _, result = run(world)
    assert_refused(result, "check 1: agentseam")


# Check 2 ---------------------------------------------------------------------------


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_refuses_failed_check_run(world, run, conclusion):
    world.check_runs["chock"].append({"name": "lint", "status": "completed", "conclusion": conclusion})
    _, result = run(world)
    assert_refused(result, f"check 2: chock: check 'lint' concluded {conclusion}")


def test_refuses_check_run_in_progress(world, run):
    world.check_runs["chock-catalog"] = [{"name": "shard", "status": "in_progress", "conclusion": None}]
    _, result = run(world)
    assert_refused(result, "check 2: chock-catalog: check 'shard' is in_progress")


def test_refuses_commit_without_ci(world, run):
    world.check_runs["chock-quickstart"] = []
    _, result = run(world)
    assert_refused(result, "check 2: chock-quickstart: no CI ran")


def test_refuses_failed_commit_status(world, run):
    world.statuses["chock-site"] = {"state": "failure", "statuses": [{"context": "ci", "state": "failure"}]}
    _, result = run(world)
    assert_refused(result, "check 2: chock-site: commit status is failure")


def test_accepts_status_only_ci(world, run):
    world.check_runs["chock-site"] = []
    world.statuses["chock-site"] = {"state": "success", "statuses": [{"context": "ci", "state": "success"}]}
    _, result = run(world)
    assert result.ok, result.errors


def test_reads_every_page_of_check_runs(world, run):
    runs = [{"name": f"job{i}", "status": "completed", "conclusion": "success"} for i in range(150)]
    runs[140]["conclusion"] = "failure"
    pages = {1: runs[:100], 2: runs[100:]}
    original = world.get

    def paged(path):
        if path.startswith("/repos/open-coder-ai/chock/commits/") and "/check-runs" in path:
            page = int(path.rsplit("page=", 1)[1])
            return {"total_count": 150, "check_runs": pages.get(page, [])}
        return original(path)

    world.get = paged
    _, result = run(world)
    assert_refused(result, "check 'job140' concluded failure")


# Check 3 ---------------------------------------------------------------------------


def test_refuses_engine_version_mismatch(world, run):
    world.put("chock", "pyproject.toml", '[project]\nname = "chock"\nversion = "9.8.1"\n')
    _, result = run(world)
    assert_refused(result, "check 3: chock: __version__ is '9.8.1'")


def test_refuses_engine_without_version(world, run):
    world.put("chock", "pyproject.toml", '[project]\nname = "chock"\n')
    _, result = run(world)
    assert_refused(result, "check 3: chock: pyproject.toml has no [project].version")


def test_refuses_engine_pyproject_missing(world, run):
    del world.files[("chock", "pyproject.toml")]
    _, result = run(world)
    assert_refused(result, "chock: pyproject.toml not found")


# Check 4 ---------------------------------------------------------------------------


def test_refuses_catalog_framework_ref_mismatch(world, run):
    world.put("chock-catalog", ".framework-ref", sha("old engine"))
    _, result = run(world)
    assert_refused(result, "check 4: chock-catalog: .framework-ref")


def test_refuses_catalog_version_mismatch(world, run):
    world.put("chock-catalog", "VERSION", "9.8.6\n")
    _, result = run(world)
    assert_refused(result, "check 4: chock-catalog: VERSION is '9.8.6'")


def test_refuses_catalog_version_file_missing(world, run):
    del world.files[("chock-catalog", "VERSION")]
    _, result = run(world)
    assert_refused(result, "chock-catalog: VERSION not found")


# Check 5 ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["catalog", "engine"])
def test_refuses_plugin_generated_from_mismatch(world, run, key):
    gen = {"catalog": world.commits["chock-catalog"], "engine": world.commits["chock"]}
    gen[key] = sha("stale")
    world.put("chock-cursor-plugins", "generated_from.yaml", yaml.safe_dump({"generated_from": gen}))
    _, result = run(world)
    assert_refused(result, f"check 5: chock-cursor-plugins: generated_from.{key}")


def test_refuses_plugin_without_provenance(world, run):
    del world.files[("chock-codex-plugins", "generated_from.yaml")]
    _, result = run(world)
    assert_refused(result, "chock-codex-plugins: generated_from.yaml not found")


def test_refuses_plugin_malformed_provenance(world, run):
    world.put("chock-claude-plugins", "generated_from.yaml", "catalog: x\n")
    _, result = run(world)
    assert_refused(result, "check 5: chock-claude-plugins: generated_from.yaml has no generated_from mapping")


# Check 6 ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "source"), [(".framework-ref", "chock"), (".catalog-ref", "chock-catalog")])
def test_refuses_site_pin_mismatch(world, run, path, source):
    world.put("chock-site", path, sha("older pin"))
    _, result = run(world)
    assert_refused(result, f"check 6: chock-site: {path}")
    assert_refused(result, f"manifest {source} is")


def test_refuses_site_version_mismatch(world, run):
    world.put("chock-site", "data/version.json", json.dumps({"version": "9.8.6"}))
    _, result = run(world)
    assert_refused(result, "check 6: chock-site: data/version.json version is '9.8.6'")


def test_refuses_site_version_malformed(world, run):
    world.put("chock-site", "data/version.json", "{}")
    _, result = run(world)
    assert_refused(result, "check 6: chock-site: data/version.json has no version")


# Check 7 ---------------------------------------------------------------------------


def test_refuses_quickstart_wrong_engine_commit(world, run):
    world.put(
        "chock-quickstart",
        "README.md",
        f'pip install "chock @ git+https://github.com/open-coder-ai/chock@{sha("old")}"\n',
    )
    _, result = run(world)
    assert_refused(result, "check 7: chock-quickstart: README.md pins chock commit")


def test_refuses_example_wrong_pypi_version(world, run):
    world.put("chock-example", "requirements.txt", "chock==9.8.6\n")
    _, result = run(world)
    assert_refused(result, "check 7: chock-example: requirements.txt pins chock version '9.8.6'")


def test_refuses_one_stale_pin_among_good_ones(world, run):
    world.put(
        "chock-example",
        "README.md",
        f"pip install chock=={VERSION}\npip install 'chock @ git+https://github.com/open-coder-ai/chock@main'\n",
    )
    _, result = run(world)
    assert_refused(result, "check 7: chock-example: README.md pins chock commit 'main'")


def test_refuses_archive_pin_to_wrong_commit(world, run):
    world.put(
        "chock-example",
        "pyproject.toml",
        f'dependencies = ["chock @ https://github.com/open-coder-ai/chock/archive/{sha("old")}.tar.gz"]\n',
    )
    _, result = run(world)
    assert_refused(result, "check 7: chock-example: pyproject.toml pins chock commit")


def test_refuses_pinner_without_pin(world, run):
    del world.files[("chock-quickstart", "README.md")]
    _, result = run(world)
    assert_refused(result, "check 7: chock-quickstart: no chock pin found")


def test_other_packages_are_not_chock_pins(world, run):
    world.put("chock-example", "requirements.txt", f"chock=={VERSION}\npre-chock==0.1\nchock-extra==0.2\n")
    _, result = run(world)
    assert result.ok, result.errors


# Checks 8 and 9 --------------------------------------------------------------------


def test_existing_tag_at_manifest_commit_passes(world, run):
    world.tags["chock"] = {"object": {"type": "commit", "sha": world.commits["chock"]}}
    _, result = run(world)
    assert result.ok, result.errors
    assert ("chock", world.commits["chock"], "exists") in result.tags


def test_existing_annotated_tag_at_manifest_commit_passes(world, run):
    world.tags["chock-catalog"] = {"object": {"type": "tag", "sha": sha("tag object")}}
    world.tag_objects[sha("tag object")] = {"object": {"type": "commit", "sha": world.commits["chock-catalog"]}}
    _, result = run(world)
    assert result.ok, result.errors


def test_refuses_existing_tag_at_other_commit(world, run):
    world.tags["chock-site"] = {"object": {"type": "commit", "sha": sha("elsewhere")}}
    _, result = run(world)
    assert_refused(result, f"check 9: chock-site: tag v{VERSION} already exists at '{sha('elsewhere')}'")


def test_refuses_existing_annotated_tag_at_other_commit(world, run):
    world.tags["chock"] = {"object": {"type": "tag", "sha": sha("tag object")}}
    world.tag_objects[sha("tag object")] = {"object": {"type": "commit", "sha": sha("elsewhere")}}
    _, result = run(world)
    assert_refused(result, "check 9: chock: tag")


# Reporting and failure modes -------------------------------------------------------


def test_reports_every_mismatch_at_once(world, run):
    world.compare["chock-example"] = "ahead"
    world.check_runs["chock"] = [{"name": "t", "status": "completed", "conclusion": "failure"}]
    world.put("chock", "pyproject.toml", '[project]\nversion = "0.0.1"\n')
    world.put("chock-catalog", "VERSION", "0.0.1")
    world.put("chock-devin-plugins", "generated_from.yaml", "generated_from: {catalog: a, engine: b}\n")
    world.put("chock-site", ".catalog-ref", sha("old"))
    world.put("chock-example", "requirements.txt", "chock==0.0.1\n")
    world.tags["chock-quickstart"] = {"object": {"type": "commit", "sha": sha("elsewhere")}}
    _, result = run(world)
    for needle in ["check 1:", "check 2:", "check 3:", "check 4:", "check 5:", "check 6:", "check 7:", "check 9:"]:
        assert_refused(result, needle)
    assert len(errors_matching(result, "check 5:")) == 2


def test_api_error_fails_closed(world, run):
    world.errors["/repos/open-coder-ai/chock-catalog/contents/"] = "GET x: HTTP 500"
    _, result = run(world)
    assert_refused(result, "chock-catalog: GitHub API error: GET x: HTTP 500")


def test_main_lists_all_mismatches_on_stderr(world, tmp_path, monkeypatch, capsys):
    world.put("chock-catalog", "VERSION", "0.0.1")
    world.put("chock-site", "data/version.json", '{"version": "0.0.1"}')
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(yaml.safe_dump(world.manifest()), encoding="utf-8")
    monkeypatch.setattr(check, "GitHub", lambda token=None: world)
    assert check.main([str(path), "--version", VERSION]) == 1
    err = capsys.readouterr().err
    assert "REFUSED: 2 mismatch(es)" in err
    assert "check 4:" in err and "check 6:" in err


# The real client (urlopen mocked) --------------------------------------------------


class FakeResponse:
    def __init__(self, body: object):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self.body


def test_client_sends_token_as_bearer_header(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return FakeResponse({"ok": 1})

    monkeypatch.setattr(check.urllib.request, "urlopen", fake_urlopen)
    assert check.GitHub(token="t0ken-value").get("/repos/o/r") == {"ok": 1}
    assert seen == {"url": "https://api.github.com/repos/o/r", "auth": "Bearer t0ken-value"}


def test_client_returns_none_on_404_and_never_leaks_token(monkeypatch):
    def raise_http(code):
        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "x", req.headers, None)

        return fake_urlopen

    gh = check.GitHub(token="t0ken-value")
    monkeypatch.setattr(check.urllib.request, "urlopen", raise_http(404))
    assert gh.get("/repos/o/r") is None
    monkeypatch.setattr(check.urllib.request, "urlopen", raise_http(403))
    with pytest.raises(check.GitHubError) as exc:
        gh.get("/repos/o/r")
    assert "t0ken-value" not in str(exc.value)
    assert exc.value.__cause__ is None


def test_main_reads_token_from_environment_only(monkeypatch, tmp_path, capsys):
    world = World.consistent()
    captured = {}

    def factory(token=None):
        captured["token"] = token
        return world

    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(yaml.safe_dump(world.manifest()), encoding="utf-8")
    monkeypatch.setattr(check, "GitHub", factory)
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken-value")
    check.main([str(path), "--version", VERSION, "--plan"])
    out = capsys.readouterr()
    assert captured["token"] == "t0ken-value"
    assert "t0ken-value" not in out.out + out.err


def test_no_real_versions_outside_releases():
    """Real versions live only in releases/. Code, tests and docs use placeholders."""
    tracked = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    offenders = []
    for name in tracked:
        path = ROOT / name
        if name.startswith(("releases/", "requirements/")) or path == Path(__file__) or not path.is_file():
            continue
        if "1.0.0" in path.read_text(encoding="utf-8"):
            offenders.append(name)
    assert offenders == []
