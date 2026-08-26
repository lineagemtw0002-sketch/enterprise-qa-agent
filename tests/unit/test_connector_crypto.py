"""
`connector_crypto.py` 单测 —— P0 修复：租户连接器凭证不再明文落库 (2026-08-26)。

背景：`tenant_connectors.auth_config` 原来是 `json.dumps(auth_config)` 直接写进
JSONB 列，数据库一旦泄露，所有企业接入自己知识库/考勤系统用的第三方 API token
跟着泄露。这个文件只测纯函数（密钥解析 + 加解密），不碰数据库；
`TenantConnectorStore` 集成到读写路径的部分见
`test_tenant_connector_store_encryption.py`。

判别力检查（CLAUDE.md §7.2 要求"写完测试问：它在旧实现下会失败吗？"）：
`TestEncryptDecryptRoundtrip::test_ciphertext_does_not_contain_plaintext_secret`
和 `test_encrypted_wrapper_is_not_the_original_dict` 两条在"旧实现"
（`json.dumps(auth_config)` 直接落库，没有 encrypt 这一步）下必然失败——旧实现里
"落库的内容"就是明文本身，一定包含明文子串、一定等于原始 dict。
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.fernet import Fernet

from src.ragent_backend.connector_crypto import (
    DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY,
    build_fernet,
    decrypt_auth_config,
    encrypt_auth_config,
    is_encrypted,
    resolve_connector_encryption_key,
)

_REAL_KEY = Fernet.generate_key().decode()


class TestResolveConnectorEncryptionKeyFailFast:
    """照抄 auth.py::resolve_jwt_secret 的 fail-fast 惯例：密钥缺失/不安全时，
    非调试模式一律拒绝（这里体现为 RuntimeError），调试模式下允许回退到一个
    固定的、可识别的不安全默认值。"""

    def test_missing_key_raises_without_debug(self):
        with pytest.raises(RuntimeError, match="RAGENT_CONNECTOR_ENCRYPTION_KEY 未设置"):
            resolve_connector_encryption_key(key=None, debug=False)

    def test_missing_key_falls_back_to_insecure_default_in_debug_mode(self):
        key = resolve_connector_encryption_key(key=None, debug=True)
        assert key == DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY.encode()

    def test_default_insecure_key_rejected_outside_debug(self):
        """即便调用方显式传了这个默认值（比如从一个从未被真正配置过的
        .env 文件里读出来的），只要不在调试模式下，也必须拒绝——不能因为
        "字符串格式合法"就放行内置默认值。"""
        with pytest.raises(RuntimeError, match="不安全默认开发密钥"):
            resolve_connector_encryption_key(key=DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY, debug=False)

    def test_default_insecure_key_allowed_in_debug(self):
        key = resolve_connector_encryption_key(key=DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY, debug=True)
        assert key == DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY.encode()

    def test_invalid_key_format_raises_even_in_debug(self):
        """格式不合法（不是 32 字节 url-safe base64）时，就算在调试模式下也不能
        放行——调试模式只豁免"用内置默认值"，不豁免"随便一个字符串也能当密钥"。"""
        with pytest.raises(RuntimeError, match="不是合法的 Fernet 密钥"):
            resolve_connector_encryption_key(key="not-a-valid-fernet-key", debug=True)

    def test_invalid_key_format_raises_outside_debug(self):
        with pytest.raises(RuntimeError, match="不是合法的 Fernet 密钥"):
            resolve_connector_encryption_key(key="not-a-valid-fernet-key", debug=False)

    def test_valid_real_key_accepted(self):
        key = resolve_connector_encryption_key(key=_REAL_KEY, debug=False)
        assert key == _REAL_KEY.encode()
        # 能被 Fernet 正常构造，不抛异常
        Fernet(key)

    def test_key_param_takes_precedence_over_env(self, monkeypatch):
        """参数可注入是为了单测不用碰 os.environ——这里验证显式传参确实优先于
        环境变量，而不是被环境变量覆盖掉。"""
        monkeypatch.setenv("RAGENT_CONNECTOR_ENCRYPTION_KEY", DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY)
        key = resolve_connector_encryption_key(key=_REAL_KEY, debug=False)
        assert key == _REAL_KEY.encode()

    def test_reads_from_env_when_key_param_omitted(self, monkeypatch):
        monkeypatch.setenv("RAGENT_CONNECTOR_ENCRYPTION_KEY", _REAL_KEY)
        monkeypatch.delenv("RAGENT_DEBUG", raising=False)
        key = resolve_connector_encryption_key()
        assert key == _REAL_KEY.encode()

    def test_reads_debug_flag_from_env_when_omitted(self, monkeypatch):
        monkeypatch.delenv("RAGENT_CONNECTOR_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("RAGENT_DEBUG", "true")
        key = resolve_connector_encryption_key()
        assert key == DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY.encode()

    def test_debug_flag_case_insensitive_and_defaults_false(self, monkeypatch):
        monkeypatch.delenv("RAGENT_CONNECTOR_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("RAGENT_DEBUG", "TRUE")
        assert resolve_connector_encryption_key() == DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY.encode()

        monkeypatch.delenv("RAGENT_DEBUG", raising=False)
        with pytest.raises(RuntimeError):
            resolve_connector_encryption_key()

    def test_build_fernet_returns_usable_fernet(self):
        fernet = build_fernet(key=_REAL_KEY, debug=False)
        token = fernet.encrypt(b"hello")
        assert fernet.decrypt(token) == b"hello"


class TestDefaultInsecureKeyIsWellFormed:
    def test_default_key_is_valid_fernet_key(self):
        # 不能只是"看起来像"，必须真的能被 Fernet 接受——否则调试模式下第一次
        # 加密就会崩，而不是等到生产环境才暴露问题。
        Fernet(DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY.encode())

    def test_default_key_is_deterministic(self):
        """必须是固定值而不是随机生成——resolve 函数要能精确识别"这就是那个内置
        默认值"并拒绝在生产环境使用，如果每次都随机生成，这条判断就失效了。
        直接从同一个固定 seed 重新推导一遍，验证跟模块里的常量完全一致。"""
        seed = b"dev-only-insecure-connector-key!"
        assert len(seed) == 32
        recomputed = base64.urlsafe_b64encode(seed).decode()
        assert recomputed == DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY


class TestEncryptDecryptRoundtrip:
    def setup_method(self):
        self.fernet = Fernet(_REAL_KEY.encode())

    def test_roundtrip_preserves_dict(self):
        original = {"token": "sk-super-secret-12345"}
        stored = encrypt_auth_config(original, self.fernet)
        restored = decrypt_auth_config(stored, self.fernet)
        assert restored == original

    def test_roundtrip_empty_dict(self):
        stored = encrypt_auth_config({}, self.fernet)
        assert decrypt_auth_config(stored, self.fernet) == {}

    def test_roundtrip_none_treated_as_empty_dict(self):
        stored = encrypt_auth_config(None, self.fernet)
        assert decrypt_auth_config(stored, self.fernet) == {}

    def test_roundtrip_nested_and_unicode_values(self):
        original = {"token": "秘密令牌-😀", "extra": {"scope": ["read", "write"]}}
        stored = encrypt_auth_config(original, self.fernet)
        assert decrypt_auth_config(stored, self.fernet) == original

    def test_encrypted_wrapper_is_not_the_original_dict(self):
        """判别力检查：旧实现落库的就是原始 dict 本身，这条在旧实现下会失败。"""
        original = {"token": "sk-super-secret-12345"}
        stored = encrypt_auth_config(original, self.fernet)
        assert stored != original

    def test_ciphertext_does_not_contain_plaintext_secret(self):
        """判别力检查：加密后的密文字符串不应该包含明文 token 的可见子串。
        旧实现（直接 json.dumps 落库）下，明文子串必然完整出现在落库内容里，
        这条断言在旧实现下会失败。"""
        secret = "sk-super-secret-12345"
        original = {"token": secret}
        stored = encrypt_auth_config(original, self.fernet)
        serialized = json.dumps(stored)
        assert secret not in serialized

    def test_stored_wrapper_is_jsonb_safe(self):
        """确保加密结果能被 json.dumps 序列化后原样存进 JSONB 列（纯 ASCII token）。"""
        stored = encrypt_auth_config({"token": "abc"}, self.fernet)
        serialized = json.dumps(stored)
        assert json.loads(serialized) == stored
        token = stored["__enc__"]
        assert token.encode("ascii")  # 不抛异常即说明是纯 ASCII

    def test_wrong_key_cannot_decrypt(self):
        other_fernet = Fernet(Fernet.generate_key())
        stored = encrypt_auth_config({"token": "abc"}, self.fernet)
        with pytest.raises(ValueError, match="无法用当前"):
            decrypt_auth_config(stored, other_fernet)


class TestLegacyPlaintextCompatibility:
    """迁移脚本跑之前，数据库里的存量行就是纯明文 dict（没有 `__enc__` 这个哨兵
    key）。读路径必须原样兼容它们，不能因为"看起来不是密文格式"就报错或丢数据——
    这是让"先部署代码、后跑迁移脚本"这个正常部署顺序不中断服务的关键。"""

    def setup_method(self):
        self.fernet = Fernet(_REAL_KEY.encode())

    def test_decrypt_returns_legacy_plaintext_unchanged(self):
        legacy = {"token": "old-plaintext-token"}
        assert decrypt_auth_config(legacy, self.fernet) == legacy

    def test_decrypt_empty_legacy_dict(self):
        assert decrypt_auth_config({}, self.fernet) == {}

    def test_decrypt_non_dict_returns_empty_dict(self):
        assert decrypt_auth_config(None, self.fernet) == {}
        assert decrypt_auth_config("not-a-dict", self.fernet) == {}

    def test_is_encrypted_true_for_wrapper(self):
        stored = encrypt_auth_config({"token": "abc"}, self.fernet)
        assert is_encrypted(stored) is True

    def test_is_encrypted_false_for_legacy_plaintext(self):
        assert is_encrypted({"token": "abc"}) is False
        assert is_encrypted({}) is False

    def test_is_encrypted_false_when_marker_key_not_alone(self):
        """哨兵 key 必须是 dict 里唯一的 key 才判定为密文——如果某个明文
        auth_config 恰好也有一个叫 __enc__ 的字段（理论上不会，但防御性地测一下
        边界），不应该被误判成密文从而尝试解密失败。"""
        weird_plaintext = {"__enc__": "not-a-real-token", "token": "abc"}
        assert is_encrypted(weird_plaintext) is False
        assert decrypt_auth_config(weird_plaintext, self.fernet) == weird_plaintext
