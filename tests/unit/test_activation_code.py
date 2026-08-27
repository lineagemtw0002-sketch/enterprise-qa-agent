"""激活码的回归保护。设计见 `docs/account_lifecycle_design.md` §4.1b、风险 R-4。

对应 T-10 ~ T-13。

⚠️ **这四条防护是一整套，少任何一条 `/api/v1/activate` 就从改进变成弱点。**
它是全系统唯一不带鉴权的端点（R-4），爆破、用户名枚举、DoS 都落在这一个端点上。
所以下面每一条都单独钉住其中一条防护，而不是笼统测一遍"能激活"。

**限流是第四条防护，本文件测不了** —— 它属于中间件层，而且全仓目前没有任何
限流基础设施可复用（见设计 §9）。实施时要么引入一个（如 slowapi）要么手写，
无论哪种都要另配测试。**这块现在是空的，交付时必须如实说。**
"""

from __future__ import annotations

import hashlib
import time

import pytest

from src.ragent_backend.activation import (
    DEFAULT_TTL_SECONDS,
    PUBLIC_FAILURE_DETAIL,
    ActivationFailure,
    check_activation,
    generate_activation_code,
    hash_activation_code,
    issue_activation,
    verify_activation_code,
)

NOW = 1_800_000_000.0


class TestCodeIsHighEntropyAndNeverStoredInClear:
    """T-10：只存哈希；以及支撑"用 SHA-256 而不是 bcrypt"的那个前提。"""

    def test_code_has_at_least_128_bits(self):
        """⚠️ **这条钉的是一个前提，不是一个功能。**

        `activation.py` 选 SHA-256 而不是 bcrypt，理由之一是"码是 128 bit
        随机数，爆破在信息论上不可行，不需要靠计算成本去补低熵"。
        如果哪天有人把码改成"6 位数字方便电话里念"，这个理由就塌了，
        SHA-256 立刻变成错误的选择。这条测试会在那一刻变红。
        """
        code = generate_activation_code()
        # token_urlsafe(16) -> 22 个字符，每字符 6 bit，实际熵 128 bit
        assert len(code) >= 22

    def test_codes_do_not_repeat(self):
        assert len({generate_activation_code() for _ in range(2000)}) == 2000

    def test_stored_value_is_not_the_code(self):
        """T-10：库里存的东西不能反推出码。"""
        code = generate_activation_code()
        stored = hash_activation_code(code)
        assert stored != code
        assert code not in stored
        assert stored == hashlib.sha256(code.encode()).hexdigest()

    def test_issue_returns_clear_text_only_once(self):
        """`issue_activation` 是明文唯一的出口 —— 它返回明文是为了展示给管理员，
        库里该存的是第二个返回值。签名把这两者分开，就是为了让"别把明文落库"
        在调用点是显式可见的。"""
        code, stored, expires = issue_activation(NOW)
        assert verify_activation_code(code, stored)
        assert expires == NOW + DEFAULT_TTL_SECONDS


class TestVerificationIsConstantTime:
    def test_uses_compare_digest_not_equality(self):
        """恒定时间比较。

        `==` 会在第一个不同的字符处提前返回，逐字符试探就能把码问出来。
        断言的是实现选择本身 —— 行为上 `==` 和 `compare_digest` 完全等价，
        测不出差别，只能钉住"用的是哪个函数"。

        ⚠️ **必须走 AST，不能用 `"compare_digest" in inspect.getsource(...)`。**
        第一版就是那么写的，然后把实现真的换成 `==` 跑了一遍 —— **测试照样绿**，
        因为 `getsource` 连 docstring 一起返回，而那个函数的 docstring 里正好
        反复出现 `compare_digest` 这个词。子串匹配命中的是注释，不是代码。
        这是本仓库第二次踩同一个形态（见 `test_last_turn_tokens_reset.py`），
        靠"把修复撤掉看红不红"的变异检查才抓出来。
        """
        import ast
        import inspect

        from src.ragent_backend import activation

        fn = ast.parse(
            inspect.getsource(activation.verify_activation_code)
        ).body[0]

        calls = {
            ast.unparse(n.func)
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))
        }
        assert "hmac.compare_digest" in calls, (
            f"verify_activation_code 没有调用 hmac.compare_digest，实际调用了 {calls}"
        )

        eq_compares = [
            ast.unparse(n) for n in ast.walk(fn)
            if isinstance(n, ast.Compare)
            and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in n.ops)
            # `not stored_hash` 这类真值判断不是 Compare，不会误伤；
            # 这里只拦"拿哈希跟哈希比大小"的写法。
            and "hash" in ast.unparse(n).lower()
        ]
        assert not eq_compares, (
            f"用 == 比较哈希会泄露时间信息，改用 hmac.compare_digest：{eq_compares}"
        )

    def test_wrong_code_fails(self):
        code, stored, _ = issue_activation(NOW)
        assert verify_activation_code(code, stored)
        assert not verify_activation_code(code + "x", stored)

    def test_missing_stored_hash_fails_closed(self):
        """没有待激活的码时必须返回 False，不能抛异常也不能放行。"""
        assert not verify_activation_code("anything", None)
        assert not verify_activation_code("anything", "")


class TestSingleUseAndExpiry:
    def base(self, **kw):
        code, stored, expires = issue_activation(NOW)
        args = dict(
            submitted_code=code, stored_hash=stored, expires_at=expires,
            activated_at=None, now=NOW + 60, user_exists=True,
        )
        args.update(kw)
        return args

    def test_happy_path(self):
        assert check_activation(**self.base()).ok

    def test_already_activated_is_rejected(self):
        """T-11：同一个码兑换两次，第二次失败。

        单次使用靠的是 `activated_at` 一旦有值就拒绝 —— 兑换成功后
        调用方必须把它写上，否则这条防护不存在。
        """
        r = check_activation(**self.base(activated_at=NOW + 30))
        assert not r.ok and r.failure is ActivationFailure.ALREADY_ACTIVATED

    def test_expired_is_rejected(self):
        """T-12：过期拒绝。"""
        r = check_activation(**self.base(now=NOW + DEFAULT_TTL_SECONDS + 1))
        assert not r.ok and r.failure is ActivationFailure.EXPIRED

    def test_exactly_at_expiry_is_rejected(self):
        """边界：等于过期时刻算过期。差一秒的方向要定死，不然复现不了。"""
        _, stored, expires = issue_activation(NOW)
        r = check_activation(
            submitted_code="x", stored_hash=stored, expires_at=expires,
            activated_at=None, now=expires,
        )
        assert r.failure is ActivationFailure.EXPIRED

    def test_activated_check_comes_before_expiry_and_code(self):
        """⚠️ **判定顺序有意义。**

        先判"已激活/有没有码"，再判过期，最后才比码。反过来（先比码再看过期）
        会让一个**过期但正确**的码和一个**过期且错误**的码走不同分支，
        时间上可能可分。这条测试固定顺序：一个已激活 + 已过期 + 码错误的请求，
        报出来的必须是最靠前的那个原因。
        """
        r = check_activation(
            submitted_code="wrong", stored_hash=hash_activation_code("right"),
            expires_at=NOW, activated_at=NOW, now=NOW + 99999,
        )
        assert r.failure is ActivationFailure.ALREADY_ACTIVATED

    def test_no_pending_code(self):
        r = check_activation(
            submitted_code="x", stored_hash=None, expires_at=None,
            activated_at=None, now=NOW,
        )
        assert r.failure is ActivationFailure.NO_PENDING_CODE


class TestDoesNotLeakWhetherUserExists:
    """T-13：**这条是这一组里最重要的。**

    `/activate` 是全系统唯一无鉴权端点。如果"用户不存在"和"码不对"返回不同的
    错误，攻击者随便试一个 username，就能从差异里反推出这家企业的员工花名册。
    """

    @pytest.mark.parametrize("kwargs", [
        dict(submitted_code="x", stored_hash=None, expires_at=None,
             activated_at=None, now=NOW, user_exists=False),
        dict(submitted_code="x", stored_hash=hash_activation_code("y"),
             expires_at=NOW + 100, activated_at=None, now=NOW),
        dict(submitted_code="x", stored_hash=hash_activation_code("x"),
             expires_at=NOW, activated_at=None, now=NOW + 1),
        dict(submitted_code="x", stored_hash=None, expires_at=None,
             activated_at=NOW, now=NOW + 1),
    ])
    def test_all_failures_share_one_public_message(self, kwargs):
        r = check_activation(**kwargs)
        assert not r.ok
        assert r.public_detail == PUBLIC_FAILURE_DETAIL

    def test_internal_reasons_stay_distinguishable(self):
        """对外一句话，对内必须还能分辨 —— 否则排障和审计日志就没法写了。
        这条同时防止有人"为了不泄露"把内部原因也一起抹平。"""
        no_user = check_activation(
            submitted_code="x", stored_hash=None, expires_at=None,
            activated_at=None, now=NOW, user_exists=False,
        )
        mismatch = check_activation(
            submitted_code="x", stored_hash=hash_activation_code("y"),
            expires_at=NOW + 100, activated_at=None, now=NOW,
        )
        assert no_user.failure is not mismatch.failure

    def test_success_has_no_public_detail(self):
        code, stored, expires = issue_activation(NOW)
        r = check_activation(
            submitted_code=code, stored_hash=stored, expires_at=expires,
            activated_at=None, now=NOW,
        )
        assert r.ok and r.public_detail is None


class TestTtlGuards:
    def test_non_positive_ttl_is_rejected(self):
        """TTL <= 0 会签发一个出生即过期的码。与其让管理员发现"所有人都激活
        不了"，不如在配置时就报错。"""
        with pytest.raises(ValueError):
            issue_activation(NOW, ttl_seconds=0)

    def test_default_ttl_is_seven_days(self):
        """够长到覆盖"周五导入、员工周一才看到"，够短到让泄露的清单很快作废。"""
        assert DEFAULT_TTL_SECONDS == 7 * 24 * 3600
