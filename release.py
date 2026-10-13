#!/usr/bin/env python3
"""Release steps 2-6 for one manifest. Runs only after check.py passes, in the
`release` environment, on the owner's approval.

  tag      Tag every train repo v<version> at its manifest commit (skips a tag that
           already points at that commit; refuses one that points elsewhere).
  pypi     Wait until PyPI serves chock==<version> as one sdist and one wheel.
  site     Move chock-site's `release` branch to the manifest commit (fast-forward only).
  verify   Fetch <site>/version.json and refuse unless it reports the manifest's facts.
  summary  Print the release summary (manifest, tags, PyPI URL, site URL) as Markdown.

Credentials come only from the environment (GITHUB_TOKEN, a chock-release-bot app
token). They are never printed. Every step re-runs the consistency check first; steps
after `tag` skip check 2 (CI green), since the new tags start check runs of their own.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import check

PYPI_URL = "https://pypi.org/pypi/chock/{version}/json"
DEFAULT_SITE = "https://chock-site.pages.dev"


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
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https base)
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise check.GitHubError(f"{method} {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise check.GitHubError(f"{method} {path}: {type(exc).__name__}") from None


def fetch_json(url: str) -> object | None:
    if not url.startswith("https://"):
        raise SystemExit(f"refusing non-https URL {url!r}")
    headers = {"User-Agent": "chock-release", "Cache-Control": "no-cache"}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (https checked above)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https URLs)
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def step_tag(gh: GitHubWriter, manifest: check.Manifest, result: check.Result) -> None:
    for repo, commit, state in result.tags:
        name = f"v{manifest.version}"
        if state == "exists":
            print(f"{repo}: {name} already at {commit}, skipped")
            continue
        gh.send("POST", f"/repos/{check.OWNER}/{repo}/git/refs", {"ref": f"refs/tags/{name}", "sha": commit})
        print(f"{repo}: tagged {name} -> {commit}")


def step_pypi(manifest: check.Manifest, timeout_s: int, interval_s: int = 30) -> dict:
    url = PYPI_URL.format(version=manifest.version)
    deadline = time.monotonic() + timeout_s
    while True:
        data = fetch_json(url)
        if isinstance(data, dict):
            files = data.get("urls") or []
            kinds = sorted(f.get("packagetype") for f in files)
            if kinds == ["bdist_wheel", "sdist"]:
                for f in files:
                    print(f"PyPI: {f.get('filename')} sha256={f.get('digests', {}).get('sha256')}")
                return data
            raise SystemExit(f"PyPI serves chock=={manifest.version} with files {kinds}, want one sdist and one wheel")
        if time.monotonic() >= deadline:
            raise SystemExit(f"PyPI did not serve chock=={manifest.version} within {timeout_s}s")
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
    parser.add_argument("step", choices=["tag", "pypi", "site", "verify", "summary"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--version", dest="expect_version", required=True)
    parser.add_argument("--site", default=DEFAULT_SITE)
    parser.add_argument("--timeout", type=int, default=1800, help="seconds to wait (pypi, verify)")
    args = parser.parse_args(argv)

    gh = GitHubWriter(token=os.environ.get("GITHUB_TOKEN") or None)
    manifest, result = check.check(args.manifest, gh, args.expect_version, ci=args.step == "tag")
    if not result.ok or manifest is None:
        print(f"REFUSED: {len(result.errors)} mismatch(es); nothing was changed:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    try:
        if args.step == "tag":
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
