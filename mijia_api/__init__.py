"""米家API SDK - 重构版本（N.E.K.O 插件内嵌）

分层架构：core / domain / infrastructure / repositories / services / factory。

用法：
    from mijia_api import create_async_api_client
    from mijia_api.infrastructure.credential_provider import CredentialProvider

    provider = CredentialProvider()
    credential = await provider.get_qrcode_async()   # 扫码登录（异步包装）
    api = create_async_api_client(credential)
    devices = await api.get_devices(home_id="your_home_id")

注：同步 API（MijiaAPI / create_api_client / create_multi_user_clients）已移除
——插件宿主是 asyncio 事件循环，同步实现既用不上，又要跟着异步版双份维护。
"""

__version__ = "2.0.1"
__author__ = "MijiaAPI Contributors"

from .api_client import AsyncMijiaAPI
from .domain.exceptions import (
    AuthenticationError,
    DeviceError,
    MijiaAPIException,
    NetworkError,
    ValidationError,
)
from .domain.models import (
    Credential,
    Device,
    DeviceAction,
    DeviceProperty,
    Home,
    Scene,
)
from .factory import (
    create_async_api_client,
    create_auth_service,
)

__all__ = [
    # 版本信息
    "__version__",
    "__author__",
    # API客户端
    "AsyncMijiaAPI",
    # 工厂函数
    "create_async_api_client",
    "create_auth_service",
    # 领域模型
    "Credential",
    "Device",
    "DeviceProperty",
    "DeviceAction",
    "Home",
    "Scene",
    # 异常类
    "MijiaAPIException",
    "AuthenticationError",
    "DeviceError",
    "NetworkError",
    "ValidationError",
]
