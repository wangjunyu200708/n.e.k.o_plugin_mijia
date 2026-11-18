"""凭据存储

提供凭据的存储和加载功能。
"""

import getpass
import json
import os
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ..core.logging import get_logger
from ..domain.models import Credential

logger = get_logger(__name__)


class ICredentialStore(ABC):
    """凭据存储接口"""

    @abstractmethod
    def save(self, credential: Credential, path: Optional[str] = None) -> None:
        """保存凭据"""
        pass

    @abstractmethod
    def load(self, path: Optional[str] = None) -> Optional[Credential]:
        """加载凭据"""
        pass

    @abstractmethod
    def delete(self, path: Optional[str] = None) -> None:
        """删除凭据"""
        pass


class FileCredentialStore(ICredentialStore):
    """文件凭据存储

    将凭据以JSON格式存储到文件系统。
    支持从配置文件读取存储路径，默认为项目根目录下的 .mijia/credential.json。
    文件权限设置为0o600（仅所有者可读写）以保护敏感信息。
    """

    def __init__(self, default_path: Optional[Path] = None):
        """初始化文件凭据存储

        Args:
            default_path: 默认存储路径，如果未指定则使用 .mijia/credential.json
        """
        if default_path:
            self._default_path = default_path
        else:
            # 默认使用项目根目录下的 .mijia/credential.json
            # 项目根目录定义为包含 pyproject.toml 的目录
            project_root = self._find_project_root()
            self._default_path = project_root / ".mijia" / "credential.json"
        
        # 确保目录存在
        self._default_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _find_project_root(self) -> Path:
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

    def save(self, credential: Credential, path: Optional[str] = None) -> None:
        """保存凭据到文件

        先在同目录建临时文件、收紧权限后再写入内容，最后原子替换到目标路径。
        不能直接 ``open(target, "w")``：那会先建出一个继承父目录权限的文件
        （Windows 上通常普通用户可读），写完才 chmod/icacls，中间存在明文可读窗口；
        而且中途崩溃会在目标路径留下半截 JSON。

        Args:
            credential: 要保存的凭据对象
            path: 可选的文件路径，如果未指定则使用默认路径

        Raises:
            Exception: 当文件写入失败或权限收紧失败时抛出（不静默降级为明文）
        """
        file_path = self._get_path(path)

        # 确保父目录存在（支持自定义路径如 foo/bar/credential.json）
        file_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path: Optional[Path] = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(file_path.parent), prefix=".credential-", suffix=".tmp"
            )
            os.close(fd)
            tmp_path = Path(tmp_name)

            # 顺序关键：先收紧权限，再写内容
            self._restrict_permissions(tmp_path)

            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(credential.to_dict(), f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, file_path)
            tmp_path = None
            logger.info(f"凭据已保存到: {file_path}")
        except Exception as e:
            logger.error(f"保存凭据失败: {e}")
            raise
        finally:
            # 失败时清掉临时文件，别把明文碎片留在目录里
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(f"清理临时凭据文件失败: {tmp_path}")

    @staticmethod
    def _restrict_permissions(file_path: Path) -> None:
        """把凭据文件收紧到仅当前用户可读写。

        失败直接抛出：宁可保存失败，也不要静默留一个明文可读的凭据。
        """
        if sys.platform == "win32":
            username = getpass.getuser()
            # 不用 text=True：icacls 的输出是本地代码页（中文 Windows 为 GBK），
            # 宿主若开了 PYTHONUTF8，subprocess 解码线程会抛 UnicodeDecodeError。
            result = subprocess.run(
                ["icacls", str(file_path), "/inheritance:r", "/grant:r", f"{username}:F"],
                check=False, capture_output=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
                raise IOError(f"icacls failed: {detail}")
        else:
            file_path.chmod(0o600)

    def load(self, path: Optional[str] = None) -> Optional[Credential]:
        """从文件加载凭据

        Args:
            path: 可选的文件路径，如果未指定则使用默认路径

        Returns:
            加载的凭据对象，如果文件不存在或加载失败则返回None
        """
        file_path = self._get_path(path)

        if not file_path.exists():
            logger.warning(f"凭据文件不存在: {file_path}")
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            credential = Credential.from_dict(data)
            # 脱敏：只显示用户ID前4位
            masked_id = credential.user_id[:4] + "****" if len(credential.user_id) > 4 else "****"
            logger.info(f"凭据已加载，用户ID: {masked_id}")
            return credential
        except Exception as e:
            logger.error(f"加载凭据失败: {e}")
            return None

    def delete(self, path: Optional[str] = None) -> None:
        """删除凭据文件

        Args:
            path: 可选的文件路径，如果未指定则使用默认路径
        """
        file_path = self._get_path(path)

        if file_path.exists():
            file_path.unlink()
            logger.info(f"凭据已删除: {file_path}")

    def _get_path(self, path: Optional[str]) -> Path:
        """获取文件路径

        Args:
            path: 可选的文件路径

        Returns:
            Path对象，如果path为None则返回默认路径
        """
        if path:
            file_path = Path(path)
            # 展开用户目录符号 ~
            if str(file_path).startswith("~"):
                file_path = file_path.expanduser()
            return file_path
        return self._default_path
