"""让插件在独立仓库里可被 pytest 直接导入（不再需要 staging 拷贝）。

背景：仓库根 `__init__.py` 是插件入口，模块级 import 了三个宿主专属模块
（`config` / `plugin.sdk.plugin` / `utils.file_utils`），并用相对 import 拉
`mijia_api` / `nlp`。目录名 `n.e.k.o_plugin_mijia` 不是合法标识符，pytest 无法
把它当包导入，于是既报 `No module named 'config'`，又报相对 import 失败。

本文件只在独立仓库生效——宿主树里三个模块都存在，`STANDALONE` 为假，直接跳过，
因此对宿主布局零影响。独立仓库里做两件事：

  1. 注入最小桩件，让模块级 import 通过
  2. 以 `mijia_plugin` 为包名加载根 `__init__.py`（`submodule_search_locations`
     指向仓库根，使 `from .mijia_api import ...` 等相对 import 解析到真实子包）

桩件只覆盖插件在**导入期**用到的名字。`Ok` / `Err` 的 `is_ok()` / `value` /
`error` 与真实 SDK 语义一致，因为 `_tool_envelope` 依赖它们。
"""
from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent


def _missing(name: str) -> bool:
    try:
        __import__(name)
    except Exception:
        return True
    return False


STANDALONE = _missing("plugin.sdk.plugin")


if STANDALONE:
    # ── config ──────────────────────────────────────────────────────────
    _cfg = types.ModuleType("config")
    _cfg.USER_PLUGIN_BASE = "http://127.0.0.1:48916"
    sys.modules["config"] = _cfg

    # ── plugin.sdk.plugin ───────────────────────────────────────────────
    class SdkError(Exception):
        """桩件；真实实现在宿主 SDK。"""

    class Ok:
        __slots__ = ("value",)

        def __init__(self, value=None):
            self.value = value

        def is_ok(self) -> bool:
            return True

    class Err:
        __slots__ = ("error",)

        def __init__(self, error=None):
            self.error = error

        def is_ok(self) -> bool:
            return False

    def _identity_decorator(*_args, **_kwargs):
        """装饰器桩件：只保留函数，注册发生在运行时，导入期不需要。"""

        def _wrap(fn):
            return fn

        return _wrap

    def neko_plugin(cls):
        return cls

    def tr(key, default=None, **_kwargs):
        return default if default is not None else key

    def get_plugin_logger(name):
        return logging.getLogger(name)

    class _Ui:
        context = staticmethod(_identity_decorator)
        action = staticmethod(_identity_decorator)

    class NekoPluginBase:
        def __init__(self, ctx=None):
            self.ctx = ctx

    _plugin_sdk_plugin = types.ModuleType("plugin.sdk.plugin")
    for _name, _obj in {
        "Err": Err,
        "NekoPluginBase": NekoPluginBase,
        "Ok": Ok,
        "SdkError": SdkError,
        "get_plugin_logger": get_plugin_logger,
        "lifecycle": _identity_decorator,
        "llm_tool": _identity_decorator,
        "neko_plugin": neko_plugin,
        "plugin_entry": _identity_decorator,
        "timer_interval": _identity_decorator,
        "tr": tr,
        "ui": _Ui,
    }.items():
        setattr(_plugin_sdk_plugin, _name, _obj)
    sys.modules["plugin"] = types.ModuleType("plugin")
    sys.modules["plugin.sdk"] = types.ModuleType("plugin.sdk")
    sys.modules["plugin.sdk.plugin"] = _plugin_sdk_plugin

    # ── utils.file_utils ────────────────────────────────────────────────
    async def read_json_async(path, *_args, **_kwargs):
        raise FileNotFoundError(str(path))

    async def atomic_write_json_async(path, data, **_kwargs):
        return None

    _utils_file = types.ModuleType("utils.file_utils")
    _utils_file.read_json_async = read_json_async
    _utils_file.atomic_write_json_async = atomic_write_json_async
    sys.modules["utils"] = types.ModuleType("utils")
    sys.modules["utils.file_utils"] = _utils_file


def _load_plugin_root():
    """以 `mijia_plugin` 为包名加载根 `__init__.py`。

    `submodule_search_locations=[ROOT]` 让模块成为以仓库根为根目录的包，于是
    `from .mijia_api import ...` / `from .nlp import ...` 解析到真实子包。
    """
    spec = importlib.util.spec_from_file_location(
        "mijia_plugin",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["mijia_plugin"] = module
    spec.loader.exec_module(module)
    return module


try:
    plugin_root = _load_plugin_root()
except Exception:  # pragma: no cover - 宿主布局或加载失败时相关测试自行 skip
    plugin_root = None
else:
    # pytest 会尝试 `import __init__`（仓库根在 pythonpath 上）。提前登记，
    # 让它命中已加载的模块，而不是重新执行一遍宿主 import。
    sys.modules.setdefault("__init__", plugin_root)


@pytest.fixture
def plugin_module():
    """插件根模块（MijiaPlugin 所在模块）。不可用时跳过。"""
    if plugin_root is None:
        pytest.skip("插件根模块不可用（宿主布局或加载失败）")
    return plugin_root
