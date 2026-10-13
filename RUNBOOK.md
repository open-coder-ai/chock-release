# Release runbook

How the owner releases the chock train: one version, every train repo tagged `vX.Y.Z`
at the commit its manifest names. The design is org-plan `plan/release-1.md`.
`X.Y.Z` below stands for the version being released.

Only the owner runs the release workflow. Agents never trigger it, never tag, never
create the environment, the app or its secrets.

## What is where

| Path | What it is |
|---|---|
| `releases/X.Y.Z.yaml` | The manifest: `version`, one 40-hex `commit` per train repo, and recorded `inputs` (agentseam). |
| `releases/*.example.yaml` | The manifest shape with placeholder commits. `check.py` refuses it. |
| `check.py` | The consistency check (checks 1–9 below). Lists every mismatch, then exits 1. |
| `release.py` | Release steps 2–6. Each step re-runs the check before it writes anything. |
| `.github/workflows/release.yml` | `workflow_dispatch` with `version` and `dry_run` (default true). |

The train: `chock`, `chock-catalog`, `chock-{claude,cursor,codex,copilot,devin}-plugins`,
`chock-site`, `chock-quickstart`, `chock-example`. Recorded but never tagged: `agentseam`.

## What the check refuses

1. A train repo missing, an extra repo, a commit that is not 40 lowercase hex, the
   all-zero placeholder, or a commit that is not on the repo's default branch.
2. CI not green on a named commit: any check run not completed, any conclusion other
   than success, neutral or skipped, a failing commit status, or no CI at all.
3. `chock`'s `pyproject.toml` `[project].version` (that is `chock.__version__`) is not `X.Y.Z`.
4. `chock-catalog`'s `.framework-ref` is not the engine commit, or its `VERSION` is not `X.Y.Z`.
5. A plugin repo's `generated_from.yaml` (`generated_from: {catalog, engine}`) does not
   name the manifest's catalog and engine commits.
6. `chock-site`'s `.framework-ref` or `.catalog-ref` differs from the manifest, or its
   `data/version.json` `version` is not `X.Y.Z`.
7. `chock-quickstart` or `chock-example` has no chock pin in `README.md`,
   `requirements.txt` or `pyproject.toml`, or any pin there is neither the engine commit
   nor `chock==X.Y.Z`.
8. and 9. A `vX.Y.Z` tag already exists at a different commit. A tag at the manifest
   commit passes, so a re-run after a partial release continues where it stopped.

A train repo that is private reads as "repository not found" with the dry run's token,
so the check refuses until `chock-site` is public (owner setup checklist item 2).

## One-time owner setup (plan/release-1.md, "Owner setup checklist")

- In chock-release, create the environment `release`: required reviewer the owner,
  deployment branches `main` only. Add its secrets `RELEASE_APP_CLIENT_ID` (the
  chock-release-bot app's client ID) and `RELEASE_APP_PRIVATE_KEY`. Optionally set the
  variable `RELEASE_SITE_URL` (default `https://chock-site.pages.dev`; set it to
  `https://chock.sh` after launch).
- The app, chock-release-bot, has Contents read/write and is installed only on the
  train repos. The check reads CI state through the public API, so every train repo
  must be public.
- After this repo's bootstrap PR merges, remove chock-release from the Claude app's
  repository access.

## Before the release: agentseam first

chock depends on agentseam. PyPI rejects a direct-reference dependency
(`agentseam @ git+…` or an archive URL), so:

1. Release agentseam first, with a version that carries main's adapters, and confirm
   PyPI serves it.
2. In chock, replace the direct-reference pin with that released version in
   `pyproject.toml` and every requirements file (a normal owner-merged PR).
3. Only then build the chock release candidate. Record the agentseam commit of that
   release under `inputs.agentseam` in the manifest.

Then the prepare phase (plan/release-1.md): version files and pins in every train repo,
each through a normal PR, then the manifest PR adding `releases/X.Y.Z.yaml` here.

## Dry run

1. Actions → Release → Run workflow, on `main`. Enter `version: X.Y.Z`, leave
   `dry_run` ticked.
2. The `Check and plan` job runs `check.py releases/X.Y.Z.yaml --version X.Y.Z --plan`.
   It needs no approval and no credentials beyond the job's read-only token.
3. On a mismatch it prints `REFUSED: N mismatch(es)` and every one of them. Fix each in
   its repo, update the manifest by PR, and dry-run again.
4. On success it prints `OK` and the plan: each tag it would create, the PyPI wait,
   the `release` branch move, the site check and the GitHub release. Nothing changes.

Locally, the same check: `pip install --require-hashes -r requirements/release.txt`,
then `python check.py releases/X.Y.Z.yaml --version X.Y.Z --plan`. Set `GITHUB_TOKEN`
in the environment only if the API rate limit bites; the check never prints it.

## Real run

1. Run the workflow on `main` with `version: X.Y.Z` and `dry_run` unticked.
2. `Check and plan` runs again. Then the `release` job waits for your approval of the
   `release` environment.
3. After approval it mints a short-lived chock-release-bot token and runs, stopping at
   the first failure:
   - `release.py tag`: re-runs the full check, then tags every train repo `vX.Y.Z`
     at its manifest commit (existing tags at the same commit are skipped).
   - `release.py pypi`: the chock tag starts chock's own `release.yml` (guard, then the
     `pypi` job in chock's `pypi` environment, which you approve there too). This step
     waits up to an hour for PyPI to serve `chock==X.Y.Z` as one sdist and one wheel,
     and prints their sha256.
   - `release.py site`: moves `chock-site`'s `release` branch to the manifest commit,
     fast-forward only. Cloudflare Pages builds production from it.
   - `release.py verify`: waits up to 30 minutes for `<site>/version.json` to report
     the manifest's version, engine and catalog.
   - `release.py summary`: writes the summary (tags, inputs, PyPI URL, site URL, the
     manifest) to the job summary.
4. `publish-summary` (approve `release` again) creates the GitHub release `vX.Y.Z` in
   chock-release with that summary.

Steps after `tag` re-run the check without check 2, because the new tags start check
runs of their own on the same commits.

### Re-running after a partial failure

Re-run the whole workflow with the same version. Tags already at the manifest commits
pass checks 8 and 9 and are skipped. If a tag-triggered run left a failed check on a
named commit, check 2 refuses: find out why before going further, and if anything was
published, go to Rollback.

## Rollback

- **Site:** in Cloudflare Pages, open the project's deployments and roll back
  production to the previous deployment. To make it stick, move `chock-site`'s
  `release` branch back to the previous release commit (the owner, by hand; the
  workflow only moves it forward).
- **PyPI:** a published version cannot be replaced. Yank it on PyPI if it is harmful,
  then ship a patch release `X.Y.(Z+1)` through the full process (prepare PRs, manifest,
  dry run, real run). **Never delete or move a `vX.Y.Z` tag, and never re-tag.**
- **Tags without a PyPI release** (the run stopped before PyPI published): leave the
  tags, fix the cause, and either re-run the same version (if the tagged commits are
  still right) or release the next patch version.
- **GitHub release in chock-release:** edit its notes to say it was superseded; the
  patch release gets its own.

## Changing this repo

Every change to this repo is an owner-merged PR. CI runs ruff, pytest, actionlint and
zizmor. Workflow actions stay pinned by full commit SHA, with `permissions: {}` at the
top and the minimum granted per job.
