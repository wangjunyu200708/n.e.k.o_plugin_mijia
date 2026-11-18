"""多指令分发（一句话控多设备）的分发层测试。

分两层：
  * **预检**（`_resolve_segment` / `_segment_routable`）走真实 `nlp.route`，
    验证"某段能不能执行""共享动词补得上补不上"；
  * **汇总**（`_execute_multi`）把 `_dispatch_single` 换成桩，只验证成功/失败
    的统计与错误码——真实下发由 `test_execution_branches.py` 覆盖。

之所以要预检：拆分是"宁可切错也不漏切"的，切坏设备名（"和风空调"）时
必须整句回退，否则会把半句当成真指令执行。
"""

from unittest.mock import AsyncMock, MagicMock


def _devices() -> list[dict]:
    """客厅灯 / 卧室灯 / 客厅插座：够覆盖匹配成功与失败。"""
    return [
        {"did": "d1", "name": "客厅灯", "alias": "", "room_name": "客厅",
         "is_online": True, "properties": [], "actions": []},
        {"did": "d2", "name": "卧室灯", "alias": "", "room_name": "卧室",
         "is_online": True, "properties": [], "actions": []},
        {"did": "d3", "name": "插座", "alias": "", "room_name": "客厅",
         "is_online": True, "properties": [], "actions": []},
    ]


def _make_plugin(root, *, devices=None, load_failure=None):
    """只挂分发层真正读到的属性，绕过 __init__。"""
    plugin = root.MijiaPlugin.__new__(root.MijiaPlugin)
    plugin.logger = MagicMock()
    plugin.api = AsyncMock()
    plugin._lang = "zh-CN"

    async def _load():
        return (devices if devices is not None else _devices()), load_failure

    # 实例属性覆盖方法：调用时不会自动传 self
    plugin._load_devices_cache = _load
    return plugin


class TestSegmentRoutable:
    """预检判据：场景/查询放行，开关/控制必须匹配到设备。"""

    async def test_switch_to_known_device(self, plugin_module):
        plugin = _make_plugin(plugin_module)
        assert await plugin._segment_routable("打开客厅灯", _devices()) is True

    async def test_unknown_branch_rejected(self, plugin_module):
        """'打开' 只剩动词、没有设备 → 这条指令不成立。"""
        plugin = _make_plugin(plugin_module)
        assert await plugin._segment_routable("打开", _devices()) is False

    async def test_unmatched_device_rejected(self, plugin_module):
        plugin = _make_plugin(plugin_module)
        assert await plugin._segment_routable("打开车库门", _devices()) is False

    async def test_scene_always_routable(self, plugin_module):
        """场景不依赖设备缓存，预检不能因为设备列表为空就否掉。"""
        plugin = _make_plugin(plugin_module)
        assert await plugin._segment_routable("执行回家场景", []) is True

    async def test_query_routable(self, plugin_module):
        """查询的匹配发生在 query_device_state 内部，预检放行。"""
        plugin = _make_plugin(plugin_module)
        assert await plugin._segment_routable("客厅灯亮度是多少", _devices()) is True


class TestResolveSegment:
    async def test_keeps_segment_that_works_alone(self, plugin_module):
        plugin = _make_plugin(plugin_module)
        assert await plugin._resolve_segment("关掉插座", "打开", _devices()) == "关掉插座"

    async def test_inherits_shared_verb(self, plugin_module):
        """'打开客厅灯和卧室灯' 的第二段 '卧室灯' 自身不成立，补动词后成立。"""
        plugin = _make_plugin(plugin_module)
        assert await plugin._resolve_segment("卧室灯", "打开", _devices()) == "打开卧室灯"

    async def test_returns_none_when_nothing_works(self, plugin_module):
        plugin = _make_plugin(plugin_module)
        assert await plugin._resolve_segment("风空调", "", _devices()) is None

    async def test_no_verb_to_inherit(self, plugin_module):
        plugin = _make_plugin(plugin_module)
        assert await plugin._resolve_segment("卧室灯", "", _devices()) is None


class TestDispatchMultiFallsBack:
    """拆分不成立时必须返回 None，让调用方整句回退。"""

    async def test_unroutable_segment_aborts_whole_split(self, plugin_module):
        """'打开和风空调' 被切成 ['打开','风空调']，两段都接不住 → 回退。"""
        plugin = _make_plugin(plugin_module)
        assert await plugin._dispatch_multi(["打开", "风空调"]) is None

    async def test_load_failure_defers_to_single_path(self, plugin_module):
        """设备列表拿不到时不要在拆分层报错，交给单指令路径报真实原因。"""
        plugin = _make_plugin(plugin_module, devices=[], load_failure="未登录米家账号")
        assert await plugin._dispatch_multi(["打开客厅灯", "关掉插座"]) is None

    async def test_resolvable_split_proceeds(self, plugin_module):
        """正常拆分：第二段补上共享动词后成立，进入执行。"""
        plugin = _make_plugin(plugin_module)
        seen: list[str] = []

        async def _record(cmd, *, devices=None):
            seen.append(cmd)
            return plugin_module.Ok({"success": True, "message": f"已执行 {cmd}"})

        plugin._dispatch_single = _record
        result = await plugin._dispatch_multi(["打开客厅灯", "卧室灯"])

        assert seen == ["打开客厅灯", "打开卧室灯"]
        assert result.is_ok()
        assert result.value["total"] == 2


class TestExecuteMultiSummary:
    """汇总：全成功走 Ok，有失败走 Err（含逐条 results）。"""

    async def test_all_success(self, plugin_module):
        plugin = _make_plugin(plugin_module)

        async def _ok(cmd, *, devices=None):
            return plugin_module.Ok({"success": True, "message": f"已执行 {cmd}"})

        plugin._dispatch_single = _ok
        result = await plugin._execute_multi(["打开客厅灯", "关掉插座"], _devices())

        assert result.is_ok()
        out = result.value
        assert out["success"] is True
        assert out["total"] == 2
        assert [r["success"] for r in out["results"]] == [True, True]
        assert "2 条指令：2 成功" in out["message"]
        assert "✅ 打开客厅灯" in out["message"]

    async def test_partial_failure_uses_error_envelope(self, plugin_module):
        plugin = _make_plugin(plugin_module)

        async def _mixed(cmd, *, devices=None):
            if cmd == "打开客厅灯":
                return plugin_module.Ok({"success": True, "message": "已打开客厅灯"})
            return plugin_module.Err(plugin_module.SdkError("未找到设备「车库门」", code="device_not_found"))

        plugin._dispatch_single = _mixed
        result = await plugin._execute_multi(["打开客厅灯", "打开车库门"], _devices())

        assert not result.is_ok(), "部分失败必须走 Err，否则模型以为全部生效"
        err = result.error
        assert err.code == "multi_control_partial_failure"
        assert err.details["succeeded"] == 1
        assert err.details["failed"] == 1
        assert err.details["results"][0]["success"] is True
        assert err.details["results"][1]["code"] == "device_not_found"
        assert "2 条指令：1 成功，1 失败" in str(err)

    async def test_all_failed_uses_distinct_code(self, plugin_module):
        plugin = _make_plugin(plugin_module)

        async def _fail(cmd, *, devices=None):
            return plugin_module.Err(plugin_module.SdkError("执行失败", code="control_failed"))

        plugin._dispatch_single = _fail
        result = await plugin._execute_multi(["打开车库门", "关掉地暖"], _devices())

        assert not result.is_ok()
        assert result.error.code == "multi_control_failed"
        assert result.error.details["succeeded"] == 0

    async def test_success_item_carries_message(self, plugin_module):
        plugin = _make_plugin(plugin_module)

        async def _ok(cmd, *, devices=None):
            return plugin_module.Ok({"success": True, "message": "已打开客厅灯"})

        plugin._dispatch_single = _ok
        result = await plugin._execute_multi(["打开客厅灯"], _devices())
        assert result.value["results"][0]["message"] == "已打开客厅灯"


class TestDispatchSmartControlEntry:
    """入口只做"要不要拆"的判断，单指令路径必须原样透传。"""

    async def test_single_command_not_split(self, plugin_module):
        plugin = _make_plugin(plugin_module)
        seen: list[str] = []

        async def _record(cmd, *, devices=None):
            seen.append(cmd)
            return plugin_module.Ok({"success": True, "message": "ok"})

        plugin._dispatch_single = _record
        await plugin._dispatch_smart_control("打开卧室灯")
        assert seen == ["打开卧室灯"]

    async def test_multi_command_split_and_executed(self, plugin_module):
        plugin = _make_plugin(plugin_module)
        seen: list[str] = []

        async def _record(cmd, *, devices=None):
            seen.append(cmd)
            return plugin_module.Ok({"success": True, "message": "ok"})

        plugin._dispatch_single = _record
        await plugin._dispatch_smart_control("打开客厅灯和卧室灯")
        assert seen == ["打开客厅灯", "打开卧室灯"]

    async def test_not_logged_in(self, plugin_module):
        plugin = _make_plugin(plugin_module)
        plugin.api = None
        result = await plugin._dispatch_smart_control("打开客厅灯和卧室灯")

        assert not result.is_ok()
        assert result.error.code == "not_logged_in"

    async def test_degenerate_split_falls_back_to_whole_sentence(self, plugin_module):
        """设备名含"和"被切坏时，整句交给单指令路径（由它报设备未找到）。"""
        plugin = _make_plugin(plugin_module)
        seen: list[str] = []

        async def _record(cmd, *, devices=None):
            seen.append(cmd)
            return plugin_module.Ok({"success": True, "message": "ok"})

        plugin._dispatch_single = _record
        await plugin._dispatch_smart_control("打开和风空调")
        assert seen == ["打开和风空调"]
