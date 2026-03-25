# Agent Guide

## Purpose

This repository converts pure vector SVG artwork into A4 paint-by-numbers PDF output.
The application is currently a single Python CLI script supported by a small set of assets and folders.

Primary entrypoint:
- `svg_to_paint_by_numbers_pdf.py`

Primary human-facing docs:
- `README.md`

Persistent agent memory files in this folder:
- `project-map.md`
- `python-map.md`
- `repo-state.md`
- `runbook.md`
- `conventions.md`
- `update-checklist.md`

Automation support:
- `scripts/generate_python_map.py`

## What the agent should know first

- The repo is not a packaged Python application. There is no `pyproject.toml`, `setup.py`, or `tests/` folder.
- The codebase is centered on one large script that contains CLI parsing, SVG parsing, geometry, label placement, PDF rendering, diagnostics, and batch execution.
- `README.md` is written in Spanish and describes the intended CLI behavior.
- Generated PDFs and logs are disposable artifacts, not source code.
- `.env` exists in the repo root and must not be inspected or committed unless the user explicitly asks for that.

## Source of truth order

When answering future prompts, use this order:
1. Current code in `svg_to_paint_by_numbers_pdf.py`
2. `README.md`
3. `.opencode/python-map.md`
4. `.opencode/project-map.md`
5. `.opencode/repo-state.md`

If any of these disagree, trust the code first and update the memory files.

## Current pipeline

The main conversion flow is:
1. Parse CLI args and resolve mode
2. Read SVG and collect supported drawable shapes
3. Build colorable zones from fills and optional strokes
4. Normalize the nearest dark color to pure black
5. Optionally load and apply a mystery pattern to fragment zones
6. Build a sorted palette and assign one-character labels
7. Render outline, optional mystery boundaries, labels, legend, and final PDF
8. Emit profiling logs when `--test` is enabled

## Folder semantics

- `inputs/`: recommended source folder for batch runs
- `output/`: generated output for single-file runs
- `pdf-output-*`: generated batch output folders
- `patterns/`: SVG assets used for mystery/obfuscation splitting
- `fonts/`: required font assets, especially `Montserrat-Regular.ttf`
- `.resources/`: local/generated resources and historical outputs

## Required maintenance behavior

After every meaningful code change, keep this folder in sync.

Update rules:
- Update `python-map.md` when a Python function, class, argument, return shape, stage, or responsibility changes.
- Regenerate the AST inventory block in `python-map.md` with `python scripts/generate_python_map.py --write` after top-level Python symbol changes.
- Update `project-map.md` when folders, assets, runtime flow, or cross-file relationships change.
- Update `repo-state.md` when the repo architecture, workflow, technical debt summary, or major recent changes shift.
- Update `runbook.md` when a common command, install step, or expected output path changes.
- Update `conventions.md` when the repo adopts a new coding or operational convention.
- Update `update-checklist.md` if the maintenance process itself changes.

## Editing guidance for future prompts

- Preserve the Spanish CLI tone already used in `README.md` and user-visible command help.
- Prefer small, targeted edits because the application logic is concentrated in one file.
- Avoid treating generated PDFs, logs, or `.pyc` files as part of the implementation.
- If modifying label placement, rendering, or geometry, also refresh the maps because those areas are dense and easy to desynchronize.
- If adding new Python modules later, convert `python-map.md` from a single-file map into a per-module map rather than keeping everything under one heading.

## Quick operating commands

- Install deps: `python -m pip install -r requirements.txt`
- Single SVG: `python svg_to_paint_by_numbers_pdf.py numbers.svg`
- Mystery mode: `python svg_to_paint_by_numbers_pdf.py numbers.svg --mystery-pattern patterns/pattern.svg`
- Batch mode: `python svg_to_paint_by_numbers_pdf.py inputs`
- CLI help: `python svg_to_paint_by_numbers_pdf.py --help`
- Refresh AST inventory: `python scripts/generate_python_map.py --write`

## Definition of done for repo-aware changes

A change is not fully done until:
- the code is updated,
- relevant `.opencode` files are updated,
- generated artifacts are left out of source edits unless intentionally produced for verification,
- and the documented behavior still matches the implementation.
