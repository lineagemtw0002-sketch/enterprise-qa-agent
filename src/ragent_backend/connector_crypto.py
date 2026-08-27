"""
租户连接器凭证加密 (P0 修复 — 2026-08-26)

背景：`tenant_connectors.auth_config`（`tenant_connector_store.py`）存的是企业连接
自己知识库/考勤系统用的凭证（如 API token），改之前是明文 JSON 直接落库——数据库
一旦泄露，所有企业的第三方系统凭证跟着泄露。

方案：应用层对称加密，用 `cryptography.fernet.Fernet`（对称加密、自带
IV/HMAC/时间戳、密钥轮转有现成的 `MultiFernet` 可用，没有自己发明格式的必要）。
密钥策略照抄 `auth.py::resolve_jwt_secret` 的 fail-fast 惯例（见该文件同名函数）：
- 参数可注入（`key=` / `debug=`），方便单测不碰环境变量、不用真的拒绝进程退出；
- 非 `RAGENT_DEBUG=true` 时，密钥缺失、等于内置不安全默认值、或格式不合法，
  一律 `raise RuntimeError` 拒绝启动；
- **密钥本身绝不能落库**——只能来自环境变量 `RAGENT_CONNECTOR_ENCRYPTION_KEY`
  或调用方显式传入，`tenant_connectors` 表里不存在何处能推出密钥的字段。

存储格式：`auth_config` 列本身还是 JSONB 不变。写入时把整个明文 dict 序列化成
JSON 字符串再加密，包一层哨兵 key `{"__enc__": "<fernet-token>"}` 存进去
（Fernet token 是纯 ASCII 字符串，JSONB 能装下，不需要改列类型）。
读取时按哨兵 key 分流：
- 命中哨兵 key（且只有这一个 key）→ 走加密路径解密；
- 没命中（老数据，行内容本来就是明文 dict，如 `{"token": "..."}` 或 `{}`）
  → 原样当明文返回，不当错误处理。

这不是要长期维护"两套读后端"（不是 `bm25_storage_design.md` 那种场景）——纯粹是
为了让"先部署这版代码、再跑一次性迁移脚本"这个顺序，在迁移脚本还没跑的窗口期
不中断服务：部署当下库里全是旧明文行，如果读路径只认加密格式，服务会直接读出
乱码/抛异常。迁移脚本
（`scripts/migrate_connector_auth_config_encryption.py`）跑完之后，全表应该都是
密文，明文分支理论上再也走不到，但保留这个兼容分支的代价是 0（哨兵 key 检测
一次 dict 长度和键名，比在迁移完成后特意删掉这条分支、将来还要防止再有人绕过
`TenantConnectorStore` 直接手写 SQL 插入明文行，成本低得多），所以刻意不加
"迁移完成后必须移除"的死期。
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional

import os

from cryptography.fernet import Fernet, InvalidToken

# JSONB 里用来标记"这个 dict 是密文包装"的哨兵 key。真实的 auth_config 明文
# （目前只有 `{"token": "..."}` 一种形状）不会用这个名字当 key，所以拿它当判据
# 足够安全；`decrypt_auth_config` 额外要求"只有这一个 key"进一步降低误判概率。
_ENCRYPTED_MARKER_KEY = "__enc__"

# 内置的不安全开发默认密钥——仅当 RAGENT_DEBUG=true 且未设置
# RAGENT_CONNECTOR_ENCRYPTION_KEY 时才会被使用，让本地开发不需要额外配置就能跑
# 起来。刻意用一个固定的、可读的种子生成（而不是随机数），这样它在任何环境里
# 算出来的值都相同，才能被 resolve_connector_encryption_key 精确识别并拒绝在
# 生产环境使用——这跟 auth.py 里 `"dev-only-insecure-secret-change-me"` 是同一个
# 思路，只是 Fernet 密钥必须是"32 字节经 url-safe base64 编码"这个固定格式，
# 不能直接拿一个任意字符串当默认值（那样连 Fernet(key) 都构造不出来）。
_INSECURE_DEV_KEY_SEED = b"dev-only-insecure-connector-key!"  # 正好 32 字节
assert len(_INSECURE_DEV_KEY_SEED) == 32, "seed 必须正好 32 字节才能当 Fernet 原始密钥用"
DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY = base64.urlsafe_b64encode(_INSECURE_DEV_KEY_SEED).decode()


def resolve_connector_encryption_key(
    key: Optional[str] = None,
    debug: Optional[bool] = None,
) -> bytes:
    """解析用于加密/解密 `tenant_connectors.auth_config` 的 Fernet 密钥。

    Args:
        key: 显式传入的密钥字符串；为 None 时从环境变量
            `RAGENT_CONNECTOR_ENCRYPTION_KEY` 读取。参数可注入是为了单测不需要
            碰 `os.environ`（照抄 `auth.py::resolve_jwt_secret` 的写法）。
        debug: 是否处于调试模式；为 None 时从 `RAGENT_DEBUG` 环境变量读取。

    Returns:
        校验通过的密钥，编码成 bytes（`Fernet()` 构造函数要的类型）。

    Raises:
        RuntimeError: 密钥缺失、等于内置不安全默认值、或不是合法的 Fernet 密钥格式，
            且当前不处于调试模式。
    """
    if key is None:
        key = os.getenv("RAGENT_CONNECTOR_ENCRYPTION_KEY")
    if debug is None:
        debug = os.getenv("RAGENT_DEBUG", "false").lower() == "true"

    if not key:
        if debug:
            return DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY.encode()
        raise RuntimeError(
            "RAGENT_CONNECTOR_ENCRYPTION_KEY 未设置。租户连接器凭证（企业接入自己"
            "知识库/考勤系统用的 API token）需要加密后才能落库，缺少密钥就拒绝启动，"
            "避免明文落库。生成一个真实密钥：\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
            "把输出设进环境变量 RAGENT_CONNECTOR_ENCRYPTION_KEY。仅当 RAGENT_DEBUG=true "
            "时才允许回退到内置的不安全开发密钥（切勿在生产环境这样做）。"
        )

    if key == DEFAULT_INSECURE_CONNECTOR_ENCRYPTION_KEY and not debug:
        raise RuntimeError(
            "RAGENT_CONNECTOR_ENCRYPTION_KEY 等于内置的不安全默认开发密钥，拒绝启动。"
            "请生成一个真实密钥并通过环境变量覆盖（生成命令见上）。"
        )

    key_bytes = key.encode("ascii") if isinstance(key, str) else key
    try:
        Fernet(key_bytes)
    except Exception as exc:  # pragma: no cover - 具体异常类型由 cryptography 决定
        raise RuntimeError(
            "RAGENT_CONNECTOR_ENCRYPTION_KEY 不是合法的 Fernet 密钥（必须是 32 字节"
            f"经 url-safe base64 编码的字符串）：{exc}"
        ) from exc
    return key_bytes


def build_fernet(key: Optional[str] = None, debug: Optional[bool] = None) -> Fernet:
    """`resolve_connector_encryption_key` + `Fernet(...)` 的便捷封装。"""
    return Fernet(resolve_connector_encryption_key(key=key, debug=debug))


def encrypt_auth_config(auth_config: Optional[Dict[str, Any]], fernet: Fernet) -> Dict[str, Any]:
    """把明文 auth_config dict 加密，包装成可以直接塞进 JSONB 列的哨兵 dict。"""
    plaintext = json.dumps(auth_config or {}, ensure_ascii=False).encode("utf-8")
    token = fernet.encrypt(plaintext).decode("ascii")
    return {_ENCRYPTED_MARKER_KEY: token}


def decrypt_auth_config(stored: Any, fernet: Fernet) -> Dict[str, Any]:
    """把从 JSONB 列读出来的 dict 还原成明文 auth_config。

    兼容两种形态（模块 docstring「存储格式」一节有完整解释）：
    - 新格式 `{"__enc__": "<fernet-token>"}` → 解密还原成原始 dict；
    - 旧格式 / 非加密形态（迁移前的存量数据，比如 `{"token": "..."}` 或 `{}`）
      → 原样当明文返回。
    """
    if not isinstance(stored, dict):
        return {}
    if is_encrypted(stored):
        token = stored[_ENCRYPTED_MARKER_KEY]
        try:
            plaintext = fernet.decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise ValueError(
                "auth_config 密文无法用当前 RAGENT_CONNECTOR_ENCRYPTION_KEY 解密——"
                "密钥是否已轮换但存量数据还没有用新密钥重新加密？"
            ) from exc
        return json.loads(plaintext.decode("utf-8"))
    return dict(stored)


def is_encrypted(stored: Any) -> bool:
    """判断一个从 auth_config 列读出来的 dict 是不是本模块加密过的密文包装。"""
    return isinstance(stored, dict) and len(stored) == 1 and _ENCRYPTED_MARKER_KEY in stored
