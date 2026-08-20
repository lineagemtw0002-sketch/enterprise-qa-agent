"""生成 + 摄入平台内部固定的 6 个部门知识库测试数据。

对应 `src/mcp_server/tools/query_knowledge_hub.py` 里的 DEPARTMENT_KB_COLLECTIONS
（"全库混合召回 + 重排"改造引入的固定部门知识库清单）：
    hr_admin_kb          人力资源与行政知识库
    finance_kb           财务与报销制度知识库
    it_support_kb        IT 支持与技术运维知识库
    sales_marketing_kb   销售话术与市场知识库
    rd_product_kb        研发与产品代码知识库
    customer_success_kb  客户成功与售后服务知识库

每条独立写成一个小文件（几百字，远小于 chunk_size），保证"一个文件 ≈ 一个 chunk"；
摄入进本地共享 Chroma（`data/db/chroma`，不是租户委托那套，走的是这个项目自己组织
internal_chroma 检索路径）。

用法：
    python scripts/seed_department_kb_v2.py              # 生成语料 + 摄入 + 建角色/关联
    python scripts/seed_department_kb_v2.py --skip-ingest # 只生成语料文件，不摄入、不建角色
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

REPO_ROOT = Path(__file__).parent.parent
CORPUS_DIR = REPO_ROOT / "data" / "dept_kb_corpus"
ENTRIES_PER_KB = 14
SEED = 2026


def _sample_entries(template_fn, count: int, rng: random.Random) -> list[str]:
    seen: set[str] = set()
    entries: list[str] = []
    attempts = 0
    while len(entries) < count and attempts < count * 50:
        attempts += 1
        text = template_fn(rng)
        if text not in seen:
            seen.add(text)
            entries.append(text)
    return entries


def _write_collection(collection: str, entries: list[str]) -> int:
    d = CORPUS_DIR / collection
    d.mkdir(parents=True, exist_ok=True)
    for i, text in enumerate(entries, start=1):
        (d / f"{i:03d}.txt").write_text(text, encoding="utf-8")
    return len(entries)


# ============================================================
# 1. 人力资源与行政知识库 hr_admin_kb
# ============================================================
_HR_TEAMS = ["研发部", "市场部", "销售部", "客户成功部", "行政部", "财务部"]

def _hr_admin(rng: random.Random) -> str:
    team = rng.choice(_HR_TEAMS)
    days = rng.choice([5, 10, 15])
    remote_days = rng.choice([2, 4, 6])
    templates = [
        f"{team}员工年假按司龄计算，入职满一年起每年 {days} 天，当年未休完可顺延到次年 3 月底，超期作废。",
        f"{team}远程办公需提前一天在 OA 系统提交申请，每月不超过 {remote_days} 天，试用期员工暂不适用。",
        f"新员工入职当天由行政前台统一领取工位、门禁卡和办公用品，当天下午由 HR 介绍公司制度。",
        f"{team}请病假 3 天以内只需提交医院诊断证明，超过 3 天需额外提交复工评估报告。",
        f"会议室通过 OA 系统「会议室预订」模块预约，超过 15 分钟未签到自动释放给候补预约人。",
        f"{team}员工离职需提前 30 天书面通知，交接清单由直属主管确认签字后 HR 才启动离职结算。",
        f"常规办公用品在各楼层茶水间自助领取，打印纸、硒鼓等耗材用完请在行政群登记补货。",
        f"{team}绩效考核每半年一次，分为 S/A/B/C/D 五档，连续两次 C 档以上触发绩效改进计划。",
    ]
    return rng.choice(templates)


# ============================================================
# 2. 财务与报销制度知识库 finance_kb
# ============================================================
def _finance(rng: random.Random) -> str:
    amount = rng.choice([500, 1000, 2000, 5000])
    days = rng.choice([3, 5, 7])
    templates = [
        f"差旅报销需在出差结束后 {days} 个工作日内在 OA 系统提交，超期提交需要直属主管特批。",
        f"单笔报销金额低于 {amount} 元只需部门负责人审批，超过 {amount} 元需要财务负责人联签。",
        f"发票必须是增值税普通发票或专用发票，个人抬头、无发票的支出一律不予报销。",
        f"出差住宿标准：普通员工每晚不超过 {amount} 元，超标部分个人承担，需提前报备特殊情况。",
        f"报销单据丢失的，可用消费流水+情况说明走特批流程，但每人每年最多 2 次。",
        f"采购合同到期前 21 天，系统自动提醒负责人评估是否续约，逾期未处理自动转为待续约状态。",
        f"部门预算按季度下达，超支需要提交书面说明并经分管副总裁审批后才能继续列支。",
        f"招待费报销必须注明陪同人员姓名及事由，单次超过 {amount} 元需附上级审批意见。",
    ]
    return rng.choice(templates)


# ============================================================
# 3. IT 支持与技术运维知识库 it_support_kb
# ============================================================
def _it_support(rng: random.Random) -> str:
    minutes = rng.choice([15, 30, 60])
    templates = [
        "公司 VPN 客户端为 GlobalProtect，下载地址在内网门户「软件中心」，首次接入用工号+域账号密码登录。",
        "域账号密码 90 天强制更换一次，长度不少于 10 位，需包含大小写字母、数字和特殊符号。",
        f"生产环境故障按严重程度分 P0-P3 四级，P0 要求 {minutes} 分钟内响应、1 小时内止血。",
        "电脑无法开机先检查电源线和适配器指示灯，仍无法解决联系 IT 现场支援分机 8801。",
        "无法连接内网打印机，先确认已连接公司 Wi-Fi「Company-Secure」，再重新添加打印机驱动。",
        "发现钓鱼邮件请直接转发给 security@company.internal，不要点击其中链接或下载附件。",
        "禁止将公司文件上传至个人网盘或私人邮箱，禁止在办公电脑安装未经审批的软件。",
        "新员工入职电脑、显示器由 IT 统一配发，入职当天可在前台领取；额外外设需在 IT 门户提交工单。",
        "访问生产环境数据库一律通过堡垒机跳转，禁止个人电脑直连，堡垒机会话全程录屏留存 30 天。",
    ]
    return rng.choice(templates)


# ============================================================
# 4. 销售话术与市场知识库 sales_marketing_kb
# ============================================================
def _sales_marketing(rng: random.Random) -> str:
    discount = rng.choice([5, 8, 10, 15])
    templates = [
        f"标准报价单折扣权限：销售代表最高可给 {discount}% 折扣，超出需要销售总监审批。",
        "客户异议处理话术：遇到「价格太贵」类问题，先确认对方对比的是哪个竞品，再突出差异化功能。",
        "市场活动物料（海报/易拉宝/宣传册）统一走品牌设计团队制作，禁止销售自行修改公司 Logo 使用规范。",
        "潜在客户线索进入 CRM 系统后需在 24 小时内完成首次跟进，超过 48 小时未跟进自动转公海。",
        "竞品对比材料每季度更新一次，销售不得使用超过 3 个月未更新的竞品数据对外宣传。",
        "签约合同金额超过 50 万元需要销售总监和法务共同会签，标准合同模板不得随意删改条款。",
        "客户续约提醒：合同到期前 60 天，CRM 系统自动提醒对应销售跟进续约，逾期视为流失客户。",
        "参展/路演费用需提前两周提交预算申请，市场部审核通过后才能对外确认参展信息。",
    ]
    return rng.choice(templates)


# ============================================================
# 5. 研发与产品代码库 rd_product_kb
# ============================================================
def _rd_product(rng: random.Random) -> str:
    n = rng.choice([1, 2, 3])
    templates = [
        f"代码合并要求至少 {n} 名评审人通过，且 CI 流水线全部检查项为绿色才允许合并到主分支。",
        "生产环境发布必须先在灰度环境跑满 30 分钟金丝雀流量，指标异常自动回滚，不允许跳过。",
        "数据库字段变更涉及个人信息类型的，必须先经过数据合规团队评审，评估是否需要更新隐私政策。",
        "API 网关服务的数据库备份策略是每日全量 + 每小时增量，保留 30 天。",
        "新功能上线前需要产品经理确认验收标准，未达成验收标准的任务自动流转到下一个迭代。",
        "线上问题按严重程度分 P0-P3 四级，复盘报告需要在 48 小时内完成根因分析并归档。",
        "技术债清理优先级最高的是老版本 API 下线，需要提前通知存量客户完成迁移。",
        "代码仓库权限按项目组隔离，离职员工交接完成前不予提前回收仓库权限。",
    ]
    return rng.choice(templates)


# ============================================================
# 6. 客户成功与售后服务知识库 customer_success_kb
# ============================================================
def _customer_success(rng: random.Random) -> str:
    minutes = rng.choice([15, 30, 60, 120])
    tier = rng.choice(["黄金", "白银", "青铜"])
    templates = [
        f"{tier}套餐客户的工单响应 SLA 是 {minutes} 分钟内首次响应，客户成功团队 7x24 小时轮班值守。",
        f"{tier}套餐客户故障恢复目标（RTO）是 {minutes} 分钟，超出目标需要在复盘报告里说明原因。",
        "客户报障工单按影响面分为单租户问题和多租户问题，多租户问题直接拉相关负责人进战情室处理。",
        "客户对产品提出的功能建议统一录入产品需求池，不承诺一定采纳，但会在评审会反馈进度。",
        "客户流失预警：连续两个月使用量下降超过 30% 的账号，客户成功经理需主动联系了解原因。",
        "退款申请需要客户成功经理和财务共同确认，7 个工作日内完成审核并答复客户。",
        "重大故障影响到客户时，客户成功团队需要在规定时限内主动外呼说明情况，不能等客户先联系。",
        "客户满意度回访每季度一次，NPS 低于 6 分的客户需要标记为重点关注对象。",
    ]
    return rng.choice(templates)


DEPARTMENT_KBS = {
    "hr_admin_kb": ("人力资源与行政知识库", _hr_admin),
    "finance_kb": ("财务与报销制度知识库", _finance),
    "it_support_kb": ("IT 支持与技术运维知识库", _it_support),
    "sales_marketing_kb": ("销售话术与市场知识库", _sales_marketing),
    "rd_product_kb": ("研发与产品代码知识库", _rd_product),
    "customer_success_kb": ("客户成功与售后服务知识库", _customer_success),
}


def generate_corpus() -> None:
    for slug, (label, template_fn) in DEPARTMENT_KBS.items():
        rng = random.Random(f"{SEED}:{slug}")
        entries = _sample_entries(template_fn, ENTRIES_PER_KB, rng)
        n = _write_collection(slug, entries)
        print(f"  [{slug}] {label}: {n} 条")


async def ingest_corpus() -> None:
    from src.core.settings import load_settings
    from src.ingestion.pipeline import IngestionPipeline

    settings = load_settings()
    for slug in DEPARTMENT_KBS:
        files = sorted((CORPUS_DIR / slug).glob("*.txt"))
        pipeline = IngestionPipeline(settings, collection=slug, force=True)
        ok = 0
        for f in files:
            result = pipeline.run(str(f))
            if result.success:
                ok += 1
            else:
                print(f"  [FAIL] {f}: {result.error}")
        print(f"  [{slug}] 摄入 {ok}/{len(files)} 个文件")


async def setup_roles_and_assign(assign_to_usernames: list[str]) -> None:
    """给 6 个部门知识库各建一个同名角色（内部标识跟 collection 同名，方便对应），
    关联对应 collection，并把这几个角色批量分配给指定的测试账号（一人一角色的
    约束是"每次分配只能选一个角色"，不是"一个人只能测一个库"——这里是用脚本
    直接写 user_roles 表，不经过那个前端/API 的单角色校验，纯粹是为了方便一个
    测试账号能一次性验证全部 6 个库，不代表产品上鼓励一个人挂 6 个角色）。"""
    from src.ragent_backend.role_store import RoleStore
    from src.ragent_backend.user_store import UserStore

    role_store = RoleStore()
    user_store = UserStore()
    try:
        role_ids = []
        for slug, (label, _) in DEPARTMENT_KBS.items():
            role = await role_store.get_or_create_role_by_name(slug, label)
            await role_store.add_role_collection(role.role_id, slug)
            role_ids.append(role.role_id)
            print(f"  角色就绪: {label} ({slug}) -> collection={slug}")

        users = await user_store.list_users()
        by_username = {u.username: u for u in users}
        for username in assign_to_usernames:
            user = by_username.get(username)
            if user is None:
                print(f"  [SKIP] 用户不存在: {username}")
                continue
            for role_id in role_ids:
                await role_store.add_user_role(user.user_id, role_id)
            print(f"  已给 {username} 分配全部 6 个部门知识库角色")
    finally:
        await role_store.close()
        await user_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="生成并摄入 6 个固定部门知识库的测试数据")
    parser.add_argument("--skip-ingest", action="store_true", help="只生成语料文件，不摄入、不建角色/分配")
    parser.add_argument(
        "--assign-to", nargs="*", default=["bob"],
        help="把 6 个部门角色分配给哪些用户（默认 bob），传空列表则不分配",
    )
    args = parser.parse_args()

    print("生成 6 个部门知识库语料...")
    generate_corpus()

    if args.skip_ingest:
        return

    print("\n摄入到本地 Chroma...")
    asyncio.run(ingest_corpus())

    print("\n建角色 + 关联知识库 + 分配测试账号...")
    asyncio.run(setup_roles_and_assign(args.assign_to))


if __name__ == "__main__":
    main()
