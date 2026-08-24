"""生成 Acme / Globex 两家模拟企业的知识库语料——2026-08-22 第二版：改成跟
平台之前那 6 个本地部门知识库同一套分类（人力资源与行政 / 财务与报销制度 /
IT 支持与技术运维 / 销售话术与市场 / 研发与产品代码 / 客户成功与售后服务，
见 query_knowledge_hub.py DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES），不再各自
用一套自定义分类（老版本 Acme 是 troubleshoot/security/product/...，Globex
是 fleet/warehouse/customs/... 共 11 个，互不相同）。统一成这 6 个之后，
委托企业的知识库分组过滤（hr_admin_kb 分组只看得到"人力资源与行政"类目，
finance_kb 分组只看得到"财务与报销制度"类目……）才能对两家企业用同一套
分组名生效，不需要为每家企业单独维护一份类目映射。

Acme 依然是云软件/SaaS 公司人设，Globex 依然是跨境物流集团人设——两家在
"财务/销售"这类通用部门上内容自然会有些相似（毕竟都是公司），但"研发与
产品代码""客户成功"这类内容会按各自业务差异明显区分开（Acme 的研发是
微服务/API，Globex 的"研发"是物流调度系统/路由优化算法）。

每条独立写成一个小文件（几百字，远小于 chunk_size=1000），保证摄入后
"一个文件 ≈ 一个 chunk"，条数数得清楚，不依赖切块器怎么切。

用法：
    python scripts/generate_tenant_kb_corpus.py
"""

from __future__ import annotations

import random
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ENTRIES_PER_CATEGORY = 20
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
ACME_TEAMS = ["平台研发部", "增长研发部", "数据研发部", "客户成功部", "IT 运维部", "安全合规部"]
ACME_TOOLS = ["GitLab", "Jenkins", "ArgoCD", "Prometheus", "Grafana", "Jira", "Confluence"]
ACME_COMPETITORS = ["竞品 A", "竞品 B", "行业老牌厂商"]

def acme_hr_admin(rng: random.Random) -> str:
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
        f"{team}员工离职需要提前 {days * 2} 天书面通知，交接清单由直属主管确认签字后"
        f"HR 才会启动离职结算流程。",
        f"{team}新员工入职第一周需要完成：领取电脑、开通内部系统账号、阅读代码规范文档、"
        f"完成一次结对编程练习，由 mentor 全程带教。",
        f"{team}新员工需要在入职 {months} 周内完成 {rng.choice(ACME_TOOLS)} 权限申请和"
        f"基础操作培训，未完成培训的账号不开放生产环境访问权限。",
        f"{team}的试用期为 {months + 2} 个月，转正需要提交转正述职报告，由 mentor 和"
        f"直属主管共同评估后决定是否通过。",
        f"公司行政通过 OA 系统「会议室预订」模块统一管理会议室，预约后超过 15 分钟未"
        f"签到会自动释放给候补预约人。",
        f"{team}员工办公用品（键盘鼠标、显示器等）领用需在 OA 系统提交申请，行政审批后"
        f"两个工作日内配发，损坏丢失需自行说明原因。",
    ]
    return rng.choice(templates)

def acme_finance(rng: random.Random) -> str:
    amount = rng.choice([500, 1000, 2000, 5000])
    team = rng.choice(ACME_TEAMS)
    days = rng.choice([3, 5, 7, 10])
    limit = rng.choice([5000, 20000, 50000])
    templates = [
        f"{team}员工日常报销单笔超过 {amount} 元需要直属主管审批，超过 {amount * 4} 元"
        f"需要财务负责人联签，发票必须是增值税普通发票或专用发票，个人抬头一律不予报销。",
        f"{team}差旅报销需在出差结束后 {days} 个工作日内在 OA 系统提交，附行程单和发票"
        f"原件，逾期超过 30 天未提交财务有权拒绝报销。",
        f"{team}招待费报销必须注明陪同人员姓名及事由，单次超过 {amount} 元需附上级"
        f"审批意见，超过 {amount * 2} 元需要财务负责人额外审批。",
        f"{team}单笔采购金额低于 {limit} 元由部门负责人审批即可，超过 {limit} 元需要"
        f"财务负责人联签，超过 {limit * 5} 元需要 CFO 审批。",
        f"{team}供应商发票需要在收货确认后 {days} 个工作日内提交财务系统报销，超期"
        f"提交需要额外走特批流程，不接受口头承诺补交。",
        f"{team}的采购合同到期前 {days * 3} 天，系统会自动提醒负责人评估是否续约，"
        f"逾期未处理的合同自动转为待续约状态，不会自动延长。",
        f"{team}引入新供应商前需要走资质审核，包括营业执照、税务登记证明、过往合作"
        f"案例，审核周期通常为 {days} 个工作日。",
        f"{team}的预算按季度下达，单季度超支超过 10% 需要提交书面说明，连续两个季度"
        f"超支的部门下季度预算会被收紧。",
        f"公司年度审计前，各部门需要在 {days * 2} 个工作日内完成本部门费用凭证的自查"
        f"归档，缺失凭证的支出可能被追回。",
    ]
    return rng.choice(templates)

def acme_it_support(rng: random.Random) -> str:
    svc = rng.choice(ACME_SERVICES)
    days = rng.choice([60, 90, 120, 150])
    team = rng.choice(ACME_TEAMS)
    retain_days = rng.choice([30, 60, 90, 180])
    templates = [
        f"{team}的域账号密码每 {days} 天强制更换一次，新密码不能与最近 5 次使用过的"
        f"密码重复，连续输错 5 次会锁定账号 30 分钟，需要联系 IT 分机解锁。",
        f"{team}访问生产环境数据库一律通过堡垒机跳转，禁止个人电脑直连，堡垒机会话"
        f"全程录屏留存 {retain_days} 天，供安全审计追溯。",
        f"新员工入职电脑、显示器由 IT 统一配发，入职当天可在前台领取；额外外设需在"
        f"IT 门户提交工单，审批后 2 个工作日内发放。",
        f"发现钓鱼邮件应直接转发给 security@acme.internal 并删除，不要点击其中链接"
        f"或下载附件，安全团队会在 1 个工作日内确认是否需要全员预警。",
        f"{svc}响应变慢排查步骤：先看 Grafana 面板确认是否是数据库慢查询导致，再"
        f"检查是否有异常大流量客户触发限流，最后确认是否是依赖的下游服务超时。",
        f"{svc}出现 5xx 报错排查步骤：先查最近 30 分钟的部署记录是否有变更，再看"
        f"错误日志里的堆栈是否集中在同一个模块，怀疑是变更引入的问题先回滚再定位根因。",
        f"VPN 连不上排查步骤：先确认本地网络是否正常，再检查证书是否过期（每年需要"
        f"手动续期一次），仍无法解决联系 IT 分机 8801。",
        f"{team}员工离职当天，IT 会在 {retain_days // 10} 小时内回收其所有系统账号"
        f"权限，包括 VPN、代码仓库、内部工单系统，离职交接完成前不予提前回收。",
        f"{svc}的数据库备份策略是每日全量 + 每小时增量，保留 30 天，异地对象存储"
        f"额外同步一份，防止单一区域故障导致数据不可恢复。",
    ]
    return rng.choice(templates)

def acme_sales_marketing(rng: random.Random) -> str:
    pct = rng.choice([5, 10, 15])
    competitor = rng.choice(ACME_COMPETITORS)
    amount = rng.choice([10000, 30000, 50000])
    svc = rng.choice(ACME_SERVICES)
    templates = [
        f"标准报价单折扣权限：销售代表最高可给 {pct}% 折扣，超出需要销售总监审批，"
        f"超过 {pct * 2}% 需要 VP 特批，年框合同另有单独折扣政策。",
        f"客户异议处理话术：遇到「价格太贵」类问题，先确认对方对比的是{competitor}还是"
        f"其他产品，再突出差异化功能，不要第一时间就提折扣。",
        f"客户提出「{competitor}功能更全」时的应对话术：承认对方在某些细分场景确实"
        f"有优势，转而强调我们在集成成本和服务响应速度上的差异。",
        f"参展/路演费用需提前两周提交预算申请，单场活动预算超过 {amount} 元需要市场"
        f"总监审核通过后才能对外确认参展信息。",
        f"营销活动的获客成本超过 {amount // 10} 元/线索需要暂停投放并复盘，市场部"
        f"每周汇总投放数据到 OA 系统供销售团队参考跟进优先级。",
        f"{svc}的销售演示统一使用标准 Demo 环境账号，禁止用客户生产数据做演示，"
        f"演示环境数据每周一自动重置。",
        f"新客户签约后，销售需要在 3 个工作日内完成客户信息交接给客户成功团队，"
        f"交接内容包括合同关键条款、客户诉求和后续跟进计划。",
        f"续约谈判前，销售需要先查看客户过去一年的产品使用数据，续约折扣跟使用率"
        f"挂钩，使用率低于 40% 的客户优先安排客户成功团队介入而非直接报价。",
        f"公司官网线索需在 {rng.choice([2, 4, 8])} 小时内由销售团队跟进联系，超时"
        f"未跟进的线索会被系统自动分配给下一位空闲销售代表。",
    ]
    return rng.choice(templates)

def acme_rd_product(rng: random.Random) -> str:
    team = rng.choice(ACME_TEAMS)
    tool = rng.choice(ACME_TOOLS)
    n = rng.choice([1, 2, 3])
    svc = rng.choice(ACME_SERVICES)
    limit = rng.choice([100, 500, 1000, 5000])
    days = rng.choice([90, 180, 365])
    templates = [
        f"{team}的代码合并要求至少 {n} 名评审人通过，且 CI 流水线（{tool}）全部检查项"
        f"为绿色才允许合并到主分支，禁止使用 --no-verify 之类的方式跳过检查。",
        f"{team}的 Sprint 周期是两周一个迭代，迭代评审会在 {tool} 上登记每个任务的"
        f"验收标准，未达成验收标准的任务自动流转到下一个迭代，不计入本迭代完成率。",
        f"{team}的线上问题按严重程度分 P0-P3 四级，P0 要求 15 分钟内响应、1 小时内"
        f"止血，复盘报告需要在 {tool} 上归档，48 小时内完成根因分析。",
        f"{svc}在标准版套餐下的限流阈值是 {limit} QPS，超出后返回 429，企业版客户"
        f"可以联系客户成功团队申请提升配额，提升生效前需要走一次容量评估。",
        f"{svc}支持通过控制台或 API 两种方式配置，控制台改动会有 5 分钟的缓存延迟"
        f"才能生效，API 改动通过 webhook 通知下游服务，延迟通常在 10 秒以内。",
        f"{svc}的用户行为日志保留 {days} 天后自动归档到冷存储，超过 2 年的日志会被"
        f"永久删除，删除前会通知数据合规团队做最后确认。",
        f"{svc}的客户数据按敏感等级分为公开、内部、机密三级，机密数据的访问需要"
        f"单独申请权限，且访问记录会被审计日志完整记录。",
        f"{svc}的数据库字段变更涉及个人信息类型时，必须先经过数据合规团队评审，"
        f"评估是否需要更新隐私政策告知用户。",
        f"产品需求评审会每周二下午举行，所有新功能必须先出低保真原型，经评审通过"
        f"后才能进入 {team} 的开发排期，紧急需求需产品负责人特批插队。",
    ]
    return rng.choice(templates)

def acme_customer_success(rng: random.Random) -> str:
    minutes = rng.choice([15, 30, 60, 120])
    tier = rng.choice(["黄金", "白银", "青铜"])
    svc = rng.choice(ACME_SERVICES)
    templates = [
        f"{tier}套餐客户的工单响应 SLA 是 {minutes} 分钟内首次响应，客户成功团队"
        f"7x24 小时轮班值守，超时未响应会自动升级给值班经理。",
        f"{svc}相关的客户报障工单按影响面分为单租户问题和多租户问题，多租户问题"
        f"直接拉相关服务负责人进战情室处理，不走常规工单排期。",
        f"{tier}套餐客户的{svc}故障恢复目标（RTO）是 {minutes} 分钟，超出目标需要"
        f"在事后复盘报告里说明超时原因并给出改进措施。",
        f"客户对{svc}提出的功能建议统一录入产品需求池，{tier}套餐客户的建议会在"
        f"下一次产品评审会优先讨论，但不承诺一定采纳。",
        f"{svc}出现影响{tier}套餐客户的重大故障时，客户成功团队需要在 {minutes} "
        f"分钟内主动外呼受影响客户说明情况，不能等客户先联系。",
        f"客户满意度回访每季度一次，NPS 低于 6 分的客户需要标记为重点关注对象，"
        f"客户成功经理需在两周内制定挽留计划。",
        f"客户提出退款申请需在 {minutes // 10} 个工作日内给出初步答复，符合退款"
        f"条款的按合同约定周期原路退回，不符合的需说明具体条款依据。",
        f"{tier}套餐客户续约前一个月，客户成功经理需要完成一次业务回顾会，汇总"
        f"使用数据和 ROI，作为续约谈判的支撑材料。",
    ]
    return rng.choice(templates)

ACME_CATEGORIES = {
    "hr_admin": ("人力资源与行政", acme_hr_admin),
    "finance": ("财务与报销制度", acme_finance),
    "it_support": ("IT 支持与技术运维", acme_it_support),
    "sales_marketing": ("销售话术与市场", acme_sales_marketing),
    "rd_product": ("研发与产品代码", acme_rd_product),
    "customer_success": ("客户成功与售后服务", acme_customer_success),
}


# ============================================================
# Globex 环球集团（跨境物流集团）
# ============================================================

GLOBEX_HUBS = ["上海枢纽", "深圳枢纽", "宁波枢纽", "青岛枢纽", "天津枢纽", "厦门枢纽"]
GLOBEX_VEHICLES = ["重型卡车", "厢式货车", "冷链车", "集装箱拖车", "叉车"]
GLOBEX_TEAMS = ["车队管理部", "仓储运营部", "报关合规部", "供应链协调部", "安全生产部", "客户服务部"]

def globex_hr_admin(rng: random.Random) -> str:
    hours = rng.choice([1.5, 2, 2.5, 3])
    hub = rng.choice(GLOBEX_HUBS)
    days = rng.choice([5, 8, 10])
    templates = [
        f"{hub}驾驶员超过法定工时的加班按 {hours} 倍工资计算，连续驾驶不得超过 4 "
        f"小时，必须休息至少 20 分钟才能继续上路，这是安全红线不容突破。",
        f"{hub}仓库员工实行三班倒排班制，换班前必须完成交接记录，交接不清导致的"
        f"库存差异由当班责任人承担。",
        f"{hub}驾驶员年假按驾龄计算，满 3 年起每年增加 1 天，最多累计到 {days} "
        f"天，旺季（11-1 月）原则上不受理年假申请。",
        f"{hub}仓库新员工入职前 {days} 天为岗前培训期，培训期工资按当地最低工资"
        f"标准发放，培训合格后转为正式排班。",
        f"{hub}员工因工受伤需要休养的，休养期间按 {hours} 倍基本工资的比例发放"
        f"病假工资，具体天数以医院诊断证明为准。",
        f"{hub}新员工入职需要完成安全生产培训并考核合格才能上岗，涉及特种作业"
        f"（叉车、危险品）的岗位额外需要取得对应操作证。",
        f"{hub}驾驶员入职需要提供近三年无重大交通事故证明，入职后第一个月由资深"
        f"驾驶员带教，考核合格才能独立执行长途运输任务。",
        f"{hub}进入仓库作业区域必须佩戴安全帽和反光背心，未按规定佩戴劳保用品的"
        f"员工不得进入作业区，班组长负责现场检查。",
        f"总部行政通过 OA 系统统一管理会议室预订，各枢纽视频会议室使用需提前一天"
        f"在系统登记，避免跟其他部门时段冲突。",
    ]
    return rng.choice(templates)

def globex_finance(rng: random.Random) -> str:
    days = rng.choice([3, 5, 7, 10])
    hub = rng.choice(GLOBEX_HUBS)
    amount = rng.choice([50000, 200000, 500000])
    vehicle = rng.choice(GLOBEX_VEHICLES)
    templates = [
        f"{hub}的运费结算周期为月结，客户对账单有异议需在收到账单后 {days} 个"
        f"工作日内提出，逾期视为确认账单无异议。",
        f"{hub}驾驶员油费、过路费报销需附电子发票和行车轨迹截图，单笔超过 "
        f"{days * 200} 元需要车队管理部负责人审批。",
        f"{hub}货物破损理赔涉及保险公司的，财务部需在责任认定报告出具后 "
        f"{days} 个工作日内提交理赔材料，避免超出保险公司受理时限。",
        f"{hub}采购新{vehicle}单笔金额超过 {amount} 元必须走三家以上供应商比价"
        f"流程，比价记录留存供审计部门核查。",
        f"{hub}租赁{vehicle}的年度预算超过 {amount} 元需要总部审批，日常小额"
        f"租赁（应急用车）由枢纽负责人自行审批。",
        f"{hub}进口货物的关税预缴款需要在货物到港前 {days} 天完成，逾期会产生"
        f"滞纳金，由财务部统一跟进缴纳进度。",
        f"{hub}出口退税申报材料需要在报关单放行后 {days * 2} 天内提交税务系统，"
        f"逾期申报会影响当月退税进度。",
        f"大客户信用账期最长 {days * 3} 天，超期未付款的客户会被系统自动暂停"
        f"新订单受理，需财务部人工解除限制才能恢复下单。",
        f"{hub}的{vehicle}报废需要经过安全生产部技术评估，评估通过后走资产"
        f"处置流程，处置所得归还财务部统一入账。",
    ]
    return rng.choice(templates)

def globex_it_support(rng: random.Random) -> str:
    hub = rng.choice(GLOBEX_HUBS)
    hours = rng.choice([1, 2, 3, 4])
    days = rng.choice([60, 90])
    templates = [
        f"{hub}仓库管理系统（WMS）账号密码每 {days} 天强制更换一次，忘记密码"
        f"联系 IT 支持热线现场或电话重置，不接受邮件申请重置。",
        f"车辆 GPS 定位系统离线排查步骤：先确认车载终端电源是否正常，再检查"
        f"SIM 卡信号，{hours} 小时内无法恢复需要联系设备厂商上门维修。",
        f"{hub}货物在途异常排查步骤：先查车载 GPS 定位确认车辆实际位置，再"
        f"联系驾驶员确认是否遇到交通管制或车辆故障，超过 {hours} 小时无法恢复"
        f"行驶需要启动备用车辆调度。",
        f"{hub}仓库库存对不上排查步骤：先核对最近一次盘点记录和出入库单据是否"
        f"一致，再检查是否存在未及时录入系统的手工出库记录，最后确认是否发生"
        f"盘点期间的并发操作。",
        f"运输管理系统（TMS）派单失败排查步骤：先确认车辆状态是否为可用，再"
        f"检查是否跟其他订单时段冲突，{hours} 分钟内无法定位联系系统支持组。",
        f"{hub}报关系统单据与实际库存不一致排查步骤：先确认是否是跨枢纽调拨"
        f"在途未确认收货，再检查是否存在重复录入，{hours} 小时内无法定位原因"
        f"要上报系统支持组人工核查。",
        f"新员工入职需要 IT 支持组开通 WMS/TMS 账号并分配对应枢纽权限，账号"
        f"开通后需当天完成系统操作基础培训才能上岗使用。",
        f"{hub}的手持扫描枪批量离线故障排查步骤：先确认仓库 WiFi 信号覆盖，"
        f"再检查设备固件版本是否为最新，仍无法连接需联系 IT 支持组现场处理。",
    ]
    return rng.choice(templates)

def globex_sales_marketing(rng: random.Random) -> str:
    hub = rng.choice(GLOBEX_HUBS)
    pct = rng.choice([5, 8, 12])
    amount = rng.choice([100000, 500000, 1000000])
    templates = [
        f"{hub}新客户首年运费报价折扣权限：销售最高可给 {pct}% 折扣，超出需要"
        f"区域销售总监审批，年运费承诺超过 {amount} 元的大客户合同需总部特批。",
        f"客户提出「同行报价更低」时的应对话术：先确认对方比较的是否包含同等"
        f"时效和保险条款，再强调准点率和货损理赔响应速度的差异化优势。",
        f"大客户合同谈判需要销售、法务、财务三方会签，合同期限超过 2 年的"
        f"需要总部商务负责人参与最终谈判。",
        f"{hub}区域市场拓展的年度预算需提前一个季度提交总部审批，单场行业展会"
        f"预算超过 {amount // 10} 元需要市场部负责人现场审核通过。",
        f"新签约客户需在 3 个工作日内由销售完成与客户服务部的交接，交接内容"
        f"包括时效承诺、特殊操作要求和客户对接人信息。",
        f"续约谈判前，销售需要先调取客户过去一年的准点率和投诉记录，服务质量"
        f"不达标的客户优先安排客户服务经理介入沟通，而非直接报价续约。",
        f"官网询价线索需在 {rng.choice([2, 4, 6])} 小时内由销售跟进回复，超时"
        f"未跟进的线索会被系统自动分配给下一位空闲销售代表。",
    ]
    return rng.choice(templates)

def globex_rd_product(rng: random.Random) -> str:
    hub = rng.choice(GLOBEX_HUBS)
    pct = rng.choice([1, 2, 3, 5])
    team = rng.choice(GLOBEX_TEAMS)
    templates = [
        f"路由优化算法迭代前需要在历史订单数据上跑离线回测，新算法相比旧版本"
        f"平均配送时长必须下降至少 {pct}% 才允许灰度上线到单个枢纽试运行。",
        f"WMS 系统新功能上线前需要先在测试枢纽跑满两周，库存差异率控制在 "
        f"{pct}% 以内才能全量推广到所有枢纽，超标需要回滚排查。",
        f"{hub}参与 TMS 系统新功能的用户验收测试（UAT），{team}提出的需求"
        f"变更必须走产品评审会，紧急运营问题可以直接找系统支持组开临时工单。",
        f"物流平台的需求评审会每两周举行一次，新功能上线前必须先出低保真"
        f"原型和验收标准，未达成验收标准的功能延期到下一轮迭代。",
        f"车辆调度算法的数据来源涉及驾驶员位置信息，属于个人信息范畴，接入"
        f"新数据源前必须经过数据合规团队评审。",
        f"平台系统的订单数据保留 3 年后自动归档到冷存储，客户主动要求删除"
        f"个人信息的，需在 15 个工作日内完成关联数据清理并出具回执。",
        f"{hub}提出的系统功能改进建议统一录入产品需求池，涉及多个枢纽共性"
        f"问题的需求优先排期，单一枢纽的特殊需求评估投入产出比后再决定。",
    ]
    return rng.choice(templates)

def globex_customer_service(rng: random.Random) -> str:
    hours = rng.choice([2, 4, 8, 12])
    hub = rng.choice(GLOBEX_HUBS)
    templates = [
        f"{hub}客户投诉货物破损需要在 {hours} 小时内响应，客户服务部联系仓储"
        f"运营部核实破损发生环节，属于运输环节的走保险理赔流程。",
        f"{hub}客户查询在途货物状态可以通过小程序自助查询，系统每 {hours} "
        f"小时同步一次最新物流节点，人工客服只处理自助查询无法覆盖的异常件。",
        f"{hub}客户要求变更收货地址，需要在货物发出后 {hours} 小时内提出，"
        f"超过时限的变更申请需要额外支付改派费用。",
        f"{hub}大客户的专属客服经理每月需要主动回访一次，收集服务体验反馈"
        f"并录入 CRM 系统，作为季度合作评估的一部分。",
        f"{hub}客户对时效不满的投诉，客户服务部需要在 {hours} 小时内给出"
        f"书面说明，涉及赔付的转交财务部按合同条款核算。",
        f"客户满意度回访每季度一次，NPS 低于 6 分的客户需要标记为重点关注"
        f"对象，客户服务经理需在两周内制定挽留计划。",
        f"{hub}报关延误导致客户投诉的，客户服务部需在 {hours // 2} 小时内"
        f"联系报关合规部确认延误原因，并主动同步进度给客户，不能等客户先问。",
    ]
    return rng.choice(templates)

GLOBEX_CATEGORIES = {
    "hr_admin": ("人力资源与行政", globex_hr_admin),
    "finance": ("财务与报销制度", globex_finance),
    "it_support": ("IT 支持与技术运维", globex_it_support),
    "sales_marketing": ("销售话术与市场", globex_sales_marketing),
    "rd_product": ("研发与产品代码", globex_rd_product),
    "customer_success": ("客户成功与售后服务", globex_customer_service),
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
    print(f"Acme 共 {acme_total} 条\n")

    print("生成 Globex 知识库语料...")
    globex_total = generate_tenant("globex", GLOBEX_CATEGORIES)
    print(f"Globex 共 {globex_total} 条")


if __name__ == "__main__":
    main()
