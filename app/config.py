"""Runtime configuration. Secrets come only from the environment or a local .env file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemma-4-31b-it")
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "")

SLACK_WEBHOOK_SUPPORT = os.getenv("SLACK_WEBHOOK_SUPPORT", "")
SLACK_WEBHOOK_DEV = os.getenv("SLACK_WEBHOOK_DEV", "")

# owner/repo for real bug tickets via the GitHub CLI; empty means "draft only".
GITHUB_REPO = os.getenv("GITHUB_REPO", "")

BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER", "")
BASIC_AUTH_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD", "")

DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "data" / "scriber_care.db")))
DOCS_DIR = ROOT / "docs"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
