"""release.py: refuses before any write when the check fails; each step's writes and refusals."""

from __future__ import annotations

import http.client
import io
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest
import yaml

import check
import release
from conftest import VERSION, World, sha

STEPS = ("build", "tag", "site", "pypi", "verify", "summary")


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


def test_help_opens_with_a_whole_sentence(capsys):
    with pytest.raises(SystemExit):
        release.main(["--help"])
    first = release.__doc__.splitlines()[0]
    assert first.endswith(".") and first in capsys.readouterr().out.replace("\n", " ")


def test_refuses_before_any_write_when_check_fails(tmp_path, monkeypatch, capsys):
    w = consistent_writer()
    w.put("chock-catalog", "VERSION", "0.0.1")
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: pytest.fail("the build ran"))
    for step in STEPS:
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


def test_every_step_runs_the_full_check_after_tagging(tmp_path, monkeypatch):
    # The v<version> tag starts chock's release.yml and ci.yml again on the tagged commit;
    # those runs carry the tag as head_branch and are not this commit's CI, so a later step
    # (or a re-run of the whole workflow) still passes check 2.
    w = consistent_writer()
    w.add_run("chock", "release.yml", head_branch=f"v{VERSION}", status="waiting", conclusion=None)
    w.add_run("chock", "ci.yml", head_branch=f"v{VERSION}", status="in_progress", conclusion=None)
    for repo in check.TRAIN:
        w.tags[repo] = {"object": {"type": "commit", "sha": w.commits[repo]}}
    w.get = (lambda orig: lambda p: None if p.endswith("/git/ref/heads/release") else orig(p))(w.get)
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    assert release.main(["tag", write_manifest(tmp_path, w), "--version", VERSION]) == 0
    assert release.main(["site", write_manifest(tmp_path, w), "--version", VERSION]) == 0
    w.workflows["chock"]["ci.yml"][0]["conclusion"] = "failure"  # the real CI run on main
    assert release.main(["site", write_manifest(tmp_path, w), "--version", VERSION]) == 1


# Step 1: build ---------------------------------------------------------------------


def metadata(version: str = VERSION, name: str = "chock", requires: tuple[str, ...] = ("pyyaml>=6.0",)) -> str:
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Dist: {r}" for r in requires]
    return "\n".join(lines) + "\n\nThe long description.\n"


def make_wheel(dist: Path, meta: str, version: str = VERSION, inner: str | None = None) -> Path:
    path = dist / f"chock-{version}-py3-none-any.whl"
    inner = inner or f"chock-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("chock/__init__.py", "")
        zf.writestr(inner, meta)
    return path


def make_sdist(dist: Path, meta: str, version: str = VERSION, inner: str | None = None) -> Path:
    path = dist / f"chock-{version}.tar.gz"
    inner = inner or f"chock-{version}/PKG-INFO"
    with tarfile.open(path, "w:gz") as tf:
        data = meta.encode()
        info = tarfile.TarInfo(inner)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return path


def test_inspect_dist_accepts_one_clean_sdist_and_wheel(tmp_path):
    make_wheel(tmp_path, metadata(requires=("pyyaml>=6.0", "agentseam==7.6.5")))
    make_sdist(tmp_path, metadata(requires=("pyyaml>=6.0", "agentseam==7.6.5")))
    assert release.inspect_dist(tmp_path, VERSION) == []


def test_inspect_dist_refuses_direct_reference(tmp_path):
    ref = f"agentseam @ git+https://github.com/open-coder-ai/agentseam@{sha('a')}"
    make_wheel(tmp_path, metadata(requires=("pyyaml>=6.0", ref)))
    make_sdist(tmp_path, metadata())
    errors = release.inspect_dist(tmp_path, VERSION)
    assert errors == [
        f"chock-{VERSION}-py3-none-any.whl: Requires-Dist {ref!r} is a direct reference, which PyPI rejects"
    ]


def test_inspect_dist_refuses_wrong_version_and_name(tmp_path):
    make_wheel(tmp_path, metadata(version="9.8.6"))
    make_sdist(tmp_path, metadata(name="chock-engine"))
    errors = release.inspect_dist(tmp_path, VERSION)
    assert f"chock-{VERSION}.tar.gz: Name is 'chock-engine', want 'chock'" in errors
    assert f"chock-{VERSION}-py3-none-any.whl: Version is '9.8.6', manifest says '{VERSION}'" in errors


def test_inspect_dist_refuses_anything_but_one_of_each(tmp_path):
    make_wheel(tmp_path, metadata())
    make_wheel(tmp_path, metadata(), version="9.8.6")
    assert "dist holds 0 sdist(s) and 2 wheel(s), want one of each" in release.inspect_dist(tmp_path, VERSION)


def test_inspect_dist_refuses_unreadable_metadata(tmp_path):
    make_wheel(tmp_path, metadata(), inner="chock/METADATA.txt")
    make_sdist(tmp_path, metadata(), inner="chock/deeper/PKG-INFO")
    errors = release.inspect_dist(tmp_path, VERSION)
    assert len(errors) == 2 and all("cannot read its metadata (KeyError)" in e for e in errors)


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def fake_runner(commit: str, dist_meta=metadata, calls: list | None = None, rev_parse: str | None = None):
    def run(argv, env, capture_output, text, check):
        if calls is not None:
            calls.append((argv, env))
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return Proc(stdout=(rev_parse or commit) + "\n")
        if argv[1:3] == ["-m", "build"]:
            dist = Path(argv[argv.index("--outdir") + 1])
            dist.mkdir(exist_ok=True)
            make_wheel(dist, dist_meta())
            make_sdist(dist, dist_meta())
        return Proc()

    return run


def test_build_fetches_the_manifest_commit_and_scrubs_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken-value")
    monkeypatch.setenv("RELEASE_APP_PRIVATE_KEY", "k3y-value")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    m = check.Manifest(VERSION, {check.ENGINE: sha("chock")}, {})
    calls: list = []
    files = release.step_build(m, tmp_path, run=fake_runner(sha("chock"), calls=calls))
    assert [p.name for p in files] == [f"chock-{VERSION}-py3-none-any.whl", f"chock-{VERSION}.tar.gz"]
    fetch = [argv for argv, _ in calls if "fetch" in argv][0]
    assert fetch[-2:] == [release.ENGINE_URL, sha("chock")] and "--depth" in fetch
    assert [argv[1:3] for argv, _ in calls][-1] == ["-m", "build"]
    for _, env in calls:
        assert "GITHUB_TOKEN" not in env and "RELEASE_APP_PRIVATE_KEY" not in env and "GIT_CONFIG_COUNT" not in env
        assert env["HOME"] == str(tmp_path)
    out = capsys.readouterr().out
    assert "sha256=" in out and "nothing uploaded" in out and "t0ken-value" not in out


def test_build_refuses_what_pypi_would_refuse(tmp_path):
    m = check.Manifest(VERSION, {check.ENGINE: sha("chock")}, {})
    bad = lambda: metadata(requires=("agentseam @ https://example.invalid/a.tar.gz",))  # noqa: E731
    with pytest.raises(SystemExit, match="PyPI would refuse this release") as exc:
        release.step_build(m, tmp_path, run=fake_runner(sha("chock"), dist_meta=bad))
    assert "direct reference" in str(exc.value)


def test_build_refuses_a_checkout_at_another_commit(tmp_path):
    m = check.Manifest(VERSION, {check.ENGINE: sha("chock")}, {})
    with pytest.raises(SystemExit, match=f"checked out '{sha('other')}', want {sha('chock')}"):
        release.step_build(m, tmp_path, run=fake_runner(sha("chock"), rev_parse=sha("other")))


def test_build_reports_a_failed_command_without_its_environment(tmp_path):
    m = check.Manifest(VERSION, {check.ENGINE: sha("chock")}, {})

    def failing(argv, env, capture_output, text, check):
        return Proc(returncode=128, stderr="fatal: could not read from remote")

    with pytest.raises(SystemExit, match="build: `git init") as exc:
        release.step_build(m, tmp_path, run=failing)
    assert "exited 128" in str(exc.value) and "could not read" in str(exc.value)


def test_main_build_runs_the_check_then_builds_in_a_temporary_directory(tmp_path, monkeypatch, capsys):
    w = consistent_writer()
    monkeypatch.setattr(release, "GitHubWriter", lambda token=None: w)
    seen: list[Path] = []

    def run(argv, env, capture_output, text, check):
        if argv[1:3] == ["-m", "build"]:
            seen.append(Path(argv[-1]))
        return fake_runner(w.commits["chock"])(argv, env, capture_output, text, check)

    monkeypatch.setattr(release.subprocess, "run", run)
    assert release.main(["build", write_manifest(tmp_path, w), "--version", VERSION]) == 0
    assert w.writes == []
    assert len(seen) == 1 and not seen[0].exists()  # the temporary clone is gone
    assert f"chock {VERSION} at {w.commits['chock']} is fit for PyPI" in capsys.readouterr().out


# Step 3: PyPI ----------------------------------------------------------------------


def pypi(*files: dict) -> dict:
    return {"urls": list(files)}


WHEEL = {"packagetype": "bdist_wheel", "filename": "a.whl", "digests": {"sha256": "b" * 64}}
SDIST = {"packagetype": "sdist", "filename": "a.tar.gz", "digests": {"sha256": "a" * 64}}


def test_pypi_accepts_one_sdist_and_one_wheel(monkeypatch, capsys):
    monkeypatch.setattr(release, "fetch_json", lambda url: pypi(SDIST, WHEEL))
    release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=0)
    out = capsys.readouterr().out
    assert f"a.whl sha256={'b' * 64}" in out and f"a.tar.gz sha256={'a' * 64}" in out


def test_pypi_keeps_polling_while_the_upload_is_partial(monkeypatch):
    answers = iter([None, pypi(), pypi(WHEEL), pypi(SDIST, WHEEL)])
    polls = []
    monkeypatch.setattr(release, "fetch_json", lambda url: next(answers))
    monkeypatch.setattr(release.time, "sleep", lambda s: polls.append(s))
    release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=3600, interval_s=7)
    assert polls == [7, 7, 7]


def test_pypi_times_out_naming_what_it_last_saw(monkeypatch):
    monkeypatch.setattr(release, "fetch_json", lambda url: pypi(WHEEL))
    with pytest.raises(SystemExit, match=r"did not serve .* within 0s \(last seen: files \['bdist_wheel'\]\)"):
        release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=0)
    monkeypatch.setattr(release, "fetch_json", lambda url: None)
    with pytest.raises(SystemExit, match=r"\(last seen: nothing yet\)"):
        release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=0)


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ((SDIST, WHEEL, WHEEL), "with files ['bdist_wheel', 'bdist_wheel', 'sdist']"),
        ((SDIST, {**WHEEL, "packagetype": "bdist_egg"}), "with files ['bdist_egg', 'sdist']"),
        ((SDIST, {**WHEEL, "packagetype": None}), "with files ['None', 'sdist']"),
        ((SDIST, {**WHEEL, "yanked": True}), "with yanked files ['a.whl']"),
        ((SDIST, {**WHEEL, "digests": {}}), "without a sha256 for ['a.whl']"),
        ((SDIST, {**WHEEL, "digests": {"sha256": "short"}}), "without a sha256 for ['a.whl']"),
    ],
)
def test_pypi_refuses_an_unusable_version_at_once(monkeypatch, files, reason):
    monkeypatch.setattr(release, "fetch_json", lambda url: pypi(*files))
    monkeypatch.setattr(release.time, "sleep", lambda s: pytest.fail("polled again"))
    with pytest.raises(SystemExit) as exc:
        release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=3600)
    assert reason in str(exc.value)


def test_pypi_refuses_an_unreadable_file_list(monkeypatch):
    monkeypatch.setattr(release, "fetch_json", lambda url: {"urls": "nope"})
    with pytest.raises(SystemExit, match="unreadable file list"):
        release.step_pypi(check.Manifest(VERSION, {}, {}), timeout_s=3600)


# Steps 5 and 6 ---------------------------------------------------------------------


def test_verify_checks_version_engine_and_catalog(monkeypatch):
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


# The network ------------------------------------------------------------------------


def test_write_error_never_leaks_token(monkeypatch):
    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "x", req.headers, None)

    monkeypatch.setattr(check.OPENER, "open", fake_open)
    with pytest.raises(check.GitHubError) as exc:
        release.GitHubWriter(token="t0ken-value").send("POST", "/repos/o/r/git/refs", {})
    assert "t0ken-value" not in str(exc.value)
    assert str(exc.value) == "POST /repos/o/r/git/refs: HTTP 422"


@pytest.mark.parametrize("error", [http.client.IncompleteRead(b""), ConnectionResetError(), TimeoutError()])
def test_write_turns_every_network_error_into_a_github_error(monkeypatch, error):
    def fake_open(req, timeout=None):
        raise error

    monkeypatch.setattr(check.OPENER, "open", fake_open)
    with pytest.raises(check.GitHubError, match=type(error).__name__):
        release.GitHubWriter(token="t0ken-value").send("POST", "/repos/o/r/git/refs", {})


@pytest.mark.parametrize(
    "error",
    [
        http.client.IncompleteRead(b""),
        http.client.BadStatusLine("x"),
        ConnectionResetError(),
        urllib.error.URLError("x"),
    ],
)
def test_fetch_json_returns_none_on_a_dropped_connection(monkeypatch, error):
    class Body:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            raise error

    monkeypatch.setattr(check.OPENER, "open", lambda req, timeout=None: Body())
    assert release.fetch_json("https://pypi.org/pypi/chock/x/json") is None


def test_fetch_json_refuses_http():
    with pytest.raises(SystemExit, match="non-https"):
        release.fetch_json("http://pypi.org/pypi/chock/x/json")
