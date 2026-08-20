"""
多模型批改汇总 LLM

将多个模型的独立判断整合为统一的批改结果，解决：
1. 各模型 point_id 不一致导致汇聚后要点过少的问题
2. 分歧检测不准确的问题
3. 参考答案对照不清晰的问题
"""

import json
import logging

from src.config import get_llm_config
from src.services.grader.llm_client import call_chat_completion
from src.services.grader.types import JudgeResult

logger = logging.getLogger(__name__)

SUMMARIZE_SYSTEM_PROMPT = """你是申论批改总评人。你的任务是将多个 AI 阅卷人的独立评分综合为一份最终批改报告。

输入：多个独立阅卷人的评分结果（JSON 格式），包含各自的 hit_points、missing_points、dimension_scores、ai_feedback、score_rate。

你的任务：
1. 合并所有阅卷人的 hit_points：相同语义的要点合并，保留最准确的表述和最全的证据
2. 合并所有阅卷人的 missing_points：去重，统一表述
3. 分析真正的分歧点：两个以上阅卷人对同一要点判定不一致的情况
4. 综合所有维度的评分
5. 生成统一的 ai_feedback 和 improving_suggestions
6. 给出最终 score_rate

原则：
- 要点数量应该充分，不要过度合并——每个独立采分点单独列出
- 大部分阅卷人认为命中的要点 → 判定为 hit
- 大部分阅卷人认为遗漏的要点 → 判定为 missing
- 分歧要点标注各模型的不同判定
- feedback 要具体，引用各模型的关键评语
- score_rate 取各模型加权平均，确保与要点判定一致"""


def build_summarize_prompt(judgments: list[JudgeResult], question: dict, user_answer: str, material: list = None) -> list:
    """构建汇总 prompt"""
    judges_data = []
    for j in judgments:
        if j.status != "completed":
            continue
        judges_data.append({
            "model_id": j.model_id,
            "score_rate": j.score_rate,
            "dimension_scores": j.dimension_scores,
            "hit_points": j.hit_points,
            "missing_points": j.missing_points,
            "ai_feedback": j.ai_feedback[:500],
            "improving_suggestions": j.improving_suggestions[:3] if j.improving_suggestions else [],
        })

    prompt = f"""题目：{question.get('stem', '')}
考生答案（{len(user_answer)}字）：{user_answer[:3000]}

以下是 {len(judges_data)} 个独立阅卷人的评分结果：

{json.dumps(judges_data, ensure_ascii=False, indent=2)}

请综合以上结果，按以下 JSON 格式输出最终批改报告：

{{
    "score_rate": 综合百分制得分(0-100),
    "dimension_scores": {{"维度名": 分数, ...}},
    "hit_points": [
        {{
            "point": "采分点内容",
            "score": 综合得分,
            "max_score": 该要点满分,
            "evidence": "综合证据",
            "verdict": "full/partial/none",
            "judge_count": 命中该点的阅卷人数,
            "total_judges": 有效阅卷人总数,
            "disagreement": true/false
        }}
    ],
    "missing_points": [
        {{
            "point": "遗漏要点内容",
            "max_score": 该要点满分,
            "judge_count": 认为遗漏的阅卷人数,
            "total_judges": 有效阅卷人总数
        }}
    ],
    "disagreement_analysis": [
        {{
            "point": "争议要点",
            "models_hit": ["model_id1"],
            "models_miss": ["model_id2"],
            "resolution": "综合判定及理由"
        }}
    ],
    "ai_feedback": "综合所有阅卷人意见的详细评语（300字以内，引用各模型关键评语）",
    "improving_suggestions": ["具体的改进建议1", "建议2"]
}}

重要：hit_points 和 missing_points 要尽量完整，不要只列少数几个。每个独立采分点都应有对应条目。
只输出合法 JSON，不要 markdown 标记。"""

    return [
        {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]


def summarize_judgments(
    judgments: list[JudgeResult],
    question: dict,
    user_answer: str,
    material: list = None,
) -> dict:
    """调用汇总 LLM 整合多模型批改结果

    Args:
        judgments: 所有模型的 JudgeResult 列表
        question: 题目信息
        user_answer: 考生答案
        material: 给定材料

    Returns:
        汇总后的批改结果字典，包含 score_rate, hit_points, missing_points,
        disagreement_analysis, ai_feedback, improving_suggestions
    """
    valid = [j for j in judgments if j.status == "completed" and j.score_rate is not None]

    if len(valid) == 0:
        return {
            "score_rate": None,
            "dimension_scores": {},
            "hit_points": [],
            "missing_points": [],
            "disagreement_analysis": [],
            "ai_feedback": "所有模型均未返回有效结果",
            "improving_suggestions": [],
            "valid_judges": 0,
            "failed_judges": len(judgments),
            "needs_review": True,
        }

    if len(valid) == 1:
        # 只有一个有效模型，直接返回其结果
        j = valid[0]
        result = {
            "score_rate": j.score_rate,
            "dimension_scores": dict(j.dimension_scores),
            "hit_points": [dict(p) for p in j.hit_points],
            "missing_points": [dict(p) for p in j.missing_points],
            "disagreement_analysis": [],
            "ai_feedback": j.ai_feedback or "",
            "improving_suggestions": j.improving_suggestions or [],
            "valid_judges": 1,
            "failed_judges": len(judgments) - 1,
            "needs_review": False,
        }
        # 大作文两阶段字段透传
        if j.essay_anchor or j.tier or j.paragraph_analysis:
            result["essay"] = {
                "tier": j.tier,
                "tier_reason": j.tier_reason,
                "genre_judgment": j.genre_judgment,
                "thesis_comparison": j.thesis_comparison,
                "paragraph_analysis": j.paragraph_analysis,
                "structure_analysis": j.structure_analysis,
                "overall_evaluation": j.overall_evaluation,
                "top_improvements": j.top_improvements,
                "anchor": j.essay_anchor,
                "anchor_from_cache": j.anchor_from_cache,
            }
        return result

    # 多模型汇总
    messages = build_summarize_prompt(valid, question, user_answer, material)

    try:
        from src.services.feature_model_service import call_feature_llm
        result = call_feature_llm("summarize", messages, parse_json=True)
    except Exception as e:
        logger.warning(f"汇总 LLM 调用失败，回退到加权平均: {e}")
        return _fallback_aggregate(judgments)

    # 校验和补齐字段
    hit_points = result.get("hit_points", [])
    missing_points = result.get("missing_points", [])
    if not isinstance(hit_points, list):
        hit_points = []
    if not isinstance(missing_points, list):
        missing_points = []

    disagreement = result.get("disagreement_analysis", [])
    if not isinstance(disagreement, list):
        disagreement = []

    score_rate = result.get("score_rate")
    if not isinstance(score_rate, (int, float)):
        score_rate = sum(float(j.score_rate or 0) for j in valid) / len(valid)

    dimensions = result.get("dimension_scores", {})
    if not isinstance(dimensions, dict):
        dimensions = {}

    summary = {
        "score_rate": round(float(score_rate), 1),
        "dimension_scores": dimensions,
        "hit_points": hit_points,
        "missing_points": missing_points,
        "disagreement_analysis": disagreement,
        "ai_feedback": result.get("ai_feedback", ""),
        "improving_suggestions": result.get("improving_suggestions", []),
        "valid_judges": len(valid),
        "failed_judges": len(judgments) - len(valid),
        "needs_review": False,
    }
    # 多模型汇总时，大作文字段取首个有效评审的锚点/档次/逐段分析
    essay_judgment = next(
        (jj for jj in valid if jj.essay_anchor or jj.tier or jj.paragraph_analysis), None
    )
    if essay_judgment is not None:
        summary["essay"] = {
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
        }
    return summary


def _fallback_aggregate(judgments: list[JudgeResult]) -> dict:
    """汇总 LLM 失败时的降级方案：简单加权平均"""
    from src.services.grader.aggregation import aggregate_judgments
    result = aggregate_judgments(judgments)
    result["disagreement_analysis"] = []
    return result
