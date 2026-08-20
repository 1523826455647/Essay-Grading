"""Shared result types for model providers and ensemble grading."""

from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """An error safe to expose to the grading workflow."""

    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass
class ProviderResponse:
    content: str
    latency_ms: int
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PointJudgment:
    point_id: str
    score: float
    max_score: float
    evidence: str
    verdict: str


@dataclass
class JudgeResult:
    model_id: str
    status: str = "completed"
    score_rate: float | None = None
    dimension_scores: dict[str, float] = field(default_factory=dict)
    hit_points: list[dict[str, Any]] = field(default_factory=list)
    missing_points: list[dict[str, Any]] = field(default_factory=list)
    ai_feedback: str = ""
    improving_suggestions: list[Any] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    latency_ms: int | None = None

    # 大作文两阶段批改专有字段
    essay_anchor: dict[str, Any] | None = None
    tier: str = ""
    tier_reason: str = ""
    genre_judgment: dict[str, Any] = field(default_factory=dict)
    thesis_comparison: dict[str, Any] = field(default_factory=dict)
    paragraph_analysis: list[dict[str, Any]] = field(default_factory=list)
    structure_analysis: dict[str, Any] = field(default_factory=dict)
    overall_evaluation: str = ""
    top_improvements: list[Any] = field(default_factory=list)
    anchor_from_cache: bool = False
