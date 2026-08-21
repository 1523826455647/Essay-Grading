from flask import Blueprint, request
import json

from src.services import paper_service
from src.services.grader.scorer import grade_answer
from src.api.utils import api_success, api_error, clamp_per_page

papers_bp = Blueprint('papers', __name__, url_prefix='/api/papers')


def _loads_json(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _public_questions(raw_questions):
    """Return exam-facing question fields without scoring keys."""
    questions = _loads_json(raw_questions, [])
    public = []
    if not isinstance(questions, list):
        return public
    for item in questions:
        if not isinstance(item, dict):
            continue
        public.append({
            'qid': item.get('qid'),
            'type': item.get('type'),
            'stem': item.get('stem') or item.get('question_text') or '',
            'score_max': item.get('score_max') or item.get('score') or 0,
            'word_limit': item.get('word_limit') or '',
            'requirement': item.get('requirement') or '',
            'document_type': item.get('document_type') or '',
        })
    return public


def _public_paper(paper: dict, include_material: bool = False) -> dict:
    item = dict(paper)
    item.pop('answer_keys', None)
    questions = _public_questions(item.get('questions'))
    item['questions'] = questions
    item['question_count'] = len(questions)
    material = _loads_json(item.get('material'), [])
    item['material'] = material if include_material else []
    if not include_material:
        item['has_material'] = bool(material)
    tags = _loads_json(item.get('tag'), [])
    item['tag'] = tags if isinstance(tags, list) else []
    return item


@papers_bp.route('', methods=['GET'])
def list_papers():
    exam_type = request.args.get('exam_type')
    year = request.args.get('year', type=int)
    province = request.args.get('province')
    keyword = (request.args.get('keyword') or request.args.get('q') or '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = clamp_per_page(request.args.get('limit', 20, type=int))

    result = paper_service.get_papers(
        exam_type=exam_type,
        year=year,
        province=province,
        keyword=keyword or None,
        page=page,
        per_page=per_page
    )
    if isinstance(result, dict) and 'papers' in result:
        result['papers'] = [
            _public_paper(paper, include_material=False)
            for paper in result['papers']
        ]
    return api_success(result)


@papers_bp.route('/question-search', methods=['GET'])
def question_search():
    """按题目内容反查所属试卷：输入题干关键词，返回命中的题目 + 所属试卷。"""
    q = (request.args.get('q') or '').strip()
    limit = clamp_per_page(request.args.get('limit', 30, type=int))
    if not q:
        return api_success({'matches': [], 'total': 0})
    if len(q) > 200:
        return api_error('关键词过长', 400)
    return api_success(paper_service.search_questions(q, limit=limit))


@papers_bp.route('/meta', methods=['GET'])
def papers_meta():
    """卷库筛选元信息：年份列表、省份列表（含数量）、总数。"""
    return api_success(paper_service.get_papers_meta())


@papers_bp.route('/<pid>', methods=['GET'])
def get_paper(pid):
    paper = paper_service.get_paper_by_pid(pid)
    if not paper:
        return api_error("试卷不存在", 404)
    if paper.get('status') == 'custom':
        return api_error("试卷不存在", 404)
    return api_success(_public_paper(paper, include_material=True))


@papers_bp.route('/<pid>/question/<qid>', methods=['GET'])
def get_question(pid, qid):
    question = paper_service.get_question_by_qid(pid, qid)
    if not question:
        return api_error("题目不存在", 404)
    # Never expose scoring keys / reference answers on the public exam page.
    public = {
        'qid': question.get('qid'),
        'type': question.get('type'),
        'stem': question.get('stem'),
        'score_max': question.get('score_max'),
        'word_limit': question.get('word_limit'),
        'material': question.get('material') or [],
        'material_scope': question.get('material_scope') or '全部材料',
        'requirement': question.get('requirement') or '',
        'document_type': question.get('document_type') or '',
    }
    return api_success(public)


@papers_bp.route('/demo/grade', methods=['POST'])
def demo_grade():
    """免登录试用批改接口"""
    data = request.get_json()
    if not data:
        return api_error("请提供答案", 400)

    pid = data.get('pid')
    qid = data.get('qid')
    user_answer = data.get('user_answer', '').strip()

    if not pid or not qid or not user_answer:
        return api_error("缺少必要参数", 400)

    question = paper_service.get_question_by_qid(pid, qid, include_scoring=True)
    if not question:
        return api_error("题目不存在", 404)

    paper = paper_service.get_paper_by_pid(pid)
    material = json.loads(paper['material']) if paper and paper['material'] else None

    try:
        grading_result = grade_answer(pid, qid, question, user_answer, material)
        return api_success({
            'score': grading_result['score'],
            'dimension_scores': grading_result.get('dimension_scores'),
            'ai_feedback': grading_result.get('ai_feedback'),
            'hit_points': grading_result.get('hit_points', []),
            'missing_points': grading_result.get('missing_points', []),
            'improving_suggestions': grading_result.get('improving_suggestions')
        })
    except Exception as e:
        return api_error("批改失败，请稍后重试", 500)
