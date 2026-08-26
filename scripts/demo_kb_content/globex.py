"""Globex 环球集团的 6 个部门知识库注册表。"""

from __future__ import annotations

from scripts.demo_kb_content import (
    KbSpec,
    globex_cs,
    globex_fin,
    globex_hr,
    globex_it,
    globex_rd,
    globex_sm,
)

ORG = "Globex 环球集团"


def _kb(category: str, label: str, mod) -> KbSpec:
    return KbSpec(
        collection=f"globex_{category}_kb",
        tenant="globex",
        category=category,
        org_name=ORG,
        kb_label=label,
        build=mod.build,
    )


KBS = [
    _kb("hr_admin", "人力行政知识库", globex_hr),
    _kb("finance", "财务知识库", globex_fin),
    _kb("it_support", "IT支持知识库", globex_it),
    _kb("rd_product", "研发产品知识库", globex_rd),
    _kb("sales_marketing", "销售市场知识库", globex_sm),
    _kb("customer_success", "客户成功知识库", globex_cs),
]
