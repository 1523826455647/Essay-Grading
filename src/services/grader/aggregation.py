"""Deterministic point-level and dimension-level ensemble aggregation."""

from collections import defaultdict

from src.services.grader.point_ids import normalize_key_points
from src.services.grader.types import JudgeResult

VERDICT_RANK = {"none": 0, "partial": 1, "full": 2}


def _round(value):
    return round(float(value), 1)


def _weighted_average(values, weights):
    if not values:
        return None
    total_weight = sum(weights)
    return _round(sum(value * weight for value, weight in zip(values, weights)) / total_weight)


def _point_records(judgment: JudgeResult):
    records = []
    for point in judgment.hit_points:
        item = dict(point)
        item.setdefault("verdict", "full" if item.get("score", 0) else "none")
        records.append(item)
    for point in judgment.missing_points:
        item = dict(point)
        item.setdefault("score", 0)
        item.setdefault("verdict", "none")
        records.append(item)
    return records


def _aggregate_point(point_id, records, weights):
    score_values = [float(record.get("score", 0) or 0) for record, _ in records]
    score_weights = [weight for _, weight in records]
    max_score = max(float(record.get("max_score", 0) or 0) for record, _ in records)
    verdict_weights = defaultdict(float)
    evidence = []
    point_text = point_id
    for (record, weight) in records:
        verdict = record.get("verdict", "none")
        if verdict not in VERDICT_RANK:
            verdict = "none"
        verdict_weights[verdict] += weight
        point_text = record.get("point") or point_text
        if record.get("evidence") and record["evidence"] not in evidence:
            evidence.append(str(record["evidence"]))
    max_vote = max(verdict_weights.values())
    winners = [verdict for verdict, weight in verdict_weights.items() if weight == max_vote]
    verdict = min(winners, key=lambda value: VERDICT_RANK[value])
    return {
        "point_id": point_id,
        "point": point_text,
        "score": _round(sum(value * weight for value, weight in zip(score_values, score_weights)) / sum(score_weights)),
        "max_score": _round(max_score),
        "evidence": "；".join(evidence),
        "verdict": verdict,
        "agreement": _round(max_vote / sum(score_weights)),
    }


def _expected_points(rubric: dict | None) -> dict[str, dict]:
    if not isinstance(rubric, dict):
        return {}
    points = rubric.get('points')
    if points is None and 'key_points' in rubric:
        points = normalize_key_points(rubric)
    if not isinstance(points, list):
        return {}
    expected = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        point_id = point.get('point_id')
        if not isinstance(point_id, str) or not point_id.strip():
            continue
        item = dict(point)
        item['point_id'] = point_id.strip()
        item['max_score'] = item.get('max_score', item.get('score', 0))
        expected[item['point_id']] = item
    return expected


def aggregate_judgments(
    judgments: list[JudgeResult],
    rubric: dict | None = None,
    weights: dict[str, float] | None = None,
    agreement_threshold: float = 0.67,
    spread_threshold: float = 20.0,
) -> dict:
    weights = weights or {}
    valid = [
        judgment
        for judgment in judgments
        if judgment.status == "completed" and judgment.score_rate is not None
    ]
    failed = len(judgments) - len(valid)
    valid_weights = [max(float(weights.get(j.model_id, 1.0)), 0.01) for j in valid]
    score_rate = _weighted_average(
        [float(j.score_rate) for j in valid], valid_weights
    )
    score_values = [float(j.score_rate) for j in valid]
    score_spread = _round(max(score_values) - min(score_values)) if score_values else None

    dimensions = {}
    dimension_names = sorted({key for judgment in valid for key in judgment.dimension_scores})
    for name in dimension_names:
        values = [
            (float(judgment.dimension_scores[name]), weight)
            for judgment, weight in zip(valid, valid_weights)
            if name in judgment.dimension_scores
        ]
        dimensions[name] = _weighted_average(
            [value for value, _ in values], [weight for _, weight in values]
        )

    point_groups = defaultdict(list)
    expected_points = _expected_points(rubric)
    for judgment in valid:
        weight = max(float(weights.get(judgment.model_id, 1.0)), 0.01)
        returned_point_ids = set()
        for point in _point_records(judgment):
            point_id = str(point.get("point_id") or f"legacy_{len(point_groups) + 1}")
            returned_point_ids.add(point_id)
            point_groups[point_id].append((point, weight))
        for point_id, expected in expected_points.items():
            if point_id in returned_point_ids:
                continue
            point_groups[point_id].append(
                (
                    {
                        'point_id': point_id,
                        'point': expected.get('point') or point_id,
                        'score': 0,
                        'max_score': expected.get('max_score', 0),
                        'evidence': '',
                        'verdict': 'none',
                    },
                    weight,
                )
            )
    points = [
        _aggregate_point(point_id, records, weights)
        for point_id, records in sorted(point_groups.items())
    ]
    hit_points = [point for point in points if point["verdict"] != "none"]
    missing_points = [point for point in points if point["verdict"] == "none"]
    agreement_rate = (
        _round(sum(point["agreement"] for point in points) / len(points))
        if points
        else (_round(1 - min(score_spread / 100, 1)) if score_spread is not None else None)
    )
    needs_review = (
        not valid
        or (agreement_rate is not None and agreement_rate < agreement_threshold)
        or (score_spread is not None and score_spread > spread_threshold)
        or failed > 0
    )
    result = {
        "status": "completed" if valid else "pending_review",
        "score_rate": score_rate,
        "dimension_scores": dimensions,
        "hit_points": hit_points,
        "missing_points": missing_points,
        "agreement_rate": agreement_rate,
        "score_spread": score_spread,
        "valid_judges": len(valid),
        "failed_judges": failed,
        "needs_review": needs_review,
    }

    # 大作文两阶段字段：取首个有效评审的锚点/档次/逐段分析透传
    # （作文是主观题，多模型 ensemble 主要取分数平均，文案以一份为准）
    essay_judgment = next(
        (j for j in valid if j.essay_anchor or j.tier or j.paragraph_analysis), None
    )
    if essay_judgment is not None:
        result["essay"] = {
            "tier": essay_judgment.tier,
            "tier_reason": essay_judgment.tier_reason,
            "genre_judgment": essay_judgment.genre_judgment,
            "thesis_comparison": essay_judgment.thesis_comparison,
            "paragraph_analysis": essay_judgment.paragraph_analysis,
            "structure_analysis": essay_judgment.structure_analysis,
            "overall_evaluation": essay_judgment.overall_evaluation,
            "top_improvements": essay_judgment.top_improvements,
            "anchor": essay_judgment.essay_anchor,
            "anchor_from_cache": essay_judgment.anchor_from_cache,
            "panel": essay_judgment.essay_panel,
        }
    return result
