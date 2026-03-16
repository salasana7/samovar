#!/usr/bin/env python3
"""Samovar — OSINT research harness that orchestrates AI agents for threat intelligence."""

import argparse
import json
import logging
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from lib.agent import spawn_agent
from lib.checkpoint import run_checkpoint
from lib.project import (
    CONFIG_FILENAME,
    ensure_project_dirs,
    find_project_dir,
    load_config,
    load_lexicon,
)
from lib.state import State

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "project"

log = logging.getLogger("samovar")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args):
    """Scaffold a new samovar project and launch setup agent."""
    target = Path.cwd() / args.name
    if target.exists():
        print(f"Error: directory '{args.name}' already exists.", file=sys.stderr)
        sys.exit(1)

    # 1. Scaffold
    shutil.copytree(TEMPLATES_DIR, target)
    config_path = target / CONFIG_FILENAME
    text = config_path.read_text()
    text = text.replace('name: ""', f'name: "{args.name}"', 1)
    config_path.write_text(text)

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

    print(f"\n  ✓ Created {target}")
    print(f"  ✓ Initialized git repo")
    print(f"\n  Launching setup agent...\n")

    # 3. Spawn interactive Claude session with setup skill
    skill_path = Path(__file__).resolve().parent / "skills" / "setup.md"
    skill_text = skill_path.read_text()

    subprocess.run(
        [
            "claude",
            "--system-prompt", skill_text,
            "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep",
            "Start setting up this samovar project. Introduce yourself briefly and ask me about my research.",
        ],
        cwd=str(target),
    )


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

    if summary["last_run"]:
        print(f"\n  Last run: {summary['last_run']['started_at']} ({summary['last_run']['status']})")
    else:
        print("\n  No runs yet.")
    print()


def cmd_run(args):
    """Full pipeline: coordinator plans, harness executes."""
    project_dir = find_project_dir()
    config = load_config(project_dir)
    lexicon = load_lexicon(project_dir)
    ensure_project_dirs(project_dir)
    state = State(project_dir)

    summary = state.summary()
    run_id = state.start_run()

    # Step 1: Coordinator decides what to do
    print("  Consulting coordinator agent...")
    lexicon_summary = {}
    for k, v in lexicon.items():
        entry_count = v.count("\n## ")
        lexicon_summary[k] = {"entry_count": entry_count, "content": v}
    # Always send corrections in full — coordinator needs to know known mistakes
    plan = spawn_agent(
        skill="coordinator",
        context={
            "state": summary,
            "config": config,
            "lexicon_summary": lexicon_summary,
        },
        project_dir=project_dir,
    )

    steps = plan.get("steps", [])
    if not steps:
        print(f"  Coordinator says: {plan.get('reasoning', 'nothing to do')}")
        state.finish_run(run_id, "completed_empty")
        state.close()
        return

    print(f"  Plan: {plan.get('reasoning', '')}")
    print(f"  Steps: {len(steps)}")

    state.conn.execute(
        "UPDATE runs SET plan_json = ? WHERE id = ?",
        (json.dumps(plan), run_id),
    )
    state.conn.commit()

    # Step 2: Execute the plan
    # Group consecutive investigate steps for parallel execution
    grouped_steps = _group_investigate_steps(steps)

    step_num = 0
    for group in grouped_steps:
        if isinstance(group, list):
            # Parallel investigate batch
            step_num += 1
            print(f"\n  [{step_num}/{len(grouped_steps)}] investigate ({len(group)} posts in parallel)...")
            try:
                _run_investigate_batch(group, config, lexicon, project_dir, state, run_id)
            except Exception as e:
                log.error("Investigate batch failed: %s", e)
                print(f"  Error in investigate batch: {e}")
                state.finish_run(run_id, "failed_at_investigate")
                state.close()
                sys.exit(1)
            continue

        step = group
        action = step["action"]
        params = step.get("params", {})
        step_num += 1
        print(f"\n  [{step_num}/{len(grouped_steps)}] {action}...")

        try:
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
                flagged = state.get_flagged_classifications()
                corrections = run_checkpoint(flagged, state, project_dir)
                if corrections:
                    lexicon = load_lexicon(project_dir)  # Reload after edits

            else:
                print(f"  Unknown action: {action}, skipping.")

        except Exception as e:
            log.error("Step %s failed: %s", action, e)
            print(f"  Error in {action}: {e}")
            state.finish_run(run_id, f"failed_at_{action}")
            state.close()
            sys.exit(1)

    state.finish_run(run_id, "completed")
    state.close()
    print("\n  Run completed.\n")


def cmd_collect(args):
    """Run collection only (deterministic, no AI)."""
    project_dir = find_project_dir()
    config = load_config(project_dir)
    ensure_project_dirs(project_dir)
    state = State(project_dir)

    source_name = args.source
    sources = config.get("sources", {})

    if source_name:
        if source_name not in sources:
            print(f"Error: source '{source_name}' not in config.", file=sys.stderr)
            sys.exit(1)
        _run_collect({"source": source_name}, config, project_dir, state)
    else:
        for name in sources:
            _run_collect({"source": name}, config, project_dir, state)

    state.close()


def cmd_classify(args):
    """Run classification only (AI, bypasses coordinator)."""
    project_dir = find_project_dir()
    config = load_config(project_dir)
    lexicon = load_lexicon(project_dir)
    ensure_project_dirs(project_dir)
    state = State(project_dir)
    run_id = state.start_run({"direct": "classify"})

    _run_classify(config, lexicon, project_dir, state, run_id)

    state.finish_run(run_id)
    state.close()


def cmd_investigate(args):
    """Investigate a specific post (AI, bypasses coordinator)."""
    project_dir = find_project_dir()
    config = load_config(project_dir)
    lexicon = load_lexicon(project_dir)
    ensure_project_dirs(project_dir)
    state = State(project_dir)
    run_id = state.start_run({"direct": "investigate", "post_id": args.post_id})

    post = state.get_post(args.post_id)
    if not post:
        print(f"Error: post '{args.post_id}' not found.", file=sys.stderr)
        sys.exit(1)

    _run_investigate(
        {"post_id": args.post_id, "thread_url": post.get("thread_url"), "reason": "manual"},
        config, lexicon, project_dir, state, run_id,
    )

    state.finish_run(run_id)
    state.close()


def cmd_review(args):
    """Jump directly to checkpoint CLI for human review."""
    project_dir = find_project_dir()
    state = State(project_dir)

    flagged = state.get_flagged_classifications()
    if not flagged:
        print("\n  No flagged items to review.\n")
    else:
        run_checkpoint(flagged, state, project_dir)

    state.close()


def cmd_report(args):
    """Generate report only (AI, bypasses coordinator)."""
    project_dir = find_project_dir()
    config = load_config(project_dir)
    lexicon = load_lexicon(project_dir)
    ensure_project_dirs(project_dir)
    state = State(project_dir)

    _run_report(config, lexicon, project_dir, state)

    state.close()


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


def _run_investigate_batch(
    steps: list[dict], config: dict, lexicon: dict,
    project_dir: Path, state: State, run_id: int,
):
    """Run multiple investigations in parallel via ThreadPoolExecutor."""

    # Pre-fetch posts in the main thread (sqlite3 connections are not thread-safe)
    tasks = []
    for step in steps:
        params = step.get("params", {})
        post_id = params.get("post_id")
        post = state.get_post(str(post_id))
        if not post:
            print(f"    Post {post_id}: not found, skipping.")
            continue
        tasks.append((params, post))

    if not tasks:
        return

    def _investigate_one(params: dict, post: dict) -> tuple[dict, dict]:
        """Spawn a single investigate agent. Returns (params, result)."""
        thread_url = params.get("thread_url") or post.get("thread_url")
        reason = params.get("reason", "flagged for investigation")

        result = spawn_agent(
            skill="investigate",
            context={
                "post": {**post, "current_label": params.get("label", ""), "reason": reason},
                "thread_url": thread_url,
                "lexicon": lexicon,
                "taxonomy": config.get("taxonomy", {}),
            },
            project_dir=project_dir,
        )
        return params, result

    # Spawn all investigations in parallel
    with ThreadPoolExecutor(max_workers=min(len(tasks), 5)) as executor:
        futures = {
            executor.submit(_investigate_one, params, post): params
            for params, post in tasks
        }

        for future in as_completed(futures):
            params, result = future.result()
            post_id = str(params.get("post_id", "?"))

            if result.get("error"):
                print(f"    Post {post_id}: {result['error']}")
                continue

            state.add_investigation(result, run_id)

            new_entries = result.get("new_lexicon_entries", [])
            if new_entries:
                _append_lexicon_entries(new_entries, project_dir, source_post_id=post_id)

            rec = result.get("recommendation", "?")
            print(f"    Post {post_id}: {rec}")

            if rec == "reclassify" and result.get("revised_label"):
                state.update_classification(
                    post_id, result["revised_label"], result.get("confidence", "medium")
                )

    print(f"  Completed {len(steps)} investigations.")


def _run_collect(params: dict, config: dict, project_dir: Path, state: State):
    """Run a source's collector script and ingest results."""
    source_name = params.get("source")
    sources = config.get("sources", {})

    if not source_name or source_name not in sources:
        print(f"  No source config for '{source_name}', skipping collect.")
        return

    source_cfg = sources[source_name]
    script = source_cfg.get("script")
    if not script:
        print(f"  Source '{source_name}' has no script configured.")
        return

    script_path = project_dir / script
    if not script_path.exists():
        print(f"  Script not found: {script_path}")
        return

    cmd_args = source_cfg.get("args", [])
    cmd = [sys.executable, str(script_path)] + cmd_args

    print(f"  Running collector: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_dir))

    if result.returncode != 0:
        print(f"  Collector failed: {result.stderr[:300]}")
        return

    # Collector writes to its DB; now import into state.
    # Convention: collector outputs JSON lines to stdout with post objects.
    if result.stdout.strip():
        posts = []
        for line in result.stdout.strip().splitlines():
            try:
                post = json.loads(line)
                post.setdefault("source", source_name)
                posts.append(post)
            except json.JSONDecodeError:
                continue
        if posts:
            state.add_posts(posts)
            print(f"  Collected {len(posts)} posts from {source_name}.")
        else:
            print(f"  No new posts from {source_name}.")
    else:
        print(f"  Collector produced no output.")


def _run_classify(config: dict, lexicon: dict, project_dir: Path, state: State, run_id: int):
    """Classify unclassified posts via the classify agent, batching as needed."""
    batch_size = 50
    total_classified = 0
    total_flagged = 0

    while True:
        posts = state.get_unclassified_posts(limit=batch_size)
        if not posts:
            break

        print(f"  Classifying batch of {len(posts)} posts...")
        result = spawn_agent(
            skill="classify",
            context={
                "taxonomy": config.get("taxonomy", {}),
                "lexicon": lexicon,
                "posts": posts,
            },
            project_dir=project_dir,
        )

        classifications = result.get("classifications", [])
        if classifications:
            state.add_classifications(classifications, run_id)
            total_classified += len(classifications)
        else:
            # Agent returned no classifications — break to avoid infinite loop
            log.warning("Classify agent returned no classifications for batch of %d posts", len(posts))
            break

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


def _run_investigate(
    params: dict, config: dict, lexicon: dict, project_dir: Path, state: State, run_id: int
):
    """Investigate a specific post via the investigate agent."""
    post_id = params.get("post_id")
    post = state.get_post(str(post_id))
    if not post:
        print(f"  Post {post_id} not found, skipping investigation.")
        return

    thread_url = params.get("thread_url") or post.get("thread_url")
    reason = params.get("reason", "flagged for investigation")

    print(f"  Investigating post {post_id}...")
    result = spawn_agent(
        skill="investigate",
        context={
            "post": {**post, "current_label": params.get("label", ""), "reason": reason},
            "thread_url": thread_url,
            "lexicon": lexicon,
            "taxonomy": config.get("taxonomy", {}),
        },
        project_dir=project_dir,
    )

    state.add_investigation(result, run_id)

    # Apply new lexicon entries if any
    new_entries = result.get("new_lexicon_entries", [])
    if new_entries:
        _append_lexicon_entries(new_entries, project_dir, source_post_id=str(post_id))
        print(f"  Added {len(new_entries)} new lexicon entries.")

    rec = result.get("recommendation", "?")
    print(f"  Recommendation: {rec}")

    # Update classification if reclassified
    if rec == "reclassify" and result.get("revised_label"):
        state.update_classification(
            str(post_id), result["revised_label"], result.get("confidence", "medium")
        )


def _run_review(config: dict, lexicon: dict, project_dir: Path, state: State, run_id: int):
    """Run adversarial review on unreviewed medium/high findings."""
    findings = state.get_unreviewed_findings()
    if not findings:
        print("  No unreviewed findings.")
        return

    print(f"  Reviewing {len(findings)} findings...")
    result = spawn_agent(
        skill="review",
        context={
            "findings": findings,
            "lexicon": lexicon,
            "taxonomy": config.get("taxonomy", {}),
        },
        project_dir=project_dir,
    )

    reviews = result.get("reviews", [])
    if reviews:
        state.add_reviews(reviews, run_id)
        print(f"  Review: {result.get('summary', f'{len(reviews)} reviewed')}")

        escalated = []
        for r in reviews:
            status = r["status"]
            post_id = str(r["post_id"])

            if status == "reclassified" and r.get("revised_label"):
                state.update_classification(
                    post_id, r["revised_label"], "high", r.get("revised_severity")
                )
            elif status == "downgraded" and r.get("revised_severity"):
                label = r.get("revised_label") or r.get("original_label", "")
                state.update_classification(post_id, label, "high", r["revised_severity"])
            elif status == "escalated":
                escalated.append(r)

        if escalated:
            print(f"\n  *** {len(escalated)} ESCALATED finding(s) — requires immediate analyst attention ***")
            for e in escalated:
                print(f"      Post #{e['post_id']}: {e.get('reason', '')[:120]}")


def _run_report(config: dict, lexicon: dict, project_dir: Path, state: State):
    """Generate threat intel report from reviewed findings."""
    findings = state.get_reviewed_findings()
    if not findings:
        print("  No reviewed findings to report on.")
        return

    (project_dir / "reports").mkdir(exist_ok=True)

    print(f"  Generating report from {len(findings)} findings...")
    result = spawn_agent(
        skill="report",
        context={
            "findings": findings,
            "lexicon": lexicon,
            "config": config,
            "report_date": date.today().isoformat(),
        },
        project_dir=project_dir,
    )

    # Harness writes the report — agent only generates content
    report_content = result.get("report_content", "")
    report_filename = f"{date.today().isoformat()}_threat_report.md"
    report_path = project_dir / "reports" / report_filename

    if report_content:
        report_path.write_text(report_content)
        print(f"  Report written to: reports/{report_filename}")
    else:
        print("  Warning: report agent returned no content.")

    print(f"  Summary: {result.get('summary', 'done')}")


def _append_lexicon_entries(entries: list[dict], project_dir: Path, source_post_id: str = ""):
    """Append new entries to slang.md with provenance tracking."""
    slang_path = project_dir / "lexicon" / "slang.md"
    with open(slang_path, "a") as f:
        for entry in entries:
            term = entry.get("term", "unknown")
            meaning = entry.get("meaning", "")
            category = entry.get("category", "")
            confidence = entry.get("confidence", "")
            f.write(f"\n## {term}\n")
            f.write(f"- **Meaning:** {meaning}\n")
            if category:
                f.write(f"- **Category:** {category}\n")
            if confidence:
                f.write(f"- **Confidence:** {confidence}\n")
            provenance = source_post_id or entry.get("source_post_id", "")
            if provenance:
                f.write(f"- **Discovered in:** post #{provenance}\n")
            f.write(f"- **Added by:** investigate agent (auto-discovered)\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="samovar",
        description="OSINT research harness — orchestrates AI agents for threat intelligence.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
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

    # report
    sub.add_parser("report", help="generate threat intel report")

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
        "report": cmd_report,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
