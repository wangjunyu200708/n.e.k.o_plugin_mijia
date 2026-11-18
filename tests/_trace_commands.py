"""逐条模拟 MijiaPlugin.smart_control 对 tests/智能家居语音常用命令.txt 的处理。

只复现主程序的**纯规则判定**（nlp/ 纯函数 + branch 分发 + 属性定位 + 能力校验），
不调用任何米家 API、不读写缓存/凭据：

    normalize_utterance → detect_scene（场景早退）→ route() 五步短路 → branch 分发

输出：
    tests/commands_execution_trace.json   完整机器可读执行数据
    tests/commands_execution_trace.md     分析报告（汇总 + 逐条明细）

注意：DEVICES 是**模拟设备表**（覆盖命令表出现的设备类型），仅用于验证
"NLP 解析 → 执行意图"的映射；真实结果取决于用户账号下的实际设备。
设备/属性定位逻辑复刻自 __init__.py（_execute_switch / _find_property_for_control /
_execute_action_branch），属性关键词表为 PROP_KEYWORDS 的子集。

运行：python tests/_trace_commands.py
"""

import asyncio
import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _import_nlp():
    """延迟导入 nlp：仓库根必须先进入 sys.path 才能导入。

    Market CI 的 ruff 带 `--ignore-noqa`，模块级 import 写在 sys.path 调整之后
    会触发 E402 且 noqa 注释无效（CI 会直接失败），所以改用函数作用域导入。
    """
    from nlp.action_verbs import VERB_TO_ACTION
    from nlp.capability import validate_command
    from nlp.device_matcher import match_devices
    from nlp.intent_terms import detect_scene
    from nlp.normalizer import normalize_utterance
    from nlp.router import route

    return (
        VERB_TO_ACTION,
        validate_command,
        match_devices,
        detect_scene,
        normalize_utterance,
        route,
    )


(
    VERB_TO_ACTION,
    validate_command,
    match_devices,
    detect_scene,
    normalize_utterance,
    route,
) = _import_nlp()

CMDS_FILE = ROOT / "tests" / "智能家居语音常用命令.txt"
JSON_OUT = ROOT / "tests" / "commands_execution_trace.json"
MD_OUT = ROOT / "tests" / "commands_execution_trace.md"

# ── 属性关键词（__init__.py PROP_KEYWORDS 子集）──
PROP_KEYWORDS = {
    "亮度": ["亮度", "brightness"],
    "色温": ["色温", "color temperature"],
    "温度": ["目标温度", "设定温度", "温度", "target temperature", "temperature"],
    "音量": ["音量", "volume"],
    "风速": ["风速", "风量", "风档", "档位", "fan speed", "fan level", "fan_level"],
    "模式": ["模式", "mode"],
    "浓度": ["浓度", "density"],
    "湿度": ["湿度", "设定湿度", "humidity"],
    "位置": ["位置", "position"],
    "吸力": ["吸力", "suction"],
    "档位": ["档位", "heat level"],
    "水温": ["水温", "设定温度", "target temperature"],
    "颜色": ["颜色", "color", "rgb"],
}


def _sw(siid, piid, name, **kw):
    p = {"siid": siid, "piid": piid, "name": name, "access": "read_write", "type": "bool",
         "service_desc": kw.pop("svc", "Switch")}
    p.update(kw)
    return p


def _num(siid, piid, name, lo, hi, step, unit=None, **kw):
    p = {"siid": siid, "piid": piid, "name": name, "access": "read_write",
         "type": "uint8", "value_range": [lo, hi, step]}
    if unit:
        p["unit"] = unit
    p.update(kw)
    return p


def _enum(siid, piid, name, items, **kw):
    p = {"siid": siid, "piid": piid, "name": name, "access": "read_write",
         "type": "uint8", "value_list": items}
    p.update(kw)
    return p


def _vl(*labels):
    return [{"value": i + 1, "description": lab} for i, lab in enumerate(labels)]


def _dev(did, name, room, props=(), actions=(), alias="", online=True, model="mijia.mock"):
    d = {"did": did, "name": name, "model": model, "room_name": room,
         "is_online": online, "properties": list(props), "actions": list(actions)}
    if alias:
        d["alias"] = alias
    return d


_LIGHT_PROPS = [
    _sw(2, 1, "Power"),
    _num(2, 2, "Brightness", 1, 100, 1, unit="%"),
    _num(2, 3, "Color Temperature", 2700, 6500, 100),
]
_AC_PROPS = [
    _sw(2, 1, "Power"),
    _enum(2, 2, "Mode", _vl("制冷", "制热", "自动", "送风", "除湿", "睡眠")),
    _num(2, 3, "Target Temperature", 16, 30, 1, unit="℃"),
    _num(2, 4, "Fan Level", 1, 3, 1),
]

DEVICES = [
    # 灯光
    _dev("light_living", "客厅灯", "客厅", _LIGHT_PROPS),
    _dev("light_bedroom", "卧室灯", "卧室", _LIGHT_PROPS),
    _dev("light_bedside", "床头灯", "卧室", _LIGHT_PROPS),
    _dev("light_balcony", "阳台灯", "阳台", _LIGHT_PROPS),
    _dev("light_study", "书房灯", "书房", _LIGHT_PROPS + [
        _enum(2, 9, "Scene Mode", _vl("阅读", "观影", "明亮"))]),
    _dev("light_kitchen", "厨房灯", "厨房", _LIGHT_PROPS),
    _dev("light_bath", "卫生间灯", "卫生间", _LIGHT_PROPS),
    _dev("light_dining", "餐厅灯", "餐厅", _LIGHT_PROPS, alias="氛围灯"),
    _dev("light_corridor", "走廊灯", "走廊", _LIGHT_PROPS, alias="夜灯"),
    _dev("light_entry", "玄关灯", "玄关", _LIGHT_PROPS),
    _dev("light_strip", "灯带", "客厅", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Brightness", 1, 100, 1, unit="%"),
        {"siid": 2, "piid": 3, "name": "Color", "access": "read_write",
         "type": "uint32", "value_range": [0, 16777215, 1]},
    ], alias="氛围灯带"),
    _dev("light_floor", "地灯", "客厅", _LIGHT_PROPS),
    # 空调 / 环境电器
    _dev("ac_living", "客厅空调", "客厅", _AC_PROPS, alias="空调"),
    _dev("ac_bedroom", "卧室空调", "卧室", _AC_PROPS),
    _dev("ac_master", "主卧空调", "主卧", _AC_PROPS),
    _dev("fresh_air", "新风系统", "客厅", [
        _sw(2, 1, "Power"),
        _enum(2, 2, "Mode", _vl("自动", "手动", "静音")),
        _num(2, 3, "Fan Level", 1, 3, 1),
    ]),
    _dev("purifier", "空气净化器", "客厅", [
        _sw(2, 1, "Power"),
        _enum(2, 2, "Mode", _vl("自动", "睡眠", "最爱")),
        _num(2, 3, "Fan Level", 1, 3, 1),
        {"siid": 3, "piid": 1, "name": "PM2.5", "access": "read", "type": "uint16"},
    ]),
    _dev("humidifier", "加湿器", "卧室", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Humidity", 30, 80, 1, unit="%"),
        _enum(2, 3, "Mode", _vl("自动", "睡眠", "恒湿")),
    ]),
    _dev("fan", "电风扇", "客厅", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Fan Level", 1, 4, 1),
        _enum(2, 3, "Mode", _vl("正常风", "自然风", "睡眠风", "摇头")),
    ]),
    _dev("heater", "地暖", "客厅", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Target Temperature", 16, 35, 1, unit="℃"),
    ]),
    _dev("water_heater", "热水器", "卫生间", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Target Temperature", 30, 65, 1, unit="℃"),
        _num(2, 3, "Water Temperature", 0, 99, 1, unit="℃", access="read"),
    ]),
    _dev("dehumidifier", "除湿机", "客厅", [
        _sw(2, 1, "Power"),
        _enum(2, 2, "Mode", _vl("自动", "连续", "干衣")),
        _num(2, 3, "Humidity", 30, 80, 1, unit="%"),
    ]),
    _dev("hood", "抽油烟机", "厨房", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Fan Level", 1, 3, 1),
        _enum(2, 3, "Mode", _vl("自动", "手动")),
    ]),
    # 窗帘 / 晾衣
    _dev("curtain_living", "客厅窗帘", "客厅", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Position", 0, 100, 1, unit="%"),
    ], alias="窗帘"),
    _dev("curtain_bedroom", "卧室窗帘", "卧室", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Position", 0, 100, 1, unit="%"),
    ], alias="卧室纱帘"),
    _dev("curtain_balcony", "阳台窗帘", "阳台", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Position", 0, 100, 1, unit="%"),
    ]),
    _dev("rack", "晾衣架", "阳台", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Position", 0, 100, 1, unit="%"),
        {"siid": 2, "piid": 3, "name": "Dry", "access": "read_write", "type": "bool"},
    ]),
    # 安防
    _dev("lock", "智能门锁", "玄关", [
        {"siid": 2, "piid": 1, "name": "Door Lock State", "access": "read", "type": "uint8"},
    ], actions=[{"siid": 3, "aiid": 1, "name": "Lock"}], alias="门锁"),
    _dev("camera_door", "门口监控", "门口", [
        _sw(2, 1, "Power"),
    ], alias="门口摄像头"),
    _dev("camera_living", "客厅摄像头", "客厅", [_sw(2, 1, "Power")], alias="摄像头"),
    # 清洁电器
    _dev("vacuum", "扫地机器人", "客厅", [
        _sw(2, 1, "Power"),
    ], actions=[
        {"siid": 2, "aiid": 1, "name": "Start Sweep"},
        {"siid": 2, "aiid": 2, "name": "Pause Sweeping"},
        {"siid": 2, "aiid": 3, "name": "Stop Sweeping"},
        {"siid": 2, "aiid": 4, "name": "Start Charge"},
    ], alias="扫地机"),
    # 厨卫电器
    _dev("washer", "洗衣机", "卫生间", [
        _sw(2, 1, "Power"),
        _enum(2, 2, "Mode", _vl("快洗", "标准洗", "轻柔")),
        {"siid": 2, "piid": 3, "name": "Remaining Time", "access": "read", "type": "uint16"},
    ]),
    _dev("dishwasher", "洗碗机", "厨房", [_sw(2, 1, "Power")]),
    _dev("cooker", "电饭煲", "厨房", [
        _sw(2, 1, "Power"),
        _enum(2, 2, "Mode", _vl("煮饭", "煮粥", "保温", "预约")),
    ]),
    _dev("microwave", "微波炉", "厨房", [_sw(2, 1, "Power")]),
    _dev("toilet", "智能马桶盖", "卫生间", [
        _sw(2, 1, "Power"),
    ], actions=[{"siid": 3, "aiid": 1, "name": "Flush"}], alias="马桶盖"),
    # 影音
    _dev("tv", "电视", "客厅", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Volume", 0, 100, 1, unit="%"),
        {"siid": 2, "piid": 3, "name": "TV Input Control", "access": "read_write",
         "type": "uint8", "value_list": _vl("HDMI1", "HDMI2", "AV")},
    ]),
    _dev("projector", "投影仪", "客厅", [_sw(2, 1, "Power")]),
    _dev("speaker", "智能音箱", "客厅", [
        _sw(2, 1, "Power"),
        _num(2, 2, "Volume", 0, 100, 1, unit="%"),
    ], alias="音箱"),
    _dev("socket", "智能插座", "客厅", [_sw(2, 1, "Power")], alias="插座"),
]


def _norm(s):
    return unicodedata.normalize("NFKC", (s or "").strip().lower())


# ── 已核验的解析缺陷（由 nlp 层实际函数输出核对，见 _probe 探针记录）──
FINDINGS = [
    ("F1", "『百分之X』数值解析错误（恒为 100）",
     "第 5、37、51、101 条",
     "parse_prop_value('灯光调到百分之五十') → (None, 100, '')、'百分之三十' 同样 →100；"
     "value_resolver.py:14 `_CN_NUM` 先命中 '百'→100，'五十' 被丢弃"),
    ("F2", "窗帘『拉开/关上一半/全部拉开』完全无法识别",
     "第 50、51、55、141、156 条",
     "parse_control_command('窗帘拉开一半') → None → branch=unknown；"
     "'拉开/关上/一半/全部' 不在词表（仅支持 打开/关闭 + 位置）"),
    ("F3", "『空调定时两小时后关闭』被当作立即关机",
     "第 28 条",
     "parse → action=switch value=False, device='空调定时两小时后'；"
     "hint 含噪音仍模糊命中客厅空调 → 计划写 Power=False，定时语义被丢弃"),
    ("F4", "『把电视调到央视一套』误解析为 亮度=1",
     "第 98 条",
     "parse → prop='亮度' value=1（'一' 取自 '一套'）；电视无亮度属性 → "
     "实际会报『没有可控制的亮度属性』，频道语义丢失"),
    ("F5", "动词前置/无设备名的控制命令无法解析",
     "第 82、99、100、148 条",
     "parse_control_command('调暗床头灯') → None、'音量调大一点' → None → unknown"
     "（解析器要求 设备名 + 动词/属性 的分界）"),
    ("F6", "动作名形态差异导致『回充』匹配不上",
     "第 81 条",
     "VERB_TO_ACTION['回充']=['start-charge','start_charge']，而 spec 动作名常为 'Start Charge'（空格）→ "
     "精确/子串均不命中；'开始' 因候选含 'start' 仍可模糊命中 'Start Sweep'"),
    ("F7", "口语场景别名缺『我出门了』",
     "第 144 条",
     "detect_scene('我出门了') → None（SCENE_PHRASE_MAP 仅有 我回家了/我到家了/我回来了/到家了）→ unknown，"
     "而『我回来了』可命中"),
    ("F8", "『打开游戏模式』被判为场景而非电视模式",
     "第 155 条",
     "detect_scene('打开游戏模式') → '游戏' → branch=scene：SCENE_MODE_RE 对任意『打开X模式』都判场景，"
     "无设备名时无法区分场景与设备模式"),
]


def find_prop(device, prop_name, value=None):
    """复刻 _find_property_for_control 的主路径（关键词 → 可写属性）。"""
    if not prop_name:
        return None, "prop 为空"
    used = PROP_KEYWORDS.get(prop_name, [prop_name.lower()])
    for p in device.get("properties", []):
        if p.get("access") not in ("write", "read_write", "notify_read_write"):
            continue
        name = (p.get("name") or "").lower()
        if any(k in name for k in used):
            return p, ""
    if prop_name == "模式":
        for p in device.get("properties", []):
            if p.get("access") not in ("write", "read_write", "notify_read_write"):
                continue
            if p.get("value_list") or p.get("type") in ("string", "uint8"):
                return p, "模式命令回退：第一个枚举/可写属性"
    return None, "设备无可写属性匹配该关键词"


def enum_resolvable(prop, value):
    if not isinstance(value, str) or not prop.get("value_list"):
        return None
    for item in prop.get("value_list", []):
        if isinstance(item, dict):
            labels = [str(item.get(k)) for k in ("description", "comment", "value") if item.get(k) is not None]
            if any(value == lab or value in lab or lab in value for lab in labels):
                return True
        elif value == str(item):
            return True
    return False


def switch_capability(device, value):
    """复刻 _execute_switch：电源动作优先 → 可写 bool 开关属性。"""
    kws = ["turn on", "power on", "switch on", "power-on"] if value else \
          ["turn off", "power off", "switch off", "power-off"]
    action = next((a for a in device.get("actions", [])
                   if any(k in (a.get("name") or "").lower() for k in kws)), None)
    props = [p for p in device.get("properties", [])
             if any(k in (p.get("name") or "").lower() for k in ["开关", "电源", "power", "switch", "on"])
             and p.get("access") in ("write", "read_write", "notify_read_write")
             and p.get("type") == "bool"]
    return action, props


def analyze(index, cmd):
    norm = normalize_utterance(cmd)
    scene_shortcut = detect_scene(norm)  # smart_control 在 route() 之前先判场景
    res = asyncio.run(route(norm, DEVICES))

    rec = {
        "index": index,
        "input": cmd,
        "normalized": norm,
        "scene_shortcut": scene_shortcut,
        "branch": res.branch,
        "device_hint": res.device_hint,
        "scene_name": res.scene_name,
        "verb": res.verb,
        "query_prop": res.query_prop,
        "match": None,
        "parsed": None,
        "plan": "",
        "verdict": "",
    }
    if res.match is not None:
        rec["match"] = {
            "status": res.match.status,
            "devices": [{"did": d.get("did"), "name": d.get("name"), "room": d.get("room_name")}
                        for d in res.match.devices],
            "message": getattr(res.match, "message", "") or "",
        }
    if res.parsed is not None:
        rec["parsed"] = {"device": res.parsed.device, "action": res.parsed.action,
                         "prop": res.parsed.prop, "value": res.parsed.value,
                         "unit": res.parsed.unit, "direction": res.parsed.direction,
                         "delta": res.parsed.delta}

    matched = res.match.devices[0] if (res.match is not None and res.match.status == "ok"
                                       and len(res.match.devices) == 1) else None

    if res.branch == "scene":
        rec["plan"] = f"execute_scene(name≈'{res.scene_name or scene_shortcut}')"
        rec["verdict"] = "scene_execute"
    elif res.branch == "query":
        # route() 的 query 分支不带 match，真实匹配发生在 query_device_state() 内
        qm = match_devices(res.device_hint, DEVICES)
        rec["match"] = {
            "status": qm.status,
            "devices": [{"did": d.get("did"), "name": d.get("name"), "room": d.get("room_name")}
                        for d in qm.devices],
            "message": getattr(qm, "message", "") or "",
        }
        if qm.status == "ok" and len(qm.devices) == 1:
            dev = qm.devices[0]
            readable = [p.get("name") for p in dev.get("properties", []) if "read" in (p.get("access") or "")]
            rec["plan"] = f"query_device_state('{res.device_hint}') → 读 {dev['name']} 的 {len(readable)} 个可读属性"
            rec["verdict"] = "query_ok"
        elif qm.status == "ambiguous":
            rec["plan"] = f"query_device_state('{res.device_hint}') → 歧义（{len(qm.devices)} 个候选）"
            rec["verdict"] = "ambiguous_device"
        else:
            rec["plan"] = f"query_device_state('{res.device_hint}') → 设备未匹配"
            rec["verdict"] = "query_device_missing"
    elif res.branch == "action":
        if matched:
            cands = VERB_TO_ACTION.get(res.verb, [res.verb])
            hit = next((a for a in matched.get("actions", [])
                        if any(c.lower() == (a.get("name") or "").lower() for c in cands)), None)
            if hit is None:
                hit = next((a for a in matched.get("actions", [])
                            if any(c.lower() in (a.get("name") or "").lower() for c in cands)), None)
            if hit:
                rec["plan"] = f"call_device_action(siid={hit['siid']}, aiid={hit['aiid']}, '{hit['name']}')"
                rec["verdict"] = "action_ok"
            else:
                avail = [a.get("name") for a in matched.get("actions", [])]
                rec["plan"] = f"动作'{res.verb}'无对应 action（设备可用：{avail or '无'}）"
                rec["verdict"] = "action_no_matching_action"
        else:
            rec["plan"] = "设备未匹配 → Err 未找到设备"
            rec["verdict"] = "action_device_missing"
    elif res.branch == "switch":
        if matched:
            value = res.parsed.value if res.parsed else None
            action, props = switch_capability(matched, value)
            if action:
                rec["plan"] = f"电源动作 '{action['name']}'(siid={action['siid']}, aiid={action['aiid']})；失败则回落属性通道"
                rec["verdict"] = "switch_via_power_action"
            elif props:
                rec["plan"] = f"写开关属性 {[p['name'] for p in props]} → 写后回读校验"
                rec["verdict"] = "switch_via_property"
            else:
                rec["plan"] = "无可写 bool 开关属性、无电源动作 → 报错"
                rec["verdict"] = "switch_no_switchable_prop"
        else:
            rec["plan"] = "设备未匹配 → Err 未找到设备"
            rec["verdict"] = "switch_device_missing"
    elif res.branch == "control":
        if matched:
            parsed = res.parsed
            prop, why = find_prop(matched, parsed.prop, parsed.value)
            if parsed.action == "adjust_prop":
                if prop:
                    rec["plan"] = (f"读 '{prop['name']}' 当前值 → 方向{parsed.direction} 步长/幅度{parsed.delta} "
                                   f"→ clamp{prop.get('value_range', '')} → 写回")
                    rec["verdict"] = "adjust_ok"
                else:
                    rec["plan"] = f"找不到属性'{parsed.prop}'（{why}）→ 报错列可用属性"
                    rec["verdict"] = "control_prop_missing"
            else:
                if not parsed.prop:
                    rec["plan"] = "prop 为空 → Err '请指定要调整的属性'"
                    rec["verdict"] = "control_prop_unspecified"
                elif not prop:
                    rec["plan"] = f"找不到属性'{parsed.prop}'（{why}）→ 报错列可用属性"
                    rec["verdict"] = "control_prop_missing"
                else:
                    chk = validate_command(prop, parsed.prop, parsed.value, parsed.unit)
                    enum_ok = enum_resolvable(prop, parsed.value)
                    if chk.status != "valid":
                        rec["plan"] = f"能力校验拦截：{chk.message}"
                        rec["verdict"] = f"capability_{chk.status}"
                    elif enum_ok is False:
                        labels = [i.get("description") or i.get("value") for i in prop.get("value_list", [])]
                        rec["plan"] = f"枚举'{parsed.value}'不在 {labels} → 报错列可用模式"
                        rec["verdict"] = "control_enum_invalid"
                    else:
                        extra = "（枚举转换后写回）" if enum_ok else ""
                        rec["plan"] = (f"control_device(siid={prop['siid']}, piid={prop['piid']}, "
                                       f"{prop['name']}={parsed.value}{parsed.unit}){extra}")
                        rec["verdict"] = "control_ok"
        else:
            rec["plan"] = "设备未匹配 → Err 未找到设备"
            rec["verdict"] = "control_device_missing"
    else:
        rec["plan"] = "Err 无法理解命令（返回支持格式提示）"
        rec["verdict"] = "unknown_intent"

    if rec["match"] and rec["match"]["status"] == "ambiguous" and rec["verdict"].endswith("device_missing"):
        rec["verdict"] = "ambiguous_device"

    # ── 标记：需人工复核的解析现象（客观条件，不含主观判断）──
    flags = []
    if matched:
        hint_norm = (res.device_hint or "").replace("的", "").strip().lower()
        labels = {matched["name"].lower()}
        labels |= {a.strip().lower() for a in (matched.get("alias") or "").split(",") if a.strip()}
        if hint_norm and hint_norm not in labels:
            flags.append("fuzzy_hint")  # 模糊命中：hint 含额外文本，语义可能被丢弃
    if "百分之" in cmd and rec["parsed"] and rec["parsed"]["value"] is not None:
        flags.append("percent_expr")  # "百分之X" 的数值需人工核对
    if rec["parsed"] and rec["parsed"]["prop"] is None and res.branch == "control":
        flags.append("prop_unspecified")
    rec["flags"] = flags
    return rec


def md_escape(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def main():
    lines = [ln.strip() for ln in CMDS_FILE.read_text(encoding="utf-8").splitlines()]
    cmds = [ln for ln in lines if ln]
    records = [analyze(i + 1, c) for i, c in enumerate(cmds)]

    JSON_OUT.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    branch_counts, verdict_counts = {}, {}
    for r in records:
        branch_counts[r["branch"]] = branch_counts.get(r["branch"], 0) + 1
        verdict_counts[r["verdict"]] = verdict_counts.get(r["verdict"], 0) + 1

    focus = [r for r in records if r["verdict"].startswith(("unknown", "control_prop", "capability",
                                                           "action_no", "enum", "switch_no"))]
    missing = [r for r in records if "device_missing" in r["verdict"] or r["verdict"] == "ambiguous_device"]

    out = []
    out.append("# 米家插件：常用语音命令执行轨迹")
    out.append("")
    out.append(f"> 由 `tests/_trace_commands.py` 生成；输入 `tests/智能家居语音常用命令.txt`（{len(records)} 条）。")
    out.append("> 复现 `MijiaPlugin.smart_control` 的纯规则判定（normalize → 场景早退 → `nlp.route()` 五步短路 → branch 分发），")
    out.append("> **不调用任何米家 API**，因此不代表真实设备执行结果，只反映「插件打算怎么执行」。")
    out.append("")
    out.append("## 1. 方法说明")
    out.append("")
    out.append("- 归一化：`normalize_utterance()`（剥 把/请/帮我 等前缀，不碰实体内部文本）")
    out.append("- 场景早退：`detect_scene()` 命中即执行场景（与主程序一致，先于设备缓存加载）")
    out.append("- 路由：`nlp.router.route()`，五步短路 场景 → 开关标记 → 查询 → 动作动词 → 属性控制")
    out.append("- 执行意图：按 branch 复刻 `_execute_scene_by_name` / `query_device_state` /")
    out.append("  `_execute_action_branch` / `_execute_switch` / `_execute_set_prop` / `_execute_adjust_prop` 的动作")
    out.append("- 属性定位：复刻 `_find_property_for_control` 的关键词路径；能力校验用 `capability.validate_command`")
    out.append("")
    out.append("## 2. 模拟设备表（分析用，非真实设备）")
    out.append("")
    out.append("| 设备 | 房间 | 别名 | 可写属性 | actions | 在线 |")
    out.append("|---|---|---|---|---|---|")
    for d in DEVICES:
        w = [p["name"] for p in d["properties"] if p.get("access") in ("write", "read_write", "notify_read_write")]
        a = [x["name"] for x in d.get("actions", [])]
        out.append("| {} | {} | {} | {} | {} | {} |".format(
            d["name"], d["room_name"], d.get("alias", ""), ", ".join(w) or "-",
            ", ".join(a) or "-", "✅" if d["is_online"] else "❌"))
    out.append("")
    out.append("## 3. 汇总")
    out.append("")
    out.append(f"共 **{len(records)}** 条命令。")
    out.append("")
    out.append("### 3.1 路由分支分布")
    out.append("")
    out.append("| branch | 条数 | 含义 |")
    out.append("|---|---|---|")
    branch_meaning = {
        "scene": "场景执行（含口语别名/XX模式）",
        "query": "状态查询",
        "action": "设备动作（开始/暂停/回充…）",
        "switch": "二元开关",
        "control": "属性/模式控制",
        "unknown": "无法理解",
    }
    for b, c in sorted(branch_counts.items(), key=lambda kv: -kv[1]):
        out.append(f"| {b} | {c} | {branch_meaning.get(b, '')} |")
    out.append("")
    out.append("### 3.2 执行判定分布")
    out.append("")
    out.append("| verdict | 条数 |")
    out.append("|---|---|")
    for v, c in sorted(verdict_counts.items(), key=lambda kv: -kv[1]):
        out.append(f"| {v} | {c} |")
    out.append("")
    out.append(f"### 3.3 需关注：解析失败/能力拦截/动作缺映射（{len(focus)} 条）")
    out.append("")
    if focus:
        out.append("| # | 输入 | branch | 解析 | 说明 | verdict |")
        out.append("|---|---|---|---|---|---|")
        for r in focus:
            p = r["parsed"]
            ps = (f"{p['action']}/prop={p['prop']}/value={p['value']}/unit={p['unit']!r}") if p else "-"
            out.append(f"| {r['index']} | {md_escape(r['input'])} | {r['branch']} | {md_escape(ps)} | "
                       f"{md_escape(r['plan'])} | {r['verdict']} |")
    else:
        out.append("（无）")
    out.append("")
    out.append(f"### 3.4 设备未匹配/歧义（{len(missing)} 条）")
    out.append("")
    if missing:
        out.append("| # | 输入 | device_hint | match | 结果 |")
        out.append("|---|---|---|---|---|")
        for r in missing:
            m = r["match"] or {}
            out.append(f"| {r['index']} | {md_escape(r['input'])} | {md_escape(r['device_hint'])} | "
                       f"{m.get('status', '-')} | {md_escape(r['plan'])} |")
    else:
        out.append("（无）")
    out.append("")
    flag_counts = {}
    for r in records:
        for f in r["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    out.append(f"### 3.5 复核标记（{sum(flag_counts.values())} 条命中）")
    out.append("")
    out.append("| flag | 条数 | 含义 |")
    out.append("|---|---|---|")
    flag_meaning = {
        "percent_expr": "含『百分之X』表达，数值需核对（见 §5）",
        "fuzzy_hint": "模糊命中：device_hint 含额外文本，语义可能被丢弃",
        "prop_unspecified": "控制分支未解析出属性",
    }
    for f, c in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
        out.append(f"| {f} | {c} | {flag_meaning.get(f, '')} |")
    out.append("")
    out.append("## 4. 逐条明细")
    out.append("")
    out.append("| # | 输入 | 归一化 | branch | 目标 | 解析 | 执行计划 | verdict | flags |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in records:
        p = r["parsed"]
        if p:
            ps = f"{p['action']} prop={p['prop']} value={p['value']} unit={p['unit']!r} dir={p['direction']} δ={p['delta']}"
        else:
            ps = "-"
        if r["branch"] == "scene":
            target = r["scene_name"] or r["scene_shortcut"] or "-"
        elif r["branch"] == "query":
            target = f"{r['device_hint']} (prop={r['query_prop'] or '-'})"
        else:
            target = r["device_hint"]
        out.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["index"], md_escape(r["input"]), md_escape(r["normalized"]) or "-", r["branch"],
            md_escape(target) or "-", md_escape(ps), md_escape(r["plan"]), r["verdict"],
            ", ".join(r["flags"]) or "-"))
    out.append("")
    out.append("## 5. 已核验的解析缺陷（探针实测，非脚本推导）")
    out.append("")
    out.append("以下结论均由 `nlp` 层实际函数输出核对（不依赖本脚本的模拟设备表），可直接作为修复输入：")
    out.append("")
    out.append("| # | 现象 | 影响命令 | 实测 |")
    out.append("|---|---|---|---|")
    for idx, symptom, affected, evidence in FINDINGS:
        out.append(f"| {idx} | {symptom} | {affected} | {md_escape(evidence)} |")
    out.append("")
    MD_OUT.write_text("\n".join(out), encoding="utf-8")

    print("commands      :", len(records))
    print("branches      :", json.dumps(branch_counts, ensure_ascii=False))
    print("focus (parse/capability) :", len(focus))
    print("device missing/ambiguous :", len(missing))
    print("flags         :", json.dumps(flag_counts, ensure_ascii=False))
    print("json ->", JSON_OUT)
    print("md   ->", MD_OUT)


if __name__ == "__main__":
    main()
