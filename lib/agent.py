"""Claude Code session spawning for AI agents."""

import json
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("samovar")

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# Default tool permissions per agent skill
SKILL_TOOLS = {
    "coordinator": ["Read"],
    "classify": ["Read"],
    "investigate": ["Read", "Bash"],
    "review": ["Read"],
    "report": ["Read"],
}


def spawn_agent(
    skill: str,
    context: dict,
    project_dir: Path,
    allowed_tools: list[str] | None = None,
    retry: bool = True,
) -> dict:
    """Spawn a Claude Code session with a skill and context, return parsed JSON."""

    skill_path = SKILLS_DIR / f"{skill}.md"
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill file not found: {skill_path}")

    skill_text = skill_path.read_text()
    context_json = json.dumps(context, ensure_ascii=False, indent=2)

    # Write context to a temp file so the prompt stays small
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, dir=str(project_dir), prefix=".ctx_"
    ) as f:
        f.write(context_json)
        context_path = f.name

    prompt = (
        f"Read your context from {context_path}. Follow your skill instructions. "
        "Return your output as a single JSON object — no markdown fences, no commentary."
    )

    tools = allowed_tools or SKILL_TOOLS.get(skill, ["Read"])

    cmd = [
        "claude",
        "-p",
        prompt,
        "--system-prompt",
        skill_text,
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--allowedTools",
        ",".join(tools),
    ]

    log.info("Spawning %s agent", skill)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        log.error("Agent %s timed out after 300s", skill)
        if retry:
            log.info("Retrying %s agent once...", skill)
            return spawn_agent(
                skill, context, project_dir, allowed_tools, retry=False
            )
        raise RuntimeError(f"Agent {skill} timed out after retry")

    # Clean up temp file
    try:
        Path(context_path).unlink()
    except OSError:
        pass

    if result.returncode != 0:
        log.error("Agent %s failed (rc=%d): %s", skill, result.returncode, result.stderr[:500])
        if retry:
            log.info("Retrying %s agent once...", skill)
            return spawn_agent(
                skill, context, project_dir, allowed_tools, retry=False
            )
        raise RuntimeError(f"Agent {skill} failed: {result.stderr[:500]}")

    return _parse_output(result.stdout, skill)


def _parse_output(raw: str, skill: str) -> dict:
    """Parse agent stdout as JSON. Handles --output-format json wrapper."""
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Agent {skill} returned non-JSON output: {raw[:300]}") from e

    # --output-format json wraps the result: {"type":"result","result":"..."}
    if isinstance(outer, dict) and outer.get("type") == "result":
        inner = outer.get("result", "")
        if isinstance(inner, dict):
            return inner
        # result is a string containing JSON
        try:
            return json.loads(inner)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Agent {skill} result is not valid JSON: {inner[:300]}"
            ) from e

    # Direct JSON object
    if isinstance(outer, dict):
        return outer

    raise RuntimeError(f"Agent {skill} returned unexpected output format: {raw[:300]}")
