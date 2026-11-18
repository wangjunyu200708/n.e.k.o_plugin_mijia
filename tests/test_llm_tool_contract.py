"""@llm_tool 返回信封的契约测试（_tool_error / _tool_envelope）。

为什么用源码提取而不是 import：仓库根 `__init__.py` 是插件入口，模块级 import
宿主专属模块（`config` / `plugin.sdk` / 包内相对 import），在 staging 布局（只拷
`nlp/` + `tests/`，没有根包的 `__init__.py`）里 pytest 导入它必失败（见
AGENTS.md「运行测试（关键坑）」）。`_tool_error` / `_tool_envelope` 是零依赖纯
函数，按 AST 从真实源码摘出来 exec，验证的仍是真代码，却不依赖任何宿主环境。

契约来源：references/21-llm-tool-registration.md §错误返回 + §注意事项 4，
以及 plugin/server/routes/llm_tools.py:196-218 的信封判定——
  * 成功 → {"output": <Ok.value>, "is_error": False}
  * 失败 → {"output": {"error": <人类可读原因>}, "is_error": True, "error": <稳定码>}
  * 非 Result 的值原样进 output（不带 is_error，交给宿主包信封）

`test_ok_envelope_never_reports_failure` 守的是改造时踩到的坑：内部用
`Ok({"success": False})` 表示语义失败时，信封会把 is_error 标成 false 对模型假
报成功——故内部一切语义失败都必须走 Err。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_HELPERS = ("_tool_error", "_tool_envelope")


def _locate_plugin_entry() -> Path | None:
    """从 tests/ 向上找插件入口 __init__.py。

    staging 布局下 `tests/` 的直接父目录没有 `__init__.py`（正是这一点让 pytest
    能跑），但上一级就是仓库根，仍能定位到真源码；CI 的宿主布局下第一跳即命中。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "__init__.py"
        if candidate.is_file() and "_tool_envelope" in candidate.read_text(encoding="utf-8"):
            return candidate
    return None


_SRC = _locate_plugin_entry()
if _SRC is None:  # pragma: no cover - 环境定位失败时不误判为回归
    pytest.skip("找不到插件入口 __init__.py，跳过信封契约测试", allow_module_level=True)


def _load_helpers() -> dict:
    text = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(_SRC))
    lines = text.splitlines()
    funcs = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _HELPERS
    ]
    assert {f.name for f in funcs} == set(_HELPERS), "@llm_tool 信封 helper 被改名/删除，请同步本测试"
    source = "\n".join("\n".join(lines[f.lineno - 1:f.end_lineno]) for f in funcs)
    namespace: dict = {"Any": object}
    exec(compile(source, str(_SRC), "exec"), namespace)  # noqa: S102
    return namespace


_H = _load_helpers()
_tool_error = _H["_tool_error"]
_tool_envelope = _H["_tool_envelope"]


class _Ok:
    def __init__(self, value):
        self.value = value

    def is_ok(self):
        return True


class _Err:
    def __init__(self, error):
        self.error = error

    def is_ok(self):
        return False


class _SdkError(Exception):
    """与宿主 SdkError 一致的最小面：code / details 可选。"""

    def __init__(self, message="", *, code=None, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


@pytest.mark.parametrize(
    "value",
    [
        {"success": True, "message": "✅ 已打开卧室灯"},
        {"success": True, "message": "✅ 已执行场景'回家'"},
        [],
        "",
    ],
)
def test_ok_becomes_success_envelope(value):
    assert _tool_envelope(_Ok(value)) == {"output": value, "is_error": False}


def test_err_becomes_error_envelope_with_stable_code():
    got = _tool_envelope(_Err(_SdkError("未找到设备'卧室灯'")))
    assert got["is_error"] is True
    # output 是结构化错误：code + detail + message；error 与 code 同为稳定码
    assert got["output"] == {
        "code": "control_failed",
        "detail": {},
        "message": "未找到设备'卧室灯'",
    }
    assert got["error"] == "control_failed"


def test_err_carries_code_and_detail_from_sdk_error():
    """内部 _err(code, msg, **detail) → SdkError(code=, details=) → 工具输出。"""
    err = _SdkError(
        "未找到设备'卧室灯'",
        code="device_not_found",
        details={"candidates": [{"name": "卧室灯", "room": "主卧"}]},
    )
    got = _tool_envelope(_Err(err))
    assert got["output"]["code"] == "device_not_found"
    assert got["output"]["detail"] == {"candidates": [{"name": "卧室灯", "room": "主卧"}]}
    assert got["error"] == "device_not_found"


def test_ok_never_reports_failure():
    """语义失败必须走 Err：Ok 一律 is_error=false，别指望它表达失败。"""
    assert _tool_envelope(_Ok({"success": False, "message": "❌ 失败"}))["is_error"] is False


def test_tool_error_shape_uses_caller_supplied_code():
    got = _tool_error("缺少 command 参数", code="missing_command")
    assert got == {
        "output": {"code": "missing_command", "detail": {}, "message": "缺少 command 参数"},
        "is_error": True,
        "error": "missing_command",
    }


def test_tool_error_includes_detail():
    got = _tool_error(
        "有多个开关", code="gang_ambiguous",
        detail={"gang_options": [{"index": 1, "label": "Left"}]},
    )
    assert got["output"]["detail"] == {"gang_options": [{"index": 1, "label": "Left"}]}


def test_plain_value_passthrough_keeps_value_intact():
    got = _tool_envelope({"anything": 1})
    assert got == {"output": {"anything": 1}, "is_error": False}
    assert got["output"] == {"anything": 1}


def test_envelopes_are_json_serialisable():
    for result in (_Ok({"success": True, "message": "✅"}), _Err(_SdkError("失败"))):
        json.dumps(_tool_envelope(result), ensure_ascii=False)
