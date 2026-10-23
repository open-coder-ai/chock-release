"""plugins.py publish against a fake GitHub API. Nothing here touches the network or dispatches a real run."""

from __future__ import annotations

import base64
import json
import urllib.parse

import pytest

import check
import plugins
from conftest import sha

CATALOG = sha("catalog-commit")
ENGINE = sha("engine-commit")
OLD = sha("old")
ENGINE_PYPROJECT = '[project]\nname = "chock"\ndependencies = ["pyyaml>=6", "agentseam==0.4.1"]\n'


def lock(version: str) -> str:
    return f"pyyaml==6.0.2 \\\n    --hash=sha256:aa\nagentseam=={version} \\\n    --hash=sha256:bb\n"


def b64(text: str) -> dict:
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def flat(catalog: str, engine: str) -> str:
    return f"generated_from:\n  catalog: {catalog}\n  engine: {engine}\n"


def nested(catalog: str, engine: str) -> str:
    return (
        f'catalog:\n  repository: open-coder-ai/chock-catalog\n  ref: "x"\n  sha: "{catalog}"\n'
        f'framework:\n  repository: open-coder-ai/chock\n  ref: "y"\n  sha: "{engine}"\n'
    )


class FakeGitHub(check.GitHub):
    """Dispatching a workflow adds a run that completes after `polls` reads; publish moves main."""

    def __init__(self) -> None:
        super().__init__(token=None)
        self.framework_ref = ENGINE + "\n"
        self.catalog_main = CATALOG
        self.polls = 2
        self.runs: dict[tuple[str, str], list[dict]] = {}
        self.pending: dict[int, int] = {}
        self.conclusion: dict[tuple[str, str], str] = {}  # (repo, workflow) -> conclusion
        self.verify_jobs: dict[str, list[dict]] = {}
        self.never_appears: set[tuple[str, str]] = set()
        self.main: dict[str, str] = {r: OLD for r in check.PLUGINS}
        self.provenance: dict[str, str] = {r: flat(CATALOG, ENGINE) for r in check.PLUGINS}
        self.engine_pyproject = ENGINE_PYPROJECT
        self.locks: dict[str, str | None] = {r: lock("0.4.1") for r in check.PLUGINS}
        self.dispatched: list[tuple[str, str, dict]] = []
        self.errors: dict[str, str] = {}
        self.next_id = 1000

    def get(self, path: str):
        for prefix, msg in self.errors.items():
            if path.startswith(prefix):
                raise check.GitHubError(msg)
        prefix = f"/repos/{check.OWNER}/"
        repo, _, rest = path[len(prefix) :].partition("/")
        if repo == check.ENGINE and rest.startswith("contents/pyproject.toml"):
            return b64(self.engine_pyproject)
        if rest.startswith("contents/.github/requirements/framework.txt"):
            return b64(self.locks[repo]) if self.locks[repo] is not None else None
        if repo == check.CATALOG and rest.startswith("contents/.framework-ref"):
            return b64(self.framework_ref) if self.framework_ref is not None else None
        if repo == check.CATALOG and rest == "git/ref/heads/main":
            return {"object": {"sha": self.catalog_main}}
        if rest == "git/ref/heads/main":
            return {"object": {"sha": self.main[repo]}}
        if rest.startswith("contents/generated_from.yaml"):
            return b64(self.provenance[repo])
        if rest.startswith("actions/workflows/"):
            workflow, _, query = rest[len("actions/workflows/") :].partition("/runs?")
            params = urllib.parse.parse_qs(query)
            assert params["event"] == ["workflow_dispatch"] and params["branch"] == ["main"]
            return {"workflow_runs": list(self.runs.get((repo, workflow), []))}
        if rest.startswith("actions/runs/") and rest.endswith("/jobs?per_page=100"):
            run_id = int(rest.split("/")[2])
            return {"jobs": self.verify_jobs.get(repo, [{"name": "verify", "conclusion": "success"}])}
        if rest.startswith("actions/runs/"):
            run_id = int(rest.split("/")[2])
            for runs in self.runs.values():
                for run in runs:
                    if run["id"] == run_id:
                        self.pending[run_id] -= 1
                        if self.pending[run_id] > 0:
                            return {**run, "status": "in_progress", "conclusion": None}
                        return dict(run)
        raise AssertionError(f"unexpected GET {path}")

    def send(self, method: str, path: str, body: dict):
        assert method == "POST"
        prefix = f"/repos/{check.OWNER}/"
        repo, _, rest = path[len(prefix) :].partition("/")
        workflow = rest.split("/")[2]
        assert rest.endswith("/dispatches")
        for prefix_, msg in self.errors.items():
            if path.startswith(prefix_):
                raise check.GitHubError(msg)
        self.dispatched.append((repo, workflow, body))
        assert body["ref"] == "main"
        if (repo, workflow) in self.never_appears:
            return None
        self.next_id += 1
        conclusion = self.conclusion.get((repo, workflow), "success")
        self.runs.setdefault((repo, workflow), []).append(
            {"id": self.next_id, "status": "completed", "conclusion": conclusion}
        )
        self.pending[self.next_id] = self.polls
        if workflow == plugins.PUBLISH and body["inputs"]["dry_run"] == "false" and conclusion == "success":
            self.main[repo] = sha(f"new-{repo}")
        return None


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept = 0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += 1


def run_publish(gh: FakeGitHub, dry_run: bool = False, only: list[str] | None = None, catalog=CATALOG, engine=ENGINE):
    lines: list[str] = []
    clock = Clock()
    code = plugins.Plugins(
        gh, catalog, engine, dry_run, only, out=lines.append, sleep=clock.sleep, now=clock.now, timeout=600
    ).publish()
    return code, "\n".join(lines)


def test_real_run_publishes_verifies_and_prints_each_head():
    gh = FakeGitHub()
    code, out = run_publish(gh)
    assert code == 0, out
    publishes = [d for d in gh.dispatched if d[1] == "publish.yml"]
    assert [d[0] for d in publishes] == list(check.PLUGINS)
    for _, _, body in publishes:
        assert body == {"ref": "main", "inputs": {"catalog_ref": CATALOG, "framework_ref": "auto", "dry_run": "false"}}
    verifies = [d for d in gh.dispatched if d[1] == "generated-only.yml"]
    assert [d[0] for d in verifies] == list(check.PLUGINS)
    assert all(body == {"ref": "main"} for _, _, body in verifies)
    # publish before verify, per repo
    order = [(r, w) for r, w, _ in gh.dispatched if r == "chock-codex-plugins"]
    assert order == [("chock-codex-plugins", "publish.yml"), ("chock-codex-plugins", "generated-only.yml")]
    for repo in check.PLUGINS:
        assert f"{repo}: main is now {sha(f'new-{repo}')}" in out
        assert f"catalog {CATALOG}, engine {ENGINE}" in out
    assert out.rstrip().endswith("OK: every plugin repo is published and verified")


def test_dry_run_dispatches_dry_publish_only():
    gh = FakeGitHub()
    code, out = run_publish(gh, dry_run=True)
    assert code == 0, out
    assert {w for _, w, _ in gh.dispatched} == {"publish.yml"}
    assert all(b["inputs"]["dry_run"] == "true" for _, _, b in gh.dispatched)
    assert len(gh.dispatched) == 5
    assert gh.main == {r: OLD for r in check.PLUGINS}  # the fake moves main only on a real publish
    assert "built green (dry run)" in out


def test_only_limits_the_repos():
    gh = FakeGitHub()
    code, _ = run_publish(gh, only=["chock-devin-plugins"])
    assert code == 0
    assert {r for r, _, _ in gh.dispatched} == {"chock-devin-plugins"}


def test_refuses_when_catalog_framework_ref_is_not_the_engine():
    gh = FakeGitHub()
    gh.framework_ref = sha("other") + "\n"
    code, out = run_publish(gh)
    assert code == 1
    assert "declares framework" in out and ENGINE in out
    assert gh.dispatched == []


def test_refuses_when_catalog_has_no_framework_ref():
    gh = FakeGitHub()
    gh.framework_ref = None
    code, out = run_publish(gh)
    assert code == 1 and "no .framework-ref" in out
    assert gh.dispatched == []


@pytest.mark.parametrize("bad", ["main", "abc123", sha("x").upper(), sha("x") + "0"])
def test_refuses_a_short_or_named_commit(bad):
    gh = FakeGitHub()
    assert run_publish(gh, catalog=bad)[0] == 1
    assert run_publish(gh, engine=bad)[0] == 1
    assert gh.dispatched == []


def test_warns_when_catalog_commit_is_not_catalog_main():
    gh = FakeGitHub()
    gh.catalog_main = sha("moved")
    code, out = run_publish(gh)
    assert code == 0
    assert "WARNING" in out and "regenerates from main" in out


@pytest.mark.parametrize("text", [flat(sha("stale"), ENGINE), nested(CATALOG, sha("stale")), "nonsense: 1\n"])
def test_refuses_when_provenance_names_another_commit(text):
    gh = FakeGitHub()
    gh.provenance["chock-cursor-plugins"] = text
    code, out = run_publish(gh)
    assert code == 1
    assert "REFUSED: chock-cursor-plugins:" in out
    assert "FAILED: chock-cursor-plugins" in out
    assert out.count("REFUSED") == 1  # the other four still ran
    assert "chock-devin-plugins: in sync" in out


def test_accepts_the_layout_publish_yml_writes():
    gh = FakeGitHub()
    gh.provenance = {r: nested(CATALOG, ENGINE) for r in check.PLUGINS}
    assert run_publish(gh)[0] == 0


def test_failed_publish_run_skips_verify_for_that_repo():
    gh = FakeGitHub()
    gh.conclusion[("chock-claude-plugins", "publish.yml")] = "failure"
    code, out = run_publish(gh)
    assert code == 1
    assert "concluded 'failure'" in out
    assert ("chock-claude-plugins", "generated-only.yml") not in [(r, w) for r, w, _ in gh.dispatched]


def test_red_verify_job_is_refused():
    gh = FakeGitHub()
    gh.conclusion[("chock-codex-plugins", "generated-only.yml")] = "failure"
    code, out = run_publish(gh)
    assert code == 1 and "generated-only.yml run" in out and "concluded 'failure'" in out


def test_verify_run_without_a_green_verify_job_is_refused():
    gh = FakeGitHub()
    gh.verify_jobs["chock-copilot-plugins"] = [{"name": "lint", "conclusion": "success"}]
    code, out = run_publish(gh)
    assert code == 1
    assert "chock-copilot-plugins: generated-only.yml has no green verify job (lint=success)" in out


def test_a_run_that_never_appears_times_out():
    gh = FakeGitHub()
    gh.never_appears.add(("chock-devin-plugins", "publish.yml"))
    code, out = run_publish(gh, only=["chock-devin-plugins"])
    assert code == 1 and "no run appeared in 600s" in out


def test_api_error_on_dispatch_is_refused_and_names_no_token():
    gh = FakeGitHub()
    gh.errors["/repos/open-coder-ai/chock-devin-plugins/actions/workflows/publish.yml/dispatches"] = "POST: HTTP 403"
    code, out = run_publish(gh, only=["chock-devin-plugins"])
    assert code == 1 and "could not dispatch publish.yml" in out and "HTTP 403" in out


def test_main_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert plugins.main(["publish", "--catalog", CATALOG, "--engine", ENGINE, "--dry-run"]) == 1
    assert "GITHUB_TOKEN is not set" in capsys.readouterr().err


def test_main_rejects_a_repo_that_is_not_a_plugin_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    with pytest.raises(SystemExit) as exc:
        plugins.main(["publish", "--catalog", CATALOG, "--engine", ENGINE, "--only", "chock-site"])
    assert exc.value.code == 2


def test_send_accepts_an_empty_204_body(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    seen = {}

    def fake_open(req, timeout):
        seen["body"] = json.loads(req.data)
        seen["method"] = req.get_method()
        return Resp()

    monkeypatch.setattr(check.OPENER, "open", fake_open)
    assert (
        check.GitHub(token="t").send("POST", "/repos/o/r/actions/workflows/w.yml/dispatches", {"ref": "main"}) is None
    )
    assert seen == {"body": {"ref": "main"}, "method": "POST"}


def test_refuses_before_dispatching_when_a_plugin_repo_pins_another_agentseam():
    gh = FakeGitHub()
    gh.locks["chock-claude-plugins"] = lock("0.3.5")
    gh.locks["chock-codex-plugins"] = lock("0.3.5")
    code, out = run_publish(gh)
    assert code == 1
    assert "chock-claude-plugins: .github/requirements/framework.txt pins agentseam 0.3.5, engine" in out
    assert "requires 0.4.1" in out and "chock-codex-plugins:" in out
    assert "chock-cursor-plugins:" not in out
    assert gh.dispatched == []


def test_only_checks_the_repos_it_will_publish():
    gh = FakeGitHub()
    gh.locks["chock-claude-plugins"] = lock("0.3.5")
    code, _ = run_publish(gh, only=["chock-devin-plugins"])
    assert code == 0


def test_refuses_a_plugin_repo_with_no_agentseam_pin_or_no_lock():
    gh = FakeGitHub()
    gh.locks["chock-claude-plugins"] = "pyyaml==6.0.2\n"
    gh.locks["chock-devin-plugins"] = None
    code, out = run_publish(gh)
    assert code == 1
    assert "chock-claude-plugins: .github/requirements/framework.txt pins agentseam nothing" in out
    assert "chock-devin-plugins: no .github/requirements/framework.txt on main" in out
    assert gh.dispatched == []


def test_refuses_an_engine_with_a_direct_agentseam_reference():
    gh = FakeGitHub()
    gh.engine_pyproject = (
        '[project]\nname = "chock"\ndependencies = ["agentseam @ git+https://github.com/x/agentseam@abc"]\n'
    )
    code, out = run_publish(gh)
    assert code == 1
    assert "direct reference" in out and "release-week check 3" in out
    assert gh.dispatched == []


def test_refuses_an_engine_that_does_not_pin_agentseam_with_equals():
    gh = FakeGitHub()
    gh.engine_pyproject = '[project]\nname = "chock"\ndependencies = ["agentseam>=0.4"]\n'
    code, out = run_publish(gh)
    assert code == 1 and "not agentseam==<version>" in out
    assert gh.dispatched == []
