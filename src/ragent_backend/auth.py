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

import os
import time
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException

# 生产环境必须通过环境变量覆盖；这里的默认值只是让本地开发不需要额外配置就能跑起来。
_JWT_SECRET = os.getenv("RAGENT_JWT_SECRET", "dev-only-insecure-secret-change-me")
_JWT_ALGORITHM = "HS256"
_TOKEN_TTL_SECONDS = 24 * 60 * 60  # 24 小时


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
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def _decode_token(token: str) -> AuthenticatedUser:
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
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
