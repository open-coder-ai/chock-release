# Candidates
`<version>-rc.<n>.yaml` lists, per train repo, the PR numbers to merge into its `rc` branch, in order.
`python rc.py assemble <file>` builds `rc`; `python check.py --candidate <file>` checks the `rc` heads.
A candidate is not a manifest: `release.py` refuses it, and nothing is tagged or published.
A new round is a new file (`-rc.2`). See RUNBOOK.md, "Candidate".
