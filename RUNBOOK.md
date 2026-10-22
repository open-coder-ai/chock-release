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
| `check.py` | The consistency check (checks 1–9 below). Lists every mismatch, then exits 1. `--candidate <file>` runs it against the `rc` heads. |
| `rc.py` | Assembles, pins, opens the rc draft PRs of, and reports on the `rc` branches of a candidate (see "Candidate"). |
| `candidates/<version>-rc.<n>.yaml` | A candidate: the PR numbers to merge into each repo's `rc`. Not a manifest; `release.py` refuses it. |
| `release.py` | The dry-run build (step 1) and release steps 2–6. Each step re-runs the check before it does anything. |
| `.github/workflows/release.yml` | `workflow_dispatch` with `version` and `dry_run` (default true). |

The train: `chock`, `chock-catalog`, `chock-{claude,cursor,codex,copilot,devin}-plugins`,
`chock-site`, `chock-quickstart`, `chock-example`. Recorded but never tagged: `agentseam`.

## What the check refuses

1. A train repo missing, an extra repo, an unknown key in an entry, a duplicate key
   anywhere in the manifest, a commit that is not 40 lowercase hex, the all-zero
   placeholder, or a commit that is not on the repo's default branch.
2. CI not green on a named commit, `inputs.agentseam` included. The check reads one
   workflow per repo (the table below), on the default branch at that commit: the newest
   run must have completed with success, and every required job in it must have
   completed with success. A required job the run skipped is not green. A failing
   combined commit status also refuses. Other check runs on the commit (the catalog's
   nightly sweep and drift, a tag-triggered release run, code scanning) are not read.
3. `chock`'s `pyproject.toml` `[project].version` (that is `chock.__version__`) is not
   `X.Y.Z`; any dependency, extras included, is a direct reference (`name @ url`), which
   PyPI rejects; agentseam is not pinned `agentseam==<version>`; or that version is not
   what agentseam's `pyproject.toml` reports at `inputs.agentseam`.
4. `chock-catalog`'s `.framework-ref` is not the engine commit, or its `VERSION` is not `X.Y.Z`.
5. A plugin repo's `generated_from.yaml` (`generated_from: {catalog, engine}`) does not
   name the manifest's catalog and engine commits.
6. `chock-site`'s `.framework-ref` or `.catalog-ref` differs from the manifest, its
   `data/version.json` `version` is not `X.Y.Z`, or its `data/agentseam-source/source.json`
   `agentseam_ref` is not `inputs.agentseam`.
7. `chock-quickstart` or `chock-example` has no chock pin in `README.md`, `docs/README.md`,
   `requirements.txt` or `pyproject.toml`; a pin there is neither the engine commit nor
   `chock==X.Y.Z` (an archive of a branch or tag counts as a wrong commit); a chock
   requirement uses a range operator; or a `pip install chock` line names no version.
8. and 9. A `vX.Y.Z` tag already exists at a different commit. A tag at the manifest
   commit passes, so a re-run after a partial release continues where it stopped.

The required workflow and jobs per repo (`check.REQUIRED_WORKFLOWS`; a matrix job matches
its name prefix):

| Repo | Workflow | Required jobs |
|---|---|---|
| chock | `ci.yml` | `validate`, `acceptance (tier 1)` |
| chock-catalog | `ci.yml` | `policies` |
| chock-*-plugins | `generated-only.yml` | `verify` |
| chock-site | `ci.yml` | `lint`, `unit`, `e2e` |
| chock-quickstart, chock-example | `ci.yml` | `check` |
| agentseam (input) | `ci.yml` | `lint`, `test` |

Two consequences:

- **Plugin repos.** `publish.yml` pushes with the built-in token, which starts no workflow
  run, so the published commit has no CI until you dispatch `generated-only.yml` on
  `main` in each plugin repo. Do that after every publish, before the dry run.
- **quickstart and example.** They have no workflow until REL-1c adds `ci.yml` with a
  `check` job. Until then the check refuses them, which is the intended answer for an
  untested commit.

A train repo that is private reads as "repository not found", so the check refuses
until `chock-site` is public (see Prerequisites).

A GitHub API failure (anything but 404) is reported once, as the API error, and the
check refuses; it never turns into a false "not found".

## Prerequisites (owner, once; plan/release-1.md "Owner setup checklist")

- **The `release` environment** in chock-release, with:
  - **deployment branches limited to `main`**;
  - **a required reviewer (the owner)**;
  - its secrets: `RELEASE_APP_CLIENT_ID` (the chock-release-bot app's client ID) and
    `RELEASE_APP_PRIVATE_KEY`;
  - optionally the variable `RELEASE_SITE_URL` (default `https://chock-site.pages.dev`;
    set it to `https://chock.sh` after launch).

  The workflow's own `github.ref == 'refs/heads/main'` condition is not enough on its
  own: a dispatch from another branch runs that branch's copy of the workflow file,
  which can drop the condition. Only the environment's branch rule and reviewer keep
  the credentials away from a non-main branch.
- **chock-release-bot**, with Contents read/write, installed only on the train repos.
- **Every train repo public, `chock-site` included.** The check reads CI state and
  files through the public API, so a private repo reads as "repository not found" and
  the check refuses. Making `chock-site` public is setup checklist item 2.
- After this repo's bootstrap PR merges, remove chock-release from the Claude app's
  repository access.
- **A GitHub ruleset on `v*` tags in every train repo**, with no deletion and no update
  (no move), so a tag is the frozen record. The owner creates it; nothing here does.

## Before the release: agentseam first

chock depends on agentseam. PyPI rejects a direct-reference dependency
(`agentseam @ git+…` or an archive URL), so:

1. Release agentseam first, with a version that carries main's adapters, and confirm
   PyPI serves it.
2. In chock, replace the direct-reference pin with `agentseam==<that version>` in
   `pyproject.toml` and every requirements file (a normal owner-merged PR). Check 3
   refuses a manifest whose chock still carries the direct reference, and the dry-run
   build refuses the built wheel for the same reason, so a forgotten swap stops before
   any tag.
3. Only then build the chock release candidate. Record the agentseam commit of that
   release under `inputs.agentseam` in the manifest: check 3 compares its
   `pyproject.toml` version with chock's pin, and check 6 compares it with the site's
   `data/agentseam-source/source.json`.

Then the prepare phase (plan/release-1.md): version files and pins in every train repo,
each through a normal PR, then the manifest PR adding `releases/X.Y.Z.yaml` here.

## Candidate

A candidate is a release candidate the owner can test end to end before anything is
tagged. Design: org-plan `plan/internal-release.md`. Nothing in a candidate is tagged,
published or moved: no tag, no PyPI, no `release` branch move.

A round:

1. `candidates/X.Y.Z-rc.N.yaml` lists, per train repo, the PR numbers to merge into that
   repo's `rc` branch, in order (`[]` for none), and `base: main`. A fix to a draft PR
   or a new PR means a new round: the next file, `-rc.(N+1)`. The one exception is
   `chock-catalog`, which is merge-first (see "The catalog" below): its list names PRs that must
   already be merged to its `main`, not PRs to assemble.
2. `python rc.py assemble candidates/X.Y.Z-rc.N.yaml --dry-run` prints the plan and
   touches nothing. Without `--dry-run`, each repo is cloned; `rc` is created from
   `origin/main` only when it does not exist (otherwise it is checked out and
   `origin/main` merged in); each PR is merged from `refs/pull/<n>/head` with a merge
   commit (`--no-ff`); `rc` is pushed fast-forward only, never forced. A PR that is not
   open, a conflict, or a push that is not a fast-forward stops that repo with the repo
   and PR printed and nothing pushed for it; the other repos still run. Commits are
   authored as the owner with a sign-off.
   The candidate file may carry an optional top-level `regenerated:` mapping, repo to path globs
   (`**` crosses directories, `*` stays in one) that the candidate rewrites on `rc` after assembly:
   chock-site's two pins and its recorded engine, catalog and agentseam snapshots. When a PR merge
   conflicts only in paths matching that repo's list, `assemble` takes the PR's version of each
   (a deletion too) and completes the merge commit with the line "conflicts in regenerated paths
   taken from PR #n: <paths>", which it also prints, because `pins` and the regeneration rewrite
   those paths right after. It resolves nothing else: a conflict with any path outside the list, a
   conflict merging `main` into `rc`, and a repo with no entry all refuse as before, naming the
   paths outside the list. Rebuilding `rc` stays the owner's `--reset`.
3. `python rc.py pins candidates/X.Y.Z-rc.N.yaml` adds one commit on chock-site's `rc`
   setting `.catalog-ref` to the catalog's `main` head and `.framework-ref` to the engine's `rc`
   head, so the site build of `rc` is the candidate. The same commit moves the engine commit
   the site hand-keeps elsewhere (read from `data/kinds/engine-commit.txt` and the old
   `.framework-ref`): the whole of `data/kinds/engine-commit.txt` and the `"chock"` entry of
   `pins` in each `data/architecture/*.json` that held it (a diagram pinned at another commit
   stays). Moving pins is not enough: the site records snapshots that its CI checks against
   them. After every pins move, regenerate on `rc` (commands as chock-site documents them; chock
   installed at `.framework-ref`):
   `python -m sitegen.docsource --chock <chock checkout>`, `python -m sitegen.writedoc --record`,
   `python -m sitegen.catalogdoc --catalog <catalog checkout>`, `python -m sitegen.cliexamples --record`,
   `python -m sitegen.agentseamdoc snapshot --agentseam <agentseam checkout>`,
   `python -m sitegen.agentseamdoc run --record`, then
   `python -m sitegen.architecture --check --fetch --clones <folder> --repo chock-site=.`.
   The regeneration commit goes on `rc` only, never on `main`. `pins` prints this list after
   its commit; it does not run it.

   **The catalog.** `chock-catalog` is merge-first, not part of the `rc` train. Why: the engine's
   `chock install` refuses a catalog ref that is not on the catalog's `main` ("commit <sha> is not on
   https://github.com/open-coder-ai/chock-catalog's main branch. Nothing was installed."). That is a
   security check and stays; there is no flag around it. A site built from a pin on a catalog `rc`
   head could therefore never install anything. What to do: merge the PRs the candidate lists for
   `chock-catalog` to its `main` first ("Create a merge commit"; the catalog's own CI gates them), then
   run `pins` (it pins the catalog's `main` head), then regenerate `data/catalog-source/` on the
   site's `rc` with the `catalogdoc` command above, against that `main` head. Nothing changes for
   users meanwhile: an install carries the catalog ref the site pinned when the selection was built,
   and the live site keeps its pin until promotion. `assemble` and `prs` skip the catalog with one
   line (no `rc` branch is created, merged or pushed for it, and no draft PR), and `--only
   chock-catalog` is a usage error for them; `status` and `check.py --candidate` read its `main`.
4. `python rc.py prs candidates/X.Y.Z-rc.N.yaml` opens, for each repo whose `rc` exists, a
   draft pull request from `rc` to `main` titled `Release candidate X.Y.Z-rc.N: do not merge`,
   so that each repo's CI (which triggers on pull requests and pushes to main, not on a push
   to `rc`) runs on the `rc` head, which is what check 2 reads. The body says it is the
   staging candidate, lists the candidate's PRs it carries and, for chock-site, the two pins.
   When the PR exists, only its body is rewritten (title and draft state stay). A repo with no
   `rc` is skipped with one line, and `chock-catalog` is skipped as merge-first. `--dry-run` reads and prints what it would do. Never merge,
   mark ready or close an rc PR: promotion merges the listed PRs to `main` (the catalog's were
   already merged before `pins`, so promotion does not touch them), and the owner
   closes the rc PRs when deleting the `rc` branches. Run `prs` after `pins`, and again
   after a later round to refresh the bodies. A repo whose `rc` equals `main` (no candidate
   PRs, such as chock-quickstart and chock-example) is skipped with one line, "rc equals
   main, no PR needed"; the candidate check then reads that repo's CI from the runs on `main`
   for the same commit.
5. `python rc.py status candidates/X.Y.Z-rc.N.yaml` (also `assemble`, `prs`) takes `--only <repo>`,
   repeatable, to cover part of the train, for a session whose GitHub scope reaches one
   repo; a repo not in the candidate file is a usage error, and `chock-catalog` is a usage error for
   `assemble` and `prs`. `status` prints each repo's `rc` head, which
   listed PRs it contains, the number of its rc PR (or `none`) with, when the PR exists, one line of its CI from the
   rc head's check runs (`ci: N success, M failure`), and whether `main` has moved under it. For
   `chock-catalog` it prints its `main` head and, per listed PR, `merged` or `NOT merged (open|closed)`
   (no `rc` PR column).
6. `python check.py --candidate candidates/X.Y.Z-rc.N.yaml --plan` is the release check
   against the `rc` heads, by the same code paths; for `chock-catalog` it reads `main` instead
   (its commit is the `main` head, its CI is read on `main`, and a listed catalog PR that is not
   merged is a refusal naming the PR number). Checks 1 (on `rc`, not the default
   branch), 2 and 6a refuse. Checks 3, 4b and 6b, the version files, warn until
   REL-5 lands. Checks 4a, 5 and 7 (the catalog's `.framework-ref`, the plugins'
   `generated_from`, the quickstart and example engine pins) warn too: the owner's
   adopt-framework run, the plugin regeneration (REL-4) and REL-5 set those pins at release
   prep, never `rc.py`; they still refuse in manifest mode. A check 2 "never ran" on `rc` means
   the rc draft PR is missing: run `rc.py prs`. Only the site's
   pins (6a) are moved by `rc.py pins`, so 6a refuses (`.catalog-ref` must equal the catalog's `main` head). Checks 8 and 9 (tags) are skipped. A required job that is red on `rc` and
   also red on the repo's default branch is printed as `known red` and is a warning
   (the plugin repos' `verify`, for example). `main` having moved under `rc` is a
   warning. The plan prints "candidate: nothing is tagged, published or moved".

What agents may do: everything above except `--reset`. An agent assembles, pins, opens the rc draft PRs, reads
status and runs the candidate check; every push to `rc` is a merge commit or a
fast-forward. An agent never tags, publishes, moves `release`, or passes `--reset` or
`--i-am-the-owner`.

The owner's `--reset`: `python rc.py assemble candidates/X.Y.Z-rc.N.yaml --reset
--i-am-the-owner` deletes and recreates `rc` from `main` in every repo (a force
replacement, so it is the owner's alone, run from the laptop). `--reset` without
`--i-am-the-owner` is refused before anything is read. The owner also deletes the `rc`
branches after the release.

`release.py`, the release workflow and `check.py` given a candidate as a manifest all
refuse it outright. A candidate that passes becomes a release only through the manifest
PR (`releases/X.Y.Z.yaml`) naming the commits that reached `main`.

## Dry run

1. Actions → Release → Run workflow, on `main`. Enter `version: X.Y.Z`, leave
   `dry_run` ticked.
2. The `Check, build and plan` job runs `check.py releases/X.Y.Z.yaml --version X.Y.Z --plan`,
   then `release.py build`, which clones chock at its manifest commit, builds its sdist
   and wheel, and refuses what PyPI would refuse (a version other than `X.Y.Z`, a
   direct-reference `Requires-Dist`, anything but one sdist and one wheel). Nothing is
   uploaded. The job needs no approval and no credentials beyond its read-only token,
   which the build's subprocesses never see.
3. On a mismatch it prints `REFUSED: N mismatch(es)` and every one of them. Fix each in
   its repo, update the manifest by PR, and dry-run again.
4. On success it prints `OK`, the built files with their sha256, and the plan: the
   build, each tag it would create, the PyPI wait, the `release` branch move, the site
   check and the GitHub release. Nothing changes.

Locally, the same two commands: `pip install --require-hashes -r requirements/release.txt`,
then `python check.py releases/X.Y.Z.yaml --version X.Y.Z --plan` and
`python release.py build releases/X.Y.Z.yaml --version X.Y.Z`. Set `GITHUB_TOKEN` in
the environment first (for example `GITHUB_TOKEN=$(gh auth token)`): one run makes more
requests than GitHub's unauthenticated limit of 60 per hour allows, so an unauthenticated
run fails with `HTTP 403`. The check never prints the token. The build needs `git` and
network access to PyPI for chock's build backend.

## Real run

1. Run the workflow on `main` with `version: X.Y.Z` and `dry_run` unticked.
2. `Check, build and plan` runs again, the dry-run build included: it is the build before
   tagging. Then the `release` job waits for your approval of the `release` environment.
3. After approval it mints a short-lived chock-release-bot token and runs, stopping at
   the first failure:
   - `release.py tag`: re-runs the full check, then tags every train repo `vX.Y.Z`
     at its manifest commit (existing tags at the same commit are skipped).
   - `release.py pypi`: the chock tag starts chock's own `release.yml` (guard, then the
     `pypi` job in chock's `pypi` environment, which you approve there too). This step
     waits up to an hour for PyPI to serve `chock==X.Y.Z` as one sdist and one wheel,
     each with a sha256, neither yanked, and prints the sha256 values. While PyPI shows
     only one of the two files the upload is still in progress and the step keeps
     polling; any other file set on the version (a third file, an unknown type, a
     yanked file) refuses at once. It does not yet compare the hashes against chock's
     build provenance attestation (a 1.1 follow-up); check them by hand if in doubt.
   - `release.py site`: moves `chock-site`'s `release` branch to the manifest commit,
     fast-forward only. Cloudflare Pages builds production from it.
   - `release.py verify`: waits up to 30 minutes for `<site>/version.json` to report
     the manifest's version, engine and catalog.
   - `release.py summary`: writes the summary (tags, inputs, PyPI URL, site URL, the
     manifest) to the job summary.
4. `publish-summary` creates the GitHub release `vX.Y.Z` in chock-release with that
   summary. It runs only after the approved `release` job and uses only this
   repository's own token, so it asks for no second approval. If the release already
   exists it is left as it is; if a `vX.Y.Z` tag exists here at another commit the job
   refuses rather than move it.

Every step re-runs the full check, check 2 included: the tag-triggered runs that
`vX.Y.Z` starts on the same commits carry the tag as their branch and are not read.

### Re-running after a partial failure

Re-run the whole workflow with the same version. Tags already at the manifest commits
pass checks 8 and 9 and are skipped, and a release summary that already exists is left
alone. If the required workflow on a named commit has a newer failed run on the default
branch, check 2 refuses: find out why before going further, and if anything was
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
top and the minimum granted per job. The requirements files are compiled with
`uv pip compile --universal --python-version 3.12 --generate-hashes`, so the hash-pinned
install works on Linux, macOS and Windows alike.
