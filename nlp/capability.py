"""能力层：设备属性能力校验（单位/范围/枚举）+ 轻量 CommandIR。

边界：NLP 只产生"明确实体 + 明确属性 + 值 + 单位"；本层依据**设备能力**
校验该组合是否兼容（如 亮度 不接受 档 单位），不做语义候选/消歧。
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from .positions import detect_position

# 中文属性 → 语义单位
_UNIT_BY_ATTR = {
    "温度": "celsius", "水温": "celsius",
    "亮度": "percent", "湿度": "percent", "音量": "percent",
    "风速": "level", "档位": "level", "吸力": "level",
    "模式": "enum", "颜色": "enum",
}
# 属性名关键词（英文 spec 名 → 中文属性），用于从设备属性推断语义单位
_ATTR_KEYWORDS = {
    "温度": ["温度", "temperature", "temp"],
    "水温": ["水温", "tds", "water"],
    "亮度": ["亮度", "brightness"],
    "湿度": ["湿度", "humidity"],
    "音量": ["音量", "volume"],
    "风速": ["风速", "风量", "fan", "fan level", "fan_level", "fan-speed"],
    "档位": ["档位", "档", "level", "suction", "heat level"],
    "吸力": ["吸力", "suction"],
    "模式": ["模式", "mode"],
    "颜色": ["颜色", "color", "rgb"],
}
# 命令携带的单位 → 语义单位
_COMMAND_UNIT = {
    "度": "celsius", "℃": "celsius", "°": "celsius",
    "%": "percent",
    "档": "level", "档位": "level",
}
# 语义单位 → 可接受的字面单位（错误 detail 里回给模型，便于自纠重试）
_UNITS_FOR_SEMANTIC = {
    "celsius": ["度", "℃", "°"],
    "percent": ["%"],
    "level": ["档", "档位"],
    "enum": [],
}


@dataclass
class CommandIR:
    """指令的中间表示——**前端与后端的接缝**。

    前端（中文规则引擎 / 未来的 LLM 结构化）负责把用户原话填成这个结构；
    后端（设备匹配 + 能力校验 + 执行分支）只认这个结构，不认谁的解析结果。

    几个字段的归属：
      * ``action``        —— **要做什么**（switch / set_prop / adjust_prop /
                             scene / query），取自解析结果的语义动作，不是路由分支名
      * ``device_info``   —— 匹配到的**完整设备 dict**（含 properties / actions /
                             did / name / alias）。执行分支靠它拿属性表与动作表；
                             缺了它后端还得回头问 MatchResult，接缝就漏了
      * ``match_status`` / ``match_message`` —— 前端带过来的匹配结论，供后端在
                             未命中/歧义时复述原因，无需回传前端对象
      * ``verb``          —— 动作动词（开始 / 暂停 / 回充…），动作分支据此选 aiid
      * ``gang``          —— 多控开关的方位（"右" / "二键" / "right"）。
                             **由前端解析，后端不再扫描原始文本**。规则前端用
                             `nlp.positions.detect_position()` 从原话里抽；
                             LLM 前端可以直接给英文标签或序号
      * ``candidates``    —— 匹配失败/歧义时的候选设备，供结构化错误 detail 使用
      * ``require_explicit_gang`` —— 多控设备上没给 gang 时是否报歧义。
                             默认 False（中文规则前端沿用"自动选主开关"的旧行为）；
                             LLM 前端应置 True，让模型追问或重试
    """

    raw_text: str = ""
    action: str = ""
    room: str = ""
    device: str = ""
    device_id: str = ""
    device_info: Optional[dict] = None
    attribute: str = ""
    value: Any = None
    unit: str = ""
    direction: int = 1
    delta: Optional[float] = None
    verb: str = ""
    match_status: str = ""
    match_message: str = ""
    gang: Optional[str] = None
    candidates: list = field(default_factory=list)
    require_explicit_gang: bool = False


@dataclass
class ValidationResult:
    """能力校验结果。

    status: "valid" | "invalid_unit" | "invalid_value" | "unknown_attribute"
    detail: 结构化补充（允许范围 / 期望单位），供 @llm_tool 错误 detail 直接透出。
    """

    status: str
    message: str = ""
    detail: dict = field(default_factory=dict)


def command_unit(unit: str) -> Optional[str]:
    """命令单位 → 语义单位（度→celsius、%→percent、档→level）。"""
    return _COMMAND_UNIT.get(unit)


def to_command_ir(result, *, raw_text: Optional[str] = None) -> CommandIR:
    """把路由结果展平为 CommandIR（鸭子类型，避免循环导入）。

    `action` 取**语义动作**：有 parsed 时用 parsed.action（switch / set_prop /
    adjust_prop），否则退回分支名（scene / query / action）。执行分支据此分派。

    `raw_text` 允许调用方覆盖：规则引擎的 `RouteResult.raw_text` 是**归一化后**
    的文本（去掉了"把/请/帮我"），而方位词抽取应当看用户原话。插件在分发处传
    入原始 command，保证中文行为与改造前一致。
    """
    branch = getattr(result, "branch", "")
    text = raw_text if raw_text is not None else getattr(result, "raw_text", "")
    ir = CommandIR(raw_text=text, action=branch)
    # 方位词属于前端解析：这里一次性抽出，后端只读 ir.gang。
    ir.gang = detect_position(text)
    match = getattr(result, "match", None)
    if match is not None:
        ir.match_status = match.status
        ir.match_message = match.message or ""
        ir.candidates = list(getattr(match, "candidates", None) or [])
    if match is not None and match.status == "ok" and match.device is not None:
        ir.device_info = match.device
        ir.room = match.device.get("room_name", "")
        ir.device = match.device.get("name", "")
        ir.device_id = match.device.get("did", "")
    else:
        ir.device = getattr(result, "device_hint", "")
    ir.verb = getattr(result, "verb", "") or ""
    parsed = getattr(result, "parsed", None)
    if parsed is not None:
        ir.action = getattr(parsed, "action", "") or branch
        ir.attribute = parsed.prop or ""
        ir.value = parsed.value
        ir.unit = parsed.unit
        ir.direction = parsed.direction
        ir.delta = parsed.delta
    elif branch == "switch":
        # 路由判为 switch 但没有 parsed：只补属性名，值留给调用方，避免瞎猜。
        ir.action = "switch"
        ir.attribute = "开关"
    return ir


def property_unit(prop: dict) -> Optional[str]:
    """从设备属性推断语义单位（枚举优先，其次按属性名关键词）。"""
    if prop.get("value_list"):
        return "enum"
    name = (prop.get("name") or "").lower()
    for cn, keys in _ATTR_KEYWORDS.items():
        if any(k in name for k in keys):
            return _UNIT_BY_ATTR.get(cn)
    return None


def validate_command(prop: dict, attribute: str, value: Any, unit: str) -> ValidationResult:
    """校验属性/值/单位是否与设备能力兼容（单位 + 范围）。

    失败时附带结构化 `detail`：模型拿到「允许的范围 / 期望的单位」才能自行修正
    重试，而不是只看到一句散文。
    """
    cmd_u = command_unit(unit)
    prop_u = property_unit(prop)
    if cmd_u and prop_u and prop_u != "enum" and cmd_u != prop_u:
        return ValidationResult(
            "invalid_unit",
            f"属性'{attribute}'不接受单位'{unit}'（设备能力单位不是它）",
            {
                "attribute": attribute,
                "unit_given": unit,
                "expected_unit": prop_u,
                "allowed_units": _UNITS_FOR_SEMANTIC.get(prop_u, []),
            },
        )
    vr = prop.get("value_range") or []
    if value is not None and isinstance(value, (int, float)) and len(vr) >= 2:
        if value < vr[0] or value > vr[1]:
            return ValidationResult(
                "invalid_value",
                f"'{attribute}'={value} 超出设备支持范围 {vr[:2]}",
                {
                    "attribute": attribute,
                    "value": value,
                    "min": vr[0],
                    "max": vr[1],
                    "step": vr[2] if len(vr) >= 3 else None,
                },
            )
    return ValidationResult("valid")
