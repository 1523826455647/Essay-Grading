"""Stable identifiers for rubric points shared by all grading models."""

import re


def normalize_key_points(question: dict) -> list[dict]:
    """Return copied rubric points with deterministic IDs."""
    qid = re.sub(r"[^A-Za-z0-9_-]+", "_", str(question.get("qid") or "q"))
    points = []
    for index, raw_point in enumerate(question.get("key_points") or [], 1):
        point = dict(raw_point)
        point.setdefault("point_id", f"kp_{qid}_{index}")
        points.append(point)
    return points
