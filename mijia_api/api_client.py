"""米家API客户端（异步）

全链路异步：AsyncHttpClient -> 异步仓储层 -> 异步服务层 -> AsyncMijiaAPI。
"""

from typing import Any, Dict, List, Optional

from .core.logging import get_logger
from .domain.models import Credential, Device, Home, Scene
from .services.async_device_service import AsyncDeviceService
from .services.async_scene_service import AsyncSceneService
from .services.async_statistics_service import AsyncStatisticsService

logger = get_logger(__name__)


class _NoOpCache:
    """空操作缓存，当 cache_manager 未注入时使用，所有操作均为无操作。"""

    def invalidate_pattern(self, pattern: str) -> None:
        pass

    def clear(self, namespace: Optional[str] = None) -> None:
        pass

    def get(self, key: str, namespace: str = "default") -> None:
        return None

    def set(self, key: str, value: Any, ttl: int = 300, namespace: str = "default") -> None:
        pass


class AsyncMijiaAPI:
    """米家API客户端（异步版本）

    提供与同步版本相同的接口，但所有方法都是异步的。
    全链路异步实现：AsyncHttpClient -> 异步仓储层 -> 异步服务层 -> AsyncMijiaAPI，
    无 asyncio.to_thread 包装，不阻塞事件循环。
    """

    def __init__(
        self,
        credential: Credential,
        device_service: AsyncDeviceService,
        scene_service: AsyncSceneService,
        statistics_service: Optional[AsyncStatisticsService] = None,
        home_repository: Optional[Any] = None,
        cache_manager: Optional[Any] = None,
    ):
        self._credential = credential
        self._device_service = device_service
        self._scene_service = scene_service
        self._statistics_service = statistics_service
        self._home_repository = home_repository
        self._cache_manager = cache_manager

    @property
    def _safe_cache(self) -> Any:
        """返回缓存管理器，未注入时返回空操作缓存，防止 AttributeError。"""
        return self._cache_manager if self._cache_manager is not None else _NoOpCache()

    async def get_homes(self) -> List[Home]:
        """异步获取家庭列表"""
        if not self._home_repository:
            raise RuntimeError("家庭仓储未初始化，请使用工厂函数创建API客户端")

        return await self._home_repository.get_all(self._credential)

    async def get_devices(self, home_id: str) -> List[Device]:
        """异步获取设备列表"""
        return await self._device_service.get_devices(home_id, self._credential)

    async def get_device(self, device_id: str) -> Optional[Device]:
        """异步获取单个设备"""
        return await self._device_service.get_device_by_id(device_id, self._credential)

    async def control_device(
        self, device_id: str, siid: int, piid: int, value: Any, refresh_cache: bool = True
    ) -> bool:
        """异步控制设备属性

        控制设备后默认会刷新缓存，确保下次获取的是最新状态。
        """
        result = await self._device_service.set_device_property(
            device_id, siid, piid, value, self._credential,
        )

        if result and refresh_cache:
            device = await self._device_service.get_device_by_id(device_id, self._credential)
            if device:
                self._safe_cache.invalidate_pattern(
                    f"{self._credential.user_id}:devices:{device.home_id}"
                )
            else:
                self._safe_cache.invalidate_pattern(
                    f"{self._credential.user_id}:devices:*"
                )

        return result

    async def call_device_action(
        self,
        device_id: str,
        siid: int,
        aiid: int,
        params: Optional[List[Any]] = None,
        refresh_cache: bool = True,
    ) -> Any:
        """异步调用设备操作

        调用设备操作后默认会刷新缓存，确保下次获取的是最新状态。
        """
        result = await self._device_service.call_device_action(
            device_id, siid, aiid, params or [], self._credential,
        )

        if refresh_cache:
            device = await self._device_service.get_device_by_id(device_id, self._credential)
            if device:
                self._safe_cache.invalidate_pattern(
                    f"{self._credential.user_id}:devices:{device.home_id}"
                )
            else:
                self._safe_cache.invalidate_pattern(
                    f"{self._credential.user_id}:devices:*"
                )

        return result

    async def batch_control_devices(
        self, requests: List[Dict[str, Any]], refresh_cache: bool = True
    ) -> List[Dict[str, Any]]:
        """异步批量控制设备

        批量控制设备后默认会刷新缓存，确保下次获取的是最新状态。
        """
        results = await self._device_service.batch_control_devices(requests, self._credential)

        if refresh_cache:
            home_ids = set()
            for request in requests:
                device_id = request.get("device_id")
                if device_id:
                    device = await self._device_service.get_device_by_id(device_id, self._credential)
                    if device:
                        home_ids.add(device.home_id)

            if home_ids:
                for home_id in home_ids:
                    self._safe_cache.invalidate_pattern(
                        f"{self._credential.user_id}:devices:{home_id}"
                    )
            else:
                self._safe_cache.invalidate_pattern(
                    f"{self._credential.user_id}:devices:*"
                )

        return results

    async def get_scenes(self, home_id: str) -> List[Scene]:
        """异步获取智能列表"""
        return await self._scene_service.get_scenes(home_id, self._credential)

    async def execute_scene(self, scene_id: str, home_id: str) -> bool:
        """异步执行智能"""
        result = await self._scene_service.execute_scene(scene_id, home_id, self._credential)
        self._safe_cache.invalidate_pattern(f"{self._credential.user_id}:devices:{home_id}")
        return result

    async def get_device_statistics(self, home_id: str) -> Dict[str, Any]:
        """异步获取设备统计信息"""
        if not self._statistics_service:
            raise RuntimeError("统计服务未初始化")

        return await self._statistics_service.get_device_statistics(home_id, self._credential)

    async def get_device_spec(self, model: str) -> Optional[Any]:
        """异步获取设备规格"""
        if not model:
            return None

        from .domain.exceptions import MijiaAPIException, SpecNotFoundError

        try:
            return await self._device_service.get_device_spec(model)
        except SpecNotFoundError:
            return None
        except MijiaAPIException:
            raise
        except Exception as e:
            raise MijiaAPIException(f"获取设备规格失败: {e}") from e

    async def get_device_properties(self, requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """异步批量获取设备属性"""
        return await self._device_service.batch_get_properties(requests, self._credential)

    def update_credential(self, credential: Credential) -> None:
        """更新凭据"""
        self._credential = credential

    @property
    def credential(self) -> Credential:
        """获取当前使用的凭据"""
        return self._credential

    async def refresh_cache(self, home_id: Optional[str] = None) -> None:
        """异步刷新缓存

        主动刷新缓存，强制从API重新获取数据。
        """
        if not self._cache_manager:
            return

        if home_id:
            self._safe_cache.invalidate_pattern(f"{self._credential.user_id}:devices:{home_id}")
            self._safe_cache.invalidate_pattern(f"{self._credential.user_id}:scenes:{home_id}")
        else:
            self._safe_cache.clear(namespace=self._credential.user_id)

    async def clear_all_cache(self) -> None:
        """异步清空所有缓存"""
        self._safe_cache.clear()

    async def close(self) -> None:
        """关闭 API 客户端，释放 HTTP 连接池与缓存资源。

        各仓储共享同一个 AsyncHttpClient 实例，关一次即可覆盖；这里仍逐个尝试
        并记录失败——「谁持有共享客户端」是隐含约定，哪天被改掉时不该静默泄漏。
        """
        repo = getattr(self._device_service, "_device_repo", None)
        http_client = getattr(repo, "_http", None)
        if http_client is not None and hasattr(http_client, "close"):
            try:
                await http_client.close()
            except Exception as exc:
                logger.warning(f"关闭设备仓储 HTTP 客户端失败: {exc}")

        spec_repo = getattr(self._device_service, "_spec_repo", None)
        if spec_repo is not None and hasattr(spec_repo, "close"):
            try:
                await spec_repo.close()
            except Exception as exc:
                logger.warning(f"关闭规格仓储失败: {exc}")

        cache_close = getattr(self._cache_manager, "close", None)
        if callable(cache_close):
            try:
                cache_close()
            except Exception as exc:
                logger.warning(f"关闭缓存管理器失败: {exc}")
