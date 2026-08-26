"""BYOC 连接器会话令牌纯函数层的回归保护 —— `docs/aiops_module_design.md`
§10.1。跟 `test_activation_code.py`/`test_aiops_scope.py` 同一个模式：
零 IO，判别力靠正反用例对照。
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from src.ops.connector_session import (
    CONNECTOR_SESSION_TOKEN_TTL_SECONDS,
    HEARTBEAT_STALE_AFTER_SECONDS,
    REFRESH_TOKEN_ENTROPY_BYTES,
    REGISTER_TOKEN_ENTROPY_BYTES,
    ConnectorTokenInvalid,
    RefreshTokenFailure,
    RegisterTokenFailure,
    check_refresh_token,
    check_register_token,
    create_connector_session_jwt,
    decode_connector_session_jwt,
    derive_connector_jwt_secret,
    generate_refresh_token,
    generate_register_token,
    hash_token,
    is_heartbeat_fresh,
)


class TestTokenEntropy:
    """跟 activation.py 的 `_CODE_ENTROPY_BYTES` 测试同一个精神：SHA-256 而
    不是 bcrypt 这个选择依赖于 token 是高熵随机数，这里钉住熵值不会被
    偷偷改小（比如改成"6 位数字"）。"""

    def test_register_token_entropy(self):
        assert REGISTER_TOKEN_ENTROPY_BYTES >= 16  # 至少 128 bit

    def test_refresh_token_entropy(self):
        assert REFRESH_TOKEN_ENTROPY_BYTES >= 16

    def test_generated_tokens_are_url_safe_and_unique(self):
        tokens = {generate_register_token() for _ in range(50)}
        assert len(tokens) == 50  # 没有碰撞
        for t in tokens:
            assert all(c.isalnum() or c in "-_" for c in t)


class TestHashToken:
    def test_same_input_same_hash(self):
        assert hash_token("abc") == hash_token("abc")

    def test_different_input_different_hash(self):
        assert hash_token("abc") != hash_token("abd")

    def test_hash_is_not_reversible_looking(self):
        # 不是真的验证不可逆（那是 SHA-256 的数学性质），只确认没有把明文
        # 编码/拼接进去这种一眼假的"哈希"。
        assert "abc" not in hash_token("abc")


class TestCheckRegisterToken:
    def test_valid_token_passes(self):
        token = generate_register_token()
        result = check_register_token(
            stored_hash=hash_token(token), provided_token=token,
            used=False, expires_at=time.time() + 600,
        )
        assert result.ok is True

    def test_wrong_token_rejected(self):
        result = check_register_token(
            stored_hash=hash_token("real-token"), provided_token="wrong-token",
            used=False, expires_at=time.time() + 600,
        )
        assert result.ok is False
        assert result.failure == RegisterTokenFailure.NOT_FOUND

    def test_no_stored_hash_rejected(self):
        result = check_register_token(
            stored_hash=None, provided_token="anything", used=False, expires_at=None,
        )
        assert result.ok is False
        assert result.failure == RegisterTokenFailure.NOT_FOUND

    def test_already_used_rejected(self):
        token = generate_register_token()
        result = check_register_token(
            stored_hash=hash_token(token), provided_token=token,
            used=True, expires_at=time.time() + 600,
        )
        assert result.ok is False
        assert result.failure == RegisterTokenFailure.ALREADY_USED

    def test_expired_rejected(self):
        token = generate_register_token()
        result = check_register_token(
            stored_hash=hash_token(token), provided_token=token,
            used=False, expires_at=time.time() - 1,
        )
        assert result.ok is False
        assert result.failure == RegisterTokenFailure.EXPIRED

    def test_used_checked_before_expired(self):
        """已用完 + 已过期同时成立时，报"已用过"还是"已过期"不重要（两者
        都指向"这个 token 不能再用"），但顺序要确定，不能因为字典遍历顺序
        之类的偶然因素导致同一输入不同次跑出不同 failure。"""
        token = generate_register_token()
        result = check_register_token(
            stored_hash=hash_token(token), provided_token=token,
            used=True, expires_at=time.time() - 1,
        )
        assert result.failure == RegisterTokenFailure.ALREADY_USED


class TestCheckRefreshToken:
    def test_valid_token_passes(self):
        token = generate_refresh_token()
        result = check_refresh_token(
            stored_hash=hash_token(token), provided_token=token,
            consumed_at=None, expires_at=time.time() + 86400,
        )
        assert result.ok is True
        assert result.is_replay is False

    def test_wrong_token_rejected(self):
        result = check_refresh_token(
            stored_hash=hash_token("real"), provided_token="wrong",
            consumed_at=None, expires_at=time.time() + 86400,
        )
        assert result.ok is False
        assert result.failure == RefreshTokenFailure.NOT_FOUND
        assert result.is_replay is False

    def test_consumed_token_is_replay_signal(self):
        """核心安全用例：一个已经被消费过的 refresh_token 再次出现，必须
        标成 is_replay=True，调用方据此触发"撤销全部会话、强制重新注册"，
        不是简单地拒绝这一次请求就完事。"""
        token = generate_refresh_token()
        result = check_refresh_token(
            stored_hash=hash_token(token), provided_token=token,
            consumed_at=time.time() - 10, expires_at=time.time() + 86400,
        )
        assert result.ok is False
        assert result.failure == RefreshTokenFailure.REPLAYED
        assert result.is_replay is True

    def test_expired_but_not_consumed_is_not_replay(self):
        """过期和重放是两件不同的事——过期只是"晚了"，不代表 token 泄露，
        不应该触发跟重放同样的"撤销全部会话"这种重反应。"""
        token = generate_refresh_token()
        result = check_refresh_token(
            stored_hash=hash_token(token), provided_token=token,
            consumed_at=None, expires_at=time.time() - 1,
        )
        assert result.ok is False
        assert result.failure == RefreshTokenFailure.EXPIRED
        assert result.is_replay is False

    def test_replay_takes_priority_over_expired(self):
        """已消费 + 已过期同时成立：必须报 replay，不能被过期检查先拦下来
        掩盖掉——重放是安全事件，过期只是正常业务状态，绝不能让"反正都要
        拒绝"这种想法把两者的处理路径合并。"""
        token = generate_refresh_token()
        result = check_refresh_token(
            stored_hash=hash_token(token), provided_token=token,
            consumed_at=time.time() - 100000, expires_at=time.time() - 1,
        )
        assert result.failure == RefreshTokenFailure.REPLAYED
        assert result.is_replay is True


class TestConnectorSessionJwt:
    def test_roundtrip(self):
        secret = derive_connector_jwt_secret("main-secret")
        token = create_connector_session_jwt("opsconn_1", "org-1", secret)
        payload = decode_connector_session_jwt(token, secret)
        assert payload["connection_id"] == "opsconn_1"
        assert payload["org_id"] == "org-1"

    def test_expired_token_rejected(self):
        secret = derive_connector_jwt_secret("main-secret")
        token = create_connector_session_jwt(
            "opsconn_1", "org-1", secret, ttl_seconds=1, now=time.time() - 10,
        )
        with pytest.raises(ConnectorTokenInvalid):
            decode_connector_session_jwt(token, secret)

    def test_default_ttl_is_one_hour(self):
        assert CONNECTOR_SESSION_TOKEN_TTL_SECONDS == 3600

    def test_derived_secret_differs_from_base(self):
        base = "main-secret"
        assert derive_connector_jwt_secret(base) != base

    def test_user_token_cannot_be_decoded_as_connector_token(self):
        """核心安全用例：这是整个"派生独立密钥"设计要防的那件事——一个
        正常用户登录 JWT（用主密钥、没有 typ=ops_connector_session 字段）
        绝不能被当成连接器 session token 通过校验。"""
        main_secret = "main-secret"
        # 模拟 auth.py::create_access_token 签出来的用户 token
        fake_user_token = pyjwt.encode(
            {"sub": "user-1", "username": "alice", "iat": int(time.time()),
             "exp": int(time.time()) + 3600},
            main_secret, algorithm="HS256",
        )
        connector_secret = derive_connector_jwt_secret(main_secret)
        with pytest.raises(ConnectorTokenInvalid):
            decode_connector_session_jwt(fake_user_token, connector_secret)

    def test_connector_token_cannot_be_decoded_as_user_token(self):
        """反过来也要成立：连接器 token 拿去当用户 token 用，用主密钥校验
        签名必须失败——签名算法层面就该挡住，不是靠业务逻辑检查字段。"""
        main_secret = "main-secret"
        connector_secret = derive_connector_jwt_secret(main_secret)
        connector_token = create_connector_session_jwt("opsconn_1", "org-1", connector_secret)
        with pytest.raises(pyjwt.InvalidTokenError):
            pyjwt.decode(connector_token, main_secret, algorithms=["HS256"])

    def test_wrong_typ_rejected_even_with_right_secret(self):
        """防御性第二道：就算不知怎么用对了派生密钥签了一个 token，
        typ 字段不对也要拒绝——不能只靠"签名验证通过=一定是连接器 token"
        这一个假设。"""
        secret = derive_connector_jwt_secret("main-secret")
        wrong_typ_token = pyjwt.encode(
            {"typ": "something_else", "connection_id": "opsconn_1", "org_id": "org-1",
             "iat": int(time.time()), "exp": int(time.time()) + 3600},
            secret, algorithm="HS256",
        )
        with pytest.raises(ConnectorTokenInvalid):
            decode_connector_session_jwt(wrong_typ_token, secret)


class TestHeartbeatFreshness:
    def test_none_is_not_fresh(self):
        assert is_heartbeat_fresh(None) is False

    def test_recent_heartbeat_is_fresh(self):
        now = time.time()
        assert is_heartbeat_fresh(now - 5, now=now) is True

    def test_stale_heartbeat_is_not_fresh(self):
        now = time.time()
        assert is_heartbeat_fresh(now - HEARTBEAT_STALE_AFTER_SECONDS - 1, now=now) is False

    def test_boundary_at_stale_threshold_is_fresh(self):
        now = time.time()
        assert is_heartbeat_fresh(now - HEARTBEAT_STALE_AFTER_SECONDS, now=now) is True
