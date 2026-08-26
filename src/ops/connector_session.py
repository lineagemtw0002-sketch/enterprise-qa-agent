"""
BYOC 连接器会话令牌 —— 纯函数层

设计见 `docs/aiops_module_design.md` §10.1。跟 `activation.py`/
`auth.py::resolve_jwt_secret` 同一个模式：判定函数只接收"已经从库里读出来的"
数据，不做任何 IO，保证这段安全逻辑能被直接单测——本仓库 `tests/` 从没有
测试碰过 Postgres（`conftest.py` 无 DB fixture），把判定逻辑写进 async 端点
函数就等于永远没有测试。

## 三层令牌，各自的生命周期不同

1. **`register_token`**（一次性，10 分钟有效）—— org 管理员在连接器管理页
   生成，给客户环境的连接器进程用来发起第一次握手。跟 `activation.py` 的
   激活码同一个理由：高熵随机值不需要 bcrypt，平台只存哈希不存明文。
2. **`connector_session_token`**（JWT，TTL 1 小时）—— 握手成功后签发，
   WebSocket 连接期间用它证明身份。**故意用跟用户登录 token 不同的派生密钥
   签名**（见 `derive_connector_jwt_secret`），不是同一份密钥换个 claim 那么
   简单——原因见该函数的说明。
3. **`refresh_token`**（TTL 30 天，单次使用即轮换）—— 心跳期间用来换新的
   `connector_session_token`，不需要连接器每小时重新握手。

## 为什么连接器 JWT 要派生一个独立密钥，而不是复用 `get_jwt_secret()`

如果连接器 token 和用户登录 token 用同一把密钥签名，只靠 payload 里的
`typ` 字段区分"这是连接器 token 还是用户 token"是不够的——`typ` 只是数据，
拿到 token 的人可以看，但**验证逻辑本身**如果不强制检查它，两种 token 就
互相可以拿去当对方用。具体风险：`auth.py::_decode_token` 目前直接
`payload["sub"]`/`payload["username"]`，如果连接器 token 意外被塞进
`Authorization: Bearer` 头打到普通用户端点，且连接器 payload 里也塞了这两个
字段（为了让它"看起来像"一个用户 token），`get_current_user` 会成功解出一个
**伪造的用户身份**——`role_store` 查不到这个 user_id 的角色，挂了角色守卫的
35 个端点会被这层挡住，但**只挂 `get_current_user`、不查角色的另外 35 个
端点完全没有防线**（`CLAUDE.md` §3.2 那张不对称表）。

派生独立密钥从根上堵死这条路：`jwt.decode` 校验签名失败会直接抛
`InvalidSignatureError`（`InvalidTokenError` 的子类），`auth.py::_decode_token`
已经在捕获这个异常返回 401——连接器 token 拿去当用户 token 用，**验证这一步
就过不去**，不需要额外写"检查 typ 字段"这种容易被漏掉的防御代码。反过来，
泄露的用户 token 也不能被这边的连接器认证接受。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import jwt

# ---------------------------------------------------------------------------
# 三层令牌的熵与生命周期（§10.1 原文数字）
# ---------------------------------------------------------------------------

REGISTER_TOKEN_ENTROPY_BYTES = 16  # 128 bit，同 activation.py 的激活码
REGISTER_TOKEN_TTL_SECONDS = 10 * 60

REFRESH_TOKEN_ENTROPY_BYTES = 32  # 256 bit —— TTL 30 天，比激活码/session
# token 活得都久，泄露窗口更长，熵值给高一档

CONNECTOR_SESSION_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600

_CONNECTOR_JWT_ALGORITHM = "HS256"
_CONNECTOR_JWT_TYP = "ops_connector_session"

# §3.2/§10.1：心跳每 10 秒一次；判定"是否在线"用 3 倍心跳间隔做容错窗口，
# 给一两次丢包/网络抖动留余量，同时不会把真正失联的连接器误判成在线太久。
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_STALE_AFTER_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 3


def generate_register_token() -> str:
    return secrets.token_urlsafe(REGISTER_TOKEN_ENTROPY_BYTES)


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_TOKEN_ENTROPY_BYTES)


def hash_token(token: str) -> str:
    """SHA-256，不是 bcrypt——理由跟 `activation.py` 完全一致：这里的 token
    都是高熵随机数，不是人选的低熵密码，不需要 bcrypt 的慢来补熵；反而
    bcrypt 的慢在一个握手/心跳都可能被频繁调用的路径上是白给的 DoS 面。
    ⚠️ 这个理由只在 token 是高熵随机数时成立，跟 `activation.py` 同一条警告。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def derive_connector_jwt_secret(base_secret: str) -> str:
    """从主 JWT 密钥派生一把连接器专用的签名密钥。

    不是为了"多一层保密"，是为了让连接器 token 和用户 token **天然无法互相
    冒充**——两者用不同密钥签名，任何一边的验证逻辑校验签名时对方的 token
    都会直接失败。派生而不是要求运维再单独配一个环境变量，是为了不增加
    部署复杂度（`CLAUDE.md` 已经因为"配置双轨、含静默失效的死配置"吃过 P1
    级别的教训，见 `docs/scale_slo_and_priorities.md`），只要主密钥没变、
    派生密钥就必然一致，不需要额外的密钥管理。
    """
    return hmac.new(base_secret.encode("utf-8"), b"ops_connector_session", hashlib.sha256).hexdigest()


def create_connector_session_jwt(
    connection_id: str, org_id: str, secret: str,
    *, ttl_seconds: int = CONNECTOR_SESSION_TOKEN_TTL_SECONDS, now: Optional[float] = None,
) -> str:
    """`secret` 传入的应该是 `derive_connector_jwt_secret(get_jwt_secret())`
    的结果，不是主密钥本身——调用方（app.py）负责这层派生，这个函数只管签发。
    """
    now = now if now is not None else time.time()
    payload = {
        "typ": _CONNECTOR_JWT_TYP,
        "connection_id": connection_id,
        "org_id": org_id,
        "iat": int(now),
        "exp": int(now + ttl_seconds),
    }
    return jwt.encode(payload, secret, algorithm=_CONNECTOR_JWT_ALGORITHM)


class ConnectorTokenInvalid(ValueError):
    """连接器 session token 解码/校验失败——签名错误、过期、或 typ 不对。"""


def decode_connector_session_jwt(token: str, secret: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(token, secret, algorithms=[_CONNECTOR_JWT_ALGORITHM])
    except jwt.InvalidTokenError as e:
        raise ConnectorTokenInvalid(str(e)) from e
    if payload.get("typ") != _CONNECTOR_JWT_TYP:
        # 理论上派生密钥已经防住了跨用途冒用，这里是第二道防线——防御性校验，
        # 不依赖"签名对了 typ 就一定对"这个假设。
        raise ConnectorTokenInvalid("token 的 typ 字段不是 ops_connector_session")
    return payload


# ---------------------------------------------------------------------------
# register_token 校验（纯函数：接收已从库里查出来的字段，不做 IO）
# ---------------------------------------------------------------------------

class RegisterTokenFailure(str, Enum):
    NOT_FOUND = "not_found"
    ALREADY_USED = "already_used"
    EXPIRED = "expired"


@dataclass(frozen=True)
class RegisterTokenCheckResult:
    ok: bool
    failure: Optional[RegisterTokenFailure] = None


def check_register_token(
    *, stored_hash: Optional[str], provided_token: str,
    used: bool, expires_at: Optional[float], now: Optional[float] = None,
) -> RegisterTokenCheckResult:
    now = now if now is not None else time.time()
    if stored_hash is None or not hmac.compare_digest(stored_hash, hash_token(provided_token)):
        return RegisterTokenCheckResult(False, RegisterTokenFailure.NOT_FOUND)
    if used:
        return RegisterTokenCheckResult(False, RegisterTokenFailure.ALREADY_USED)
    if expires_at is not None and now > expires_at:
        return RegisterTokenCheckResult(False, RegisterTokenFailure.EXPIRED)
    return RegisterTokenCheckResult(True)


# ---------------------------------------------------------------------------
# refresh_token 校验：单次使用即轮换 + 重放检测
# ---------------------------------------------------------------------------

class RefreshTokenFailure(str, Enum):
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class RefreshTokenCheckResult:
    ok: bool
    failure: Optional[RefreshTokenFailure] = None
    # True 时调用方必须把这当成泄露信号处理：撤销该连接器的全部会话令牌，
    # 强制它重新走一遍 register_token 握手——不是简单拒绝这次刷新就完了。
    is_replay: bool = False


def check_refresh_token(
    *, stored_hash: Optional[str], provided_token: str,
    consumed_at: Optional[float], expires_at: Optional[float], now: Optional[float] = None,
) -> RefreshTokenCheckResult:
    """`consumed_at` 非空 = 这个 token 之前已经被用掉过一次——現在又被拿来用，
    只有两种可能：客户端逻辑有 bug（自己重放了旧值），或者 token 泄露被人
    拿去用了。区分不了是哪种，所以统一按"当成泄露"处理，这是刻意的保守。
    """
    now = now if now is not None else time.time()
    if stored_hash is None or not hmac.compare_digest(stored_hash, hash_token(provided_token)):
        return RefreshTokenCheckResult(False, RefreshTokenFailure.NOT_FOUND)
    if consumed_at is not None:
        return RefreshTokenCheckResult(False, RefreshTokenFailure.REPLAYED, is_replay=True)
    if expires_at is not None and now > expires_at:
        return RefreshTokenCheckResult(False, RefreshTokenFailure.EXPIRED)
    return RefreshTokenCheckResult(True)


# ---------------------------------------------------------------------------
# 心跳新鲜度判定：读路径现算，不信写路径缓存的 connector_status 字符串
# ---------------------------------------------------------------------------

def is_heartbeat_fresh(last_heartbeat_at: Optional[float], now: Optional[float] = None) -> bool:
    """`ops_store.py` 的 `connector_status` 字段在收到心跳时被动写成
    'online'，但连接器可能在下一次心跳前突然断线（进程崩溃、网络中断），
    这种情况下 `connector_status` 会一直停在 'online' 直到下次心跳超时逻辑
    介入——**这个函数就是那层超时判断，必须在每次读的时候现算，不能信
    上次写入的字符串**。跟 `docs/aiops_module_design.md` §3.2 的要求
    （"任何连接器在线的判断都必须来自实时心跳，不能缓存假设"）直接对应。
    """
    if last_heartbeat_at is None:
        return False
    now = now if now is not None else time.time()
    return (now - last_heartbeat_at) <= HEARTBEAT_STALE_AFTER_SECONDS
