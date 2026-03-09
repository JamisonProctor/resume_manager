from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import traceback
from typing import Iterable
import logging
import sys
import zipfile

from openai import OpenAI


class MissingDependencyError(RuntimeError):
    pass


def ensure_log_exists(subdir: Path) -> bool:
    """
    Create log.md in `subdir` if it does not exist.

    Returns True when a file was created, False if it already existed.
    Raises FileNotFoundError when jd.md is missing.
    """
    jd_path = subdir / "jd.md"
    if not jd_path.exists():
        raise FileNotFoundError(f"Missing jd.md in {subdir}")

    log_path = subdir / "log.md"
    if log_path.exists():
        return False

    birth_ts = _birth_timestamp(jd_path)
    date_str = datetime.fromtimestamp(birth_ts).date().isoformat()
    log_entry = f"{date_str} — Applied (jd.md created)\n"
    log_path.write_text(log_entry, encoding="utf-8")
    return True


def _iter_dirs(root: Path) -> Iterable[Path]:
    return (p for p in root.iterdir() if p.is_dir())


def _birth_timestamp(path: Path) -> float:
    stat = path.stat()
    # Use birth time when available (macOS), fall back to ctime elsewhere.
    return getattr(stat, "st_birthtime", stat.st_ctime)


def archive_ats_report(folder: Path) -> Path | None:
    """Rename ats_report.json (and _raw.txt) to timestamped backups. Returns archived path or None."""
    report = folder / "ats_report.json"
    if not report.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archived = folder / f"ats_report_{ts}.json"
    report.rename(archived)
    raw = folder / "ats_report_raw.txt"
    if raw.exists():
        raw.rename(folder / f"ats_report_raw_{ts}.txt")
    return archived


def cleanup_ats_files(folder: Path, dry_run: bool = False) -> list[Path]:
    """
    Delete ATS artifacts in `folder` if they exist.

    Returns the list of paths that were deleted (or would be deleted in dry_run).
    """
    targets = [
        folder / "ats_report.json",
        folder / "ats_report_raw.txt",
        folder / "ats_error.log",
    ]

    deleted: list[Path] = []
    for path in targets:
        if not path.exists():
            continue
        deleted.append(path)
        if dry_run:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            # Best-effort cleanup; file might have been removed concurrently.
            pass
    return deleted


def ensure_ats_report_exists(
    folder: Path,
    client: OpenAI,
    model: str,
    ats_prompt: str,
    *,
    verbose: bool = False,
) -> str:
    report_json_path = folder / "ats_report.json"
    raw_path = folder / "ats_report_raw.txt"
    error_log_path = folder / "ats_error.log"

    if report_json_path.exists():
        return "skipped"

    jd_path = folder / "jd.md"
    if not jd_path.exists():
        return "error_missing_jd"

    raw_output: str | None = None

    try:
        jd_text = jd_path.read_text(encoding="utf-8")

        resume_file = _pick_resume(folder)
        if resume_file is None:
            return "error_missing_pdf"

        if resume_file.suffix.lower() == ".pages":
            try:
                resume_text = _read_pages_text(resume_file)
            except (FileNotFoundError, OSError):
                # .pages extraction needs macOS/Pages; fall back to PDF counterpart
                pdf_fallback = resume_file.with_suffix(".pdf")
                if pdf_fallback.exists():
                    resume_text = _read_pdf_text(pdf_fallback)
                else:
                    raise
        else:
            resume_text = _read_pdf_text(resume_file)

        input_text = (
            ats_prompt
            + "\n\nJOB_DESCRIPTION:\n"
            + jd_text
            + "\n\nRESUME:\n"
            + resume_text
        )

        raw_output = _call_openai_responses(client, model, input_text)

        report_obj = _parse_fixed_ats_report(raw_output)
        report_obj["meta"] = {"resume_pdf_filename": resume_file.name, "jd_filename": jd_path.name}

        report_json_path.write_text(
            json.dumps(report_obj, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return "created"
    except MissingDependencyError:
        _print_ats_error(folder, "Missing dependency: pypdf (pip install pypdf)", verbose)
        return "error_openai"
    except Exception as exc:
        raw_path.write_text(raw_output or "", encoding="utf-8")
        error_log_path.write_text(traceback.format_exc(), encoding="utf-8")
        _print_ats_error(folder, f"{type(exc).__name__}: {exc}", verbose)
        return "error_openai"

def _print_ats_error(folder: Path, message: str, verbose: bool) -> None:
    if not verbose:
        return
    error_log_path = folder / "ats_error.log"
    raw_path = folder / "ats_report_raw.txt"
    print(f"ATS error - {folder.name}: {message}", file=sys.stderr)
    print(f"- {error_log_path}", file=sys.stderr)
    print(f"- {raw_path}", file=sys.stderr)


def _pick_resume(folder: Path) -> Path | None:
    files = [p for p in folder.iterdir() if p.is_file()]
    pages_files = [p for p in files if p.suffix.lower() == ".pages"]
    pdf_files = [p for p in files if p.suffix.lower() == ".pdf"]

    # Prefer .pages over .pdf
    if pages_files:
        resume_pages = [p for p in pages_files if "resume" in p.name.lower()]
        candidates = resume_pages if resume_pages else pages_files
        return max(candidates, key=lambda p: p.stat().st_size)

    if pdf_files:
        resume_pdfs = [p for p in pdf_files if "resume" in p.name.lower()]
        candidates = resume_pdfs if resume_pdfs else pdf_files
        return max(candidates, key=lambda p: p.stat().st_size)

    return None


def _read_pages_text(pages_path: Path) -> str:
    """Extract plain text from a .pages file.

    Tries two strategies in order:
    1. ZIP/QuickLook: for older Pages format that bundles QuickLook/Preview.pdf.
    2. AppleScript: exports the document as plain text via Pages on macOS.
    """
    # Strategy 1: old Pages format — QuickLook/Preview.pdf inside the ZIP
    try:
        from pypdf import PdfReader
        with zipfile.ZipFile(str(pages_path), "r") as z:
            preview = next((n for n in z.namelist() if n.lower().endswith("preview.pdf")), None)
            if preview:
                pdf_bytes = z.read(preview)
                pypdf_logger = logging.getLogger("pypdf")
                old_level = pypdf_logger.level
                pypdf_logger.setLevel(logging.ERROR)
                try:
                    reader = PdfReader(BytesIO(pdf_bytes), strict=False)
                finally:
                    pypdf_logger.setLevel(old_level)
                return "\n\n".join(p.extract_text() or "" for p in reader.pages).strip()
    except Exception:
        pass  # fall through to AppleScript

    # Strategy 2: AppleScript PDF export (macOS only, requires Pages)
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise MissingDependencyError(
            "Missing dependency: pypdf. Install it with `pip install pypdf`."
        ) from exc

    abs_path = str(pages_path.resolve())
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = Path(f.name)
    try:
        script = (
            f'tell application "Pages"\n'
            f'  set d to open POSIX file "{abs_path}"\n'
            f'  export d to POSIX file "{str(tmp_path)}" as PDF\n'
            f'  close d saving no\n'
            f'end tell'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise ValueError(
                f"AppleScript export failed for {pages_path.name}: {result.stderr.strip()}"
            )
        pypdf_logger = logging.getLogger("pypdf")
        old_level = pypdf_logger.level
        pypdf_logger.setLevel(logging.ERROR)
        try:
            reader = PdfReader(str(tmp_path), strict=False)
        finally:
            pypdf_logger.setLevel(old_level)
        return "\n\n".join(p.extract_text() or "" for p in reader.pages).strip()
    finally:
        tmp_path.unlink(missing_ok=True)


# Keep old name as alias so any external callers don't break.
_pick_resume_pdf = _pick_resume


def _read_pdf_text(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise MissingDependencyError(
            "Missing dependency: pypdf. Install it with `pip install pypdf` to enable PDF text extraction."
        ) from exc

    # pypdf can emit noisy warnings on slightly malformed PDFs; suppress those while parsing.
    pypdf_logger = logging.getLogger("pypdf")
    old_level = pypdf_logger.level
    pypdf_logger.setLevel(logging.ERROR)
    try:
        reader = PdfReader(str(pdf_path), strict=False)
    finally:
        pypdf_logger.setLevel(old_level)

    page_texts: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            page_texts.append(text)
    return "\n\n".join(page_texts).strip()


def _call_openai_responses(client: OpenAI, model: str, input_text: str) -> str:
    """
    Minimal, predictable OpenAI call for quick ATS insights.
    """
    params: dict = {"model": model, "input": input_text}
    if model.startswith("gpt-5"):
        params["reasoning"] = {"effort": "low"}

    resp = client.responses.create(**params)

    # Prefer SDK convenience property; only fall back to walking output if empty.
    text = getattr(resp, "output_text", "") or ""
    text = str(text).strip()
    if text:
        return text

    text = _extract_response_text(resp)
    if not text:
        raise ValueError("Empty model output (no text content).")
    return text


def _extract_response_text(resp: object) -> str:
    # Prefer SDK convenience property first.
    text = getattr(resp, "output_text", "") or ""
    text = str(text).strip()
    if text:
        return text

    # Fall back to concatenating any text segments in `resp.output`.
    parts: list[str] = []
    output = getattr(resp, "output", None)
    if output is None and isinstance(resp, dict):
        output = resp.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                content = item.get("content")
            else:
                content = getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for c in content:
                if isinstance(c, dict):
                    c_type = c.get("type")
                    if c_type == "refusal":
                        refusal = c.get("refusal")
                        if isinstance(refusal, str) and refusal.strip():
                            parts.append(refusal.strip())
                        continue
                    seg_text = c.get("text")
                    if isinstance(seg_text, str) and seg_text.strip():
                        parts.append(seg_text.strip())
                else:
                    c_type = getattr(c, "type", None)
                    if c_type == "refusal":
                        refusal = getattr(c, "refusal", None)
                        if isinstance(refusal, str) and refusal.strip():
                            parts.append(refusal.strip())
                        continue
                    seg_text = getattr(c, "text", None)
                    if isinstance(seg_text, str) and seg_text.strip():
                        parts.append(seg_text.strip())
    return "\n".join(parts).strip()


def _parse_json_salvage(raw_output: str) -> dict:
    text = raw_output.strip().lstrip("\ufeff")
    if not text:
        raise ValueError("Empty model output (expected JSON object).")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        obj = json.loads(text[start : end + 1])

    if not isinstance(obj, dict):
        raise ValueError("ATS output must be a single JSON object.")

    return obj


def _parse_fixed_ats_report(text: str) -> dict:
    """
    Parse the fixed line-oriented ATS format produced by prompts/ats_eval.md.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Empty model output.")

    # KEY: value map for simple header/summary fields.
    kv: dict[str, str] = {}
    for ln in lines:
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        key = k.strip().upper()
        val = v.strip()
        kv[key] = val

    def get_str(key: str) -> str | None:
        val = kv.get(key)
        if val is None:
            return None
        if val.casefold() == "null" or val == "":
            return None
        return val

    def get_enum(key: str, allowed: set[str]) -> str:
        val = (kv.get(key) or "").strip().casefold()
        if val in allowed:
            return val
        return "unknown"

    def get_float(key: str) -> float | None:
        val = (kv.get(key) or "").strip()
        if not val or val.casefold() == "null":
            return None
        try:
            return float(val)
        except ValueError:
            return None

    def split_list(key: str) -> list[str]:
        val = (kv.get(key) or "").strip()
        if not val or val.casefold() == "null":
            return []
        parts = [p.strip() for p in val.split(";")]
        return [p for p in parts if p]

    requirements: list[dict] = []
    for i in range(1, 9):
        prefix = f"REQ_{i}_"
        req_text = get_str(prefix + "TEXT")
        status_raw = (kv.get(prefix + "STATUS") or "").strip().casefold()
        if status_raw not in ("met", "partial", "missing"):
            raise ValueError(f"Invalid {prefix}STATUS: {status_raw!r}")
        evidence = kv.get(prefix + "EVIDENCE")
        evidence = None if evidence is None else evidence.strip()
        if not evidence or evidence.casefold() == "null":
            evidence_out = None
        else:
            evidence_out = evidence
        rationale = get_str(prefix + "RATIONALE") or ""
        conf = get_float(prefix + "CONFIDENCE")
        if req_text is None:
            raise ValueError(f"Missing {prefix}TEXT")
        if conf is None:
            raise ValueError(f"Missing/invalid {prefix}CONFIDENCE")
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"{prefix}CONFIDENCE out of range: {conf}")
        if status_raw in ("met", "partial") and evidence_out is None:
            raise ValueError(f"{prefix}EVIDENCE must be a resume quote when status is {status_raw}")

        requirements.append(
            {
                "text": req_text,
                "status": status_raw,
                "evidence": evidence_out,
                "rationale": rationale,
                "confidence": conf,
            }
        )

    company = get_str("COMPANY")
    job_title = get_str("JOB_TITLE")
    location = get_str("LOCATION")
    work_mode = get_enum("WORK_MODE", {"onsite", "hybrid", "remote", "unknown"})
    employment_type = get_enum(
        "EMPLOYMENT_TYPE",
        {"full_time", "part_time", "contract", "internship", "unknown"},
    )

    report = {
        "company": company,
        "job_title": job_title,
        "location": location,
        "work_mode": work_mode,
        "employment_type": employment_type,
        "requirements": requirements,
        "screen_out_flags": split_list("SCREEN_OUT_FLAGS"),
        "top_gaps": split_list("TOP_GAPS"),
        "top_strengths": split_list("TOP_STRENGTHS"),
        "rejection_likelihood": get_float("REJECTION_LIKELIHOOD"),
        "notes": get_str("NOTES"),
    }
    if report["rejection_likelihood"] is None:
        raise ValueError("Missing/invalid REJECTION_LIKELIHOOD")
    if not (0.0 <= report["rejection_likelihood"] <= 1.0):
        raise ValueError("REJECTION_LIKELIHOOD out of range")
    return report


def parse_log_md(log_path: Path) -> list[dict]:
    """
    Parse log.md into tolerant, normalized structured entries.

    - Does not modify logs on disk.
    - Keeps the original raw line for traceability.
    """
    if not log_path.exists():
        return []

    entries: list[dict] = []
    seen: set[tuple[str | None, str]] = set()

    for raw_line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw_line.rstrip("\n")
        if not raw.strip():
            continue

        parsed_date, notes = _split_log_line(raw)
        if parsed_date is None:
            event_type = "unknown"
            notes_out = _normalize_notes(raw)
        else:
            notes_out = notes or ""
            event_type = _classify_event_type(notes_out)

        key = (parsed_date, raw)
        if key in seen:
            continue
        seen.add(key)

        entries.append(
            {
                "date": parsed_date,
                "event_type": event_type,
                "notes": notes_out,
                "raw": raw,
            }
        )

    return entries


def summarize_log(entries: list[dict], today: date) -> dict:
    dated: list[tuple[date, dict]] = []
    for entry in entries:
        d = entry.get("date")
        if not isinstance(d, str) or not _is_iso_date(d):
            continue
        try:
            dt = date.fromisoformat(d)
        except ValueError:
            continue
        dated.append((dt, entry))

    dated.sort(key=lambda t: t[0])

    applied_date: date | None = None
    for dt, entry in dated:
        if entry.get("event_type") == "applied":
            applied_date = dt
            break
    if applied_date is None and dated:
        applied_date = dated[0][0]

    first_response_date: date | None = None
    for dt, entry in dated:
        if entry.get("event_type") != "applied":
            first_response_date = dt
            break

    last_event_date: date | None = dated[-1][0] if dated else None

    current_status = "unknown"
    if dated:
        last_type = dated[-1][1].get("event_type")
        if last_type in ("rejection", "screened_out"):
            current_status = "rejected"
        elif last_type == "offer":
            current_status = "offer"
        elif last_type in ("interview", "screening"):
            current_status = "in_process"
        elif last_type == "update":
            current_status = "waiting"
        elif last_type == "applied":
            current_status = "applied"

    def days_between(a: date | None, b: date | None) -> int | None:
        if a is None or b is None:
            return None
        return (b - a).days

    return {
        "applied_date": applied_date.isoformat() if applied_date else None,
        "first_response_date": first_response_date.isoformat() if first_response_date else None,
        "last_event_date": last_event_date.isoformat() if last_event_date else None,
        "current_status": current_status,
        "days_to_first_response": days_between(applied_date, first_response_date),
        "days_since_applied": days_between(applied_date, today),
        "days_since_last_event": days_between(last_event_date, today),
    }


def demo_log_parsing() -> None:
    samples = [
        "2026-02-26 - not moving forward\n2026-02-16 — Applied (jd.md created)\n",
        "2026/02/16: Applied (jd.md created)\n2026/02/20  HR screen\n2026/02/25 – interview with Alex\n",
        "16.02.2026 Applied\n\nFollow up email sent\n2026-02-22 - Unfortunately, we will not proceed\n",
    ]

    today = date(2026, 2, 26)
    for i, text in enumerate(samples, start=1):
        print(f"--- sample {i} ---")
        entries = _parse_log_text(text)
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        print(json.dumps(summarize_log(entries, today), indent=2, ensure_ascii=False))


def _parse_log_text(text: str) -> list[dict]:
    entries: list[dict] = []
    seen: set[tuple[str | None, str]] = set()

    for raw_line in text.splitlines():
        raw = raw_line.rstrip("\n")
        if not raw.strip():
            continue

        parsed_date, notes = _split_log_line(raw)
        if parsed_date is None:
            event_type = "unknown"
            notes_out = _normalize_notes(raw)
        else:
            notes_out = notes or ""
            event_type = _classify_event_type(notes_out)

        key = (parsed_date, raw)
        if key in seen:
            continue
        seen.add(key)

        entries.append(
            {
                "date": parsed_date,
                "event_type": event_type,
                "notes": notes_out,
                "raw": raw,
            }
        )

    return entries


_LOG_DATE_RE = re.compile(
    r"^\s*(?P<date>(?:\d{4}[-/]\d{1,2}[-/]\d{1,2})|(?:\d{2}\.\d{2}\.\d{4}))(?P<rest>.*)$"
)


def _split_log_line(raw: str) -> tuple[str | None, str | None]:
    m = _LOG_DATE_RE.match(raw)
    if not m:
        return None, None

    token = m.group("date")
    rest = (m.group("rest") or "").strip()
    rest = re.sub(r"^\s*[-–—:]+\s*", "", rest)
    rest = _normalize_notes(rest)

    return _normalize_date_token(token), rest


def _normalize_date_token(token: str) -> str | None:
    token = token.strip()
    try:
        if "/" in token:
            y, m, d = token.split("/")
            return date(int(y), int(m), int(d)).isoformat()
        if "-" in token:
            y, m, d = token.split("-")
            return date(int(y), int(m), int(d)).isoformat()
        if "." in token:
            d, m, y = token.split(".")
            return date(int(y), int(m), int(d)).isoformat()
    except Exception:
        return None
    return None


def _is_iso_date(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


def _normalize_notes(s: str) -> str:
    return " ".join(s.strip().split())


def _classify_event_type(text: str) -> str:
    t = text.casefold()

    def has(*needles: str) -> bool:
        return any(n.casefold() in t for n in needles)

    def has_word(word: str) -> bool:
        return re.search(rf"\\b{re.escape(word)}\\b", t) is not None

    # Priority: rejection/screened_out > offer > interview > screening > applied > update > unknown
    rejection_phrases = (
        "not moving forward",
        "no longer be considered",
        "will no longer be considered",
        "hard requirement",
        "cannot consider",
        "won't be considered",
        "unfortunately",
        "absage",
        "leider",
        "nicht berücksichtigt",
        "rejected",
        "declined",
    )
    if has(*rejection_phrases):
        gate_phrases = (
            "visa",
            "sponsorship",
            "work authorization",
            "work authorisation",
            "salary",
            "compensation",
            "pay",
            "rate",
            "language",
            "german",
            "english",
        )
        if has("hard requirement") or has(*gate_phrases):
            return "screened_out"
        return "rejection"
    if has("offer", "contract", "angebot"):
        return "offer"
    if has("interview", "call with", "interview with"):
        return "interview"
    if has("screening") or has_word("hr") or has("recruiter"):
        return "screening"
    if has("applied", "(jd.md created)"):
        return "applied"
    if has("update", "next steps", "waiting", "told", "follow up"):
        return "update"
    return "unknown"


def bootstrap_candidate_profile(resumes_root: Path, client: OpenAI, model: str) -> str:
    """Read all resumes and merge into a comprehensive candidate profile via LLM."""
    # Collect unique resume stems from both .pages and .pdf files
    stems: set[str] = set()
    for ext in ("*.pages", "*.pdf"):
        for f in resumes_root.glob(ext):
            stems.add(f.stem)
    if not stems:
        raise FileNotFoundError(f"No resume files found in {resumes_root}")

    sections = []
    for stem in sorted(stems):
        text = ""
        pages_file = resumes_root / f"{stem}.pages"
        pdf_file = resumes_root / f"{stem}.pdf"
        # Try .pages first (works on macOS), fall back to .pdf (works in Docker)
        if pages_file.exists():
            try:
                text = _read_pages_text(pages_file)
            except Exception:
                pass
        if not text.strip() and pdf_file.exists():
            try:
                text = _read_pdf_text(pdf_file)
            except Exception as exc:
                logging.warning("Failed to extract %s: %s", stem, exc)
        if text.strip():
            sections.append(f"=== RESUME: {stem} ===\n{text}")

    if not sections:
        raise RuntimeError("Could not extract text from any resume")

    combined = "\n\n".join(sections)
    prompt = (
        "You are building a comprehensive candidate profile by merging multiple resume variants.\n"
        "Each resume below targets a different role type but represents the same person.\n\n"
        f"{combined}\n\n"
        "Create a structured candidate profile that captures ALL unique information across all resumes:\n"
        "- Complete work history with all details, metrics, and achievements from any variant\n"
        "- All skills, tools, technologies mentioned anywhere\n"
        "- Education, certifications, languages\n"
        "- Any domain expertise, industry knowledge\n"
        "- Leadership experience, team sizes, budgets\n\n"
        "Output as structured text organized by: Summary, Work History (by company/role, chronological), "
        "Skills & Tools, Education, Domain Expertise.\n"
        "Include EVERY fact and metric — this profile should be a superset of all resumes.\n"
        "Do NOT invent information. Only include what is explicitly stated in the resumes."
    )

    resp = client.responses.create(model=model, input=prompt)
    return getattr(resp, "output_text", "") or ""
