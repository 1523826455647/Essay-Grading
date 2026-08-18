# 时政热点 / 申论素材 LLM 分析服务
#
# 用 deepseek-v4-flash 对抓取到的文章做结构化分析，
# 提炼申论写作真正需要的素材维度：
#   领域分类、主题关键词、核心观点、金句规范表达、
#   典型案例、数据点、申论适用角度、可化用分论点
#
# 数据源（均已验证可达）：
#   人民网评论 opinion.people.com.cn（时评，申论范文标杆）
#   求是网   qstheory.cn（理论，政策深度）
#   人民网理论 theory.people.com.cn
#   人民网党建 cpc.people.com.cn
#   新华网时政 xinhuanet.com/politics
#   学习强国 xuexi.cn

import json
import logging
import re
import requests

# 强制 IPv4（同 topic_scraper，规避容器 DNS 只返回 IPv6 导致的连接失败）
import socket
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

logger = logging.getLogger(__name__)

# 模型选择已迁移到「功能-模型映射」：调用 feature_model_service.call_feature_llm("topic_analyze", ...)。
# 管理员可在后台「功能模型配置」页面为该功能绑定/切换模型；
# 未绑定时的默认绑定由 init_feature_model.py 初始化为 deepseek-v4-flash。

# 五大领域 → 分类代码（对齐 hot_topics.category 枚举）
CATEGORY_CODES = {
    '政治': 'zhengzhi', '经济': 'jingji', '社会': 'shehui',
    '文化': 'wenhua', '生态': 'shengtai', '民生': 'minsheng',
    '治理': 'zhili', '科技': 'keji',
}

ANALYZE_PROMPT = """你是资深申论教研专家。请对给定的时政文章做结构化分析，为申论备考提炼可直接使用的素材。

【分析重点】申论写作最需要：规范的官方表述、核心观点（可作分论点）、真实案例（主体+举措+成效）、权威数据、金句。请重点提炼这些，不要泛泛而谈。

【输出严格JSON，不要markdown，不要任何解释】：
{
  "category": "从以下选一个：zhengzhi政治/jingji经济/shehui社会/wenhua文化/shengtai生态/minsheng民生/zhili治理/keji科技",
  "keywords": ["3-5个主题关键词，如：乡村振兴、基层治理、新质生产力"],
  "summary": "150-250字背景+核心观点梳理，语言规范，可直接用作申论背景段或概括材料",
  "core_viewpoint": "1-2句话提炼的核心观点，可作为大作文中心论点",
  "golden_sentences": [
    {"sentence": "规范表达或金句原文/改写", "type": "对策类/意义类/总结类/论证类"}
  ],
  "cases": [
    {"subject": "案例主体（地区/单位/人物）", "action": "关键举措", "effect": "成效结果"}
  ],
  "data_points": ["文中出现的关键数据或事实"],
  "exam_angles": ["这篇文章可用于的申论题型/角度，如：归纳概括、综合分析、提出对策、大作文论据"],
  "sub_points": ["可直接化用的2-3个分论点，句式规范完整"]
}

【硬约束】：
- 所有内容必须忠于原文，禁止编造原文没有的数据、案例、人名
- summary/core_viewpoint/golden_sentences 用规范书面语，避免口语化
- 只输出JSON"""


def analyze_article(title: str, text: str) -> dict:
    """分析一篇文章，返回结构化素材（模型由后台「功能模型配置」指定）

    Returns:
        dict 或 None（分析失败）
    """
    if not text or not text.strip():
        return None

    content_text = text[:6000]
    user_prompt = f"标题：{title}\n\n正文：\n{content_text}"
    messages = [
        {'role': 'system', 'content': ANALYZE_PROMPT},
        {'role': 'user', 'content': user_prompt},
    ]

    for attempt in range(2):
        try:
            from src.services.feature_model_service import call_feature_llm
            content = call_feature_llm("topic_analyze", messages, parse_json=False)
            content = (content or "").strip()

            # 去掉可能的 markdown 代码块
            content = re.sub(r'```json\s*', '', content)
            content = re.sub(r'```\s*$', '', content)

            m = re.search(r'\{.*\}', content, re.DOTALL)
            if not m:
                continue
            parsed = json.loads(m.group())
            return parsed
        except Exception as e:
            logger.warning(f'LLM分析失败 {title[:20]}...: {e}')

    return None


def normalize_category(category) -> str:
    """把 LLM 返回的分类归一化到枚举值，失败则给默认值"""
    if not category:
        return 'shehui'
    c = str(category).strip()
    if c in CATEGORY_CODES:
        return c
    # 中文名映射
    return CATEGORY_CODES.get(c, 'shehui')
