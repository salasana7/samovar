"""Project discovery and config loading."""

from pathlib import Path
from typing import Optional
import yaml


CONFIG_FILENAME = "samovar.yaml"


def find_project_dir(start: Optional[Path] = None) -> Path:
    """Walk up from start (or cwd) looking for samovar.yaml. Return its parent."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        if (directory / CONFIG_FILENAME).exists():
            return directory
    raise FileNotFoundError(
        f"No {CONFIG_FILENAME} found in {current} or any parent directory. "
        "Run 'samovar init <name>' to create a project."
    )


def load_config(project_dir: Path) -> dict:
    """Load and return the project's samovar.yaml."""
    config_path = project_dir / CONFIG_FILENAME
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_lexicon(project_dir: Path) -> dict:
    """Load all lexicon files into a dict keyed by filename stem."""
    lexicon_dir = project_dir / "lexicon"
    if not lexicon_dir.exists():
        return {}
    lexicon = {}
    for md_file in sorted(lexicon_dir.glob("*.md")):
        lexicon[md_file.stem] = md_file.read_text()
    return lexicon


def ensure_project_dirs(project_dir: Path) -> None:
    """Ensure all required project subdirectories exist."""
    (project_dir / ".samovar").mkdir(exist_ok=True)
    (project_dir / "data").mkdir(exist_ok=True)
    (project_dir / "reports").mkdir(exist_ok=True)
