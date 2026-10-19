#!/usr/bin/env python3
"""Release steps for one manifest: a dry-run build, then the tags, PyPI, site and summary.

Every step re-runs check.py first and refuses before any write when it fails. `build` runs
in the dry run too, with no credentials; the rest run in the `release` environment on the
owner's approval.

  build    Clone chock at its manifest commit and build its sdist and wheel, then refuse
           what PyPI would refuse: a version other than the manifest's, a direct-reference
           dependency, anything but one sdist and one wheel. Nothing is uploaded.
  tag      Tag every train repo v<version> at its manifest commit (skips a tag that
           already points at that commit; refuses one that points elsewhere).
  pypi     Wait until PyPI serves chock==<version> as one sdist and one wheel, each with
           a sha256 and neither yanked. A partial file list keeps polling; anything else
           on the version refuses.
  site     Move chock-site's `release` branch to the manifest commit (fast-forward only).
  verify   Fetch <site>/version.json and refuse unless it reports the manifest's facts.
  summary  Print the release summary (manifest, tags, PyPI URL, site URL) as Markdown.

Credentials come only from the environment (GITHUB_TOKEN, a chock-release-bot app
token). They are never printed, and the build's subprocesses run without any variable
whose name contains TOKEN, SECRET, KEY, PASSWORD or CREDENTIAL, or starts with GIT_CONFIG_.
"""

from __future__ import annotations

import argparse
import email.message
import email.parser
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import check

PYPI_URL = "https://pypi.org/pypi/chock/{version}/json"
DEFAULT_SITE = "https://chock-site.pages.dev"
ENGINE_URL = f"https://github.com/{check.OWNER}/{check.ENGINE}"
WANT_KINDS = ("bdist_wheel", "sdist")
SHA256 = re.compile(r"[0-9a-f]{64}")
# Dropped from the build's environment: anything that looks like a credential, and git's
# command-line config injection (GIT_CONFIG_COUNT/KEY_n/VALUE_n), which CI uses to pass
# credential helpers to git.
SECRET_ENV = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|^GIT_CONFIG_", re.IGNORECASE)


class GitHubWriter(check.GitHub):
    """check.GitHub plus the two writes the release needs."""

    def send(self, method: str, path: str, body: dict) -> object:
        body_bytes = json.dumps(body).encode()
        req = urllib.request.Request(self._api + path, data=body_bytes, method=method)  # noqa: S310 (https base)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "chock-release")
        req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with check.OPENER.open(req, timeout=30) as resp:  # noqa: S310 (fixed https base)
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise check.GitHubError(f"{method} {path}: HTTP {exc.code}") from None
        except check.NETWORK_ERRORS as exc:
            raise check.GitHubError(f"{method} {path}: {type(exc).__name__}") from None


def fetch_json(url: str) -> object | None:
    """Parsed JSON from an https URL, or None on any network or decoding failure (retried by the caller)."""
    if not url.startswith("https://"):
        raise SystemExit(f"refusing non-https URL {url!r}")
    headers = {"User-Agent": "chock-release", "Cache-Control": "no-cache"}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (https checked above)
    try:
        with check.OPENER.open(req, timeout=30) as resp:  # noqa: S310 (https URLs; redirects stay https)
            return json.load(resp)
    except check.NETWORK_ERRORS:
        return None


# Step 1: the dry-run build -------------------------------------------------------------


def scrubbed_env() -> dict[str, str]:
    """The environment for the build's subprocesses: every credential-looking variable dropped."""
    return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}


def read_metadata(path: Path) -> email.message.Message:
    """The core metadata of a wheel (METADATA) or an sdist (PKG-INFO)."""
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise KeyError("METADATA")
            text = zf.read(names[0]).decode("utf-8")
    else:
        with tarfile.open(path, "r:gz") as tf:
            members = [m for m in tf.getmembers() if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")]
            if len(members) != 1:
                raise KeyError("PKG-INFO")
            handle = tf.extractfile(members[0])
            if handle is None:
                raise KeyError("PKG-INFO")
            text = handle.read().decode("utf-8")
    return email.parser.HeaderParser().parsestr(text)


def inspect_dist(dist: Path, version: str) -> list[str]:
    """Every reason PyPI would refuse the files in <dist>; empty when they are fit to upload."""
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    errors: list[str] = []
    if len(wheels) != 1 or len(sdists) != 1:
        errors.append(f"dist holds {len(sdists)} sdist(s) and {len(wheels)} wheel(s), want one of each")
    for path in (*sdists, *wheels):
        try:
            meta = read_metadata(path)
        except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile, tarfile.TarError) as exc:
            errors.append(f"{path.name}: cannot read its metadata ({type(exc).__name__})")
            continue
        if meta.get("Name") != check.ENGINE:
            errors.append(f"{path.name}: Name is {meta.get('Name')!r}, want {check.ENGINE!r}")
        if meta.get("Version") != version:
            errors.append(f"{path.name}: Version is {meta.get('Version')!r}, manifest says {version!r}")
        for req in meta.get_all("Requires-Dist") or []:
            if check.DIRECT_REF.match(req):
                errors.append(f"{path.name}: Requires-Dist {req!r} is a direct reference, which PyPI rejects")
    return errors


def step_build(manifest: check.Manifest, workdir: Path, run=None) -> list[Path]:
    """Build chock at its manifest commit into <workdir>/dist and inspect the result."""
    run = run or subprocess.run
    commit = manifest.repos[check.ENGINE]
    src = workdir / "src"
    dist = workdir / "dist"
    src.mkdir()
    env = scrubbed_env()
    commands = (
        ["git", "init", "-q", str(src)],
        ["git", "-C", str(src), "fetch", "-q", "--depth", "1", ENGINE_URL, commit],
        ["git", "-C", str(src), "checkout", "-q", "--detach", "FETCH_HEAD"],
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist), str(src)],
    )
    for argv in commands:
        proc = run(argv, env=env, capture_output=True, text=True, check=False)  # noqa: S603 (fixed argv)
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
            raise SystemExit(f"build: `{' '.join(argv[:3])}` exited {proc.returncode}:\n{tail}")
        if argv[-2:] == ["rev-parse", "HEAD"] and (proc.stdout or "").strip() != commit:
            raise SystemExit(f"build: checked out {(proc.stdout or '').strip()!r}, want {commit}")
    errors = inspect_dist(dist, manifest.version)
    if errors:
        raise SystemExit("build: PyPI would refuse this release:\n" + "\n".join(f"  - {e}" for e in errors))
    files = sorted(p for p in dist.iterdir() if p.is_file())
    for path in files:
        print(f"built {path.name} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
    print(f"build: {check.ENGINE} {manifest.version} at {commit} is fit for PyPI (nothing uploaded)")
    return files


# Steps 2-6 ---------------------------------------------------------------------------------


def step_tag(gh: GitHubWriter, manifest: check.Manifest, result: check.Result) -> None:
    for repo, commit, state in result.tags:
        name = f"v{manifest.version}"
        if state == "exists":
            print(f"{repo}: {name} already at {commit}, skipped")
            continue
        gh.send("POST", f"/repos/{check.OWNER}/{repo}/git/refs", {"ref": f"refs/tags/{name}", "sha": commit})
        print(f"{repo}: tagged {name} -> {commit}")


def pypi_files(data: dict, version: str) -> tuple[list[dict], str | None]:
    """(files, verdict): verdict is None while the upload may still be in progress, "done"
    once one sdist and one wheel are served, else the reason the version is unusable."""
    files = data.get("urls") or []
    if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
        return [], f"PyPI serves chock=={version} with an unreadable file list"
    kinds = sorted(str(f.get("packagetype")) for f in files)
    yanked = [f.get("filename") for f in files if f.get("yanked")]
    if yanked:
        return files, f"PyPI serves chock=={version} with yanked files {yanked}"
    unexpected = [k for k in kinds if k not in WANT_KINDS]
    if unexpected or len(kinds) != len(set(kinds)):
        return files, f"PyPI serves chock=={version} with files {kinds}, want one sdist and one wheel"
    if kinds != sorted(WANT_KINDS):
        return files, None  # a strict subset: the publisher uploads one file at a time
    missing = [f.get("filename") for f in files if not SHA256.fullmatch(str(f.get("digests", {}).get("sha256")))]
    if missing:
        return files, f"PyPI serves chock=={version} without a sha256 for {missing}"
    return files, "done"


def step_pypi(manifest: check.Manifest, timeout_s: int, interval_s: int = 30) -> dict:
    url = PYPI_URL.format(version=manifest.version)
    deadline = time.monotonic() + timeout_s
    seen = "nothing yet"
    while True:
        data = fetch_json(url)
        if isinstance(data, dict):
            files, verdict = pypi_files(data, manifest.version)
            if verdict == "done":
                for f in files:
                    print(f"PyPI: {f.get('filename')} sha256={f['digests']['sha256']}")
                return data
            if verdict is not None:
                raise SystemExit(verdict)
            seen = f"files {sorted(str(f.get('packagetype')) for f in files)}"
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"PyPI did not serve chock=={manifest.version} as one sdist and one wheel within {timeout_s}s "
                f"(last seen: {seen})"
            )
        time.sleep(interval_s)


def step_site(gh: GitHubWriter, manifest: check.Manifest) -> None:
    commit = manifest.repos[check.SITE]
    base = f"/repos/{check.OWNER}/{check.SITE}/git"
    if gh.get(f"{base}/ref/heads/release") is None:
        gh.send("POST", f"{base}/refs", {"ref": "refs/heads/release", "sha": commit})
    else:
        # force=False: GitHub refuses a non-fast-forward move. Rollback is the owner's.
        gh.send("PATCH", f"{base}/refs/heads/release", {"sha": commit, "force": False})
    print(f"{check.SITE}: release -> {commit}")


def step_verify(manifest: check.Manifest, site: str, timeout_s: int, interval_s: int = 30) -> None:
    want = {
        "version": manifest.version,
        "engine": manifest.repos[check.ENGINE],
        "catalog": manifest.repos[check.CATALOG],
    }
    url = site.rstrip("/") + "/version.json"
    deadline = time.monotonic() + timeout_s
    while True:
        got = fetch_json(url)
        if isinstance(got, dict) and all(got.get(k) == v for k, v in want.items()):
            print(f"site: {url} reports {want}")
            return
        if time.monotonic() >= deadline:
            raise SystemExit(f"site: {url} reports {got!r}, want {want}")
        time.sleep(interval_s)


def step_summary(manifest_path: Path, manifest: check.Manifest, site: str) -> str:
    v = manifest.version
    lines = [f"# chock v{v}", "", "## Tags", ""]
    for repo, commit in manifest.repos.items():
        lines.append(f"- `{check.OWNER}/{repo}` v{v} -> `{commit}`")
    lines += ["", "## Recorded inputs (not tagged)", ""]
    for name, commit in manifest.inputs.items():
        lines.append(f"- `{check.OWNER}/{name}` @ `{commit}`")
    lines += [
        "",
        f"PyPI: https://pypi.org/project/chock/{v}/",
        f"Site: {site.rstrip('/')}/",
        "",
        "## Manifest",
        "",
        "```yaml",
        manifest_path.read_text(encoding="utf-8").rstrip(),
        "```",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["build", "tag", "pypi", "site", "verify", "summary"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--version", dest="expect_version", required=True)
    parser.add_argument("--site", default=DEFAULT_SITE)
    parser.add_argument("--timeout", type=int, default=1800, help="seconds to wait (pypi, verify)")
    args = parser.parse_args(argv)

    gh = GitHubWriter(token=os.environ.get("GITHUB_TOKEN") or None)
    manifest, result = check.check(args.manifest, gh, args.expect_version)
    if not result.ok or manifest is None:
        print(f"REFUSED: {len(result.errors)} mismatch(es); nothing was changed:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    try:
        if args.step == "build":
            with tempfile.TemporaryDirectory(prefix="chock-release-build-") as tmp:
                step_build(manifest, Path(tmp))
        elif args.step == "tag":
            step_tag(gh, manifest, result)
        elif args.step == "pypi":
            step_pypi(manifest, args.timeout)
        elif args.step == "site":
            step_site(gh, manifest)
        elif args.step == "verify":
            step_verify(manifest, args.site, args.timeout)
        else:
            print(step_summary(args.manifest, manifest, args.site))
    except check.GitHubError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
