import csv
import json
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from utilities import parse_log_md, summarize_log


def _read_json(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _get(obj: dict | None, *keys: str) -> object:
    cur: object = obj or {}
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _infer_job_title(folder: Path, ats_report: dict | None) -> str:
    title = str((ats_report or {}).get("job_title") or "").strip()
    if title:
        return title

    # Backward compatibility with older ATS schemas.
    title = str(_get(ats_report, "job", "job_title") or "").strip()
    if title:
        return title

    jd_path = folder / "jd.md"
    if jd_path.exists():
        try:
            for raw_line in jd_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    heading = line.lstrip("#").strip()
                    if heading:
                        return heading
                lower = line.casefold()
                for prefix in ("job title:", "role:", "position:"):
                    if lower.startswith(prefix):
                        value = line.split(":", 1)[1].strip()
                        if value:
                            return value
        except Exception:
            pass

    return folder.name


def _count_requirements(ats_report: dict | None) -> tuple[str, str, str]:
    report = ats_report or {}

    reqs = report.get("requirements")
    if isinstance(reqs, list):
        met = partial = missing = 0
        for item in reqs:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().casefold()
            if status == "met":
                met += 1
            elif status == "partial":
                partial += 1
            elif status == "missing":
                missing += 1
        return (str(met), str(partial), str(missing))

    # Backward compatibility with older ATS schemas.
    must_haves = _get(ats_report, "ats_match", "must_haves")
    if not isinstance(must_haves, list):
        return ("", "", "")

    met = partial = missing = 0
    for item in must_haves:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().casefold()
        if status == "met":
            met += 1
        elif status == "partial":
            partial += 1
        elif status == "missing":
            missing += 1

    return (str(met), str(partial), str(missing))


def _join_list(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            parts.append(s)
    return "; ".join(parts)


def export_applications_csv(data_path: Path, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    columns = [
        "log_applied_date",
        "company",
        "job_title",
        "location",
        "work_mode",
        "employment_type",
        "resume_pdf_filename",
        "rejection_likelihood",
        "screen_out_flags",
        "top_gaps",
        "top_strengths",
        "requirements_met",
        "requirements_partial",
        "requirements_missing",
        "log_first_response_date",
        "log_last_event_date",
        "log_current_status",
        "log_days_to_first_response",
        "log_days_since_applied",
        "log_days_since_last_event",
    ]

    today = date.today()
    rows: list[dict] = []

    for folder in sorted(p for p in data_path.iterdir() if p.is_dir()):
        ats_path = folder / "ats_report.json"
        ats_report = _read_json(ats_path) if ats_path.exists() else None

        log_path = folder / "log.md"
        entries = parse_log_md(log_path) if log_path.exists() else []
        log_summary = summarize_log(entries, today) if entries else {}

        met, partial, missing = _count_requirements(ats_report)

        row = {c: "" for c in columns}
        row["log_applied_date"] = str(log_summary.get("applied_date") or "")
        row["log_first_response_date"] = str(log_summary.get("first_response_date") or "")
        row["log_last_event_date"] = str(log_summary.get("last_event_date") or "")
        row["log_current_status"] = str(log_summary.get("current_status") or "")
        row["log_days_to_first_response"] = (
            "" if log_summary.get("days_to_first_response") is None else str(log_summary["days_to_first_response"])
        )
        row["log_days_since_applied"] = (
            "" if log_summary.get("days_since_applied") is None else str(log_summary["days_since_applied"])
        )
        row["log_days_since_last_event"] = (
            "" if log_summary.get("days_since_last_event") is None else str(log_summary["days_since_last_event"])
        )

        company_name = str((ats_report or {}).get("company") or "").strip()
        if not company_name:
            company_name = str(_get(ats_report, "job", "company_name") or "").strip()
        row["company"] = company_name if company_name else folder.name
        row["job_title"] = _infer_job_title(folder, ats_report)
        row["location"] = str((ats_report or {}).get("location") or "")
        row["work_mode"] = str((ats_report or {}).get("work_mode") or "")
        row["employment_type"] = str((ats_report or {}).get("employment_type") or "")

        row["resume_pdf_filename"] = str(_get(ats_report, "meta", "resume_pdf_filename") or "")
        row["rejection_likelihood"] = str((ats_report or {}).get("rejection_likelihood") or "")
        row["screen_out_flags"] = _join_list((ats_report or {}).get("screen_out_flags"))
        row["top_gaps"] = _join_list((ats_report or {}).get("top_gaps"))
        row["top_strengths"] = _join_list((ats_report or {}).get("top_strengths"))

        row["requirements_met"] = met
        row["requirements_partial"] = partial
        row["requirements_missing"] = missing

        rows.append(row)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return len(rows)


def main() -> None:
    load_dotenv()
    data_path_str = os.getenv("DATA_PATH")
    if not data_path_str:
        raise RuntimeError("DATA_PATH is not set. Add it to .env (e.g., DATA_PATH=/Users/.../job_hunt)")

    data_path = Path(data_path_str).expanduser().resolve()
    if not data_path.exists():
        raise RuntimeError(f"DATA_PATH does not exist: {data_path}")

    # Store the aggregate CSV alongside the DATA_PATH directory for easy access.
    out_path = data_path / "applications.csv"
    count = export_applications_csv(data_path, out_path)
    print(f"{out_path} rows={count}")


if __name__ == "__main__":
    main()
