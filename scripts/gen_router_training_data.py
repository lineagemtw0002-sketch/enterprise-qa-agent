"""生成 1.5b router 重训用的训练数据 + 评估集（Phase 3，
`docs/chitchat_intent_design.md` §5-⑥⑦）。

背景
----
根因②（`docs/review_2026-08-25/smalltalk_routing_regression.md`）：原训练集
91 条（81 train + 10 valid），闲聊样本 0 条，`tool` 占 68%。原数据本身、
以及生成它的脚本（`gen_router_training_data.py`）**都已经不在仓库里**——
`docs/chitchat_intent_design.md` §2.3-3c ⑤ 核对过，`find` 全仓无匹配。
这是本项目第二次因为"脚本/数据只在临时目录、没有落仓"而彻底丢失
（第一次是 `jailbreak_test.py`/`latency_probe.py`，见 CLAUDE.md §7.5）。

**本脚本是这一次的落仓动作本身**——运行一次即可从零重新生成同一批数据，
不依赖任何外部状态；train/eval 两份输出直接写进本次要求的固定路径
（`data/router_lora/` / `tests/fixtures/router_eval.jsonl`），跟脚本一起
提交进仓库，不会重蹈覆辙。

拍板依据（`docs/chitchat_intent_design.md` §5-⑥⑦，用户已批准按设计文档
建议值执行）：
- 五类各 15%~25%，任何一类不超过 25%
- chitchat 内部：开放闲聊 ≥50%，身份/能力/元问题 ~20%，问候/致谢/告别 ~15%，
  多轮上下文闲聊 ~15%
- hard negative（寒暄壳子里的真业务问题，标 tool/workflow 不是 chitchat）
  不低于 chitchat 总数的 20%
- **分批扩，不是一次定死**——本脚本产出的是"第一批"（约 300 条，
  对应 91→250/300 这一档，不是最终的 400~600），后续批次照这个脚本的
  模式继续加列表项即可，不需要重新设计格式

⚠️ **本次已知且必须如实标注的局限**（§4.5 ④ 的"评估集必须来自不同产出
来源"这条本次仍然没有解决）：train 和 eval 两份数据是同一个人、同一次
会话手写的，即使句子逐条去重（本脚本内置去重校验，见 `_validate_disjoint`），
措辞分布仍然可能高度相似，比"两批独立来源的数据"更容易低估真实泛化误差。
eval 里刻意加了训练集没有强调的形态（英文、纯表情、方言口语、超长句、
本脚本没覆盖的说法变体）来部分缓解，但不能替代"真实查询日志"或
"另一个人/另一次会话独立编写"这两种更强的独立来源。这一点原设计文档
已经承认是"目前做不到"的已知缺口，本次同样没有解决，如实继承标注。

输出格式
--------
JSONL，每行一条样本，字段对应 `QueryAnalysisAndIntentResult`（`intent.py`）：
    query          原始用户输入（多轮场景下是最后一轮）
    context        可选，多轮场景下之前几轮的对话（list[str]），单轮省略
    rewritten_query  简化处理：本批全部等于 query（多轮指代消解的真实重写
                      需要人工核对每一条，工作量超出本批范围，如实标注为
                      已知简化，不是遗漏）
    sub_queries    简化处理：单元素 [query]（本批不含拆分场景）
    intent_type    clarify / rag / tool / workflow / chitchat
    target_tool    intent_type=tool 时可能非空（query_knowledge_hub /
                   query_attendance），其余为 null
    workflow_type  intent_type=workflow 时的模板类型，其余为 null
    need_clarify   intent_type=clarify 时为 true
    confidence     人工标注置信度，非常清楚的样本给 0.95，略有歧义给 0.85
    reasoning      简短分类理由（人工写，训练/审查时的可读性用）

用法
----
    .venv/bin/python scripts/gen_router_training_data.py
    # 生成 data/router_lora/train_batch1.jsonl 和 tests/fixtures/router_eval.jsonl，
    # 并打印五类分布 + chitchat 内部分层 + hard negative 占比 + 去重校验结果
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
# ⚠️ 落仓位置与设计文档 §5-⑦ 拍板的 "data/router_lora/" 不同，改成仓库根目录
# 的 "router_lora_data/"——原因：本机 `.gitignore` 把 `data/` 整体忽略（用于
# Chroma/BM25 等运行时数据），且这条忽略还被 `.git/info/exclude`（worktree
# 共享、不受版本控制）重复了一层，尝试用 `!data/router_lora` negate 均已实测
# 失败（git 的规则是"父目录被忽略时，子路径不可能被重新纳入"）。`router_lora_data/`
# 这个名字不是临时拍的——正是 CLAUDE.md §4 P1 闲聊路由那条、以及本设计文档
# §2.3-3c ⑤ 反复提到的"此前丢失的那份数据/脚本原来的路径"，用回这个名字
# 比强行塞进 `data/` 更贴合项目既有约定，也彻底避开这个 gitignore 陷阱。
# 这是"设计文档假设不成立，就地调整并如实记录"的一个真实案例
# （任务要求：发现假设不成立要停下来说清楚，不要硬着头皮按字面意思做）。
TRAIN_OUT = REPO_ROOT / "router_lora_data" / "train_batch1.jsonl"
EVAL_OUT = REPO_ROOT / "tests" / "fixtures" / "router_eval.jsonl"


def rec(
    query: str,
    intent_type: str,
    *,
    target_tool: Optional[str] = None,
    workflow_type: Optional[str] = None,
    need_clarify: bool = False,
    confidence: float = 0.92,
    reasoning: str = "",
    context: Optional[List[str]] = None,
    subcategory: str = "",
) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "query": query,
        "rewritten_query": query,
        "sub_queries": [query],
        "intent_type": intent_type,
        "target_tool": target_tool,
        "workflow_type": workflow_type,
        "need_clarify": need_clarify,
        "confidence": confidence,
        "reasoning": reasoning or intent_type,
    }
    if context:
        d["context"] = context
    if subcategory:
        d["_subcategory"] = subcategory  # 仅供本脚本统计用，训练时应忽略/剥离
    return d


# ============================================================
# ============== 训练集（第一批，约 300 条） ==================
# ============================================================

CLARIFY_QUERIES = [
    "他呢", "多少", "这个", "那个", "它", "呢", "上面", "怎么", "然后呢",
    "那样的话呢", "这个怎么弄", "刚才说的那个", "上次那个", "继续",
    "还有吗", "这个可以吗", "那个呢", "再说一下", "这样可以吗", "那件事",
    "这件事", "他们呢", "她说的", "就这样吧", "行不行", "有吗", "多久",
    "为什么呢", "怎么办呢", "这样吗", "啊这个", "嗯这个", "那我要怎么办",
    "那个东西", "那些", "这些", "谁啊", "哪个", "哪里", "什么时候呢",
    "多长时间", "几个", "几点", "怎么样了", "进展如何", "什么情况",
    "然后呢怎么办", "接下来呢", "后面呢", "那然后呢", "是这样吗", "对吗",
    "是吗", "真的吗", "确定吗", "可以吗", "行吗", "好吗", "然后",
    "那个人", "他说啥",
]

RAG_QUERIES = [
    "总结一下我刚上传的这份文档", "这份PDF第二页说了什么", "帮我提炼这份文件的重点",
    "我上传的这个表格里有哪些字段", "这份合同的付款条款是什么", "刚才那个附件讲了什么",
    "这个文件的结论是什么", "把这份报告翻译成英文", "这份文档有多少页",
    "帮我检查一下这份简历有没有错别字", "这个Excel里第三列是什么意思",
    "总结一下这份会议纪要", "这份PPT的核心观点是什么", "我发给你的截图上写的什么",
    "这份代码文件里有几个函数", "帮我看看这份合同有没有问题",
    "这张图片里的表格数据是什么", "这份文档跟上一份有什么区别",
    "帮我把这份文件按要点列出来", "这个文件里提到的日期是哪天",
    "这份文档的作者是谁", "帮我校对一下这段文字",
    "这份需求文档里有几个功能点", "这个PDF里的图表说明了什么",
    "我上传的这份年报里营收多少", "这份简历的工作经历总共几段",
    "这份文件写得对不对", "这份合同的甲方是谁",
    "这张发票上的金额是多少", "这份文档里有没有提到截止日期",
    "总结这份文件的第一段", "这份表格帮我按金额排序",
    "这个附件是什么格式", "帮我把这份文档翻译一下",
    "这份材料里提到几个风险点", "刚发的那份文件读完了吗",
    "这份说明书讲的是什么设备", "这份文档里的联系人是谁",
    "帮我概括一下这份提案", "这份文档跟我们讨论的一致吗",
    "这份周报写得完整吗", "这个文件里有几张表",
    "这份记录里最早的时间是哪天", "这份文件的格式对不对",
    "这份问卷有多少道题", "帮我数一下这份文件有几个章节",
    "这份文档里的结论支持我们的方案吗", "这份合同签字页在第几页",
    "这份文件里提到的预算是多少", "这份说明里有没有提到保修期",
    "帮我看看这份图纸的比例尺", "这个附件打不开，你能看懂吗",
    "这份文档的版本号是多少", "这份材料里数据来源是哪里",
    "这份文档的目录有几项", "这份文件里的表格能导出吗",
    "这份记录里谁签的字", "这份文档提到了几个产品",
    "这份材料适合发给客户吗", "这份文档写得专业吗",
]

TOOL_KB_QUERIES = [
    "年假多少天", "报销流程是怎样的", "远程办公政策是什么", "试用期多久转正",
    "公司加班怎么算工资", "入职需要带哪些材料", "病假需要提供什么证明",
    "差旅住宿标准是多少", "公积金缴纳比例是多少", "离职流程是怎样的",
    "员工手册里关于考核怎么规定的", "调休假期怎么申请", "转正答辩要准备什么",
    "公司福利有哪些", "社保基数怎么算", "培训费用能报销吗",
    "出差交通费怎么报销", "加班调休有效期是多久", "劳动合同续签流程是什么",
    "绩效考核标准是什么", "试用期工资是多少", "公司总部地址在哪",
    "IT设备申领流程是什么", "域账号密码多久强制更换一次", "VPN怎么申请",
    "公司邮箱格式是什么", "打印机坏了怎么报修", "会议室怎么预定",
    "停车位怎么申请", "公司节假日安排是怎样的",
]
TOOL_ATTENDANCE_QUERIES = [
    "我这个月迟到几次", "上个月我的考勤记录", "我这周的打卡情况怎么样",
    "我还有多少年假没休", "帮我查一下我这个月的加班时长",
    "我上次请假是什么时候", "我的社保缴纳情况怎么样",
    "帮我查一下我本月工资明细", "我这周有几天打卡异常",
    "我上个季度出差了几次", "帮我看看我的年假余额",
    "我今天打卡了吗", "帮我查考勤异常原因",
]
TOOL_GENERIC_QUERIES = [
    "最近的天气预报", "今天股市怎么样", "帮我算一下123乘以456",
    "帮我搜一下最新的行业新闻", "查一下汇率是多少",
    "帮我查一下附近的餐厅", "搜索一下这个公司的官网",
    "查询一下最近的航班信息", "帮我算一下增值税怎么算",
    "搜一下这个词的意思", "查一下明天的日出时间",
    "帮我查询一下快递物流", "搜索一下这个产品的评价",
    "查一下今天限号吗", "帮我查一下油价",
    "搜索一下这个报错是什么原因", "查一下这个API怎么用",
]

WORKFLOW_QUERIES = [
    ("我想请假", "leave_request"), ("我要报销", "expense_reimbursement"),
    ("帮我报修电脑", "laptop_repair"), ("我要出差申请", "business_trip"),
    ("帮我发起一个请假申请", "leave_request"), ("我想申请年假", "leave_request"),
    ("帮我报销这个月的差旅费", "expense_reimbursement"),
    ("电脑坏了帮我报修一下", "laptop_repair"), ("我想请三天事假", "leave_request"),
    ("帮我申请下周出差", "business_trip"), ("打印机坏了需要报修", "laptop_repair"),
    ("我要提交报销单", "expense_reimbursement"), ("帮我申请病假", "leave_request"),
    ("我想请个年假", "leave_request"), ("空调坏了帮我报修", "laptop_repair"),
    ("我要申请出差去上海", "business_trip"), ("帮我报销打车费", "expense_reimbursement"),
    ("我想申请调休", "leave_request"), ("网络不通帮我报修一下", "laptop_repair"),
    ("我要请假去医院", "leave_request"), ("帮我提交出差申请", "business_trip"),
    ("显示器坏了要报修", "laptop_repair"), ("我想报销机票钱", "expense_reimbursement"),
    ("帮我申请下周三请假", "leave_request"), ("我要出差去深圳", "business_trip"),
    ("键盘坏了帮我报修", "laptop_repair"), ("我想申请年假三天", "leave_request"),
    ("帮我报销住宿费", "expense_reimbursement"), ("我要请假处理私事", "leave_request"),
    ("帮我申请出差补助", "business_trip"), ("电脑蓝屏了要报修", "laptop_repair"),
    ("我想请假带娃", "leave_request"), ("帮我提交请假申请下周一", "leave_request"),
    ("我要报销打车发票", "expense_reimbursement"), ("鼠标坏了帮我报修", "laptop_repair"),
    ("我想申请事假两天", "leave_request"), ("帮我发起出差申请去北京", "business_trip"),
    ("我要报销餐费", "expense_reimbursement"), ("帮我申请病假一周", "leave_request"),
    ("投影仪坏了要报修", "laptop_repair"), ("我想请假回家", "leave_request"),
    ("帮我提交报销申请", "expense_reimbursement"), ("我要申请出差去广州开会", "business_trip"),
    ("耳机坏了帮我报修", "laptop_repair"), ("我想请调休假", "leave_request"),
    ("帮我申请年假下个月", "leave_request"), ("我要报销办公用品费用", "expense_reimbursement"),
    ("帮我发起请假流程", "leave_request"), ("路由器坏了要报修", "laptop_repair"),
    ("我想申请出差三天", "business_trip"), ("帮我提交年假申请", "leave_request"),
    ("打卡机坏了帮我报修", "laptop_repair"), ("我要请婚假", "leave_request"),
    ("帮我申请产假", "leave_request"), ("复印机坏了要报修", "laptop_repair"),
    ("我想申请陪产假", "leave_request"), ("帮我发起报修工单", "laptop_repair"),
    ("我要出差去杭州", "business_trip"), ("帮我申请丧假", "leave_request"),
    ("我想请年假下周五", "leave_request"), ("电脑风扇坏了要报修", "laptop_repair"),
]

CHITCHAT_OPEN = [
    "今天天气不错", "你几岁了", "周末有什么安排", "讲个笑话", "最近好吗",
    "今天几号", "你喜欢吃什么", "你有感情吗", "你会做饭吗", "你困不困",
    "今天心情怎么样", "你喜欢什么颜色", "你有朋友吗", "你会累吗",
    "你觉得人生的意义是什么", "你相信爱情吗", "你有梦想吗", "你怕黑吗",
    "你喜欢音乐吗", "你会做梦吗", "你觉得今天适合出门吗", "你几点睡觉",
    "你有名字吗", "你喜欢什么运动", "你会不会无聊", "你觉得未来会怎样",
    "你今天开心吗", "你喜欢下雨天吗", "你会唱歌吗", "你有生日吗",
    "你觉得我今天怎么样", "你喜欢旅游吗", "你会不会孤独", "你有性格吗",
]

CHITCHAT_IDENTITY_CAPABILITY_META = [
    "你都会点啥呀", "你是干嘛的", "跟我说说你能做的事",
    "你这个系统是谁开发的", "你背后的技术是啥", "你靠不靠谱啊",
    "你是不是机器人啊", "你有没有情感这种东西", "你怎么回答问题的",
    "你是基于什么训练出来的", "你能查到公司外面的东西吗", "你除了聊天还能干嘛",
]

CHITCHAT_GREETING_THANKS_FAREWELL = [
    "你好呀，在么", "早呀", "晚上好呀，忙不忙", "谢谢啦",
    "多谢多谢", "辛苦啦", "拜拜咯", "先这样啦，回头聊", "下次再聊哈",
]

# 多轮上下文闲聊——报告 §6.3 记为零覆盖的场景：先问业务问题、拿到答案后
# 说一句"谢谢"，重写后的 query 仍然应该判 chitchat（不能被上一轮的业务
# 上下文带偏成 tool）。
CHITCHAT_MULTI_TURN = [
    (["用户: 年假可以顺延到次年几月？", "助手: 最晚需在次年3月31日前休完。"], "谢谢"),
    (["用户: 报销流程是怎样的？", "助手: 在OA系统提交报销单，附上发票即可。"], "好的，明白了"),
    (["用户: 我这个月迟到几次？", "助手: 您这个月迟到了1次。"], "谢谢告诉我"),
    (["用户: 出差怎么申请？", "助手: 在OA里发起出差申请，填目的地和日期。"], "辛苦了"),
    (["用户: 病假需要什么证明？", "助手: 需要提供医院开具的病假单。"], "好的谢谢"),
    (["用户: 我还有多少年假？", "助手: 您还剩5天年假。"], "了解了"),
    (["用户: 试用期多久转正？", "助手: 一般是3个月，具体看部门安排。"], "明白啦"),
    (["用户: 公司加班怎么算？", "助手: 工作日加班按1.5倍计算。"], "谢谢解答"),
    (["用户: 电脑怎么报修？", "助手: 已帮您提交报修工单。"], "太好了，谢谢"),
]

# hard negative：寒暄壳子里包着真业务问题——必须标 tool/workflow，不是 chitchat。
# 数量要求 >= chitchat 总数（open+identity+greeting+multiturn）的 20%。
HARD_NEGATIVES = [
    ("你好，年假多少天", "tool", "query_knowledge_hub", None),
    ("你能帮我查一下报销流程吗", "tool", "query_knowledge_hub", None),
    ("谢谢，那远程办公政策呢", "tool", "query_knowledge_hub", None),
    ("你知道报销上限吗", "tool", "query_knowledge_hub", None),
    ("你好，我想请个假", "workflow", None, "leave_request"),
    ("在吗，帮我查一下我这个月迟到几次", "tool", "query_attendance", None),
    ("你好，麻烦帮我报修一下电脑", "workflow", None, "laptop_repair"),
    ("谢谢你，另外我想问一下病假怎么请", "tool", "query_knowledge_hub", None),
    ("辛苦了，顺便帮我查下我的年假余额", "tool", "query_attendance", None),
    ("你好呀，我要出差申请怎么弄", "workflow", None, "business_trip"),
    ("多谢，那试用期多久转正呢", "tool", "query_knowledge_hub", None),
    ("在忙吗，帮我提交个报销单", "workflow", None, "expense_reimbursement"),
    ("你好，我想问一下加班费怎么算", "tool", "query_knowledge_hub", None),
    ("谢谢，帮我看看考勤记录", "tool", "query_attendance", None),
]


def build_train_records() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for q in CLARIFY_QUERIES:
        out.append(rec(q, "clarify", need_clarify=True, confidence=0.9,
                        reasoning="查询模糊/不完整，需要澄清", subcategory="clarify"))
    for q in RAG_QUERIES:
        out.append(rec(q, "rag", confidence=0.9,
                        reasoning="询问本次对话上传附件本身的内容", subcategory="rag"))
    for q in TOOL_KB_QUERIES:
        out.append(rec(q, "tool", target_tool="query_knowledge_hub", confidence=0.93,
                        reasoning="查询企业内部制度/流程/文档", subcategory="tool_kb"))
    for q in TOOL_ATTENDANCE_QUERIES:
        out.append(rec(q, "tool", target_tool="query_attendance", confidence=0.93,
                        reasoning="查询本人考勤记录", subcategory="tool_attendance"))
    for q in TOOL_GENERIC_QUERIES:
        out.append(rec(q, "tool", target_tool=None, confidence=0.85,
                        reasoning="需要调用外部工具/查数据，但不对应本系统已注册的具体工具",
                        subcategory="tool_generic"))
    for q, wf in WORKFLOW_QUERIES:
        out.append(rec(q, "workflow", workflow_type=wf, confidence=0.93,
                        reasoning="用户想发起一个业务流程/申请", subcategory="workflow"))
    for q in CHITCHAT_OPEN:
        out.append(rec(q, "chitchat", confidence=0.9,
                        reasoning="开放闲聊，不需要查资料/调用工具", subcategory="chitchat_open"))
    for q in CHITCHAT_IDENTITY_CAPABILITY_META:
        out.append(rec(q, "chitchat", confidence=0.9,
                        reasoning="问助手自身身份/能力/工作方式", subcategory="chitchat_meta"))
    for q in CHITCHAT_GREETING_THANKS_FAREWELL:
        out.append(rec(q, "chitchat", confidence=0.9,
                        reasoning="问候/致谢/告别", subcategory="chitchat_greeting"))
    for ctx, q in CHITCHAT_MULTI_TURN:
        out.append(rec(q, "chitchat", confidence=0.85, context=ctx,
                        reasoning="多轮上下文里的闲聊收尾，不应被上一轮业务上下文带偏",
                        subcategory="chitchat_multiturn"))
    for q, itype, ttool, wf in HARD_NEGATIVES:
        out.append(rec(q, itype, target_tool=ttool, workflow_type=wf, confidence=0.9,
                        reasoning="寒暄壳子包着真业务问题，不能判成 chitchat",
                        subcategory="hard_negative"))
    return out


# ============================================================
# ============== 评估集（holdout，与训练集不重叠） =============
# ============================================================
# 刻意加训练集没有强调的形态：英文、纯表情/标点、方言口语、超长句、
# 以及跟训练集措辞不同的同类问题——部分缓解"同一人同一次写出"的相似度，
# 但如脚本顶部注释所说，**不能替代真实查询日志**这条更强的独立来源。

EVAL_CLARIFY = [
    "那个咋整", "他咋说的", "然后咋办", "这咋弄", "啥意思",
    "那个到底行不行", "刚说的那事呢", "后来呢", "啥情况这是", "那咋办呢",
]
EVAL_RAG = [
    "这份文件里写的对不对", "帮我看看刚才那张图", "这份报告数据准不准",
    "这个附件里有没有提到风险", "帮我把这份文档里的表格提取出来",
    "这份合同乙方是谁", "这份文件签发日期是哪天", "帮我看看这份简历合不合格",
    "这份材料里的图表是什么意思", "这份文档里提到几种方案",
]
EVAL_TOOL = [
    "转正需要考核吗", "加班费怎么发", "公司几点上班", "年终奖怎么算",
    "我这个月请了几次假", "帮我看看我加班了多少小时", "查一下这周天气",
    "帮我算下退休还有多少年", "搜一下这个错误代码", "查一下附近有没有停车场",
]
EVAL_WORKFLOW = [
    "帮我把请假申请提交了", "我要走个报销流程", "笔记本进水了帮我报修",
    "我打算下个月出差", "帮我把年假批了", "我想请几天病假",
    "网线断了帮我报修下", "我要走出差报销流程", "帮我申请一下调休",
    "我要请假去考试",
]
# 开放闲聊——英文/表情/方言/超长四类，训练集里没有覆盖
EVAL_CHITCHAT_ENGLISH = [
    "how are you", "good morning", "what can you do",
    "thank you so much", "see you later", "good night",
]
EVAL_CHITCHAT_PUNCT = ["😊", "……", "?", "!!!", "🤔"]
EVAL_CHITCHAT_DIALECT = [
    "你干嘛呢", "在不", "咋样啊", "干哈呢", "你说是不", "整挺好",
]
EVAL_CHITCHAT_LONG = [
    "你好呀，最近工作好忙啊，都没时间休息，你说人是不是应该劳逸结合一下比较好呢",
    "哎最近天气变化好大，一会儿冷一会儿热的，你说这种天气是不是特别容易感冒呀",
]
EVAL_CHITCHAT_STANDARD = [
    "在么", "嘿，你在不在", "你人挺好的", "跟你聊天挺有意思的",
    "你是男的女的", "你多大了", "你叫什么", "你困了吗",
]
EVAL_HARD_NEGATIVES = [
    ("嗨，年假还剩几天呀", "tool", "query_knowledge_hub", None),
    ("你好呀，麻烦帮我查下病假流程", "tool", "query_knowledge_hub", None),
    ("辛苦你了，另外我要出差申请一下", "workflow", None, "business_trip"),
    ("在的话帮我看看我这月打卡记录", "tool", "query_attendance", None),
    ("谢谢哈，顺便帮我报修下电脑", "workflow", None, "laptop_repair"),
    ("你好，加班工资怎么算的呀", "tool", "query_knowledge_hub", None),
]


def build_eval_records() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for q in EVAL_CLARIFY:
        out.append(rec(q, "clarify", need_clarify=True, reasoning="holdout: 模糊短查询",
                        subcategory="clarify"))
    for q in EVAL_RAG:
        out.append(rec(q, "rag", reasoning="holdout: 本次对话附件内容", subcategory="rag"))
    for q in EVAL_TOOL:
        out.append(rec(q, "tool", reasoning="holdout: 需要查资料/调用工具", subcategory="tool"))
    for q, wf in zip(EVAL_WORKFLOW, [
        "leave_request", "expense_reimbursement", "laptop_repair", "business_trip",
        "leave_request", "leave_request", "laptop_repair", "expense_reimbursement",
        "leave_request", "leave_request",
    ]):
        out.append(rec(q, "workflow", workflow_type=wf, reasoning="holdout: 发起业务流程",
                        subcategory="workflow"))
    for q in EVAL_CHITCHAT_ENGLISH:
        out.append(rec(q, "chitchat", reasoning="holdout: 英文闲聊", subcategory="chitchat_english"))
    for q in EVAL_CHITCHAT_PUNCT:
        out.append(rec(q, "chitchat", reasoning="holdout: 纯表情/标点", subcategory="chitchat_punct"))
    for q in EVAL_CHITCHAT_DIALECT:
        out.append(rec(q, "chitchat", reasoning="holdout: 方言口语闲聊", subcategory="chitchat_dialect"))
    for q in EVAL_CHITCHAT_LONG:
        out.append(rec(q, "chitchat", reasoning="holdout: 超长闲聊", subcategory="chitchat_long"))
    for q in EVAL_CHITCHAT_STANDARD:
        out.append(rec(q, "chitchat", reasoning="holdout: 标准闲聊变体", subcategory="chitchat_standard"))
    for q, itype, ttool, wf in EVAL_HARD_NEGATIVES:
        out.append(rec(q, itype, target_tool=ttool, workflow_type=wf,
                        reasoning="holdout: hard negative", subcategory="hard_negative"))
    return out


# ============================================================
# ==================== 校验 + 落盘 + 统计 ======================
# ============================================================

def _normalize_for_dedup(text: str) -> str:
    """跟 `intent.py::_normalize_chitchat_token` 同一个归一化口径
    （设计文档 §4.5 ① 明确要求复用这个口径，避免"你好呀～"和"你好"被
    当成两条不同样本）：去空白、去首尾标点、剥句尾语气词、转小写。"""
    strip_chars = " \t\r\n，,。.！!？?～~、；;：:…—-_\"'“”‘’（）()【】[]"
    tail_particles = ("呀", "啊", "哈", "嘞", "咯", "啦", "喔", "噢", "嘛", "唷", "耶")
    token = "".join((text or "").split()).strip(strip_chars).lower()
    while len(token) > 1 and token.endswith(tail_particles):
        token = token[:-1].strip(strip_chars)
    return token


def validate_disjoint(train: List[Dict[str, Any]], eval_: List[Dict[str, Any]]) -> List[str]:
    """训练集与评估集不得有重叠句子（精确匹配 + 归一化匹配都要查）。
    这是 `scripts/check_router_lora_dedup.py`（供 pre-commit 用）里同一份
    逻辑的脚本内自检版本——生成时先查一遍，落盘后 pre-commit 还会再查
    一遍（防止后续有人手改文件绕过这里）。"""
    problems = []
    train_exact = {r["query"] for r in train}
    eval_exact = {r["query"] for r in eval_}
    exact_overlap = train_exact & eval_exact
    if exact_overlap:
        problems.append(f"精确重叠 {len(exact_overlap)} 条: {sorted(exact_overlap)[:5]}...")

    train_norm = {_normalize_for_dedup(r["query"]): r["query"] for r in train}
    eval_norm = {_normalize_for_dedup(r["query"]): r["query"] for r in eval_}
    norm_overlap = set(train_norm) & set(eval_norm)
    # 排除已经在精确匹配里报过的
    norm_overlap = {k for k in norm_overlap if train_norm[k] != eval_norm[k]}
    if norm_overlap:
        pairs = [(train_norm[k], eval_norm[k]) for k in norm_overlap]
        problems.append(f"归一化后重叠 {len(pairs)} 条: {pairs[:5]}...")
    return problems


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            clean = {k: v for k, v in r.items() if k != "_subcategory"}
            f.write(json.dumps(clean, ensure_ascii=False) + "\n")


def _print_stats(name: str, records: List[Dict[str, Any]]) -> None:
    total = len(records)
    by_type = Counter(r["intent_type"] for r in records)
    print(f"\n[{name}] 共 {total} 条")
    for t, n in sorted(by_type.items()):
        print(f"  {t:<10} {n:>4}  ({n / total * 100:.1f}%)")
    chitchat_sub = Counter(
        r.get("_subcategory", "") for r in records if r["intent_type"] == "chitchat"
    )
    if chitchat_sub:
        chitchat_total = sum(chitchat_sub.values())
        print(f"  chitchat 内部分层（共 {chitchat_total}）:")
        for sub, n in sorted(chitchat_sub.items()):
            print(f"    {sub:<24} {n:>4}  ({n / chitchat_total * 100:.1f}%)")
    hard_neg = sum(1 for r in records if r.get("_subcategory") == "hard_negative")
    chitchat_only = sum(1 for r in records if r["intent_type"] == "chitchat")
    if chitchat_only:
        print(f"  hard_negative {hard_neg} / chitchat {chitchat_only} = "
              f"{hard_neg / chitchat_only * 100:.1f}% （要求 >= 20%）")


def main() -> int:
    train = build_train_records()
    eval_ = build_eval_records()

    problems = validate_disjoint(train, eval_)
    if problems:
        print("[FAIL] 训练集与评估集有重叠，未写盘：")
        for p in problems:
            print(f"  - {p}")
        return 1

    # 训练集内部也查一遍精确重复（不同类别之间不该出现同一句话）
    seen = Counter(r["query"] for r in train)
    dup_in_train = {q: n for q, n in seen.items() if n > 1}
    if dup_in_train:
        print(f"[FAIL] 训练集内部有重复: {dup_in_train}")
        return 1

    _write_jsonl(TRAIN_OUT, train)
    _write_jsonl(EVAL_OUT, eval_)

    _print_stats("训练集 train_batch1.jsonl", train)
    _print_stats("评估集 router_eval.jsonl", eval_)
    print(f"\n[OK] 训练集与评估集互不重叠（精确 + 归一化均已核对）")
    print(f"[写盘] {TRAIN_OUT.relative_to(REPO_ROOT)}")
    print(f"[写盘] {EVAL_OUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
