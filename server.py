import difflib
import json
import os
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from openai import OpenAI

import db
from pipeline import PipelineState, run_pipeline

load_dotenv()


REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "resume_manager.sqlite3"
WEB_DIR = REPO_ROOT / "web"
ARTIFACTS_ROOT = REPO_ROOT / "jobs"
RESUMES_ROOT = REPO_ROOT / "resumes"
ATS_PROMPT_PATH = REPO_ROOT / "prompts" / "ats_eval.md"
COACHING_PROMPT_PATH = REPO_ROOT / "prompts" / "resume_coach.md"

pipeline_sessions: dict[str, PipelineState] = {}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _extract_url(msg: str) -> str | None:
    m = _URL_RE.search(msg)
    return m.group(0).rstrip(".,)>\"'") if m else None


def _client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    return OpenAI(api_key=api_key, timeout=120.0, max_retries=0)


def _model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def _ats_prompt() -> str:
    if ATS_PROMPT_PATH.exists():
        return ATS_PROMPT_PATH.read_text(encoding="utf-8")
    return ""


def _extract_response_text(resp: object) -> str:
    text = getattr(resp, "output_text", "") or ""
    text = str(text).strip()
    if text:
        return text
    output = getattr(resp, "output", None)
    if output is None and isinstance(resp, dict):
        output = resp.get("output")
    parts: list[str] = []
    if isinstance(output, list):
        for item in output:
            content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for c in content:
                if isinstance(c, dict):
                    seg_text = c.get("text")
                else:
                    seg_text = getattr(c, "text", None)
                if isinstance(seg_text, str) and seg_text.strip():
                    parts.append(seg_text.strip())
    return "\n".join(parts).strip()


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


def _next_interview_stage(conn, job_id: int) -> str:
    max_stage = db.get_max_interview_stage(conn, job_id)
    stage = min(max_stage + 1, 6) if max_stage < 6 else 6
    return f"interview_{stage}"


def _find_jobs(conn, query: str, limit: int = 10) -> tuple[list, bool]:
    """
    Smart search: exact LIKE → token split → fuzzy difflib.
    Returns (rows, is_fuzzy).
    """
    # 1. Direct substring match
    rows = list(db.search_jobs(conn, query, limit=limit))
    if rows:
        return rows, False

    # 2. Split "Company — Job Title" and try each token independently
    for token in re.split(r"\s*[—–\-]\s*", query):
        token = token.strip()
        if len(token) >= 3:
            rows = list(db.search_jobs(conn, token, limit=limit))
            if rows:
                return rows, False

    # 3. Fuzzy match against company + folder name
    all_rows = list(conn.execute(
        "SELECT id, company, job_title, status, artifact_dir FROM jobs"
    ).fetchall())
    q = query.lower()
    scored: list[tuple[float, object]] = []
    for row in all_rows:
        company = (row["company"] or "").lower()
        folder = Path(row["artifact_dir"] or "").name.lower().replace("__", " ").replace("-", " ")
        score = max(
            difflib.SequenceMatcher(None, q, company).ratio(),
            difflib.SequenceMatcher(None, q, folder).ratio(),
        )
        if score > 0.55:
            scored.append((score, row))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:limit]], bool(scored)


def _build_job_context_block(job, *, include_full_ats: bool = False) -> str:
    lines = ["=== FOCUSED JOB CONTEXT ==="]
    lines += [
        f"Company:         {job['company'] or '(unknown)'}",
        f"Job title:       {job['job_title'] or '(unknown)'}",
        f"Status:          {job['status'] or '(unknown)'}",
        f"Location:        {job['location'] or '(unknown)'}",
        f"Work mode:       {job['work_mode'] or '(unknown)'}",
        f"Selected resume: {job['selected_resume'] or '(none)'}",
    ]
    if job["ats_rejection_likelihood"] is not None:
        def _jlist(v): return json.loads(v) if v else []
        lines += [
            "\n--- ATS Evaluation Summary ---",
            f"Rejection likelihood: {job['ats_rejection_likelihood']:.0%}",
            "Top strengths: " + "; ".join(_jlist(job["ats_top_strengths"])),
            "Top gaps: " + "; ".join(_jlist(job["ats_top_gaps"])),
            "Screen-out flags: " + ("; ".join(_jlist(job["ats_screen_out_flags"])) or "(none)"),
        ]

    # Full ATS report with all 8 requirements
    if include_full_ats and job.get("artifact_dir"):
        report_path = Path(job["artifact_dir"]) / "ats_report.json"
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                reqs = report.get("requirements", [])
                if reqs:
                    lines.append("\n--- Full ATS Requirements ---")
                    for i, req in enumerate(reqs, 1):
                        lines.append(
                            f"REQ_{i}: {req.get('text', '?')} | "
                            f"Status: {req.get('status', '?')} | "
                            f"Evidence: {req.get('evidence') or 'none'} | "
                            f"Rationale: {req.get('rationale', '')}"
                        )
            except Exception:
                pass

        # Include resume text for coaching context
        from utilities import _pick_resume, _read_pages_text, _read_pdf_text
        job_dir = Path(job["artifact_dir"])
        resume_file = _pick_resume(job_dir)
        if resume_file:
            try:
                if resume_file.suffix.lower() == ".pages":
                    resume_text = _read_pages_text(resume_file)
                else:
                    resume_text = _read_pdf_text(resume_file)
                if resume_text:
                    lines += ["\n--- Resume Text ---", resume_text[:6000]]
            except Exception:
                pass

    jd = (job["jd_text"] or "").strip()
    if jd:
        lines += ["\n--- Full Job Description ---", jd]
    lines.append("=== END FOCUSED JOB CONTEXT ===")
    return "\n".join(lines)


def _route_message(client: OpenAI, model: str, message: str, context: list | None = None, job_context_block: str | None = None) -> dict:
    router_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {"type": "string", "enum": ["query", "update", "rename", "chat"]},
            "job_query": {"type": "string", "minLength": 1},
            "query_type": {
                "type": "string",
                "enum": ["last_info", "applied_date", "status", "events", "jd", "none"],
            },
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "event_type": {
                            "type": "string",
                            "enum": ["applied", "interview", "rejected", "offer", "unknown", "none"],
                        },
                        "event_date": {"type": ["string", "null"]},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["event_type", "event_date", "note"],
                },
            },
            "new_company":   {"type": ["string", "null"]},
            "new_job_title": {"type": ["string", "null"]},
        },
        "required": ["intent", "job_query", "query_type", "events", "new_company", "new_job_title"],
    }

    today = date.today().isoformat()

    # Build a short conversation context block so the LLM can resolve pronouns
    context_block = ""
    if context:
        lines = []
        for msg in context[-6:]:
            role = str(msg.get("role", "")).upper()
            text = str(msg.get("text", "")).strip()
            if role and text:
                lines.append(f"{role}: {text}")
        if lines:
            context_block = "\nRECENT_CONVERSATION:\n" + "\n".join(lines) + "\n"

    job_prefix = (job_context_block + "\n\n") if job_context_block else ""
    prompt = (
        job_prefix
        + f"Today is {today}.\n"
        + "Decide whether the user is asking a question (query) or logging an update (update).\n"
        "Rules:\n"
        "- intent=query: user asks for info. Set events=[], new_company=null, new_job_title=null.\n"
        "- intent=update: user states something happened. Set query_type='none', new_company=null, new_job_title=null. "
        "  Extract ALL events mentioned as separate objects in the events array, sorted by date oldest-first. "
        "  A single message may describe multiple events (e.g. an interview AND a rejection).\n"
        "- intent=rename: user wants to correct/change a company name or job title. "
        "  Extract the target job in job_query, new company name in new_company, new job title in new_job_title. "
        "  Set query_type='none', events=[].\n"
        "- intent=chat: user is asking an open-ended question, comparing jobs, seeking advice, "
        "  pasting a JD for discussion, or anything that doesn't fit query/update/rename. "
        "  Set query_type='none', events=[], new_company=null, new_job_title=null. "
        "  Put the most relevant company name in job_query (or 'none' if no company is relevant).\n"
        "- query_type=jd: user wants to see the job description text.\n"
        "- job_query: ONLY the company name — 1 to 3 words, never include a job title. "
        "  Examples: 'Grafana', 'Thales', 'Databricks'. "
        "  If the message uses pronouns ('they', 'them') or no company name, resolve the company from RECENT_CONVERSATION.\n"
        f"- event_date (per event): if the user mentions a specific date ('yesterday', 'March 1st', 'Feb 28th', 'last Tuesday'), "
        f"  convert it to YYYY-MM-DD using today={today} as reference. Use null if no date is mentioned.\n"
        "- note (per event): brief factual summary of what happened for that event, else null.\n"
        + context_block
        + f"\nUSER_MESSAGE:\n{message}\n"
    )

    resp = client.responses.create(
        model=model,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "job_hunt_router",
                "strict": True,
                "schema": router_schema,
            }
        },
    )
    return json.loads(_extract_response_text(resp))


def _answer_query(conn, job_id: int, query_type: str) -> str:
    """Return a structured data string — readable by both humans and the naturalizer LLM."""
    job = db.get_job(conn, job_id)
    company = (job["company"] if job else "") or ""
    title = (job["job_title"] if job else "") or ""
    header = f"Company: {company}\nJob title: {title}"

    if query_type == "applied_date":
        applied = db.get_first_event_date_by_type(conn, job_id, "applied")
        return header + f"\nApplied date: {applied or 'unknown'}"

    if query_type == "status":
        status = db.get_effective_status(conn, job_id)
        latest = db.get_latest_event(conn, job_id)
        date_line = f"\nLast event date: {latest['event_date']}" if latest else ""
        return header + f"\nStatus: {status}" + date_line

    if query_type == "events":
        evs = db.list_events(conn, job_id, limit=8)
        if not evs:
            return header + "\nEvents: none recorded"
        lines = [header, "Events (oldest first):"]
        for e in evs:
            note = f" — {e['note']}" if e["note"] else ""
            lines.append(f"  {e['event_date']}  {e['event_type']}{note}")
        return "\n".join(lines)

    if query_type == "jd":
        row = conn.execute("SELECT jd_text FROM jobs WHERE id = ?", (job_id,)).fetchone()
        jd = (row["jd_text"] if row else None) or ""
        if not jd:
            return header + "\nJob description: not available"
        return header + f"\nJob description:\n\n{jd}"

    # last_info (default)
    latest = db.get_latest_event(conn, job_id)
    if not latest:
        return header + "\nNo events recorded yet."
    note = f"\nNote: {latest['note']}" if latest["note"] else ""
    return header + f"\nLast event: {latest['event_type']} on {latest['event_date']}" + note


def _naturalize(client: OpenAI, model: str, data: str, context: list | None = None, job_context_block: str | None = None) -> str:
    """Convert a structured data string into a short, natural conversational sentence."""
    ctx_block = ""
    if context:
        lines = [f"{m['role'].upper()}: {m['text']}" for m in context[-4:] if m.get("text")]
        if lines:
            ctx_block = "\nRECENT CONVERSATION:\n" + "\n".join(lines) + "\n"
    job_prefix = (job_context_block + "\n\n") if job_context_block else ""
    resp = client.responses.create(
        model=model,
        input=(
            job_prefix
            + "You are a helpful job hunt assistant. "
            "Given the structured data below, write a brief natural response (1–2 sentences max). "
            "Be warm but concise. Reference specific details like company name and date. "
            "Do not invent any information not present in the data.\n"
            + ctx_block
            + f"\nDATA:\n{data}"
        ),
    )
    return (getattr(resp, "output_text", "") or "").strip()


def _build_related_jobs_context(conn, company_query: str, exclude_id: int | None = None) -> str:
    """Build a context block summarizing jobs related to a company query."""
    if not company_query or company_query.lower() == "none":
        return ""
    candidates, _ = _find_jobs(conn, company_query, limit=5)
    if not candidates:
        return ""
    lines = ["=== RELATED JOBS IN DATABASE ==="]
    for row in candidates:
        if exclude_id is not None and int(row["id"]) == exclude_id:
            continue
        job = db.get_full_job(conn, int(row["id"]))
        if not job:
            continue
        lines.append(f"\n--- Job #{job['id']}: {job['company'] or '?'} — {job['job_title'] or '?'} ---")
        lines.append(f"Status: {job['status'] or 'unknown'}")
        jd = (job["jd_text"] or "").strip()
        if jd:
            # Include a truncated JD for comparison
            lines.append(f"JD (first 2000 chars):\n{jd[:2000]}")
        evts = db.list_events(conn, int(job["id"]), limit=5)
        if evts:
            lines.append("Recent events:")
            for e in evts:
                note = f" — {e['note']}" if e["note"] else ""
                lines.append(f"  {e['event_date']}  {e['event_type']}{note}")
    lines.append("=== END RELATED JOBS ===")
    return "\n".join(lines)


def _chat_response(
    client: OpenAI,
    model: str,
    message: str,
    context: list | None = None,
    job_context_block: str | None = None,
    related_jobs_block: str | None = None,
    coaching_system: str | None = None,
) -> str:
    """Open-ended LLM response with full context for flexible conversation."""
    ctx_block = ""
    if context:
        lines = [f"{m['role'].upper()}: {m['text']}" for m in context[-6:] if m.get("text")]
        if lines:
            ctx_block = "\nRECENT CONVERSATION:\n" + "\n".join(lines) + "\n"

    parts = []
    if coaching_system:
        parts.append(coaching_system)
    if job_context_block:
        parts.append(job_context_block)
    if related_jobs_block:
        parts.append(related_jobs_block)

    today = date.today().isoformat()
    if coaching_system:
        parts.append(
            f"Today is {today}.\n"
            + ctx_block
            + f"\nUSER MESSAGE:\n{message}"
        )
    else:
        parts.append(
            f"Today is {today}.\n"
            "You are a helpful, thoughtful job hunt assistant. The user is managing their job search.\n"
            "You have access to their job database context above. Use it to give informed, specific answers.\n"
            "When comparing jobs, highlight meaningful differences in responsibilities, requirements, and focus areas.\n"
            "Be concise but thorough. Use markdown formatting for readability.\n"
            "Do not invent information not present in the context.\n"
            + ctx_block
            + f"\nUSER MESSAGE:\n{message}"
        )

    resp = client.responses.create(model=model, input="\n\n".join(parts))
    return (getattr(resp, "output_text", "") or "").strip()


def _run_pipeline_stream(state: PipelineState, user_input: str):
    """Returns a generator factory. Connection is created inside the generator thread."""
    def gen():
        # Create connection in THIS thread (fixes SQLite threading error)
        conn = db.connect(DB_PATH)
        db.init_db(conn)
        client = _client()
        model = _model()
        ats_prompt = _ats_prompt()

        def emit(obj: dict) -> bytes:
            return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

        try:
            saw_complete = False
            for event in run_pipeline(
                state, user_input, conn, client, model,
                ats_prompt, ARTIFACTS_ROOT, RESUMES_ROOT,
            ):
                yield emit(event)
                etype = event.get("type")
                if etype == "pipeline_error":
                    pipeline_sessions.pop(state.session_id, None)
                elif etype == "pipeline_complete":
                    saw_complete = True
                elif etype == "pipeline_coaching":
                    pass  # coaching arrived after complete
            # Clean up after generator finishes (handles both complete+coaching and complete-only)
            if saw_complete:
                pipeline_sessions.pop(state.session_id, None)
        except Exception as exc:
            pipeline_sessions.pop(state.session_id, None)
            yield emit({"type": "pipeline_error", "text": f"Pipeline crashed: {type(exc).__name__}: {exc}"})

    return gen


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


app = FastAPI()
app.add_middleware(NoCacheStaticMiddleware)
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    repaired = db.repair_statuses(conn)
    if repaired:
        print(f"[startup] repaired {repaired} job(s) from status=draft")
    bad_names = db.repair_bad_company_names(conn)
    if bad_names:
        print(f"[startup] repaired {bad_names} bad company name(s)")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    index_path = WEB_DIR / "index.html"
    return index_path.read_text(encoding="utf-8") if index_path.exists() else "<h1>Missing web/index.html</h1>"


@app.get("/api/jobs/{job_id}/conversation")
def api_get_conversation(job_id: int) -> dict:
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    job = db.get_full_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    messages = db.get_conversation(conn, job_id)
    return {
        "job_id": job_id,
        "company": job["company"] or "",
        "job_title": job["job_title"] or "",
        "messages": [{"id": int(m["id"]), "role": m["role"], "text": m["text"]} for m in messages],
    }


@app.get("/api/jobs")
def api_jobs(q: str = "") -> dict:
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    rows = db.search_jobs(conn, q.strip(), limit=50)
    return {
        "jobs": [
            {
                "id": int(r["id"]),
                "company": r["company"] or "",
                "job_title": r["job_title"] or "",
                "status": r["status"] or "",
                "artifact_dir": r["artifact_dir"] or "",
                "updated_at": r["updated_at"] or "",
            }
            for r in rows
        ]
    }


@app.post("/api/jobs/{job_id}/rerun-ats")
def api_rerun_ats(job_id: int) -> dict:
    from utilities import archive_ats_report, ensure_ats_report_exists
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    job = db.get_full_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dir = Path(job["artifact_dir"])
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job folder not found")

    # Read old rejection likelihood before archiving
    old_likelihood = job["ats_rejection_likelihood"]

    # Archive old report
    archive_ats_report(job_dir)

    # Run fresh ATS eval
    client = _client()
    model = _model()
    ats_prompt = _ats_prompt()
    result = ensure_ats_report_exists(job_dir, client, model, ats_prompt)

    if result != "created":
        raise HTTPException(status_code=500, detail=f"ATS eval failed: {result}")

    # Load new report and store in DB
    report_path = job_dir / "ats_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    new_likelihood = report.get("rejection_likelihood")

    db.store_ats_results(
        conn, job_id,
        rejection_likelihood=new_likelihood,
        top_gaps=report.get("top_gaps") or [],
        top_strengths=report.get("top_strengths") or [],
        screen_out_flags=report.get("screen_out_flags") or [],
    )

    # Build comparison message
    comparison = f"**ATS Re-evaluation Complete**\n\nRejection likelihood: **{new_likelihood:.0%}**"
    if old_likelihood is not None:
        delta = new_likelihood - old_likelihood
        direction = "improved" if delta < 0 else "worsened" if delta > 0 else "unchanged"
        comparison += f"\nPrevious: {old_likelihood:.0%} → Now: {new_likelihood:.0%} ({direction})"
    comparison += f"\nTop gaps: {'; '.join(report.get('top_gaps') or ['(none)'])}"
    comparison += f"\nTop strengths: {'; '.join(report.get('top_strengths') or ['(none)'])}"

    # Save as conversation message
    db.save_message(conn, job_id=job_id, role="assistant", text=comparison)

    return {
        "result": result,
        "rejection_likelihood": new_likelihood,
        "old_rejection_likelihood": old_likelihood,
        "top_gaps": report.get("top_gaps") or [],
        "top_strengths": report.get("top_strengths") or [],
        "screen_out_flags": report.get("screen_out_flags") or [],
        "comparison_text": comparison,
    }


@app.post("/api/jobs/{job_id}/abandon")
def api_abandon_job(job_id: int) -> dict:
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    job = db.get_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    db.update_job_status(conn, job_id, "abandoned")
    db.add_event(
        conn,
        job_id=job_id,
        event_date=date.today().isoformat(),
        event_type="abandoned",
        note="Abandoned after coaching review",
        raw_input=None,
    )

    return {"status": "abandoned", "job_id": job_id}


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: int) -> dict:
    import shutil
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    job = db.get_full_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Delete artifact directory on disk
    artifact_dir = job["artifact_dir"]
    if artifact_dir:
        dirpath = Path(artifact_dir)
        if dirpath.exists():
            shutil.rmtree(dirpath)

    # Delete from DB (FK CASCADE handles events + conversations)
    db.delete_job(conn, job_id)

    return {"deleted": True, "job_id": job_id}


@app.post("/api/chat")
async def api_chat(request: Request) -> StreamingResponse:
    payload = await request.json()
    message = str(payload.get("message") or "").strip()
    job_id = payload.get("job_id")
    session_id = payload.get("session_id")
    context = payload.get("context") or []
    focused_job_id = payload.get("focused_job_id")

    # Resume an active pipeline session
    if session_id and session_id in pipeline_sessions:
        state = pipeline_sessions[session_id]
        return StreamingResponse(_run_pipeline_stream(state, message)(), media_type="application/x-ndjson")

    # Detect new pipeline trigger: URL in message
    url = _extract_url(message)
    if url:
        state = PipelineState(session_id=str(uuid.uuid4()), url=url)
        pipeline_sessions[state.session_id] = state
        return StreamingResponse(_run_pipeline_stream(state, message)(), media_type="application/x-ndjson")

    # Regular query / update routing — connection created inside generator
    def gen() -> Iterable[bytes]:
        # Create connection in THIS thread
        conn = db.connect(DB_PATH)
        db.init_db(conn)
        client = _client()
        model = _model()

        def emit(obj: dict) -> bytes:
            return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

        def job_display(row) -> dict:
            """Return job dict with a display_name fallback for jobs missing company/title."""
            company = (row["company"] if row else "") or ""
            title = (row["job_title"] if row else "") or ""
            artifact = (row["artifact_dir"] if row else "") or ""
            # Fallback: derive a name from the folder name
            if not company and artifact:
                company = Path(artifact).name.replace("__", " / ").replace("-", " ").title()
            return {"id": int(row["id"]), "company": company, "job_title": title, "artifact_dir": artifact}

        try:
            if not message:
                yield emit({"type": "error", "text": "Message is empty."})
                return

            # Load focused job context if provided
            focused_job = None
            job_context_block = None
            if focused_job_id is not None:
                focused_job = db.get_full_job(conn, int(focused_job_id))
                if focused_job:
                    job_context_block = _build_job_context_block(focused_job)

            yield emit({"type": "status", "text": "Thinking…"})
            routed = _route_message(client, model, message, context=context, job_context_block=job_context_block)
            intent = str(routed.get("intent"))
            query = str(routed.get("job_query") or "").strip() or message

            def _row_label(r) -> str:
                company = r["company"] or Path(r["artifact_dir"] or "").name or "(unknown)"
                title = r["job_title"] or "(unknown)"
                return f"{company} — {title} [{r['status']}]"

            if intent == "query":
                # Re-route ATS-focused queries through coaching when ATS data exists
                if focused_job and focused_job["ats_rejection_likelihood"] is not None:
                    coaching_system = None
                    if COACHING_PROMPT_PATH.exists():
                        coaching_system = COACHING_PROMPT_PATH.read_text(encoding="utf-8")
                    coaching_context = _build_job_context_block(focused_job, include_full_ats=True)
                    answer_text = _chat_response(
                        client, model, message,
                        context=context,
                        job_context_block=coaching_context,
                        coaching_system=coaching_system,
                    )
                    yield emit({"type": "answer", "text": answer_text})
                    if focused_job_id is not None:
                        db.save_message(conn, job_id=int(focused_job_id), role="user", text=message)
                        db.save_message(conn, job_id=int(focused_job_id), role="assistant", text=answer_text)
                    return

                query_type = str(routed.get("query_type") or "last_info")
                if focused_job_id is not None and focused_job is not None:
                    resolved = int(focused_job_id)
                else:
                    candidates, is_fuzzy = _find_jobs(conn, query, limit=10)
                    if not candidates:
                        yield emit({"type": "answer", "text": f"No matching jobs for: {query}"})
                        return
                    if len(candidates) > 1 and job_id is None:
                        prefix = "Did you mean one of these?" if is_fuzzy else "Which job did you mean?"
                        yield emit({
                            "type": "clarify",
                            "text": prefix,
                            "options": [{"id": int(r["id"]), "label": _row_label(r)} for r in candidates],
                        })
                        return
                    # Single fuzzy match — confirm before answering
                    if is_fuzzy and job_id is None:
                        yield emit({
                            "type": "clarify",
                            "text": f"Did you mean: {_row_label(candidates[0])}?",
                            "options": [{"id": int(candidates[0]["id"]), "label": _row_label(candidates[0])}],
                        })
                        return
                    resolved = int(job_id) if job_id is not None else int(candidates[0]["id"])
                data = _answer_query(conn, resolved, query_type)
                if query_type == "jd":
                    yield emit({"type": "answer", "text": data})
                    if focused_job_id is not None:
                        db.save_message(conn, job_id=int(focused_job_id), role="user", text=message)
                        db.save_message(conn, job_id=int(focused_job_id), role="assistant", text=data)
                    return
                natural = _naturalize(client, model, data, context=context, job_context_block=job_context_block)
                answer_text = natural or data
                yield emit({"type": "answer", "text": answer_text})
                if focused_job_id is not None:
                    db.save_message(conn, job_id=int(focused_job_id), role="user", text=message)
                    db.save_message(conn, job_id=int(focused_job_id), role="assistant", text=answer_text)
                return

            # Chat intent — open-ended conversation with full context
            if intent == "chat":
                # Use coaching prompt when job has ATS data
                coaching_system = None
                if focused_job and focused_job["ats_rejection_likelihood"] is not None:
                    if COACHING_PROMPT_PATH.exists():
                        coaching_system = COACHING_PROMPT_PATH.read_text(encoding="utf-8")
                    # Upgrade context to include full ATS + resume
                    job_context_block = _build_job_context_block(focused_job, include_full_ats=True)

                related_block = _build_related_jobs_context(
                    conn, query, exclude_id=int(focused_job_id) if focused_job_id else None
                )
                answer_text = _chat_response(
                    client, model, message,
                    context=context,
                    job_context_block=job_context_block,
                    related_jobs_block=related_block or None,
                    coaching_system=coaching_system,
                )
                yield emit({"type": "answer", "text": answer_text})
                if focused_job_id is not None:
                    db.save_message(conn, job_id=int(focused_job_id), role="user", text=message)
                    db.save_message(conn, job_id=int(focused_job_id), role="assistant", text=answer_text)
                return

            # Rename intent
            if intent == "rename":
                yield emit({"type": "status", "text": "Renaming…"})
                candidates, is_fuzzy = _find_jobs(conn, query, limit=10)
                if not candidates:
                    yield emit({"type": "error", "text": f"No matching jobs for: {query}"})
                    return
                if len(candidates) > 1 or (is_fuzzy and job_id is None):
                    prefix = "Did you mean one of these?" if is_fuzzy else "Which job did you mean?"
                    yield emit({
                        "type": "clarify",
                        "text": prefix,
                        "options": [{"id": int(r["id"]), "label": _row_label(r)} for r in candidates],
                    })
                    return
                resolved = int(job_id) if job_id is not None else int(candidates[0]["id"])
                new_company   = routed.get("new_company") or None
                new_job_title = routed.get("new_job_title") or None
                if not new_company and not new_job_title:
                    yield emit({"type": "answer", "text": "I didn't catch a new name. What should I rename it to?"})
                    return
                db.update_job_fields(conn, resolved, company=new_company, job_title=new_job_title)
                job = db.get_job(conn, resolved)
                jd = job_display(job)
                rename_data = (
                    f"Company: {jd['company']}\nJob title: {jd['job_title']}\nAction: renamed successfully"
                    + (f"\nNew company: {new_company}" if new_company else "")
                    + (f"\nNew title: {new_job_title}" if new_job_title else "")
                )
                natural = _naturalize(client, model, rename_data, context=context, job_context_block=job_context_block)
                yield emit({
                    "type": "ok",
                    "text": natural or "Done, renamed.",
                    "meta": "renamed",
                    "job": jd,
                    "event": {},
                })
                return

            # Update intent
            yield emit({"type": "status", "text": "Saving…"})
            resolved = job_id
            if resolved is None and focused_job_id is not None:
                resolved = int(focused_job_id)
            elif resolved is None:
                candidates, is_fuzzy = _find_jobs(conn, query, limit=10)
                if not candidates:
                    yield emit({"type": "error", "text": f"No matching jobs for: {query}"})
                    return
                if len(candidates) > 1 or is_fuzzy:
                    prefix = "Did you mean one of these?" if is_fuzzy else "Which job did you mean?"
                    yield emit({
                        "type": "clarify",
                        "text": prefix,
                        "options": [{"id": int(r["id"]), "label": _row_label(r)} for r in candidates],
                    })
                    return
                resolved = int(candidates[0]["id"])
            else:
                resolved = int(resolved)

            raw_events = routed.get("events") or []
            # Sort oldest-first so interview stages are assigned in the right order
            raw_events = sorted(raw_events, key=lambda ev: ev.get("event_date") or "9999")

            logged: list[dict] = []
            final_status: str | None = None
            for ev in raw_events:
                et = str(ev.get("event_type") or "unknown")
                ev_date = ev.get("event_date") or date.today().isoformat()
                ev_note = ev.get("note")
                if et in ("unknown", "none"):
                    continue
                if et == "interview":
                    et = _next_interview_stage(conn, resolved)
                if et in ("rejection", "rejected"):
                    et = "rejected"
                db.add_event(conn, job_id=resolved, event_date=ev_date,
                             event_type=et, note=ev_note, raw_input=message)
                new_status = _status_for_event(et)
                if new_status:
                    final_status = new_status
                logged.append({"event_type": et, "event_date": ev_date, "note": ev_note or ""})

            if not logged:
                yield emit({"type": "answer", "text": "I can log updates (applied / interview / rejected / offer) or answer questions. Could you rephrase?"})
                return

            if final_status:
                db.update_job_status(conn, resolved, final_status)

            job = db.get_job(conn, resolved)
            jd = job_display(job)
            label = jd["company"] or jd["artifact_dir"] or "(unknown)"

            events_summary = "\n".join(
                f"  {e['event_type']} on {e['event_date']}" + (f": {e['note']}" if e["note"] else "")
                for e in logged
            )
            update_data = (
                f"Company: {jd['company'] or label}\n"
                f"Job title: {jd['job_title']}\n"
                f"Events logged:\n{events_summary}"
            )
            natural = _naturalize(client, model, update_data, context=context, job_context_block=job_context_block)

            meta_text = "events logged · " + " | ".join(
                f"{e['event_type']} · {e['event_date']}" for e in logged
            )

            ok_text = natural or "Got it, logged."
            yield emit({
                "type": "ok",
                "text": ok_text,
                "meta": meta_text,
                "job": jd,
                "event": logged[-1],
            })
            if focused_job_id is not None:
                db.save_message(conn, job_id=int(focused_job_id), role="user", text=message)
                db.save_message(conn, job_id=int(focused_job_id), role="assistant", text=ok_text)
        except RuntimeError as exc:
            yield emit({"type": "error", "text": str(exc)})
        except Exception as exc:
            yield emit({"type": "error", "text": f"Server error: {type(exc).__name__}: {exc}"})

    return StreamingResponse(gen(), media_type="application/x-ndjson")
