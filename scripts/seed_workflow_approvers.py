"""工作流审批人种子脚本 —— work-flow-v2.md 收尾方案。

内置的 4 个工作流模板（laptop_repair/leave_request/business_trip/
expense_reimbursement）出厂时 approver_role_id 为空，导致任何工作流都无法发起
（后端会提示"暂未配置审批人"）。这个脚本把系统角色 super_admin 设为这些模板的
默认审批角色（2026-08-24 起平台侧废弃 admin 角色，原本这里默认指向的 admin
已不存在，见 role_store.py 顶部说明），幂等、可重复执行：只回填 approver_role_id
仍为空的模板，已经被管理员手动改派过审批角色的模板不会被覆盖。

用法：
    python scripts/seed_workflow_approvers.py            # 执行回填
    python scripts/seed_workflow_approvers.py --dry-run   # 只打印将要处理的模板，不写库
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.ragent_backend.role_store import RoleStore, ROLE_SUPER_ADMIN
from src.ragent_backend.workflow_store import WorkflowStore


async def seed(dry_run: bool = False) -> None:
    role_store = RoleStore()
    workflow_store = WorkflowStore()
    try:
        # 触发 RoleStore._ensure_schema：确保 super_admin 系统角色种子已建好
        await role_store._get_pool()
        approver_role = await role_store.get_role_by_name(ROLE_SUPER_ADMIN)
        if approver_role is None:
            print(f"[ERROR] 系统角色 {ROLE_SUPER_ADMIN!r} 不存在，请先确认角色系统已初始化")
            return

        templates = await workflow_store.list_templates()
        print(f"共 {len(templates)} 个工作流模板，super_admin 角色 id={approver_role.role_id}")

        for template in templates:
            if template.approver_role_id:
                print(f"  跳过 {template.workflow_type}（已配置审批角色 {template.approver_role_id}）")
                continue
            if dry_run:
                print(f"  [dry-run] 将把 {template.workflow_type} 的审批角色设为 super_admin")
            else:
                await workflow_store.update_template(template.template_id, approver_role_id=approver_role.role_id)
                print(f"  已把 {template.workflow_type} 的审批角色设为 super_admin")

        print(f"\n完成{'（dry-run，未写库）' if dry_run else ''}。")
    finally:
        await role_store.close()
        await workflow_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="把内置工作流模板的审批角色默认配置为 super_admin")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要处理的模板，不写库")
    args = parser.parse_args()
    asyncio.run(seed(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
