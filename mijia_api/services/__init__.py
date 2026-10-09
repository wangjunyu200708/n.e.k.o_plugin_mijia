"""服务层

封装业务逻辑和用例，协调多个仓储完成复杂业务功能。

模块：
- auth_service: 认证服务（同步实现，插件直接使用；其网络调用经
  asyncio.to_thread 包装成异步）
- async_device_service / async_scene_service / async_statistics_service: 异步服务

注：同步 device/scene/statistics 服务已移除，见包级 __init__ 的说明。
"""

from ..services.async_device_service import AsyncDeviceService
from ..services.async_scene_service import AsyncSceneService
from ..services.async_statistics_service import AsyncStatisticsService
from ..services.auth_service import AuthService

__all__ = [
    "AuthService",
    "AsyncDeviceService",
    "AsyncSceneService",
    "AsyncStatisticsService",
]
