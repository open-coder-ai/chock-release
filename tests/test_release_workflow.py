"""The shell of release.yml's publish-summary step, run against a fake `gh`.

Only the network is replaced: the step's own script is read out of the workflow file and run by bash.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import ROOT

TARGET = "a" * 40
OTHER = "b" * 40
TAG_OBJECT = "c" * 40
STEP = "Create the GitHub release, once"

FAKE_GH = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
scenario = json.load(open(os.environ["GH_SCENARIO"]))
with open(os.environ["GH_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
if args[:2] == ["release", "view"]:
    print("release not found", file=sys.stderr)
    sys.exit(1)
if args[:1] == ["api"]:
    reply = scenario["api"].get(args[1].split("/repos/")[-1].split("repos/")[-1], scenario["api"].get("*"))
    if reply is None:
        reply = {{"status": 404}}
    if reply["status"] != 200:
        print(f"gh: error (HTTP {{reply['status']}})", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(reply["body"]))
    sys.exit(0)
if args[:2] == ["release", "create"]:
    sys.exit(0)
sys.exit(2)
"""


def script() -> str:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-summary"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == STEP)


def run_step(tmp_path: Path, api: dict) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"api": api}), encoding="utf-8")
    log = tmp_path / "gh.log"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_SCENARIO": str(scenario),
        "GH_LOG": str(log),
        "GH_REPO": "open-coder-ai/chock-release",
        "TARGET": TARGET,
        "VERSION": "1.2.3",
    }
    proc = subprocess.run(  # noqa: S603
        ["bash", "-e", "-o", "pipefail", "-c", script()],  # noqa: S607
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def created(calls: list[str]) -> bool:
    return any(c.startswith("release create") for c in calls)


def ref(kind: str, sha: str) -> dict:
    return {"status": 200, "body": {"object": {"type": kind, "sha": sha}}}


REF_PATH = "open-coder-ai/chock-release/git/ref/tags/v1.2.3"


def test_missing_tag_creates_the_release(tmp_path):
    proc, calls = run_step(tmp_path, {})  # no ref entry: 404
    assert proc.returncode == 0, proc.stderr
    assert created(calls)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
def test_any_failure_other_than_404_refuses_and_creates_nothing(tmp_path, status):
    proc, calls = run_step(tmp_path, {REF_PATH: {"status": status}})
    assert proc.returncode != 0
    assert not created(calls)
    assert "could not look up" in proc.stderr


def test_lightweight_tag_at_target_is_left_to_the_release(tmp_path):
    proc, calls = run_step(tmp_path, {REF_PATH: ref("commit", TARGET)})
    assert proc.returncode == 0, proc.stderr
    assert created(calls)


def test_lightweight_tag_at_another_commit_is_refused(tmp_path):
    proc, calls = run_step(tmp_path, {REF_PATH: ref("commit", OTHER)})
    assert proc.returncode != 0
    assert not created(calls)
    assert OTHER in proc.stderr


def test_annotated_tag_is_peeled_to_its_commit(tmp_path):
    api = {
        REF_PATH: ref("tag", TAG_OBJECT),
        f"open-coder-ai/chock-release/git/tags/{TAG_OBJECT}": ref("commit", TARGET),
    }
    proc, calls = run_step(tmp_path, api)
    assert proc.returncode == 0, proc.stderr
    assert created(calls)


def test_annotated_tag_on_another_commit_is_refused(tmp_path):
    api = {
        REF_PATH: ref("tag", TAG_OBJECT),
        f"open-coder-ai/chock-release/git/tags/{TAG_OBJECT}": ref("commit", OTHER),
    }
    proc, calls = run_step(tmp_path, api)
    assert proc.returncode != 0
    assert not created(calls)


def test_annotated_tag_that_cannot_be_read_is_refused(tmp_path):
    api = {REF_PATH: ref("tag", TAG_OBJECT), f"open-coder-ai/chock-release/git/tags/{TAG_OBJECT}": {"status": 500}}
    proc, calls = run_step(tmp_path, api)
    assert proc.returncode != 0
    assert not created(calls)


def test_tag_pointing_at_a_tree_is_refused(tmp_path):
    proc, calls = run_step(tmp_path, {REF_PATH: ref("tree", TARGET)})
    assert proc.returncode != 0
    assert not created(calls)
