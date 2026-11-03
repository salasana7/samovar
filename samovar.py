#!/usr/bin/env python3
"""Samovar — OSINT research harness that orchestrates AI agents for threat intelligence."""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import yaml
import readline  # noqa: F401 — enables arrow keys/backspace in input()
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from lib.agent import spawn_agent, agent_options
from lib.checkpoint import CheckpointInterrupted, run_checkpoint
from lib.project import (
    CONFIG_FILENAME,
    ensure_project_dirs,
    find_project_dir,
    load_config,
    load_lexicon,
)
from lib.state import State

from samovar_resources import SKILLS_DIR, TEMPLATES_DIR
from lib.validation import (
    decode_collector_json, validate_classifications, validate_investigation, validate_plan, validate_reviews,
)

log = logging.getLogger("samovar")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args):
    """Scaffold and configure a new local Samovar project without agent execution."""
    target = Path.cwd() / args.name
    if target.exists():
        print(f"Error: directory '{args.name}' already exists.", file=sys.stderr)
        sys.exit(1)

    # 1. Scaffold
    shutil.copytree(TEMPLATES_DIR, target)
    config_path = target / CONFIG_FILENAME
    config = load_config(target)
    config.setdefault("project", {})["name"] = target.name
    if getattr(args, "agent", None):
        config["agents"] = {"default": args.agent}
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))

    (target / "data").mkdir(exist_ok=True)
    (target / "reports").mkdir(exist_ok=True)
    (target / ".samovar").mkdir(exist_ok=True)

    # 2. Git init
    subprocess.run(["git", "init", "-q"], cwd=str(target), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(target), capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Initialize samovar project"],
        cwd=str(target), capture_output=True,
    )

    print(f"""
  ✓ Created {target}
  ✓ Initialized git repo

  ___  __ _ _ __ ___   _____   ____ _ _ __
 / __|/ _` | '_ ` _ \\ / _ \\ \\ / / _` | '__|
 \\__ \\ (_| | | | | | | (_) \\ V / (_| | |
 |___/\\__,_|_| |_| |_|\\___/ \\_/ \\__,_|_|
""")

    # 3. Gather local project settings deterministically
    skill_path = SKILLS_DIR / "setup.md"
    skill_text = skill_path.read_text()

    _run_setup_agent(skill_text, str(target))


def _run_setup_agent(skill_text: str, project_dir: str):
    """Configure a local project deterministically, without AI tools or script execution.

    The private function name is retained for compatibility with existing callers.
    """
    questions = [
        "What is this project about?",
        "Existing collector or dataset path? (saved as a hint only; or 'skip')",
        "Your name (for the analyst field)?",
        "Known slang or techniques? (saved as analyst notes; or 'skip')",
    ]
    answers = {}
    for index, question in enumerate(questions):
        print(f"  {question}")
        try:
            answer = input("  > ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise RuntimeError("Setup interrupted; project scaffold is saved and can be configured manually") from exc
        if answer and answer.lower() != "skip":
            answers[index] = answer
    root = Path(project_dir)
    config = load_config(root)
    project = config.setdefault("project", {})
    if 0 in answers:
        project["description"] = answers[0]
    if 2 in answers:
        project["analyst"] = answers[2]
    if 1 in answers:
        config.setdefault("setup", {})["source_path_hint"] = answers[1]
    (root / CONFIG_FILENAME).write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    if 3 in answers:
        with (root / "lexicon" / "corrections.md").open("a") as handle:
            handle.write("\n## Analyst seed notes\n" + answers[3] + "\n")
    print("  Project configuration saved locally.")
    if 1 in answers:
        print("  Source path saved as a hint. Review the collector template and configure sources in samovar.yaml before collecting.")
    else:
        print("  Add a collector in samovar.yaml when ready.")


def cmd_status(args):
    """Show current project state."""
    project_dir = find_project_dir()
    config = load_config(project_dir)
    state = State(project_dir)

    summary = state.summary()
    state.close()

    name = config.get("project", {}).get("name", project_dir.name)
    print(f"\n  Project: {name}")
    print(f"  Directory: {project_dir}\n")
    print(f"  Posts:          {summary['total_posts']}")
    print(f"  Classified:     {summary['classified']}")
    print(f"  Unclassified:   {summary['unclassified']}")
    print(f"  Flagged (low):  {summary['flagged_low_confidence']}")
    print(f"  Investigated:   {summary['investigated']}")
    print(f"  Reviewed:       {summary['reviewed']}")
    print(f"  Pending review: {summary['unreviewed_medium_high']}")
    print(f"  Escalations:    {summary.get('pending_escalations', 0)}")

    if summary["last_run"]:
        print(f"\n  Last run: {summary['last_run']['started_at']} ({summary['last_run']['status']})")
    else:
        print("\n  No runs yet.")
    print()


@contextmanager
def _tracked_run(project_dir: Path, plan=None):
    """Always persist terminal status and close the database, including errors."""
    state = State(project_dir)
    run_id = None
    try:
        run_id = state.start_run(plan)
        yield state, run_id
    except BaseException as exc:
        if run_id is not None:
            status = "interrupted" if isinstance(exc, (CheckpointInterrupted, KeyboardInterrupt, SystemExit)) else "failed"
            state.finish_run(run_id, status)
        raise
    else:
        state.finish_run(run_id, "completed")
    finally:
        state.close()


def _project_context(args=None):
    project_dir = find_project_dir()
    config = load_config(project_dir)
    if not isinstance(config, dict):
        raise ValueError("samovar.yaml must contain a configuration object")
    override = getattr(args, "agent", None)
    if override:
        prior = config.get("agents") or {}
        if not isinstance(prior, dict):
            raise ValueError("agents must be a mapping")
        config["agents"] = {"default": override, "models": prior.get("models", {})}
    ensure_project_dirs(project_dir)
    return project_dir, config, load_lexicon(project_dir)


def cmd_run(args):
    """Full pipeline: validate the coordinator plan, then execute it."""
    project_dir, config, lexicon = _project_context(args)
    with _tracked_run(project_dir) as (state, run_id):
        print("  Consulting coordinator agent...")
        plan = spawn_agent(
            skill="coordinator", **agent_options(config, "coordinator"),
            context={
                "state": state.summary(),
                "config": config,
                "lexicon_summary": {
                    key: {"entry_count": value.count("\n## "), "content": value}
                    for key, value in lexicon.items()
                },
            },
            project_dir=project_dir,
        )
        steps = validate_plan(plan, config)
        state.conn.execute("UPDATE runs SET plan_json = ? WHERE id = ?", (json.dumps(plan), run_id))
        state.conn.commit()
        if not steps:
            print(f"  Coordinator says: {plan.get('reasoning', 'nothing to do')}")
            return
        print(f"  Plan: {plan.get('reasoning', '')}")
        grouped_steps = _group_investigate_steps(steps)
        for step_num, group in enumerate(grouped_steps, 1):
            if isinstance(group, list):
                print(f"\n  [{step_num}/{len(grouped_steps)}] investigate ({len(group)} posts in parallel)...")
                _run_investigate_batch(group, config, lexicon, project_dir, state, run_id)
                lexicon = load_lexicon(project_dir)
                continue
            action, params = group["action"], group.get("params", {})
            print(f"\n  [{step_num}/{len(grouped_steps)}] {action}...")
            if action == "collect":
                _run_collect(params, config, project_dir, state)
            elif action == "classify":
                _run_classify(config, lexicon, project_dir, state, run_id)
            elif action == "investigate":
                _run_investigate(params, config, lexicon, project_dir, state, run_id)
            elif action == "review":
                _run_review(config, lexicon, project_dir, state, run_id)
            elif action == "report":
                _run_report(config, lexicon, project_dir, state)
            elif action == "checkpoint":
                print(f"  Checkpoint: {params.get('reason', 'review requested')}")
                run_checkpoint(state.get_flagged_classifications(), state, project_dir)
            lexicon = load_lexicon(project_dir)
    print("\n  Run completed.\n")


def cmd_collect(args):
    project_dir, config, _ = _project_context(args)
    sources = config.get("sources") or {}
    names = [args.source] if args.source else list(sources)
    with _tracked_run(project_dir, {"direct": "collect"}) as (state, _):
        for name in names:
            _run_collect({"source": name}, config, project_dir, state)


def cmd_classify(args):
    project_dir, config, lexicon = _project_context(args)
    with _tracked_run(project_dir, {"direct": "classify"}) as (state, run_id):
        _run_classify(config, lexicon, project_dir, state, run_id)


def cmd_investigate(args):
    project_dir, config, lexicon = _project_context(args)
    with _tracked_run(project_dir, {"direct": "investigate", "post_id": args.post_id}) as (state, run_id):
        _run_investigate({"post_id": args.post_id, "reason": "manual"}, config, lexicon, project_dir, state, run_id)


def cmd_review(args):
    project_dir = find_project_dir()
    state = State(project_dir)
    try:
        run_checkpoint(state.get_flagged_classifications(), state, project_dir)
    finally:
        state.close()


def cmd_validate(args):
    project_dir, config, lexicon = _project_context(args)
    with _tracked_run(project_dir, {"direct": "validate"}) as (state, run_id):
        _run_review(config, lexicon, project_dir, state, run_id)


def cmd_report(args):
    project_dir, config, lexicon = _project_context(args)
    with _tracked_run(project_dir, {"direct": "report"}) as (state, _):
        _run_report(config, lexicon, project_dir, state)


# ---------------------------------------------------------------------------
# Internal step runners
# ---------------------------------------------------------------------------


def _group_investigate_steps(steps: list[dict]) -> list:
    """Group consecutive investigate steps into batches for parallel execution.

    Returns a mixed list: single step dicts for non-investigate actions,
    and lists of step dicts for consecutive investigate runs.
    """
    grouped = []
    investigate_batch = []

    for step in steps:
        if step["action"] == "investigate":
            investigate_batch.append(step)
        else:
            if investigate_batch:
                # Flush the batch (keep single investigations as plain steps)
                if len(investigate_batch) == 1:
                    grouped.append(investigate_batch[0])
                else:
                    grouped.append(investigate_batch)
                investigate_batch = []
            grouped.append(step)

    # Flush any remaining
    if investigate_batch:
        if len(investigate_batch) == 1:
            grouped.append(investigate_batch[0])
        else:
            grouped.append(investigate_batch)

    return grouped


def _store_investigation(result, post, config, project_dir, state, run_id):
    result = validate_investigation(result, post, config)
    result = {**result, "classification_id": post.get("classification_id")}
    state.apply_investigation(result, run_id)
    post_id = post["post_id"]
    recommendation = result["recommendation"]
    entries = result.get("new_lexicon_entries", [])
    if entries:
        print(f"  Saved {len(entries)} proposed lexicon entries for analyst review; trusted lexicon unchanged.")
    print(f"    Post {post_id}: {recommendation}")


def _investigation_post(state, post_id):
    post = state.get_post(str(post_id))
    if not post:
        raise ValueError(f"Post {post_id} not found")
    post["collected_thread_posts"] = state.get_thread_posts(post["post_id"])
    classification = state.get_classification(post["post_id"])
    if classification:
        post["current_label"] = classification["label"]
        post["classification_id"] = classification["id"]
    return post


def _investigation_context(params, post, lexicon, config):
    return {
        "post": {**post, "current_label": post.get("current_label", ""),
                 "reason": params.get("reason", "flagged for investigation")},
        "thread_url": post.get("thread_url"),
        "thread_posts": post.get("collected_thread_posts", []),
        "network_access": "disabled; only already collected context is available",
        "lexicon": lexicon,
        "taxonomy": config.get("taxonomy", {}),
    }


def _run_investigate_batch(steps, config, lexicon, project_dir, state, run_id):
    """Resolve inputs up front; preserve successes but fail the run on any error."""
    tasks = []
    for step in steps:
        params = step.get("params", {})
        post = _investigation_post(state, params.get("post_id", ""))
        tasks.append((params, post))
    if not tasks:
        return
    with ThreadPoolExecutor(max_workers=min(len(tasks), 5)) as executor:
        futures = {
            executor.submit(
                spawn_agent, skill="investigate", **agent_options(config, "investigate"),
                context=_investigation_context(params, post, lexicon, config),
                project_dir=project_dir,
            ): post for params, post in tasks
        }
        errors = []
        completed = 0
        for future in as_completed(futures):
            post = futures[future]
            try:
                _store_investigation(future.result(), post, config, project_dir, state, run_id)
                completed += 1
            except Exception as exc:
                errors.append(f"{post['post_id']}: {exc}")
        print(f"  Completed {completed} of {len(tasks)} investigations.")
        if errors:
            raise RuntimeError("Investigation failures: " + "; ".join(errors))


def _run_collect(params: dict, config: dict, project_dir: Path, state: State):
    """Ingest a complete, validated JSONL batch or fail without partial writes."""
    source_name = params.get("source")
    sources = config.get("sources") or {}
    if not source_name or source_name not in sources:
        raise ValueError(f"No source config for {source_name!r}")
    source_cfg = sources[source_name]
    script = source_cfg.get("script")
    if not script:
        raise ValueError(f"Source {source_name!r} has no script configured")
    script_path = project_dir / script
    if not script_path.is_file():
        raise FileNotFoundError(f"Collector script not found: {script_path}")
    cmd_args = source_cfg.get("args", [])
    if not isinstance(cmd_args, list) or not all(isinstance(arg, str) for arg in cmd_args):
        raise ValueError("Collector args must be a list of strings")
    cmd = [sys.executable, str(script_path)] + cmd_args
    print(f"  Running collector: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_dir), timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"Collector failed (exit {result.returncode}): {result.stderr[:300]}")
    posts = []
    for lineno, line in enumerate(result.stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            post = decode_collector_json(line)
        except ValueError as exc:
            raise ValueError(f"Invalid collector JSON on line {lineno}") from exc
        if not isinstance(post, dict):
            raise ValueError(f"Collector line {lineno} must be an object")
        post.setdefault("source", source_name)
        if post["source"] != source_name:
            raise ValueError(f"Collector line {lineno} source does not match {source_name!r}")
        if not isinstance(post.get("post_id"), (str, int)) or isinstance(post.get("post_id"), bool) or not str(post["post_id"]).strip():
            raise ValueError(f"Collector line {lineno} requires post_id")
        if "source_post_id" in post and str(post["source_post_id"]) != str(post["post_id"]):
            raise ValueError(f"Collector line {lineno} source_post_id conflicts with post_id")
        if not isinstance(post.get("text"), str):
            raise ValueError(f"Collector line {lineno} requires text")
        if post.get("metadata") is not None and not isinstance(post["metadata"], dict):
            raise ValueError(f"Collector line {lineno} metadata must be an object")
        posts.append(post)
    state.add_posts(posts)
    print(f"  Processed {len(posts)} posts from {source_name}.")


def _run_classify(config: dict, lexicon: dict, project_dir: Path, state: State, run_id: int):
    """Classify unclassified posts via the classify agent, batching as needed."""
    batch_size = 10
    total_classified = 0
    total_flagged = 0

    while True:
        posts = state.get_unclassified_posts(limit=batch_size)
        if not posts:
            break

        # Parse metadata and match lexicon terms deterministically
        for post in posts:
            if post.get("metadata_json"):
                try:
                    post["metadata"] = json.loads(post["metadata_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            post["lexicon_matches"] = _match_lexicon(post.get("text", ""), lexicon)

        # Send lexicon summary (corrections + term list) instead of full markdown
        lexicon_summary = {}
        for k, v in lexicon.items():
            if k == "corrections":
                lexicon_summary[k] = v  # corrections always sent in full
            else:
                # Just send term names — meanings are already in lexicon_matches per post
                import re as _re
                terms = [t.split("(")[0].strip() for t in _re.findall(r'\n## (.+)', v)]
                lexicon_summary[k] = f"Known terms: {', '.join(terms)}" if terms else ""

        print(f"  Classifying batch of {len(posts)} posts...")
        result = spawn_agent(
            skill="classify", **agent_options(config, "classify"),
            context={
                "taxonomy": config.get("taxonomy", {}),
                "lexicon": lexicon_summary,
                "posts": posts,
            },
            project_dir=project_dir,
        )

        classifications = validate_classifications(result, posts, config)
        state.add_classifications(classifications, run_id)
        total_classified += len(classifications)

        flagged = result.get("flagged", [])
        total_flagged += len(flagged)

        candidates = result.get("keyword_candidates", [])
        if candidates:
            print(f"  Found {len(candidates)} keyword candidates.")

        # If we got fewer than batch_size, we're done
        if len(posts) < batch_size:
            break

    if total_classified:
        print(f"  Classified {total_classified} posts total.")
    else:
        print("  No unclassified posts.")
    if total_flagged:
        print(f"  Flagged {total_flagged} items with unknown terms.")


def _run_investigate(params, config, lexicon, project_dir, state, run_id):
    post = _investigation_post(state, params.get("post_id", ""))
    print(f"  Investigating post {post['post_id']}...")
    result = spawn_agent(
        skill="investigate", **agent_options(config, "investigate"),
        context=_investigation_context(params, post, lexicon, config),
        project_dir=project_dir,
    )
    _store_investigation(result, post, config, project_dir, state, run_id)


def _run_review(config, lexicon, project_dir, state, run_id):
    """A missing, invalid, or failed review batch fails the run, never silently skips."""
    all_findings = state.get_unreviewed_findings()
    if not all_findings:
        print("  No unreviewed findings.")
        return
    for i in range(0, len(all_findings), 15):
        batch = all_findings[i:i + 15]
        print(f"  Reviewing {len(batch)} findings...")
        result = spawn_agent(
            skill="review", **agent_options(config, "review"),
            context={"findings": batch, "lexicon": lexicon, "taxonomy": config.get("taxonomy", {})},
            project_dir=project_dir,
        )
        reviews = validate_reviews(result, batch, config)
        state.add_reviews(reviews, run_id)
        print(f"  Reviewed {len(reviews)} findings.")
        escalated = [review for review in reviews if review["status"] == "escalated"]
        if escalated:
            print(f"\n  *** {len(escalated)} ESCALATED finding(s) require analyst attention ***")
            for review in escalated:
                print(f"      Post #{review['post_id']}: {review['reason'][:120]}")


def _run_report(config: dict, lexicon: dict, project_dir: Path, state: State):
    """Generate threat intel report from reviewed findings."""
    findings = state.get_reviewed_findings()
    if not findings:
        print("  No reviewed findings to report on.")
        return

    (project_dir / "reports").mkdir(exist_ok=True)

    print(f"  Generating report from {len(findings)} findings...")
    result = spawn_agent(
        skill="report", **agent_options(config, "report"),
        context={
            "findings": findings,
            "lexicon": lexicon,
            "config": config,
            "report_date": date.today().isoformat(),
        },
        project_dir=project_dir,
    )

    # Harness writes the report — agent only generates content
    if not isinstance(result, dict) or result.get("error") or result.get("is_error"):
        raise ValueError("Report agent returned an error or invalid output")
    report_content = result.get("report_content")
    if not isinstance(report_content, str) or not report_content.strip():
        raise ValueError("Report agent returned no report content")
    report_filename = f"{date.today().isoformat()}_threat_report.md"
    report_path = project_dir / "reports" / report_filename

    # Keep unresolved escalation visibility deterministic, not dependent on model prose.
    escalations = state.get_pending_escalations()
    if escalations:
        warning = "# Unresolved analyst escalations\n\nThese items are not validated findings.\n\n"
        warning += "\n".join(f"- Post {item['post_id']}: {item.get('reason', '')}" for item in escalations)
        report_content = warning + "\n\n---\n\n" + report_content
    report_path.write_text(report_content)
    print(f"  Report written to: reports/{report_filename}")

    print(f"  Summary: {result.get('summary', 'done')}")


def _match_lexicon(text: str, lexicon: dict) -> list[dict]:
    """Deterministically match lexicon terms against post text.

    Returns a list of matches with term and meaning for each hit.
    This runs in the harness, not the agent — guaranteed no misses.
    """
    import re

    matches = []
    text_lower = text.lower()

    for filename, content in lexicon.items():
        # Parse markdown entries: ## term_name
        entries = re.split(r'\n## ', content)
        for entry in entries[1:]:  # skip header before first ##
            lines = entry.strip().split('\n')
            if not lines:
                continue

            # First line is the term (possibly with transliteration in parens)
            term_line = lines[0].strip()
            # Extract the base term (before parenthetical)
            term = re.split(r'\s*\(', term_line)[0].strip()

            if not term:
                continue

            # Check if term appears in text (case-insensitive)
            if term.lower() in text_lower:
                # Extract meaning from the entry
                meaning = ""
                category = ""
                for line in lines[1:]:
                    if line.strip().startswith("- **Meaning:**"):
                        meaning = line.split("**Meaning:**")[1].strip()
                    elif line.strip().startswith("- **Description:**"):
                        meaning = line.split("**Description:**")[1].strip()
                    elif line.strip().startswith("- **Category:**"):
                        category = line.split("**Category:**")[1].strip()

                matches.append({
                    "term": term,
                    "meaning": meaning,
                    "category": category,
                    "source_file": filename,
                })

    return matches


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="samovar",
        description="OSINT research harness — orchestrates AI agents for threat intelligence.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument("--agent", choices=("claude", "codex"), help="override the project's agent backend")
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="scaffold a new project")
    p_init.add_argument("name", help="project directory name")

    # status
    sub.add_parser("status", help="show project state")

    # run
    sub.add_parser("run", help="full pipeline (coordinator → execute)")

    # collect
    p_collect = sub.add_parser("collect", help="run collection only")
    p_collect.add_argument("source", nargs="?", default=None, help="source name (default: all)")

    # classify
    sub.add_parser("classify", help="run classification only")

    # investigate
    p_inv = sub.add_parser("investigate", help="investigate a specific post")
    p_inv.add_argument("post_id", help="post ID to investigate")

    # review
    sub.add_parser("review", help="interactive human review")

    # validate
    sub.add_parser("validate", help="adversarial review of medium/high findings")

    # report
    sub.add_parser("report", help="generate threat intel report")

    for command_parser in sub.choices.values():
        command_parser.add_argument("--agent", choices=("claude", "codex"), default=argparse.SUPPRESS,
                                    help="override the project's agent backend")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(name)s: %(message)s")

    commands = {
        "init": cmd_init,
        "status": cmd_status,
        "run": cmd_run,
        "collect": cmd_collect,
        "classify": cmd_classify,
        "investigate": cmd_investigate,
        "review": cmd_review,
        "validate": cmd_validate,
        "report": cmd_report,
    }

    if args.command in commands:
        try:
            commands[args.command](args)
        except CheckpointInterrupted as exc:
            print(f"Interrupted: {exc}", file=sys.stderr)
            raise SystemExit(130) from exc
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        except KeyboardInterrupt:
            print("Interrupted.", file=sys.stderr)
            raise SystemExit(130)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
