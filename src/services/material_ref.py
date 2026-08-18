# -*- coding: utf-8 -*-
"""材料范围解析与过滤。

申论题目常规定作答所需阅读的材料范围，例如：
- 根据材料3 → 只看材料3
- 根据给定资料1-3 → 看材料1、2、3
- 结合材料一 → 看中文编号的"材料一"
- 根据全部给定资料 / 根据给定资料（泛指）→ 看全部

本模块提供两个纯函数：
- resolve_material_ref(stem)   : 从题干解析出材料范围（'all' 或 [编号...]）
- filter_material(material, stem) : 按范围过滤材料列表，返回 (列表, 范围说明)
"""
import re

CN_NUM = {
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}


def _cn_to_int(s):
    """中文数字转整数，支持一~十、十一~十九、二十~九十九。无法解析返回 None。"""
    s = (s or '').strip()
    if not s:
        return None
    if s in CN_NUM:
        return CN_NUM[s]
    if len(s) >= 2 and s[0] == '十' and s[1] in CN_NUM:
        return 10 + CN_NUM[s[1]]
    if len(s) >= 2 and s[0] in CN_NUM and s[1] == '十':
        base = CN_NUM[s[0]] * 10
        if len(s) == 2:
            return base
        if s[2] in CN_NUM:
            return base + CN_NUM[s[2]]
    return None


# ---- 材料条目编号识别 ----
_MAT_CN = re.compile(r'^\s*(?:材料|资料)\s*([一二三四五六七八九十]+)')
_MAT_AR = re.compile(r'^\s*(?:材料|资料)\s*(\d+)')
_MAT_PLAIN = re.compile(r'^\s*(\d{1,2})\s*[\.、]')


def extract_material_index(item):
    """识别一条材料条目的编号（1-based int），无法识别返回 None。"""
    if not isinstance(item, str):
        return None
    s = item.strip()
    m = _MAT_CN.match(s)
    if m:
        return _cn_to_int(m.group(1))
    m = _MAT_AR.match(s)
    if m:
        return int(m.group(1))
    m = _MAT_PLAIN.match(s)
    if m:
        return int(m.group(1))
    return None


# ---- 题干材料范围识别 ----
_ALL = re.compile(
    r'全部\s*(?:给定)?\s*(?:资料|材料)|(?:资料|材料)\s*全部|所给全部材料|仅限所给材料'
)
_RANGE_AR = re.compile(r'(?:资料|材料)\s*(\d+)\s*[-~—–至到]\s*(\d+)')
_RANGE_CN = re.compile(
    r'(?:资料|材料)\s*([一二三四五六七八九十]+)\s*[-~—–至到]\s*([一二三四五六七八九十]+)'
)
_SINGLE_AR = re.compile(r'(?:资料|材料)\s*(\d+)')
_SINGLE_CN = re.compile(r'(?:资料|材料)\s*([一二三四五六七八九十]+)')


def resolve_material_ref(stem):
    """解析题目要求阅读的材料范围。

    Returns:
        'all'          需要阅读全部材料（明确"全部"或泛指）
        [1, 2, 3]      需要阅读的具体材料编号列表（升序去重）
    """
    if not stem:
        return 'all'
    s = stem

    if _ALL.search(s):
        return 'all'

    result = set()
    covered = []  # 已被"范围"匹配占用的区间，避免重复计数

    def _covered(span):
        return any(span[0] >= a and span[1] <= b for a, b in covered)

    for m in _RANGE_AR.finditer(s):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b <= 100:
            result.update(range(a, b + 1))
            covered.append(m.span())
    for m in _RANGE_CN.finditer(s):
        a, b = _cn_to_int(m.group(1)), _cn_to_int(m.group(2))
        if a and b and 0 < a <= b <= 100:
            result.update(range(a, b + 1))
            covered.append(m.span())
    for m in _SINGLE_AR.finditer(s):
        if _covered(m.span()):
            continue
        v = int(m.group(1))
        if 0 < v <= 100:
            result.add(v)
    for m in _SINGLE_CN.finditer(s):
        if _covered(m.span()):
            continue
        v = _cn_to_int(m.group(1))
        if v:
            result.add(v)

    if result:
        return sorted(result)
    return 'all'


def _scope_label(ref):
    """把范围列表转成可读说明，如 '材料3'、'材料1-3'、'材料1、3'。"""
    if not ref:
        return '全部材料'
    if len(ref) == 1:
        return f'材料{ref[0]}'
    # 判断是否连续
    if ref == list(range(ref[0], ref[-1] + 1)):
        return f'材料{ref[0]}-{ref[-1]}'
    return '材料' + '、'.join(str(x) for x in ref)


def filter_material(material, stem):
    """按题目要求过滤材料列表。

    Args:
        material: 材料条目列表（每条可能带"材料一/材料1/1."编号，或无编号）
        stem: 题目题干

    Returns:
        (过滤后的材料列表, 范围说明字符串)
    """
    if not isinstance(material, list):
        material = [str(material)] if material else []
    ref = resolve_material_ref(stem)
    if ref == 'all':
        return material, '全部材料'

    allowed = set(ref)
    kept = []
    has_index = False
    for item in material:
        idx = extract_material_index(item)
        if idx is not None:
            has_index = True
        if idx in allowed:
            kept.append(item)

    if kept:
        return kept, _scope_label(ref)

    # 编号对不上时：若材料全部无编号（老卷按段落排列），用位置索引兜底（第 N 条 = 资料 N）
    if not has_index and material:
        pos_kept = [item for i, item in enumerate(material, start=1) if i in allowed]
        if pos_kept:
            return pos_kept, _scope_label(ref)

    # 退化为全部，避免答题页空白
    return material, '全部材料'
