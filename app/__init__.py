"""CourtPlan: AI-powered tournament scheduler."""
from pathlib import Path

# Single source of truth for the release version is the VERSION file at the repo root.
__version__ = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
