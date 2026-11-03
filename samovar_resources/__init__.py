"""Read-only resources bundled with both source and wheel installations."""

from pathlib import Path

RESOURCE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = RESOURCE_DIR / "skills"
TEMPLATES_DIR = RESOURCE_DIR / "templates" / "project"
