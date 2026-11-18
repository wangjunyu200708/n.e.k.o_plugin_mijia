"""凭据存储的落盘安全性。

覆盖点（`mijia_api.infrastructure.credential_store`）：
  * 正常读写往返
  * 落盘文件权限仅本人可读写（POSIX）
  * 先收紧权限再写内容：权限设不上时不能留下明文
  * 写入失败不留目标文件、不留临时文件
  * 覆盖写走原子替换，不出现半截 JSON
"""

import json
import stat
import sys
from datetime import datetime, timedelta

import pytest
from mijia_api.domain.models import Credential
from mijia_api.infrastructure.credential_store import FileCredentialStore


def _credential(**overrides) -> Credential:
    payload = {
        "user_id": "u-1",
        "service_token": "tok-secret",
        "ssecurity": "ss-secret",
        "pass_token": "pass-secret",
        "c_user_id": "c-1",
        "device_id": "dev-1",
        "user_agent": "pytest",
        "expires_at": datetime.now() + timedelta(days=1),
    }
    payload.update(overrides)
    return Credential(**payload)


def test_save_load_roundtrip(tmp_path):
    store = FileCredentialStore(default_path=tmp_path / "cred.json")
    store.save(_credential())

    loaded = store.load()

    assert loaded is not None
    assert loaded.user_id == "u-1"
    assert loaded.service_token == "tok-secret"


def test_saved_json_is_readable_dict(tmp_path):
    path = tmp_path / "cred.json"
    FileCredentialStore(default_path=path).save(_credential())

    # 明文 JSON 是有意为之（需要能跨进程读），但字段必须完整可解析
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["service_token"] == "tok-secret"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 用 icacls，不设 POSIX mode")
def test_saved_file_is_owner_only(tmp_path):
    path = tmp_path / "cred.json"
    FileCredentialStore(default_path=path).save(_credential())

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"凭据文件权限应为 0600，实际 {oct(mode)}"


def test_no_temp_files_left_behind(tmp_path):
    FileCredentialStore(default_path=tmp_path / "cred.json").save(_credential())

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_permission_failure_leaves_no_plaintext(tmp_path, monkeypatch):
    """权限收紧失败必须报错，且不能留下可读的明文档。"""
    path = tmp_path / "cred.json"
    store = FileCredentialStore(default_path=path)

    def _boom(_path):
        raise IOError("icacls failed: denied")

    monkeypatch.setattr(store, "_restrict_permissions", _boom)

    with pytest.raises(IOError):
        store.save(_credential())

    assert not path.exists(), "权限收紧失败时不应在目标路径留下凭据"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_write_failure_leaves_no_temp_files(tmp_path, monkeypatch):
    store = FileCredentialStore(default_path=tmp_path / "cred.json")

    def _broken_dump(*args, **kwargs):
        raise ValueError("serialize boom")

    monkeypatch.setattr(
        "mijia_api.infrastructure.credential_store.json.dump", _broken_dump
    )

    with pytest.raises(ValueError):
        store.save(_credential())

    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_overwrite_replaces_atomically(tmp_path):
    path = tmp_path / "cred.json"
    store = FileCredentialStore(default_path=path)
    store.save(_credential(service_token="old"))
    store.save(_credential(service_token="new"))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["service_token"] == "new"
