"""仓储层

抽象数据访问逻辑，提供接口和实现。

模块：
- interfaces: 仓储抽象接口（IHomeRepository / IDeviceSpecRepository /
  ISceneRepository / IAsyncDeviceRepository）。命名不带 Async 的那几个接口目前
  也由异步实现继承，是历史命名，不是「同步专用」。
- async_device_repository: 设备仓储实现
- async_home_repository: 家庭仓储实现
- async_scene_repository: 场景仓储实现
- async_device_spec_repository: 设备规格仓储实现

注：同步仓储实现已移除，见包级 __init__ 的说明。
"""

from ..repositories.async_device_repository import AsyncDeviceRepositoryImpl
from ..repositories.async_device_spec_repository import AsyncDeviceSpecRepositoryImpl
from ..repositories.async_home_repository import AsyncHomeRepositoryImpl
from ..repositories.async_scene_repository import AsyncSceneRepositoryImpl
from ..repositories.interfaces import (
    DeviceSpec,
    IAsyncDeviceRepository,
    IDeviceSpecRepository,
    IHomeRepository,
    ISceneRepository,
)

__all__ = [
    # 仓储接口
    "IHomeRepository",
    "IAsyncDeviceRepository",
    "ISceneRepository",
    "IDeviceSpecRepository",
    # 数据模型
    "DeviceSpec",
    # 仓储实现
    "AsyncDeviceRepositoryImpl",
    "AsyncHomeRepositoryImpl",
    "AsyncSceneRepositoryImpl",
    "AsyncDeviceSpecRepositoryImpl",
]
