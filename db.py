import re
import sqlite3
from datetime import datetime
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          company TEXT,
          job_title TEXT,
          job_url TEXT,
          jd_text TEXT,
          artifact_dir TEXT,
          source_folder TEXT,
          status TEXT NOT NULL DEFAULT 'draft',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
        CREATE INDEX IF NOT EXISTS idx_jobs_job_title ON jobs(job_title);
        CREATE INDEX IF NOT EXISTS idx_jobs_source_folder ON jobs(source_folder);

        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id INTEGER NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,
          note TEXT,
          raw_input TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id);
        CREATE INDEX IF NOT EXISTS idx_events_event_date ON events(event_date);

        CREATE TABLE IF NOT EXISTS conversations (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id     INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
          role       TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
          text       TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_job_id ON conversations(job_id);
        """
    )
    _ensure_columns(conn)
    conn.commit()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "source_folder" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN source_folder TEXT")
    if "location" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN location TEXT")
    if "work_mode" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN work_mode TEXT")
    if "employment_type" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN employment_type TEXT")
    if "selected_resume" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN selected_resume TEXT")
    if "ats_rejection_likelihood" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN ats_rejection_likelihood REAL")
    if "ats_top_gaps" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN ats_top_gaps TEXT")
    if "ats_top_strengths" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN ats_top_strengths TEXT")
    if "ats_screen_out_flags" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN ats_screen_out_flags TEXT")


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def create_job(
    conn: sqlite3.Connection,
    *,
    company: str | None,
    job_title: str | None,
    job_url: str | None,
    jd_text: str,
    artifact_dir: str,
    source_folder: str | None = None,
) -> int:
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO jobs(company, job_title, job_url, jd_text, artifact_dir, source_folder, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (company, job_title, job_url, jd_text, artifact_dir, source_folder, ts, ts),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_job_id_by_source_folder(conn: sqlite3.Connection, source_folder: str) -> int | None:
    row = conn.execute("SELECT id FROM jobs WHERE source_folder = ?", (source_folder,)).fetchone()
    return int(row["id"]) if row else None


def add_event(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    event_date: str,
    event_type: str,
    note: str | None,
    raw_input: str | None,
) -> None:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO events(job_id, event_date, event_type, note, raw_input, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (job_id, event_date, event_type, note, raw_input, ts),
    )
    conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (ts, job_id))
    conn.commit()


def update_job_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    ts = now_iso()
    conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, ts, job_id))
    conn.commit()


def get_effective_status(conn: sqlite3.Connection, job_id: int) -> str:
    """Derive the real status from the event history — never returns 'draft'."""
    rows = conn.execute(
        "SELECT event_type FROM events WHERE job_id = ? ORDER BY event_date DESC, id DESC",
        (job_id,),
    ).fetchall()
    for row in rows:
        et = str(row["event_type"])
        if et in ("rejected", "screened_out"):
            return "rejected"
        if et == "offer":
            return "offer"
        if et.startswith("interview_") or et == "screening":
            return "in_process"
        if et in ("applied", "created"):
            return "applied"
    # Has no events — treat as applied (job exists, so it was at minimum applied)
    return "applied"


def repair_statuses(conn: sqlite3.Connection) -> int:
    """Fix every job whose status is 'draft' by recomputing from events. Returns count changed."""
    rows = conn.execute("SELECT id FROM jobs WHERE status = 'draft'").fetchall()
    count = 0
    ts = now_iso()
    for row in rows:
        job_id = int(row["id"])
        effective = get_effective_status(conn, job_id)
        conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (effective, ts, job_id))
        count += 1
    if count:
        conn.commit()
    return count


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, company, job_title, status, artifact_dir, updated_at FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()


def get_max_interview_stage(conn: sqlite3.Connection, job_id: int) -> int:
    rows = conn.execute(
        "SELECT event_type FROM events WHERE job_id = ? AND event_type LIKE 'interview_%'",
        (job_id,),
    ).fetchall()
    max_stage = 0
    for r in rows:
        et = str(r["event_type"])
        try:
            stage = int(et.split("_", 1)[1])
        except Exception:
            continue
        if 1 <= stage <= 6:
            max_stage = max(max_stage, stage)
    return max_stage


def search_jobs(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[sqlite3.Row]:
    q = f"%{query.strip()}%"
    return list(
        conn.execute(
            """
            SELECT id, company, job_title, status, artifact_dir, updated_at
            FROM jobs
            WHERE company LIKE ? OR job_title LIKE ? OR artifact_dir LIKE ?
            ORDER BY
              -- Open applications first, closed ones last
              CASE status
                WHEN 'applied'    THEN 0
                WHEN 'in_process' THEN 1
                WHEN 'offer'      THEN 2
                WHEN 'rejected'   THEN 3
                ELSE 0
              END ASC,
              -- Within each group: oldest update at top (most likely to need follow-up)
              updated_at ASC
            LIMIT ?
            """,
            (q, q, q, limit),
        ).fetchall()
    )


def get_latest_event(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, event_date, event_type, note, raw_input, created_at
        FROM events
        WHERE job_id = ?
        ORDER BY event_date DESC, id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()


def get_first_event_date_by_type(conn: sqlite3.Connection, job_id: int, event_type: str) -> str | None:
    row = conn.execute(
        """
        SELECT event_date
        FROM events
        WHERE job_id = ? AND event_type = ?
        ORDER BY event_date ASC, id ASC
        LIMIT 1
        """,
        (job_id, event_type),
    ).fetchone()
    return str(row["event_date"]) if row else None


def update_job_details(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    location: str | None,
    work_mode: str | None,
    employment_type: str | None,
    selected_resume: str | None,
) -> None:
    ts = now_iso()
    conn.execute(
        """
        UPDATE jobs
        SET location = ?, work_mode = ?, employment_type = ?, selected_resume = ?, updated_at = ?
        WHERE id = ?
        """,
        (location, work_mode, employment_type, selected_resume, ts, job_id),
    )
    conn.commit()


def store_ats_results(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    rejection_likelihood: float | None,
    top_gaps: list[str],
    top_strengths: list[str],
    screen_out_flags: list[str],
) -> None:
    import json as _json
    ts = now_iso()
    conn.execute(
        """
        UPDATE jobs
        SET ats_rejection_likelihood = ?,
            ats_top_gaps = ?,
            ats_top_strengths = ?,
            ats_screen_out_flags = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            rejection_likelihood,
            _json.dumps(top_gaps),
            _json.dumps(top_strengths),
            _json.dumps(screen_out_flags),
            ts,
            job_id,
        ),
    )
    conn.commit()


def list_events(conn: sqlite3.Connection, job_id: int, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT event_date, event_type, note
            FROM events
            WHERE job_id = ?
            ORDER BY event_date ASC, id ASC
            LIMIT ?
            """,
            (job_id, limit),
        ).fetchall()
    )


_BAD_COMPANY_RE = re.compile(r'implied|job_description|unknown', re.IGNORECASE)
_COMPANY_JUNK_RE = re.compile(r'^(implied|from|job|description|unknown)$', re.IGNORECASE)


def _slug_to_title(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r'[-_]+', slug) if w)


def _clean_company_slug(slug: str) -> str:
    """Title-case a company slug, stopping at the first junk word."""
    words = re.split(r'[-_]+', slug)
    clean = []
    for w in words:
        if _COMPANY_JUNK_RE.match(w):
            break
        clean.append(w.capitalize())
    return " ".join(clean)


def update_job_fields(conn: sqlite3.Connection, job_id: int, *, company=None, job_title=None) -> None:
    ts = now_iso()
    if company is not None:
        conn.execute("UPDATE jobs SET company=?, updated_at=? WHERE id=?", (company, ts, job_id))
    if job_title is not None:
        conn.execute("UPDATE jobs SET job_title=?, updated_at=? WHERE id=?", (job_title, ts, job_id))
    conn.commit()


def save_message(conn: sqlite3.Connection, *, job_id: int, role: str, text: str) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO conversations(job_id, role, text, created_at) VALUES(?, ?, ?, ?)",
        (job_id, role, text, ts),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_conversation(conn: sqlite3.Connection, job_id: int, limit: int = 200) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, role, text, created_at FROM conversations WHERE job_id = ? ORDER BY id ASC LIMIT ?",
        (job_id, limit),
    ).fetchall())


def delete_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Delete a job and all related records (events, conversations via FK CASCADE)."""
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()


def get_full_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT id, company, job_title, status, artifact_dir, updated_at,
                  jd_text, location, work_mode, employment_type, selected_resume,
                  ats_rejection_likelihood, ats_top_gaps, ats_top_strengths, ats_screen_out_flags
           FROM jobs WHERE id = ?""",
        (job_id,),
    ).fetchone()


def repair_bad_company_names(conn: sqlite3.Connection) -> int:
    """Rebuild company/job_title from artifact_dir for rows with NULL or garbled names."""
    rows = conn.execute(
        "SELECT id, company, job_title, artifact_dir FROM jobs"
    ).fetchall()
    count = 0
    for row in rows:
        company = row["company"] or ""
        title = row["job_title"] or ""
        if company and not _BAD_COMPANY_RE.search(company) and title:
            continue  # looks fine
        artifact = row["artifact_dir"] or ""
        folder = Path(artifact).name
        if not folder or folder == ".":
            continue
        parts = folder.split("__", 1)
        new_company = _clean_company_slug(parts[0]) if parts[0] else None
        new_title = _slug_to_title(parts[1]) if len(parts) > 1 else None
        if new_company:
            update_job_fields(
                conn, int(row["id"]),
                company=new_company if not company or _BAD_COMPANY_RE.search(company) else None,
                job_title=new_title if not title else None,
            )
            count += 1
    return count
