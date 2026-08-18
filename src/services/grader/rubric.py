"""Shared rubric helpers for routing questions to a grading standard."""

QUESTION_TYPE_ALIASES = {
    "guina": ("guina", "归纳概括", "概括论述", "概括题"),
    "zonghe": ("zonghe", "综合分析", "分析题"),
    "duice": ("duice", "提出对策", "对策建议", "对策题"),
    "zhixing": ("zhixing", "贯彻执行", "应用文写作", "公文写作", "应用文"),
    "zuowen": ("zuowen", "大作文", "申发论述", "文章写作"),
}

DOCUMENT_MARKERS = (
    "倡议书", "通知", "报告", "提纲", "讲话稿", "宣传稿", "调研报告",
    "汇报材料", "经验交流", "考察报告", "公开信", "短评", "导言",
)


def normalize_question_type(value: str | None, stem: str = "") -> str:
    """Return the internal question type code used by prompts and statistics."""
    raw = (value or "").strip().lower()
    for code, aliases in QUESTION_TYPE_ALIASES.items():
        if any(alias.lower() in raw for alias in aliases):
            return code

    text = (stem or "").strip()
    if any(marker in text for marker in DOCUMENT_MARKERS) or "写一份" in text:
        return "zhixing"
    if any(marker in text for marker in ("写一篇文章", "自拟题目", "申发论述", "议论文")):
        return "zuowen"
    if any(marker in text for marker in ("提出对策", "提出建议", "解决措施", "怎么办")):
        return "duice"
    if any(marker in text for marker in ("分析", "理解", "评析", "看法", "为什么")):
        return "zonghe"
    return "guina"
