"""
Vercel serverless entrypoint.

Vercel discovers Python functions ONLY inside the `api/` directory
at the project root. It does not scan `backend/`.

So we import the FastAPI `app` object from backend/main.py here.
Vercel's @vercel/python builder looks for a top-level name `app`.
"""

import sys
from pathlib import Path

# Make backend/main.py importable from inside this function.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# noqa silences unused-import linters — Vercel needs this symbol present.
from main import app  # noqa: E402,F401