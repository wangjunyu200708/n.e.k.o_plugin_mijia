"""`list_devices` 的规格预取行为（并发 + 按 model 去重）。

阶段 2 把「循环内逐台 await get_device_spec」改成「按 model 去重后 gather 预取」，
冷启动（首次调用触发全量刷新）时不再串行排队。这里钉住两条不变式：

  * 同型号的多台设备只拉一次规格（去重）
  * 规格缺失/失败仍标记刷新不完整（不把半残缓存以 v2 固化）

外加：TokenExpiredError 必须向上抛（外层统一回「凭据已过期」），不能被 gather
吞成「规格失败」。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


class _Device:
    def __init__(self, did, name, model, room_id=None, online=True):
        self.did = did
        self.name = name
        self.model = model
        self.room_id = room_id
        self._online = online

    def is_online(self) -> bool:
        return self._online


class _Prop:
    def __init__(self, siid, piid, name, type_="bool", access="read_write"):
        self.siid = siid
        self.piid = piid
        self.name = name
        self.type = type_
        self.access = access
        self.value_range = None
        self.value_list = None
        self.service_description = None


class _Action:
    def __init__(self, siid, aiid, name):
        self.siid = siid
        self.aiid = aiid
        self.name = name
        self.parameters = None


class _Spec:
    def __init__(self, properties, actions):
        self.properties = properties
        self.actions = actions


def _make_plugin(root, api):
    plugin = root.MijiaPlugin.__new__(root.MijiaPlugin)
    plugin.logger = MagicMock()
    plugin.api = api
    plugin.data_path = lambda *_a, **_k: "unused.json"
    plugin._build_room_maps = AsyncMock(return_value=({}, {}))
    return plugin


def _patch_fs(monkeypatch, root, *, cached=None):
    """把插件模块级的异步文件读写换成内存桩。"""
    written: dict = {}

    async def _read(_path, *_a, **_k):
        if cached is None:
            raise FileNotFoundError("no cache")
        return cached

    async def _write(_path, data, **_k):
        written["data"] = data

    monkeypatch.setattr(root, "read_json_async", _read)
    monkeypatch.setattr(root, "atomic_write_json_async", _write)
    return written


async def test_spec_fetched_once_per_unique_model(plugin_module, monkeypatch):
    """同型号的三台设备只拉一次规格；三台都要拿到 properties。"""
    api = AsyncMock()
    api.credential = MagicMock(user_id="u1")
    api.get_devices.return_value = [
        _Device("d1", "灯1", "modelA"),
        _Device("d2", "灯2", "modelA"),
        _Device("d3", "空调", "modelB"),
    ]
    spec_by_model = {
        "modelA": _Spec([_Prop(2, 1, "Power")], []),
        "modelB": _Spec([_Prop(2, 1, "Power")], []),
    }
    seen: list[str] = []

    async def _get_spec(model):
        seen.append(model)
        return spec_by_model[model]

    api.get_device_spec.side_effect = _get_spec
    plugin = _make_plugin(plugin_module, api)
    _patch_fs(monkeypatch, plugin_module)

    out = await plugin.list_devices(home_id="h1", refresh=True)

    assert out.is_ok()
    assert sorted(seen) == ["modelA", "modelB"], "同型号必须去重，只拉一次"
    devices = out.value["devices"]
    assert len(devices) == 3
    assert all(d.get("properties") for d in devices), "每台设备都要拿到规格"


async def test_spec_failure_marks_refresh_incomplete(plugin_module, monkeypatch):
    """规格拉取失败的设备不写 properties，且缓存版本号保留旧值（不固化 v2）。"""
    api = AsyncMock()
    api.credential = MagicMock(user_id="u1")
    api.get_devices.return_value = [_Device("d1", "灯", "modelA")]
    api.get_device_spec.side_effect = RuntimeError("spec endpoint down")
    plugin = _make_plugin(plugin_module, api)
    written = _patch_fs(
        monkeypatch,
        plugin_module,
        cached={
            "schema_version": 1,  # 旧版本
            "user_id": "u1",
            "home_id": "h1",
            "devices": [{"did": "d1", "name": "灯", "properties": [{"siid": 2}]}],
        },
    )

    out = await plugin.list_devices(home_id="h1", refresh=True)

    assert out.is_ok()
    # 规格失败 → 回填旧缓存属性
    assert out.value["devices"][0].get("properties") == [{"siid": 2}]
    # 不完整刷新：版本号必须沿用旧值（1），否则旧格式会被以 v2 固化
    assert written["data"]["schema_version"] == 1


async def test_token_expired_propagates_not_swallowed(plugin_module, monkeypatch):
    """TokenExpiredError 必须向上抛，不能被 gather 吞成「规格失败」。"""
    from mijia_plugin.mijia_api.domain.exceptions import TokenExpiredError

    api = AsyncMock()
    api.credential = MagicMock(user_id="u1")
    api.get_devices.return_value = [_Device("d1", "灯", "modelA")]
    api.get_device_spec.side_effect = TokenExpiredError("expired")
    plugin = _make_plugin(plugin_module, api)
    _patch_fs(monkeypatch, plugin_module)

    out = await plugin.list_devices(home_id="h1", refresh=True)

    # list_devices 外层捕获 TokenExpiredError 并返回 Err（凭据已过期）
    assert not out.is_ok()
    assert "过期" in str(out.error)


class TestLoadDevicesCacheErrorSemantics:
    """刷新失败必须与「账号下没有设备」区分开。

    原先失败时 `return []`，空列表进 route() 后落到 not_found，用户听到的是
    「设备没匹配上」——把未登录/网络错报成了匹配失败。
    """

    async def test_refresh_failure_carries_reason(self, plugin_module, monkeypatch):
        api = AsyncMock()
        api.credential = MagicMock(user_id="u1")
        plugin = _make_plugin(plugin_module, api)
        _patch_fs(monkeypatch, plugin_module)  # 无缓存
        plugin.list_devices = AsyncMock(
            return_value=plugin_module.Err(plugin_module.SdkError("未登录"))
        )

        devices, failure = await plugin._load_devices_cache()

        assert devices == []
        assert failure == "未登录"

    async def test_empty_account_is_not_a_failure(self, plugin_module, monkeypatch):
        """账号下确实没有设备：devices 空，但 failure 为 None（不是错误）。"""
        api = AsyncMock()
        api.credential = MagicMock(user_id="u1")
        plugin = _make_plugin(plugin_module, api)
        _patch_fs(monkeypatch, plugin_module)
        plugin.list_devices = AsyncMock(
            return_value=plugin_module.Ok({"devices": []})
        )

        devices, failure = await plugin._load_devices_cache()

        assert devices == []
        assert failure is None

    async def test_cache_hit_reports_no_failure(self, plugin_module, monkeypatch):
        api = AsyncMock()
        api.credential = MagicMock(user_id="u1")
        plugin = _make_plugin(plugin_module, api)
        _patch_fs(
            monkeypatch,
            plugin_module,
            cached={
                "schema_version": 2,
                "user_id": "u1",
                "devices": [{"did": "d1", "name": "灯"}],
            },
        )

        devices, failure = await plugin._load_devices_cache()

        assert [d["did"] for d in devices] == ["d1"]
        assert failure is None

    async def test_smart_control_surfaces_load_failure(self, plugin_module):
        """smart_control 报「无法获取设备列表」，而不是「设备没匹配上」。"""
        api = AsyncMock()
        plugin = _make_plugin(plugin_module, api)
        plugin._load_devices_cache = AsyncMock(return_value=([], "未登录"))

        out = await plugin._dispatch_smart_control("打开卧室灯")

        assert not out.is_ok()
        assert "无法获取设备列表" in str(out.error)

    async def test_query_device_state_surfaces_load_failure(self, plugin_module):
        api = AsyncMock()
        plugin = _make_plugin(plugin_module, api)
        plugin._load_devices_cache = AsyncMock(return_value=([], "网络错误"))

        out = await plugin.query_device_state("卧室灯")

        assert not out.is_ok()
        assert "无法获取设备列表" in str(out.error)