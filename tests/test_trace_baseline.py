"""中文行为基线回归：把 `_trace_commands.py` 的执行结果钉成断言。

`tests/mijia_trace_simulation.md`（24 条黄金用例）和 `commands_execution_trace.*`
是这次重构的**中文行为基线**——抽 CommandIR 缝、补能力校验、并发化规格拉取这些
改动都不应改变任何一条中文指令的解析结果。但基线是脚本产物、不在测试里，重构时
很容易「改了行为但没人发现」。

本测试跑同一套输入，与**已提交的基线 JSON** 逐条比对；不一致即失败，并打印
diff 摘要。基线有意变更时必须显式更新 `BASELINE`（并说明原因）。

比对范围刻意只取**稳定字段**（branch / device / prop / value / unit / verdict），
不取消息文案与耗时——文案会随 i18n 改动，不该让它们拦住重构。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRACE_SCRIPT = ROOT / "tests" / "_trace_commands.py"
BASELINE = ROOT / "tests" / "commands_execution_trace.json"
CMDS = ROOT / "tests" / "智能家居语音常用命令.txt"

# 只比对语义字段；文案/耗时/内部诊断不参与，避免无关改动误报。
# 记录的实际形状见 commands_execution_trace.json：
#   index / input / normalized / scene_shortcut / branch / device_hint /
#   scene_name / verb / query_prop / match{status,devices,message} /
#   parsed{device,action,prop,value,unit,direction,delta} / plan / verdict / flags
TOP_KEYS = ("branch", "device_hint", "scene_name", "verb", "query_prop", "verdict")

# 变更 log_text 的文案（plan）不参与比对——它随 i18n/措辞改动，不该拦住重构。
def _load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _stable_view(record: dict) -> dict:
    """抽出与文案无关的语义视图。

    match 只取 status + 匹配到的设备名（顺序敏感，能抓到匹配优先级漂移）；
    parsed 取全部语义字段（device/action/prop/value/unit/direction/delta）。
    """
    view = {k: record.get(k) for k in TOP_KEYS}
    match = record.get("match") or {}
    view["match_status"] = match.get("status")
    view["match_devices"] = [
        d.get("name") for d in (match.get("devices") or []) if isinstance(d, dict)
    ]
    parsed = record.get("parsed") or {}
    for key in ("device", "action", "prop", "value", "unit", "direction", "delta"):
        view[f"parsed.{key}"] = parsed.get(key)
    return view


def _records(doc) -> list:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("records", "results", "items", "cases"):
            if isinstance(doc.get(key), list):
                return doc[key]
    return []


pytestmark = pytest.mark.skipif(
    not (TRACE_SCRIPT.is_file() and BASELINE.is_file() and CMDS.is_file()),
    reason="trace 脚本 / 基线 / 命令表不全（可能未提交）",
)


@pytest.fixture(scope="module")
def fresh_trace(tmp_path_factory) -> dict:
    """跑一次 trace，输出到临时目录（不覆盖已提交的基线）。"""
    out_json = tmp_path_factory.mktemp("trace") / "out.json"
    out_md = out_json.with_suffix(".md")
    # 脚本把输出路径写死在 tests/ 下，用 import 后改常量再跑，避免污染工作区。
    spec = importlib.util.spec_from_file_location("_trace_commands_probe", TRACE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.JSON_OUT = out_json
    mod.MD_OUT = out_md
    mod.main()
    return json.loads(out_json.read_text(encoding="utf-8"))


def test_trace_command_count_matches_baseline(fresh_trace):
    """命令条数一致——基线被截断/追加时会立刻暴露。"""
    base = _load_baseline()
    assert len(_records(fresh_trace)) == len(_records(base))


def test_trace_semantics_unchanged(fresh_trace):
    """逐条比对稳定字段；输出前 N 条差异便于定位。"""
    base_records = _records(_load_baseline())
    new_records = _records(fresh_trace)
    assert len(new_records) == len(base_records), "条数不一致，先修条数再比语义"

    diffs = []
    for i, (old, new) in enumerate(zip(base_records, new_records)):
        old_view, new_view = _stable_view(old), _stable_view(new)
        if old_view != new_view:
            changed = sorted(
                k for k in set(old_view) | set(new_view)
                if old_view.get(k) != new_view.get(k)
            )
            diffs.append(
                f"#{old.get('index')} {old.get('input')!r}\n"
                f"    变更字段={changed}\n"
                f"    old={ {k: old_view.get(k) for k in changed} }\n"
                f"    new={ {k: new_view.get(k) for k in changed} }"
            )
    assert not diffs, f"{len(diffs)} 条中文指令的解析结果发生变化：\n" + "\n".join(diffs[:10])


def test_trace_branch_distribution_stable(fresh_trace):
    """分支分布是整体漂移的粗粒度探针（单条 diff 之外的兜底）。"""
    def dist(doc) -> dict:
        out: dict = {}
        for r in _records(doc):
            b = r.get("branch")
            out[b] = out.get(b, 0) + 1
        return out

    assert dist(fresh_trace) == dist(_load_baseline())
