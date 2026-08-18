"""Selectable grading backends with an optional AutoRubric adapter."""

from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass
from typing import Any

from src.services.grader.point_ids import normalize_key_points
from src.services.grader.scorer import grade_with_model
from src.services.grader.types import JudgeResult, ProviderError
from src.services.model_registry import (
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    MAX_MODEL_TIMEOUT_SECONDS,
)


class BackendConfigurationError(RuntimeError):
    """Raised when a configured grading backend cannot be loaded safely."""


@dataclass(frozen=True)
class _AutoRubricComponents:
    Rubric: Any
    LLMConfig: Any
    CriterionGrader: Any
    version: str


class InternalAggregationBackend:
    name = "internal"
    version = "1"

    async def grade(
        self,
        model_config: dict,
        question: dict,
        user_answer: str,
        material: list | None,
    ) -> JudgeResult:
        return grade_with_model(model_config, question, user_answer, material)


class AutoRubricAggregationBackend:
    name = "autorubric"

    def __init__(self, components: _AutoRubricComponents):
        self._components = components
        self.version = components.version

    async def grade(
        self,
        model_config: dict,
        question: dict,
        user_answer: str,
        material: list | None,
    ) -> JudgeResult:
        criteria, points = _autorubric_criteria(question)
        llm_config = self._components.LLMConfig(
            model=_litellm_model_name(model_config),
            api_key=str(model_config.get("api_key") or ""),
            api_base=str(model_config.get("base_url") or "").rstrip("/"),
            temperature=0.0,
            timeout=_model_timeout(model_config),
            max_tokens=_max_tokens(model_config),
            max_retries=0,
        )
        grader = self._components.CriterionGrader(
            llm_config=llm_config,
            normalize=True,
            shuffle_options=False,
        )
        rubric = self._components.Rubric.from_dict(criteria)
        started = time.perf_counter()
        try:
            report = await rubric.grade(
                to_grade=user_answer,
                grader=grader,
                query=_grading_query(question, material),
                reference_submission=question.get("model_answer") or None,
            )
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("upstream", "AutoRubric grading failed") from None

        score = getattr(report, "score", None)
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
            or getattr(report, "error", None)
        ):
            raise ProviderError("upstream", "AutoRubric grading failed")

        hit_points, missing_points, reasons = _map_autorubric_report(
            getattr(report, "report", None), points
        )
        suggestions = [
            f"补充或完善：{point['point']}"
            for point in (*hit_points, *missing_points)
            if point["verdict"] != "full"
        ]
        return JudgeResult(
            model_id=str(model_config.get("model_id") or ""),
            score_rate=round(float(score) * 100, 1),
            dimension_scores={},
            hit_points=hit_points,
            missing_points=missing_points,
            ai_feedback="；".join(reasons),
            improving_suggestions=suggestions,
            raw_metadata={
                "backend": self.name,
                "backend_version": self.version,
            },
            latency_ms=round((time.perf_counter() - started) * 1000),
        )


def _load_autorubric() -> _AutoRubricComponents:
    try:
        from autorubric import LLMConfig, Rubric, __version__
        from autorubric.graders import CriterionGrader
    except (ImportError, ModuleNotFoundError):
        raise BackendConfigurationError(
            "AutoRubric backend requires requirements-autorubric.txt"
        ) from None
    return _AutoRubricComponents(
        Rubric=Rubric,
        LLMConfig=LLMConfig,
        CriterionGrader=CriterionGrader,
        version=str(__version__),
    )


def get_aggregation_backend(name: str | None = None):
    selected = str(name or os.getenv("GRADING_BACKEND") or "internal").strip().lower()
    if selected == "internal":
        return InternalAggregationBackend()
    if selected == "autorubric":
        return AutoRubricAggregationBackend(_load_autorubric())
    raise BackendConfigurationError(f"unknown grading backend: {selected}")


def grade_with_backend(
    model_config: dict,
    question: dict,
    user_answer: str,
    material: list | None,
    backend_name: str | None = None,
    backend=None,
) -> JudgeResult:
    backend = backend or get_aggregation_backend(backend_name)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            backend.grade(model_config, question, user_answer, material)
        )
    raise BackendConfigurationError(
        "synchronous grading bridge cannot run inside an active event loop"
    )


def _model_timeout(model_config: dict) -> int:
    try:
        value = int(
            model_config.get("timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS)
        )
    except (TypeError, ValueError):
        raise ProviderError("configuration", "Invalid model timeout") from None
    return max(5, min(value, MAX_MODEL_TIMEOUT_SECONDS))


def _max_tokens(model_config: dict) -> int | None:
    try:
        value = int(model_config.get("max_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(128, min(value, 200000))


def _litellm_model_name(model_config: dict) -> str:
    protocol = str(model_config.get("protocol") or "").strip().lower()
    model_name = str(model_config.get("model_name") or "").strip()
    if protocol not in {"openai", "anthropic"} or not model_name:
        raise ProviderError("configuration", "Invalid AutoRubric model configuration")
    prefix = f"{protocol}/"
    return model_name if model_name.lower().startswith(prefix) else prefix + model_name


def _autorubric_criteria(question: dict) -> tuple[list[dict], dict[str, dict]]:
    criteria = []
    points = {}
    for point in normalize_key_points(question):
        point_id = str(point.get("point_id") or "").strip()
        point_text = str(point.get("point") or point_id).strip()
        raw_max_score = point.get("max_score", point.get("score"))
        try:
            max_score = float(raw_max_score)
        except (TypeError, ValueError):
            raise ProviderError("configuration", "Invalid rubric point") from None
        if not point_id or not point_text or not math.isfinite(max_score) or max_score <= 0:
            raise ProviderError("configuration", "Invalid rubric point")
        points[point_id] = {
            "point_id": point_id,
            "point": point_text,
            "max_score": max_score,
        }
        criteria.append(
            {
                "name": point_id,
                "requirement": point_text,
                "weight": max_score,
                "scale_type": "ordinal",
                "options": [
                    {"label": "none", "value": 0.0},
                    {"label": "partial", "value": 0.5},
                    {"label": "full", "value": 1.0},
                ],
            }
        )
    if not criteria:
        raise ProviderError("configuration", "Rubric has no grading points")
    return criteria, points


def _grading_query(question: dict, material: list | None) -> str:
    stem = str(question.get("stem") or "").strip()
    if not material:
        return stem
    material_text = "\n".join(
        str(item.get("content") if isinstance(item, dict) else item)
        for item in material
    )
    return f"{stem}\n\n给定资料：\n{material_text}".strip()


def _map_autorubric_report(
    reports: Any, expected_points: dict[str, dict]
) -> tuple[list[dict], list[dict], list[str]]:
    if not isinstance(reports, list) or len(reports) != len(expected_points):
        raise ProviderError("response_format", "Invalid AutoRubric report")
    mapped = {}
    reasons = []
    for report in reports:
        point_id = str(getattr(report, "name", "") or "").strip()
        if point_id not in expected_points or point_id in mapped or getattr(report, "error", None):
            raise ProviderError("response_format", "Invalid AutoRubric report")
        choice = getattr(report, "multi_choice_verdict", None)
        label = str(getattr(choice, "selected_label", "") or "").strip().lower()
        value = getattr(choice, "value", None)
        if label not in {"none", "partial", "full"}:
            raise ProviderError("response_format", "Invalid AutoRubric report")
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            raise ProviderError("response_format", "Invalid AutoRubric report") from None
        expected_value = {"none": 0.0, "partial": 0.5, "full": 1.0}[label]
        if not math.isfinite(numeric_value) or not math.isclose(
            numeric_value, expected_value, rel_tol=0, abs_tol=1e-9
        ):
            raise ProviderError("response_format", "Invalid AutoRubric report")
        point = expected_points[point_id]
        reason = str(getattr(report, "reason", "") or "").strip()[:2000]
        if reason:
            reasons.append(reason)
        mapped[point_id] = {
            **point,
            "score": round(point["max_score"] * numeric_value, 1),
            "evidence": reason,
            "verdict": label,
        }
    ordered = [mapped[point_id] for point_id in expected_points]
    return (
        [point for point in ordered if point["verdict"] != "none"],
        [point for point in ordered if point["verdict"] == "none"],
        reasons,
    )
