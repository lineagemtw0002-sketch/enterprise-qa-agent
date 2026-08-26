"""
JWT 登录鉴权。

职责：
1. 签发/校验 JWT access token（登录成功后发一个，之后的请求带着它证明身份）
2. FastAPI 依赖 `get_current_user`：从 Authorization header 解析出可信的
   user_id/username，供各端点使用——绝不相信请求体里客户端自己声明的 user_id，
   身份只能来自校验通过的 token。

3. FastAPI 依赖工厂 `require_role`：在 `get_current_user` 基础上再查一次数据库里的
   实时角色（不信任 token 里的角色声明——因为 token 24 小时内不会过期，如果角色被
   管理员现改了，靠 token 里的旧角色判断会导致改权限不能立刻生效）

不做的事（保持简单）：
- 没有 refresh token / 登出黑名单，token 到期前始终有效，到期后重新登录即可
- "能访问哪些 collection" 仍由 UserStore.allowed_collections 决定，跟角色是两个维度
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException

logger = logging.getLogger(__name__)

# 这个字符串公开在源码里，任何拿到仓库的人都能用它签发任意 user_id 的 token。
# 它只是让本地开发不用额外配置就能跑起来，绝不能出现在真实部署里。
_DEV_FALLBACK_SECRET = "dev-only-insecure-secret-change-me"


def resolve_jwt_secret(
    secret: Optional[str] = None,
    debug_mode: Optional[bool] = None,
) -> str:
    """决定实际使用的 JWT 签名密钥，配置不安全时直接拒绝启动。

    2026-08-24 代码审计发现的 P0：原本这里是
    `os.getenv("RAGENT_JWT_SECRET", "dev-only-insecure-secret-change-me")`，
    而全仓（`.env`、`.env.example`、所有文档）都没有出现过 RAGENT_JWT_SECRET
    这个变量名——也就是说真实部署里它几乎必然就是上面那个公开的默认值。
    `get_current_user` 是整个系统身份的唯一来源，`require_role`、多租户
    collection ACL 全部建立在它解出的 user_id 之上，所以密钥一旦可预测，
    上层所有权限设计（角色校验、tenant_ 前缀拦截、归属二次校验）一起失效。

    这里选择 fail-fast 而不是继续"打个日志然后照常启动"：不安全的密钥不会
    产生任何可观察的异常，服务照常工作、登录照常成功，问题只在被利用时才
    暴露——这种缺陷必须在启动阶段挡住，不能指望运维记得看警告。

    放行条件只有一个：显式开着 RAGENT_DEBUG=true（本地开发）。这里复用的是
    `RAGENT_DEBUG` 这同一个环境变量本身（`resolve_cors_origins` 也复用同一个），
    不再引入新的环境概念——2026-08-26 起它不再对应任何绕过 ACL 的端点，
    纯粹是"本地开发默认值 vs 生产环境必须显式配置"这层含义。

    参数可注入是为了让这段策略能被直接单测，不必操作进程环境变量。
    """
    if secret is None:
        secret = os.getenv("RAGENT_JWT_SECRET", "")
    secret = secret.strip()

    if secret and secret != _DEV_FALLBACK_SECRET:
        return secret

    if debug_mode is None:
        debug_mode = os.getenv("RAGENT_DEBUG", "false").strip().lower() == "true"

    if debug_mode:
        # ⚠️ 这条必须是 WARNING 且必须显眼：它是"本地配置被带上生产"的唯一
        # 早期信号。改造日志体系时**不许把它降成 debug**——root 没配置时
        # logging 的 lastResort 也会把 WARNING 打到 stderr，可见性不弱于原来的 print。
        logger.warning(
            "[Auth] 警告：正在使用源码内置的开发用 JWT 密钥，任何人都可以伪造身份。"
            "仅限本地开发；部署前必须设置 RAGENT_JWT_SECRET。",
            extra={"event": "auth.jwt_secret.dev_fallback_in_use"},
        )
        return _DEV_FALLBACK_SECRET

    raise RuntimeError(
        "RAGENT_JWT_SECRET 未设置（或仍是源码里的开发默认值），拒绝启动。\n"
        "该密钥是整个系统身份校验的唯一凭据，使用默认值等于任何人都能伪造任意用户"
        "（包括 super_admin）的登录凭证，多租户权限隔离将完全失效。\n"
        "请设置一个随机密钥，例如：\n"
        "    python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
        "然后写入部署环境的 RAGENT_JWT_SECRET。\n"
        "（仅本地开发可通过 RAGENT_DEBUG=true 使用内置开发密钥。）"
    )


_JWT_ALGORITHM = "HS256"
_TOKEN_TTL_SECONDS = 24 * 60 * 60  # 24 小时

# 惰性解析：import 这个模块本身不应该有"可能让进程崩掉"的副作用，否则任何想
# 引用 AuthenticatedUser 的测试/脚本都得先准备好完整环境。真正的 fail-fast 由
# `create_app()` 启动时显式调用 `get_jwt_secret()` 完成（那时崩掉才是想要的）。
_jwt_secret_cache: Optional[str] = None


def get_jwt_secret() -> str:
    """取实际使用的签名密钥；配置不安全时抛 RuntimeError。

    结果缓存，保证同一进程内签发和校验用的一定是同一个密钥（否则轮换配置时
    会出现"签出来的 token 自己验不过"这种极难排查的状态）。
    """
    global _jwt_secret_cache
    if _jwt_secret_cache is None:
        _jwt_secret_cache = resolve_jwt_secret()
    return _jwt_secret_cache


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str


def create_access_token(user_id: str, username: str) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "iat": now,
        "exp": now + _TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=_JWT_ALGORITHM)


def _decode_token(token: str) -> AuthenticatedUser:
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的登录凭证")

    return AuthenticatedUser(user_id=payload["sub"], username=payload["username"])


async def get_current_user(authorization: Optional[str] = Header(default=None)) -> AuthenticatedUser:
    """FastAPI 依赖：要求请求带有效的 `Authorization: Bearer <token>`。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")

    return _decode_token(token)


def require_role(*allowed_roles: str):
    """依赖工厂：要求当前用户的（实时查库的）角色集合与 allowed_roles 有交集，否则 403。

    角色从数据库现查，而不是从 token claims 里读——token 24 小时有效期内，
    管理员改了某人的角色应该立刻生效，不用等对方重新登录。

    一个用户可以同时拥有多个角色（如 admin + "IT部"），只要其中任意一个角色名
    命中 allowed_roles 即放行；单角色场景（只有 admin 一个角色）跟老版本行为完全一致。
    """

    async def _dependency(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        # 延迟导入，避免 auth.py 和 role_store.py 循环引用
        from src.ragent_backend.role_store import RoleStore

        # FastAPI 依赖每次请求都会重新调用这个闭包，但 RoleStore 内部的连接池是
        # 懒创建且跨调用复用的单例（见 role_store.py 的 _pool 缓存），所以这里
        # 每次 new 一个 RoleStore() 不会重复建池，可以放心用。
        store = RoleStore()
        role_names = {r.name for r in await store.get_user_roles(current_user.user_id)}
        if not role_names & set(allowed_roles):
            raise HTTPException(status_code=403, detail="权限不足")
        return current_user

    return _dependency


async def require_same_org_or_platform(
    user_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """FastAPI 依赖：要求当前用户要么是平台管理员，要么和路径里的目标 `user_id`
    属于同一家企业，否则 403。

    这是叠加在 `require_role(ROLE_SUPER_ADMIN)` 之上的另一层判断，不是替代关系——
    `require_role` 只回答"这个人是不是某种管理员"，这个依赖回答"这个管理员能不能碰
    这个具体的目标用户"，两件事分开判断，路由上两个依赖一起挂（见 `app.py` 的
    `admin_delete_user`/`admin_set_user_roles`）。

    跟 `require_role` 同样的实时性原则：企业归属现查库，不信 token，管理员被
    平台管理员改派了企业，下一次请求立刻按新的企业边界生效。
    """
    # 延迟导入，避免 auth.py 和 org_store.py 循环引用（跟 require_role 里对
    # role_store 的处理方式一致）
    from src.ragent_backend.org_store import OrgStore

    store = OrgStore()
    if await store.is_platform_admin(current_user.user_id):
        return current_user

    actor_org = await store.get_org_for_user(current_user.user_id)
    target_org = await store.get_org_for_user(user_id)
    if actor_org is None or target_org is None or actor_org.org_id != target_org.org_id:
        raise HTTPException(status_code=403, detail="无权操作其他企业的用户")
    return current_user


async def require_platform_admin(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """FastAPI 依赖：要求当前用户属于平台组织（`organizations.is_platform=TRUE`），
    否则 403。给"建组织"、"改派用户所属企业"这类跨企业操作用——这两件事不能交给
    某家客户企业自己的管理员做，只有我们自己（平台组织）能做。"""
    from src.ragent_backend.org_store import OrgStore

    if not await OrgStore().is_platform_admin(current_user.user_id):
        raise HTTPException(status_code=403, detail="仅平台管理员可操作")
    return current_user
