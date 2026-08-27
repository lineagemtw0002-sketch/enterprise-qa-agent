"""`resolve_cors_origins` 的策略测试——2026-08-26 代码审计 P0 的回归保护。

原始缺陷：`app.py` 的 CORS 中间件写成
`allow_origins=["*"]` + `allow_credentials=True`——允许任意网站携带用户凭证
跨域调用本服务的 API，是明确的错误配置。

这组测试锁住修复后的策略：来源必须来自显式配置的 `RAGENT_ALLOWED_ORIGINS`
（逗号分隔），未配置时只有 `RAGENT_DEBUG=true` 才回退到本地开发常见来源，
生产环境未配置则是空列表（失败可见，不是悄悄放行）。

注意 `resolve_cors_origins` 的两个参数可注入，因此这里不需要改动进程环境变量，
测试之间没有全局状态污染。
"""

from src.ragent_backend.app import resolve_cors_origins


class TestExplicitConfiguration:
    """显式配置了 RAGENT_ALLOWED_ORIGINS 时，直接按配置来，不看 debug 模式。"""

    def test_single_origin(self):
        assert resolve_cors_origins(raw="https://app.acme.com", debug_mode=False) == [
            "https://app.acme.com"
        ]

    def test_multiple_comma_separated_origins(self):
        assert resolve_cors_origins(
            raw="https://a.example.com, https://b.example.com", debug_mode=False
        ) == ["https://a.example.com", "https://b.example.com"]

    def test_strips_whitespace_and_drops_empty_entries(self):
        assert resolve_cors_origins(raw=" https://a.example.com ,, ", debug_mode=False) == [
            "https://a.example.com"
        ]

    def test_explicit_config_wins_even_in_debug_mode(self):
        """配置了就用配置的，不会被 debug 模式的本地回退覆盖掉。"""
        assert resolve_cors_origins(raw="https://a.example.com", debug_mode=True) == [
            "https://a.example.com"
        ]


class TestUnconfiguredFallback:
    """未配置 RAGENT_ALLOWED_ORIGINS 时的行为——这是本次修复最容易被绕过的一环，
    如果在生产环境把空配置悄悄当成允许所有来源，等于换了个方式复现原缺陷。"""

    def test_unconfigured_and_not_debug_returns_empty_list(self):
        """生产环境忘记配置：必须是空列表（浏览器会因 CORS 拦截请求），
        绝不能回退到通配符或本地开发来源。"""
        assert resolve_cors_origins(raw="", debug_mode=False) == []

    def test_unconfigured_and_debug_returns_local_dev_origins(self):
        assert resolve_cors_origins(raw="", debug_mode=True) == [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]

    def test_whitespace_only_config_treated_as_unconfigured(self):
        assert resolve_cors_origins(raw="   ", debug_mode=False) == []

    def test_wildcard_mixed_into_explicit_config_is_filtered_out(self):
        """有人把 `*` 和真实来源混在一起配置——`*` 必须被过滤掉，不是整体拒绝。"""
        assert resolve_cors_origins(raw="https://a.example.com, *", debug_mode=False) == [
            "https://a.example.com"
        ]

    def test_wildcard_only_config_falls_back_like_unconfigured(self):
        assert resolve_cors_origins(raw="*", debug_mode=False) == []
        assert resolve_cors_origins(raw="*", debug_mode=True) == [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]

    def test_never_returns_wildcard(self):
        """无论哪种组合，都不应该出现通配符——这是原缺陷的核心，必须钉死。"""
        for raw in ("", "   ", "https://a.example.com", "*"):
            for debug_mode in (True, False):
                origins = resolve_cors_origins(raw=raw, debug_mode=debug_mode)
                assert "*" not in origins
