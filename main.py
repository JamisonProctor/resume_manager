import os
from dotenv import load_dotenv
from pathlib import Path
import sys
from openai import OpenAI
from utilities import ensure_log_exists

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

client = OpenAI(api_key=api_key)


def main() -> None:
    missing_jd = []
    created = 0

    for subdir in sorted(p for p in data_path.iterdir() if p.is_dir()):
        try:
            if ensure_log_exists(subdir):
                created += 1
        except FileNotFoundError:
            missing_jd.append(subdir)

    print("Log initialization complete.")
    print(f"Logs initialized: {created}")

    if missing_jd:
        print("Missing jd.md (skipped):", file=sys.stderr)
        for p in missing_jd:
            print(f"- {p}", file=sys.stderr)


if __name__ == "__main__":
    main()
