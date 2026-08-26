"""Acme 有限公司的 6 个部门知识库注册表。"""

from __future__ import annotations

from scripts.demo_kb_content import KbSpec, acme_cs, acme_fin, acme_hr, acme_it, acme_rd, acme_sm

ORG = "Acme 有限公司"


def _kb(category: str, label: str, mod) -> KbSpec:
    return KbSpec(
        collection=f"acme_{category}_kb",
        tenant="acme",
        category=category,
        org_name=ORG,
        kb_label=label,
        build=mod.build,
    )


KBS = [
    _kb("hr_admin", "人力行政知识库", acme_hr),
    _kb("finance", "财务知识库", acme_fin),
    _kb("it_support", "IT支持知识库", acme_it),
    _kb("rd_product", "研发产品知识库", acme_rd),
    _kb("sales_marketing", "销售市场知识库", acme_sm),
    _kb("customer_success", "客户成功知识库", acme_cs),
]
