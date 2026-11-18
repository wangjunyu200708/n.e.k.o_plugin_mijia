import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from config import USER_PLUGIN_BASE
from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    get_plugin_logger,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    timer_interval,
    tr,
    ui,
)
from utils.file_utils import atomic_write_json_async, read_json_async

# 导入内嵌的 mijia_api
from .mijia_api import create_async_api_client
from .mijia_api.api_client import AsyncMijiaAPI
from .mijia_api.domain.exceptions import MijiaAPIException, TokenExpiredError
from .mijia_api.domain.models import Credential
from .mijia_api.infrastructure.credential_provider import CredentialProvider
from .mijia_api.infrastructure.credential_store import FileCredentialStore
from .mijia_api.services.auth_service import AuthService

# 导入 NLP 规则引擎（纯函数，见 nlp/ 子包）
from .nlp import (
    CommandIR,
    match_devices,
    position_keywords,
    route,
    to_command_ir,
    validate_command,
)
from .nlp.action_verbs import VERB_TO_ACTION
from .nlp.intent_terms import detect_scene
from .nlp.normalizer import normalize_utterance
from .nlp.value_resolver import resolve_adjust_target
from .property_tables import (  # noqa: E402  静态表，见该模块 docstring
    NAME_MAP,
    PROP_KEYWORDS,
    UNIT_MAP,
)


# ── 同步 helper（已禁用自动跳转，仅作备用）──────────────────────────────
def _open_url_in_browser(url: str) -> None:
    """在系统默认浏览器中打开 URL（同步，通过 to_thread 调用）"""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception:
        raise

_EMBEDDED_BY_AGENT = os.getenv("NEKO_PLUGIN_HOSTED_BY_AGENT", "").strip().lower() == "true"

# 设备缓存 schema 版本：value_list 从纯值列表升级为带 description/comment 的
# dict 列表（v2）。读缓存时校验版本，不匹配则视为失效重新拉取，避免旧缓存缺
# 枚举描述导致中文模式命令失效。
_DEVICES_CACHE_SCHEMA = 2

# list_devices 并发拉取设备规格的并发上限。规格接口是公网 HTTP，8 路并发既能
# 压掉串行等待，又不会因为一次刷新把连接数打满。
_SPEC_FETCH_CONCURRENCY = 8

# ── 对话期 LLM 工具（@llm_tool）──────────────────────────────────────────
# 动作分发类能力走 @llm_tool（对话 LLM 直接调用），查询类留 @plugin_entry
# （analyzer / 插件管理器 / 面板 /runs 路径），与 game_agent_minecraft 的
# minecraft_task + 查询入口同一分工。
#
# 关键：同一能力**不能**两套都挂。宿主会把 analyzer 视野里的
# __llm_tool__* 动态入口剥掉（app/agent_server/api_runtime.py:865-875），
# 但 @plugin_entry 是 analyzer 看得见的——两者同时存在就会出现
# "analyzer 触发入口 + 对话 LLM 调工具" 的双执行。
#
# description / parameters 必须是纯字符串（不能用 tr()）：tr() 返回的是
# {"$i18n": key, "default": ...} 延迟引用 dict，只在入口元数据/UI 上下文里由
# 宿主解析；@llm_tool 的描述经 IPC 直送 main_server 的 /api/tools/register，
# 途中只有 str(description) 一次兜底转换（plugin/core/communication.py:1063），
# 传 dict 会把整份引用对象拍成字符串喂给模型。
SMART_CONTROL_DESCRIPTION = (
    "用一句自然语言控制米家（小米）智能家居设备，或执行米家 App 里预设的智能场景。\n\n"
    "何时调用——用户明确要求开关、调节某个设备，或执行某个场景时：\n"
    "  开关：打开卧室灯 / 关掉插座\n"
    "  亮度·色温·百分比：灯亮度调到 50% / 客厅灯亮度 30\n"
    "  温度·模式：空调调到 26 度 / 空调调制冷\n"
    "  场景：执行回家场景 / 离家模式\n"
    "不要在闲聊、只是问某个设备现在什么状态、问你能做什么、或想确认上一条指令"
    "到底生效没有时调用——这些都不是控制动作。\n\n"
    "command 参数：\n"
    "  ⚠️ 传用户的原话，不要改写、翻译，也不要自行补充设备名、房间名、数值或单位。\n"
    "  必须是完整的一句话，不要传裸标识符（on / power / 100 这种）。\n"
    "  ⚠️ 一次调用只传一条命令——本插件不解析多指令。用户一句话里含多个动作时，"
    "为每个动作各调用一次本工具，不要把多个动作塞进同一个 command。\n\n"
    "返回值：成功时 output 带 success 与 message（实际执行的设备与动作，"
    "message 可直接转述给用户）。\n"
    "失败时 is_error=true，output 形如 {code, detail, message}：\n"
    "  code 是稳定错误码（如 device_not_found / device_ambiguous / "
    "attribute_not_found / invalid_unit / invalid_value / gang_ambiguous），\n"
    "  detail 是可用于重试的结构化数据（候选设备 candidates、可选方位 "
    "gang_options、允许范围 min/max、可用属性 available、可选模式 "
    "allowed_modes 等），\n"
    "  message 是给用户的一句人话。\n"
    "⚠️ 收到失败时：能凭 detail 修正就重试一次（例如换用候选设备名、按 "
    "gang_options 指定方位、按 min/max 调整数值）；仍无法确定就如实说明并追问，"
    "不要编造执行结果。"
)

SMART_CONTROL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": (
                "用户的自然语言控制指令原话，如'打开卧室灯'、'灯亮度调到50%'、"
                "'空调26度'、'执行回家场景'"
            ),
        },
    },
    "required": ["command"],
}


def _tool_error(message: str, *, code: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造 @llm_tool 的错误信封。

    `error` / `output.code` 放稳定错误码（大写+下划线，供程序/日志/模型重试判断），
    `output.detail` 放模型自纠所需的结构化数据（候选设备、允许范围、可选方位…），
    `output.message` 是给用户的一句人话（中文路径保留中文，不强制本地化）。
    llm_tools.py 只在返回值带 is_error / output 时才走信封分支，缺了就会把整份
    dict 当作 output 塞给模型。
    """
    return {
        "output": {
            "code": code,
            "detail": detail or {},
            "message": message,
        },
        "is_error": True,
        "error": code,
    }


def _err(code: str, message: str, **detail: Any) -> Any:
    """构造带稳定错误码 + 结构化 detail 的内部 Err。

    `detail` 走 SdkError.details，`_tool_envelope` 会原样透到工具输出。
    内部站点没标 code 时回落到 MIJIA_CONTROL_FAILED（仍是 is_error=True）。
    """
    return Err(SdkError(message, code=code, details=detail or None))


def _tool_envelope(result: Any) -> dict[str, Any]:
    """把内部 Result（Ok/Err）翻成 @llm_tool 的返回信封。

    工具 handler 的返回值经 IPC 回到 user_plugin_server，再被
    plugin/server/routes/llm_tools.py 包成 {"output": ..., "is_error": ...}
    喂给对话 LLM。Err 直接返回会走 IPC 异常路径，把 PluginExecutionError
    的报错串渗进模型上下文，所以这里显式降级成 is_error + 稳定错误码。

    ⚠️ 内部一切语义失败都必须走 Err——Ok 一律翻成 is_error=false，若某条分支用
    Ok({"success": False}) 表示失败，模型会被告知"成功"。
    """
    if callable(getattr(result, "is_ok", None)):
        if result.is_ok():
            return {"output": result.value, "is_error": False}
        err = result.error
        code = getattr(err, "code", None) or "control_failed"
        details = getattr(err, "details", None)
        return _tool_error(
            str(err),
            code=code,
            detail=details if isinstance(details, dict) else None,
        )
    return {"output": result, "is_error": False}

@neko_plugin
class MijiaPlugin(NekoPluginBase):
    """米家智能家居插件"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = get_plugin_logger(__name__)
        self.api: Optional[AsyncMijiaAPI] = None
        self.auth_service: Optional[AuthService] = None
        # CredentialProvider 持有自己的 httpx.Client，此前只在 __del__ 里兜底关闭，
        # 而 shutdown / logout 都不持有该对象 → 连接一直挂到 GC。这里存一份引用，
        # 由 _close_credential_provider() 显式释放。
        self._credential_provider: Optional[CredentialProvider] = None
        self.credential_path: Optional[Path] = None
        # 运行时消息语言。`self.i18n.t(key)` 不传 locale 时会退回 plugin.toml 的
        # default_locale(=zh-CN)，所以必须显式解析宿主语言并透传；默认值与
        # default_locale 一致，解析失败时不会意外切成英文。
        self._lang: str = "zh-CN"
        self._lock = asyncio.Lock()
        self._background_tasks: set = set()  # 持有后台 Task 引用，防止被 GC 提前回收

    # ========== Hosted UI ==========
    @ui.context(id="dashboard", title="米家智能家居控制面板")
    async def get_dashboard_context(self):
        """为 Hosted UI 面板提供状态数据"""
        logged_in = self.api is not None
        homes = []
        devices = []
        scenes = []

        if logged_in:
            try:
                # 获取家庭列表
                raw_homes = await self.api.get_homes()
                homes = [{"id": h.id, "name": h.name} for h in raw_homes if h.id]

                # 获取设备列表（从缓存，需归属校验防止跨用户泄露）
                cache_path = self.data_path("devices_cache.json")
                try:
                    raw = await read_json_async(cache_path)
                    cached = raw if isinstance(raw, dict) else None
                except Exception:
                    cached = None
                if cached is not None:
                    cache_user_id = cached.get('user_id')
                    current_user_id = self.api.credential.user_id if self.api.credential else None
                    if not current_user_id or cache_user_id == current_user_id:
                        devices = cached.get("devices", [])
                    else:
                        self.logger.debug("设备缓存归属不匹配，跳过")

                # 获取场景列表（从缓存，需归属校验防止跨用户泄露）
                scenes_cache_path = self.data_path("scenes_cache.json")
                try:
                    raw = await read_json_async(scenes_cache_path)
                    cached = raw if isinstance(raw, dict) else None
                except Exception:
                    cached = None
                if cached is not None:
                    cache_user_id = cached.get('user_id')
                    current_user_id = self.api.credential.user_id if self.api.credential else None
                    if not current_user_id or cache_user_id == current_user_id:
                        scenes = cached.get("scenes", [])
                    else:
                        self.logger.debug("场景缓存归属不匹配，跳过")
            except Exception as e:
                self.logger.warning(f"获取UI状态失败: {e}")

        return {
            "logged_in": logged_in,
            "homes": homes,
            "devices": devices,
            "scenes": scenes,
            "device_count": len(devices),
            "scene_count": len(scenes),
            "online_count": sum(1 for d in devices if d.get("is_online")),
        }

    @staticmethod
    def _resolve_lang() -> str:
        """读取宿主全局语言；独立仓库 / 测试环境下回落到 zh-CN。

        与 game_agent_minecraft 同源（utils.language_utils）。该函数在宿主里是
        同步读配置的轻量调用，且只在 startup 解析一次。
        """
        try:
            from utils.language_utils import get_global_language_full
            lang = str(get_global_language_full() or "").strip()
            return lang or "zh-CN"
        except Exception:
            return "zh-CN"

    # ========== 生命周期 ==========
    @lifecycle(id="startup")
    async def on_startup(self, **_):
        """插件启动：加载凭据并初始化API客户端"""
        self.logger.info("米家插件启动中...")

        # 运行时消息语言：i18n.t 不传 locale 会用 default_locale(zh-CN)，
        # 所以必须显式解析并透传（见 3 处 self.i18n.t(locale=self._lang)）。
        self._lang = self._resolve_lang()

        # 读取配置
        self.credential_path = self.data_path("credential.json")
        self.logger.debug(f"凭据路径: {self.credential_path}")

        # 后台静默加载凭据，不阻塞启动
        task = asyncio.create_task(self._background_load_credential())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # 注册静态UI
        # register_static_ui 接受相对目录名，内部会拼接 self.config_dir / directory
        # static/ 目录下的入口文件为 index.html
        if (self.config_dir / "static").exists():
            ok = self.register_static_ui(
                "static",
                index_file="index.html",
                cache_control="no-cache, no-store, must-revalidate"
            )
            if ok:
                self.logger.info("已注册米家配置页面，访问路径: /plugin/mijia/ui/")
            else:
                self.logger.warning("注册静态UI失败，请检查 static/index.html 是否存在")

        return Ok({"status": "ready"})

    async def _background_load_credential(self):
        """后台静默加载凭据，不阻塞插件启动"""
        try:
            store = FileCredentialStore(default_path=self.credential_path)
            from .mijia_api.core.config import ConfigManager
            config = ConfigManager()
            provider = CredentialProvider(config)
            self._credential_provider = provider
            self.auth_service = AuthService(provider, store)

            credential = await self._load_credential()
            if credential:
                try:
                    await self._init_api(credential)
                    self.logger.info("米家插件启动成功，已加载已有凭据")
                except Exception as e:
                    self.logger.error(f"API初始化失败，插件将在未登录状态下运行: {e}")
            else:
                self.logger.warning("未找到有效凭据，请在Web UI中登录")
        except Exception as e:
            self.logger.error(f"后台加载凭据失败: {e}")



    def _ensure_auth_service(self):
        """懒加载初始化认证服务（供手动入口调用，避免启动时阻塞）"""
        if self.auth_service:
            return
        from .mijia_api.core.config import ConfigManager
        config = ConfigManager()
        store = FileCredentialStore(default_path=self.credential_path)
        provider = CredentialProvider(config)
        self._credential_provider = provider
        self.auth_service = AuthService(provider, store)

    def _close_credential_provider(self) -> None:
        """释放凭据提供者的 HTTP 连接并置空引用（幂等）。"""
        provider = self._credential_provider
        self._credential_provider = None
        if provider is not None:
            try:
                provider.close()
            except Exception as e:
                self.logger.warning(f"关闭凭据 HTTP 客户端时出错: {e}")

    async def _build_room_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        """构建 room_id→room_name 和 device_did→room_name 两种映射

        device_did→room_name 映射从 gethome_merged 的 roomlist 中提取，
        用于设备列表 API 不返回 room_id 时的降级方案。
        """
        if not self.api:
            self.logger.info("构建房间映射跳过：API 未就绪")
            return {}, {}
        try:
            homes = await self.api.get_homes()
            room_map: dict[str, str] = {}
            device_room_map: dict[str, str] = {}
            device_field_candidates = ("dids", "devices", "device_list", "child_devices")
            for home in homes:
                if not home.rooms:
                    self.logger.info(f"家庭 {home.name}({home.id}) 无房间数据")
                    continue
                for room in home.rooms:
                    rid = str(room.get("id", ""))
                    rname = room.get("name", "")
                    if not rid or not rname:
                        continue
                    room_map[rid] = rname

                    # 每个房间独立检测设备列表字段，不同 room API 返回可能使用不同 key
                    room_field = next(
                        (f for f in device_field_candidates if f in room),
                        None,
                    )
                    dids = room.get(room_field) if room_field else None
                    if room_field is None:
                        self.logger.debug(f"房间 '{rname}'({rid}) 的 key 不在候选列表中: {list(room.keys())}")
                    if dids and isinstance(dids, list):
                        for did in dids:
                            did_str = str(did)
                            if did_str and did_str not in device_room_map:
                                device_room_map[did_str] = rname

            total_rooms = len(room_map)
            room_names = list(room_map.values())
            from_device = len(device_room_map)
            if from_device:
                self.logger.info(
                    f"房间映射构建完成: {total_rooms} 个房间 {room_names}, "
                    f"设备→房间映射 {from_device} 条"
                )
            else:
                self.logger.info(f"房间映射构建完成: {total_rooms} 个房间 {room_names}, 无设备→房间映射")
            return room_map, device_room_map
        except Exception as e:
            self.logger.warning(f"构建房间映射失败: {e}")
            return {}, {}

    # ========== 设备匹配与命令解析 ==========

    async def _load_devices_cache(self) -> tuple[list[dict], Optional[str]]:
        """加载设备缓存，不存在或版本不匹配时自动拉取。

        返回 ``(devices, failure)``。``failure`` 非空表示**拉取失败**（未登录 /
        网络错 / 凭据过期），与「账号下确实没有设备」是两回事——调用方据此报真实
        原因，而不是把刷新失败塌缩成空列表、再让上层报成「设备没匹配上」。
        """
        cache_path = self.data_path("devices_cache.json")
        if self.api:
            try:
                raw = await read_json_async(cache_path)
                cached = raw if isinstance(raw, dict) else None
            except Exception:
                cached = None
            if cached is not None:
                cache_user_id = cached.get('user_id')
                current_user_id = self.api.credential.user_id if self.api.credential else None
                if cached.get("schema_version") != _DEVICES_CACHE_SCHEMA:
                    self.logger.info("设备缓存 schema 版本不匹配，自动刷新")
                elif not current_user_id or cache_user_id == current_user_id:
                    devices = cached.get('devices', [])
                    if devices:
                        self.logger.info(f"设备缓存有效: {len(devices)} 个设备")
                        return devices, None
                    self.logger.info("设备缓存为空，自动刷新")
        self.logger.info("从 API 刷新设备缓存")
        result = await self.list_devices(refresh=True)
        if result.is_ok():
            fresh_devices = result.value.get('devices', [])
            self.logger.info(f"API 刷新完成: {len(fresh_devices)} 个设备")
            return fresh_devices, None
        failure = str(getattr(result, "error", "") or "设备列表刷新失败")
        self.logger.info(f"API 刷新设备列表失败: {failure}")
        return [], failure

    @plugin_entry(
        id="open_ui",
        name=tr("entries.open_ui.name", default="打开配置页面"),
        description=tr("entries.open_ui.description", default="在浏览器中打开米家插件的 Web UI 配置页面"),
        kind="action"
    )
    async def open_ui(self, **_):
        """在浏览器中打开米家配置页面"""
        url = f"{USER_PLUGIN_BASE}/plugin/mijia/ui/"
        try:
            await asyncio.to_thread(_open_url_in_browser, url)
            self.logger.info(f"已在浏览器中打开: {url}")
            return Ok({"success": True, "url": url, "message": "已在浏览器打开配置页面"})
        except Exception as e:
            self.logger.exception("打开配置页面失败")
            return Err(SdkError(f"打开配置页面失败: {e}"))

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        """插件关闭：清理资源"""
        self.logger.info("米家插件关闭")

        # 取消所有后台任务
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
                self._background_tasks.clear()

        if self.api:
            try:
                await self.api.close()
            except Exception as e:
                self.logger.warning(f"关闭API客户端时出错: {e}")
            finally:
                self.api = None
        self._close_credential_provider()
        return Ok({"status": "stopped"})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        """配置变化（如用户在UI修改了凭据路径）时重新加载"""
        self.logger.info("配置变化，重新加载凭据")
        await self._reload_credential()
        return Ok({"reloaded": True})

    @plugin_entry(
        id="reload_credential",
        name=tr("entries.reload_credential.name", default="重新加载凭据"),
        description=tr("entries.reload_credential.description", default="重新从文件加载米家凭据并初始化API，防止插件重载后凭据未及时加载导致显示未登录"),
        kind="action"
    )
    async def reload_credential(self, **_):
        """重新加载凭据（供前端刷新状态前调用）"""
        try:
            await self._reload_credential()
        except Exception as e:
            self.logger.warning(f"reload_credential 失败: {e}")
        return Ok({
            "success": True,
            "logged_in": self.api is not None,
        })

    # ========== 凭据管理 ==========
    async def _load_credential(self) -> Optional[Credential]:
        """从文件加载凭据"""
        if not self.credential_path or not self.credential_path.exists():
            return None
        try:
            text = await asyncio.to_thread(self.credential_path.read_text)
            text = text.strip()
            if not text:
                # 文件存在但内容为空，视同未登录
                return None
            data = json.loads(text)
            credential = Credential.model_validate(data)
            if credential.is_expired():
                self.logger.warning("凭据已过期，需要刷新")
                # 尝试刷新
                return await self._refresh_credential(credential)
            return credential
        except Exception as e:
            self.logger.error(f"加载凭据失败: {e}")
            return None

    async def _save_credential(self, credential: Credential):
        """保存凭据到文件,权限600"""
        if not self.credential_path:
            self.credential_path = self.data_path("credential.json")

        # 确保目录存在（使用 to_thread 避免阻塞）
        await asyncio.to_thread(self.credential_path.parent.mkdir, parents=True, exist_ok=True)

        # 写入凭据内容
        await asyncio.to_thread(
            self.credential_path.write_text, credential.model_dump_json()
        )

        # 设置文件权限（仅所有者可读写）
        if sys.platform == "win32":
            try:
                def _apply_windows_acl() -> tuple[int, str]:
                    username = subprocess.check_output(
                        ["cmd", "/c", "echo", "%USERNAME%"], text=True
                    ).strip()
                    path_str = str(self.credential_path)
                    # 先移除所有继承权限，再授权当前用户完全控制
                    result = subprocess.run(
                        ["icacls", path_str, "/inheritance:r", "/grant:r", f"{username}:F"],
                        check=False, capture_output=True, text=True
                    )
                    return result.returncode, (result.stderr or "").strip()

                returncode, stderr = await asyncio.to_thread(_apply_windows_acl)
                if returncode != 0:
                    self.logger.warning(
                        f"设置凭据文件权限失败(Windows): icacls 返回码 {returncode}"
                        + (f", stderr: {stderr}" if stderr else "")
                    )
                else:
                    self.logger.debug("凭据文件权限已设置（仅当前用户）")
            except Exception as e:
                self.logger.warning(f"设置凭据文件权限失败(Windows): {e}")
        else:
            await asyncio.to_thread(self.credential_path.chmod, 0o600)
        self.logger.info("凭据已保存")

    async def _refresh_credential(self, credential: Credential) -> Optional[Credential]:
        if not self.auth_service:
            return None
        try:
            new_cred = await self.auth_service.async_refresh_credential(credential)
            if new_cred:
                await self._save_credential(new_cred)
                self.logger.info("凭据刷新成功并已保存")
            return new_cred
        except Exception as e:
            self.logger.error(f"刷新凭据失败: {e}")
            return None

    def _parse_xiaomi_response(self, text: str) -> dict:
        """解析小米登录返回的 &&&START&&&{...} 格式"""
        marker = "&&&START&&&"
        idx = text.find(marker)
        if idx == -1:
            # 尝试直接解析 JSON
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}
        json_str = text[idx + len(marker):]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return {}

    @ui.action(label=tr("actions.login.label", default="扫码登录"), tone="primary", group="auth", order=10, refresh_context=True)
    @plugin_entry(
        id="start_qrcode_login",
        name=tr("entries.start_qrcode_login.name", default="开始二维码登录"),
        description=tr("entries.start_qrcode_login.description", default="获取二维码图片并开始登录流程"),
        kind="action"
    )
    async def start_qrcode_login(self, **_):
        self._ensure_auth_service()
        if not self.auth_service:
            return Err(SdkError("认证服务未初始化"))
        try:
            raw_qr_data, login_url = await self.auth_service.async_get_qrcode()
            # 解析小米原始响应格式 &&&START&&&{...}
            qr_data = self._parse_xiaomi_response(raw_qr_data)
            qr_url = qr_data.get("qr", raw_qr_data)  # 如果解析失败，返回原始数据
            # login_url 也可能是原始格式，尝试解析
            if login_url.startswith("&&&START&&&"):
                login_data = self._parse_xiaomi_response(login_url)
                login_url = login_data.get("loginUrl", login_url)
            return Ok({"qr_url": qr_url, "login_url": login_url})
        except Exception as e:
            return Err(SdkError(f"生成二维码失败: {e}"))

    @plugin_entry(
        id="check_login_status",
        name=tr("entries.check_login_status.name", default="检查登录状态"),
        description=tr("entries.check_login_status.description", default="轮询检查二维码登录是否成功"),
        kind="action"
    )
    async def check_login_status(self, login_url: str, **_):
        self._ensure_auth_service()
        if not self.auth_service:
            return Err(SdkError("认证服务未初始化"))
        try:
            credential = await self.auth_service.async_poll_login(login_url, timeout=120)
            if credential:
                await self._save_credential(credential)
                await self._init_api(credential)
                return Ok({"success": True, "user_id": credential.user_id})
            else:
                return Ok({"success": False, "message": "登录超时或未扫码"})
        except Exception as e:
            return Err(SdkError(f"检查登录状态失败: {e}"))

    async def _init_api(self, credential: Credential):
        """使用凭据初始化API客户端"""
        # 先构建新实例，探活成功后再替换，避免旧连接在验证期间被提前丢弃
        new_api = create_async_api_client(credential)
        try:
            await new_api.get_homes()
        except Exception as e:
            self.logger.error(f"API初始化失败: {e}")
            try:
                await new_api.close()
            except Exception:
                pass
            raise
        
        # 验证通过，关闭旧客户端后原子替换
        old_api = self.api
        self.api = new_api
        if old_api is not None:
            try:
                await old_api.close()
            except Exception as close_err:
                self.logger.warning(f"关闭旧API客户端时出错: {close_err}")
        
        self.logger.info("API客户端初始化成功")

    async def _reload_credential(self):
        """重新加载凭据（如配置变化）"""
        async with self._lock:
            credential = await self._load_credential()
            if credential:
                await self._init_api(credential)
            else:
                # 关闭旧 client 再置 None，防止 httpx 连接池 / CacheManager / Redis 泄漏
                old_api = self.api
                self.api = None
                if old_api is not None:
                    try:
                        await old_api.close()
                    except Exception as close_err:
                        self.logger.warning(f"关闭旧API客户端时出错: {close_err}")

    # ========== 定时刷新凭据 ==========
    @timer_interval(id="refresh_credential", seconds=86400, auto_start=True)  # 每天一次
    async def _auto_refresh_credential(self, **_):
        """自动刷新凭据，避免过期"""
        if not self.api:
            return Ok({"skipped": "no_api"})
        new_cred = None
        credential = self.api.credential
        if credential:
            # 同时处理"7天内即将过期"和"已经过期但尚未处理"两种情况
            if not credential.is_expired() and credential.expires_in() >= 7 * 86400:
                return Ok({"skipped": "not_near_expiry"})
            # 已过期或在7天内，尝试刷新
            if credential.is_expired():
                self.logger.warning("凭据已过期，尝试刷新")
            else:
                self.logger.info("凭据即将过期，尝试刷新")
            new_cred = await self._refresh_credential(credential)
            if new_cred:
                await self._init_api(new_cred)
                self.logger.info("凭据刷新成功")
            else:
                self.logger.warning("凭据刷新失败，请手动登录")
        return Ok({"refreshed": new_cred is not None})

    # ========== Web UI 端点（供前端调用） ==========
    
    @ui.action(label=tr("actions.logout.label", default="登出"), tone="danger", group="auth", order=20, refresh_context=True)
    @plugin_entry(
        id="logout",
        name=tr("entries.logout.name", default="登出"),
        description=tr("entries.logout.description", default="清除保存的凭据并清空本地数据"),
        kind="action"
    )
    async def logout(self, **_):
        """清除本地凭据和数据"""
        # 删除凭据文件
        if self.credential_path and self.credential_path.exists():
            await asyncio.to_thread(self.credential_path.unlink)

        # 清空 data 文件夹（使用线程避免阻塞）
        data_dir = self.data_path()
        if data_dir and data_dir.exists():

            def _delete_all():
                deleted = 0
                for item in data_dir.iterdir():
                    try:
                        if item.is_file():
                            item.unlink()
                            deleted += 1
                        elif item.is_dir():
                            shutil.rmtree(item)
                            deleted += 1
                    except Exception as e:
                        self.logger.warning(f"删除数据文件失败 {item}: {e}")
                return deleted

            deleted = await asyncio.to_thread(_delete_all)
            self.logger.debug(f"已删除 {deleted} 个数据文件")
        
        # 关闭旧 client 再置 None，防止 httpx 连接池 / CacheManager / Redis 泄漏
        old_api = self.api
        self.api = None
        self.auth_service = None
        self._close_credential_provider()
        if old_api is not None:
            try:
                await old_api.close()
            except Exception as close_err:
                self.logger.warning(f"关闭旧API客户端时出错: {close_err}")
        self.logger.info("已登出，凭据和数据已删除")
        return Ok({"success": True, "message": "✅ 已登出，所有本地数据已清除"})

    # ========== 核心功能入口 ==========
    @plugin_entry(
        id="list_homes",
        name=tr("entries.list_homes.name", default="获取家庭列表"),
        description=tr("entries.list_homes.description", default="列出当前账号下所有米家家庭及其 ID"),
        llm_result_fields=["message"]
    )
    async def list_homes(self, **_):
        """获取家庭列表"""
        if not self.api:
            return Err(SdkError("未登录或凭据无效，请先登录"))
        try:
            homes = await self.api.get_homes()
            # 转换为简单字典供AI使用，过滤掉没有id的家庭
            result = [{"id": h.id, "name": h.name} for h in homes if h.id]
            if not result:
                self.logger.warning(f"获取到 {len(homes)} 个家庭，但都没有有效ID")
            
            # 构建友好消息
            lines = [f"🏠 共有 {len(result)} 个家庭:"]
            for h in result:
                lines.append(f"  • {h.get('name')} (ID: {h.get('id')})")
            message = "\n".join(lines)
            
            return Ok({"success": True, "message": message, "homes": result, "count": len(result)})
        except TokenExpiredError:
            return _err("token_expired", "凭据已过期，请重新登录")
        except Exception as e:
            self.logger.exception("获取家庭列表失败")
            return Err(SdkError(f"获取家庭列表失败: {e}"))

    @ui.action(label=tr("actions.refreshDevices.label", default="刷新设备"), tone="secondary", group="device", order=10, refresh_context=True)
    @plugin_entry(
        id="list_devices",
        name=tr("entries.list_devices.name", default="获取设备列表"),
        description=tr("entries.list_devices.description", default="获取设备列表，home_id留空自动使用第一个家庭，支持缓存"),
        input_schema={
            "type": "object",
            "properties": {
                "home_id": {"type": "string", "description": "家庭ID，留空自动用第一个"},
                "refresh": {"type": "boolean", "description": "是否强制刷新缓存"}
            },
            "required": []
        },
        llm_result_fields=["message"]
    )
    async def list_devices(self, home_id: str = None, refresh: bool = False, **_):
        """获取设备列表并缓存"""
        cache_path = self.data_path("devices_cache.json")

        # 异步读旧缓存（缺失抛 FileNotFoundError，不经同步 exists() 检查）：
        # 用于命中缓存、保留 home_id 与用户自定义别名
        cached = None
        if self.api:
            try:
                raw = await read_json_async(cache_path)
                cached = raw if isinstance(raw, dict) else None
            except Exception:
                cached = None

        current_user_id = self.api.credential.user_id if self.api and self.api.credential else None
        cache_home_id = cached.get('home_id') if cached is not None else None
        cache_user_id = cached.get('user_id') if cached is not None else None

        # 旧缓存里的设备别名与完整设备数据（按 DID）：仅在缓存归属严格匹配当前
        # 用户时提取，否则跨账号共享 DID 会把上一账号的自定义别名/元数据泄露并
        # 持久化到本账号
        old_aliases: dict[str, str] = {}
        old_devices: dict[str, dict] = {}
        if cached is not None and (not current_user_id or cache_user_id == current_user_id):
            for d in cached.get("devices", []) or []:
                did = d.get("did")
                if not did:
                    continue
                old_devices[did] = d
                if d.get("alias"):
                    old_aliases[did] = d["alias"]

        # schema 不匹配：归属校验通过则保留旧 home_id，刷新仍用同一家庭，避免切错
        if cached is not None and cached.get("schema_version") != _DEVICES_CACHE_SCHEMA:
            self.logger.info("设备缓存 schema 版本不匹配，刷新并保留 home_id")
            if not home_id and (not current_user_id or cache_user_id == current_user_id):
                home_id = cache_home_id

        # 不强制刷新且缓存归属匹配才返回缓存；否则走网络请求
        if not refresh and cached is not None and cached.get("schema_version") == _DEVICES_CACHE_SCHEMA:
            if cache_home_id == home_id and (not current_user_id or cache_user_id == current_user_id):
                devices = cached.get('devices', [])
                self.logger.info(f"从缓存读取设备列表: {len(devices)} 个设备")
                lines = [f"📱 共有 {len(devices)} 个设备（缓存）:"]
                for d in devices:
                    status = "🟢" if d.get("is_online") else "🔴"
                    lines.append(f"  {status} {d.get('name')} (型号: {d.get('model')})")
                message = "\n".join(lines)
                return Ok({"success": True, "message": message, "devices": devices, "from_cache": True, "count": len(devices)})
            self.logger.warning("缓存归属不匹配，跳过缓存")

        if not self.api:
            return _err("not_logged_in", "未登录")
        
        # 如果 home_id 为空，尝试获取第一个家庭
        if not home_id:
            try:
                homes = await self.api.get_homes()
                valid_homes = [h for h in homes if h.id]
                if not valid_homes:
                    return Err(SdkError("没有可用的家庭，请先创建家庭或检查登录状态"))
                home_id = valid_homes[0].id
            except Exception as e:
                return Err(SdkError(f"无法获取默认家庭: {e}"))
        
        try:
            devices = await self.api.get_devices(home_id)
            # 构建房间映射，注入 room_name 到每个设备
            room_map, device_room_map = await self._build_room_maps()
            result = []
            room_filled = 0
            room_empty = 0
            spec_failed = False

            # 规格预取：按 model 去重后并发拉取。原先在设备循环里串行 await，
            # N 台设备就是 N 次排队 RTT，冷启动（首次调用触发全量刷新）时最明显。
            # 仓储层本身按 model 有长缓存，这里省的是「首次 / 缓存失效」时的串行等待，
            # 以及同型号设备的重复等待。
            spec_map: dict[str, Any] = {}
            spec_error_models: set[str] = set()
            _unique_models = list(dict.fromkeys(d.model for d in devices if d.model))
            if _unique_models:
                _sem = asyncio.Semaphore(_SPEC_FETCH_CONCURRENCY)

                async def _fetch_spec(model: str):
                    async with _sem:
                        return await self.api.get_device_spec(model)

                _fetched = await asyncio.gather(
                    *(_fetch_spec(m) for m in _unique_models),
                    return_exceptions=True,
                )
                for _model, _outcome in zip(_unique_models, _fetched):
                    if isinstance(_outcome, BaseException):
                        if isinstance(_outcome, TokenExpiredError):
                            raise _outcome  # 外层统一返回「凭据已过期」
                        spec_error_models.add(_model)
                        self.logger.debug(
                            f"获取设备规格失败: model={_model}, {_outcome}"
                        )
                    else:
                        spec_map[_model] = _outcome

            for d in devices:
                room_name = ""
                if d.room_id:
                    room_name = room_map.get(str(d.room_id), "")
                if not room_name:
                    # 降级：通过设备 DID 在房间数据结构中查找
                    room_name = device_room_map.get(d.did, "")
                if room_name:
                    room_filled += 1
                else:
                    room_empty += 1
                device_info = {
                    "did": d.did,
                    "name": d.name,
                    "model": d.model,
                    "is_online": d.is_online(),
                    "room_id": d.room_id,
                    "room_name": room_name,
                }
                # 回填用户自定义别名（按 DID），避免刷新重建后别名丢失
                if d.did in old_aliases:
                    device_info["alias"] = old_aliases[d.did]
                
                # 规格来自上面的并发预取（按 model 去重）。缺失或拉取失败都标记
                # spec_failed 并回填旧缓存属性，避免不完整缓存以 v2 固化（否则旧
                # 格式的扁平 value_list 会被永久写死，中文模式命令随之失效）。
                if d.model:
                    spec = spec_map.get(d.model)
                    if not (spec and (spec.properties or spec.actions)):
                        spec_failed = True
                        reason = "获取失败" if d.model in spec_error_models else "规格缺失"
                        self.logger.debug(
                            f"设备 {d.name}({d.model}) 规格{reason}，标记刷新不完整"
                        )
                        old = old_devices.get(d.did)
                        if old:
                            if old.get("properties"):
                                device_info["properties"] = old["properties"]
                            if old.get("actions"):
                                device_info["actions"] = old["actions"]
                    else:
                        # 缓存属性信息（包含 siid, piid）
                        properties = []
                        for p in spec.properties:
                            prop = {
                                "siid": p.siid,
                                "piid": p.piid,
                                "name": p.name,
                                "type": p.type.value if hasattr(p.type, "value") else str(p.type),
                                "access": p.access.value if hasattr(p.access, "value") else str(p.access),
                            }
                            if p.value_range:
                                prop["value_range"] = p.value_range
                            if p.value_list:
                                prop["value_list"] = p.value_list
                            if p.service_description:
                                prop["service_desc"] = p.service_description
                            properties.append(prop)

                        # 缓存操作信息（含 siid/aiid 与参数元数据，动作调用时据此构造 in）
                        actions = []
                        for a in spec.actions:
                            action = {
                                "siid": a.siid,
                                "aiid": a.aiid,
                                "name": a.name,
                            }
                            if a.parameters:
                                action["parameters"] = [
                                    {
                                        "name": p.name,
                                        "type": p.type.value if hasattr(p.type, "value") else str(p.type),
                                        "required": p.required,
                                    }
                                    for p in a.parameters
                                ]
                            actions.append(action)

                        device_info["properties"] = properties
                        device_info["actions"] = actions
                result.append(device_info)

            self.logger.info(f"设备房间注入: {room_filled} 个有房间, {room_empty} 个无房间")

            # 保存到缓存（使用异步写入避免阻塞）
            try:
                user_id = self.api.credential.user_id if self.api and self.api.credential else None
                # 刷新不完整（有设备规格获取失败）时保留旧版本号而非升级到 v2，
                # 使下次读取仍判定为"版本不匹配"并重试；否则会把旧格式（扁平
                # value_list）的兜底属性以 v2 固化，导致中文模式命令永久失效
                schema_to_write = _DEVICES_CACHE_SCHEMA
                if spec_failed:
                    schema_to_write = cached.get("schema_version") if cached is not None else None
                await atomic_write_json_async(
                    cache_path,
                    {"schema_version": schema_to_write, "devices": result, "home_id": home_id, "user_id": user_id},
                    ensure_ascii=False,
                    indent=2
                )
                self.logger.info(f"设备列表已缓存: {len(result)} 个设备")
            except Exception as e:
                self.logger.warning(f"保存缓存失败: {e}")
            
            # 构建友好消息
            lines = [f"📱 共有 {len(result)} 个设备:"]
            for d in result:
                status = "🟢" if d.get("is_online") else "🔴"
                lines.append(f"  {status} {d.get('name')} (型号: {d.get('model')})")
            message = "\n".join(lines)
            
            return Ok({"success": True, "message": message, "devices": result, "from_cache": False, "count": len(result)})
        except TokenExpiredError:
            return _err("token_expired", "凭据已过期，请重新登录")
        except Exception as e:
            self.logger.exception("获取设备列表失败")
            return Err(SdkError(f"获取设备列表失败: {e}"))

    @plugin_entry(
        id="get_cached_devices",
        name=tr("entries.get_cached_devices.name", default="获取缓存的设备列表"),
        description=tr("entries.get_cached_devices.description", default="读取本地缓存的设备列表，缓存不存在时自动拉取"),
        input_schema={
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "是否强制刷新缓存"}
            },
            "required": []
        },
        llm_result_fields=["message"]
    )
    async def get_cached_devices(self, refresh: bool = False, **_):
        """获取缓存的设备列表"""
        cache_path = self.data_path("devices_cache.json")

        # 必须已登录才能读缓存，防止跨用户缓存泄露
        if not refresh and self.api:
            try:
                raw = await read_json_async(cache_path)
                cached = raw if isinstance(raw, dict) else None
            except Exception:
                cached = None
            if cached is not None:
                # 跨用户校验，防止缓存泄漏
                cache_user_id = cached.get('user_id')
                current_user_id = self.api.credential.user_id if self.api.credential else None
                # 归属匹配才返回缓存；不匹配时跳过，继续走网络请求
                if cached.get("schema_version") != _DEVICES_CACHE_SCHEMA:
                    self.logger.info("设备缓存 schema 版本不匹配，跳过缓存")
                elif not current_user_id or cache_user_id == current_user_id:
                    devices = cached.get('devices', [])
                    self.logger.info(f"AI 从缓存读取设备列表: {len(devices)} 个设备")
                    lines = [f"📱 共有 {len(devices)} 个设备:"]
                    for d in devices:
                        status = "🟢" if d.get("is_online") else "🔴"
                        lines.append(f"  {status} {d.get('name')} (型号: {d.get('model')})")
                    message = "\n".join(lines)
                    return Ok({"success": True, "message": message, "devices": devices, "from_cache": True, "count": len(devices)})
                self.logger.warning("缓存归属不匹配，跳过缓存")

        # 缓存不存在或刷新，调用 list_devices
        return await self.list_devices(refresh=refresh)

    @plugin_entry(
        id="list_scenes",
        name=tr("entries.list_scenes.name", default="获取智能场景列表"),
        description=tr("entries.list_scenes.description", default="列出当前账号下所有米家智能场景，支持缓存"),
        input_schema={
            "type": "object",
            "properties": {
                "home_id": {"type": "string", "description": "家庭ID，留空自动使用第一个"},
                "refresh": {"type": "boolean", "description": "是否强制刷新缓存"}
            },
            "required": []
        },
        llm_result_fields=["message"]
    )
    async def list_scenes(self, home_id: str = None, refresh: bool = False, **_):
        """获取智能场景列表并缓存"""
        cache_path = self.data_path("scenes_cache.json")

        # 如果不强制刷新，尝试从缓存读取（必须已登录，防止跨用户缓存泄露）
        if not refresh and self.api:
            try:
                raw = await read_json_async(cache_path)
                cached = raw if isinstance(raw, dict) else None
            except Exception:
                cached = None
            if cached is not None:
                cache_home_id = cached.get('home_id')
                cache_user_id = cached.get('user_id')
                current_user_id = self.api.credential.user_id if self.api.credential else None
                # 归属不匹配：跳过缓存，继续走网络请求
                if cache_home_id == home_id and (not current_user_id or cache_user_id == current_user_id):
                    scenes = cached.get('scenes', [])
                    self.logger.info(f"AI 从缓存读取场景列表: {len(scenes)} 个场景")
                    lines = [f"🎬 共有 {len(scenes)} 个智能场景:"]
                    for s in scenes:
                        lines.append(f"  • {s.get('name')} (ID: {s.get('id')})")
                    message = "\n".join(lines)
                    return Ok({"success": True, "message": message, "scenes": scenes, "from_cache": True, "count": len(scenes)})
                self.logger.warning("场景缓存归属不匹配，跳过缓存")

        if not self.api:
            return _err("not_logged_in", "未登录")

        # 获取 home_id
        if not home_id:
            try:
                homes = await self.api.get_homes()
                valid_homes = [h for h in homes if h.id]
                if not valid_homes:
                    return Err(SdkError("没有可用的家庭"))
                home_id = valid_homes[0].id
            except Exception as e:
                return Err(SdkError(f"无法获取默认家庭: {e}"))

        try:
            scenes = await self.api.get_scenes(home_id)
            result = [{"id": s.scene_id, "name": s.name} for s in scenes if s.scene_id]

            # 保存缓存（使用异步写入避免阻塞）
            try:
                user_id = self.api.credential.user_id if self.api and self.api.credential else None
                await atomic_write_json_async(
                    cache_path,
                    {"scenes": result, "home_id": home_id, "user_id": user_id},
                    ensure_ascii=False,
                    indent=2
                )
                self.logger.info(f"场景列表已缓存: {len(result)} 个场景")
            except Exception as e:
                self.logger.warning(f"保存场景缓存失败: {e}")

            lines = [f"🎬 共有 {len(result)} 个智能场景:"]
            for s in result:
                lines.append(f"  • {s.get('name')} (ID: {s.get('id')})")
            message = "\n".join(lines)
            return Ok({"success": True, "message": message, "scenes": result, "from_cache": False, "count": len(result)})
        except TokenExpiredError:
            return _err("token_expired", "凭据已过期，请重新登录")
        except Exception as e:
            self.logger.exception("获取场景列表失败")
            return Err(SdkError(f"获取场景列表失败: {e}"))

    @plugin_entry(
        id="set_device_alias",
        name=tr("entries.set_device_alias.name", default="设置设备别名"),
        description=tr("entries.set_device_alias.description", default="为指定设备设置自定义别名，方便用别名控制设备"),
        input_schema={
            "type": "object",
            "properties": {
                "did": {"type": "string", "description": "设备 DID"},
                "alias": {"type": "string", "description": "自定义别名，多个别名用逗号分隔，如'卧室插座,床头插座'，留空则清除别名"}
            },
            "required": ["did"]
        },
        llm_result_fields=["message"]
    )
    async def set_device_alias(self, did: str, alias: str = "", **_):
        """设置设备别名到缓存"""
        cache_path = self.data_path("devices_cache.json")
        try:
            data = await read_json_async(cache_path)
        except FileNotFoundError:
            return _err("device_cache_missing", "设备缓存不存在，请先获取设备列表")
        except Exception as e:
            return Err(SdkError(f"读取设备缓存失败: {e}"))

        # 归属校验：别名只能写入当前用户的缓存，防止跨账号泄露
        current_user_id = self.api.credential.user_id if self.api and self.api.credential else None
        cache_user_id = data.get('user_id') if isinstance(data, dict) else None
        if current_user_id and cache_user_id and cache_user_id != current_user_id:
            return _err("device_cache_owner_mismatch", "设备缓存归属不匹配，请先刷新设备列表")

        try:
            devices = data.get("devices", [])
            found = False
            for d in devices:
                if d.get("did") == did:
                    if alias:
                        d["alias"] = alias.strip()
                        msg = f"已将'{d.get('name')}'的别名设为：{alias.strip()}"
                    else:
                        d.pop("alias", None)
                        msg = f"已清除'{d.get('name')}'的别名"
                    found = True
                    break

            if not found:
                return Err(SdkError(f"未找到 DID 为 {did} 的设备"))

            await atomic_write_json_async(cache_path, data, ensure_ascii=False, indent=2)

            return Ok({"success": True, "message": msg, "did": did, "alias": alias.strip() if alias else ""})
        except Exception as e:
            return Err(SdkError(f"保存别名失败: {e}"))

    @plugin_entry(
        id="get_device_aliases",
        name=tr("entries.get_device_aliases.name", default="获取设备别名列表"),
        description=tr("entries.get_device_aliases.description", default="返回所有设备的别名映射（did -> alias）"),
        llm_result_fields=["message"]
    )
    async def get_device_aliases(self, **_):
        """获取所有设备别名"""
        cache_path = self.data_path("devices_cache.json")
        try:
            data = await read_json_async(cache_path)
        except FileNotFoundError:
            return Ok({"success": True, "aliases": {}, "message": "无缓存数据"})
        except Exception as e:
            return Err(SdkError(f"读取设备缓存失败: {e}"))

        # 归属校验：只读当前用户的别名，防止跨账号泄露
        current_user_id = self.api.credential.user_id if self.api and self.api.credential else None
        cache_user_id = data.get('user_id') if isinstance(data, dict) else None
        if current_user_id and cache_user_id and cache_user_id != current_user_id:
            return _err("device_cache_owner_mismatch", "设备缓存归属不匹配，请先刷新设备列表")

        try:
            devices = data.get("devices", [])
            aliases = {d.get("did"): d.get("alias", "") for d in devices if d.get("alias")}
            lines = [f"📝 共有 {len(aliases)} 个设备别名:"]
            for did, alias in aliases.items():
                lines.append(f"  • {alias} (DID: {did})")
            message = "\n".join(lines) if aliases else "暂无别名"
            return Ok({"success": True, "aliases": aliases, "message": message})
        except Exception as e:
            return Err(SdkError(f"读取别名失败: {e}"))

    @llm_tool(
        name="smart_control",
        description=SMART_CONTROL_DESCRIPTION,
        parameters=SMART_CONTROL_SCHEMA,
        # 单次控制最长等待：米家云端 prop/set + 写后回读校验偶发要几秒，30s
        # 足够，且远低于 main_server 的 300s 上限。
        timeout=30.0,
    )
    async def smart_control(self, *, command: Any = None, **_) -> dict[str, Any]:
        """对话 LLM 直接调用的设备控制工具（对齐 game_agent_minecraft 的 minecraft_task）。

        只挂 @llm_tool、不再挂 @plugin_entry：宿主会把 analyzer 看的入口列表里的
        __llm_tool__* 剥掉，两边不会对同一条指令各执行一次。
        """
        # command 声明成 Any = None 而非必填 str：LLM 违反 schema（漏传 / 传 null /
        # 传非字符串）时也要进方法体返回一句能读懂的错误，而不是在调用分发处抛
        # TypeError，让模型收到一个无从下手的通用报错。
        if not isinstance(command, str) or not command.strip():
            return _tool_error(
                "缺少 command 参数：请把用户的原话作为 command 传入，如 '打开卧室灯'。",
                code="missing_command",
                detail={"expected": "non-empty string", "param": "command"},
            )
        return _tool_envelope(await self._dispatch_smart_control(command.strip()))

    async def _dispatch_smart_control(self, command: str) -> Any:
        """统一设备控制的分发核心，返回内部 Result（Ok/Err）。

        由 smart_control 的 @llm_tool 边界翻译成工具信封；query / action /
        control 三个执行分支共用同一套 Result 约定。
        """
        if not self.api:
            return _err("not_logged_in", "未登录米家账号，无法控制设备：请先在米家插件面板完成扫码登录")

        # 原始指令属会话文本，按仓库规范用 print 而非 logger
        print(f"[smart_control] 指令: {command}", flush=True)

        # 归一化（剥"把/请/帮我"前缀）+ 场景短路：场景命令与设备无关，
        # 先短路避免加载设备缓存（缓存缺失时会触发网络刷新）
        norm = normalize_utterance(command)
        scene_name = detect_scene(norm)
        if scene_name:
            return await self._execute_scene_by_name(scene_name)

        # 意图路由（nlp/ 纯规则引擎）：开关/查询/动作/属性分支短路
        devices, load_failure = await self._load_devices_cache()
        if load_failure and not devices:
            # 刷新失败 ≠ 账号下没有设备。直接报真实原因，否则 route() 会落到
            # not_found，用户听到的是「设备没匹配上」，掩盖了未登录/网络错。
            return _err("device_list_unavailable", f"无法获取设备列表：{load_failure}", reason=load_failure)
        result = await route(norm, devices)
        # 房间映射惰性获取：仅当匹配确实需要（not_found/ambiguous 且存在缺
        # room_name 的设备——可能是目标设备）时才调 get_homes，避免精确匹配拖慢
        if (
            result.branch in ("switch", "control", "action")
            and result.match is not None
            and result.match.status in ("not_found", "ambiguous")
            and devices
            and any(not d.get("room_name") for d in devices)
        ):
            room_map, device_room_map = await self._build_room_maps()
            result = await route(
                command, devices, api_room_map=room_map, device_room_map=device_room_map
            )
        self.logger.info(f"路由结果: branch={result.branch}")
        # 设备/解析诊断含用户原文片段，按规范用 print
        print(
            f"[smart_control] 路由: branch={result.branch}, "
            f"device={result.device_hint!r}, parsed={result.parsed!r}",
            flush=True,
        )

        if result.branch == "query":
            return await self.query_device_state(result.device_hint)

        # ── 接缝：前端（规则引擎）的解析结果展平为 CommandIR，后端只认它 ──
        # 阶段 4 的 LLM 结构化前端会构出同样结构的 CommandIR，复用同一条执行路径。
        # raw_text 传用户原话：方位词抽取要在归一化前做，保持与改造前一致。
        ir = to_command_ir(result, raw_text=command)
        if result.branch == "action":
            return await self._execute_action_branch(ir)
        if result.branch in ("switch", "control"):
            return await self._execute_control_branch(ir)

        return Err(SdkError(
            "无法理解命令。支持的格式：\n"
            "  开关：'打开卧室灯' / '关掉插座'\n"
            "  亮度：'灯调到50%' / '灯亮度50'\n"
            "  温度：'空调调26度'\n"
            "  模式：'空调调制冷'\n"
            "  场景：'执行回家场景'"
        ))

    async def _execute_action_branch(self, ir: CommandIR) -> Any:
        """执行设备动作分支（开始/暂停/停止/回充等）。

        入参是 CommandIR 而非 RouteResult——后端不再认识前端的解析对象，
        这样 LLM 结构化前端（阶段 4）能复用同一条执行路径。
        """
        if ir.match_status == "ok" and ir.device_info is not None:
            device = ir.device_info
            did = ir.device_id
            actions = device.get("actions", [])
            display_name = device.get("alias") or device.get("name", ir.device)

            # 从动词推断 action name
            candidates = VERB_TO_ACTION.get(ir.verb, [ir.verb])
            matched_action = None
            for a in actions:
                aname = a.get("name", "").lower()
                if any(c.lower() == aname for c in candidates):
                    matched_action = a
                    break
            if not matched_action:
                # 模糊匹配
                for a in actions:
                    aname = a.get("name", "").lower()
                    if any(c.lower() in aname for c in candidates):
                        matched_action = a
                        break

            if matched_action:
                try:
                    act_result = await self.api.call_device_action(
                        did, matched_action["siid"], matched_action["aiid"]
                    )
                    # call_device_action 返回 False 表示动作被设备/网关拒绝，不能报成功
                    if act_result:
                        return Ok({"success": True, "message": f"✅ 已对'{display_name}'执行'{ir.verb}'操作", "device": display_name, "action": ir.verb, "result": act_result})
                    return _err(
                        "action_rejected",
                        self.i18n.t(
                            "switch.action_rejected",
                            locale=self._lang,
                            default="对'{name}'执行'{action}'操作失败（动作被拒绝）",
                            name=display_name, action=ir.verb,
                        ),
                        device=display_name, action=ir.verb,
                    )
                except TokenExpiredError:
                    return _err("token_expired", "凭据已过期，请重新登录")
                except Exception as e:
                    return _err("device_control_failed", f"对'{display_name}'执行'{ir.verb}'操作失败: {e}", device=display_name, action=ir.verb)
            else:
                action_names = [a.get("name") for a in actions]
                return _err(
                    "action_not_found",
                    f"'{display_name}'没有'{ir.verb}'操作。可用操作：{', '.join(action_names) if action_names else '无'}",
                    device=display_name,
                    action=ir.verb,
                    available=action_names,
                )

        # 设备未匹配/歧义：明确报错（原逻辑在此场景会掉落到属性解析，产生无意义结果）
        if ir.match_status == "ambiguous":
            return _err(
                "device_ambiguous",
                ir.match_message or "找到多个匹配设备",
                candidates=ir.candidates,
            )
        if ir.match_message:
            return _err("device_not_found", ir.match_message, candidates=ir.candidates)
        return _err("device_not_found", f"未找到设备'{ir.device}'", device=ir.device, candidates=ir.candidates)

    async def _execute_control_branch(self, ir: CommandIR) -> Any:
        """执行控制分支（switch / set_prop / adjust_prop）"""
        if ir.match_status == "ambiguous":
            return _err(
                "device_ambiguous",
                ir.match_message or "找到多个匹配设备",
                candidates=ir.candidates,
            )
        if ir.match_status != "ok" or ir.device_info is None:
            return _err(
                "device_not_found",
                ir.match_message or "未找到设备",
                candidates=ir.candidates,
            )
        if not ir.action:
            return _err("unparsable_command", "无法解析控制命令")

        device = ir.device_info
        did = ir.device_id
        display_name = device.get("alias") or device.get("name", ir.device)
        props = device.get("properties", [])

        if ir.action == "switch":
            return await self._execute_switch(ir)
        if ir.action == "adjust_prop":
            return await self._execute_adjust_prop(props, did, display_name, ir)
        return await self._execute_set_prop(props, did, display_name, ir)

    @staticmethod
    def _find_power_action(actions: list[dict], value: Any) -> Optional[dict]:
        """在设备动作中查找电源开关动作：True → Turn On，False → Turn Off。

        MIoT 电视/显示器等设备没有可写的 power 属性，电源开关以动作形式暴露
        （如 xiaomi.tv.rmh1 的 "Turn On"(siid 6) / "Turn Off"(siid 2)）。
        不收录 "set power" 类动作——它们通常带 in 参数，无参调用会被云端拒绝。
        """
        keywords = ["turn on", "power on", "switch on", "power-on"]
        if not value:
            keywords = ["turn off", "power off", "switch off", "power-off"]
        for a in actions:
            aname = (a.get("name") or "").lower()
            if any(k in aname for k in keywords):
                return a
        return None

    async def _execute_switch(self, ir: CommandIR) -> Any:
        """执行二元开关控制（电源动作 → 开关属性，含多控开关方位匹配）。

        方位由前端解析后放在 `ir.gang`；本方法**不再扫描原始文本**。
        `ir.require_explicit_gang=True` 时（LLM 前端），多控设备缺方位会返回
        `gang_ambiguous` + 候选，让模型追问或重试；规则前端保持"自动选主开关"。
        """
        device = ir.device_info or {}
        did = ir.device_id or device.get("did")
        display_name = device.get("alias") or device.get("name") or ir.device
        props = device.get("properties", [])
        actions = device.get("actions", [])
        value = ir.value
        action_text = "打开" if value else "关闭"

        # 1) 电源动作优先；网关不支持动作时（如 /miotspec/action 返回 -6）回落属性通道。
        #    仅当动作真正执行成功（act_result 为 True）才返回成功，否则继续回落。
        power_action = self._find_power_action(actions, value)
        if power_action:
            try:
                act_result = await self.api.call_device_action(
                    did, power_action["siid"], power_action["aiid"]
                )
                if act_result:
                    return Ok({"success": True, "message": f"✅ 已{action_text}'{display_name}'", "device": display_name, "action": action_text, "result": act_result})
                self.logger.info("电源动作返回失败，回落属性控制")
            except TokenExpiredError:
                return _err("token_expired", "凭据已过期，请重新登录")
            except Exception as e:
                self.logger.info(f"电源动作不可用，回落属性控制: {e}")

        # 2) 收集所有可写的开关属性
        switch_props = []
        for p in props:
            pname = p.get("name", "").lower()
            if any(k in pname for k in ["开关", "电源", "power", "switch", "on"]):
                if p.get("access") in ["write", "read_write", "notify_read_write"]:
                    switch_props.append(p)
        # 二进制开关指令只应发给 bool 型属性：无条件过滤掉名字含 "on" 的非布尔
        # 属性（"TV Input Control"/position/ventilation/illumination 等），
        # 否则 True/False 会被发给 enum/int/string 属性并触发控制失败。
        # 兜底不再接受"任意可写 bool"——仅保留名称明确标识电源的属性，
        # 避免把 Mute/儿童锁等无关布尔属性误当电源开关。
        switch_props = [p for p in switch_props if p.get("type") == "bool"]

        if not switch_props:
            return _err("switch_not_found", f"'{display_name}'没有可控制的开关", device=display_name)

        # 多控开关：方位来自 CommandIR.gang（前端解析），后端不碰原文
        switch = None
        if len(switch_props) == 1:
            switch = switch_props[0]
        else:
            gang_keywords = position_keywords(ir.gang) if ir.gang else []
            if gang_keywords:
                for p in switch_props:
                    sdesc = (p.get("service_desc") or "").lower()
                    if any(kw.lower() in sdesc for kw in gang_keywords):
                        switch = p
                        break

            if not switch:
                if ir.require_explicit_gang:
                    # LLM 前端：不替模型猜，回候选让它追问或重试
                    return _err(
                        "gang_ambiguous",
                        f"'{display_name}'有多个开关，请指定要控制哪一个",
                        device=display_name,
                        gang_options=[
                            {
                                "index": i,
                                "label": p.get("service_desc") or p.get("name") or f"gang {i}",
                                "siid": p.get("siid"),
                                "piid": p.get("piid"),
                            }
                            for i, p in enumerate(switch_props, 1)
                        ],
                    )
                # 规则前端（中文）保持旧行为：自动选主开关
                switch = self._pick_default_switch(switch_props, display_name)
                self.logger.info(
                    f"多控开关未指定方位，自动选择默认主开关: "
                    f"device={display_name}, service_desc={switch.get('service_desc')}, "
                    f"siid={switch.get('siid')}, piid={switch.get('piid')}"
                )

        siid = switch.get("siid")
        piid = switch.get("piid")
        try:
            success = await self.api.control_device(did, siid, piid, value)
            action_text = "打开" if value else "关闭"
            if not success:
                # 语义失败必须走 Err：@llm_tool 边界按 Ok/Err 决定 is_error，
                # Ok({"success": False}) 会被标成 is_error=false 对模型假报成功。
                return _err("device_rejected", f"{action_text}'{display_name}'失败", device=display_name, action=action_text)
            # 写后回读校验：防止写入"属性合法但并非电源"的字段（如电视 speaker-mode
            # 的 is-on）被 API 照单全收返回 code 0，却对真机毫无作用。
            # 只写（access="write"）属性读不回来，无法验证是否真生效 → 返回未确认，
            # 不能直接报成功（可能写的是非电源功能开关）。
            # 云端属性传播有延迟（实测关闭可慢至数秒），故轮询而非单次回读。
            if switch.get("access") == "write":
                return _err(
                    "write_unconfirmed",
                    self.i18n.t(
                        "switch.unconfirmed",
                        locale=self._lang,
                        default="已向'{name}'发送{action}指令，但无法确认是否已生效",
                        name=display_name, action=action_text,
                    ),
                    device=display_name, action=action_text,
                )
            actual = None
            for wait in (0.5, 1.5, 3.0):
                await asyncio.sleep(wait)
                try:
                    chk = await self.api.get_device_properties(
                        [{"did": did, "siid": siid, "piid": piid}]
                    )
                    actual = chk[0].get("value") if chk else None
                except TokenExpiredError:
                    return _err("token_expired", "凭据已过期，请重新登录")
                except Exception:
                    actual = None
                if actual is not None and bool(actual) == bool(value):
                    actual = value
                    break
            if actual != value:
                self.logger.warning(
                    f"开关回读校验失败: device={display_name}, siid={siid}, piid={piid}, "
                    f"期望={value}, 实际={actual}（写入未生效）"
                )
                return _err(
                    "no_response",
                    self.i18n.t(
                        "switch.no_response",
                        locale=self._lang,
                        default="{action}'{name}'失败：设备未响应电源控制（{name} 可能不支持云端开关）",
                        action=action_text, name=display_name,
                    ),
                    device=display_name, action=action_text,
                )
            return Ok({"success": True, "message": f"✅ 已{action_text}'{display_name}'", "device": display_name, "action": action_text})
        except TokenExpiredError:
            return _err("token_expired", "凭据已过期，请重新登录")
        except MijiaAPIException as e:
            self.logger.warning(f"API控制失败: device={display_name}, did={did}, siid={siid}, piid={piid}, value={value}, error={e}")
            if e.code == -6:
                return _err("device_rejected", f"控制'{display_name}'失败：设备不支持该操作或参数有误（siid={siid}, piid={piid}），请检查设备是否在线", device=display_name)
            return _err("device_control_failed", f"控制'{display_name}'失败: {e}", device=display_name)
        except Exception as e:
            self.logger.exception("控制失败")
            return _err("device_control_failed", f"控制失败: {e}")

    async def _execute_adjust_prop(self, props: list[dict], did: str, display_name: str, ir: CommandIR) -> Any:
        """执行相对值调整（调亮一点/温度高一点）"""
        prop_name = ir.attribute
        direction = ir.direction
        delta = ir.delta
        prop = self._find_property_for_control(props, prop_name, None)
        if not prop:
            available = [p.get("name") for p in props if p.get("access") in ["write", "read_write", "notify_read_write"]]
            return _err("attribute_not_found", f"'{display_name}'没有可控制的'{prop_name}'属性。可控制属性：{', '.join(available) if available else '无'}", device=display_name, attribute=prop_name, available=available)

        siid = prop.get("siid")
        piid = prop.get("piid")
        vr = prop.get("value_range", [])

        # 读取当前值
        try:
            cur_results = await self.api.get_device_properties([{"did": did, "siid": siid, "piid": piid}])
            cur_value = cur_results[0].get("value") if cur_results else None
        except TokenExpiredError:
            return _err("token_expired", "凭据已过期，请重新登录")
        except Exception:
            cur_value = None

        if cur_value is None:
            return _err("current_value_unavailable", f"无法读取'{display_name}'的{prop_name}当前值", device=display_name, attribute=prop_name)

        # 计算目标值（钳值 + 步长对齐，逻辑见 nlp/value_resolver.py）
        target = resolve_adjust_target(cur_value, direction, delta, vr)

        try:
            success = await self.api.control_device(did, siid, piid, target)
            if success:
                return Ok({"success": True, "message": f"✅ 已将'{display_name}'的{prop_name}从{cur_value}调整为{target}", "device": display_name, "property": prop_name, "value": target})
            # 语义失败走 Err（同 _execute_set_prop：Ok({"success": False}) 会被
            # @llm_tool 边界标成 is_error=false）
            return _err("device_rejected", f"调整'{display_name}'的{prop_name}失败", device=display_name, attribute=prop_name)
        except TokenExpiredError:
            return _err("token_expired", "凭据已过期，请重新登录")
        except MijiaAPIException as e:
            return _err("device_control_failed", f"调整'{display_name}'的{prop_name}失败: {e}", device=display_name, attribute=prop_name)
        except Exception as e:
            self.logger.exception("调整失败")
            return _err("device_control_failed", f"调整失败: {e}")

    async def _execute_set_prop(self, props: list[dict], did: str, display_name: str, ir: CommandIR) -> Any:
        """执行属性控制（亮度/温度/模式等）"""
        prop_name = ir.attribute
        if not prop_name:
            return _err("attribute_required", "请指定要调整的属性，如'灯亮度50%'")

        prop = self._find_property_for_control(props, prop_name, ir.value)
        if not prop:
            available = [p.get("name") for p in props if p.get("access") in ["write", "read_write", "notify_read_write"]]
            return _err("attribute_not_found", f"'{display_name}'没有可控制的'{prop_name}'属性。可控制属性：{', '.join(available) if available else '无'}", device=display_name, attribute=prop_name, available=available)

        # 能力校验：单位/范围与设备能力不兼容时（如 亮度 不接受 档），由能力层判
        # INVALID_UNIT / INVALID_VALUE，而非 NLP 层替设备决定
        cap_check = validate_command(prop, prop_name, ir.value, ir.unit)
        if cap_check.status != "valid":
            return _err(cap_check.status, cap_check.message, **cap_check.detail)

        value = ir.value
        # 极值处理："最高"/"最低" → 从 value_range 取边界
        if value in ("max", "min"):
            vr = prop.get("value_range", [])
            if len(vr) >= 2:
                value = vr[1] if value == "max" else vr[0]
            elif prop.get("value_list"):
                vals = [
                    item.get("value", 0) if isinstance(item, dict) else item
                    for item in prop.get("value_list", [])
                ]
                value = max(vals) if value == "max" else min(vals)
            else:
                return _err("value_range_unavailable", f"'{display_name}'的{prop_name}没有值范围信息，无法设置极值", device=display_name, attribute=prop_name)

        # 模式/档位枚举转换：中文 → 设备 spec 数字值
        if isinstance(value, str) and prop.get("value_list"):
            resolved = self._resolve_enum_value(prop, value)
            if resolved is not None:
                # 日志不落用户输入的中文模式词，只记转换后的数字值
                self.logger.info(f"枚举转换成功 → {resolved}")
                value = resolved
            else:
                # 标签回退顺序：description → comment → value
                available_modes = [
                    (
                        f"{item.get('description') or item.get('comment') or item.get('value')}"
                        f"(={item.get('value')})"
                        if isinstance(item, dict) else str(item)
                    )
                    for item in prop.get("value_list", [])
                ]
                return _err(
                    "value_not_allowed",
                    f"'{display_name}'的{prop_name}不支持'{value}'。"
                    f"可用模式：{', '.join(available_modes)}",
                    device=display_name,
                    attribute=prop_name,
                    value=value,
                    allowed_modes=available_modes,
                )

        # 窗帘位置：MIoT spec 定义 0=关闭, 100=全开，直接透传用户设定值
        # 不做反转——用户说"位置到80"即设为 80（80% 开）

        siid = prop.get("siid")
        piid = prop.get("piid")
        self.logger.info(f"控制属性: {prop.get('name')}, siid={siid}, piid={piid}, value={value}")

        try:
            success = await self.api.control_device(did, siid, piid, value)
            if success:
                unit = prop.get("unit", "")
                value_display = f"{value}{unit}" if unit else str(value)
                return Ok({"success": True, "message": f"✅ 已将'{display_name}'的{prop_name}设为{value_display}", "device": display_name, "property": prop_name, "value": value})
            # 语义失败走 Err：下面 MijiaAPIException 分支也是 Err，同一类失败不
            # 能一半 Ok 一半 Err——@llm_tool 边界只看 Ok/Err 定 is_error。
            return _err("device_rejected", f"设置'{display_name}'的{prop_name}失败", device=display_name, attribute=prop_name)
        except TokenExpiredError:
            return _err("token_expired", "凭据已过期，请重新登录")
        except MijiaAPIException as e:
            self.logger.warning(f"API控制失败: device={display_name}, did={did}, siid={siid}, piid={piid}, value={value}, error={e}")
            if e.code == -6:
                return _err("device_rejected", f"设置'{display_name}'的{prop_name}失败：设备不支持该操作或参数有误", device=display_name, attribute=prop_name)
            return _err("device_control_failed", f"设置'{display_name}'的{prop_name}失败: {e}", device=display_name, attribute=prop_name)
        except Exception as e:
            self.logger.exception("控制失败")
            return _err("device_control_failed", f"控制失败: {e}")

    @staticmethod
    def _pick_default_switch(switch_props: list, display_name: str = "") -> dict:
        """当多控开关未指定方位时，按默认主开关优先级自动选择。

        优先级：
        1. 通用 Switch（无方位前缀，主开关 / USB 插座的主插孔）
        2. Left Switch Service（左键）
        3. First Switch Service（第 1 键）
        4. Middle Switch Service（中键）
        5. Right Switch Service（右键）
        6. Second / Third / Fourth / Fifth / Sixth Switch Service
        7. 兜底：第一个可写开关
        """
        import re as _re

        _POSITIONAL_PREFIXES = {"left", "right", "middle", "center",
                                "first", "second", "third", "fourth", "fifth", "sixth"}

        def _svc_desc(p):
            return (p.get("service_desc") or "").lower()

        def _is_generic_switch(sd):
            """通用 Switch：含 'switch' 但不含方位/序号前缀，也非 USB 开关。"""
            if "switch" not in sd:
                return False
            if "usb" in sd:
                return False
            words = sd.split()
            # "switch" 或 "switch service" → 通用主开关
            if words[0] == "switch":
                return True
            # 如果第一个词是方位/序号前缀，则不是通用的
            return words[0] not in _POSITIONAL_PREFIXES

        # 按优先级匹配
        _POSITIONAL_ORDER = [
            (["left"], r"left\s+switch"),
            (["first"], r"first\s+switch"),
            (["middle", "center"], r"(?:middle|center)\s+switch"),
            (["right"], r"right\s+switch"),
            (["second"], r"second\s+switch"),
            (["third"], r"third\s+switch"),
            (["fourth"], r"fourth\s+switch"),
            (["fifth"], r"fifth\s+switch"),
            (["sixth"], r"sixth\s+switch"),
        ]

        # 第一优先：通用 Switch（主开关 / 主插孔）
        for p in switch_props:
            if _is_generic_switch(_svc_desc(p)):
                return p

        # 第二优先：按方位/序号顺序
        for _, pattern in _POSITIONAL_ORDER:
            for p in switch_props:
                if _re.search(pattern, _svc_desc(p)):
                    return p

        # 第二步：过滤掉 USB 子开关，取第一个剩余的
        non_usb = [p for p in switch_props if "usb" not in _svc_desc(p)]
        if non_usb:
            return non_usb[0]

        # 最终兜底：直接取第一个
        return switch_props[0]

    def _find_property_for_control(self, props: list[dict], prop_name: str, value: Any) -> Optional[dict]:
        """根据属性名和目标值，从设备属性列表中找到匹配的可写属性"""
        prop_name_lower = prop_name.lower()

        keywords = PROP_KEYWORDS.get(prop_name, [prop_name_lower])

        # 1. 按关键词匹配可写属性
        for p in props:
            if p.get("access") not in ["write", "read_write", "notify_read_write"]:
                continue
            pname = p.get("name", "").lower()
            if any(kw in pname for kw in keywords):
                return p

        # 2. 模式命令：找第一个可写 string/uint 属性（通常模式属性是 enum）
        if prop_name == "模式":
            for p in props:
                if p.get("access") not in ["write", "read_write", "notify_read_write"]:
                    continue
                ptype = p.get("type", "")
                if ptype in ["string", "uint8", "uint16", "uint32", "int8", "int16", "int32"]:
                    # 检查是否有 value_list（枚举属性）
                    if p.get("value_list"):
                        return p

        return None

    # ── 中文模式名 → 英文枚举关键词映射（对齐小爱同学翻译表） ──
    _MODE_CN_TO_EN: dict[str, list[str]] = {
        # 空调
        "制冷": ["Cool"], "制热": ["Heat"], "送风": ["Fan"],
        # 通用
        "自动": ["Auto"], "睡眠": ["Sleep"], "静音": ["Silent", "Qtet"],
        "强力": ["Strong", "Intensive", "Turbo"], "智能": ["Smart"],
        "节能": ["Energy Saving"], "舒适": ["Comfort"],
        "标准": ["Basic", "Standard"], "手动": ["None", "Manual"],
        "最爱": ["Favorite"], "除湿": ["Dry"],
        # 灯
        "日光": ["Day"], "月光": ["Night"], "彩光": ["Color"],
        "温馨": ["Warmth"], "电视": ["Tv"], "阅读": ["Reading"],
        "电脑": ["Computer"], "娱乐": ["Entertainment"],
        "休闲": ["Leisure"], "办公": ["Office"], "儿童": ["Baby", "Baby Care"],
        "夜灯": ["Night Light", "Nightlight"],
        # 风扇
        "自然风": ["Natural Wind"], "直吹风": ["Straight Wind"],
        "冷风": ["Cold Air"],
        # 净化器/新风机
        "低风": ["Low"], "中风": ["Medium"], "高风": ["High"],
        "超强": ["Turbo"],
        # 浴霸
        "暖风": ["Hot", "Warm", "Heat"], "热风": ["Hot"],
        "换气": ["Ventilate"], "干燥": ["Dry"], "吹风": ["Fan"],
        "待机": ["Idle"], "恒温": ["Constant Temperature"],
        # 洗衣机
        "日常洗": ["Daily Wash"], "快速洗": ["Quick Wash"], "快洗": ["Quick Wash"],
        "轻柔": ["Delicate Wash", "Delicate"], "大件": ["Heavy Wash", "Large wash"],
        "棉麻": ["Cotton"], "化纤": ["Synthetic"], "羊毛": ["Wool"],
        "婴童": ["Baby Care"], "内衣": ["Underwear"], "丝绸": ["Silk"],
        "牛仔": ["Jeans"], "蒸汽": ["Steam Wash"], "护色": ["Color Protection"],
        "防过敏": ["Anti-allergy"], "顽渍": ["Stain Wash"],
        "智能洗": ["Smart"], "混合": ["Mix"], "冲锋衣": ["Jacket"],
        "衬衣": ["Shirt"], "桶自洁": ["Drum Clean"],
        "烘干": ["Dry", "Wash Dry"], "除菌": ["Sterilization"],
        "除螨": ["Mite Removal"], "新衣": ["New-Clothes Wash"],
        "单漂": ["Rinse"], "单脱": ["Spin"], "自定义": ["User Define"],
        "高温": ["Boiling"], "空气洗": ["Dry Air Wash"],
        "快洗烘": ["Quick Wash Dry"], "智能洗烘": ["Wash Dry"],
        "运动": ["Sportswear", "Sport Mode"],
        # 洗碗机
        "玻璃": ["Glass"], "预洗": ["Prewash"], "少量": ["Bit Wash"],
        "自洁": ["Self Clean"], "消毒": ["Disinfecting"],
        "奶瓶": ["Bottle"], "大物": ["Large wash"], "锅具": ["Pot wash"],
        "分层": ["Layered Wash"], "随心": ["Pleased"], "及时": ["Timely"],
        # 冰箱
        "假日": ["Holiday"], "速冷": ["Quick Cooling"], "速冻": ["Quick Frozen"],
        # 扫地机
        "安静": ["Silent"], "全速": ["Full Speed"],
        # 按摩椅
        "全身": ["Full Body"], "肩颈": ["Shoulder and Neck"],
        "腰臀": ["Waist and Hip"], "日常放松": ["Relaxed"],
        "助眠": ["Sleep"], "解压": ["Relieve Stress"],
        "零重力": ["Zero Gravity"],
        # 电饭煲
        "精煮": ["Fine Cook"], "快煮": ["Quick Cook"],
        "煮粥": ["Cook Congee"], "保温": ["Keep Warm"],
        "蒸饭": ["Steam Rice"], "煲仔饭": ["ClaypotRice"],
        "杂粮": ["MultigrainRice"], "炖汤": ["Soup"],
        # 香薰机
        "唤醒": ["Wake Up"],
        # 热水器
        "速热": ["Quick Heat"],
    }

    def _resolve_enum_value(self, prop: dict, chinese_value: str) -> Optional[int]:
        """将中文模式/档位名转换为设备 spec 的数字枚举值。

        Args:
            prop: 设备属性字典（需含 value_list）
            chinese_value: 用户输入的中文值，如 "制冷"

        Returns:
            匹配到的数字值，未匹配返回 None
        """
        value_list = prop.get("value_list")
        if not value_list:
            return None

        # 直接尝试把输入当数字
        if isinstance(chinese_value, (int, float)):
            return int(chinese_value)

        en_keywords = self._MODE_CN_TO_EN.get(chinese_value, [])
        chinese_lower = chinese_value.lower()

        for item in value_list:
            # 兼容旧缓存：value_list 可能是纯值列表 [0,1,2]（无描述，无法映射中文）
            if not isinstance(item, dict):
                continue
            desc = str(item.get("description", ""))
            comment = str(item.get("comment", ""))
            desc_lower = desc.lower()
            comment_lower = comment.lower()
            for field_lower in (desc_lower, comment_lower):
                # 英文枚举名精确匹配
                if any(field_lower == kw.lower() for kw in en_keywords):
                    return item["value"]
                # 英文枚举名包含匹配
                if any(kw.lower() in field_lower for kw in en_keywords):
                    return item["value"]
            # 中文直接匹配（description/comment 可能是中文，如 spec 的 comment 恒湿/睡眠/风干）
            if chinese_lower in desc_lower or chinese_lower in comment_lower:
                return item["value"]

        return None

    async def _execute_scene_by_name(self, scene_name: str) -> Any:
        """按场景名称查找并执行场景"""
        cache_path = self.data_path("scenes_cache.json")
        scenes = []
        cached_home_id = None
        cached_user_id = None
        try:
            cached = await read_json_async(cache_path)
            scenes = cached.get('scenes', [])
            cached_home_id = cached.get('home_id')
            cached_user_id = cached.get('user_id')
        except Exception:
            pass

        if not scenes:
            return _err("scene_list_empty", "场景列表为空，请先获取场景列表")

        # 归属校验：场景缓存必须属于当前账号（user_id 严格匹配）
        current_user_id = self.api.credential.user_id if self.api else None
        if current_user_id and cached_user_id and cached_user_id != current_user_id:
            return _err("scene_cache_owner_mismatch", "场景缓存归属不匹配，请先刷新场景列表")

        # 归属校验：缓存的 home_id 必须属于当前账号
        if cached_home_id and self.api:
            try:
                homes = await self.api.get_homes()
                valid_home_ids = {h.id for h in homes if h.id}
                if cached_home_id not in valid_home_ids:
                    return _err("scene_cache_owner_mismatch", "场景缓存归属不匹配，请先刷新场景列表")
            except Exception:
                pass

        # 模糊匹配场景名
        name_lower = scene_name.lower()
        matched = [s for s in scenes if name_lower in s.get("name", "").lower()]

        if not matched:
            names = [s.get("name") for s in scenes]
            return _err("scene_not_found", f"未找到场景'{scene_name}'。当前场景：{', '.join(names)}", scene=scene_name, available=names)
        if len(matched) > 1:
            names = [s.get("name") for s in matched]
            return _err("scene_ambiguous", f"找到多个匹配场景：{', '.join(names)}，请更精确指定", candidates=names)

        scene = matched[0]
        # 优先使用缓存中的 home_id（场景所属家庭），否则回退到第一个家庭
        home_id = cached_home_id
        if not home_id:
            try:
                homes = await self.api.get_homes()
                home_id = homes[0].id if homes else None
            except Exception:
                home_id = None

        if not home_id:
            return _err("home_not_found", "未找到可用家庭")

        try:
            success = await self.api.execute_scene(scene["id"], home_id)
            if success:
                return Ok({"success": True, "message": f"✅ 已执行场景'{scene['name']}'"})
            return _err("scene_execution_failed", f"执行场景'{scene['name']}'失败", scene=scene.get("name"))
        except Exception as e:
            return _err("scene_execution_failed", f"执行场景失败: {e}")


    # ========== 辅助功能：获取设备规格（可选） ==========
    @plugin_entry(
        id="query_device_state",
        name=tr("entries.query_device_state.name", default="查询设备状态"),
        description=tr("entries.query_device_state.description", default="按名称查询设备所有可读属性的当前值，支持设备名、别名、房间名+设备名"),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "设备名称、别名或房间名+设备名，如'灯'、'卧室灯'、'床头插座'"}
            },
            "required": ["name"]
        },
        llm_result_fields=["message"]
    )
    async def query_device_state(self, name: str, **_):
        """根据设备名称查询设备状态"""
        if not self.api:
            return _err("not_logged_in", "未登录")

        # 统一设备匹配（支持区域+设备名、别名、模糊匹配）
        cached_devices, load_failure = await self._load_devices_cache()
        if load_failure and not cached_devices:
            return _err("device_list_unavailable", f"无法获取设备列表：{load_failure}", reason=load_failure)
        match_result = match_devices(name, cached_devices)
        # 房间映射惰性获取：仅当匹配需要（not_found/ambiguous 且存在缺 room_name
        # 的设备）时才调 get_homes，避免精确匹配被拖慢
        if (
            match_result.status in ("not_found", "ambiguous")
            and cached_devices
            and any(not d.get("room_name") for d in cached_devices)
        ):
            room_map, device_room_map = await self._build_room_maps()
            match_result = match_devices(
                name, cached_devices, api_room_map=room_map, device_room_map=device_room_map
            )
        if match_result.status == "not_found":
            return _err("device_not_found", match_result.message, candidates=match_result.candidates)
        if match_result.status == "ambiguous":
            return _err("device_ambiguous", match_result.message, candidates=match_result.candidates)

        devices = match_result.devices
        
        device = devices[0]
        did = device.get("did")
        device_name = device.get("name", name)
        props = device.get("properties", [])
        
        if not props:
            return Ok({
                "success": True,
                "message": f"📱 设备 '{device_name}' 没有可查询的属性",
                "device": device_name,
                "states": []
            })
        
        # 构建查询请求（所有可读属性）
        requests = []
        readable_props = []
        for p in props:
            access = p.get("access", "")
            if access in ["read", "read_write", "notify_read", "notify_read_write"]:
                requests.append({
                    "did": did,
                    "siid": p.get("siid"),
                    "piid": p.get("piid")
                })
                readable_props.append(p)
        
        if not requests:
            return Ok({
                "success": True,
                "message": f"📱 设备 '{device_name}' 没有可读属性",
                "device": device_name,
                "states": []
            })
        
        try:
            results = await self.api.get_device_properties(requests)
            
            # 用 (siid, piid) 建立索引，不依赖返回顺序
            result_map = {}
            for res in results:
                key = (res.get("siid"), res.get("piid"))
                result_map[key] = res
            
            # 整理状态信息
            states = []
            lines = [f"📱 设备 '{device_name}' 当前状态："]
            lines.append("")

            for prop in readable_props:
                key = (prop.get("siid"), prop.get("piid"))
                res = result_map.get(key)
                if res is None:
                    continue

                siid = prop.get("siid")
                piid = prop.get("piid")
                original_name = prop.get("name", f"属性{piid}")

                # 属性名本地化（保留原始名用于调试）
                display_name = NAME_MAP.get(original_name, original_name)

                value = res.get("value")
                code = res.get("code", -1)
                # 优先使用 spec 中的 unit，否则使用硬编码映射
                unit = prop.get("unit") or UNIT_MAP.get(original_name)

                if code == 0:
                    # 格式化值
                    if isinstance(value, bool):
                        value_str = "✅ 开启" if value else "❌ 关闭"
                    else:
                        # 窗帘 current-position 算法修正：
                        # MIoT spec 定义 0=关闭, 100=全开
                        # 临界点 0-2 判定为"关着"，3-100 判定为"开着"
                        sdesc_lower = (prop.get("service_desc") or "").lower()
                        is_curtain = "curtain" in sdesc_lower or "窗帘" in sdesc_lower
                        if is_curtain and original_name == "Current Position" and isinstance(value, (int, float)):
                            state_text = "关闭" if value <= 2 else "开启"
                            value_str = f"{'❌' if value <= 2 else '✅'} {state_text}（{value}%）"
                        else:
                            value_str = str(value)
                            # 添加单位
                            if unit:
                                value_str = f"{value_str} {unit}"

                    states.append({
                        "name": display_name,
                        "original_name": original_name,
                        "value": value,
                        "siid": siid,
                        "piid": piid,
                        "unit": unit
                    })
                    lines.append(f"  • {display_name}: {value_str}")
            
            if not states:
                lines.append("  （暂无可用状态数据）")
            
            message = "\n".join(lines)
            return Ok({
                "success": True,
                "message": message,
                "device": device_name,
                "states": states
            })
            
        except TokenExpiredError:
            return _err("token_expired", "凭据已过期，请重新登录")
        except Exception as e:
            self.logger.exception("查询设备状态失败")
            return Err(SdkError(f"查询设备状态失败: {e}"))

