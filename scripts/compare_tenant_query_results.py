"""同一句话，两家企业的员工各自查，验证查到的是各自企业知识库里的不同数据。

用法：
    python scripts/compare_tenant_query_results.py "新员工入职需要做什么"
    python scripts/compare_tenant_query_results.py "新员工入职需要做什么" --acme-user bob_acme --globex-user carol_globex

不传 --acme-user/--globex-user 时，默认从各自企业里各挑一个真实存在的用户
（按用户名排序取第一个）。Acme/Globex 都是委托模式（http_api 连接器），
按 knowledge-base-tenant-federation.md 5.2 节的设计，委托模式下不做角色级
ACL，同一家企业内任何用户查都是查同一个远端知识库——所以这里选哪个用户
不影响查询结果，只影响"以谁的身份查"这件事本身能不能通过路由。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.core.settings import load_settings
from src.ragent_backend.org_store import OrgStore
from src.ragent_backend.user_store import UserStore
from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool


async def _users_by_org(user_store: UserStore, org_store: OrgStore) -> dict:
    """一次性把所有用户按所属企业分组，避免对每个用户都单独查一次 org（N+1）。"""
    users = await user_store.list_users()
    grouped: dict[str, list] = {}
    for u in users:
        org = await org_store.get_org_for_user(u.user_id)
        if org is not None:
            grouped.setdefault(org.org_id, []).append(u)
    return grouped


async def _pick_user(user_store: UserStore, org_store: OrgStore, org_name_contains: str):
    orgs = await org_store.list_organizations()
    org = next(o for o in orgs if org_name_contains in o.name)
    grouped = await _users_by_org(user_store, org_store)
    candidates = sorted(grouped.get(org.org_id, []), key=lambda u: u.username)
    if not candidates:
        raise SystemExit(f"{org.name} 名下没有用户")
    return candidates[0]


async def _resolve_user(user_store: UserStore, username: str):
    users = await user_store.list_users()
    for u in users:
        if u.username == username:
            return u
    raise SystemExit(f"用户不存在: {username}")


async def run(query: str, acme_username: str | None, globex_username: str | None) -> None:
    settings = load_settings("config/settings.yaml")
    user_store = UserStore()
    org_store = OrgStore()
    tool = QueryKnowledgeHubTool(settings=settings, user_store=user_store, org_store=org_store)

    acme_user = await _pick_user(user_store, org_store, "Acme") if acme_username is None else await _resolve_user(user_store, acme_username)
    globex_user = await _pick_user(user_store, org_store, "Globex") if globex_username is None else await _resolve_user(user_store, globex_username)
    acme_username, acme_user_id = acme_user.username, acme_user.user_id
    globex_username, globex_user_id = globex_user.username, globex_user.user_id

    print(f"提问: {query!r}\n")

    for label, username, user_id in [
        ("Acme 有限公司", acme_username, acme_user_id),
        ("Globex 环球集团", globex_username, globex_user_id),
    ]:
        response = await tool.execute(query=query, user_id=user_id)
        print(f"{'='*70}\n{label} — 以 {username} 身份查询\n{'='*70}")
        print(response.content)
        print()

    await user_store.close()
    await org_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="同一句话在两家企业各自的知识库里查，对比结果")
    parser.add_argument("query", help="查询语句")
    parser.add_argument("--acme-user", default=None, help="以哪个 Acme 用户身份查（默认自动挑一个）")
    parser.add_argument("--globex-user", default=None, help="以哪个 Globex 用户身份查（默认自动挑一个）")
    args = parser.parse_args()
    asyncio.run(run(args.query, args.acme_user, args.globex_user))


if __name__ == "__main__":
    main()
