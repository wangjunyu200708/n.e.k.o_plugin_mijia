"""执行分支的直测（回归网）。

为什么单独写：`tests/` 里 145+ 个用例只覆盖 `nlp/`（纯函数子包），而
`_execute_switch` / `_execute_adjust_prop` / `_execute_set_prop` /
`_execute_action_branch` / `_execute_control_branch` 这五个真正下发设备指令的
方法在 `__init__.py` 里**零覆盖**。阶段性重构（抽 CommandIR 缝、补能力校验、
并发化规格拉取）没有这层网就无从验收。

做法：`MijiaPlugin` 在 `conftest.py` 加载的 root 模块里可直接取到（模块级
import 已被桩件打通）。实例用 `MijiaPlugin.__new__(...)` 绕过 `__init__`，
只手挂执行分支真正读到的那几个属性（`logger` / `api` / `i18n`），不触发宿主
依赖。外部 API 用 `AsyncMock`。

覆盖的是**分支选择与结果信封**，不是米家云端的真实行为——真实行为只能实机验。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def make_plugin(root, *, api=None):
    """构造只带执行分支所需属性的插件实例。"""
    plugin = root.MijiaPlugin.__new__(root.MijiaPlugin)
    plugin.logger = MagicMock()
    plugin.api = api if api is not None else AsyncMock()
    # 真实 self.i18n.t(key, default=..., **params) 会用 params 渲染 default。
    # 桩件照做，否则断言只能看到未渲染的 '{name}' 模板。
    def _t(_key, default=None, **params):
        if default is None:
            return ""
        try:
            return default.format(**params)
        except Exception:
            return default

    i18n = MagicMock()
    i18n.t.side_effect = _t
    plugin.i18n = i18n
    return plugin


def _envelope_ok(result) -> bool:
    return getattr(result, "is_ok", lambda: False)()


def _envelope_error(result) -> str:
    return str(getattr(result, "error", ""))


def _ir(root, **fields):
    """构造 CommandIR（执行分支的入参）。

    执行分支只认 CommandIR——不再接受 RouteResult。这里用真实 dataclass，
    避免 MagicMock 把未设置的字段变成假对象（那样会掩盖真实签名）。
    """
    return root.CommandIR(**fields)


class TestExecuteSwitch:
    """二元开关：电源动作优先 → 开关属性 → 写后回读校验。"""

    async def test_power_action_success_short_circuits(self, plugin_module):
        """电源动作执行成功时直接返回，不再走属性通道。"""
        api = AsyncMock()
        api.call_device_action.return_value = True
        plugin = make_plugin(plugin_module, api=api)
        actions = [{"name": "Turn On", "siid": 6, "aiid": 1}]

        result = await plugin._execute_switch(
            [], actions, "did-1", "卧室灯", True, "打开卧室灯"
        )

        assert _envelope_ok(result)
        assert result.value["success"] is True
        api.call_device_action.assert_awaited_once_with("did-1", 6, 1)
        api.control_device.assert_not_awaited()

    async def test_power_action_failure_falls_back_to_property(self, plugin_module):
        """动作被拒（False）时回落到属性通道，而不是直接报成功。"""
        api = AsyncMock()
        api.call_device_action.return_value = False
        api.control_device.return_value = True
        api.get_device_properties.return_value = [{"value": True}]
        plugin = make_plugin(plugin_module, api=api)
        actions = [{"name": "Turn On", "siid": 6, "aiid": 1}]
        props = [
            {
                "name": "Power",
                "access": "read_write",
                "type": "bool",
                "siid": 2,
                "piid": 1,
            }
        ]

        result = await plugin._execute_switch(
            props, actions, "did-1", "卧室灯", True, "打开卧室灯"
        )

        assert _envelope_ok(result)
        api.call_device_action.assert_awaited_once()
        api.control_device.assert_awaited_once_with("did-1", 2, 1, True)

    async def test_write_only_property_reports_unconfirmed(self, plugin_module):
        """只写属性读不回来 → 返回 Err（「已发送但无法确认」），绝不假报成功。"""
        api = AsyncMock()
        api.call_device_action.return_value = False
        api.control_device.return_value = True
        plugin = make_plugin(plugin_module, api=api)
        props = [
            {
                "name": "Power",
                "access": "write",
                "type": "bool",
                "siid": 2,
                "piid": 1,
            }
        ]

        result = await plugin._execute_switch(
            props, [], "did-1", "电视", True, "打开电视"
        )

        assert not _envelope_ok(result)
        assert "电视" in _envelope_error(result)

    async def test_readback_mismatch_reports_failure(self, plugin_module):
        """回读值与期望不符 → Err（写入未生效）。"""
        api = AsyncMock()
        api.call_device_action.return_value = False
        api.control_device.return_value = True
        # 三次轮询都读到 False（期望 True）
        api.get_device_properties.return_value = [{"value": False}]
        plugin = make_plugin(plugin_module, api=api)
        props = [
            {
                "name": "Power",
                "access": "read_write",
                "type": "bool",
                "siid": 2,
                "piid": 1,
            }
        ]

        result = await plugin._execute_switch(
            props, [], "did-1", "卧室灯", True, "打开卧室灯"
        )

        assert not _envelope_ok(result)

    async def test_no_switch_property_found(self, plugin_module):
        """设备没有任何 bool 型开关属性 → Err 并说明。"""
        plugin = make_plugin(plugin_module)
        props = [{"name": "Brightness", "access": "read_write", "type": "uint8"}]

        result = await plugin._execute_switch(
            props, [], "did-1", "台灯", True, "打开台灯"
        )

        assert not _envelope_ok(result)
        assert "台灯" in _envelope_error(result)

    async def test_non_bool_named_on_is_filtered_out(self, plugin_module):
        """名字含 on 但非 bool 的属性（TV Input Control 等）必须被过滤。"""
        plugin = make_plugin(plugin_module)
        props = [
            {"name": "TV Input Control", "access": "read_write", "type": "uint8"},
        ]

        result = await plugin._execute_switch(
            props, [], "did-1", "电视", True, "打开电视"
        )

        assert not _envelope_ok(result)


class TestExecuteActionBranch:
    """动作分支：动词 → 动作名（精确优先，模糊兜底）。"""

    async def test_exact_action_match(self, plugin_module):
        api = AsyncMock()
        api.call_device_action.return_value = True
        plugin = make_plugin(plugin_module, api=api)

        out = await plugin._execute_action_branch(_ir(plugin_module, **{
            "action": "action", "verb": "开始",
            "match_status": "ok", "device": "扫地机",
            "device_id": "did-1",
            "device_info": {
                "did": "did-1",
                "name": "扫地机",
                "actions": [{"name": "Start", "siid": 3, "aiid": 1}],
            },
        }))

        assert _envelope_ok(out)
        api.call_device_action.assert_awaited_once_with("did-1", 3, 1)

    async def test_action_rejected_returns_err(self, plugin_module):
        """call_device_action 返回 False = 被拒，不能报成功。"""
        api = AsyncMock()
        api.call_device_action.return_value = False
        plugin = make_plugin(plugin_module, api=api)

        out = await plugin._execute_action_branch(_ir(plugin_module, **{
            "action": "action", "verb": "开始",
            "match_status": "ok", "device": "扫地机",
            "device_id": "did-1",
            "device_info": {
                "did": "did-1",
                "name": "扫地机",
                "actions": [{"name": "Start", "siid": 3, "aiid": 1}],
            },
        }))

        assert not _envelope_ok(out)

    async def test_ambiguous_device_returns_match_message(self, plugin_module):
        plugin = make_plugin(plugin_module)

        out = await plugin._execute_action_branch(_ir(plugin_module, **{
            "action": "action", "verb": "开始", "device": "灯",
            "match_status": "ambiguous",
            "match_message": "找到多个匹配设备：客厅灯、卧室灯",
        }))

        assert not _envelope_ok(out)
        assert "客厅灯" in _envelope_error(out)


class TestExecuteControlBranch:
    """控制分支：按 ir.action 分派到 switch / adjust_prop / set_prop。"""

    async def test_unmatched_device_returns_err(self, plugin_module):
        plugin = make_plugin(plugin_module)

        out = await plugin._execute_control_branch(_ir(plugin_module, **{
            "action": "switch", "match_status": "not_found",
            "match_message": "未找到设备",
        }))

        assert not _envelope_ok(out)

    async def test_missing_action_returns_err(self, plugin_module):
        plugin = make_plugin(plugin_module)

        out = await plugin._execute_control_branch(_ir(plugin_module, **{
            "action": "", "match_status": "ok", "device": "灯",
            "device_id": "d",
            "device_info": {"did": "d", "name": "灯"},
        }))

        assert not _envelope_ok(out)

    async def test_switch_action_dispatches_to_switch(self, plugin_module):
        """ir.action == 'switch' 时必须走 _execute_switch，不是 set_prop。"""
        api = AsyncMock()
        api.call_device_action.return_value = True
        plugin = make_plugin(plugin_module, api=api)
        plugin._execute_switch = AsyncMock(return_value="SWITCH_CALLED")

        out = await plugin._execute_control_branch(_ir(plugin_module, **{
            "action": "switch", "value": True, "raw_text": "开灯",
            "match_status": "ok", "device": "灯", "device_id": "did-1",
            "device_info": {
                "did": "did-1",
                "name": "灯",
                "properties": [],
                "actions": [{"name": "Turn On", "siid": 6, "aiid": 1}],
            },
        }))

        assert out == "SWITCH_CALLED"
        plugin._execute_switch.assert_awaited_once()


class TestExecuteSetPropCapability:
    """set_prop 的能力校验（单位/范围）——目前唯一接了 validate_command 的路径。"""

    async def test_out_of_range_value_is_rejected(self, plugin_module):
        api = AsyncMock()
        plugin = make_plugin(plugin_module, api=api)
        props = [
            {
                "name": "Target Temperature",
                "access": "read_write",
                "type": "uint8",
                "siid": 2,
                "piid": 1,
                "value_range": [16, 30],
            }
        ]

        out = await plugin._execute_set_prop(
            props, "did-1", "空调",
            _ir(plugin_module, attribute="温度", value=99, unit="度"),
        )

        assert not _envelope_ok(out)
        api.control_device.assert_not_awaited()


class TestExecuteAdjustPropCapability:
    """adjust_prop 路径的能力校验。

    现状：`validate_command` 只接在 `_execute_set_prop`，本路径全程不校验。
    下面的用例把该缺口固化成测试——补上校验后，断言方向要反转。
    """

    async def test_adjust_prop_currently_skips_capability_check(self, plugin_module):
        api = AsyncMock()
        # 调值前的读当前值
        api.get_device_properties.return_value = [{"value": 20}]
        api.control_device.return_value = True
        plugin = make_plugin(plugin_module, api=api)

        props = [
            {
                "name": "Target Temperature",
                "access": "read_write",
                "type": "uint8",
                "siid": 2,
                "piid": 1,
                "value_range": [16, 30],
            }
        ]

        out = await plugin._execute_adjust_prop(
            props, "did-1", "空调",
            _ir(plugin_module, attribute="温度", direction=1, delta=50),
        )

        # 现状：不校验，直接下发（补上校验后这里应改为断言 not _envelope_ok）
        assert _envelope_ok(out)
        api.control_device.assert_awaited()


class TestMovedTablesStillWork:
    """静态表搬到 property_tables.py 后，四条真实取用路径仍正确。

    这四张表原先定义在方法体内、每次调用重建；抽到模块级后行为必须不变。
    下面分别走一遍真实调用路径，防止「抽错了表/漏了引用」。
    """

    async def test_pos_map_selects_multi_gang_switch(self, plugin_module):
        """多控开关按方位词选择（_POS_MAP：右 → Right）。"""
        api = AsyncMock()
        api.call_device_action.return_value = False
        api.control_device.return_value = True
        api.get_device_properties.return_value = [{"value": True}]
        plugin = make_plugin(plugin_module, api=api)
        props = [
            {
                "name": "Switch Status",
                "access": "read_write",
                "type": "bool",
                "siid": 2,
                "piid": 1,
                "service_desc": "Left Switch",
            },
            {
                "name": "Switch Status",
                "access": "read_write",
                "type": "bool",
                "siid": 3,
                "piid": 1,
                "service_desc": "Right Switch",
            },
        ]

        await plugin._execute_switch(
            props, [], "did-1", "双控开关", True, "打开右边的开关"
        )

        api.control_device.assert_awaited_once_with("did-1", 3, 1, True)

    async def test_pos_map_chinese_numeral_gang(self, plugin_module):
        """「二键」→ Second（_POS_MAP 的中文数字分支）。"""
        api = AsyncMock()
        api.call_device_action.return_value = False
        api.control_device.return_value = True
        api.get_device_properties.return_value = [{"value": True}]
        plugin = make_plugin(plugin_module, api=api)
        props = [
            {
                "name": "Switch Status",
                "access": "read_write",
                "type": "bool",
                "siid": 2,
                "piid": 1,
                "service_desc": "First Switch",
            },
            {
                "name": "Switch Status",
                "access": "read_write",
                "type": "bool",
                "siid": 3,
                "piid": 1,
                "service_desc": "Second Switch",
            },
        ]

        await plugin._execute_switch(
            props, [], "did-1", "双控开关", True, "打开二键"
        )

        api.control_device.assert_awaited_once_with("did-1", 3, 1, True)

    def test_prop_keywords_maps_brightness(self, plugin_module):
        """PROP_KEYWORDS：中文属性名 → 英文 spec 属性（亮度 → Brightness）。"""
        plugin = make_plugin(plugin_module)
        props = [
            {"name": "Color Temperature", "access": "read_write", "type": "uint16"},
            {"name": "Brightness", "access": "read_write", "type": "uint8"},
        ]

        prop = plugin._find_property_for_control(props, "亮度", 50)

        assert prop is not None
        assert prop["name"] == "Brightness"

    def test_prop_keywords_maps_temperature(self, plugin_module):
        """PROP_KEYWORDS：温度 → target temperature（优先级高于泛化 temperature）。"""
        plugin = make_plugin(plugin_module)
        props = [
            {"name": "Current Temperature", "access": "read", "type": "float"},
            {"name": "Target Temperature", "access": "read_write", "type": "uint8"},
        ]

        prop = plugin._find_property_for_control(props, "温度", 26)

        assert prop is not None
        assert prop["name"] == "Target Temperature"

    def test_prop_keywords_ignores_readonly_props(self, plugin_module):
        """PROP_KEYWORDS 只匹配可写属性——只读属性不能作为控制目标。"""
        plugin = make_plugin(plugin_module)
        props = [
            {"name": "Brightness", "access": "read", "type": "uint8"},
        ]

        assert plugin._find_property_for_control(props, "亮度", 50) is None


class TestSeamFrontendB:
    """缝的证明：手工构造 CommandIR 直接驱动执行分支，全程不经过规则引擎。

    这是阶段 4（LLM 结构化前端）的接入预演——前端 B 产出的就是这样的 IR：
    attribute 直接是英文 spec 属性名、value 是已解析的数值。执行分支若仍依赖
    RouteResult / ParseResult，这里就通不过。
    """

    async def test_structured_set_prop_end_to_end(self, plugin_module):
        """前端 B 的 set_prop：attribute=Brightness（英文 spec 名），无中文解析。"""
        api = AsyncMock()
        api.control_device.return_value = True
        plugin = make_plugin(plugin_module, api=api)

        ir = _ir(
            plugin_module,
            raw_text="set living room light to 50",
            action="set_prop",
            device="Living Room Light",
            device_id="light-1",
            attribute="Brightness",
            value=50,
            unit="percent",
            match_status="ok",
            device_info={
                "did": "light-1",
                "name": "Living Room Light",
                "properties": [
                    {
                        "name": "Brightness",
                        "access": "read_write",
                        "type": "uint8",
                        "siid": 2,
                        "piid": 1,
                        "value_range": [1, 100],
                    }
                ],
                "actions": [],
            },
        )

        out = await plugin._execute_control_branch(ir)

        assert _envelope_ok(out)
        api.control_device.assert_awaited_once_with("light-1", 2, 1, 50)

    async def test_structured_switch_end_to_end(self, plugin_module):
        """前端 B 的 switch：action=switch、value=True，走电源动作通道。"""
        api = AsyncMock()
        api.call_device_action.return_value = True
        plugin = make_plugin(plugin_module, api=api)

        ir = _ir(
            plugin_module,
            raw_text="turn on the light",
            action="switch",
            device="Light",
            device_id="light-1",
            value=True,
            match_status="ok",
            device_info={
                "did": "light-1",
                "name": "Light",
                "properties": [],
                "actions": [{"name": "Turn On", "siid": 6, "aiid": 1}],
            },
        )

        out = await plugin._execute_control_branch(ir)

        assert _envelope_ok(out)
        api.call_device_action.assert_awaited_once_with("light-1", 6, 1)

    async def test_structured_action_end_to_end(self, plugin_module):
        """前端 B 的动作分支：verb 直接是英文（Start），VERB_TO_ACTION 退化到原词。"""
        api = AsyncMock()
        api.call_device_action.return_value = True
        plugin = make_plugin(plugin_module, api=api)

        ir = _ir(
            plugin_module,
            raw_text="start the vacuum",
            action="action",
            verb="start",
            device="Vacuum",
            device_id="vac-1",
            match_status="ok",
            device_info={
                "did": "vac-1",
                "name": "Vacuum",
                "actions": [{"name": "start", "siid": 3, "aiid": 1}],
            },
        )

        out = await plugin._execute_action_branch(ir)

        assert _envelope_ok(out)
        api.call_device_action.assert_awaited_once_with("vac-1", 3, 1)

    async def test_frontend_b_does_not_need_route_result(self, plugin_module):
        """构造一个没有 match 的 IR（前端 B 匹配失败时的形态），后端要能正确报错。"""
        plugin = make_plugin(plugin_module)

        ir = _ir(
            plugin_module,
            raw_text="set the oven to 200",
            action="set_prop",
            device="Oven",
            match_status="not_found",
            match_message="no device named 'Oven' found",
        )

        out = await plugin._execute_control_branch(ir)

        assert not _envelope_ok(out)
        assert "Oven" in _envelope_error(out)
