"""依赖注入工厂

提供便捷的工厂函数，自动创建和组装所有依赖组件。
"""

import os
from pathlib import Path
from typing import Any, Optional

from .api_client import AsyncMijiaAPI
from .core.config import ConfigManager
from .core.logging import get_logger
from .domain.models import Credential
from .infrastructure.cache_manager import CacheManager
from .infrastructure.credential_provider import CredentialProvider, _mask_user_id
from .infrastructure.credential_store import FileCredentialStore, ICredentialStore
from .infrastructure.crypto_service import CryptoService
from .infrastructure.http_client import AsyncHttpClient
from .repositories.async_device_repository import AsyncDeviceRepositoryImpl
from .repositories.async_device_spec_repository import AsyncDeviceSpecRepositoryImpl
from .repositories.async_home_repository import AsyncHomeRepositoryImpl
from .repositories.async_scene_repository import AsyncSceneRepositoryImpl
from .services.async_device_service import AsyncDeviceService
from .services.async_scene_service import AsyncSceneService
from .services.async_statistics_service import AsyncStatisticsService
from .services.auth_service import AuthService

logger = get_logger(__name__)


def _find_project_root() -> Path:
    """查找项目根目录
    
    从当前文件向上查找，直到找到包含 pyproject.toml 的目录。
    如果找不到，则使用当前工作目录。
    
    Returns:
        项目根目录路径
    """
    current = Path(__file__).resolve()
    
    # 向上查找，最多查找10层
    for _ in range(10):
        current = current.parent
        if (current / "pyproject.toml").exists():
            return current
        # 到达文件系统根目录
        if current.parent == current:
            break
    
    # 如果找不到，使用当前工作目录
    return Path.cwd()


def create_config_manager(config_path: Optional[Path] = None) -> ConfigManager:
    """创建配置管理器

    Args:
        config_path: 配置文件路径（可选）

    Returns:
        配置管理器实例
    """
    if config_path is None:
        # SDK包的配置文件路径（相对于当前文件）
        sdk_config = Path(__file__).parent.parent / "configs" / "mijiaAPI.toml"
        
        # 尝试从默认位置加载配置（优先级从高到低）
        default_paths = [
            Path("configs/mijiaAPI.toml"),  # 项目根目录的configs目录
            Path("config.toml"),  # 项目根目录
            Path.home() / ".mijia" / "config.toml",  # 用户主目录
            sdk_config,  # SDK自带的默认配置（最低优先级）
        ]

        for path in default_paths:
            if path.exists():
                config_path = path
                logger.info(f"使用配置文件: {config_path}")
                break

    return ConfigManager(config_path)


def create_async_api_client(
    credential: Credential,
    config_path: Optional[Path] = None,
    cache_dir: Optional[Path] = None,
    redis_client: Optional[Any] = None,
) -> AsyncMijiaAPI:
    """创建米家API客户端（异步版本）

    自动创建和组装所有异步依赖组件，提供全链路异步API接口。
    全链路异步：AsyncHttpClient -> 异步仓储层 -> 异步服务层 -> AsyncMijiaAPI。

    Args:
        credential: 用户凭据对象
        config_path: 配置文件路径（可选）
        cache_dir: 缓存目录（可选）
        redis_client: Redis客户端（可选）

    Returns:
        配置好的AsyncMijiaAPI实例

    Example:
        >>> import asyncio
        >>>
        >>> async def main():
        ...     credential = load_credential()
        ...     api = create_async_api_client(credential)
        ...     devices = await api.get_devices(home_id="123456")
        ...     print(f"找到 {len(devices)} 个设备")
        >>>
        >>> asyncio.run(main())
    """
    # 1. 创建配置管理器
    config = create_config_manager(config_path)

    # 2. 创建异步基础设施组件
    crypto_service = CryptoService()
    async_http_client = AsyncHttpClient(config, crypto_service)
    cache_manager = CacheManager(cache_dir=cache_dir, redis_client=redis_client)

    # 3. 创建异步仓储层
    async_home_repo = AsyncHomeRepositoryImpl(async_http_client, cache_manager)
    async_device_repo = AsyncDeviceRepositoryImpl(async_http_client, cache_manager)
    async_scene_repo = AsyncSceneRepositoryImpl(async_http_client)
    async_spec_repo = AsyncDeviceSpecRepositoryImpl(async_http_client, cache_manager)

    # 4. 创建异步服务层
    async_device_service = AsyncDeviceService(async_device_repo, async_spec_repo, cache_manager)
    async_scene_service = AsyncSceneService(async_scene_repo)
    async_statistics_service = AsyncStatisticsService(async_device_repo)

    # 5. 创建异步API客户端
    api = AsyncMijiaAPI(
        credential=credential,
        device_service=async_device_service,
        scene_service=async_scene_service,
        statistics_service=async_statistics_service,
        home_repository=async_home_repo,
        cache_manager=cache_manager,
    )

    logger.info(f"异步API客户端创建成功，用户ID: {_mask_user_id(credential.user_id)}")

    return api


def create_auth_service(
    config_path: Optional[Path] = None, credential_store: Optional[ICredentialStore] = None,
    credential_path: Optional[Path] = None,
) -> AuthService:
    """创建认证服务

    用于获取、刷新和管理用户凭据。

    Note:
        默认凭据路径为 ``.mijia/credential.json``（相对于项目根目录），
        与插件入口 ``self.data_path("credential.json")`` 的默认路径不同。
        如需统一，可通过 ``MIJIA_CREDENTIAL_PATH`` 环境变量或
        ``credential_path`` 参数显式指定同一路径。

    Args:
        config_path: 配置文件路径（可选）
        credential_store: 凭据存储实现（可选，默认使用文件存储）

    Returns:
        认证服务实例

    Example:
        >>> # 二维码登录
        >>> auth_service = create_auth_service()
        >>> credential = auth_service.login_by_qrcode()
        >>> auth_service.save_credential(credential)

        >>> # 加载已保存的凭据
        >>> credential = auth_service.load_credential()
        >>> if credential and credential.is_expired():
        ...     credential = auth_service.refresh_credential(credential)
        ...     auth_service.save_credential(credential)
    """
    # 创建配置管理器
    config = create_config_manager(config_path)

    # 创建凭据提供者
    provider = CredentialProvider(config)

    # 创建凭据存储
    if credential_store is None:
        # 凭据路径优先级：参数 > 环境变量 > 配置 > 默认值
        _effective_path = credential_path or (
            os.environ.get("MIJIA_CREDENTIAL_PATH")
            or config.get("CREDENTIAL_PATH")
            or ".mijia/credential.json"
        )
        _cred_path = Path(_effective_path) if isinstance(_effective_path, (str, Path)) else Path(str(_effective_path))

        # 如果是相对路径，相对于项目根目录
        if not _cred_path.is_absolute() and not str(_cred_path).startswith("~"):
            # 查找项目根目录
            project_root = _find_project_root()
            _cred_path = project_root / _cred_path
        # 如果是用户目录路径，展开 ~
        elif str(_cred_path).startswith("~"):
            _cred_path = _cred_path.expanduser()

        credential_store = FileCredentialStore(default_path=_cred_path)

    # 创建认证服务
    auth_service = AuthService(provider, credential_store)

    logger.info("认证服务创建成功")

    return auth_service
