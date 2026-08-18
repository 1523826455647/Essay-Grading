"""API for user-uploaded materials + multi-model grading."""

from __future__ import annotations

import json
import logging
import os

from flask import Blueprint, request

from src.api.utils import api_error, api_success, get_db, token_required
from src.services import custom_grade_service, submission_service, weak_point_service, drill_service, diagnosis_service
from src.services.grader.aggregation import aggregate_judgments
from src.services.grader.backends import BackendConfigurationError, get_aggregation_backend, grade_with_backend
from src.services.grader.ensemble import run_ensemble, run_fallback
from src.services.grader.scorer import grade_answer, grade_with_model
from src.services.model_registry import get_model, list_models, MAX_MODELS_PER_SUBMISSION
from src.services.grader.rubric import normalize_question_type

custom_grade_bp = Blueprint('custom_grade', __name__, url_prefix='/api/custom-grade')
logger = logging.getLogger(__name__)


def _multi_model_enabled() -> bool:
    return str(os.getenv('MULTI_MODEL_ENABLED', 'false')).strip().lower() in {
        '1', 'true', 'yes', 'on'
    }


def _score_on_question_scale(score_rate, score_max):
    if score_rate is None:
        return None
    try:
        maximum = float(score_max)
    except (TypeError, ValueError):
        maximum = 100.0
    return round(float(score_rate) * maximum / 100, 1)


def _selected_model_configs(current_user: dict, model_ids) -> list[dict]:
    if model_ids is None:
        models = list_models(public_only=current_user.get('role') not in ('admin', 'super_admin'))
        models = [m for m in models if m.get('enabled')]
        if not models:
            return []
        # default: top priority public models, capped
        return [
            get_model(m['model_id'], include_secret=True)
            for m in models[:MAX_MODELS_PER_SUBMISSION]
            if get_model(m['model_id'], include_secret=True)
        ]

    if not isinstance(model_ids, list) or not model_ids:
        raise ValueError('model_ids must be a non-empty list')

    unique_ids = []
    for model_id in model_ids:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError('model_ids contains an invalid value')
        normalized = model_id.strip()
        if normalized not in unique_ids:
            unique_ids.append(normalized)
    if len(unique_ids) > MAX_MODELS_PER_SUBMISSION:
        raise ValueError('too many selected models')

    is_admin = current_user.get('role') in ('admin', 'super_admin')
    configs = []
    for model_id in unique_ids:
        model = get_model(model_id, include_secret=True)
        if (
            not model
            or not model.get('enabled')
            or (not is_admin and not model.get('public_visible'))
        ):
            raise ValueError('selected model is unavailable')
        configs.append(model)
    return configs


def _combined_feedback(judgments) -> tuple[str, list]:
    valid = [
        judgment for judgment in judgments
        if judgment.status == 'completed' and judgment.score_rate is not None
    ]
    feedback = next(
        (judgment.ai_feedback for judgment in valid if judgment.ai_feedback),
        '',
    )
    suggestions = []
    for judgment in valid:
        for suggestion in judgment.improving_suggestions or []:
            if suggestion not in suggestions:
                suggestions.append(suggestion)
    return feedback, suggestions


def _record_side_effects(current_user, sid, pid, qid, question, score_rate, dimension_scores, missing_points):
    try:
        submission_service.record_learning(current_user['uid'], 'submit', sid, score_rate)
    except Exception:
        logger.exception('learning record failed')
    for missing in missing_points or []:
        try:
            weak_point_service.record_weak_point(
                current_user['uid'], missing, topic_tag=question.get('type')
            )
        except Exception:
            pass
    try:
        drill_service.record_drill(
            uid=current_user['uid'],
            question_type=normalize_question_type(question.get('type'), question.get('stem', '')),
            pid=pid,
            qid=qid,
            sid=sid,
            score=score_rate,
            dimension_scores=dimension_scores,
        )
    except Exception:
        logger.exception('drill record failed')
    try:
        diagnosis_service.generate_diagnostic_report(current_user['uid'], sid)
    except Exception:
        pass
    if current_user.get('role') not in ('admin', 'super_admin', 'vip'):
        if not current_user.get('free_trial_used'):
            try:
                db = get_db()
                db.execute(
                    "UPDATE users SET free_trial_used = 1 WHERE uid = ?",
                    (current_user['uid'],),
                )
                db.commit()
            except Exception:
                pass


@custom_grade_bp.route('', methods=['POST'])
@token_required
def create_custom_grade(current_user):
    data = request.get_json(silent=True) or {}
    material = data.get('material')
    stem = (data.get('stem') or data.get('question') or '').strip()
    user_answer = (data.get('user_answer') or data.get('answer') or '').strip()
    reference_answer = (data.get('reference_answer') or data.get('answer_key') or '').strip()
    question_type = data.get('question_type') or data.get('type') or 'guina'
    score_max = data.get('score_max', 20)
    word_limit = data.get('word_limit') or ''
    key_points = data.get('key_points')
    title = data.get('title') or ''
    requested_mode = data.get('mode')
    requested_model_ids = data.get('model_ids')

    if not material or not stem or not user_answer:
        return api_error('请提供材料、题目和你的答案', 400)
    if len(user_answer) > 20000:
        return api_error('答案过长', 400)

    try:
        created = custom_grade_service.create_custom_paper(
            uid=current_user['uid'],
            material=material,
            stem=stem,
            user_answer=user_answer,
            reference_answer=reference_answer,
            question_type=question_type,
            score_max=score_max,
            word_limit=word_limit,
            key_points=key_points,
            title=title,
        )
    except ValueError as error:
        return api_error(str(error), 400)

    pid = created['pid']
    qid = created['qid']
    question = created['question']
    material_parts = created['material']
    score_max = question.get('score_max', 20)

    sid = submission_service.create_submission(
        current_user['uid'], pid, qid, user_answer
    )

    # Prefer multi-model registry path when models are available / requested.
    use_registry = requested_mode is not None or requested_model_ids is not None
    model_configs = []
    try:
        if use_registry or _multi_model_enabled() or list_models(public_only=True):
            model_configs = _selected_model_configs(current_user, requested_model_ids)
            use_registry = bool(model_configs)
    except (ValueError, RuntimeError):
        return api_error('所选模型不可用', 400)

    if use_registry and model_configs:
        grading_mode = requested_mode or ('ensemble' if len(model_configs) >= 2 and _multi_model_enabled() else 'fallback')
        if grading_mode not in ('fallback', 'ensemble'):
            return api_error('mode must be fallback or ensemble', 400)
        if grading_mode == 'ensemble' and not _multi_model_enabled():
            # allow ensemble when user explicitly uploaded multi-model job and env enables it later;
            # if disabled, fall back to sequential fallback with selected models.
            grading_mode = 'fallback'
        if grading_mode == 'ensemble' and len(model_configs) < 2:
            grading_mode = 'fallback'

        try:
            grading_backend = get_aggregation_backend()
        except BackendConfigurationError:
            grading_backend = None

        if grading_backend and grading_backend.name != 'internal':
            judge_fn = lambda config: grade_with_backend(
                config, question, user_answer, material_parts, backend=grading_backend
            )
        else:
            judge_fn = lambda config: grade_with_model(
                config, question, user_answer, material_parts
            )

        if grading_mode == 'ensemble':
            judgments = run_ensemble(model_configs, judge_fn)
        else:
            judgments = run_fallback(model_configs, judge_fn)

        weights = {
            model['model_id']: float(model.get('weight', 1.0))
            for model in model_configs
        }
        aggregate = aggregate_judgments(judgments, rubric=question, weights=weights)
        feedback, suggestions = _combined_feedback(judgments)
        try:
            judgment_details = submission_service.persist_submission_grading(
                sid,
                grading_mode,
                judgments,
                aggregate,
                ai_feedback=feedback,
                improving_suggestions=suggestions,
            )
        except Exception:
            logger.exception('persist multi-model grading failed')
            submission_service.mark_submission_pending_review(sid)
            return api_success({
                'sid': sid,
                'pid': pid,
                'qid': qid,
                'status': 'pending_review',
                'message': '模型服务暂时不可用，已转入人工复核。',
            })

        if aggregate.get('score_rate') is not None:
            _record_side_effects(
                current_user, sid, pid, qid, question,
                aggregate['score_rate'],
                aggregate.get('dimension_scores', {}),
                aggregate.get('missing_points', []),
            )

        return api_success({
            'sid': sid,
            'pid': pid,
            'qid': qid,
            'title': created['title'],
            'status': aggregate.get('status', 'completed'),
            'grading_mode': grading_mode,
            'score': _score_on_question_scale(aggregate.get('score_rate'), score_max),
            'score_rate': aggregate.get('score_rate'),
            'score_max': score_max,
            'dimension_scores': aggregate.get('dimension_scores', {}),
            'agreement_rate': aggregate.get('agreement_rate'),
            'score_spread': aggregate.get('score_spread'),
            'valid_judges': aggregate.get('valid_judges', 0),
            'failed_judges': aggregate.get('failed_judges', 0),
            'needs_review': bool(aggregate.get('needs_review')),
            'aggregate': aggregate,
            'judgments': judgment_details,
            'ai_feedback': feedback,
            'improving_suggestions': suggestions,
            'hit_points': aggregate.get('hit_points', []),
            'missing_points': aggregate.get('missing_points', []),
        })

    # Single-model legacy path via env default.
    try:
        grading_result = grade_answer(pid, qid, question, user_answer, material_parts)
        submission_service.update_submission_grading(
            sid=sid,
            score=grading_result['score'],
            dimension_scores=grading_result['dimension_scores'],
            ai_feedback=grading_result.get('ai_feedback'),
            hit_points=grading_result.get('hit_points', []),
            missing_points=grading_result.get('missing_points', []),
            improving_suggestions=grading_result.get('improving_suggestions'),
        )
        _record_side_effects(
            current_user, sid, pid, qid, question,
            grading_result['score'],
            grading_result.get('dimension_scores', {}),
            grading_result.get('missing_points', []),
        )
        return api_success({
            'sid': sid,
            'pid': pid,
            'qid': qid,
            'title': created['title'],
            'status': 'completed',
            'grading_mode': 'single',
            'score': _score_on_question_scale(grading_result['score'], score_max),
            'score_rate': grading_result['score'],
            'score_max': score_max,
            'dimension_scores': grading_result.get('dimension_scores', {}),
            'ai_feedback': grading_result.get('ai_feedback'),
            'improving_suggestions': grading_result.get('improving_suggestions'),
            'hit_points': grading_result.get('hit_points', []),
            'missing_points': grading_result.get('missing_points', []),
        })
    except Exception as error:
        logger.warning('custom grade failed: %s', type(error).__name__)
        submission_service.mark_submission_pending_review(sid)
        return api_success({
            'sid': sid,
            'pid': pid,
            'qid': qid,
            'status': 'pending_review',
            'message': '模型服务暂时不可用，已转入人工复核。',
        })
