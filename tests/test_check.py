"""One test per refusal in check.py, plus the passing and reporting paths."""

from __future__ import annotations

import http.client
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

import check
from conftest import AGENTSEAM_VERSION, ENGINE_PYPROJECT, ROOT, VERSION, World, sha


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
    assert f"Dry-run build of {check.OWNER}/chock at {world.commits['chock']}" in plan
    assert "agentseam" in plan


def test_main_dry_run_prints_plan_and_exits_zero(world, tmp_path, monkeypatch, capsys):
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(yaml.safe_dump(world.manifest()), encoding="utf-8")
    monkeypatch.setattr(check, "GitHub", lambda token=None: world)
    assert check.main([str(path), "--version", VERSION, "--plan"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK:")
    assert "Release plan" in out


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


def test_refuses_entry_that_is_not_a_mapping(world, run):
    data = world.manifest()
    data["repos"]["chock"] = sha("chock")
    _, result = run(world, data)
    assert_refused(result, "repos.chock must be a mapping with a commit")


def test_refuses_unknown_key_inside_an_entry(world, run):
    data = world.manifest()
    data["inputs"]["agentseam"]["branch"] = "dev"
    _, result = run(world, data)
    assert_refused(result, "inputs.agentseam has an unknown key 'branch'")
    assert len(result.errors) == 1


def test_refuses_unknown_top_level_key(world, run):
    _, result = run(world, world.manifest(extra=1))
    assert_refused(result, "unknown top-level key 'extra'")


def test_refuses_duplicate_yaml_keys(world, tmp_path):
    text = yaml.safe_dump(world.manifest(), sort_keys=False)
    text = text.replace("version: 9.8.7\n", "version: 0.0.1\nversion: 9.8.7\n", 1)
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(text, encoding="utf-8")
    _, result = check.check(path, world, VERSION)
    assert_refused(result, "cannot read")
    assert_refused(result, "duplicate key 'version'")
    assert world.calls == []


def test_refuses_duplicate_repo_entry(world, tmp_path):
    text = yaml.safe_dump(world.manifest(), sort_keys=False)
    text = text.replace("  chock:\n", f"  chock:\n    commit: {'0' * 40}\n  chock:\n", 1)
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(text, encoding="utf-8")
    _, result = check.check(path, world, VERSION)
    assert_refused(result, "duplicate key 'chock'")


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


def test_every_train_repo_and_input_has_a_required_workflow():
    assert set(check.REQUIRED_WORKFLOWS) == set(check.TRAIN) | set(check.INPUTS)
    for workflow, jobs in check.REQUIRED_WORKFLOWS.values():
        assert workflow.endswith(".yml") and jobs


def test_reads_the_required_workflow_on_the_default_branch_only(world, run):
    _, result = run(world)
    assert result.ok
    runs = [c for c in world.calls if "/actions/workflows/" in c]
    assert len(runs) == len(check.TRAIN) + len(check.INPUTS)
    assert all(c.endswith(f"head_sha={world.commits[c.split('/')[3]]}&branch=main&per_page=100") for c in runs)
    assert not any("/check-runs" in c for c in world.calls)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", "skipped"])
def test_refuses_failed_workflow_run(world, run, conclusion):
    world.workflows["chock"].clear()
    world.green("chock", conclusion=conclusion)
    _, result = run(world)
    assert_refused(result, f"check 2: chock: ci.yml run {world.workflows['chock']['ci.yml'][0]['id']}")
    assert_refused(result, f"concluded {conclusion}")


def test_refuses_workflow_run_in_progress(world, run):
    world.workflows["chock-catalog"].clear()
    world.green("chock-catalog", status="in_progress", conclusion=None)
    _, result = run(world)
    assert_refused(result, "check 2: chock-catalog: ci.yml run")
    assert_refused(result, "is in_progress")


def test_refuses_commit_without_a_run(world, run):
    world.workflows["chock-quickstart"]["ci.yml"].clear()
    _, result = run(world)
    assert_refused(result, "check 2: chock-quickstart: ci.yml never ran on")


def test_refuses_repo_without_the_workflow(world, run):
    del world.workflows["chock-example"]["ci.yml"]
    _, result = run(world)
    assert_refused(result, "check 2: chock-example: workflow ci.yml does not exist")


def test_refuses_input_commit_without_green_ci(world, run):
    world.workflows["agentseam"].clear()
    world.green("agentseam", conclusion="failure")
    _, result = run(world)
    assert_refused(result, "check 2: agentseam: ci.yml run")


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "failure", "cancelled"])
def test_refuses_required_job_not_successful(world, run, conclusion):
    run_ = world.workflows["chock-site"]["ci.yml"][0]
    for job in world.jobs[run_["id"]]:
        if job["name"] == "e2e":
            job["conclusion"] = conclusion
    _, result = run(world)
    assert_refused(result, f"check 2: chock-site: required job 'e2e' concluded {conclusion}")


def test_refuses_required_job_missing_from_the_run(world, run):
    run_ = world.workflows["chock"]["ci.yml"][0]
    world.jobs[run_["id"]] = [j for j in world.jobs[run_["id"]] if j["name"] != "validate"]
    _, result = run(world)
    assert_refused(result, "check 2: chock: required job 'validate' is not in ci.yml run")


def test_refuses_required_job_in_progress(world, run):
    run_ = world.workflows["chock"]["ci.yml"][0]
    world.jobs[run_["id"]][0].update({"status": "in_progress", "conclusion": None})
    _, result = run(world)
    assert_refused(result, "check 2: chock: required job 'validate' is in_progress")


def test_matrix_jobs_match_their_name_prefix(world, run):
    run_ = world.workflows["chock"]["ci.yml"][0]
    world.jobs[run_["id"]] = [
        {"name": "validate (ubuntu-latest, 3.12)", "status": "completed", "conclusion": "success"},
        {"name": "validate (ubuntu-latest, 3.13)", "status": "completed", "conclusion": "skipped"},
        {"name": "acceptance (tier 1)", "status": "completed", "conclusion": "success"},
        {"name": "validate-docs", "status": "completed", "conclusion": "failure"},
    ]
    _, result = run(world)
    assert result.errors == [
        f"check 2: chock: required job 'validate (ubuntu-latest, 3.13)' concluded skipped in ci.yml run {run_['id']} on"
        f" {world.commits['chock'][:12]}"
    ]


def test_newest_run_supersedes_an_older_failure(world, run):
    world.workflows["chock"].clear()
    world.green("chock", conclusion="failure")
    newer = world.green("chock")
    newer["created_at"] = "2026-01-02T00:00:00Z"
    _, result = run(world)
    assert result.ok, result.errors


def test_newest_run_failing_refuses_despite_an_older_success(world, run):
    older = world.workflows["chock"]["ci.yml"][0]
    older["created_at"] = "2025-12-31T00:00:00Z"
    world.green("chock", conclusion="failure")
    _, result = run(world)
    assert_refused(result, "check 2: chock: ci.yml run")


def test_runs_on_a_tag_or_another_branch_are_not_read(world, run):
    # A v<version> tag push starts the workflow again with the tag as head_branch, and a
    # scheduled or PR run carries another branch: none of them is this commit's CI.
    world.add_run("chock", "ci.yml", head_branch=f"v{VERSION}", status="in_progress", conclusion=None)
    world.add_run("chock", "ci.yml", head_branch="feature", conclusion="failure")
    world.add_run("chock", "release.yml", head_branch=f"v{VERSION}", status="waiting", conclusion=None)
    world.add_run("chock-catalog", "nightly-transcripts.yml", conclusion="failure")
    _, result = run(world)
    assert result.ok, result.errors


def test_refuses_failed_commit_status(world, run):
    world.statuses["chock-site"] = {"state": "failure", "statuses": [{"context": "ci", "state": "failure"}]}
    _, result = run(world)
    assert_refused(result, "check 2: chock-site: commit status is failure")


def test_commit_status_alone_is_not_ci(world, run):
    world.workflows["chock-site"]["ci.yml"].clear()
    world.statuses["chock-site"] = {"state": "success", "statuses": [{"context": "ci", "state": "success"}]}
    _, result = run(world)
    assert_refused(result, "check 2: chock-site: ci.yml never ran")


def test_reads_every_page_of_jobs(world, run):
    run_ = world.workflows["chock"]["ci.yml"][0]
    jobs = [{"name": f"job{i}", "status": "completed", "conclusion": "success"} for i in range(150)]
    jobs[140] = {"name": "validate", "status": "completed", "conclusion": "failure"}
    jobs[141] = {"name": "acceptance (tier 1)", "status": "completed", "conclusion": "success"}
    pages = {1: jobs[:100], 2: jobs[100:]}
    original = world.get

    def paged(path):
        if f"/actions/runs/{run_['id']}/jobs" in path:
            page = int(path.rsplit("page=", 1)[1])
            return {"total_count": 150, "jobs": pages.get(page, [])}
        return original(path)

    world.get = paged
    _, result = run(world)
    assert_refused(result, "required job 'validate' concluded failure")


# Check 3 ---------------------------------------------------------------------------


def put_engine(world, version=VERSION, deps='["pyyaml>=6.0", "agentseam==7.6.5"]', extras=""):
    world.put(
        "chock", "pyproject.toml", f'[project]\nname = "chock"\nversion = "{version}"\ndependencies = {deps}\n{extras}'
    )


def test_refuses_engine_version_mismatch(world, run):
    put_engine(world, version="9.8.1")
    _, result = run(world)
    assert_refused(result, "check 3: chock: __version__ is '9.8.1'")


def test_refuses_engine_without_version(world, run):
    world.put("chock", "pyproject.toml", '[project]\nname = "chock"\n')
    _, result = run(world)
    assert_refused(result, "check 3: chock: pyproject.toml has no [project].version")


def test_refuses_engine_without_project_table(world, run):
    world.put("chock", "pyproject.toml", '[tool.x]\nname = "chock"\n')
    _, result = run(world)
    assert_refused(result, "check 3: chock: pyproject.toml has no [project] table")


def test_refuses_engine_pyproject_missing(world, run):
    del world.files[("chock", "pyproject.toml")]
    _, result = run(world)
    assert_refused(result, "chock: pyproject.toml not found")


def test_refuses_direct_reference_dependency(world, run):
    ref = f"agentseam @ git+https://github.com/open-coder-ai/agentseam@{sha('as')}"
    put_engine(world, deps=f'["pyyaml>=6.0", "{ref}"]')
    _, result = run(world)
    assert_refused(result, f"check 3: chock: dependency '{ref}' is a direct reference, which PyPI rejects")
    assert_refused(result, "check 3: chock: pyproject.toml pins no agentseam==<version>")


def test_refuses_direct_reference_in_an_extra(world, run):
    put_engine(world, extras='[project.optional-dependencies]\ndev = ["tool @ https://example.invalid/t.tar.gz"]\n')
    _, result = run(world)
    assert_refused(result, "check 3: chock: dependency 'tool @ https://example.invalid/t.tar.gz' is a direct reference")


@pytest.mark.parametrize("dep", ["agentseam>=7.6", "agentseam~=7.6.5", "agentseam", "AgentSeam>=1"])
def test_refuses_agentseam_not_pinned_with_double_equals(world, run, dep):
    put_engine(world, deps=f'["{dep}"]')
    _, result = run(world)
    assert_refused(result, f"check 3: chock: dependency '{dep}' does not pin agentseam==<version>")


def test_refuses_engine_without_agentseam(world, run):
    put_engine(world, deps='["pyyaml>=6.0"]')
    _, result = run(world)
    assert_refused(result, "check 3: chock: pyproject.toml pins no agentseam==<version>")


def test_refuses_two_different_agentseam_pins(world, run):
    put_engine(
        world, deps='["agentseam==7.6.5"]', extras='[project.optional-dependencies]\ndev = ["agentseam==7.6.4"]\n'
    )
    _, result = run(world)
    assert_refused(result, "check 3: chock: pyproject.toml pins agentseam to ['7.6.4', '7.6.5']")


def test_refuses_agentseam_pin_not_matching_input_commit(world, run):
    world.put("agentseam", "pyproject.toml", '[project]\nname = "agentseam"\nversion = "7.6.6"\n')
    _, result = run(world)
    commit = world.commits["agentseam"][:12]
    assert_refused(
        result, f"check 3: agentseam: version is '7.6.6' at {commit}, chock pins agentseam=={AGENTSEAM_VERSION}"
    )


def test_refuses_agentseam_pyproject_missing(world, run):
    del world.files[("agentseam", "pyproject.toml")]
    _, result = run(world)
    assert_refused(result, "agentseam: pyproject.toml not found")


@pytest.mark.parametrize(
    "dep", ['agentseam[extra]==7.6.5; python_version >= \\"3.11\\"', "AgentSeam == 7.6.5", "agentseam==7.6.5 "]
)
def test_accepts_agentseam_pin_spellings(world, run, dep):
    put_engine(world, deps=f'["{dep}"]')
    _, result = run(world)
    assert result.ok, result.errors


def test_engine_pyproject_fixture_matches_the_real_shape():
    assert "[project.optional-dependencies]" in ENGINE_PYPROJECT


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


def test_refuses_plugin_provenance_with_duplicate_keys(world, run):
    c = world.commits
    text = f"generated_from:\n  catalog: {sha('old')}\n  catalog: {c['chock-catalog']}\n  engine: {c['chock']}\n"
    world.put("chock-claude-plugins", "generated_from.yaml", text)
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


def test_refuses_site_agentseam_snapshot_mismatch(world, run):
    world.put("chock-site", "data/agentseam-source/source.json", json.dumps({"agentseam_ref": sha("older")}))
    _, result = run(world)
    assert_refused(result, "check 6: chock-site: data/agentseam-source/source.json agentseam_ref is")
    assert_refused(result, f"manifest agentseam is '{world.commits['agentseam']}'")


def test_refuses_site_agentseam_snapshot_missing(world, run):
    del world.files[("chock-site", "data/agentseam-source/source.json")]
    _, result = run(world)
    assert_refused(result, "chock-site: data/agentseam-source/source.json not found")


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


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/v0.9.0"])
def test_refuses_archive_pin_to_a_branch_or_tag(world, run, ref):
    url = f"https://github.com/open-coder-ai/chock/archive/{ref}.tar.gz"
    world.put("chock-example", "README.md", f'pip install chock=={VERSION}\npip install "chock @ {url}"\n')
    _, result = run(world)
    assert_refused(result, f"check 7: chock-example: README.md pins chock commit '{ref}'")


@pytest.mark.parametrize("spec", ["chock>=0.1.0", "chock~=9.8", "chock[dev]<10", "chock !=9.8.6"])
def test_refuses_range_pin(world, run, spec):
    world.put("chock-example", "requirements.txt", f"chock=={VERSION}\n{spec}\n")
    _, result = run(world)
    assert_refused(result, f"check 7: chock-example: requirements.txt has '{spec}', want chock=={VERSION}")


@pytest.mark.parametrize(
    "line", ["pip install chock", "pip install chock && chock init .", "python -m pip install --upgrade chock\n"]
)
def test_refuses_unpinned_pip_install(world, run, line):
    world.put("chock-example", "docs/README.md", f"Adopt it: `{line}`\n")
    _, result = run(world)
    assert_refused(result, "check 7: chock-example: docs/README.md has 'pip install")
    assert_refused(result, "which is unpinned")


def test_accepts_extras_pinned_to_the_version(world, run):
    world.put("chock-example", "requirements.txt", f"chock[dev]=={VERSION}\n")
    _, result = run(world)
    assert result.ok, result.errors


def test_refuses_pinner_without_pin(world, run):
    del world.files[("chock-quickstart", "README.md")]
    _, result = run(world)
    assert_refused(result, "check 7: chock-quickstart: no chock pin found")
    assert_refused(result, "docs/README.md")


def test_other_packages_are_not_chock_pins(world, run):
    world.put(
        "chock-example",
        "requirements.txt",
        f"chock=={VERSION}\npre-chock==0.1\nchock-extra==0.2\nchock-catalog>=1\npip install chock-site\n",
    )
    world.put(
        "chock-example",
        "README.md",
        'pip install "chock @ git+https://github.com/open-coder-ai/chock@' + sha("chock") + '"\n',
    )
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
    world.workflows["chock"].clear()
    world.green("chock", conclusion="failure")
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


def test_api_error_is_not_reported_as_not_found(world, run):
    world.errors["/repos/open-coder-ai/chock-site/contents/"] = "GET x: HTTP 502"
    world.errors["/repos/open-coder-ai/chock/git/"] = "GET y: HTTP 502"
    world.errors["/repos/open-coder-ai/chock-example/"] = "GET z: HTTP 502"
    _, result = run(world)
    assert not result.ok
    assert all("GitHub API error" in e for e in result.errors), result.errors
    assert not any("not found" in e or "already exists" in e or "does not exist" in e for e in result.errors)
    assert not any(repo == "chock" for repo, _, _ in result.tags)


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


# The real client (the opener mocked) -----------------------------------------------


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

    def fake_open(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return FakeResponse({"ok": 1})

    monkeypatch.setattr(check.OPENER, "open", fake_open)
    assert check.GitHub(token="t0ken-value").get("/repos/o/r") == {"ok": 1}
    assert seen == {"url": "https://api.github.com/repos/o/r", "auth": "Bearer t0ken-value"}


def test_client_returns_none_on_404_and_never_leaks_token(monkeypatch):
    def raise_http(code):
        def fake_open(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", req.headers, None)

        return fake_open

    gh = check.GitHub(token="t0ken-value")
    monkeypatch.setattr(check.OPENER, "open", raise_http(404))
    assert gh.get("/repos/o/r") is None
    monkeypatch.setattr(check.OPENER, "open", raise_http(403))
    with pytest.raises(check.GitHubError) as exc:
        gh.get("/repos/o/r")
    assert "t0ken-value" not in str(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.parametrize(
    "error",
    [
        http.client.IncompleteRead(b""),
        http.client.BadStatusLine("x"),
        ConnectionResetError(),
        TimeoutError(),
        urllib.error.URLError("x"),
        json.JSONDecodeError("x", "", 0),
    ],
)
def test_client_turns_every_network_error_into_a_github_error(monkeypatch, error):
    class Body:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            raise error

    monkeypatch.setattr(check.OPENER, "open", lambda req, timeout=None: Body())
    with pytest.raises(check.GitHubError, match=type(error).__name__):
        check.GitHub(token="t0ken-value").get("/repos/o/r")


def test_redirects_leave_https_only_through_an_error():
    handler = check.HttpsOnlyRedirects()
    req = urllib.request.Request("https://api.github.com/x")
    assert handler.redirect_request(req, None, 302, "Found", {}, "http://api.github.com/x") is None
    followed = handler.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/y")
    assert followed is not None and followed.full_url == "https://api.github.com/y"
    assert any(isinstance(h, check.HttpsOnlyRedirects) for h in check.OPENER.handlers)


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
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    offenders = []
    for name in filter(None, tracked):
        path = ROOT / name
        if name.startswith(("releases/", "requirements/")) or path == Path(__file__) or not path.is_file():
            continue
        if b"1.0.0" in path.read_bytes():
            offenders.append(name)
    assert offenders == []
