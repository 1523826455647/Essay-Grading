import json
from datetime import datetime
from src.api.utils import get_db, generate_uuid
from src.services.grader.types import JudgeResult


def create_submission(uid: str, pid: str, qid: str, user_answer: str):
    db = get_db()
    sid = 'sub_' + generate_uuid()

    db.execute(
        """INSERT INTO submissions (sid, uid, pid, qid, user_answer, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (sid, uid, pid, qid, user_answer, datetime.now().isoformat())
    )
    db.commit()
    return sid


def get_submission(sid: str):
    db = get_db()
    sub = db.execute(
        "SELECT s.*, p.title as paper_title FROM submissions s "
        "JOIN papers p ON s.pid = p.pid WHERE s.sid = ?",
        (sid,)
    ).fetchone()
    return dict(sub) if sub else None


def update_submission_grading(sid: str, score: float, dimension_scores: dict,
                              ai_feedback: str, hit_points: list, missing_points: list,
                              improving_suggestions: str = None):
    db = get_db()
    if isinstance(improving_suggestions, (list, dict)):
        improving_suggestions = json.dumps(
            improving_suggestions, ensure_ascii=False
        )
    db.execute(
        """UPDATE submissions SET
           score = ?, dimension_scores = ?, ai_feedback = ?,
           hit_points = ?, missing_points = ?, improving_suggestions = ?,
           graded_at = ?, is_reviewed = 0, needs_review = ?
           WHERE sid = ?""",
        (
            score,
            json.dumps(dimension_scores, ensure_ascii=False),
            ai_feedback,
            json.dumps(hit_points, ensure_ascii=False),
            json.dumps(missing_points, ensure_ascii=False),
            improving_suggestions,
            datetime.now().isoformat(),
            1 if score is not None and score < 60 else 0,
            sid
        )
    )
    db.commit()


def mark_submission_pending_review(
    sid: str,
    message: str = "模型服务暂时不可用，已转入人工复核。",
):
    """Keep a failed grading submission without exposing provider details."""
    db = get_db()
    db.execute(
        """UPDATE submissions SET
           score = NULL, dimension_scores = NULL, ai_feedback = ?,
           hit_points = '[]', missing_points = '[]',
           improving_suggestions = NULL, graded_at = NULL,
           is_reviewed = 0, needs_review = 1
           WHERE sid = ?""",
        (message, sid),
    )
    db.commit()


def create_judgment(sid: str, model_id: str, commit: bool = True) -> str:
    db = get_db()
    judgment_id = 'judgment_' + generate_uuid()
    db.execute(
        """INSERT INTO submission_judgments
           (judgment_id, sid, model_id, status, created_at)
           VALUES (?, ?, ?, 'grading', ?)""",
        (judgment_id, sid, model_id, datetime.now().isoformat()),
    )
    if commit:
        db.commit()
    return judgment_id


def _safe_judgment_result(judgment: JudgeResult) -> dict:
    """Serialize only fields intended for persistence and API responses."""
    return {
        'model_id': judgment.model_id,
        'status': judgment.status,
        'score_rate': judgment.score_rate,
        'dimension_scores': judgment.dimension_scores,
        'hit_points': judgment.hit_points,
        'missing_points': judgment.missing_points,
        'ai_feedback': judgment.ai_feedback,
        'improving_suggestions': judgment.improving_suggestions,
        'error_code': judgment.error_code,
        'latency_ms': judgment.latency_ms,
    }


def complete_judgment(
    judgment_id: str,
    judgment: JudgeResult,
    commit: bool = True,
) -> None:
    result = _safe_judgment_result(judgment)
    db = get_db()
    db.execute(
        """UPDATE submission_judgments SET
           status = 'completed', score = ?, dimension_scores = ?,
           result_json = ?, error_code = NULL, latency_ms = ?
           WHERE judgment_id = ?""",
        (
            judgment.score_rate,
            json.dumps(judgment.dimension_scores, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            judgment.latency_ms,
            judgment_id,
        ),
    )
    if commit:
        db.commit()


def fail_judgment(
    judgment_id: str,
    error_code: str | None,
    latency_ms: int | None = None,
    commit: bool = True,
) -> None:
    db = get_db()
    db.execute(
        """UPDATE submission_judgments SET
           status = 'failed', score = NULL, dimension_scores = NULL,
           result_json = NULL, error_code = ?, latency_ms = ?
           WHERE judgment_id = ?""",
        (error_code or 'internal', latency_ms, judgment_id),
    )
    if commit:
        db.commit()


def get_submission_judgments(sid: str) -> list[dict]:
    rows = get_db().execute(
        """SELECT j.judgment_id, j.model_id, j.status, j.score,
                  j.dimension_scores, j.result_json, j.error_code,
                  j.latency_ms, j.created_at, m.name AS model_name,
                  m.protocol
           FROM submission_judgments j
           JOIN llm_models m ON m.model_id = j.model_id
           WHERE j.sid = ?
           ORDER BY j.created_at ASC, j.judgment_id ASC""",
        (sid,),
    ).fetchall()
    judgments = []
    for row in rows:
        item = dict(row)
        raw_result = item.pop('result_json', None)
        if raw_result:
            try:
                item.update(json.loads(raw_result))
            except (json.JSONDecodeError, TypeError):
                pass
        item['score_rate'] = item.pop('score', None)
        raw_dimensions = item.get('dimension_scores')
        if isinstance(raw_dimensions, str):
            try:
                item['dimension_scores'] = json.loads(raw_dimensions)
            except (json.JSONDecodeError, TypeError):
                item['dimension_scores'] = {}
        elif not isinstance(raw_dimensions, dict):
            item['dimension_scores'] = {}
        judgments.append(item)
    return judgments


def update_submission_aggregate(
    sid: str,
    grading_mode: str,
    aggregate: dict,
    ai_feedback: str = '',
    improving_suggestions: list | None = None,
    commit: bool = True,
) -> None:
    db = get_db()
    score_rate = aggregate.get('score_rate')
    completed = score_rate is not None
    db.execute(
        """UPDATE submissions SET
           score = ?, dimension_scores = ?, ai_feedback = ?,
           hit_points = ?, missing_points = ?, improving_suggestions = ?,
           grading_mode = ?, agreement_rate = ?, score_spread = ?,
           valid_judges = ?, failed_judges = ?, aggregate_json = ?,
           graded_at = ?, is_reviewed = 0, needs_review = ?
           WHERE sid = ?""",
        (
            score_rate,
            json.dumps(aggregate.get('dimension_scores', {}), ensure_ascii=False),
            ai_feedback,
            json.dumps(aggregate.get('hit_points', []), ensure_ascii=False),
            json.dumps(aggregate.get('missing_points', []), ensure_ascii=False),
            json.dumps(improving_suggestions or [], ensure_ascii=False),
            grading_mode,
            aggregate.get('agreement_rate'),
            aggregate.get('score_spread'),
            int(aggregate.get('valid_judges', 0)),
            int(aggregate.get('failed_judges', 0)),
            json.dumps(aggregate, ensure_ascii=False),
            datetime.now().isoformat() if completed else None,
            int(bool(aggregate.get('needs_review', not completed))),
            sid,
        ),
    )
    if commit:
        db.commit()


def clear_submission_judgments(sid: str, commit: bool = True) -> None:
    """Remove prior multi-model judgment rows for a submission."""
    db = get_db()
    db.execute("DELETE FROM submission_judgments WHERE sid = ?", (sid,))
    if commit:
        db.commit()


def prepare_submission_regrade(
    sid: str,
    user_answer: str | None = None,
    commit: bool = True,
) -> None:
    """Reset grading fields and clear old judgments before a re-run."""
    db = get_db()
    clear_submission_judgments(sid, commit=False)
    if user_answer is not None:
        db.execute(
            """UPDATE submissions SET
               user_answer = ?,
               score = NULL,
               dimension_scores = NULL,
               ai_feedback = NULL,
               hit_points = '[]',
               missing_points = '[]',
               improving_suggestions = NULL,
               grading_mode = NULL,
               agreement_rate = NULL,
               score_spread = NULL,
               valid_judges = 0,
               failed_judges = 0,
               aggregate_json = NULL,
               graded_at = NULL,
               is_reviewed = 0,
               needs_review = 0
               WHERE sid = ?""",
            (user_answer, sid),
        )
    else:
        db.execute(
            """UPDATE submissions SET
               score = NULL,
               dimension_scores = NULL,
               ai_feedback = NULL,
               hit_points = '[]',
               missing_points = '[]',
               improving_suggestions = NULL,
               grading_mode = NULL,
               agreement_rate = NULL,
               score_spread = NULL,
               valid_judges = 0,
               failed_judges = 0,
               aggregate_json = NULL,
               graded_at = NULL,
               is_reviewed = 0,
               needs_review = 0
               WHERE sid = ?""",
            (sid,),
        )
    if commit:
        db.commit()


def persist_submission_grading(
    sid: str,
    grading_mode: str,
    judgments: list[JudgeResult],
    aggregate: dict,
    ai_feedback: str = '',
    improving_suggestions: list | None = None,
) -> list[dict]:
    """Persist judgment details and their aggregate as one transaction."""
    db = get_db()
    try:
        # Ensure regrade doesn't append onto previous judgment rows.
        clear_submission_judgments(sid, commit=False)
        for judgment in judgments:
            judgment_id = create_judgment(
                sid, judgment.model_id, commit=False
            )
            if judgment.status == 'completed' and judgment.score_rate is not None:
                complete_judgment(
                    judgment_id, judgment, commit=False
                )
            else:
                fail_judgment(
                    judgment_id,
                    judgment.error_code,
                    judgment.latency_ms,
                    commit=False,
                )
        update_submission_aggregate(
            sid,
            grading_mode,
            aggregate,
            ai_feedback=ai_feedback,
            improving_suggestions=improving_suggestions,
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_submission_judgments(sid)


def get_user_submissions(uid: str, page=1, per_page=20):
    db = get_db()
    offset = (page - 1) * per_page

    total = db.execute(
        "SELECT COUNT(*) FROM submissions WHERE uid = ?", (uid,)
    ).fetchone()[0]

    subs = db.execute(
        """SELECT s.*, p.title as paper_title
           FROM submissions s
           JOIN papers p ON s.pid = p.pid
           WHERE s.uid = ?
           ORDER BY s.created_at DESC
           LIMIT ? OFFSET ?""",
        (uid, per_page, offset)
    ).fetchall()

    return {
        'submissions': [dict(s) for s in subs],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    }


def record_learning(uid: str, action: str, target_id: str, score: float = None):
    db = get_db()
    db.execute(
        """INSERT INTO learning_records (uid, action, target_id, score)
           VALUES (?, ?, ?, ?)""",
        (uid, action, target_id, score)
    )
    db.commit()
