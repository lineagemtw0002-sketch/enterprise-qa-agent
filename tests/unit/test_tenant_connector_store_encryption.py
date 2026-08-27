"""
`TenantConnectorStore` 加解密集成单测 —— P0 修复 (2026-08-26)。

只测"加解密有没有正确接进读写路径"，不碰真实 PostgreSQL——本仓库的
`RAGENT_POSTGRES_URL` 默认指向一个跟其它开发/测试会话共用的本地库
（`conftest.py` 本来就没有 DB fixture），直接连它跑测试既跑不出隔离性，也有
弄脏共享开发数据的风险。所以这里用最小的手工 fake 替身接住 `asyncpg` 的
`Pool.acquire()` / `Connection.fetchrow()` / `.execute()`，只验证：

1. 构造 `TenantConnectorStore` 时，密钥缺失/不安全会 fail-fast（不需要真的
   连数据库就能验证，因为密钥解析发生在 `__init__`，比建连接池早）。
2. `upsert()` 传给 SQL 的 `auth_config` 参数是加密后的密文包装，不是明文
   `json.dumps(auth_config)`——这是本次修复的核心断言，旧实现下必然失败。
3. `_row_to_connector` 正确解密新格式，且向后兼容读到旧格式的明文行
   （迁移脚本跑之前，数据库里就是这个形状）。
4. `get()` 端到端（经过 mock 的 pool）也能正确解密。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from src.ragent_backend.connector_crypto import (
    DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY,
    decrypt_auth_config,
    encrypt_auth_config,
)
from src.ragent_backend.tenant_connector_store import TenantConnectorStore

_REAL_KEY = Fernet.generate_key().decode()

_ALL_COLUMNS = (
    "id",
    "org_id",
    "capability",
    "connector_type",
    "endpoint",
    "auth_config",
    "remote_tool_name",
    "field_mapping",
    "is_active",
    "created_at",
    "call_count",
    "failure_count",
    "last_called_at",
    "last_latency_ms",
    "last_error",
)


def _fake_row(auth_config_json: str) -> Dict[str, Any]:
    """伪造一行 `SELECT ... FROM tenant_connectors`——`_row_to_connector` 只用
    `row[col]` 取值，普通 dict 完全满足接口，不需要真的 asyncpg.Record。"""
    return {
        "id": "conn-1",
        "org_id": "org-1",
        "capability": "knowledge_base",
        "connector_type": "http_api",
        "endpoint": "https://example.com",
        "auth_config": auth_config_json,
        "remote_tool_name": None,
        "field_mapping": "{}",
        "is_active": True,
        "created_at": 0.0,
        "call_count": 0,
        "failure_count": 0,
        "last_called_at": None,
        "last_latency_ms": None,
        "last_error": None,
    }


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """只支持 `.acquire()` 的最小 fake，测试用例按需给 `conn` 打桩。"""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


class TestConstructionFailFast:
    """密钥解析在 __init__ 里发生，早于任何真正的数据库连接（`_get_pool` 是懒
    连接），所以这里不需要 mock 数据库就能验证 fail-fast。"""

    def test_missing_key_raises_without_debug(self, monkeypatch):
        monkeypatch.delenv("RAGENT_CONNECTOR_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("RAGENT_DEBUG", raising=False)
        with pytest.raises(RuntimeError, match="RAGENT_CONNECTOR_ENCRYPTION_KEY 未设置"):
            TenantConnectorStore()

    def test_default_insecure_key_rejected(self, monkeypatch):
        monkeypatch.delenv("RAGENT_DEBUG", raising=False)
        with pytest.raises(RuntimeError, match="不安全默认开发密钥"):
            TenantConnectorStore(encryption_key=DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY)

    def test_valid_injected_key_succeeds(self):
        store = TenantConnectorStore(encryption_key=_REAL_KEY)
        assert store is not None


class TestUpsertEncryptsBeforeWriting:
    """核心断言：写库的 auth_config 参数必须是密文，不是调用方传入的明文
    dict 序列化结果。旧实现是 `json.dumps(auth_config)` 直接落库，这组测试
    在旧实现下会失败。"""

    @pytest.mark.asyncio
    async def test_upsert_sql_param_is_not_plaintext_json(self, monkeypatch):
        store = TenantConnectorStore(encryption_key=_REAL_KEY)

        secret_token = "sk-super-secret-should-never-be-stored-in-clear"
        plaintext_auth_config = {"token": secret_token}

        # RETURNING 子句会把刚写入的行读回来，upsert() 会拿它再跑一遍
        # _row_to_connector（含解密）——这里用一个用同一把密钥加密过的占位值，
        # 让"RETURNING 行"本身能正常解密，测试关注点在于 fetchrow 收到的
        # *调用参数*，不是这个返回值。
        fake_conn = AsyncMock()
        fake_conn.fetchrow = AsyncMock(
            return_value=_fake_row(json.dumps(encrypt_auth_config({}, store._fernet)))
        )
        fake_pool = _FakePool(fake_conn)
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=fake_pool))

        await store.upsert(
            org_id="org-1",
            capability="knowledge_base",
            connector_type="http_api",
            endpoint="https://example.com",
            auth_config=plaintext_auth_config,
        )

        assert fake_conn.fetchrow.await_count == 1
        _, positional_args, _ = fake_conn.fetchrow.mock_calls[0]
        sql_text = positional_args[0]
        params = positional_args[1:]
        assert "INSERT INTO tenant_connectors" in sql_text

        # 第 6 个 SQL 参数（$6）就是 auth_config —— 位置对应
        # upsert() 里 conn.fetchrow(sql, connector_id, org_id, capability,
        # connector_type, endpoint, auth_config_json, ...) 的实参顺序。
        auth_config_param = params[5]

        # 判别力核心：不能是明文 json.dumps 的结果，也不能在序列化后的字符串里
        # 直接看到明文 token。
        assert auth_config_param != json.dumps(plaintext_auth_config)
        assert secret_token not in auth_config_param

        # 必须是本模块定义的密文包装格式，且能用同一把密钥解密还原。
        stored_dict = json.loads(auth_config_param)
        assert set(stored_dict.keys()) == {"__enc__"}
        restored = decrypt_auth_config(stored_dict, store._fernet)
        assert restored == plaintext_auth_config

    @pytest.mark.asyncio
    async def test_upsert_empty_auth_config_still_encrypted_wrapper(self, monkeypatch):
        store = TenantConnectorStore(encryption_key=_REAL_KEY)
        fake_conn = AsyncMock()
        fake_conn.fetchrow = AsyncMock(
            return_value=_fake_row(json.dumps(encrypt_auth_config({}, store._fernet)))
        )
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        await store.upsert(
            org_id="org-1",
            capability="attendance",
            connector_type="internal_postgres",
        )

        params = fake_conn.fetchrow.mock_calls[0][1]
        auth_config_param = params[6]  # 注意：这里 params 含 sql 本身在 [0]
        stored_dict = json.loads(auth_config_param)
        assert decrypt_auth_config(stored_dict, store._fernet) == {}


class TestRowToConnectorDecryption:
    """`_row_to_connector` 是纯函数式的行转换逻辑，不需要真的数据库连接。"""

    def test_decrypts_new_format_row(self):
        store = TenantConnectorStore(encryption_key=_REAL_KEY)
        original = {"token": "abc-123"}
        encrypted = encrypt_auth_config(original, store._fernet)
        row = _fake_row(json.dumps(encrypted))

        connector = store._row_to_connector(row)

        assert connector.auth_config == original
        assert connector.connector_id == "conn-1"
        assert connector.org_id == "org-1"

    def test_backward_compatible_with_legacy_plaintext_row(self):
        """迁移脚本跑之前，数据库里存量行的 auth_config 就是明文 dict——
        读路径必须原样兼容，不能抛异常或返回空。"""
        store = TenantConnectorStore(encryption_key=_REAL_KEY)
        legacy_plaintext = {"token": "old-plaintext-token"}
        row = _fake_row(json.dumps(legacy_plaintext))

        connector = store._row_to_connector(row)

        assert connector.auth_config == legacy_plaintext

    def test_legacy_empty_auth_config_row(self):
        store = TenantConnectorStore(encryption_key=_REAL_KEY)
        row = _fake_row(json.dumps({}))
        connector = store._row_to_connector(row)
        assert connector.auth_config == {}


class TestGetEndToEndWithMockedPool:
    @pytest.mark.asyncio
    async def test_get_decrypts_stored_connector(self, monkeypatch):
        store = TenantConnectorStore(encryption_key=_REAL_KEY)
        original = {"token": "abc-123"}
        encrypted = encrypt_auth_config(original, store._fernet)
        row = _fake_row(json.dumps(encrypted))

        fake_conn = AsyncMock()
        fake_conn.fetchrow = AsyncMock(return_value=row)
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        connector = await store.get("org-1", "knowledge_base")

        assert connector is not None
        assert connector.auth_config == original

    @pytest.mark.asyncio
    async def test_get_returns_none_when_no_row(self, monkeypatch):
        store = TenantConnectorStore(encryption_key=_REAL_KEY)
        fake_conn = AsyncMock()
        fake_conn.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        connector = await store.get("org-1", "knowledge_base")
        assert connector is None
