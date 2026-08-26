"""
账号激活码 (Activation Code) — 纯函数层

设计见 `docs/account_lifecycle_design.md` §4.1b。

## 为什么有这个模块

用户已定：**不做邮件、不做短信**（O-1）。凭证分发因此必然是人工的——
管理员从系统里导出，再通过他们自己的渠道发给员工。

既然分发一定是人工的，那就让**被分发的东西尽可能不值钱**。这是本模块唯一的
设计原则。CSV 里不出现密码；导入完成后管理端一次性给出 `username → 激活码`，
员工凭码自行设置密码。码是**单次使用 + 7 天过期**的，泄露的后果因此有界；
而任何形式的"初始密码"都是长期有效的，没登录过的那些一直有效。

## 这里刻意不碰数据库

`check_activation` 接收的是**已经从库里读出来的**哈希与过期时间，返回一个
判定结果，不做任何 IO。这样这段安全逻辑能被直接单测——本仓库
`tests/` 下从没有测试碰过 Postgres（`CLAUDE.md` §7.1 说的"现有结构测不了"），
把判定逻辑留在 async DB 方法里等于它永远没有测试。
参照的是 `auth.py::resolve_jwt_secret` 那个已验证有效的模式。

## 为什么用 SHA-256 而不是 bcrypt

`user_store.py` 的密码走 bcrypt，这里**故意不一样**，两条理由：

1. **bcrypt 慢是它的功能，在这里却是漏洞。** `/api/v1/activate` 是全系统唯一
   不带鉴权的端点，每次调用做一遍 bcrypt（约 100ms 量级）等于给了攻击者一个
   放大倍数很高的 CPU 消耗手段。密码登录端点有同样的问题，但它至少不是
   本次新增的攻击面。
2. **bcrypt 的慢是为了补低熵。** 人选的密码熵很低，必须靠计算成本把爆破拖住；
   而激活码是 `secrets.token_urlsafe(16)` 出来的 **128 bit 随机数**，
   爆破在信息论上就不可行，不需要额外的计算成本去补。

⚠️ **这个理由只在码是高熵随机数时成立。** 如果哪天有人把码改成
"6 位数字方便电话里念"，SHA-256 就立刻变成错的选择，必须同时换回 bcrypt
（并接受它的 DoS 面）——`_CODE_ENTROPY_BYTES` 上有测试钉着这一点。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# 16 字节 = 128 bit 熵，token_urlsafe 编码后是 22 个字符。
# 见模块文档：SHA-256 这个选择依赖于这个熵值，改小它就必须一起改哈希算法。
_CODE_ENTROPY_BYTES = 16

# 默认有效期 7 天。够长到覆盖"周五导入、员工周一才看到"，
# 又够短到让一份泄露的清单很快作废。
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


class ActivationFailure(str, Enum):
    """激活失败的**内部**原因。

    ⚠️ **这个枚举不能出现在 HTTP 响应里。** 它区分了"用户不存在"、"码不对"、
    "已过期"、"已激活"，而对外必须全部塌缩成同一个错误
    （见 `PUBLIC_FAILURE_DETAIL`）——否则 `/activate` 就成了用户名枚举接口：
    攻击者随便试一个 username，从"用户不存在"和"码不对"的差异里就能反推出
    这家企业的员工列表。内部保留区分是为了写审计日志和排障。
    """

    NO_SUCH_USER = "no_such_user"
    NO_PENDING_CODE = "no_pending_code"
    ALREADY_ACTIVATED = "already_activated"
    EXPIRED = "expired"
    CODE_MISMATCH = "code_mismatch"


# 对外只有这一句。四种失败原因共用它，长度、标点、措辞都必须完全一致。
PUBLIC_FAILURE_DETAIL = "激活码无效或已过期，请联系管理员重新获取"


@dataclass(frozen=True)
class ActivationCheck:
    ok: bool
    failure: Optional[ActivationFailure] = None

    @property
    def public_detail(self) -> Optional[str]:
        """给 HTTP 层用的对外文案：失败一律同一句，不泄露具体原因。"""
        return None if self.ok else PUBLIC_FAILURE_DETAIL


def generate_activation_code() -> str:
    """生成一个新的激活码（明文）。

    调用方只应该在**两个时刻**持有明文：生成时（要展示给管理员）和
    兑换时（用户提交上来）。库里只存 `hash_activation_code()` 的结果。
    """
    return secrets.token_urlsafe(_CODE_ENTROPY_BYTES)


def hash_activation_code(code: str) -> str:
    """算存库用的哈希。见模块文档"为什么用 SHA-256 而不是 bcrypt"。

    不加盐是刻意的：盐的作用是让同一个明文在不同行里哈希不同，防的是彩虹表
    和"两个用户密码相同"这类信息泄露。激活码本身就是 128 bit 随机数，
    天然不会重复、也没有彩虹表可查，加盐只增加一列存储和一次读取。
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def verify_activation_code(code: str, stored_hash: Optional[str]) -> bool:
    """恒定时间比较。

    ⚠️ **必须用 `hmac.compare_digest` 而不是 `==`。** 字符串 `==` 会在第一个
    不同的字符处提前返回，逐字符试探就能把码问出来。这里比的是十六进制摘要
    而不是原始码，攻击面小一些，但"能不能从时间差里读出信息"这件事不该靠
    "攻击起来比较麻烦"来兜底。
    """
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_activation_code(code), stored_hash)


def check_activation(
    *,
    submitted_code: str,
    stored_hash: Optional[str],
    expires_at: Optional[float],
    activated_at: Optional[float],
    now: float,
    user_exists: bool = True,
) -> ActivationCheck:
    """把一次激活尝试的全部判定收在这里。

    参数全是**已经从库里读出来的值**，本函数不做 IO（见模块文档）。
    `now` 显式传入而不是 `time.time()`，是为了让过期判定能被确定性地测试。

    ⚠️ **顺序有意义**：先判"有没有待激活的码"，再判过期，最后才比码。
    反过来（先比码再看过期）会让一个**过期但正确**的码和一个**过期且错误**的码
    走不同的分支，时间上可能可分。
    """
    if not user_exists:
        return ActivationCheck(False, ActivationFailure.NO_SUCH_USER)
    if activated_at is not None:
        return ActivationCheck(False, ActivationFailure.ALREADY_ACTIVATED)
    if not stored_hash or expires_at is None:
        return ActivationCheck(False, ActivationFailure.NO_PENDING_CODE)
    if now >= expires_at:
        return ActivationCheck(False, ActivationFailure.EXPIRED)
    if not verify_activation_code(submitted_code, stored_hash):
        return ActivationCheck(False, ActivationFailure.CODE_MISMATCH)
    return ActivationCheck(True)


def issue_activation(now: float, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> tuple[str, str, float]:
    """签发一个激活码，返回 `(明文, 哈希, 过期时间戳)`。

    明文**只**给调用方展示一次，不要落库、不要写日志。
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds 必须为正数")
    code = generate_activation_code()
    return code, hash_activation_code(code), now + ttl_seconds
