"""Interactive checkpoint CLI for human review of flagged items."""

import sys
from pathlib import Path

from lib.state import State


def run_checkpoint(flagged_items: list[dict], state: State, project_dir: Path) -> list[dict]:
    """Present flagged items for human review. Returns list of corrections made."""
    if not flagged_items:
        print("\n  No items to review.\n")
        return []

    corrections = []
    total = len(flagged_items)

    print(f"\n{'═' * 50}")
    print(f"  REVIEW: {total} item{'s' if total != 1 else ''} flagged")
    print(f"{'═' * 50}\n")

    for i, item in enumerate(flagged_items, 1):
        post_id = item["post_id"]
        label = item.get("label", "unknown")
        confidence = item.get("confidence", "?")
        text = item.get("text", "")[:200]
        evidence = item.get("evidence_en", "")
        unknown_terms = item.get("unknown_terms_json", "")

        print(f"  [{i}/{total}] Post #{post_id}")
        print(f"           Label: {label} (confidence: {confidence})")
        print(f"           Text: {text}")
        if evidence:
            print(f"           Reason: {evidence}")
        if unknown_terms:
            print(f"           Unknown terms: {unknown_terms}")
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
