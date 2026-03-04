import os
import shutil
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

import db
from utilities import parse_log_md


REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "resume_manager.sqlite3"


def _slug(s: str) -> str:
    out = []
    for ch in s.strip().lower():
        if ch.isalnum():
            out.append(ch)
        else:
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "job"


def _best_effort_company_title(src_folder: Path) -> tuple[str | None, str | None]:
    ats_path = src_folder / "ats_report.json"
    if ats_path.exists():
        try:
            import json

            obj = json.loads(ats_path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                company = obj.get("company")
                title = obj.get("job_title")
                if isinstance(company, str) and company.strip() and isinstance(title, str) and title.strip():
                    return company.strip(), title.strip()

                extracted = obj.get("extracted")
                if isinstance(extracted, dict):
                    c2 = extracted.get("company_name")
                    t2 = extracted.get("job_title")
                    if isinstance(c2, str) and c2.strip():
                        company = c2.strip()
                    if isinstance(t2, str) and t2.strip():
                        title = t2.strip()
                    if company or title:
                        return company, title
        except Exception:
            pass

    jd_path = src_folder / "jd.md"
    if jd_path.exists():
        try:
            for line in jd_path.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    heading = s.lstrip("#").strip()
                    if heading:
                        return None, heading
        except Exception:
            pass

    return None, None


def _copy_selected_files(src: Path, dst: Path) -> int:
    copied = 0
    for p in sorted(src.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if name in ("jd.md", "ats_report.json"):
            shutil.copy2(p, dst / name)
            copied += 1
            continue
        if p.suffix.lower() in (".pdf", ".pages"):
            shutil.copy2(p, dst / name)
            copied += 1
            continue
    return copied


def _map_event_type(t: str) -> str:
    if t in ("not_moving_forward", "rejection", "screened_out"):
        return "rejection"
    if t in ("screening", "interview", "update", "offer", "applied"):
        return t
    return "note"


def main() -> None:
    load_dotenv()
    source_str = os.getenv("DATA_PATH")
    if not source_str:
        raise RuntimeError("DATA_PATH must point to your original job_hunt folder for migration.")

    source_root = Path(source_str).expanduser().resolve()
    if not source_root.exists():
        raise RuntimeError(f"DATA_PATH does not exist: {source_root}")

    conn = db.connect(DB_PATH)
    db.init_db(conn)

    jobs_root = Path(db.get_setting(conn, "artifacts_root") or (REPO_ROOT / "jobs")).expanduser().resolve()
    jobs_root.mkdir(parents=True, exist_ok=True)
    db.set_setting(conn, "artifacts_root", str(jobs_root))

    created = 0
    skipped = 0
    events_added = 0
    files_copied = 0

    folders = sorted(p for p in source_root.iterdir() if p.is_dir())
    total = len(folders)

    for idx, src_folder in enumerate(folders, start=1):
        existing_id = db.get_job_id_by_source_folder(conn, str(src_folder))
        if existing_id is not None:
            skipped += 1
            print(f"[{idx}/{total}] skip {src_folder.name}")
            continue

        company, job_title = _best_effort_company_title(src_folder)
        base = f"{_slug(company or src_folder.name)}__{_slug(job_title or 'job')}"
        dst = jobs_root / base
        n = 1
        while dst.exists():
            n += 1
            dst = jobs_root / f"{base}_{n}"
        dst.mkdir(parents=True, exist_ok=False)

        jd_path = src_folder / "jd.md"
        jd_text = jd_path.read_text(encoding="utf-8", errors="replace") if jd_path.exists() else ""
        if jd_text:
            (dst / "jd.md").write_text(jd_text.rstrip() + "\n", encoding="utf-8")

        files_copied += _copy_selected_files(src_folder, dst)

        job_id = db.create_job(
            conn,
            company=company,
            job_title=job_title,
            job_url=None,
            jd_text=jd_text,
            artifact_dir=str(dst),
            source_folder=str(src_folder),
        )
        created += 1

        log_path = src_folder / "log.md"
        if log_path.exists():
            for entry in parse_log_md(log_path):
                event_date = entry.get("date") or date.today().isoformat()
                event_type = _map_event_type(str(entry.get("event_type") or "note"))
                note = str(entry.get("notes") or "").strip() or None
                raw_input = str(entry.get("raw") or "").strip() or None
                db.add_event(
                    conn,
                    job_id=job_id,
                    event_date=event_date,
                    event_type=event_type,
                    note=note,
                    raw_input=raw_input,
                )
                events_added += 1

        print(f"[{idx}/{total}] imported {src_folder.name} -> {dst.name}")

    print(
        f"Done. created={created} skipped={skipped} events_added={events_added} files_copied={files_copied} jobs_root={jobs_root}"
    )


if __name__ == "__main__":
    main()

