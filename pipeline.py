from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import httpx
from bs4 import BeautifulSoup
from openai import OpenAI

import db
from utilities import ensure_ats_report_exists, _pick_resume, _read_pages_text, _read_pdf_text

CONFIDENCE_THRESHOLD = 0.75

PIPELINE_FIELDS = [
    {"key": "company", "label": "Company name"},
    {"key": "job_title", "label": "Job title"},
    {"key": "location", "label": "Location"},
    {"key": "work_mode", "label": "Work mode", "options": ["onsite", "hybrid", "remote", "unknown"]},
    {"key": "employment_type", "label": "Employment type", "options": ["full_time", "part_time", "contract", "internship", "unknown"]},
]


@dataclass
class PipelineState:
    session_id: str
    url: str
    jd_text: str = ""
    fields: dict = field(default_factory=dict)
    step: str = "fetch_jd"
    field_index: int = 0
    pending_field: str | None = None
    job_id: int | None = None
    artifact_dir: str | None = None
    selected_resume: str | None = None


def fetch_jd(url: str) -> str:
    resp = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def _extract_field_schema(f: dict) -> dict:
    if "options" in f:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "value": {"type": "string", "enum": f["options"]},
                "confidence": {"type": "number"},
            },
            "required": ["value", "confidence"],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["value", "confidence"],
    }


def extract_field_llm(
    client: OpenAI, model: str, jd_text: str, field_config: dict
) -> tuple[str, float]:
    label = field_config["label"]
    schema = _extract_field_schema(field_config)
    prompt = (
        f"Extract the '{label}' from the job description below.\n"
        "Return a JSON object with 'value' (string) and 'confidence' (0.0–1.0).\n"
        "If you cannot determine the value, use 'unknown' or an empty string with low confidence.\n\n"
        f"JOB_DESCRIPTION:\n{jd_text[:6000]}"
    )
    resp = client.responses.create(
        model=model,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "field_extraction",
                "strict": True,
                "schema": schema,
            }
        },
    )
    out = getattr(resp, "output_text", "") or ""
    data = json.loads(str(out).strip())
    return str(data["value"]), float(data["confidence"])


def select_resume_llm(
    client: OpenAI, model: str, jd_text: str, resume_names: list[str]
) -> tuple[str, str, float]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "filename": {"type": "string", "enum": resume_names},
            "reasoning": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["filename", "reasoning", "confidence"],
    }
    names_list = "\n".join(f"- {n}" for n in resume_names)
    prompt = (
        "Select the best-fit resume for the following job description from the list of available resumes.\n"
        "The filenames are descriptive and indicate the focus area.\n\n"
        f"AVAILABLE RESUMES:\n{names_list}\n\n"
        f"JOB_DESCRIPTION:\n{jd_text[:6000]}"
    )
    resp = client.responses.create(
        model=model,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "resume_selection",
                "strict": True,
                "schema": schema,
            }
        },
    )
    out = getattr(resp, "output_text", "") or ""
    data = json.loads(str(out).strip())
    return str(data["filename"]), str(data["reasoning"]), float(data["confidence"])


def _slug(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:40].strip("-")


def run_pipeline(
    state: PipelineState,
    user_input: str,
    conn: sqlite3.Connection,
    client: OpenAI,
    model: str,
    ats_prompt: str,
    artifacts_root: Path,
    resumes_root: Path,
) -> Generator[dict, None, None]:
    """
    Generator yielding event dicts. Caller streams these as NDJSON.
    Call again with user_input when state.pending_field is set.
    """

    # --- Handle pending field answer from user ---
    if state.pending_field is not None:
        state.fields[state.pending_field] = user_input.strip()
        yield {"type": "pipeline_field", "key": state.pending_field, "value": user_input.strip(), "confirmed": True}
        state.pending_field = None
        state.field_index += 1
        state.step = "extract_fields"

    # --- step: fetch_jd ---
    if state.step == "fetch_jd":
        yield {"type": "pipeline_step", "session_id": state.session_id, "text": "Fetching job description..."}
        try:
            state.jd_text = fetch_jd(state.url)
        except Exception as exc:
            yield {"type": "pipeline_error", "text": f"Failed to fetch job description: {exc}"}
            return
        state.step = "extract_fields"
        state.field_index = 0

    # --- step: extract_fields ---
    if state.step == "extract_fields":
        while state.field_index < len(PIPELINE_FIELDS):
            f = PIPELINE_FIELDS[state.field_index]
            key = f["key"]

            if key in state.fields:
                state.field_index += 1
                continue

            yield {"type": "pipeline_step", "session_id": state.session_id, "text": f"Extracting {f['label']}..."}
            try:
                value, confidence = extract_field_llm(client, model, state.jd_text, f)
            except Exception as exc:
                yield {"type": "pipeline_error", "text": f"Field extraction failed ({key}): {exc}"}
                return

            if confidence >= CONFIDENCE_THRESHOLD:
                state.fields[key] = value
                yield {"type": "pipeline_field", "key": key, "label": f["label"], "value": value, "confidence": confidence}
                state.field_index += 1
            else:
                # Ask user
                state.pending_field = key
                yield {
                    "type": "pipeline_ask",
                    "session_id": state.session_id,
                    "key": key,
                    "label": f["label"],
                    "suggested": value,
                    "confidence": confidence,
                    "text": f"I'm not confident about the **{f['label']}** (got: `{value}`, confidence {confidence:.0%}). Please confirm or correct it:",
                }
                return  # pause; wait for next user_input

        state.step = "select_resume"

    # --- step: select_resume ---
    if state.step == "select_resume":
        yield {"type": "pipeline_step", "session_id": state.session_id, "text": "Selecting best resume..."}
        # Collect all resumes; prefer .pages over .pdf when both exist for the same stem
        all_files = sorted(resumes_root.iterdir())
        pages_files = {p.stem: p for p in all_files if p.suffix.lower() == ".pages"}
        pdf_files = {p.stem: p for p in all_files if p.suffix.lower() == ".pdf"}
        # Merge: .pages wins over .pdf for the same stem
        resume_map: dict[str, Path] = {**pdf_files, **pages_files}
        if not resume_map:
            yield {"type": "pipeline_error", "text": "No resumes (.pages or .pdf) found in resumes folder."}
            return

        resume_names = [p.name for p in resume_map.values()]
        try:
            filename, reasoning, confidence = select_resume_llm(client, model, state.jd_text, resume_names)
        except Exception as exc:
            yield {"type": "pipeline_error", "text": f"Resume selection failed: {exc}"}
            return

        state.selected_resume = filename
        yield {"type": "pipeline_resume", "session_id": state.session_id, "filename": filename, "reasoning": reasoning, "confidence": confidence}
        state.step = "create_job"

    # --- step: create_job ---
    if state.step == "create_job":
        yield {"type": "pipeline_step", "session_id": state.session_id, "text": "Creating job entry..."}
        company = state.fields.get("company", "unknown")
        job_title = state.fields.get("job_title", "unknown")
        folder_name = f"{_slug(company)}__{_slug(job_title)}"

        job_dir = artifacts_root / folder_name
        # Avoid collisions
        if job_dir.exists():
            job_dir = artifacts_root / f"{folder_name}_2"

        job_dir.mkdir(parents=True, exist_ok=True)

        # Write jd.md
        (job_dir / "jd.md").write_text(state.jd_text, encoding="utf-8")

        # Copy selected resume — always include both .pages (for editing) and .pdf (for ATS text extraction)
        src_resume = resumes_root / state.selected_resume
        if src_resume.exists():
            shutil.copy2(str(src_resume), str(job_dir / src_resume.name))
        # Always copy the counterpart format if it exists
        if src_resume.suffix.lower() == ".pdf":
            pages_counterpart = src_resume.with_suffix(".pages")
            if pages_counterpart.exists():
                shutil.copy2(str(pages_counterpart), str(job_dir / pages_counterpart.name))
        elif src_resume.suffix.lower() == ".pages":
            pdf_counterpart = src_resume.with_suffix(".pdf")
            if pdf_counterpart.exists():
                shutil.copy2(str(pdf_counterpart), str(job_dir / pdf_counterpart.name))

        try:
            job_id = db.create_job(
                conn,
                company=company,
                job_title=job_title,
                job_url=state.url,
                jd_text=state.jd_text,
                artifact_dir=str(job_dir),
            )
        except Exception as exc:
            yield {"type": "pipeline_error", "text": f"DB create_job failed: {exc}"}
            return

        state.job_id = job_id
        state.artifact_dir = str(job_dir)

        try:
            db.update_job_details(
                conn,
                job_id,
                location=state.fields.get("location"),
                work_mode=state.fields.get("work_mode"),
                employment_type=state.fields.get("employment_type"),
                selected_resume=state.selected_resume,
            )
            db.add_event(
                conn,
                job_id=job_id,
                event_date=__import__("datetime").date.today().isoformat(),
                event_type="created",
                note="Created via pipeline",
                raw_input=state.url,
            )
        except Exception as exc:
            yield {"type": "pipeline_error", "text": f"DB update failed: {exc}"}
            return

        state.step = "run_ats"

    # --- step: run_ats ---
    if state.step == "run_ats":
        yield {"type": "pipeline_step", "session_id": state.session_id, "text": "Running ATS analysis..."}
        job_dir = Path(state.artifact_dir)
        try:
            result = ensure_ats_report_exists(job_dir, client, model, ats_prompt)
        except Exception as exc:
            yield {"type": "pipeline_error", "text": f"ATS eval failed: {exc}"}
            return

        if result == "created" and state.job_id is not None:
            report_path = job_dir / "ats_report.json"
            if report_path.exists():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    db.store_ats_results(
                        conn,
                        state.job_id,
                        rejection_likelihood=report.get("rejection_likelihood"),
                        top_gaps=report.get("top_gaps") or [],
                        top_strengths=report.get("top_strengths") or [],
                        screen_out_flags=report.get("screen_out_flags") or [],
                    )
                except Exception:
                    pass  # ATS results stored best-effort

        company = state.fields.get("company", "")
        job_title = state.fields.get("job_title", "")

        # Load ATS report for the completion event
        ats_report = None
        report_path = job_dir / "ats_report.json"
        if report_path.exists():
            try:
                ats_report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        yield {
            "type": "pipeline_complete",
            "session_id": state.session_id,
            "job_id": state.job_id,
            "company": company,
            "job_title": job_title,
            "artifact_dir": state.artifact_dir,
            "selected_resume": state.selected_resume,
            "ats_result": result,
            "ats_report": ats_report,
            "text": (
                f"Done! Created **{company} — {job_title}**.\n"
                f"Resume: {state.selected_resume}\n"
                f"ATS eval: {result}"
            ),
        }

        # --- Generate coaching intro via LLM ---
        if ats_report and state.job_id is not None:
            try:
                coaching_prompt_path = Path(__file__).resolve().parent / "prompts" / "resume_coach.md"
                coaching_system = coaching_prompt_path.read_text(encoding="utf-8") if coaching_prompt_path.exists() else ""

                # Build context: ATS report + resume text + JD
                resume_text = ""
                resume_file = _pick_resume(job_dir)
                if resume_file:
                    try:
                        if resume_file.suffix.lower() == ".pages":
                            resume_text = _read_pages_text(resume_file)
                        else:
                            resume_text = _read_pdf_text(resume_file)
                    except Exception:
                        pass

                ats_context = json.dumps(ats_report, indent=2)
                jd_text = state.jd_text[:6000]

                # Load candidate master profile if available
                profile_block = ""
                try:
                    profile = db.get_candidate_profile(conn)
                    if profile:
                        profile_block = (
                            "\n\n=== CANDIDATE MASTER PROFILE ===\n"
                            "(Full background across all resume variants. Check here for "
                            "experience that addresses gaps but wasn't in this specific resume.)\n"
                            + profile[:8000]
                        )
                except Exception:
                    pass

                coaching_input = (
                    coaching_system
                    + "\n\n=== ATS EVALUATION REPORT ===\n" + ats_context
                    + "\n\n=== JOB DESCRIPTION ===\n" + jd_text
                    + "\n\n=== RESUME TEXT ===\n" + resume_text[:6000]
                    + profile_block
                    + "\n\nThis is your first message. Deliver the go/no-go assessment now."
                )

                resp = client.responses.create(model=model, input=coaching_input)
                coaching_text = (getattr(resp, "output_text", "") or "").strip()

                if coaching_text:
                    # Save coaching intro to conversation history
                    db.save_message(conn, job_id=state.job_id, role="assistant", text=coaching_text)
                    yield {
                        "type": "pipeline_coaching",
                        "session_id": state.session_id,
                        "job_id": state.job_id,
                        "text": coaching_text,
                    }
            except Exception:
                pass  # Coaching is best-effort; pipeline already succeeded
