import argparse
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

import db


REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "resume_manager.sqlite3"


def _slug(s: str) -> str:
    s = s.strip().casefold()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "job"


def _default_artifacts_root() -> Path:
    return REPO_ROOT / "jobs"


def _default_resumes_root() -> Path:
    return REPO_ROOT / "resumes"


def _ensure_roots(conn) -> tuple[Path, Path]:
    artifacts_root = db.get_setting(conn, "artifacts_root") or str(_default_artifacts_root())
    resumes_root = db.get_setting(conn, "resumes_root") or str(_default_resumes_root())
    return (Path(artifacts_root).expanduser(), Path(resumes_root).expanduser())


def _extract_company_title_llm(client: OpenAI, model: str, jd_text: str) -> tuple[str | None, str | None]:
    # Keep this deterministic to parse: two lines, COMPANY and JOB_TITLE.
    prompt = (
        "Extract company and job title from the following JOB_DESCRIPTION.\n"
        "Return ONLY two lines:\n"
        "COMPANY: <string or null>\n"
        "JOB_TITLE: <string or null>\n\n"
        "JOB_DESCRIPTION:\n"
        + jd_text
    )
    resp = client.responses.create(model=model, input=prompt)
    text = (getattr(resp, "output_text", "") or "").strip()
    company = None
    job_title = None
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().upper()
        v = v.strip()
        if v.casefold() == "null" or not v:
            v_out = None
        else:
            v_out = v
        if k == "COMPANY":
            company = v_out
        elif k == "JOB_TITLE":
            job_title = v_out
    return company, job_title


def _choose_resume(resumes_root: Path, resume_arg: str | None) -> Path:
    resumes_root.mkdir(parents=True, exist_ok=True)
    pages = sorted(p for p in resumes_root.iterdir() if p.is_file() and p.suffix.lower() == ".pages")
    if resume_arg:
        candidate = Path(resume_arg).expanduser()
        p = candidate if candidate.is_absolute() else (resumes_root / candidate)
        if not p.exists():
            raise RuntimeError(f"Resume not found: {p}")
        return p
    if len(pages) == 1:
        return pages[0]
    if not pages:
        raise RuntimeError(f"No .pages resumes found in {resumes_root}")
    names = ", ".join(p.name for p in pages[:10])
    raise RuntimeError(f"Multiple resumes found; pass --resume. Examples: {names}")


def cmd_init(args: argparse.Namespace) -> None:
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    artifacts_root, resumes_root = _ensure_roots(conn)
    db.set_setting(conn, "artifacts_root", str(artifacts_root))
    db.set_setting(conn, "resumes_root", str(resumes_root))
    Path(artifacts_root).mkdir(parents=True, exist_ok=True)
    Path(resumes_root).mkdir(parents=True, exist_ok=True)
    print(f"db={DB_PATH}")
    print(f"artifacts_root={artifacts_root}")
    print(f"resumes_root={resumes_root}")


def cmd_set_root(args: argparse.Namespace) -> None:
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    key = "artifacts_root" if args.which == "artifacts" else "resumes_root"
    db.set_setting(conn, key, str(Path(args.path).expanduser().resolve()))
    print(f"{key}={db.get_setting(conn, key)}")


def cmd_add_job(args: argparse.Namespace) -> None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key, timeout=180.0, max_retries=0)

    conn = db.connect(DB_PATH)
    db.init_db(conn)
    artifacts_root, resumes_root = _ensure_roots(conn)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    if args.jd_file:
        jd_text = Path(args.jd_file).read_text(encoding="utf-8", errors="replace")
    else:
        jd_text = sys.stdin.read()
    jd_text = jd_text.strip()
    if not jd_text:
        raise RuntimeError("JD text is empty. Provide --jd-file or pipe JD text via stdin.")

    company, job_title = _extract_company_title_llm(client, model, jd_text)
    folder_name = _slug(company or "company") + "__" + _slug(job_title or "job")
    job_dir = artifacts_root / folder_name
    counter = 1
    while job_dir.exists():
        counter += 1
        job_dir = artifacts_root / f"{folder_name}_{counter}"
    job_dir.mkdir(parents=True, exist_ok=False)

    # Store JD both in DB and as an artifact for convenience.
    (job_dir / "jd.md").write_text(jd_text + "\n", encoding="utf-8")

    resume_pages = _choose_resume(resumes_root, args.resume)
    shutil.copy2(resume_pages, job_dir / resume_pages.name)

    job_id = db.create_job(
        conn,
        company=company,
        job_title=job_title,
        job_url=args.url,
        jd_text=jd_text,
        artifact_dir=str(job_dir),
    )
    db.add_event(
        conn,
        job_id=job_id,
        event_date=date.today().isoformat(),
        event_type="created",
        note=None,
        raw_input=None,
    )

    print(f"created job_id={job_id} dir={job_dir}")


def _extract_nl_event(client: OpenAI, model: str, message: str) -> dict:
    prompt = (
        "You help log job-application updates.\n"
        "From the user's message, extract a company search term, optional job title term, an event_type, and a short note.\n"
        "Return ONLY these 4 lines:\n"
        "COMPANY_QUERY: <string or null>\n"
        "JOB_TITLE_QUERY: <string or null>\n"
        "EVENT_TYPE: applied|interview|rejected|offer|unknown\n"
        "NOTE: <string or null>\n\n"
        "USER_MESSAGE:\n"
        f"{message}\n"
    )
    resp = client.responses.create(model=model, input=prompt)
    text = (getattr(resp, "output_text", "") or "").strip()

    out: dict[str, str | None] = {"company_query": None, "job_title_query": None, "event_type": "unknown", "note": None}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().upper()
        v = v.strip()
        v_out = None if (not v or v.casefold() == "null") else v
        if k == "COMPANY_QUERY":
            out["company_query"] = v_out
        elif k == "JOB_TITLE_QUERY":
            out["job_title_query"] = v_out
        elif k == "EVENT_TYPE":
            out["event_type"] = (v_out or "unknown").casefold()
        elif k == "NOTE":
            out["note"] = v_out
    return out


def _pick_job_interactive(conn, candidates: list) -> int | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return int(candidates[0]["id"])

    print("I found multiple matching jobs. Which did you mean?")
    for i, row in enumerate(candidates, start=1):
        company = row["company"] or "(unknown company)"
        title = row["job_title"] or "(unknown title)"
        status = row["status"]
        print(f"{i}) {company} — {title} [{status}]")
    while True:
        choice = input(f"Select 1-{len(candidates)} (or blank to cancel): ").strip()
        if not choice:
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(candidates):
                return int(candidates[idx - 1]["id"])
        print("Invalid selection.")


def _status_for_event(event_type: str) -> str | None:
    if event_type == "applied":
        return "applied"
    if event_type.startswith("interview"):
        return "in_process"
    if event_type == "rejected":
        return "rejected"
    if event_type == "offer":
        return "offer"
    return None


def cmd_nl(args: argparse.Namespace) -> None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key, timeout=60.0, max_retries=0)

    conn = db.connect(DB_PATH)
    db.init_db(conn)

    extracted = _extract_nl_event(client, model, args.message)
    query = extracted["company_query"] or extracted["job_title_query"] or args.message
    candidates = db.search_jobs(conn, str(query), limit=10)
    job_id = _pick_job_interactive(conn, candidates)
    if job_id is None:
        print("Canceled.")
        return

    event_type = str(extracted.get("event_type") or "unknown")
    if event_type == "rejection":
        event_type = "rejected"
    if event_type == "interview":
        max_stage = db.get_max_interview_stage(conn, job_id)
        stage = min(max_stage + 1, 6) if max_stage < 6 else 6
        event_type = f"interview_{stage}"
    m = re.fullmatch(r"interview_(\d)", event_type)
    if m:
        stage = int(m.group(1))
        if not (1 <= stage <= 6):
            event_type = "unknown"
    note = extracted.get("note")
    db.add_event(
        conn,
        job_id=job_id,
        event_date=date.today().isoformat(),
        event_type=event_type,
        note=note,
        raw_input=args.message,
    )

    new_status = _status_for_event(event_type)
    if new_status:
        db.update_job_status(conn, job_id, new_status)

    job = db.get_job(conn, job_id)
    if job:
        company = job["company"] or "(unknown company)"
        title = job["job_title"] or "(unknown title)"
        print(f"Updated: {company} — {title} event_type={event_type}")
    else:
        print(f"Updated job_id={job_id} event_type={event_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume Manager UI (DB-first)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize the repo-root SQLite DB and defaults.")
    p_init.set_defaults(func=cmd_init)

    p_set = sub.add_parser("set-root", help="Set artifacts or resumes root paths.")
    p_set.add_argument("which", choices=["artifacts", "resumes"])
    p_set.add_argument("path")
    p_set.set_defaults(func=cmd_set_root)

    p_add = sub.add_parser("add-job", help="Create a job from JD text and optional URL.")
    p_add.add_argument("--url", default=None)
    p_add.add_argument("--jd-file", default=None, help="Path to a JD text/markdown file.")
    p_add.add_argument("--resume", default=None, help="Resume .pages filename (in resumes_root) or full path.")
    p_add.set_defaults(func=cmd_add_job)

    p_nl = sub.add_parser("nl", help="Update a job using a natural-language message.")
    p_nl.add_argument("message")
    p_nl.set_defaults(func=cmd_nl)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
