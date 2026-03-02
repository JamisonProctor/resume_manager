import os
from dotenv import load_dotenv
from pathlib import Path
import sys
import argparse
from openai import OpenAI
from utilities import cleanup_ats_files, ensure_ats_report_exists, ensure_log_exists
from export_dataset import export_applications_csv

load_dotenv()

# --- paths ---
data_path_str = os.getenv("DATA_PATH")
if not data_path_str:
    raise RuntimeError("DATA_PATH is not set. Add it to .env (e.g., DATA_PATH=/Users/.../Resume)")

data_path = Path(data_path_str).expanduser().resolve()
if not data_path.exists():
    raise RuntimeError(f"DATA_PATH does not exist: {data_path}")

# --- prompt file ---
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "ats_eval.md"
ATS_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

# --- OpenAI config ---
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not set in .env")

model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Some models (especially reasoning models) can take longer; keep a finite timeout to avoid "hangs".
client = OpenAI(api_key=api_key, timeout=180.0, max_retries=0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume manager")
    parser.add_argument(
        "--folder",
        help="Run against a single job subfolder (name under DATA_PATH or a full/relative path).",
    )
    parser.add_argument(
        "--cleanup-ats",
        action="store_true",
        help="Delete ATS artifacts (ats_report.json, ats_report_raw.txt, ats_error.log) in targeted folder(s).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without deleting (use with --cleanup-ats).",
    )
    parser.add_argument("--verbose", action="store_true", help="Print full error details.")
    args = parser.parse_args()

    if args.folder:
        candidate = Path(args.folder).expanduser()
        subdir = candidate if candidate.is_absolute() else (data_path / candidate)
        subdir = subdir.resolve()
        if not subdir.exists() or not subdir.is_dir():
            raise RuntimeError(f"--folder is not a directory: {subdir}")
        subdirs = [subdir]
    else:
        subdirs = sorted(p for p in data_path.iterdir() if p.is_dir())

    if args.cleanup_ats:
        total = len(subdirs)
        folders_touched = 0
        files_deleted = 0

        for subdir in subdirs:
            deleted = cleanup_ats_files(subdir, dry_run=args.dry_run)
            if deleted:
                folders_touched += 1
                files_deleted += len(deleted)

        action = "matched" if args.dry_run else "deleted"
        print(f"ATS cleanup complete. folders_touched={folders_touched}/{total} files_{action}={files_deleted}")
        return

    missing_jd: set[Path] = set()
    logs_initialized = 0

    ats_created = 0
    ats_skipped = 0
    ats_missing_jd = 0
    ats_missing_pdf = 0
    ats_errors = 0

    total = len(subdirs)
    for idx, subdir in enumerate(subdirs, start=1):
        try:
            log_created = ensure_log_exists(subdir)
            log_status = "created" if log_created else "skip"
            if log_created:
                logs_initialized += 1
        except FileNotFoundError:
            log_status = "missing_jd"
            missing_jd.add(subdir)

        ats_status = ensure_ats_report_exists(subdir, client, model, ATS_PROMPT, verbose=args.verbose)
        if ats_status == "created":
            ats_created += 1
        elif ats_status == "skipped":
            ats_skipped += 1
        elif ats_status == "error_missing_jd":
            ats_missing_jd += 1
            missing_jd.add(subdir)
        elif ats_status == "error_missing_pdf":
            ats_missing_pdf += 1
        elif ats_status == "error_openai":
            ats_errors += 1

        print(f"[{idx}/{total}] {subdir.name}: log={log_status} ats={ats_status}", flush=True)

    print(
        "Done."
        f" logs_initialized={logs_initialized}"
        f" ats_created={ats_created}"
        f" ats_skipped={ats_skipped}"
        f" ats_missing_jd={ats_missing_jd}"
        f" ats_missing_pdf={ats_missing_pdf}"
        f" ats_errors={ats_errors}"
    )

    if missing_jd:
        print(f"Missing jd.md folders: {len(missing_jd)}", file=sys.stderr)
        for p in sorted(missing_jd):
            print(f"- {p}", file=sys.stderr)

    # Update aggregate CSV after each pass.
    try:
        csv_path = data_path / "applications.csv"
        rows = export_applications_csv(data_path, csv_path)
        print(f"CSV: {csv_path} rows={rows}")
    except Exception as exc:
        print(f"CSV export failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
