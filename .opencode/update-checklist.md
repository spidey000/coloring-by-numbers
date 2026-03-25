# Update Checklist

Use this checklist after each meaningful repo change.

## Always review

- Does the code change alter actual behavior or only formatting?
- Does a future agent need new repo context to answer correctly?
- Did any new generated or local-only files appear that should be ignored?

## Update `python-map.md` when

- a Python function is added, removed, renamed, or materially changed
- a class or dataclass changes fields or purpose
- a new pipeline stage appears or an old one is removed
- return values, error behavior, or major invariants change
- after those changes, run `python scripts/generate_python_map.py --write`

## Update `project-map.md` when

- a new source file or source folder is added
- assets move or new runtime assets become required
- entrypoints change
- cross-file relationships change
- the repo stops being effectively single-module

## Update `repo-state.md` when

- current workflow changes
- the branch snapshot or major recent-history summary should be refreshed
- tests, packaging, CI, Docker, or automation are introduced
- known debt or architecture status materially changes

## Update `runbook.md` when

- install steps change
- new common commands are added
- command flags change in a user-visible way
- output locations or log behavior changes

## Update `conventions.md` when

- user-facing language changes
- rendering rules or label rules change
- project structure conventions change
- safety expectations change

## Update ignore files when

- new generated outputs are created consistently
- new local state folders appear
- new cache or log files are introduced
- Docker build context should exclude new non-runtime files

## Before finishing a task

- Re-read the touched code paths.
- Re-read the relevant `.opencode` files.
- Make sure the memory files describe the repo as it exists now, not as it existed before the edit.
