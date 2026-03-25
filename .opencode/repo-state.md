# Repo State

## Current snapshot

- Date captured: 2026-03-25
- Active branch during capture: `master`
- Working tree during capture: clean
- Repository type: small Python CLI repo
- Application packaging: none; single script plus assets

## Current architecture status

- Main implementation file: `svg_to_paint_by_numbers_pdf.py`
- Test suite status: no dedicated automated test suite found
- Build system status: no package build metadata found
- Runtime dependency source: `requirements.txt`
- User documentation source: `README.md`
- Memory automation: `scripts/generate_python_map.py` keeps the AST inventory in `.opencode/python-map.md` current

## Main capabilities currently implemented

- Convert one SVG into one A4 paint-by-numbers PDF
- Convert a directory of SVG files in batch mode
- Include stroke-only geometry as colorable zones when requested
- Normalize the nearest dark color to pure black
- Apply mystery/obfuscation splitting from a second SVG pattern
- Render labels with collision avoidance and fallback placement
- Emit progress and ETA information in the CLI
- Emit per-output profiling logs in `--test` mode

## Known structural realities

- The repo is functionally a monolith. Most future edits will touch `svg_to_paint_by_numbers_pdf.py`.
- There is no formal unit/integration test harness, so manual CLI verification is important after behavior changes.
- The `.resources/` folder contains local/generated material and should not be mistaken for active application structure.
- Generated PDFs already exist in `output/` and historical batch directories; they are examples/artifacts, not source.

## Operational assumptions

- Python 3.10+ is expected by the current README.
- A working `Montserrat-Regular.ttf` file is required unless the user overrides `--font-path`.
- Typical source SVGs are expected to be pure vector files, not raster-heavy or effect-heavy compositions.
- The script expects supported element types such as `path`, `rect`, `circle`, `ellipse`, `polygon`, `polyline`, and `line`.

## Known gaps and debt

- No automated regression tests for geometry, labels, rendering, or CLI behavior.
- No module split yet, despite the main script size.
- No packaging metadata, so install and execution are manual.
- No documented developer automation for keeping project maps synchronized; this `.opencode` folder now fills that role.
- The Python symbol inventory is now partially automated via `scripts/generate_python_map.py`, but the narrative explanations are still hand-maintained.

## Recent history context

Recent commits seen during capture:
- `5043155` Refine CLI help output and ignore local env
- `4cfc4b6` Merge pull request #1 from spidey000/ofuscated
- `f4fa963` Use fixed 3pt labels to cut placement time
- `32996db` Add batch progress and remaining ETA
- `d78c9f8` Add live CLI progress and ETA for PDF generation

This suggests recent work focused on CLI clarity, performance, mystery/obfuscated behavior, and progress visibility.

## Files agents should inspect first in future sessions

1. `svg_to_paint_by_numbers_pdf.py`
2. `README.md`
3. `.opencode/python-map.md`
4. `.opencode/project-map.md`
5. `.opencode/runbook.md`

## When this file must be updated

Update this file when any of the following changes:
- the repo shifts from single-script to multi-module,
- a new entrypoint or new persistent folder is added,
- automated tests or packaging metadata are introduced,
- the dominant workflow or major feature set changes,
- or the repo state described here is no longer accurate.
