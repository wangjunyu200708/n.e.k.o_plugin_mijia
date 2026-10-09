"""资源释放：缓存 / 凭据提供者 / API 客户端三处的 close 语义。

背景：三处此前都有泄漏
  * `CacheManager` 没有 close，注入的 Redis 客户端永不关闭
  * `CredentialProvider` 只在 `__del__` 里关 httpx.Client，而插件 shutdown 与
    logout 都不持有该对象 → 连接挂到 GC 才释放
  * `AsyncMijiaAPI.close` 用 `except Exception: pass` 吞掉所有失败，且不关缓存
这里把「显式关闭」「幂等」「失败不静默」三条钉成断言。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from mijia_api.api_client import AsyncMijiaAPI
from mijia_api.infrastructure.cache_manager import CacheManager


class TestCacheManagerClose:
    def test_closes_injected_redis_client(self, tmp_path):
        redis = MagicMock()
        cache = CacheManager(cache_dir=tmp_path, redis_client=redis)

        cache.close()

        redis.close.assert_called_once()

    def test_is_idempotent(self, tmp_path):
        redis = MagicMock()
        cache = CacheManager(cache_dir=tmp_path, redis_client=redis)

        cache.close()
        cache.close()  # 第二次不应再碰已置空的引用

        assert redis.close.call_count == 1

    def test_no_redis_is_a_noop(self, tmp_path):
        CacheManager(cache_dir=tmp_path).close()  # 不应抛

    def test_redis_close_failure_is_logged_not_raised(self, tmp_path):
        redis = MagicMock()
        redis.close.side_effect = RuntimeError("connection reset")
        cache = CacheManager(cache_dir=tmp_path, redis_client=redis)

        cache.close()  # 不应把异常抛给调用方（shutdown 路径不能被它打断）

        assert cache._redis_client is None


class TestAsyncMijiaAPIClose:
    def _api_with(self, *, http=None, spec=None, cache=None):
        device_service = MagicMock()
        device_service._device_repo = MagicMock()
        device_service._device_repo._http = http
        device_service._spec_repo = spec
        return AsyncMijiaAPI(
            credential=MagicMock(),
            device_service=device_service,
            scene_service=MagicMock(),
            cache_manager=cache,
        )

    async def test_closes_http_and_cache(self):
        http = MagicMock()
        http.close = AsyncMock()
        cache = MagicMock()
        api = self._api_with(http=http, cache=cache)

        await api.close()

        http.close.assert_awaited_once()
        cache.close.assert_called_once()

    async def test_one_failure_does_not_skip_the_rest(self):
        """HTTP 关闭失败时，缓存仍要被关（原实现一处异常就整体跳过）。"""
        http = MagicMock()
        http.close = AsyncMock(side_effect=RuntimeError("boom"))
        cache = MagicMock()
        api = self._api_with(http=http, cache=cache)

        await api.close()  # 不应抛

        cache.close.assert_called_once()

    async def test_missing_cache_manager_is_a_noop(self):
        http = MagicMock()
        http.close = AsyncMock()
        api = self._api_with(http=http, cache=None)

        await api.close()

        http.close.assert_awaited_once()


class TestPluginCloseCredentialProvider:
    def test_close_is_idempotent_and_null_safe(self, plugin_module):
        plugin = plugin_module.MijiaPlugin.__new__(plugin_module.MijiaPlugin)
        plugin.logger = MagicMock()
        provider = MagicMock()
        plugin._credential_provider = provider

        plugin._close_credential_provider()
        plugin._close_credential_provider()  # 第二次应为空操作（引用已置空）

        assert provider.close.call_count == 1
        assert plugin._credential_provider is None

    def test_provider_close_failure_is_swallowed(self, plugin_module):
        """provider.close() 抛错不该让 shutdown 路径崩掉。"""
        plugin = plugin_module.MijiaPlugin.__new__(plugin_module.MijiaPlugin)
        plugin.logger = MagicMock()
        provider = MagicMock()
        provider.close.side_effect = RuntimeError("already closed")
        plugin._credential_provider = provider

        plugin._close_credential_provider()

        assert plugin._credential_provider is None