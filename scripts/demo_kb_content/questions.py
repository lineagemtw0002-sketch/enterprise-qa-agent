"""人工测试数据集的问题库（正向问题 / 应被拒绝的问题 / 跨账号对照组）。

跟语料写在同一个包里是刻意的：**问题的"预期关键事实"必须跟语料同源**，
分开维护一定会漂移。改语料里的锚点数字时，这里的 `keywords` 必须一起改，
`scripts/generate_demo_kb_dataset.py --verify` 会当场把不一致跑出来。

账号与权限是**现网实际状态**（2026-08-26 从 Postgres 查得，本次未做任何改动）：
- `alice_acme`   org_admin（系统角色）→ 隐式通配，可访问 Acme 名下全部 6 个库
- `bob_acme`     企业角色「Acme人事部」→ 只有 acme_hr_admin_kb
- `carol_globex` org_admin（系统角色）→ 隐式通配，可访问 Globex 名下全部 6 个库
- `dave_globex`  企业角色「人力资源与行政知识库」→ 只有 globex_hr_admin_kb
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class Account:
    username: str
    password: str
    org: str
    role_display: str
    role_kind: str
    collections: List[str]
    note: str


ACME_KBS = [
    "acme_hr_admin_kb",
    "acme_finance_kb",
    "acme_it_support_kb",
    "acme_rd_product_kb",
    "acme_sales_marketing_kb",
    "acme_customer_success_kb",
]
GLOBEX_KBS = [
    "globex_hr_admin_kb",
    "globex_finance_kb",
    "globex_it_support_kb",
    "globex_rd_product_kb",
    "globex_sales_marketing_kb",
    "globex_customer_success_kb",
]

KB_DISPLAY = {
    "acme_hr_admin_kb": "Acme 有限公司 人力行政知识库",
    "acme_finance_kb": "Acme 有限公司 财务知识库",
    "acme_it_support_kb": "Acme 有限公司 IT支持知识库",
    "acme_rd_product_kb": "Acme 有限公司 研发产品知识库",
    "acme_sales_marketing_kb": "Acme 有限公司 销售市场知识库",
    "acme_customer_success_kb": "Acme 有限公司 客户成功知识库",
    "globex_hr_admin_kb": "Globex 环球集团 人力行政知识库",
    "globex_finance_kb": "Globex 环球集团 财务知识库",
    "globex_it_support_kb": "Globex 环球集团 IT支持知识库",
    "globex_rd_product_kb": "Globex 环球集团 研发产品知识库",
    "globex_sales_marketing_kb": "Globex 环球集团 销售市场知识库",
    "globex_customer_success_kb": "Globex 环球集团 客户成功知识库",
}

ACCOUNTS: Dict[str, Account] = {
    "alice_acme": Account(
        "alice_acme", "alice_acme123", "Acme 有限公司", "企业管理员（org_admin）",
        "系统角色", ACME_KBS,
        "企业管理员，不需要显式配置知识库分组也能访问本企业全部知识库（role_store 里的隐式通配）",
    ),
    "bob_acme": Account(
        "bob_acme", "bob_acme123", "Acme 有限公司", "Acme人事部", "企业角色",
        ["acme_hr_admin_kb"],
        "受限员工：角色只关联了人力行政知识库，本企业其他 5 个库一律拒绝",
    ),
    "carol_globex": Account(
        "carol_globex", "carol_globex123", "Globex 环球集团", "企业管理员（org_admin）",
        "系统角色", GLOBEX_KBS,
        "另一家企业的管理员，本企业全库可见，但看不到 Acme 的任何内容",
    ),
    "dave_globex": Account(
        "dave_globex", "dave_globex123", "Globex 环球集团", "人力资源与行政知识库",
        "企业角色", ["globex_hr_admin_kb"],
        "受限员工：角色只关联了人力行政知识库，本企业其他 5 个库一律拒绝",
    ),
}


@dataclass(frozen=True)
class Question:
    collection: str
    query: str
    keywords: List[str]
    source: str
    note: str = ""


# ---------------------------------------------------------------- 正向问题
# 每个知识库 5 条，关键事实全部来自本包的语料，`source` 是文档编号。
POSITIVE: Dict[str, List[Question]] = {
    "acme_hr_admin_kb": [
        Question("acme_hr_admin_kb", "入职满一年后每年有多少天年假？", ["15 天", "15天"], "ACME-HR-001",
                 "跨企业对照锚点：Globex 是 20 天"),
        Question("acme_hr_admin_kb", "当年没休完的年假可以顺延到什么时候？", ["3 月 31 日", "3月31日", "次年 3 月"], "ACME-HR-001",
                 "跨企业对照锚点：Globex 是次年 6 月 30 日"),
        Question("acme_hr_admin_kb", "每个月最多可以远程办公几天？", ["8 天", "8天"], "ACME-HR-006"),
        # 措辞是实测挑出来的：直接问「正式员工离职需要提前多少天」会被原有语料里
        # 「客户成功部员工离职需要提前 20 天」那类部门特例压过去（见文档 §0.1）；
        # 把「正式员工和试用期」并列问，才稳定命中 ACME-HR-008。
        Question("acme_hr_admin_kb", "正式员工和试用期员工的离职通知期分别是多少天？", ["30 天", "30天"], "ACME-HR-008"),
        Question("acme_hr_admin_kb", "公司的核心工作时间是几点到几点？", ["10:00", "16:00"], "ACME-HR-005"),
    ],
    "acme_finance_kb": [
        Question("acme_finance_kb", "报销单笔超过多少金额需要 CFO 审批？", ["20000", "20,000", "2 万", "2万"], "ACME-FIN-001"),
        Question("acme_finance_kb", "去一线城市出差，住宿费每晚上限是多少？", ["600"], "ACME-FIN-003"),
        Question("acme_finance_kb", "出差期间伙食补贴每天多少钱？", ["120"], "ACME-FIN-005"),
        Question("acme_finance_kb", "采购金额超过多少必须三家比价？", ["50000", "50,000", "5 万", "5万"], "ACME-FIN-007"),
        Question("acme_finance_kb", "员工因公借款单次上限是多少？", ["20000", "20,000", "2 万", "2万"], "ACME-FIN-011"),
    ],
    "acme_it_support_kb": [
        Question("acme_it_support_kb", "域账号密码多久强制更换一次？", ["90 天", "90天"], "ACME-IT-001",
                 "跨企业对照锚点：Globex 的 WMS 是 60 天"),
        Question("acme_it_support_kb", "密码连续输错几次会锁定账号，锁多久？", ["5 次", "5次", "30 分钟", "30分钟"], "ACME-IT-001"),
        Question("acme_it_support_kb", "堡垒机会话录像保留多少天？", ["180 天", "180天"], "ACME-IT-004"),
        Question("acme_it_support_kb", "P1 级 IT 工单要求多久内响应？", ["30 分钟", "30分钟"], "ACME-IT-005"),
        Question("acme_it_support_kb", "收到钓鱼邮件应该转发到哪个邮箱？", ["security@acme.internal"], "ACME-IT-007"),
    ],
    "acme_rd_product_kb": [
        Question("acme_rd_product_kb", "代码合并到主分支需要几名评审人通过？", ["2 名", "2名", "两名"], "ACME-RD-001"),
        Question("acme_rd_product_kb", "常规发布窗口是什么时候？", ["周四", "20:00"], "ACME-RD-003"),
        Question("acme_rd_product_kb", "P0 级线上故障要求多久内止血？", ["1 小时", "1小时"], "ACME-RD-004"),
        Question("acme_rd_product_kb", "企业版客户的 API 限流阈值是多少？", ["5000"], "ACME-RD-008"),
        Question("acme_rd_product_kb", "弃用对外接口需要提前多久公告？", ["6 个月", "6个月"], "ACME-RD-007"),
    ],
    "acme_sales_marketing_kb": [
        Question("acme_sales_marketing_kb", "销售代表最高能给客户多少折扣？", ["10%"], "ACME-SM-001",
                 "跨企业对照锚点：Globex 销售是 8%"),
        Question("acme_sales_marketing_kb", "专业版每个用户每月多少钱？", ["399"], "ACME-SM-002"),
        Question("acme_sales_marketing_kb", "官网线索需要多久内完成首次联系？", ["2 小时", "2小时"], "ACME-SM-004"),
        Question("acme_sales_marketing_kb", "标准 POC 试用期是多久，可以延长几次？", ["14 天", "14天", "一次"], "ACME-SM-006"),
        Question("acme_sales_marketing_kb", "渠道代理商的标准返点是多少？", ["15%"], "ACME-SM-012"),
    ],
    "acme_customer_success_kb": [
        Question("acme_customer_success_kb", "黄金套餐客户的工单多久内首次响应？", ["15 分钟", "15分钟"], "ACME-CS-001"),
        Question("acme_customer_success_kb", "公司承诺的月度服务可用性是多少？", ["99.9"], "ACME-CS-002"),
        Question("acme_customer_success_kb", "发生重大故障后多久内要主动外呼客户？", ["30 分钟", "30分钟"], "ACME-CS-004"),
        Question("acme_customer_success_kb", "NPS 低于几分要标记为重点关注客户？", ["6 分", "6分"], "ACME-CS-006"),
        Question("acme_customer_success_kb", "新客户的标准上手周期是多长？", ["30 天", "30天"], "ACME-CS-009"),
    ],
    "globex_hr_admin_kb": [
        Question("globex_hr_admin_kb", "入职满一年后每年有多少天年假？", ["20 天", "20天"], "GLBX-HR-001",
                 "跨企业对照锚点：Acme 是 15 天"),
        Question("globex_hr_admin_kb", "当年没休完的年假可以顺延到什么时候？", ["6 月 30 日", "6月30日", "次年 6 月"], "GLBX-HR-001",
                 "跨企业对照锚点：Acme 是次年 3 月 31 日"),
        Question("globex_hr_admin_kb", "驾驶员连续驾驶最多多少小时，必须休息多久？", ["4 小时", "4小时", "20 分钟", "20分钟"], "GLBX-HR-002"),
        Question("globex_hr_admin_kb", "仓储岗位夜班是几点到几点，夜班津贴多少？", ["22:00", "80 元", "80元"], "GLBX-HR-003"),
        Question("globex_hr_admin_kb", "新员工安全生产培训需要多少学时？", ["16 学时", "16学时"], "GLBX-HR-007"),
    ],
    "globex_finance_kb": [
        Question("globex_finance_kb", "客户对账单有异议须在几个工作日内提出？", ["7 个工作日", "7个工作日"], "GLBX-FIN-001"),
        Question("globex_finance_kb", "报销单笔超过多少金额需要 CFO 审批？", ["30000", "30,000", "3 万", "3万"], "GLBX-FIN-002",
                 "跨企业对照锚点：Acme 是 20000 元"),
        Question("globex_finance_kb", "进口货物的关税预缴款要在到港前几天完成？", ["5 天", "5天"], "GLBX-FIN-006"),
        Question("globex_finance_kb", "出口退税要在报关单放行后多少天内申报？", ["30 天", "30天"], "GLBX-FIN-007"),
        Question("globex_finance_kb", "运营车辆的折旧年限是多少年？", ["8 年", "8年"], "GLBX-FIN-012"),
    ],
    "globex_it_support_kb": [
        Question("globex_it_support_kb", "WMS 账号密码多久强制更换一次？", ["60 天", "60天"], "GLBX-IT-001",
                 "跨企业对照锚点：Acme 域账号是 90 天"),
        Question("globex_it_support_kb", "GPS 车载终端故障多久没恢复要联系设备厂商？", ["2 小时", "2小时"], "GLBX-IT-003"),
        Question("globex_it_support_kb", "系统常规变更窗口是什么时候？", ["周三", "01:00"], "GLBX-IT-007"),
        Question("globex_it_support_kb", "WMS 库存对不上应该怎么排查？", ["盘点", "出库", "调拨"], "GLBX-IT-010"),
        Question("globex_it_support_kb", "集团 IT 热线电话是多少？", ["400-820-9000"], "GLBX-IT-006"),
    ],
    "globex_rd_product_kb": [
        Question("globex_rd_product_kb", "路由优化算法要达到什么指标才允许灰度上线？", ["3%"], "GLBX-RD-001"),
        Question("globex_rd_product_kb", "WMS 新功能全量推广前，库存差异率要控制在多少以内？", ["0.5%"], "GLBX-RD-002"),
        Question("globex_rd_product_kb", "平台的常规发布窗口是什么时候？", ["周三", "01:00"], "GLBX-RD-003"),
        Question("globex_rd_product_kb", "运单和订单数据保留多久？", ["3 年", "3年"], "GLBX-RD-008"),
        Question("globex_rd_product_kb", "客户要求删除个人信息，多久内要完成清理？", ["15 个工作日", "15个工作日"], "GLBX-RD-008"),
    ],
    "globex_sales_marketing_kb": [
        Question("globex_sales_marketing_kb", "销售代表最高能给客户多少折扣？", ["8%"], "GLBX-SM-001",
                 "跨企业对照锚点：Acme 销售代表是 10%"),
        Question("globex_sales_marketing_kb", "年运费承诺超过多少的大客户合同需要总部特批？", ["100 万", "100万"], "GLBX-SM-001"),
        Question("globex_sales_marketing_kb", "运费报价单的有效期是多久？", ["15 天", "15天"], "GLBX-SM-003"),
        Question("globex_sales_marketing_kb", "集团对外承诺的整体准点率是多少？", ["95%"], "GLBX-SM-005"),
        Question("globex_sales_marketing_kb", "保价服务怎么收费？", ["0.3%", "30 元", "30元"], "GLBX-SM-010"),
    ],
    "globex_customer_success_kb": [
        Question("globex_customer_success_kb", "货物破损投诉要多久内首次响应？", ["4 小时", "4小时"], "GLBX-CS-001"),
        Question("globex_customer_success_kb", "未保价的货物损坏了怎么赔付？", ["3 倍", "3倍", "2000"], "GLBX-CS-002"),
        Question("globex_customer_success_kb", "客户要改收货地址，多久内免费，超时收多少钱？", ["4 小时", "4小时", "80"], "GLBX-CS-004"),
        Question("globex_customer_success_kb", "货损理赔要在多少个工作日内完成？", ["15 个工作日", "15个工作日"], "GLBX-CS-002"),
        Question("globex_customer_success_kb", "报关延误导致客户投诉，多久内要联系报关合规部？", ["2 小时", "2小时"], "GLBX-CS-007"),
    ],
}


@dataclass(frozen=True)
class DenialQuestion:
    account: str
    query: str
    kind: str          # 'no_permission' | 'cross_org'
    target: str        # 期望它去问、但实际不该拿到内容的库
    why: str
    forbidden: Tuple[str, ...] = ()
    """另一家企业**独有**的标记词。判据检查的是检索片段（回答里 `> ` 开头的引用行），
    不是整段回答——回答开头会原样回显用户的问题，问题里本来就带着「Globex」
    这类词，拿整段回答做子串匹配一定会误判成越权（2026-08-26 实测踩过：
    `bob_acme` 问「Globex 的驾驶员年假是多少天？」被判 BREACH，
    实际 4 条来源全是 Acme 自己库里的文档，零跨企业数据）。"""


# --------------------------------------------------- 应该被拒绝 / 查不到的问题
# 每个账号至少 2 条：一条本企业内无权限，一条跨企业。
DENIALS: List[DenialQuestion] = [
    DenialQuestion("alice_acme", "驾驶员连续驾驶最多多少小时就必须休息？", "cross_org",
                   "globex_hr_admin_kb",
                   "这是 Globex 的安全红线。alice 是 Acme 的企业管理员，本企业内是通配权限，"
                   "但候选库先被 _org_owned_collections(Acme) 收敛过，Globex 的库根本不在候选集里",
                   forbidden=("枢纽", "Globex 环球集团", "驾驶员", "报关")),
    DenialQuestion("alice_acme", "未保价的货物损坏了按运费的几倍赔付？", "cross_org",
                   "globex_customer_success_kb",
                   "同上，跨企业。Acme 是 SaaS 公司，本来也没有货物赔付这种业务",
                   forbidden=("枢纽", "Globex 环球集团", "驾驶员", "报关")),
    DenialQuestion("alice_acme", "产品需求文档库里对需求评审有什么要求？", "cross_org",
                   "product_req_kb（测试新公司）",
                   "第三家企业的知识库，跨企业隔离",
                   forbidden=("产品需求文档库", "测试新公司")),
    DenialQuestion("bob_acme", "报销单笔超过多少金额需要 CFO 审批？", "no_permission",
                   "acme_finance_kb",
                   "**本企业内无权限**——bob 的角色「Acme人事部」只关联了人力行政知识库。"
                   "这是最能体现角色级隔离的一条：同一家公司、同一份数据、换个账号就查不到"),
    DenialQuestion("bob_acme", "域账号密码多久强制更换一次？", "no_permission",
                   "acme_it_support_kb",
                   "同上，IT 支持知识库不在 bob 的角色权限内"),
    DenialQuestion("bob_acme", "驾驶员连续驾驶最多多少小时就必须休息？", "cross_org",
                   "globex_hr_admin_kb",
                   "跨企业。这条是 Globex 的安全红线，Acme 的人力行政库里没有任何对应内容——"
                   "刻意挑一个「本企业没有近似话题」的问题，避免检索器拿 Acme 自己的年假文档来凑数",
                   forbidden=("枢纽", "Globex 环球集团", "驾驶员", "报关")),
    DenialQuestion("carol_globex", "入职满一年后每年有 15 天年假吗？", "cross_org",
                   "acme_hr_admin_kb",
                   "15 天是 Acme 的口径。carol 是 Globex 管理员，应该只答出 Globex 的 20 天，"
                   "或者明确说没有 15 天这个规定——**答出「是的，15 天」就说明串库了**",
                   forbidden=("Acme 有限公司", "堡垒机", "域账号", "套餐")),
    DenialQuestion("carol_globex", "堡垒机会话录像保留多少天？", "cross_org",
                   "acme_it_support_kb",
                   "Acme 的 IT 制度，Globex 侧没有堡垒机这一说",
                   forbidden=("Acme 有限公司", "堡垒机", "域账号", "套餐")),
    DenialQuestion("dave_globex", "客户对账单有异议须在几个工作日内提出？", "no_permission",
                   "globex_finance_kb",
                   "**本企业内无权限**——dave 的角色只关联了人力行政知识库"),
    DenialQuestion("dave_globex", "货物破损投诉要多久内首次响应？", "no_permission",
                   "globex_customer_success_kb",
                   "同上，客户成功知识库不在 dave 的角色权限内"),
    DenialQuestion("dave_globex", "Acme 的员工每月最多可以远程办公几天？", "cross_org",
                   "acme_hr_admin_kb",
                   "跨企业。dave 同样有「人力行政」类目权限，但那是 **Globex 的**",
                   forbidden=("Acme 有限公司", "堡垒机", "域账号", "套餐")),
]


@dataclass(frozen=True)
class ComparisonRow:
    query: str
    expected: Dict[str, str]   # username -> 预期结果
    why: str


# ------------------------------------------------------------ 跨账号对照表
COMPARISON: List[ComparisonRow] = [
    ComparisonRow(
        "入职满一年后每年有多少天年假？",
        {
            "alice_acme": "15 天（Acme 口径）",
            "bob_acme": "15 天（Acme 口径）",
            "carol_globex": "20 天（Globex 口径）",
            "dave_globex": "20 天（Globex 口径）",
        },
        "四个账号都有各自企业人力行政库的权限，**同一个问题必须答出两个不同的数字**。"
        "如果四个账号答出同一个数字，或者某个账号同时提到 15 和 20，就是跨企业串库。",
    ),
    ComparisonRow(
        "当年没休完的年假可以顺延到什么时候？",
        {
            "alice_acme": "次年 3 月 31 日",
            "bob_acme": "次年 3 月 31 日",
            "carol_globex": "次年 6 月 30 日",
            "dave_globex": "次年 6 月 30 日",
        },
        "同上，第二个独立锚点。两条同时对得上，基本可以排除「碰巧蒙对」。",
    ),
    ComparisonRow(
        "报销单笔超过多少金额需要 CFO 审批？",
        {
            "alice_acme": "20000 元（Acme 口径）",
            "bob_acme": "🚫 无权访问（角色里没有财务库）",
            "carol_globex": "30000 元（Globex 口径）",
            "dave_globex": "🚫 无权访问（角色里没有财务库）",
        },
        "这一行同时演示两件事：**跨企业数据不同** + **同企业内按角色拒绝**。"
        "两个 org_admin 答出不同数字，两个受限员工被拒。",
    ),
    ComparisonRow(
        "销售代表最高能给客户多少折扣？",
        {
            "alice_acme": "10%（Acme 口径）",
            "bob_acme": "🚫 无权访问（角色里没有销售市场库）",
            "carol_globex": "8%（Globex 口径）",
            "dave_globex": "🚫 无权访问（角色里没有销售市场库）",
        },
        "同上结构，换一个部门再验一次，避免只在财务库上碰巧成立。",
    ),
    ComparisonRow(
        "密码多久强制更换一次？",
        {
            "alice_acme": "域账号 90 天（Acme 口径）",
            "bob_acme": "🚫 无权访问（角色里没有 IT 支持库）",
            "carol_globex": "WMS 账号 60 天（Globex 口径）",
            "dave_globex": "🚫 无权访问（角色里没有 IT 支持库）",
        },
        "两家企业连「密码策略」这种最容易雷同的制度都给了不同数字，"
        "串库时最容易在这一行暴露。",
    ),
]


def questions_for_account(username: str) -> Dict[str, List[Question]]:
    """按账号实际可访问的 collection 过滤正向问题。"""
    acct = ACCOUNTS[username]
    return {c: POSITIVE[c] for c in acct.collections if c in POSITIVE}
