from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable


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
