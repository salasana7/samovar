"""Validate untrusted agent output completely before changing project state."""


import json
import math


class OutputValidationError(ValueError):
    """An agent response does not match its requested work or schema."""


def decode_collector_json(line):
    """Reject ambiguous keys and nonfinite numbers before storing source data."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"Nonfinite JSON value: {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON number is outside finite floating-point range")
        return number

    return json.loads(line, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)


def _object(value, name):
    if not isinstance(value, dict):
        raise OutputValidationError(f"{name} must be an object")
    if value.get("error") or value.get("is_error"):
        raise OutputValidationError(f"{name} reported an error: {value.get('error', 'is_error')}")
    return value


def _text(value, name, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise OutputValidationError(f"{name} must be a nonempty string")


def _enum(value, allowed, name):
    if not isinstance(value, str) or value not in allowed:
        raise OutputValidationError(f"Invalid {name}: {value!r}; expected one of {sorted(allowed)}")


def taxonomy_values(config):
    taxonomy = config.get("taxonomy") or {}
    categories = taxonomy.get("categories", [])
    if not isinstance(categories, list) or not categories:
        raise OutputValidationError("taxonomy.categories must be a nonempty list")
    labels = []
    for category in categories:
        _object(category, "taxonomy category")
        _text(category.get("id"), "taxonomy category id")
        labels.append(category["id"])
    if len(set(labels)) != len(labels):
        raise OutputValidationError("taxonomy category IDs must be unique")
    for field in ("severity", "confidence"):
        values = taxonomy.get(field, ["low", "medium", "high"])
        if not isinstance(values, list) or not values or any(v not in ("low", "medium", "high") for v in values):
            raise OutputValidationError(f"taxonomy.{field} must contain low, medium, or high values")
    return set(labels), set(taxonomy.get("severity", ["low", "medium", "high"])), set(taxonomy.get("confidence", ["low", "medium", "high"]))


def _batch(result, key, inputs):
    _object(result, "agent output")
    rows = result.get(key)
    if not isinstance(rows, list):
        raise OutputValidationError(f"{key} must be an array")
    expected = {str(item["post_id"]) for item in inputs}
    seen = set()
    for row in rows:
        _object(row, f"{key} item")
        post_id = row.get("post_id")
        _text(post_id, "post_id")
        if post_id not in expected or post_id in seen:
            raise OutputValidationError(f"Unexpected or duplicate post_id: {post_id!r}")
        seen.add(post_id)
    if seen != expected:
        raise OutputValidationError(f"{key} must cover every input once; missing {sorted(expected - seen)}")
    return rows


def validate_classifications(result, posts, config):
    rows = _batch(result, "classifications", posts)
    labels, severities, confidences = taxonomy_values(config)
    rows = [dict(row) for row in rows]
    by_id = {row["post_id"]: row for row in rows}
    for row in rows:
        _enum(row.get("label"), labels, "label")
        _enum(row.get("severity"), severities, "severity")
        _enum(row.get("confidence"), confidences, "confidence")
        _text(row.get("evidence_en"), "evidence_en")
        terms = row.get("unknown_terms", [])
        if not isinstance(terms, list):
            raise OutputValidationError("unknown_terms must be an array of strings")
        for term in terms:
            _text(term, "unknown term")
        row["unknown_terms"] = list(terms)
    flagged = result.get("flagged", [])
    if not isinstance(flagged, list):
        raise OutputValidationError("flagged must be an array")
    for flag in flagged:
        _object(flag, "flagged item")
        _text(flag.get("post_id"), "flagged post_id")
        if flag["post_id"] not in by_id:
            raise OutputValidationError("flagged post_id is not in this batch")
        _text(flag.get("term"), "flagged term")
        row = by_id[flag["post_id"]]
        if flag["term"] not in row["unknown_terms"]:
            row["unknown_terms"].append(flag["term"])
    for row in rows:
        if row["unknown_terms"]:
            row["confidence"] = "low"
    return rows


def validate_reviews(result, findings, config):
    rows = _batch(result, "reviews", findings)
    labels, severities, _ = taxonomy_values(config)
    by_id = {str(item["post_id"]): item for item in findings}
    validated = []
    for row in rows:
        row = dict(row)
        original = by_id[row["post_id"]]
        _enum(row.get("status"), {"confirmed", "reclassified", "downgraded", "dismissed", "escalated"}, "review status")
        _text(row.get("reason"), "review reason")
        for key, field in (("original_label", "label"), ("original_severity", "severity")):
            if row.get(key) != original[field]:
                raise OutputValidationError(f"Review {key} does not match requested classification")
        if row.get("revised_label") is not None:
            _enum(row["revised_label"], labels, "revised_label")
        if row.get("revised_severity") is not None:
            _enum(row["revised_severity"], severities, "revised_severity")
        if row["status"] == "reclassified":
            _enum(row.get("revised_label"), labels, "revised_label")
            _enum(row.get("revised_severity"), severities, "revised_severity")
        if row["status"] == "downgraded":
            _enum(row.get("revised_severity"), severities, "revised_severity")
            ranks = {"low": 0, "medium": 1, "high": 2}
            if ranks[row["revised_severity"]] >= ranks[original["severity"]]:
                raise OutputValidationError("A downgrade must reduce severity")
        if row["status"] in {"confirmed", "escalated"}:
            if row.get("revised_label", original["label"]) != original["label"] or row.get("revised_severity", original["severity"]) != original["severity"]:
                raise OutputValidationError("Confirmed/escalated review cannot revise classification")
        row["classification_id"] = original["id"]
        validated.append(row)
    return validated


def validate_investigation(result, post, config):
    _object(result, "investigation")
    if result.get("post_id") != str(post["post_id"]):
        raise OutputValidationError("Investigation post_id does not match requested post")
    labels, _, confidences = taxonomy_values(config)
    _enum(result.get("recommendation"), {"confirm", "reclassify", "dismiss", "needs_human"}, "recommendation")
    _enum(result.get("confidence"), confidences, "confidence")
    _text(result.get("thread_context_summary"), "thread_context_summary")
    for field in ("original_label", "revised_label"):
        if result.get(field):
            _enum(result[field], labels, field)
    if post.get("current_label") and result.get("original_label") != post["current_label"]:
        raise OutputValidationError("Investigation original_label does not match current classification")
    if result["recommendation"] == "reclassify":
        _enum(result.get("revised_label"), labels, "revised_label")
    entries = result.get("new_lexicon_entries", [])
    if not isinstance(entries, list):
        raise OutputValidationError("new_lexicon_entries must be an array")
    for entry in entries:
        _object(entry, "lexicon entry")
        _text(entry.get("term"), "lexicon term")
        _text(entry.get("meaning"), "lexicon meaning")
        _enum(entry.get("confidence"), confidences, "lexicon confidence")
    return result


def validate_plan(plan, config):
    _object(plan, "coordinator output")
    steps = plan.get("steps")
    if not isinstance(steps, list) or len(steps) > 100:
        raise OutputValidationError("Coordinator steps must be an array of at most 100 steps")
    for step in steps:
        _object(step, "step")
        action = step.get("action")
        _enum(action, {"collect", "classify", "investigate", "review", "report", "checkpoint"}, "action")
        params = step.get("params", {})
        _object(params, "step params")
        if action == "collect":
            _text(params.get("source"), "collect source")
            if params["source"] not in (config.get("sources") or {}):
                raise OutputValidationError("Collect source is not configured")
        if action == "investigate":
            _text(params.get("post_id"), "investigate post_id")
        for name in ("thread_url", "reason", "label"):
            if name in params:
                _text(params[name], name)
    return steps
