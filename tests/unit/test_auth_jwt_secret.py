"""`resolve_jwt_secret` 的策略测试——2026-08-24 代码审计 P0-2 的回归保护。

原始缺陷：`auth.py` 里的 JWT 密钥写成
`os.getenv("RAGENT_JWT_SECRET", "dev-only-insecure-secret-change-me")`，
而全仓（`.env`、`.env.example`、所有文档）从未出现过这个变量名，实际部署
必然使用源码里的公开默认值 —— 任何人都能签发任意 user_id（含 super_admin）
的 token，多租户权限隔离全部失效。

这组测试锁住修复后的策略：只有显式开着 RAGENT_DEBUG=true 时才允许使用内置
开发密钥，其余情况一律拒绝启动。

注意 `resolve_jwt_secret` 的两个参数可注入，因此这里不需要改动进程环境变量，
测试之间没有全局状态污染。
"""

import pytest

from src.ragent_backend.auth import _DEV_FALLBACK_SECRET, resolve_jwt_secret


class TestRejectsInsecureConfiguration:
    """非 debug 模式下，任何"密钥等于公开默认值"的情况都必须拒绝启动。"""

    def test_missing_secret_refuses_to_start(self):
        with pytest.raises(RuntimeError) as exc:
            resolve_jwt_secret(secret="", debug_mode=False)
        assert "RAGENT_JWT_SECRET" in str(exc.value)

    def test_explicit_default_secret_refuses_to_start(self):
        """把默认值显式写进环境变量，不能绕过校验——这正是最可能发生的误用。"""
        with pytest.raises(RuntimeError):
            resolve_jwt_secret(secret=_DEV_FALLBACK_SECRET, debug_mode=False)

    def test_whitespace_only_secret_refuses_to_start(self):
        """`RAGENT_JWT_SECRET="   "` 等价于没设置，不能被当成"已配置"。"""
        with pytest.raises(RuntimeError):
            resolve_jwt_secret(secret="   ", debug_mode=False)

    def test_default_secret_with_surrounding_whitespace_refuses_to_start(self):
        """`.env` 里复制粘贴常带上空格，strip 之后仍是默认值，同样要拦住。"""
        with pytest.raises(RuntimeError):
            resolve_jwt_secret(secret=f"  {_DEV_FALLBACK_SECRET}  ", debug_mode=False)

    def test_error_message_tells_operator_how_to_fix(self):
        """fail-fast 的价值取决于报错能不能让人立刻知道怎么办。"""
        with pytest.raises(RuntimeError) as exc:
            resolve_jwt_secret(secret=None, debug_mode=False)
        message = str(exc.value)
        assert "token_urlsafe" in message, "应给出生成密钥的具体命令"
        assert "RAGENT_DEBUG" in message, "应说明本地开发的放行方式"


class TestAcceptsValidConfiguration:
    def test_real_secret_is_used_as_is(self):
        secret = resolve_jwt_secret(secret="a-real-random-secret", debug_mode=False)
        assert secret == "a-real-random-secret"

    def test_real_secret_is_stripped(self):
        secret = resolve_jwt_secret(secret="  padded-secret  ", debug_mode=False)
        assert secret == "padded-secret"

    def test_real_secret_accepted_in_debug_mode_too(self):
        """开着 debug 也不该悄悄换掉用户显式配置的密钥。"""
        secret = resolve_jwt_secret(secret="a-real-random-secret", debug_mode=True)
        assert secret == "a-real-random-secret"


class TestDebugModeEscapeHatch:
    def test_debug_mode_allows_builtin_dev_secret(self, capsys):
        secret = resolve_jwt_secret(secret="", debug_mode=True)
        assert secret == _DEV_FALLBACK_SECRET

    def test_debug_mode_warns_loudly(self, caplog):
        """放行可以，但必须留下痕迹，否则本地配置被带上生产时无人察觉。

        2026-08-25：告警从 `print` 改为 `logger.warning`（可观测性阶段一），
        断言随之从 stdout 改为日志通道。**级别必须仍是 WARNING** —— 这条
        断言就是防"迁移日志时顺手把安全告警降成 debug/info"。
        """
        import logging

        with caplog.at_level(logging.WARNING):
            resolve_jwt_secret(secret="", debug_mode=True)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("警告" in r.getMessage() for r in warnings)


class TestSecretIsNotPredictable:
    def test_dev_fallback_is_never_returned_without_debug(self):
        """兜底断言：任何非 debug 的调用路径都不该返回那个公开常量。

        这条比上面几条更抽象，但它是这次 P0 的本质——只要这个常量能在生产
        路径上被返回，上层所有权限设计都失效。
        """
        for candidate in ["", "   ", None, _DEV_FALLBACK_SECRET]:
            with pytest.raises(RuntimeError):
                resolve_jwt_secret(secret=candidate, debug_mode=False)
