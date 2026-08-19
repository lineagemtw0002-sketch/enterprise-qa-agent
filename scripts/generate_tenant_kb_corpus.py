"""生成 Acme / Globex 两家模拟企业的知识库语料——每家几百条、彼此内容 90%+
不重叠（Acme 是云软件公司，覆盖研发/云基础设施/产品这类主题；Globex 是物流
集团，覆盖车队/仓储/报关这类主题，两家除了报销这一项刻意保留的重叠主题外，
知识域完全不同），供 `scripts/seed_tenant_kb_demo.py` 摄入用。

每条独立写成一个小文件（几百字，远小于 chunk_size=1000），保证摄入后
"一个文件 ≈ 一个 chunk"，`几百条` 数得清楚，不依赖切块器怎么切。

用法：
    python scripts/generate_tenant_kb_corpus.py
"""

from __future__ import annotations

import itertools
import random
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ENTRIES_PER_CATEGORY = 21
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


def _write_category(base_dir: Path, category_slug: str, entries: list[str]) -> int:
    cat_dir = base_dir / category_slug
    cat_dir.mkdir(parents=True, exist_ok=True)
    for i, text in enumerate(entries, start=1):
        (cat_dir / f"{i:03d}.txt").write_text(text, encoding="utf-8")
    return len(entries)


# ============================================================
# Acme 有限公司（云软件/SaaS 公司）
# ============================================================

ACME_SERVICES = ["数据看板服务", "API 网关服务", "消息队列服务", "权限中心服务", "计费结算服务",
                  "工单系统服务", "文件存储服务", "搜索索引服务", "通知推送服务", "日志采集服务"]
ACME_ENVS = ["生产环境", "预发环境", "测试环境", "灰度环境"]
ACME_TEAMS = ["平台研发部", "增长研发部", "数据研发部", "客户成功部", "IT 运维部", "安全合规部"]
ACME_TOOLS = ["GitLab", "Jenkins", "ArgoCD", "Prometheus", "Grafana", "Jira", "Confluence"]

def acme_cloud_infra(rng: random.Random) -> str:
    svc = rng.choice(ACME_SERVICES)
    env = rng.choice(ACME_ENVS)
    window = rng.choice(["每周二 02:00-04:00", "每周四 01:00-03:00", "每月第一个周日 00:00-02:00"])
    sla = rng.choice([99.9, 99.95, 99.99])
    templates = [
        f"{svc}在{env}的变更窗口固定为{window}，窗口外的发布需要走紧急变更审批，"
        f"由值班 SRE 和对应研发负责人双人确认后才能执行，避免非计划时间的发布影响用户。",
        f"{svc}对外承诺的可用性 SLA 为 {sla}%，{env}每次发布前必须先在灰度环境跑满 30 分钟"
        f"金丝雀流量，指标异常自动回滚，人工发布不允许跳过这一步。",
        f"{svc}的数据库备份策略是每日全量 + 每小时增量，保留 30 天，{env}的备份额外同步一份到"
        f"异地对象存储，防止单一区域故障导致数据不可恢复。",
    ]
    return rng.choice(templates)

def acme_dev_process(rng: random.Random) -> str:
    team = rng.choice(ACME_TEAMS)
    tool = rng.choice(ACME_TOOLS)
    n = rng.choice([1, 2, 3])
    templates = [
        f"{team}的代码合并要求至少 {n} 名评审人通过，且 CI 流水线（{tool}）全部检查项为绿色"
        f"才允许合并到主分支，禁止使用 --no-verify 之类的方式跳过检查。",
        f"{team}的 Sprint 周期是两周一个迭代，迭代评审会在{tool}上登记每个任务的验收标准，"
        f"未达成验收标准的任务自动流转到下一个迭代，不计入本迭代完成率。",
        f"{team}的线上问题按严重程度分 P0-P3 四级，P0 要求 15 分钟内响应、1 小时内止血，"
        f"复盘报告需要在{tool}上归档，48 小时内完成根因分析。",
    ]
    return rng.choice(templates)

def acme_product(rng: random.Random) -> str:
    svc = rng.choice(ACME_SERVICES)
    limit = rng.choice([100, 500, 1000, 5000, 10000])
    unit = rng.choice(["QPS", "条/秒", "MB/s", "并发连接数"])
    templates = [
        f"{svc}在标准版套餐下的限流阈值是 {limit} {unit}，超出后返回 429，企业版客户可以联系"
        f"客户成功团队申请提升配额，提升生效前需要走一次容量评估。",
        f"{svc}支持通过控制台或 API 两种方式配置，控制台改动会有 5 分钟的缓存延迟才能生效，"
        f"API 改动通过 webhook 通知下游服务，延迟通常在 10 秒以内。",
        f"{svc}的免费版功能限制是最多创建 {limit // 10} 个实例，付费版本没有实例数量上限，"
        f"但单个实例的资源规格仍然受套餐等级约束。",
    ]
    return rng.choice(templates)

def acme_security(rng: random.Random) -> str:
    days = rng.choice([60, 90, 120, 150])
    team = rng.choice(ACME_TEAMS)
    retain_days = rng.choice([30, 60, 90, 180])
    templates = [
        f"{team}的域账号密码每 {days} 天强制更换一次，新密码不能与最近 5 次使用过的密码重复，"
        f"连续输错 5 次会锁定账号 30 分钟，需要联系 IT 分机解锁。",
        f"{team}访问生产环境数据库一律通过堡垒机跳转，禁止个人电脑直连，堡垒机会话全程录屏"
        f"留存 {retain_days} 天，供安全审计追溯。",
        f"{team}的笔记本电脑必须开启全盘加密，禁止在个人网盘、私人邮箱存放包含客户数据的"
        f"文件，违反者按照信息安全管理办法处理。",
        f"{team}发现钓鱼邮件应直接转发给 security@acme.internal 并删除，不要点击其中链接或"
        f"下载附件，安全团队会在 1 个工作日内确认是否需要全员预警。",
        f"{team}的员工离职当天，IT 会在 {retain_days // 10} 小时内回收其所有系统账号权限，"
        f"包括 VPN、代码仓库、内部工单系统，离职交接完成前不予提前回收。",
        f"{team}新申请生产环境权限需要直属主管 + 安全合规部双人审批，权限默认有效期 "
        f"{days} 天，到期自动回收，需要继续使用须重新申请。",
    ]
    return rng.choice(templates)

def acme_hr(rng: random.Random) -> str:
    days = rng.choice([5, 10, 15, 20])
    team = rng.choice(ACME_TEAMS)
    remote_days = rng.choice([4, 6, 8, 10])
    months = rng.choice([1, 2, 3])
    templates = [
        f"{team}的年假按司龄计算，入职满一年起每年 {days} 天，当年未休完的年假可以顺延到"
        f"次年 3 月底前用完，超期作废，不折算成工资。",
        f"{team}的远程办公需要提前一天在 OA 系统提交申请，直属主管审批后生效，每月远程"
        f"办公天数不超过 {remote_days} 天，试用期员工暂不适用。",
        f"{team}的绩效考核每半年一次，分为 S/A/B/C/D 五档，连续两次 C 档或以上一次 D 档"
        f"会触发绩效改进计划（PIP），为期 {months} 个月。",
        f"{team}员工请病假 {days} 天以内只需要提交医院诊断证明，超过 {days} 天需要额外"
        f"提交复工评估报告，由 HR 和直属主管共同确认能否复工。",
        f"{team}的调薪窗口固定在每年 4 月和 10 月，非调薪窗口期原则上不受理个人涨薪申请，"
        f"晋升导致的调薪除外。",
        f"{team}员工离职需要提前 {days * 2} 天书面通知，交接清单由直属主管确认签字后"
        f"HR 才会启动离职结算流程。",
    ]
    return rng.choice(templates)

def acme_support(rng: random.Random) -> str:
    minutes = rng.choice([15, 30, 60, 120])
    tier = rng.choice(["黄金", "白银", "青铜"])
    svc = rng.choice(ACME_SERVICES)
    templates = [
        f"{tier}套餐客户的工单响应 SLA 是 {minutes} 分钟内首次响应，客户成功团队 7x24 小时"
        f"轮班值守，超时未响应会自动升级给值班经理。",
        f"{svc}相关的客户报障工单按影响面分为单租户问题和多租户问题，多租户问题直接拉"
        f"相关服务负责人进战情室处理，不走常规工单排期。",
        f"{tier}套餐客户的{svc}故障恢复目标（RTO）是 {minutes} 分钟，超出目标需要在事后"
        f"复盘报告里说明超时原因并给出改进措施。",
        f"客户对{svc}提出的功能建议统一录入产品需求池，{tier}套餐客户的建议会在下一次"
        f"产品评审会优先讨论，但不承诺一定采纳。",
        f"{svc}出现影响{tier}套餐客户的重大故障时，客户成功团队需要在 {minutes} 分钟内"
        f"主动外呼受影响客户说明情况，不能等客户先联系。",
    ]
    return rng.choice(templates)

def acme_vendor(rng: random.Random) -> str:
    amount = rng.choice([5000, 20000, 50000, 100000])
    team = rng.choice(ACME_TEAMS)
    days = rng.choice([3, 5, 7, 10])
    templates = [
        f"{team}单笔采购金额低于 {amount} 元由部门负责人审批即可，超过 {amount} 元需要财务"
        f"负责人联签，超过 {amount * 5} 元需要 CFO 审批。",
        f"{team}引入新供应商前需要走资质审核，包括营业执照、税务登记证明、过往合作案例，"
        f"审核通过后才能进入采购系统下单，审核周期通常为 {days} 个工作日。",
        f"{team}的采购合同到期前 {days * 3} 天，系统会自动提醒负责人评估是否续约，逾期未"
        f"处理的合同自动转为待续约状态，不会自动延长。",
        f"{team}供应商发票需要在收货确认后 {days} 个工作日内提交财务系统报销，超期提交"
        f"需要额外走特批流程。",
        f"{team}对年度采购金额超过 {amount * 10} 元的核心供应商，每年至少组织一次现场"
        f"审计，评估交付质量和合规情况。",
    ]
    return rng.choice(templates)

def acme_meeting(rng: random.Random) -> str:
    team = rng.choice(ACME_TEAMS)
    week = rng.choice(["第 12 周", "第 18 周", "第 23 周", "第 31 周"])
    templates = [
        f"{team}{week}周会纪要：本周上线 2 个功能点，1 个因灰度指标异常回滚，会上决定"
        f"下周优先修复该问题再重新发布，责任人已明确并同步到工单系统。",
        f"{team}{week}周会纪要：讨论了下季度技术债清理计划，优先级最高的是老版本 API "
        f"下线，预计需要两个迭代周期完成存量客户迁移沟通。",
    ]
    return rng.choice(templates)

def acme_troubleshoot(rng: random.Random) -> str:
    svc = rng.choice(ACME_SERVICES)
    templates = [
        f"{svc}响应变慢排查步骤：先看 Grafana 面板确认是否是数据库慢查询导致，再检查"
        f"是否有异常大流量客户触发限流，最后确认是否是依赖的下游服务超时。",
        f"{svc}出现 5xx 报错排查步骤：先查最近 30 分钟的部署记录是否有变更，再看错误日志"
        f"里的堆栈是否集中在同一个模块，怀疑是变更引入的问题先回滚再定位根因。",
    ]
    return rng.choice(templates)

def acme_onboarding(rng: random.Random) -> str:
    team = rng.choice(ACME_TEAMS)
    tool = rng.choice(ACME_TOOLS)
    months = rng.choice([1, 2, 3])
    templates = [
        f"{team}新员工入职第一周需要完成：领取电脑、开通内部系统账号、阅读代码规范文档、"
        f"完成一次结对编程练习，由 mentor 全程带教。",
        f"{team}新员工入职当天由 HR 统一介绍公司制度，下午由所在部门负责人安排 1v1，明确"
        f"试用期目标和第一个月的具体任务清单。",
        f"{team}新员工需要在入职 {months} 周内完成 {tool} 权限申请和基础操作培训，未完成"
        f"培训的账号不开放生产环境访问权限。",
        f"{team}的试用期为 {months} 个月，转正需要提交转正述职报告，由 mentor 和直属主管"
        f"共同评估后决定是否通过。",
        f"{team}的 mentor 需要在新人入职后 {months * 4} 周内完成至少 4 次 1v1 沟通，"
        f"记录新人适应情况，异常情况及时上报 HRBP。",
    ]
    return rng.choice(templates)

def acme_data_governance(rng: random.Random) -> str:
    days = rng.choice([90, 180, 365])
    svc = rng.choice(ACME_SERVICES)
    templates = [
        f"{svc}的用户行为日志保留 {days} 天后自动归档到冷存储，超过 2 年的日志会被永久删除，"
        f"删除前会通知数据合规团队做最后确认。",
        f"{svc}的客户数据按敏感等级分为公开、内部、机密三级，机密数据的访问需要单独申请"
        f"权限，且访问记录会被审计日志完整记录。",
        f"{svc}收到用户的数据删除请求（GDPR/个人信息保护法）后，需要在 {days // 3} 天内"
        f"完成全部关联数据的删除，并向请求人出具删除确认回执。",
        f"{svc}导出客户数据前必须脱敏处理身份证号、手机号等 PII 字段，导出记录留存"
        f"{days} 天，供事后审计追溯是谁导出过什么数据。",
        f"{svc}的数据库字段变更涉及个人信息类型时，必须先经过数据合规团队评审，评估是否"
        f"需要更新隐私政策告知用户。",
    ]
    return rng.choice(templates)

ACME_CATEGORIES = {
    "cloud_infra": ("云基础设施规范", acme_cloud_infra),
    "dev_process": ("研发流程规范", acme_dev_process),
    "product": ("产品功能说明", acme_product),
    "security": ("信息安全政策", acme_security),
    "hr": ("人力资源制度", acme_hr),
    "support": ("客户支持流程", acme_support),
    "vendor": ("供应商与采购制度", acme_vendor),
    "meeting": ("会议纪要摘要", acme_meeting),
    "troubleshoot": ("常见故障排查", acme_troubleshoot),
    "onboarding": ("新员工入职指南", acme_onboarding),
    "data_governance": ("数据治理规范", acme_data_governance),
}


# ============================================================
# Globex 环球集团（跨境物流集团）
# ============================================================

GLOBEX_HUBS = ["上海枢纽", "深圳枢纽", "宁波枢纽", "青岛枢纽", "天津枢纽", "厦门枢纽"]
GLOBEX_VEHICLES = ["重型卡车", "厢式货车", "冷链车", "集装箱拖车", "叉车"]
GLOBEX_TEAMS = ["车队管理部", "仓储运营部", "报关合规部", "供应链协调部", "安全生产部", "客户服务部"]

def globex_fleet(rng: random.Random) -> str:
    vehicle = rng.choice(GLOBEX_VEHICLES)
    hub = rng.choice(GLOBEX_HUBS)
    km = rng.choice([5000, 8000, 10000, 15000])
    templates = [
        f"{hub}的{vehicle}每行驶 {km} 公里必须进行一次例行保养，保养记录录入车队管理系统，"
        f"逾期未保养的车辆系统会自动锁定派单权限。",
        f"{vehicle}驾驶员必须持有对应准驾车型的驾驶证和从业资格证，证件到期前 30 天系统"
        f"会自动提醒车队管理部安排复审，证件过期一律禁止派单。",
        f"{hub}的{vehicle}燃油消耗超过历史均值 15% 会触发异常预警，需要车队管理部核实"
        f"是否存在跑冒滴漏或驾驶习惯问题。",
    ]
    return rng.choice(templates)

def globex_warehouse(rng: random.Random) -> str:
    hub = rng.choice(GLOBEX_HUBS)
    pct = rng.choice([1, 2, 3, 5])
    templates = [
        f"{hub}仓库每月进行一次全盘点，库存差异率超过 {pct}% 需要仓储运营部出具书面说明，"
        f"连续两个月超标会触发专项审计。",
        f"{hub}仓库叉车作业区域地面划有明确标线，非持证人员不得操作叉车，违规操作按安全"
        f"生产规范处以警告直至解除劳动合同。",
        f"{hub}仓库月台装卸货优先级按照货物类型排序：冷链＞危险品＞普通货物，同类型按照"
        f"预约时间先后顺序装卸。",
    ]
    return rng.choice(templates)

def globex_customs(rng: random.Random) -> str:
    days = rng.choice([3, 5, 7, 10])
    hub = rng.choice(GLOBEX_HUBS)
    templates = [
        f"{hub}出口货物需要在发货前 {days} 个工作日完成报关资料准备，缺少任一必要单据"
        f"（发票、装箱单、原产地证）报关合规部有权拒绝受理。",
        f"{hub}的 HS 编码归类错误是报关最常见的问题，报关合规部要求每票新品类货物在首次"
        f"出运前完成归类预审，避免清关延误产生滞港费。",
        f"{hub}涉及危险品运输的货物必须提供 MSDS（安全数据表），未提供的一律不予受理"
        f"出运申请，这是硬性合规红线，不接受任何例外。",
        f"{hub}进口货物的关税预缴款需要在货物到港前 {days} 天完成，逾期会产生滞纳金，"
        f"由报关合规部统一跟进缴纳进度。",
        f"{hub}遇到海关查验的货物，报关合规部需要在 {days} 个工作日内配合完成查验，"
        f"查验产生的仓储费由货主和承运方按合同约定分摊。",
        f"{hub}出口退税申报材料需要在报关单放行后 {days * 2} 天内提交税务系统，逾期"
        f"申报会影响当月退税进度。",
    ]
    return rng.choice(templates)

def globex_supply_chain(rng: random.Random) -> str:
    days = rng.choice([2, 3, 5, 7])
    hub = rng.choice(GLOBEX_HUBS)
    templates = [
        f"{hub}的供应商交货延迟超过 {days} 天，供应链协调部需要在系统里登记延迟原因并"
        f"同步给客户服务部，避免客户在不知情的情况下投诉。",
        f"{hub}的跨境运输时效承诺以海运/空运两种方式区分，海运延误超过 {days} 天视为"
        f"异常，需要启动备用航线预案，空运延误超过 1 天即触发预案。",
        f"{hub}的旺季运力储备需要提前 {days * 10} 天与外协车队/仓库签订临时协议，避免"
        f"旺季爆仓导致货物积压。",
        f"{hub}的供应商绩效每季度评估一次，连续 {days // 2 + 1} 次交货延迟的供应商会被"
        f"列入观察名单，情节严重的取消合作资格。",
        f"{hub}遇到供应链中断（如上游工厂停产）时，供应链协调部需要在 {days} 天内评估"
        f"备选供应商方案，避免影响下游客户交付。",
    ]
    return rng.choice(templates)

def globex_safety(rng: random.Random) -> str:
    hub = rng.choice(GLOBEX_HUBS)
    hours = rng.choice([1, 2, 4])
    templates = [
        f"{hub}进入仓库作业区域必须佩戴安全帽和反光背心，未按规定佩戴劳保用品的员工不得"
        f"进入作业区，班组长负责现场检查。",
        f"{hub}危险品仓储区域禁止明火和吸烟，消防器材每月由安全生产部巡检一次，巡检记录"
        f"留档两年备查。",
        f"{hub}发生工伤事故必须在 {hours} 小时内上报安全生产部，24 小时内完成事故初步"
        f"调查报告，隐瞒不报按严重违纪处理。",
        f"{hub}每季度组织一次消防应急演练，全员参与率要求达到 100%，缺席人员需要在"
        f"{hours} 周内补训并通过考核。",
        f"{hub}的叉车、装卸设备每日班前必须完成安全检查清单，发现异常立即停用报修，"
        f"不得带故障作业。",
        f"{hub}的安全生产月度检查发现的隐患，责任部门必须在 {hours * 3} 天内完成整改，"
        f"逾期未整改上报集团安全委员会。",
    ]
    return rng.choice(templates)

def globex_hr(rng: random.Random) -> str:
    hours = rng.choice([1.5, 2, 2.5, 3])
    hub = rng.choice(GLOBEX_HUBS)
    days = rng.choice([5, 8, 10])
    templates = [
        f"{hub}驾驶员超过法定工时的加班按 {hours} 倍工资计算，连续驾驶不得超过 4 小时，"
        f"必须休息至少 20 分钟才能继续上路，这是安全红线不容突破。",
        f"{hub}仓库员工实行三班倒排班制，换班前必须完成交接记录，交接不清导致的库存"
        f"差异由当班责任人承担。",
        f"{hub}驾驶员年假按驾龄计算，满 3 年起每年增加 1 天，最多累计到 {days} 天，"
        f"旺季（11-1 月）原则上不受理年假申请。",
        f"{hub}仓库新员工入职前 {days} 天为岗前培训期，培训期工资按当地最低工资标准"
        f"发放，培训合格后转为正式排班。",
        f"{hub}员工因工受伤需要休养的，休养期间按 {hours} 倍基本工资的比例发放病假"
        f"工资，具体天数以医院诊断证明为准。",
    ]
    return rng.choice(templates)

def globex_customer_service(rng: random.Random) -> str:
    hours = rng.choice([2, 4, 8, 12])
    hub = rng.choice(GLOBEX_HUBS)
    templates = [
        f"{hub}客户投诉货物破损需要在 {hours} 小时内响应，客户服务部联系仓储运营部核实"
        f"破损发生环节，属于运输环节的走保险理赔流程。",
        f"{hub}客户查询在途货物状态可以通过小程序自助查询，系统每 {hours} 小时同步一次"
        f"最新物流节点，人工客服只处理自助查询无法覆盖的异常件。",
        f"{hub}客户要求变更收货地址，需要在货物发出后 {hours} 小时内提出，超过时限的"
        f"变更申请需要额外支付改派费用。",
        f"{hub}大客户的专属客服经理每月需要主动回访一次，收集服务体验反馈并录入 CRM "
        f"系统，作为季度合作评估的一部分。",
        f"{hub}客户对时效不满的投诉，客户服务部需要在 {hours} 小时内给出书面说明，"
        f"涉及赔付的转交财务部按合同条款核算。",
    ]
    return rng.choice(templates)

def globex_procurement(rng: random.Random) -> str:
    amount = rng.choice([50000, 200000, 500000, 1000000])
    hub = rng.choice(GLOBEX_HUBS)
    vehicle = rng.choice(GLOBEX_VEHICLES)
    templates = [
        f"{hub}采购新{vehicle}单笔金额超过 {amount} 元必须走三家以上供应商比价流程，"
        f"比价记录留存供审计部门核查。",
        f"{hub}的{vehicle}维修保养定点供应商每年重新评估一次资质，评估不合格的供应商"
        f"取消定点资格，改由车队管理部重新招标。",
        f"{hub}租赁{vehicle}的年度预算超过 {amount} 元需要总部审批，日常小额租赁"
        f"（应急用车）由枢纽负责人自行审批。",
        f"{hub}的{vehicle}报废需要经过安全生产部技术评估，评估通过后走资产处置流程，"
        f"处置所得归还财务部统一入账。",
        f"{hub}新增{vehicle}采购需要提前一个季度纳入年度预算计划，临时追加采购超过"
        f"{amount} 元需要 CFO 特批。",
    ]
    return rng.choice(templates)

def globex_meeting(rng: random.Random) -> str:
    team = rng.choice(GLOBEX_TEAMS)
    week = rng.choice(["第 9 周", "第 16 周", "第 22 周", "第 29 周"])
    templates = [
        f"{team}{week}运营会纪要：本周有 3 单出现延迟交付，其中 2 单是天气原因，1 单是"
        f"车辆故障，会上决定给该车队增加一台备用车辆。",
        f"{team}{week}运营会纪要：讨论了旺季运力储备方案，预计需要提前一个月与外协车队"
        f"签订临时运力协议，避免旺季爆仓。",
    ]
    return rng.choice(templates)

def globex_troubleshoot(rng: random.Random) -> str:
    hub = rng.choice(GLOBEX_HUBS)
    hours = rng.choice([1, 2, 3, 4])
    templates = [
        f"{hub}货物在途异常排查步骤：先查车载 GPS 定位确认车辆实际位置，再联系驾驶员"
        f"确认是否遇到交通管制或车辆故障，超过 {hours} 小时无法恢复行驶需要启动备用"
        f"车辆调度。",
        f"{hub}仓库库存对不上排查步骤：先核对最近一次盘点记录和出入库单据是否一致，再"
        f"检查是否存在未及时录入系统的手工出库记录，最后确认是否发生盘点期间的并发操作。",
        f"{hub}货物破损投诉排查步骤：先调取装卸货现场监控确认破损发生环节，再核对装车"
        f"前验货记录，{hours} 个工作日内出具责任认定报告。",
        f"{hub}报关延误排查步骤：先确认是资料缺失还是海关查验导致，资料问题联系客户"
        f"补件，查验问题联系报关合规部现场跟进，预计延误超过 {hours} 天要提前通知客户。",
        f"{hub}系统单据与实际库存不一致排查步骤：先确认是否是跨枢纽调拨在途未确认收货，"
        f"再检查是否存在重复录入，{hours} 小时内无法定位原因要上报仓储运营部人工盘点。",
    ]
    return rng.choice(templates)

def globex_onboarding(rng: random.Random) -> str:
    team = rng.choice(GLOBEX_TEAMS)
    hub = rng.choice(GLOBEX_HUBS)
    months = rng.choice([1, 2, 3])
    templates = [
        f"{hub}{team}新员工入职需要完成安全生产培训并考核合格才能上岗，涉及特种作业"
        f"（叉车、危险品）的岗位额外需要取得对应操作证。",
        f"{hub}驾驶员入职需要提供近三年无重大交通事故证明，入职后第一个月由资深驾驶员"
        f"带教，考核合格才能独立执行长途运输任务。",
        f"{hub}{team}新员工试用期为 {months} 个月，试用期内由带教师傅每周评估一次工作"
        f"表现，评估结果录入 HR 系统。",
        f"{hub}仓库新员工入职需要先通过消防安全和设备操作两项考核，未通过考核不得单独"
        f"上岗，可申请补考一次。",
        f"{hub}{team}新员工入职 {months} 周内需要完成集团合规培训（含反腐败、数据保护"
        f"基础），未完成培训的账号会被暂停系统访问权限。",
    ]
    return rng.choice(templates)

GLOBEX_CATEGORIES = {
    "fleet": ("车队运营规范", globex_fleet),
    "warehouse": ("仓储管理制度", globex_warehouse),
    "customs": ("报关与合规流程", globex_customs),
    "supply_chain": ("供应链协调流程", globex_supply_chain),
    "safety": ("安全生产规范", globex_safety),
    "hr": ("人力资源制度", globex_hr),
    "customer_service": ("客户服务流程", globex_customer_service),
    "procurement": ("车辆与设备采购制度", globex_procurement),
    "meeting": ("会议纪要摘要", globex_meeting),
    "troubleshoot": ("常见运营问题排查", globex_troubleshoot),
    "onboarding": ("新员工入职指南", globex_onboarding),
}


def generate_tenant(tenant_name: str, categories: dict) -> int:
    base_dir = REPO_ROOT / "data" / "tenant_demo" / tenant_name / "kb_corpus"
    rng = random.Random(f"{SEED}:{tenant_name}")
    total = 0
    for slug, (label, template_fn) in categories.items():
        entries = _sample_entries(template_fn, ENTRIES_PER_CATEGORY, rng)
        n = _write_category(base_dir, slug, entries)
        total += n
        print(f"  [{tenant_name}] {label} ({slug}): {n} 条")
    return total


def main() -> None:
    print("生成 Acme 知识库语料...")
    acme_total = generate_tenant("acme", ACME_CATEGORIES)
    print(f"Acme 共 {acme_total} 条（不含已有的 it_reimbursement.txt）\n")

    print("生成 Globex 知识库语料...")
    globex_total = generate_tenant("globex", GLOBEX_CATEGORIES)
    print(f"Globex 共 {globex_total} 条（不含已有的 travel_reimbursement.txt）")


if __name__ == "__main__":
    main()
