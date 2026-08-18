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
