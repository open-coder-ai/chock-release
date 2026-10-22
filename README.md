# chock-release

The control repository of the chock release train: one version, every train repo tagged
`vX.Y.Z` at the commit a manifest names, chock published to PyPI, the site's `release`
branch moved. Design: org-plan `plan/release-1.md`.

| Path | What it is |
|---|---|
| `RUNBOOK.md` | How the owner runs a release: dry run, real run, re-run, rollback. Start there. |
| `releases/X.Y.Z.yaml` | The manifest of one release (`releases/*.example.yaml` shows the shape). |
| `check.py` | The consistency check across the train. Lists every mismatch and exits 1. |
| `rc.py` | Assembles the `rc` branches of a release candidate (`candidates/*.yaml`); `check.py --candidate` checks them. Nothing is tagged. |
| `release.py` | The dry-run build and the release steps. Each re-runs the check first. |
| `.github/workflows/release.yml` | The owner-triggered workflow, dry run by default. |
| `tests/` | One test per refusal, against a fake GitHub. No test touches the network. |

Only the owner runs the release workflow. Agents never tag, release or publish.

## License

Apache-2.0, see [LICENSE](LICENSE).
