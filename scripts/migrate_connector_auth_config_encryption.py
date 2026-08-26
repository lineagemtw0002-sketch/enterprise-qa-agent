"""租户连接器凭证加密 —— 存量数据迁移 (P0 修复配套脚本, 2026-08-26)

背景（完整设计见 `src/ragent_backend/connector_crypto.py` 模块 docstring 与
`tenant_connector_store.py` 文件头"安全"一节）：`tenant_connectors.auth_config`
原来是明文 JSON 直接落库，代码已经改成"写入前加密、读取时自动解密/兼容明文"，
但代码改动本身不会动已经在库里的存量行——这些行在这个脚本跑之前，`auth_config`
列里存的仍然是明文（比如 `{"token": "sk-xxxx"}`）。`TenantConnectorStore` 的读路径
兼容分支只是保证"读到明文也能正常工作、不中断服务"，不代表这些行已经安全存储。

用法：
    python scripts/migrate_connector_auth_config_encryption.py            # 执行加密
    python scripts/migrate_connector_auth_config_encryption.py --dry-run  # 只打印将要处理的行，不写库

前置条件：
    环境变量 `RAGENT_CONNECTOR_ENCRYPTION_KEY` 必须已经配置成一个真实密钥
    （跟应用进程用的必须是同一个，否则应用之后读不出这次加密的数据）。这里复用
    生产同一个 `resolve_connector_encryption_key`/`build_fernet`，没配置会直接
    RuntimeError 退出——这是有意的：不允许在没有真实密钥的情况下"顺手"跑迁移。

处理规则：
    - 逐行读 `tenant_connectors.auth_config`，用 `connector_crypto.is_encrypted`
      判断是不是已经是密文包装；已经是密文的跳过（脚本整体幂等，可重复执行、
      可在迁移中途失败后直接重跑）。
    - 明文行：加密后按 `id` 原地 UPDATE 回同一行，不改 `created_at` /
      `connector_id` / 其它任何字段。不通过 `TenantConnectorStore.upsert()`——
      `upsert()` 是 `(org_id, capability)` 唯一键上的 `INSERT ... ON CONFLICT`，
      语义是"新建或整体覆盖"，用它做迁移得手工凑齐全部字段，一旦某个字段传漏
      就会在"迁移"的同时悄悄把别的字段改掉；直接对 `id` 做单列 `UPDATE` 更安全、
      意图也更清楚。
    - 迁移前统计一次（总行数 / 明文行数 / 密文行数），迁移后重新查一遍确认
      明文行数归零，作为脚本自身的验收，不依赖人工检查数据库。

关于要不要长期维护"双读"或提供"回滚到明文"开关（有意的设计决策，不是漏做）：
    - **不提供回滚到明文的开关。** 明文存储正是这次要修的 P0 本身，特意留一个
      "改回明文"的开关等于在代码里常驻一个自我否定的后门，且没有任何合法场景
      需要它——如果发现加密引入了功能性 bug，正确做法是修 bug 或回滚这次代码
      改动本身（git revert），而不是给数据判两次刑。
    - **不需要长期维护"两套读后端"**（不是 `bm25_storage_design.md` 那种场景，
      那边是磁盘布局/查询算法整体换代，需要按规模决定何时切换、且切换本身
      有性能取舍）。这里只是同一个 JSONB 列的内容从明文变密文，`decrypt_auth_config`
      的"读到明文就原样返回"分支是为了让"先部署代码、再跑这个脚本"这个正常的
      部署顺序在脚本跑之前的窗口期不中断服务，跑完这个脚本后明文分支理论上
      再也走不到，但保留它的运行时成本接近零（就是一次 dict 长度和 key 名判断），
      所以不设"迁移完成后必须删除"的死期，也不算"长期维护两套后端"。
    - **已知的运营风险（不是这次要解决的问题，但必须写清楚）**：`RAGENT_CONNECTOR_
      ENCRYPTION_KEY` 一旦丢失，迁移完成后的所有连接器凭证将无法解密、永久不可
      恢复（这是对称加密的正常代价，不是 bug）。需要各企业管理员通过管理后台
      重新填一次 token 才能恢复委托查询能力。这次改动没有实现密钥轮转
      （`cryptography.fernet.MultiFernet` 支持多密钥、可以做到"用新密钥加密、
      用新旧密钥都能解密"），因为当前只有一个密钥版本，轮转是未来需要真的换密钥
      时才有意义的功能，这次不做，避免在没有需求的情况下引入额外复杂度。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import asyncpg

from src.ragent_backend.connector_crypto import build_fernet, encrypt_auth_config, is_encrypted


def _load_auth_config(raw) -> dict:
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def migrate(dry_run: bool = False) -> None:
    dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")
    # 复用生产同一个 fail-fast 解析：没配置真实密钥直接在这里报错退出，
    # 不允许迁移脚本自己悄悄回退到不安全默认值。
    fernet = build_fernet()

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, org_id, capability, auth_config FROM tenant_connectors ORDER BY org_id, capability"
            )

            plaintext_rows = []
            already_encrypted = 0
            for row in rows:
                stored = _load_auth_config(row["auth_config"])
                if is_encrypted(stored):
                    already_encrypted += 1
                else:
                    plaintext_rows.append((row["id"], row["org_id"], row["capability"], stored))

            print(
                f"共 {len(rows)} 条连接器配置，其中 {len(plaintext_rows)} 条仍是明文、"
                f"{already_encrypted} 条已是密文（本次会跳过，脚本幂等）。"
            )

            if not plaintext_rows:
                print("没有需要迁移的明文行，退出。")
                return

            for connector_id, org_id, capability, plaintext in plaintext_rows:
                if dry_run:
                    print(
                        f"  [dry-run] {org_id}/{capability} ({connector_id}): "
                        f"auth_config keys={sorted(plaintext.keys())} -> 将加密"
                    )
                    continue
                encrypted = encrypt_auth_config(plaintext, fernet)
                await conn.execute(
                    "UPDATE tenant_connectors SET auth_config = $1 WHERE id = $2",
                    json.dumps(encrypted),
                    connector_id,
                )
                print(f"  已加密 {org_id}/{capability} ({connector_id})")

            if dry_run:
                print(f"\n迁移（dry-run，未写库）完成，{len(plaintext_rows)} 条待加密。")
                return

            # 验收：重新查一遍，确认明文行数归零，不依赖人工检查数据库。
            rows_after = await conn.fetch("SELECT auth_config FROM tenant_connectors")
            remaining_plaintext = sum(
                1 for r in rows_after if not is_encrypted(_load_auth_config(r["auth_config"]))
            )
            verdict = "符合预期" if remaining_plaintext == 0 else "异常，请人工检查！"
            print(
                f"\n迁移完成。本次加密 {len(plaintext_rows)} 条。"
                f"复查：剩余明文行数 = {remaining_plaintext}（{verdict}）"
            )
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 tenant_connectors.auth_config 里存量的明文凭证原地加密（幂等，可重复执行）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将要处理的行，不写库")
    args = parser.parse_args()
    asyncio.run(migrate(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
