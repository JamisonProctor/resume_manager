# Repository Guidelines

## Project Overview
This repository is a small Python CLI tool for managing and evaluating a job-hunt folder of job applications. It reads configuration from `.env`, scans job subfolders under `DATA_PATH`, and runs repeatable “maintenance” tasks (e.g., initializing `log.md`).

## Project Structure & Module Organization
- `main.py`: orchestration only (directory loop, simple `if` guards, reporting). Keep this file easy to scan.
- `utilities.py`: reusable folder/task logic (e.g., `ensure_log_exists(subdir)`).
- `prompts/`: Markdown prompt templates (currently `prompts/ats_eval.md`).
- `venv/`: local virtual environment (do not rely on this being present for other contributors).

Expected data layout (outside this repo):
- `$DATA_PATH/<job_folder>/jd.md`
- `$DATA_PATH/<job_folder>/log.md`

## Build, Test, and Development Commands
- Create env + install deps:
  - `python -m venv venv`
  - `source venv/bin/activate`
  - `pip install openai python-dotenv`
- Run locally:
  - `python main.py` (scans `DATA_PATH` and runs tasks)
- Quick sanity check (no test suite yet):
  - `python -m py_compile main.py utilities.py`

## Configuration & Security
- Use a local `.env` file with:
  - `DATA_PATH=/absolute/path/to/job_hunt`
  - `OPENAI_API_KEY=...`
  - `OPENAI_MODEL=...` (optional)
- Never commit secrets. Prefer keeping `.env` local and adding new required keys to documentation when introduced.

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints where practical, prefer `pathlib.Path` over string paths.
- Naming: `snake_case` for functions/vars; keep task functions small and single-purpose.
- Design: add new per-folder actions as functions in `utilities.py` (or a future `tasks/` module) and call them from `main.py`’s loop.

## Testing Guidelines
No automated tests are configured yet. If you add tests, use `pytest` and place them under `tests/` with filenames like `test_*.py`.

## Commit & Pull Request Guidelines
- Commit messages: short, imperative summaries (e.g., “Initialize missing logs”, “Add ATS prompt loader”).
- PRs: include a brief description, how to run/verify (`python main.py`), and any expected output changes.
