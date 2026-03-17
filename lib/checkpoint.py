"""Interactive checkpoint CLI for human review of flagged items."""

import json
import sys
from pathlib import Path

from lib.state import State


def run_checkpoint(flagged_items: list[dict], state: State, project_dir: Path) -> list[dict]:
    """Present flagged items for human review, grouped by type."""
    if not flagged_items:
        print("\n  No items to review.\n")
        return []

    # Separate into review queues
    unknown_term_items = []
    low_confidence_items = []

    for item in flagged_items:
        terms = _parse_unknown_terms(item.get("unknown_terms_json"))
        if terms:
            item["_parsed_terms"] = terms
            unknown_term_items.append(item)
        elif item.get("confidence") == "low":
            low_confidence_items.append(item)
        # High confidence items don't appear in checkpoint

    corrections = []

    # Queue 1: Unknown terms
    if unknown_term_items:
        print(f"\n{'═' * 60}")
        print(f"  UNKNOWN TERMS: {len(unknown_term_items)} posts with unrecognized slang/jargon")
        print(f"{'═' * 60}\n")
        corrections += _review_items(unknown_term_items, state, project_dir)

    # Queue 2: Low confidence (no unknown terms)
    if low_confidence_items:
        print(f"\n{'═' * 60}")
        print(f"  LOW CONFIDENCE: {len(low_confidence_items)} posts needing analyst judgment")
        print(f"{'═' * 60}\n")
        corrections += _review_items(low_confidence_items, state, project_dir)

    return corrections


def _review_items(items: list[dict], state: State, project_dir: Path) -> list[dict]:
    """Present items one at a time for review."""
    corrections = []
    total = len(items)

    for i, item in enumerate(items, 1):
        post_id = item["post_id"]
        label = item.get("label", "unknown")
        confidence = item.get("confidence", "?")
        severity = item.get("severity", "?")
        text = item.get("text", "")
        url = item.get("url", "")
        evidence = item.get("evidence_en", "")
        parsed_terms = item.get("_parsed_terms", [])

        print(f"  [{i}/{total}] Post #{post_id}")
        print(f"           Label: {label} | Severity: {severity} | Confidence: {confidence}")
        if url:
            print(f"           URL: {url}")
        print(f"           ───────────────────────────────────────")
        print(f"           {text}")
        print(f"           ───────────────────────────────────────")
        if evidence:
            print(f"           Evidence: {evidence}")
        if parsed_terms:
            print(f"           Unknown terms: {', '.join(parsed_terms)}")
        print()
        print("           (a) Accept    (r) Reclassify    (l) Add to lexicon    (s) Skip    (q) Quit")
        print()

        try:
            choice = _prompt("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Review interrupted.\n")
            break

        if choice == "a":
            state.update_classification(post_id, label, "high")
            state.add_checkpoint(post_id, "accept", {"label": label})
            corrections.append({"post_id": post_id, "action": "accept", "label": label})
            print("  ✓ Accepted\n")

        elif choice == "r":
            new_label = _prompt("  New label: ").strip()
            if new_label:
                state.update_classification(post_id, new_label, "high")
                state.add_checkpoint(
                    post_id, "reclassify", {"old_label": label, "new_label": new_label}
                )
                corrections.append(
                    {"post_id": post_id, "action": "reclassify", "old_label": label, "new_label": new_label}
                )
                print(f"  ✓ Reclassified as {new_label}\n")
            else:
                print("  — Skipped (no label entered)\n")

        elif choice == "l":
            correction = _add_to_lexicon(post_id, label, project_dir, state)
            if correction:
                corrections.append(correction)

        elif choice == "q":
            print("  Review ended by user.\n")
            break

        else:
            print("  — Skipped\n")

    return corrections


def _parse_unknown_terms(raw) -> list[str]:
    """Parse unknown_terms_json into a list of readable strings."""
    if not raw:
        return []
    try:
        if isinstance(raw, str):
            terms = json.loads(raw)
        else:
            terms = raw
        if isinstance(terms, list):
            return [str(t) for t in terms]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _add_to_lexicon(post_id: str, current_label: str, project_dir: Path, state: State) -> dict | None:
    """Interactive lexicon entry addition."""
    term = _prompt("  Term: ").strip()
    if not term:
        print("  — Skipped (no term entered)\n")
        return None

    meaning = _prompt("  Meaning: ").strip()
    if not meaning:
        print("  — Skipped (no meaning entered)\n")
        return None

    print("  File: (1) slang  (2) techniques  (3) corrections")
    file_choice = _prompt("  > ").strip()
    file_map = {"1": "slang", "2": "techniques", "3": "corrections"}
    filename = file_map.get(file_choice, "slang")

    lexicon_path = project_dir / "lexicon" / f"{filename}.md"

    # Build entry based on file type
    if filename == "slang":
        entry = f"\n## {term}\n- **Meaning:** {meaning}\n- **Category:** {current_label}\n- **Discovered in:** post #{post_id}\n"
    elif filename == "techniques":
        entry = f"\n## {term}\n- **Description:** {meaning}\n- **Category:** {current_label}\n- **Indicators:** (add manually)\n"
    else:  # corrections
        new_label = _prompt("  Correct label: ").strip() or current_label
        entry = (
            f"\n## {term} misclassified as {current_label}\n"
            f"- **Corrected to:** {new_label}\n"
            f"- **Reason:** {meaning}\n"
        )
        if new_label != current_label:
            state.update_classification(post_id, new_label, "high")

    # Append to lexicon file
    with open(lexicon_path, "a") as f:
        f.write(entry)

    state.add_checkpoint(post_id, "add_to_lexicon", {"term": term, "file": filename})
    print(f"  ✓ Added to lexicon/{filename}.md\n")
    return {"post_id": post_id, "action": "add_to_lexicon", "term": term, "file": filename}


def _prompt(text: str) -> str:
    """Read input, handling non-interactive environments."""
    if not sys.stdin.isatty():
        raise EOFError("Non-interactive environment")
    return input(text)
