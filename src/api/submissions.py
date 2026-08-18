from flask import Blueprint, request
import json
import logging
import os

from src.api.utils import api_success, api_error, token_required, optional_token, get_db, clamp_per_page
from src.services import submission_service, paper_service, weak_point_service, drill_service, diagnosis_service
from src.services.grader.scorer import grade_answer, grade_with_model
from src.services.grader.aggregation import aggregate_judgments
from src.services.grader.summarize import summarize_judgments
from src.services.grader.backends import (
    BackendConfigurationError,
    get_aggregation_backend,
    grade_with_backend,
)
from src.services.grader.ensemble import run_ensemble, run_fallback
from src.services.grader.rubric import normalize_question_type
from src.services.auth import is_vip_user
from src.services.model_registry import get_model, MAX_MODELS_PER_SUBMISSION
from src.services import exchange_code_service

submissions_bp = Blueprint('submissions', __name__, url_prefix='/api/submissions')
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
        for suggestion in judgment.improving_suggestions:
            if suggestion not in suggestions:
                suggestions.append(suggestion)
    return feedback, suggestions


def _aggregate_response(aggregate: dict, score_max) -> dict:
    result = dict(aggregate)
    result['score'] = _score_on_question_scale(
        aggregate.get('score_rate'), score_max
    )
    result['score_max'] = score_max
    return result


def _run_side_effect(name: str, operation) -> None:
    try:
        operation()
    except Exception as error:
        try:
            get_db().rollback()
        except Exception:
            pass
        logger.warning(
            "Post-grading %s failed (%s)", name, type(error).__name__
        )


def _record_grading_side_effects(
    current_user: dict,
    sid: str,
    pid: str,
    qid: str,
    question: dict,
    score_rate: float,
    dimension_scores: dict,
    missing_points: list,
) -> None:
    if current_user.get('role') not in ('admin', 'super_admin', 'vip'):
        if not current_user.get('free_trial_used'):
            def mark_trial_used():
                db = get_db()
                db.execute(
                    "UPDATE users SET free_trial_used = 1 WHERE uid = ?",
                    (current_user['uid'],),
                )
                db.commit()

            _run_side_effect('free_trial', mark_trial_used)

    _run_side_effect(
        'learning',
        lambda: submission_service.record_learning(
            current_user['uid'], 'submit', sid, score_rate
        ),
    )
    for missing in missing_points:
        _run_side_effect(
            'weak_point',
            lambda missing=missing: weak_point_service.record_weak_point(
                current_user['uid'], missing,
                topic_tag=question.get('type'),
            ),
        )
    _run_side_effect(
        'drill',
        lambda: drill_service.record_drill(
            uid=current_user['uid'],
            question_type=question.get('type', 'guina'),
            pid=pid,
            qid=qid,
            sid=sid,
            score=score_rate,
            dimension_scores=dimension_scores,
        ),
    )
    _run_side_effect(
        'diagnosis',
        lambda: diagnosis_service.generate_diagnostic_report(
            current_user['uid'], sid
        ),
    )


def _deduct_grading_credits(current_user: dict, judgments: list, model_configs: list = None) -> None:
    """批改成功后扣减积分，只扣成功模型的消耗"""
    if current_user.get('role') in ('admin', 'super_admin'):
        return
    # Only charge for models that actually completed successfully
    success_ids = set()
    if judgments:
        for j in judgments:
            if getattr(j, 'status', None) == 'completed' and getattr(j, 'score_rate', None) is not None:
                if getattr(j, 'model_id', None):
                    success_ids.add(j.model_id)
    if model_configs:
        success_ids &= {m.get('model_id') for m in model_configs if m.get('model_id')}
    if success_ids:
        cost = exchange_code_service.calculate_credit_cost(list(success_ids))
    else:
        cost = 1.0
    try:
        exchange_code_service.deduct_credits(current_user['uid'], cost)
    except Exception as e:
        logger.warning("Credit deduction failed: %s", e)


@submissions_bp.route('', methods=['POST'])
@token_required
def create_submission(current_user):
    data = request.get_json()
    if not data:
        return api_error("请提供答案", 400)

    pid = data.get('pid')
    qid = data.get('qid')
    user_answer = data.get('user_answer', '').strip()
    requested_mode = data.get('mode')
    requested_model_ids = data.get('model_ids')
    uses_model_registry = requested_mode is not None or requested_model_ids is not None

    if not pid or not qid or not user_answer:
        return api_error("缺少必要参数", 400)

    # 检查批改次数（管理员跳过）
    if current_user.get('role') not in ('admin', 'super_admin'):
        model_ids = requested_model_ids or []
        credit_cost = exchange_code_service.calculate_credit_cost(model_ids) if model_ids else 1.0
        balance = exchange_code_service.get_user_credits(current_user['uid'])
        if balance < credit_cost:
            return api_error(
                f"批改次数不足！本次需要 {credit_cost} 次（剩余 {balance} 次），请使用兑换码充值",
                402
            )

    # Get question info
    question = paper_service.get_question_by_qid(pid, qid, include_scoring=True)
    if not question:
        return api_error("题目不存在", 404)

    model_configs = None
    grading_mode = None
    grading_backend = None
    if uses_model_registry:
        grading_mode = requested_mode or 'fallback'
        if grading_mode not in ('fallback', 'ensemble'):
            return api_error('mode must be fallback or ensemble', 400)
        if grading_mode == 'ensemble' and not _multi_model_enabled():
            return api_error('multi-model grading is disabled', 503)
        try:
            model_configs = _selected_model_configs(
                current_user, requested_model_ids
            )
        except (ValueError, RuntimeError):
            return api_error('selected model is unavailable', 400)
        if grading_mode == 'ensemble' and len(model_configs) < 2:
            return api_error(
                'ensemble mode requires 2 to 4 distinct models', 400
            )
        try:
            grading_backend = get_aggregation_backend()
        except BackendConfigurationError:
            return api_error('grading backend is unavailable', 503)

    # Create submission record
    sid = submission_service.create_submission(
        current_user['uid'], pid, qid, user_answer
    )

    # Get paper material
    paper = paper_service.get_paper_by_pid(pid)
    material = json.loads(paper['material']) if paper and paper['material'] else None

    if uses_model_registry:
        if grading_backend.name == 'internal':
            judge_fn = lambda config: grade_with_model(
                config, question, user_answer, material
            )
        else:
            judge_fn = lambda config: grade_with_backend(
                config,
                question,
                user_answer,
                material,
                backend=grading_backend,
            )
        if grading_mode == 'ensemble':
            judgments = run_ensemble(model_configs, judge_fn)
        else:
            judgments = run_fallback(model_configs, judge_fn)

        weights = {
            model['model_id']: float(model.get('weight', 1.0))
            for model in model_configs
        }
        # Use summary LLM to synthesize multi-model results
        aggregate = summarize_judgments(
            judgments, question=question, user_answer=user_answer, material=material
        )
        feedback = aggregate.get('ai_feedback', '')
        suggestions = aggregate.get('improving_suggestions', [])
        try:
            judgment_details = submission_service.persist_submission_grading(
                sid,
                grading_mode,
                judgments,
                aggregate,
                ai_feedback=feedback,
                improving_suggestions=suggestions,
            )
        except Exception as error:
            logger.warning(
                "Multi-model persistence failed for submission %s (%s)",
                sid,
                type(error).__name__,
            )
            submission_service.mark_submission_pending_review(sid)
            score_max = question.get('score_max', 100)
            return api_success({
                'sid': sid,
                'status': 'pending_review',
                'grading_mode': grading_mode,
                'score': None,
                'score_rate': None,
                'score_max': score_max,
                'dimension_scores': {},
                'agreement_rate': None,
                'score_spread': None,
                'valid_judges': 0,
                'failed_judges': len(judgments),
                'needs_review': True,
                'aggregate': None,
                'judgments': [],
            })

        if aggregate.get('score_rate') is not None:
            _record_grading_side_effects(
                current_user=current_user,
                sid=sid,
                pid=pid,
                qid=qid,
                question=question,
                score_rate=aggregate['score_rate'],
                dimension_scores=aggregate.get('dimension_scores', {}),
                missing_points=aggregate.get('missing_points', []),
            )
            _deduct_grading_credits(current_user, judgments, model_configs)

        score_max = question.get('score_max', 100)
        public_aggregate = _aggregate_response(aggregate, score_max)
        return api_success({
            'sid': sid,
            'status': aggregate.get('status', 'completed'),
            'grading_mode': grading_mode,
            'score': public_aggregate['score'],
            'score_rate': aggregate.get('score_rate'),
            'score_max': score_max,
            'dimension_scores': aggregate.get('dimension_scores', {}),
            'agreement_rate': aggregate.get('agreement_rate'),
            'score_spread': aggregate.get('score_spread'),
            'valid_judges': aggregate.get('valid_judges', 0),
            'failed_judges': aggregate.get('failed_judges', 0),
            'needs_review': bool(aggregate.get('needs_review')),
            'aggregate': public_aggregate,
            'judgments': judgment_details,
        })

    # Grade the answer
    try:
        grading_result = grade_answer(pid, qid, question, user_answer, material)

        # Mark free trial as used for non-VIP users
        if current_user.get('role') not in ('admin', 'super_admin', 'vip'):
            if not current_user.get('free_trial_used'):
                db = get_db()
                db.execute("UPDATE users SET free_trial_used = 1 WHERE uid = ?", (current_user['uid'],))
                db.commit()

        # Update submission with grading result
        submission_service.update_submission_grading(
            sid=sid,
            score=grading_result['score'],
            dimension_scores=grading_result['dimension_scores'],
            ai_feedback=grading_result['ai_feedback'],
            hit_points=grading_result.get('hit_points', []),
            missing_points=grading_result.get('missing_points', []),
            improving_suggestions=grading_result.get('improving_suggestions')
        )

        # Record learning
        submission_service.record_learning(
            current_user['uid'], 'submit', sid, grading_result['score']
        )

        # Record weak points
        for missing in grading_result.get('missing_points', []):
            weak_point_service.record_weak_point(
                current_user['uid'], missing,
                topic_tag=question.get('type')
            )

        # Record drill stats for question type
        question_type = question.get('type', 'guina')
        drill_service.record_drill(
            uid=current_user['uid'],
            question_type=question_type,
            pid=pid, qid=qid, sid=sid,
            score=grading_result['score'],
            dimension_scores=grading_result['dimension_scores']
        )

        # Auto-generate diagnostic report
        try:
            diagnosis_service.generate_diagnostic_report(current_user['uid'], sid)
        except Exception:
            pass  # don't fail the submission if diagnosis fails

        score_max = question.get('score_max', 100)
        return api_success({
            'sid': sid,
            'status': 'completed',
            'score': _score_on_question_scale(
                grading_result['score'], score_max
            ),
            'score_rate': grading_result['score'],
            'score_max': score_max,
            'dimension_scores': grading_result['dimension_scores']
        })

    except Exception as e:
        logger.warning(
            "Grading failed for submission %s (%s)", sid, type(e).__name__
        )
        submission_service.mark_submission_pending_review(sid)
        return api_success({
            'sid': sid,
            'status': 'pending_review',
            'message': '模型服务暂时不可用，已转入人工复核。'
        })


@submissions_bp.route('/<sid>', methods=['GET'])
@optional_token
def get_submission(current_user, sid):
    if not current_user:
        return api_error("请先登录查看详细批改结果", 401)
    submission = submission_service.get_submission(sid)
    if not submission:
        return api_error("提交记录不存在", 404)

    # Check ownership
    if submission['uid'] != current_user['uid']:
        return api_error("无权查看此提交", 403)

    # The owner can always inspect their complete grading result.
    is_vip = is_vip_user(current_user)

    question = paper_service.get_question_by_qid(
        submission['pid'], submission['qid']
    ) or {}
    score_max = question.get('score_max', 100)
    result = {
        'sid': submission['sid'],
        'pid': submission['pid'],
        'paper_title': submission['paper_title'],
        'qid': submission['qid'],
        'question_stem': question.get('stem', ''),
        'reference_answer': question.get('reference_answer') or '',
        'user_answer': submission['user_answer'],
        'score': _score_on_question_scale(submission['score'], score_max),
        'score_rate': submission['score'],
        'score_max': score_max,
        'graded_at': submission['graded_at'],
        'created_at': submission['created_at'],
        'is_vip': is_vip,
        'grading_mode': submission.get('grading_mode'),
        'agreement_rate': submission.get('agreement_rate'),
        'score_spread': submission.get('score_spread'),
        'valid_judges': submission.get('valid_judges', 0),
        'failed_judges': submission.get('failed_judges', 0),
        'needs_review': bool(submission.get('needs_review')),
    }
    result['question_type'] = normalize_question_type(
        question.get('type'), question.get('stem', '')
    )
    if submission['graded_at']:
        result['status'] = 'completed'
    elif submission['needs_review']:
        result['status'] = 'pending_review'
    else:
        result['status'] = 'grading'

    result['dimension_scores'] = json.loads(submission['dimension_scores']) if submission['dimension_scores'] else None
    result['ai_feedback'] = submission['ai_feedback']
    result['hit_points'] = json.loads(submission['hit_points']) if submission['hit_points'] else []
    result['missing_points'] = json.loads(submission['missing_points']) if submission['missing_points'] else []
    suggestions = submission['improving_suggestions']
    if suggestions:
        try:
            suggestions = json.loads(suggestions)
        except (json.JSONDecodeError, TypeError):
            pass
    result['improving_suggestions'] = suggestions

    aggregate = None
    if submission.get('aggregate_json'):
        try:
            aggregate = json.loads(submission['aggregate_json'])
        except (json.JSONDecodeError, TypeError):
            aggregate = None
    if aggregate:
        aggregate = _aggregate_response(aggregate, score_max)
    result['aggregate'] = aggregate
    result['judgments'] = submission_service.get_submission_judgments(sid)

    return api_success(result)


@submissions_bp.route('/history', methods=['GET'])
@optional_token
def get_history(current_user):
    if not current_user:
        return api_success({'submissions': [], 'total': 0, 'page': 1, 'pages': 0})
    page = request.args.get('page', 1, type=int)
    per_page = clamp_per_page(request.args.get('limit', 20, type=int))

    result = submission_service.get_user_submissions(
        current_user['uid'], page, per_page
    )
    return api_success(result)


@submissions_bp.route('/<sid>/regrade', methods=['POST'])
@token_required
def regrade_submission(current_user, sid):
    """Re-run grading for an existing submission (same sid)."""
    submission = submission_service.get_submission(sid)
    if not submission:
        return api_error("提交记录不存在", 404)
    if submission['uid'] != current_user['uid'] and current_user.get('role') not in ('admin', 'super_admin'):
        return api_error("无权操作此提交", 403)

    data = request.get_json(silent=True) or {}
    user_answer = (data.get('user_answer') or submission.get('user_answer') or '').strip()
    if not user_answer:
        return api_error("答案不能为空", 400)
    if len(user_answer) > 20000:
        return api_error("答案过长", 400)

    pid = submission['pid']
    qid = submission['qid']
    question = paper_service.get_question_by_qid(pid, qid, include_scoring=True)
    if not question:
        return api_error("题目不存在", 404)

    paper = paper_service.get_paper_by_pid(pid)
    material = json.loads(paper['material']) if paper and paper.get('material') else None
    if question.get('material'):
        material = question.get('material')

    requested_mode = data.get('mode')
    requested_model_ids = data.get('model_ids')

    # Prefer explicit selection; otherwise reuse models from prior judgments.
    if not requested_model_ids:
        prior = submission_service.get_submission_judgments(sid)
        prior_ids = []
        for item in prior:
            mid = item.get('model_id')
            if isinstance(mid, str) and mid and mid not in prior_ids:
                prior_ids.append(mid)
        requested_model_ids = prior_ids or None

    uses_model_registry = (
        requested_mode is not None
        or requested_model_ids is not None
        or _multi_model_enabled()
    )

    # Reset previous grading output before re-running. The original answer is
    # preserved unless the caller explicitly supplies a new one.
    new_answer = (
        user_answer if user_answer != (submission.get('user_answer') or '') else None
    )
    submission_service.prepare_submission_regrade(sid, user_answer=new_answer)

    score_max = question.get('score_max', 100)

    if uses_model_registry:
        grading_mode = requested_mode or submission.get('grading_mode') or ('ensemble' if _multi_model_enabled() else 'fallback')
        if grading_mode not in ('fallback', 'ensemble'):
            grading_mode = 'fallback'
        if grading_mode == 'ensemble' and not _multi_model_enabled():
            grading_mode = 'fallback'

        try:
            if requested_model_ids:
                model_configs = _selected_model_configs(current_user, requested_model_ids)
            else:
                # Fall back to all public/enabled models (capped).
                from src.services.model_registry import list_models
                public_models = [
                    m for m in list_models(
                        public_only=current_user.get('role') not in ('admin', 'super_admin')
                    )
                    if m.get('enabled')
                ]
                model_configs = _selected_model_configs(
                    current_user,
                    [m['model_id'] for m in public_models[:MAX_MODELS_PER_SUBMISSION]] or None,
                )
        except (ValueError, RuntimeError, TypeError):
            # If registry selection fails, try legacy single-model path below.
            model_configs = []

        if model_configs:
            if grading_mode == 'ensemble' and len(model_configs) < 2:
                grading_mode = 'fallback'
            try:
                grading_backend = get_aggregation_backend()
            except BackendConfigurationError:
                grading_backend = None

            if grading_backend and grading_backend.name != 'internal':
                judge_fn = lambda config: grade_with_backend(
                    config, question, user_answer, material, backend=grading_backend
                )
            else:
                judge_fn = lambda config: grade_with_model(
                    config, question, user_answer, material
                )

            if grading_mode == 'ensemble':
                judgments = run_ensemble(model_configs, judge_fn)
            else:
                judgments = run_fallback(model_configs, judge_fn)

            weights = {
                model['model_id']: float(model.get('weight', 1.0))
                for model in model_configs
            }
            # Use summary LLM to synthesize multi-model results
            aggregate = summarize_judgments(
                judgments, question=question, user_answer=user_answer, material=material
            )
            feedback = aggregate.get('ai_feedback', '')
            suggestions = aggregate.get('improving_suggestions', [])
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
                logger.exception('regrade persistence failed')
                submission_service.mark_submission_pending_review(sid)
                return api_success({
                    'sid': sid,
                    'status': 'pending_review',
                    'grading_mode': grading_mode,
                    'score': None,
                    'score_rate': None,
                    'score_max': score_max,
                    'message': '重新批改失败，已转入人工复核。',
                    'needs_review': True,
                    'judgments': [],
                })

            if aggregate.get('score_rate') is not None:
                _record_grading_side_effects(
                    current_user=current_user,
                    sid=sid,
                    pid=pid,
                    qid=qid,
                    question=question,
                    score_rate=aggregate['score_rate'],
                    dimension_scores=aggregate.get('dimension_scores', {}),
                    missing_points=aggregate.get('missing_points', []),
                )
                _deduct_grading_credits(current_user, judgments, model_configs)

            public_aggregate = _aggregate_response(aggregate, score_max)
            return api_success({
                'sid': sid,
                'status': aggregate.get('status', 'completed'),
                'grading_mode': grading_mode,
                'score': public_aggregate['score'],
                'score_rate': aggregate.get('score_rate'),
                'score_max': score_max,
                'dimension_scores': aggregate.get('dimension_scores', {}),
                'agreement_rate': aggregate.get('agreement_rate'),
                'score_spread': aggregate.get('score_spread'),
                'valid_judges': aggregate.get('valid_judges', 0),
                'failed_judges': aggregate.get('failed_judges', 0),
                'needs_review': bool(aggregate.get('needs_review')),
                'aggregate': public_aggregate,
                'judgments': judgment_details,
                'ai_feedback': feedback,
                'improving_suggestions': suggestions,
                'hit_points': aggregate.get('hit_points', []),
                'missing_points': aggregate.get('missing_points', []),
                'user_answer': user_answer,
                'regraded': True,
            })

    # Legacy single-model path
    try:
        grading_result = grade_answer(pid, qid, question, user_answer, material)
        submission_service.update_submission_grading(
            sid=sid,
            score=grading_result['score'],
            dimension_scores=grading_result['dimension_scores'],
            ai_feedback=grading_result.get('ai_feedback'),
            hit_points=grading_result.get('hit_points', []),
            missing_points=grading_result.get('missing_points', []),
            improving_suggestions=grading_result.get('improving_suggestions'),
        )
        _record_grading_side_effects(
            current_user=current_user,
            sid=sid,
            pid=pid,
            qid=qid,
            question=question,
            score_rate=grading_result['score'],
            dimension_scores=grading_result.get('dimension_scores', {}),
            missing_points=grading_result.get('missing_points', []),
        )
        _deduct_grading_credits(current_user, [])
        return api_success({
            'sid': sid,
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
            'user_answer': user_answer,
            'regraded': True,
        })
    except Exception:
        logger.exception('legacy regrade failed')
        submission_service.mark_submission_pending_review(sid)
        return api_success({
            'sid': sid,
            'status': 'pending_review',
            'message': '重新批改失败，已转入人工复核。',
            'needs_review': True,
            'regraded': True,
        })


@submissions_bp.route('/<sid>/feedback', methods=['POST'])
@token_required
def submit_feedback(current_user, sid):
    data = request.get_json()
    if not data or not data.get('text'):
        return api_error("请输入反馈内容", 400)

    db = get_db()
    submission = submission_service.get_submission(sid)
    if not submission or submission['uid'] != current_user['uid']:
        return api_error("提交记录不存在", 404)

    # Log feedback
    db.execute(
        """INSERT INTO admin_logs (admin_uid, action, target_type, target_id, detail)
           VALUES ('system', 'user_feedback', 'submission', ?, ?)""",
        (sid, json.dumps({'uid': current_user['uid'], 'content': data['text']}))
    )
    db.commit()

    return api_success(message="感谢反馈，我们会尽快核实")
