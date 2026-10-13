"""release.py: refuses before any write when the check fails; each step's writes."""

from __future__ import annotations

import yaml

import check
import release
from conftest import VERSION, World, sha


class Writer(World):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, str, dict]] = []

    def send(self, method, path, body):
        self.writes.append((method, path, body))
        return {}


def consistent_writer() -> Writer:
    w = Writer()
    w.__dict__.update({k: v for k, v in World.consistent().__dict__.items() if k != "writes"})
    return w


def write_manifest(tmp_path, world) -> str:
    path = tmp_path / f"{VERSION}.yaml"
    path.write_text(yaml.safe_dump(world.manifest(), sort_keys=False), encoding="utf-8")
    return str(path)


def test_refuses_before_any_write_when_check_fails(tmp_path, monkeypatch, capsys):
    w = consistent_writer()
    w.put("chock-catalog", "VERSION", "0.0.1")
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    for step in ("tag", "site", "pypi", "verify", "summary"):
        assert release.main([step, write_manifest(tmp_path, w), "--version", VERSION]) == 1
    assert w.writes == []
    assert "nothing was changed" in capsys.readouterr().err


def test_tag_creates_missing_tags_and_skips_existing(tmp_path, monkeypatch):
    w = consistent_writer()
    w.tags["chock"] = {"object": {"type": "commit", "sha": w.commits["chock"]}}
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    assert release.main(["tag", write_manifest(tmp_path, w), "--version", VERSION]) == 0
    tagged = {path.split("/")[3]: body for _, path, body in w.writes}
    assert set(tagged) == set(check.TRAIN) - {"chock"}
    assert tagged["chock-site"] == {"ref": f"refs/tags/v{VERSION}", "sha": w.commits["chock-site"]}
    assert all(method == "POST" for method, _, _ in w.writes)


def test_tag_refuses_tag_at_other_commit(tmp_path, monkeypatch):
    w = consistent_writer()
    w.tags["chock"] = {"object": {"type": "commit", "sha": sha("elsewhere")}}
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    assert release.main(["tag", write_manifest(tmp_path, w), "--version", VERSION]) == 1
    assert w.writes == []


def test_site_moves_release_fast_forward_only(tmp_path, monkeypatch):
    w = consistent_writer()
    original = w.get
    w.get = lambda path: {"object": {}} if path.endswith("/git/ref/heads/release") else original(path)
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    assert release.main(["site", write_manifest(tmp_path, w), "--version", VERSION]) == 0
    assert w.writes == [
        (
            "PATCH",
            "/repos/open-coder-ai/chock-site/git/refs/heads/release",
            {"sha": w.commits["chock-site"], "force": False},
        )
    ]


def test_site_creates_release_branch_when_absent(tmp_path, monkeypatch):
    w = consistent_writer()
    original = w.get
    w.get = lambda path: None if path.endswith("/git/ref/heads/release") else original(path)
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    assert release.main(["site", write_manifest(tmp_path, w), "--version", VERSION]) == 0
    assert w.writes == [
        (
            "POST",
            "/repos/open-coder-ai/chock-site/git/refs",
            {"ref": "refs/heads/release", "sha": w.commits["chock-site"]},
        )
    ]


def test_steps_after_tag_ignore_new_tag_check_runs(tmp_path, monkeypatch):
    w = consistent_writer()
    w.check_runs["chock"].append({"name": "pypi", "status": "in_progress", "conclusion": None})
    w.get = (lambda orig: lambda p: None if p.endswith("/git/ref/heads/release") else orig(p))(w.get)
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    assert release.main(["site", write_manifest(tmp_path, w), "--version", VERSION]) == 0
    assert release.main(["tag", write_manifest(tmp_path, w), "--version", VERSION]) == 1


def test_pypi_accepts_one_sdist_and_one_wheel(monkeypatch, capsys):
    data = {
        "urls": [
            {"packagetype": "sdist", "filename": "a.tar.gz", "digests": {"sha256": "aa"}},
            {"packagetype": "bdist_wheel", "filename": "a.whl", "digests": {"sha256": "bb"}},
        ]
    }
    monkeypatch.setattr(release, "fetch_json", lambda url: data)
    release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=0)
    assert "sha256=bb" in capsys.readouterr().out


def test_pypi_refuses_wrong_files_and_times_out(monkeypatch):
    import pytest

    monkeypatch.setattr(release, "fetch_json", lambda url: {"urls": [{"packagetype": "sdist"}]})
    with pytest.raises(SystemExit, match="want one sdist and one wheel"):
        release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=0)
    monkeypatch.setattr(release, "fetch_json", lambda url: None)
    with pytest.raises(SystemExit, match="did not serve"):
        release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=0)


def test_verify_checks_version_engine_and_catalog(monkeypatch):
    import pytest

    m = check.Manifest(VERSION, {"chock": sha("e"), "chock-catalog": sha("c")}, {})
    good = {"version": VERSION, "engine": sha("e"), "catalog": sha("c")}
    monkeypatch.setattr(release, "fetch_json", lambda url: good)
    release.step_verify(m, "https://site.invalid", timeout_s=0)
    monkeypatch.setattr(release, "fetch_json", lambda url: {**good, "catalog": sha("old")})
    with pytest.raises(SystemExit, match="want"):
        release.step_verify(m, "https://site.invalid", timeout_s=0)


def test_summary_lists_tags_inputs_and_manifest(tmp_path, monkeypatch, capsys):
    w = consistent_writer()
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    assert release.main(["summary", write_manifest(tmp_path, w), "--version", VERSION]) == 0
    out = capsys.readouterr().out
    assert f"# chock v{VERSION}" in out
    assert w.commits["chock-devin-plugins"] in out
    assert "agentseam" in out
    assert f"https://pypi.org/project/chock/{VERSION}/" in out
    assert w.writes == []


def test_write_error_never_leaks_token(monkeypatch):
    import urllib.error

    import pytest

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 422, "x", req.headers, None)

    monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(check.GitHubError) as exc:
        release.GitHubWriter(token="t0ken-value").send("POST", "/repos/o/r/git/refs", {})
    assert "t0ken-value" not in str(exc.value)
    assert str(exc.value) == "POST /repos/o/r/git/refs: HTTP 422"
