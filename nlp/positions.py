"""多控开关的方位词（Front-end A 资产：中文输入 → 英文 spec 关键词）。

原先定义在 `property_tables._POS_MAP`，被 `_execute_switch` 直接拿去扫描用户
原话。这既是**语言相关**的解析（属于规则前端），又让后端耦合了原始文本。
现在：

* 解析发生在前端：`to_command_ir` 用 `detect_position()` 抽出一个 `gang`；
* 后端只读 `CommandIR.gang`，通过 `position_keywords()` 翻成 service_desc 关键词。

Front-end B（LLM）可以直接给英文标签（"right"、"Second"）或中文词；未知形式
原样返回，交给 service_desc 子串匹配兜底。
"""

from __future__ import annotations

import re
from typing import Optional

# (中文方位词/序号的正则, service_desc 里对应的英文关键词)
POSITION_MAP: list[tuple[str, list[str]]] = [
    (r"左", ["Left", "left"]),
    (r"右", ["Right", "right"]),
    (r"中", ["Middle", "middle", "Center", "center"]),
    (r"(?:一|1)键", ["First", "first", "1"]),
    (r"(?:二|2)键", ["Second", "second", "2"]),
    (r"(?:三|3)键", ["Third", "third", "3"]),
    (r"(?:四|4)键", ["Fourth", "fourth", "4"]),
    (r"(?:五|5)键", ["Fifth", "fifth", "5"]),
    (r"(?:六|6)键", ["Sixth", "sixth", "6"]),
]


def detect_position(text: str) -> Optional[str]:
    """从用户原话里抽方位词，返回命中的字面量（"右"、"二键"）；没有返回 None。

    保持与原 `_execute_switch` 相同的匹配顺序（POSITION_MAP 顺序、取第一个命中）。
    """
    for pattern, _keywords in POSITION_MAP:
        m = re.search(pattern, text or "")
        if m:
            return m.group(0)
    return None


def position_keywords(position: str) -> list[str]:
    """把 `CommandIR.gang` 翻成 service_desc 匹配关键词。

    * 规则前端产出的是中文词（"右"、"二键"）→ 命中 POSITION_MAP，返回英文关键词
    * LLM 前端可能直接给 "right" / "Second" / "2" → 原样返回，由子串匹配兜底
    """
    if not position:
        return []
    for pattern, keywords in POSITION_MAP:
        if re.fullmatch(pattern, position):
            return keywords
    return [position]


__all__ = ["POSITION_MAP", "detect_position", "position_keywords"]
