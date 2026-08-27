# 意图分类第五类 `chitchat` —— 设计方案

> ## 🟢 已实施到 Phase 2；Phase 3（重训 LoRA）已备料，训练本身未执行
> **2026-08-27 用户明确批准启动**（覆盖了 2026-08-25 的搁置状态）。
> 实施顺序严格按 §2.5：**Phase 1a → Phase 1b → Phase 2 → Phase 3**，一个
> 阶段做完再进下一个。
>
> **当前状态**（`task/chitchat-intent-class` 分支，独立 worktree 完成）：
> | 阶段 | 状态 | 提交 |
> |---|---|---|
> | 1a（`intent.py`/`schemas.py`/新模块 `chitchat.py`，零行为变化） | ✅ 已实施并验证 | `feat(chitchat): Phase 1a` |
> | 1b（LLM 分类 schema 认识 chitchat，跑通对照回归） | ✅ 已实施并验证 | `feat(chitchat): Phase 1b` |
> | 2（`workflow.py` 接线，chitchat 真正路由到 generate） | ✅ 已实施并验证（含真机 LLM 验证） | `feat(chitchat): Phase 2` |
> | 3（重训 LoRA） | 🟡 **数据/评估设施已备好，真实训练未执行**（无 `mlx-lm`/`peft`/`torch`），见下 | 待用户拍板是否投入训练环境 |
>
> **Phase 1/2 已验证的真实效果**：可枚举闲聊（问候/致谢/告别/身份/能力/元问题）
> 现在**零 LLM 调用**直接返回固定文案，且已用真实本机 `qwen2.5:7b` 验证（不是
> mock）；开放闲聊现在也直接进对话生成（不再借用 `rag`），但**分类准确率没有
> 变化**——过渡期仍然只靠 `intent.py` 白名单短路，1.5b router 没有重训，见下面
> 实测的真实基线数字。**"加了第五类"≠"闲聊分类变准了"**，这条本文档最初的
> 警告依然成立，用真实数字而不是外推验证过了。
>
> **Phase 3 已完成的部分**（`router_lora_data/train_batch1.jsonl` 320 条 +
> `tests/fixtures/router_eval.jsonl` 73 条 holdout + 去重校验 +
> `scripts/eval_router_against_holdout.py`）：在冻结 holdout 上跑当前生产
> `qwen2.5-1.5b-router` 的真实基线——**总体准确率 53.4%（39/73），chitchat
> 只有 11.1%（3/27）**，其余四类 85.7%~100%。这是"重训前"基准，不是训练结果。
>
> **Phase 3 未完成、且本次没有勉强执行的部分**：真实 LoRA 微调训练本身。
> 本机没有安装 `mlx-lm`/`peft`/`torch` 中任何一个训练工具链，且
> `docs/optimization_tracking.md` 记录过原模型是走 LoRA+MLX 流程训练的——
> 不是"参数没调对"，是**训练环境完全没有搭建过**，现装+下载基座模型+训练+
> 转回 GGUF 注册进 Ollama 是一次独立的、有实际耗时的工程投入，不能在本次
> 顺带做完，如实标注为未完成，不伪造训练结果。
>
> ⚠️ **一处与本文档原拍板值的偏离**（发现假设不成立后就地调整，未静默按
> 字面执行）：§5-⑦ 训练数据落仓位置原定 `data/router_lora/`，实施时发现
> 本机 `.gitignore`（+ `.git/info/exclude` 里 worktree 共享的重复规则）把
> 整个 `data/` 目录忽略，塞进去的文件会被 git 悄悄当成不存在——这正是
> §2.3-3c ⑤ 想避免的"数据丢失"本身。改用仓库根目录 `router_lora_data/`
> （与 CLAUDE.md 历史记录里提到的路径名一致），已验证会被 git 正常跟踪。
>
> **重启/继续本方案时该做什么**：如果决定投入训练，直接用
> `router_lora_data/train_batch1.jsonl` + `tests/fixtures/router_eval.jsonl`
> 起步（不用重新造数据），装好训练工具链后跑一遍、跟上面的基线数字比对，
> 新模型注册成新的 Ollama 模型名（不要覆盖 `qwen2.5-1.5b-router`），
> 由用户决定是否切换生产。
>
> **状态**：**Phase 1a/1b/2 已实施并验证；Phase 3 数据与评估设施已实施，
> 训练本身未实施**。
> **日期**：2026-08-27（首版 2026-08-25，2026-08-25 曾搁置，2026-08-27 用户批准重启并实施）
> **死期**：**2026-10-31**，维持不变——针对 Phase 3 剩余的训练执行部分。
> Phase 1a/1b/2 已完成，不再受死期约束。
>
> **结论分档**（严格区分，全文不混用）：
> - **已验证通过** = 有可复现的验证手段（自动化测试或可重跑脚本）
> - **已跑通** = 手工执行过一次，未沉淀成可复现资产
> - **已实现但未验证** = 读通了代码逻辑，本次没有执行验证
>
> 本文以下 §0~§7 是 2026-08-25 原始设计草案，**按其字面内容基本已实施**，
> 保留原文供施工记录对照；与实施结果有出入的地方已在上方状态区说明，
> 不在正文里逐条修改（避免"设计"与"实施记录"混成一份文档、以后分不清
> 哪句是原计划哪句是事后改的）。

---

## 0. 结论速览

| 问题 | 推荐 | 一句话理由 |
|---|---|---|
| **路由去哪** | **混合方案 B+**：`chitchat → generate`（不新增节点），在 `_generate_node` 里加**第四道模板短路**，模板未命中的开放闲聊才走 LLM | 可枚举的用模板（跟现有三处短路同一个模式），开放的走 LLM；**复用 generate 就自动继承越权短路 / 泄露过滤 / token 计量**，不用把三段安全代码抄第二份 |
| **怎么防编造** | 独立的 chitchat prompt（**不复用** `_build_prompt`），核心是一份**能力白名单**：只许承认清单内能力，清单外一律"我做不到" | 闲聊时零检索依据，是模型编造系统自身能力的最高发场景；这是 `orchestration_design.md` D4/D5 的同构约束 |
| **重训之前靠什么触发** | **只靠现有白名单短路**，够用且已验证 | 白名单已把误判从 81% 打到 0%（`test_intent_chitchat_routing.py` 132 条守着）。第五类**本身修不了开放闲聊** |
| **未知类型兜底** | 现状是 **Pydantic `Literal` 校验失败 → 异常 → 规则 fallback**；`_route_after_intent` 还有一道默认 `retrieve` | 两层都在，不需要新增兜底 |
| **重训配比** | 五类各 15~25%，chitchat 内部**开放闲聊 ≥ 50%**，另配 ≥20% 的 hard negative（寒暄壳子里的真业务问题，标 `tool`） | 不能把 `tool` 68% 的偏斜换成 `chitchat` 的新偏斜 |
| **132 条单测怎么改** | **只改 1 处标签值断言 + 1 处函数名**；`TestVagueShortQueryStillClarifies` / `TestBusinessQueriesUnaffected` **一行不动** | 回归保护主体守的是"哪些句子算闲聊"，跟标签叫什么无关 |
| **实施顺序** | **1a（只碰 `intent.py`/`schemas.py`/新模块，零行为变化，可独立上线）→ 1b（动 prompt/enum，需跑对照回归）→ 2（等 `workflow.py` 写权限）→ 3（重训）** | 1a 之所以零风险，是因为 `workflow.py:356` 的默认分支恰好把 `chitchat` 当成今天的 `rag` 走 |

**最重要的一条**：**加第五类并不能修白名单外的开放闲聊**（`今天天气不错` / `你几岁了`）。
它是根因②（重训 router）的**前置条件**——分类体系不定下来，补的样本标什么标签都是错的。
把"加第五类"当成"开放闲聊就好了"是本设计最容易被误读的地方。

---

## 1. 背景

### 1.1 三条根因的当前状态

| 根因 | 内容 | 状态 |
|---|---|---|
| ① | `_needs_clarify_rule` 的 `len < 4` 阈值把 2~3 字寒暄 100% 拦成澄清话术 | **已修**（`_match_chitchat_intent` 白名单短路，误判 81% → 0%，`tests/unit/test_intent_chitchat_routing.py` 132 条守着） |
| ② | 1.5b router LoRA 训练集 91 条**闲聊 0 条**、`tool` 占 68% | **未修** |
| ③ | `_INTENT_CLASSIFY_RULES` 四个桶里没有"直接对话回答"这一类 | **未修 ← 本文** |

来源：`docs/review_2026-08-25/smalltalk_routing_regression.md`（状态：已验证通过）。

### 1.2 当前的临时状态：借用 `rag`

`intent.py:453` —— `_match_chitchat_intent` 命中后返回 `intent_type="rag"`。
它能工作，是因为 `rag` 是四桶里唯一能走到"generate 正常调 LLM 生成"的分支。

**但语义上不是正解**，而且有三处实际代价（**已实现但未验证**，读代码推断）：

1. **`rag` 的本义被污染**。`_INTENT_CLASSIFY_RULES`（`intent.py:585-596`）里 `rag` 明确写着
   "关于用户在当前这次对话里自己上传的文件/附件本身的内容"。闲聊借用它之后，这个桶同时
   承载两种毫不相干的语义——**这正是根因②补训练样本时最先会撞上的墙**：一条"你好"该标
   `rag` 吗？标了，模型就同时学到"rag = 问附件"和"rag = 打招呼"两个互斥先验。
2. **每句"你好"都白跑一次真实检索**。`rag` 路由到 `_retrieve_node`（`workflow.py:1066`），
   它会真的对 `conv_{conversation_id}` 这个 collection 发起一次检索调用；查不到时
   （`workflow.py:1129`）往 `retrieval_context` 里塞一句
   `"该对话暂无文件或检索服务暂时不可用。"`，然后这句话作为【检索上下文】进了
   `_build_prompt`（`workflow.py:1465-1473`）。用一次网络往返换来一句给模型添乱的噪音。
3. **用的是企业知识库助手的 prompt**。`_build_prompt`（`workflow.py:1437`）开头就是
   "你是企业级知识库助手，基于检索结果、工具执行结果……回答用户问题"，结尾是
   "请给出准确、有用的回答"。**零检索依据 + "请给出准确有用的回答"** 这个组合，
   正是 `_KB_EMPTY_HIT_MESSAGE` 那道短路当初被加出来要避免的东西
   （`workflow.py:60-71` 的注释：*"把免责声明交给本地小模型自由发挥本身就是风险源"*）。

### 1.3 白名单覆盖不到的部分

`_CHITCHAT_EXACT`（`intent.py:343-362`）是**穷举表**，`_CHITCHAT_PATTERNS`（`intent.py:366-377`）
是 10 条**锚定 `^...$` 的高精度正则**。两者都只覆盖**可枚举的寒暄**。

实测（`docs/review_2026-08-25/smalltalk_routing_regression.md` §3，**已验证通过**）：
白名单外的开放闲聊（`今天天气不错` / `你几岁了` / `周末有什么安排`）在 1.5b router 下
**仍然 100% 撞 `_KB_EMPTY_HIT_MESSAGE`**——模型把"你"当成公司里一个待查实体，
reasoning 里直接写着"询问用户姓名，应查企业知识库"。

> **注**：这三条具体句子**不在** `verify_smalltalk_routing.py` 现有的 25 条用例里
> （现有用例见该脚本 `CASES`，`scripts/verify_smalltalk_routing.py:124-179`）。
> "100% 撞拒绝话术"是按该脚本 §3.1 规律 2（含"你/你的"+ 属性疑问词的短句全部
> `kb_refusal`）**外推**的，属于**已实现但未验证**——**本设计的第一件事就是把这类用例
> 补进脚本、跑出真实基线**（见 §5.4）。

---

## 2. 设计问题逐条分析

### 2.1 问题 1：`chitchat` 路由到哪里？

#### 先澄清一个前提：三个方案在"要不要动 workflow.py"上**没有差别**

| | 需要改 `_route_after_intent` | 需要改 `_build_graph` 的边 | 需要改 `TracePanel.jsx` |
|---|---|---|---|
| A 新增 `chitchat` 节点 | ✅ | ✅（加节点 + 加边 `chitchat → memory_manage`） | ✅（`TracePanel.jsx:16` 的 `NODES` 是**写死的数组**） |
| B 直连 `generate` | ✅ | ✅（conditional edges 的 map 里加一项 `"generate": "generate"`） | ❌ 不用改 |
| C 固定模板 | ✅ | 取决于模板放哪个节点 | 取决于同上 |

**所以"哪个方案更省 workflow.py 写权限"这个维度分不出胜负**，三个都要等。
但 A 额外要动前端（`frontend/src/components/TracePanel.jsx:16` 那行硬编码的
`NODES = ['session','intent','clarify','retrieve','tool_subgraph','generate','memory_manage','archive']`），
多一个文件、多一个会话的协调成本。

#### 方案 A：新增 `chitchat` 节点

| | |
|---|---|
| **做法** | 独立节点，自己构建 prompt、自己调 LLM、自己流式吐 token、自己写 `final_answer` |
| **优点** | 职责最清晰；可以完全不受 `_generate_node` 里那三道安全短路的干扰；trace 上能一眼看出走的是闲聊分支 |
| **致命缺点** | **要把三段安全代码抄第二份**：越权话术输入侧短路（`workflow.py:1221-1247`）、系统提示词泄露输出侧流式过滤（`workflow.py:1336-1380`）、token 用量计量。这三段全是安全关键路径，两份实现必然慢慢跑偏——`_reconcile_intent_result` 的 docstring（`intent.py:599-612`）专门为"两条路径各写一份校验会慢慢跑偏"设计过共用后处理，本项目对这个失败模式有过明确判断 |
| **附带成本** | 要改前端硬编码的节点列表；要新增一条 `chitchat → memory_manage` 边（否则这轮对话不进记忆、不归档） |
| **判定** | ❌ **不推荐**。收益是"干净"，代价是安全代码复制粘贴，方向反了 |

#### 方案 B：直接连到 `generate`

| | |
|---|---|
| **做法** | `_route_after_intent` 对 `chitchat` 返回 `"generate"`；`_build_graph` 的 conditional edges map 加一项；`_build_prompt` 按 `intent_type` 选模板 |
| **优点** | **自动继承** `_generate_node` 已有的一切：越权短路、泄露过滤、`GENERATE_MAX_TOKENS`、token 计量、trace 埋点、`used_model`、流式透传。零安全代码复制 |
| **优点** | **顺手砍掉一次无效检索**（§1.2 第 2 点）：`generate` 不经过 `retrieve`，`retrieval_context` 天然为空 |
| **优点** | 前端零改动 |
| **缺点** | `_generate_node` 已经 200+ 行、已有三道短路，再加分支会更长 |
| **缺点** | **"你能做什么"仍然交给 LLM 生成**——这正是最不该交给 LLM 的一类 |
| **判定** | ⚠️ 骨架对，但单独用不够安全 |

#### 方案 C：固定模板，完全不调 LLM

| | |
|---|---|
| **做法** | 闲聊一律回固定文案，跟 `_KB_EMPTY_HIT_MESSAGE`（`workflow.py:67-71`）、ACL 拒绝（`workflow.py:1262-1281`）、`_PRIVILEGE_CLAIM_BLOCKED_MESSAGE`（`workflow.py:93-97`）、简短确认语（`workflow.py:654-668`）同一个模式 |
| **优点** | **最快**（零 LLM 往返，闲聊变成毫秒级）、**最可控**（措辞是人写的，逐字审过） |
| **优点** | **跟本项目已有的一致做法完全对齐**，理由都写在代码注释里：`workflow.py:60-71` "把免责声明交给本地小模型自由发挥本身就是风险源"；`workflow.py:1203-1213` 安全复测发现"交给 LLM 判断这件事本身靠不住" |
| **优点** | **"你能做什么""你用什么模型"这类问题恰恰是模板的最佳适用面**：答案是系统自身的客观事实，不需要生成、不该生成、生成了就是风险 |
| **致命缺点** | `今天天气不错` / `周末有什么安排` / `讲个笑话` **模板答不了**。硬答只能回一句"我是企业知识库助手，只能回答……"，等于把开放闲聊变成另一种拒绝话术——**换了个更礼貌的失败模式，不是修好了** |
| **判定** | ✅ 对可枚举部分是**最优解**；对开放部分**无解** |

#### 推荐：**混合方案 B+**（C 的模板闸门装在 B 的路由上）

```
intent (chitchat)
   │
   └─→ generate 节点
         ├─ [既有] 越权话术短路           ← 不动
         ├─ [既有] ACL 拒绝短路           ← 不动（chitchat 无 tool_summary，天然不触发）
         ├─ [既有] 知识库空命中短路        ← 不动（chitchat 无 target_tool，天然不触发）
         ├─ [新增] chitchat 模板短路 ────→ 命中：固定文案，不调 LLM
         │                                 （身份 / 能力 / 元问题 / 问候 / 致谢 / 告别）
         └─ [新增] chitchat prompt 分支 ─→ 未命中：走 LLM，用**受约束的闲聊 prompt**
                                            （开放闲聊）
```

**为什么混合值得**——复杂度增量只有三处，且都很薄：

1. `_route_after_intent` 加 2 行（`if intent_type == "chitchat": return "generate"`）。
2. `_build_graph` 的 conditional edges map 加 1 个键值对。
3. `_generate_node` 加 ~8 行胶水 + `_build_prompt` 加一个分支。

**模板判定逻辑本身不放在 `workflow.py`**，而是做成一个**纯函数**放在新模块
`src/ragent_backend/chitchat.py`：

```
match_chitchat_reply(query: str) -> Optional[str]     # 命中返回固定文案，否则 None
build_chitchat_prompt(query, recent_history) -> str   # 开放闲聊用的受约束 prompt
CAPABILITY_MANIFEST: list[str]                        # 能力白名单，模板和 prompt 共用同一份
```

这样带来三个好处：
- `workflow.py` 的 diff 缩到最小（**写权限被别人持有时，diff 越小越好合**）；
- 模板逻辑是**纯函数**，不需要任何 DB/LLM fixture 就能单测（见 §5.2）；
- 模板文案和 prompt 里的能力清单**共用同一个常量**，不会一处改一处忘。

**为什么不把模板判定塞进 `intent.py`**：`intent.py` 已 990 行，职责是分类。
"用户看到什么文案"是生成侧的事。新模块 `chitchat.py` 让 `intent.py` 只 import 判定，
不承载文案。（这一条是**拍板点**，见 §6-⑨。）

**模板 lane 的覆盖边界建议**（拍板点 §6-③）：

| 子类 | 走模板？ | 理由 |
|---|---|---|
| 身份（你是谁 / 你叫什么） | ✅ 模板 | 客观事实，且是编造重灾区 |
| 能力（你能做什么 / 你会什么） | ✅ **必须模板** | **LLM 最容易编造系统能力的地方**（"我可以帮你订机票"） |
| 元问题（你用什么模型 / 你怎么工作的） | ✅ **必须模板** | 直接踩 `_PROMPT_LEAK_BLOCKED_MESSAGE` 的防线（`workflow.py:83-86`）：这类问题的正确答案就是"不透露内部实现" |
| 问候（你好 / 在吗） | ⚠️ 建议模板，但**要给 2~3 条随机文案** | 永远同一句"你好！有什么可以帮您？"机械感很强；但让 1.5b/7b 生成一句问候语的边际价值也几乎为零 |
| 致谢 / 告别（谢谢 / 再见） | ⚠️ 同上 | 同上 |
| 开放闲聊（天气 / 年龄 / 周末 / 笑话） | ❌ 走 LLM | 模板无解 |

---

### 2.2 问题 2：生成时如何防止编造？

只有**开放闲聊那一条 lane** 会走 LLM。它的 prompt 必须是**独立的**，不能复用
`_build_prompt`（`workflow.py:1422-1492`）——那个 prompt 的第一句就是"你是企业级知识库助手，
基于检索结果、工具执行结果……回答"，而闲聊场景下这三样**全是空的**。

#### 必须写进 prompt 的约束（按重要性排序）

**① 能力白名单（最重要，其余都是它的推论）**

不是"不要编造能力"这种否定式约束——本项目已有实测证据说明否定式约束对本地小模型效果有限
（`docs/review_2026-08-25/smalltalk_routing_regression.md` §5-E：1.5b 对 prompt 里
"clarify 判断要从严"那段"基本无反应"）。要用**正面枚举 + 封闭声明**：

```
你能做的事情**只有下面这份清单里的**（清单之外的一律回答"我做不到"）：
  1. 查询企业知识库里的制度/流程/文档
  2. 查询你自己的考勤记录
  3. 帮你发起报修 / 请假 / 出差 / 报销申请
清单之外的任何能力（订机票、发邮件、打电话、访问互联网、看实时行情、
操作其他系统…），一律明确回答"这个我做不到"，**不许说"可以试试""也许可以"**。
```

⚠️ 清单内容**必须与 `tool_registry` / `workflow_store` 的真实内容对齐**，且**会漂移**
（新增/下线工具或流程时）。见 §7 风险 R3 与 §5.5 的漂移检查。

**② 禁止透露内部实现**（照抄 `_build_prompt` 已有的那段，`workflow.py:1445-1447`）

闲聊恰恰是"你用的是什么模型""你是怎么工作的"这类问题的高发区。这条与
`looks_like_prompt_leak` 输出侧过滤（`workflow.py:1336-1380`）是**两道独立防线**，
两道都要在。走 B+ 方案时输出侧那道自动继承，**这是选 B 而不是 A 的一个具体收益**。

**③ 数据缺口必须声明（`orchestration_design.md` D5 的同构约束）**

D5 原文是"不得用政策数字顶替用户个人数据"（`docs/orchestration_design.md:175`）。
闲聊场景的同构版本是：

```
你**没有**访问实时信息的能力（今天几号、天气、新闻、股价、汇率）。
遇到这类问题必须直接说"我查不到实时信息"，
**不许**用你训练时见过的内容去回答，也不许说一个约数或者"一般来说"。
```

`今天天气不错` 这类句子最危险的不是它本身，是它容易滑向 `那明天呢`。

**④ 禁止跨材料编造关系（D4 的同构约束，`docs/orchestration_design.md:174`）**

D4 原文是"除非材料明确陈述，不得推导跨材料因果/加减/换算"。闲聊 lane 没有材料，
所以它的同构版本更强——**没有任何材料，所以不许给出任何需要材料支撑的论断**：

```
如果用户在闲聊里夹带了业务问题（公司制度、报销标准、假期天数……），
**不要凭你的训练知识回答**，请引导用户单独提问，由系统去查企业知识库。
你在这次对话里**没有拿到任何企业资料**，凡是需要资料支撑的话一句都不能说。
```

这条是**最容易出事的一条**：白名单的业务词一票否决（`_CHITCHAT_BUSINESS_VETO`，
`intent.py:384-392`）只在**规则短路**这条路上生效；重训之后模型自己判 `chitchat` 时，
一句"你好，我们公司报销上限是多少来着"完全可能被判成闲聊、直接落到这个 lane 上。
**规则层的一票否决必须在 prompt 层有一份对应的约束**，否则重训之后会出现一个新的编造入口。

**⑤ 指令层级声明**（照抄 `workflow.py:1439-1450`）

闲聊输入同样可能是注入载体，不能因为"只是聊天"就省掉这一段。

**⑥ 长度上限**

闲聊回答 1~3 句。回答越长，编造越多——这是经验判断，**未验证**，但代价极低。
实现上直接复用 `GENERATE_MAX_TOKENS`（`workflow.py:77`）不够（1200 太宽），
建议给闲聊 lane 单独一个更紧的上限（例如 200），理由与 `GENERATE_MAX_TOKENS`
当初被引入的理由一致（`workflow.py:73-77`）。

#### 一个不能忘的点

模板 lane **也要过安全检查**。`detect_privilege_claim`（`workflow.py:1221`）在
`_generate_node` 最顶上，模板短路排在它**之后**，所以自动被覆盖——
**这是方案 B+ 里"模板短路必须放在 `_generate_node` 内部、而不是路由层"的原因**。
如果在 `_route_after_intent` 里就把模板答案定下来，就绕过了越权检查。

---

### 2.3 问题 3：与 1.5b router 模型的兼容

#### 3a. 重训之前，第五类靠什么触发？

**答：只靠现有的白名单短路，且这是足够的过渡方案。**

理由是这三处短路的位置——它们**全在 LLM 之前**（`intent.py`）：

| 位置 | 行号 | 说明 |
|---|---|---|
| `detect_intent` Step 0 | `intent.py:520-527` | 旧的两次调用路径，LLM 之前 |
| `analyze_and_route` Step 0（前置） | `intent.py:790-795` | **线上走的合并路径**，LLM 之前，命中即零 LLM 调用 |
| `analyze_and_route` Step 0（后置） | `intent.py:856-859` | 作用于 LLM 自己重写出来的 `rewritten_query`（重写可能把"嗨~"整理成"你好"） |

所以只要 `_match_chitchat_intent` 的返回值从 `"rag"` 改成 `"chitchat"`，**今天所有能被修好的
闲聊，明天照样被修好，一条不多一条不少**——因为触发源根本没变。

**这也正是"第五类修不了开放闲聊"的原因**：开放闲聊本来就不被白名单命中，
它走到 LLM，而 LLM 不会输出 `chitchat`。**必须重训才有效**（Phase 3）。
把 Phase 1/2 上线后说"闲聊修好了"是错误陈述。

#### 3b. 模型输出未知类型时，现在的代码是什么行为？

**已实现但未验证**（读代码推断，未构造异常输出实测）：

1. `with_structured_output(..., method="json_mode")` 拿到的 JSON 会由 Pydantic 校验。
   `intent_type` 是 `Literal["clarify","rag","tool","workflow"]`（`intent.py:37` / `intent.py:81`），
   模型吐出枚举外的值 → **`ValidationError`**，不是静默透传。
2. `detect_intent` 里被 `except Exception` 捕获（`intent.py:553-554`），
   打一行日志后落到规则 fallback `_detect_intent_rule_based`（`intent.py:881`）。
3. `analyze_and_route` 里被 `except Exception` 捕获（`intent.py:866`），
   整个函数降级回旧的两次调用路径。
4. 万一有 `str` 值绕过校验进了 state（`RAGState.intent_type` 是裸 `str`，`schemas.py:506`），
   `_route_after_intent`（`workflow.py:320-356`）的四个 `if` 都不命中 → 落到最后一行
   `return "retrieve"`（`workflow.py:356`）。

**结论：两层兜底都已存在，本设计不需要新增任何兜底。**
而且第 4 点有个直接的工程红利——**它就是 §4 里 Phase 1 能独立上线的技术依据**：
`chitchat` 在 workflow.py 眼里是"未知值"，会走 `retrieve`，即今天 `rag` 的走法，**行为完全等价**。

#### 3c. 重训之后，`chitchat` 该占多大比例？

根因②的教训精确地说是两条，不能只记住一条：

- `tool` 占 68% → 模型学到"拿不准就判 tool"的先验
- **总量只有 91 条**（81 train + 10 valid）→ 任何一类的样本量都不足以支撑泛化

所以**先解决总量，再谈配比**。建议（**均为设计建议，未验证，需拍板**）：

**① 总量**：91 条太少。建议先扩到 **400~600 条**量级。
> ⚠️ 这个数字是经验判断，**本项目没有做过样本量 vs 准确率的曲线实测**。
> 更稳妥的做法是分批扩（91 → 250 → 500），每批用**同一个 holdout** 测一次，
> 看准确率何时不再上升。这比一次性定一个数字可靠。

**② 五类配比**：各 **15%~25%**，任何一类不超过 25%。
不追求精确均衡（真实线上分布本来就不均衡），但**必须避免任何一类超过 1/3**——
68% 那个先验就是这么来的。
> 更好的做法是按**真实线上查询分布**采样。但本项目目前**没有可用的线上查询日志统计**
> （至少本次未找到），所以退而求其次用近似均衡。这是拍板点 §6-⑥。

**③ `chitchat` 内部分层**——这一条比总配比更重要：

| 子类 | 占 chitchat 的建议比例 | 理由 |
|---|---|---|
| 开放闲聊（天气/年龄/周末/笑话/心情/节日…） | **≥ 50%** | **白名单已经 100% 覆盖可枚举部分了**。模型在可枚举部分的边际价值≈0，全部价值都在开放部分 |
| 身份 / 能力 / 元问题 | ~20% | 白名单覆盖了整句形式，但换个措辞（"你都会点啥呀"）就漏 |
| 问候 / 致谢 / 告别 | ~15% | 同上，主要补白名单漏掉的措辞变体 |
| 多轮上下文里的闲聊 | ~15% | 报告 §6.3 明确记为**零覆盖**：先问年假、再说"谢谢"，指代消解会把"谢谢"重写成什么？可能直接让白名单失效 |

**④ Hard negatives（必须有，且要单独计数）**：
"寒暄壳子里包着真业务问题"这一类，**必须标成 `tool` 而不是 `chitchat`**，
数量**不低于 chitchat 总数的 20%**。例句：`你好，年假多少天` / `你能帮我查一下报销流程吗` /
`谢谢，那远程办公政策呢` / `你知道报销上限是多少吗`。

没有这批样本，模型会学到"带'你'字 + 短 = chitchat"，把根因②的 `tool` 偏斜**镜像**成一个
新的 `chitchat` 偏斜——那时的症状会更难发现：不再是"知识库里没有你是谁"，
而是"报销上限我不清楚哦～"，**用户根本不会报 bug，只会觉得这系统没用**。

**⑤ 前置动作**（沿用报告 §5 的结论，仍然成立）：
`router_lora_data/` 和 `gen_router_training_data.py` **本次核对确认仍不在仓库里**
（`find` 全仓无匹配，**已跑通**）。**动 Phase 3 之前必须先落仓**。

---

### 2.4 问题 4：`_match_chitchat_intent` 怎么改，132 条单测怎么办

`tests/unit/test_intent_chitchat_routing.py` **本次实测收集到 132 条**（
`pytest --collect-only -q` 输出 `132 tests collected`，**已跑通**）。

#### 逐类盘点：哪些必须改、哪些一行不动

| 测试类 | 用例数（近似） | 改动 | 说明 |
|---|---|---|---|
| `TestChitchatWhitelist` | ~35 参数化 + 6 具名 | **1 处** | 只有 `test_chitchat_is_matched_and_routed_to_rag` 里那行 `assert result.intent_type == "rag"` 要改成 `"chitchat"`，函数名一并改成 `..._to_chitchat`。其余断言（`is not None` / `need_clarify is False` / `target_tool is None`）**照旧成立** |
| `TestVagueShortQueryStillClarifies` | ~20 | **零改动** | 它测的是 `_needs_clarify_rule`，跟闲聊标签叫什么完全无关 |
| `TestChitchatShortCircuitsBeforeLLM` | ~40 | **2 处** | 两处 `assert intent.intent_type == "rag"` 改成 `"chitchat"` |
| `TestBusinessQueriesUnaffected` | ~20 | **零改动** | 它测的是"白名单不许吞掉业务问题"，与标签值无关 |

**总计要动的行：3 行断言 + 1 个函数名。**

#### 怎么让这次改动**不破坏**已有的回归保护

回归保护的主体（`TestVagueShortQueryStillClarifies` + `TestBusinessQueriesUnaffected`，
约 40 条）**守的是"哪些句子算闲聊、哪些不算"，不是"闲聊叫什么名字"**——这两组一行不改，
保护强度不变。这是本次改动天然安全的根本原因。

在此之上，建议再做三件事把保护做厚：

**① 把"标签值"和"命中判定"彻底分离**

现在 `test_chitchat_is_matched_and_routed_to_rag` 一条用例里同时断言了两件事
（命中 + 标签是 rag），改标签就得动这条参数化用例（×35）。建议拆成：

- 参数化那 35 条：只断言**行为契约**——`is not None`、`need_clarify is False`、
  `target_tool != "query_knowledge_hub"`。这三条才是"用户不会看到固定话术"的真正契约，
  比 `== "rag"` 更接近意图，**而且以后再改标签也不用动**。
- **新增一条**具名用例专门钉死标签值 `== "chitchat"`。标签值的变更从此只影响 1 处。

**② 加一条"过渡期行为等价"护栏（Phase 1 专用）**

Phase 1 只改 `intent.py`/`schemas.py`，`workflow.py` 不动。加一条测试断言
`_route_after_intent` 对 `intent_type="chitchat"` 仍返回 `"retrieve"`——
**这条测试就是"分阶段落地"那道接缝的保护**：它证明 Phase 1 单独上线不改变任何用户可见行为。
Phase 2 时把这条改成断言 `"generate"`，改动本身就是 Phase 2 的验收。

**③ 保留 `rag` 的语义测试**

新增一条：`_match_chitchat_intent("你好")` 的结果 `intent_type != "rag"`。
它钉死"闲聊不再借用 rag 这个桶"这个决定本身——**在旧实现下它会失败**（按 `CLAUDE.md` §7.2
"写完测试再问一句：它在旧实现下会失败吗？不会 → 是废测试"）。

---

### 2.5 问题 5：实施顺序与写权限约束

#### 改动清单按文件归属切分

| 文件 | 改什么 | 阶段 | 写权限 |
|---|---|---|---|
| `src/ragent_backend/schemas.py:346` | `IntentResult.intent_type` 的 `Literal` 加 `"chitchat"`（默认值仍 `"rag"`，不动） | **1a** | ✅ 干净 |
| `src/ragent_backend/intent.py:453` | `_match_chitchat_intent` 返回 `"chitchat"`，docstring 同步改 | **1a** | ✅ 干净 |
| `src/ragent_backend/chitchat.py`（**新文件**） | `CAPABILITY_MANIFEST` / `match_chitchat_reply()` / `build_chitchat_prompt()` | **1a** | ✅ 新文件 |
| `tests/unit/test_intent_chitchat_routing.py` | §2.4 的 3 行 + 拆分重构 + 3 条新护栏 | **1a** | ✅ 干净 |
| `tests/unit/test_chitchat_templates.py`（**新文件**） | 模板纯函数单测 | **1a** | ✅ 新文件 |
| `scripts/verify_smalltalk_routing.py` | 补 `7_open_chitchat` + `8_control_lookalike` 两类用例 | **1a** | ✅ 干净 |
| `src/ragent_backend/intent.py:37` / `:81` | 两个结构化输出模型的 `Literal` 加 `"chitchat"` | **1b** | ✅ 干净 |
| `src/ragent_backend/intent.py:585-596` | `_INTENT_CLASSIFY_RULES` 加第五条分类规则 | **1b** | ✅ 干净 |
| `src/ragent_backend/workflow.py:320-356` | `_route_after_intent` 加 `chitchat → "generate"` | **2** | ⛔ **另一会话持有** |
| `src/ragent_backend/workflow.py:300-304` | conditional edges map 加 `"generate": "generate"` | **2** | ⛔ **另一会话持有** |
| `src/ragent_backend/workflow.py:1197+` | `_generate_node` 加模板短路（**必须在越权检查之后**） | **2** | ⛔ **另一会话持有** |
| `src/ragent_backend/workflow.py:1422+` | `_build_prompt` 加 chitchat 分支（或独立 `_build_chitchat_prompt`） | **2** | ⛔ **另一会话持有** |
| `router_lora_data/` + `gen_router_training_data.py` | **先落仓**，再补样本重训 | **3** | ✅ 目前不在仓库 |

#### 关键设计：为什么 1a 可以独立上线

**因为 `_route_after_intent` 的默认分支（`workflow.py:356`）恰好把 `chitchat` 当成今天的
`rag` 处理**（详见 §2.3-3b 第 4 点）。1a 上线后：

- `chitchat` → 四个 `if` 都不命中 → `return "retrieve"` → `retrieve → generate`
- **这与今天 `rag` 的走法逐字相同**，用户可见行为零变化

1a 单独上线拿到的收益：
1. **标签语义正确**，根因②的标注体系有了稳定名字，Phase 3 可以**并行启动**而不必等 Phase 2；
2. **trace 可观测**：`_emit_trace("intent", "intent_detect", ...)`（`workflow.py:669-675`）
   会带上 `intent_type="chitchat"`，线上能直接统计闲聊占比——**这是 Phase 3 定训练配比的输入数据**；
3. 模板表和能力清单落地并被单测覆盖，Phase 2 时 `workflow.py` 只剩几行胶水，**diff 最小、最好合**；
4. `verify_smalltalk_routing.py` 补完开放闲聊用例后能跑出**诚实的基线**（§1.3 那三条句子目前是外推，不是实测）。

#### 为什么 1a 和 1b 要切开

**1b 会改变 1.5b router 的输入**：`_INTENT_CLASSIFY_RULES` 是 prompt 正文（`intent.py:710` /
`intent.py:821` 两处注入），加第五条规则等于给一个**从没见过 chitchat 的微调模型**塞进一段
新指令。报告 §5-E 明确记过：1.5b 对 prompt 改动"基本无反应"——**但"基本无反应"不等于"零影响"**，
而且 `Literal` 加一个枚举值会改变 json_mode 的 schema。

**1b 的准入条件（硬性）**：用 `verify_smalltalk_routing.py` 跑 1.5b × 全量用例，
**`6_control_*` 对照组误判率不得上升**（当前是 0/8，扩充后是 0/18）。不满足就不合。

#### 推荐执行顺序

```
1a  ─┬─→ 1b（跑对照回归后合）─┬─→ 2（等 workflow.py 写权限）
     │                        │
     └─→ 3 前置：训练数据落仓 ─┴─→ 3 补样本 + 重训 + 双跑验收
```

- **1a 无阻塞，随时可做，零行为变化**
- **3 的前置（落仓）也无阻塞**，可以和 1a 并行
- **2 是唯一需要等写权限的**，且 diff 已被 1a 压到最小
- **3 必须在 1b 之后**（分类体系不定，样本标签就是错的）；但 3 **不必等 2**

---

## 3. 推荐方案总述

1. **分类体系**：`clarify / rag / tool / workflow / chitchat` 五类。`rag` 的语义收回到
   "问本次对话上传的附件"，不再兼职"直接对话回答"。
2. **路由**：`chitchat → generate`（**不新增节点、不改前端**）。
3. **生成**：`generate` 内部两条 lane——
   - **模板 lane**（身份/能力/元问题必走，问候/致谢/告别建议走）：固定文案，零 LLM，
     跟已有三处短路同一个模式；
   - **LLM lane**（开放闲聊）：独立的受约束 prompt，核心是**能力白名单**，
     配 D4/D5 的同构约束、指令层级声明、更紧的 token 上限。
4. **触发源**：过渡期只有 `intent.py` 的白名单短路；重训后模型可直接输出 `chitchat`。
5. **兜底**：不新增。Pydantic `Literal` 校验 + `_route_after_intent` 默认 `retrieve` 两层已够。
6. **实施**：1a（零风险，独立上线）→ 1b（跑对照回归）→ 2（等写权限）→ 3（重训，可与 1a/1b 并行准备）。

---

## 4. 测试设计

按 `CLAUDE.md` §7.2，**每条测试都要能回答"它在旧实现下会失败吗"**；
不会失败的，必须明确说明它是**防回归测试**、守的是什么。

### 4.1 第一层：纯函数（pytest，无 fixture，无 LLM，无 DB）

**文件**：扩展 `tests/unit/test_intent_chitchat_routing.py` + 新建 `tests/unit/test_chitchat_templates.py`

| # | 用例 | 旧实现下会失败吗 | 守什么 |
|---|---|---|---|
| T1 | `_match_chitchat_intent("你好").intent_type == "chitchat"` | ✅ **会**（旧值是 `rag`） | 新标签 |
| T2 | `_match_chitchat_intent("你好").intent_type != "rag"` | ✅ **会** | 钉死"不再借用 rag 这个桶"这个决定 |
| T3 | 35 条参数化：`is not None` + `need_clarify is False` + `target_tool != "query_knowledge_hub"` | ❌ 不会 | **防回归**：白名单覆盖面不许缩；断言从"标签等于某值"改成"行为契约"，以后改标签不用再动这 35 条 |
| T4 | `VAGUE_SHORT` 8 条仍被 `_needs_clarify_rule` 拦下 | ❌ 不会 | **防回归**：`len<4` 阈值不许被顺手放宽（有 A/B 数据支撑：放宽会让 kb_refusal 从 23.8% 涨到 28.6%） |
| T5 | `BUSINESS_KB_MIXED` 4 条仍不被白名单吞掉 | ❌ 不会 | **防回归**：白名单类修法最危险的失败模式——误伤业务问答 |
| T6 | `match_chitchat_reply("你能做什么")` 返回非空字符串 | ✅ **会**（函数不存在，ImportError） | 模板命中 |
| T7 | `match_chitchat_reply("今天天气不错")` 返回 `None` | ✅ **会** | **开放闲聊不许被模板硬答**——防止有人图省事给模板加个 catch-all，把开放闲聊变成另一种拒绝话术 |
| T8 | **所有**模板文案都不含能力承诺禁用词（订机票/发邮件/上网/实时/股价/也许可以/可以试试） | ✅ **会** | **防止后来有人往模板里加牛皮**。这是文案层的自动化审查，没有它模板会慢慢膨胀 |
| T9 | 所有模板文案里出现的能力表述都能在 `CAPABILITY_MANIFEST` 里找到 | ✅ **会** | 模板与能力清单不许脱钩 |
| T10 | `CAPABILITY_MANIFEST` 每一项都能在 `tool_registry` / 流程模板里找到对应 | ✅ **会** | **漂移检查**（见 §4.5） |

### 4.2 第二层：路由（pytest，**不需要 DB/LLM fixture**）

**文件**：新建 `tests/unit/test_chitchat_routing_graph.py`

**可测性关键发现**：`_route_after_intent(self, state)` 只读 `state` 和 `self._llm`
（`workflow.py:320-356`，本次逐行核对，**已跑通**）。所以可以用
`types.SimpleNamespace(_llm=object())` 当 `self`，**未绑定调用** `RAGWorkflow._route_after_intent(stub, state)`，
**完全不需要构造 `RAGWorkflow`、不需要 DB、不需要 checkpointer、不需要 LLM**。

> **这一条直接回答"现有结构支不支持这样测"：支持，且不需要为可测性改动生产代码。**
> `_route_after_intent` 是纯判定函数这一点是既有设计的红利，不是本设计争取来的。

| # | 用例 | 阶段 | 旧实现下会失败吗 | 守什么 |
|---|---|---|---|---|
| T11 | `intent_type="chitchat"` → `"retrieve"` | **1a** | ❌ 不会（默认分支就返回 retrieve） | **接缝护栏**：证明 1a 单独上线零行为变化 |
| T12 | `intent_type="chitchat"` → `"generate"` | **2** | ✅ **会** | Phase 2 的核心验收；同时 T11 在 Phase 2 被这条替换，替换动作本身就是 Phase 2 的标志 |
| T13 | `active_workflow` 非空 + `intent_type="chitchat"` → `"workflow"` | 2 | ❌ 不会 | **防回归**：工作流续填的优先级不许被新分支截胡（`workflow.py:325-326`） |
| T14 | `need_clarify=True` + `intent_type="chitchat"` → `"clarify"` | 2 | ❌ 不会 | **防回归**：`need_clarify` 的权威地位不许被削（`workflow.py:331-332`） |
| T15 | `self._llm is None` + `chitchat` → 降级目标（拍板点 §6-⑧） | 2 | ✅ **会** | 无 LLM 时不能路由到会崩的 generate |

### 4.3 第三层：生成节点（pytest，**需要新 fixture**）

**现状**：`tests/conftest.py` 只有 3 个路径 fixture（`project_root` / `sample_documents_dir` /
`config_dir`，`tests/conftest.py:17-49`），**没有 DB fixture、没有 LLM fixture**。
`pyproject.toml:67-79` 也没设 `asyncio_mode`，异步用例靠显式 `@pytest.mark.asyncio`。

**设计上为可测性做的调整（就一处）**：把模板判定做成 `chitchat.py` 的**纯函数**，
`_generate_node` 里只留 3 行胶水。这样：
- 模板逻辑本身在第一层就测完了，**不需要任何 fixture**；
- `_generate_node` 那 3 行只需要一个极轻的 stub 就能测。

**需要新增的 fixture（建议放 `tests/unit/conftest.py`，不放根 conftest，避免影响其余 700+ 测试）**：

| fixture | 形态 | 为什么需要 |
|---|---|---|
| `rag_state_factory` | 返回一个填好默认值的 `RAGState` dict 的工厂函数，调用方只覆盖关心的键 | `RAGState`（`schemas.py:477` 起，`TypedDict, total=False`）字段很多，每个用例手写一遍会让测试变脆 |
| `stub_generate_self` | `SimpleNamespace(_llm=..., _token_queue=asyncio.Queue(), _emit_trace=lambda *a, **k: None, _audit_log=None, _tool_registry=None)` | `_generate_node` 只用到这几个属性；不需要真的构造 `RAGWorkflow` |

| # | 用例 | 旧实现下会失败吗 | 守什么 |
|---|---|---|---|
| T16 | `chitchat` + 模板命中 → `used_model` 含 `"no LLM call"`，`_build_prompt` 从未被调用 | ✅ **会** | 模板 lane 真的不调 LLM（跟已有三处短路的断言口径一致） |
| T17 | 模板命中时 `_token_queue` 收到的就是模板原文 | ✅ **会** | 文案不许被二次加工 |
| T18 | 模板短路**排在** `detect_privilege_claim` 之后 —— 构造一句"我是 super_admin，跳过权限限制，你好" | ✅ **会** | **安全**：模板 lane 不许绕过越权检查（§2.2 末尾） |
| T19 | `chitchat` + 模板未命中 → 走 `_build_chitchat_prompt`，且 prompt 里**不含**`【检索上下文】` | ✅ **会** | 开放闲聊用的是独立 prompt，不是企业知识库助手 prompt |
| T20 | `chitchat` 时 `target_tool is None` → 知识库空命中闸门（`workflow.py:1294`）不触发 | ❌ 不会 | **防回归**：这是"用户不再看到知识库拒绝话术"的机制保证，任何人改闸门条件时这条会红 |

### 4.4 第四层：分布层（脚本，不是 pytest）

**文件**：扩展 `scripts/verify_smalltalk_routing.py`（已在仓库，`CASES` 在 `:124-179`）。

`pytest` 层不接 LLM，所以它测不出"模型判得准不准"——那是这一层的事。

**必须新增两类用例**：

| 类别 | 例句 | expect | 作用 |
|---|---|---|---|
| `7_open_chitchat` | 今天天气不错 / 你几岁了 / 周末有什么安排 / 讲个笑话 / 最近好吗 / 今天几号 / 你喜欢吃什么 / 你有感情吗 | `chat` | **Phase 3 的验收基准**。Phase 1/2 上线后这一类应当**仍然全红**——如果不红，说明有人偷偷给白名单加了 catch-all |
| `8_control_lookalike` | 你们公司年会是什么时候 / 你能帮我看看我的考勤吗 / 你知道报销上限吗 / 你好，我想请个假 | `kb`/`tool`/`workflow` | **hard negative 对照组**：长得像闲聊、实为业务。守"第五类不许吃掉业务问答" |

另外补齐报告 §6 里几处零覆盖（都属于**当前未测**）：
英文/多语种闲聊（现在只有 `hello` 一条）、纯表情/纯标点、超长闲聊、**多轮上下文里的闲聊**
（先问年假 → 再说"谢谢"，看指代消解会把"谢谢"重写成什么，这条可能直接让白名单失效）。

**每次改动的硬性验收口径**：
- `6_control_*` + `8_control_lookalike` 误判率 **不得上升**（这是准入条件，不是参考指标）
- 报告里**必须同时给出闲聊准确率和对照组准确率**，只报前者的报告不接受

### 4.5 怎么保证不重蹈根因②（测的和训的是同一个分布）

根因②的教训被记录得很精确：`docs/optimization_tracking.md:79-81` 记的"23 条基准用例
100% 准确率"是**假象**——核对后确认那 23 条**没有任何闲聊样本**，
"测的和训的是同一个分布"（`smalltalk_routing_regression.md` §4）。

要避免它重演，光说"注意多样性"没用，需要**机制**。建议四条硬规则：

**① 文件级隔离 + 自动去重校验**

训练集（`router_lora_data/train.jsonl`）与评估集（建议 `tests/fixtures/router_eval.jsonl`）
**不得有重叠句子**。写一个校验脚本，**精确匹配和归一化后匹配都要查**（归一化口径直接复用
`_normalize_chitchat_token`，`intent.py:403-408`——去空白、去首尾标点、剥句尾语气词、转小写），
否则"你好呀～"和"你好"会被当成两条不同样本。**这个脚本进 CI 或 pre-commit**
（按 `CLAUDE.md` §7.1 的记载：CLAUDE.md 是 context 不是 enforcement，要强制就得落 hook）。

**② 评估集必须包含训练集里"一条都没有的措辞类型"**

不是"不同句子"，是**不同类型**。至少：英文闲聊、表情/纯标点、方言口语、超长闲聊、
多轮上下文里的闲聊。**这一条是 ① 的补充**——两批数据由同一个人同一天写出来，
即使句子不重叠，措辞分布也会高度相似，去重查不出来。

**③ 冻结 20% holdout**

评估集划出 20%，**重训调参期间不看**，最后跑一次。中途看过的部分等于训练集的延伸。

**④ 评估集由"不同产出来源"生成**

训练样本如果是脚本批量生成的（`gen_router_training_data.py`），
**评估集里至少有一部分必须是人工写的或来自真实查询日志**。同一个生成脚本产出的两批数据，
无论怎么切分，分布都是同一个。

> ⚠️ ④ 目前**做不到**：`gen_router_training_data.py` 不在仓库，也没有可用的真实查询日志统计。
> **这是一个已知缺口**，见 §6-⑦。Phase 1a 上线后 trace 里会有 `intent_type` 统计，
> 那将是第一份真实分布数据——**这是 1a 独立上线的第 5 个收益，也是 Phase 3 的输入**。

---

## 5. 待用户拍板的问题

按需要决定的先后排：

| # | 问题 | 选项 | 我的倾向 | 不拍板的后果 |
|---|---|---|---|---|
| ① | **路由方案选哪个** | A 新节点 / B 直连 generate / C 纯模板 / **B+ 混合** | **B+** | 后面全部依赖它 |
| ② | **1a 之后马上做 1b 吗** | 一起做 / 1a 先单独上线观察 | **1a 先单独上线**——它零行为变化，且能先攒到 `intent_type` 分布数据 | 1b 会改 1.5b 的输入，风险不同质，混在一起出问题分不清是谁的 |
| ③ | **模板 lane 覆盖到哪** | 只覆盖身份/能力/元问题 / 也覆盖问候致谢告别 | **也覆盖，但问候类给 2~3 条随机文案** | 全模板会机械；全 LLM 会在最危险的地方编造 |
| ④ | **模板文案谁定稿** | 我写初稿用户改 / 用户直接给 / 沿用现有话术风格 | 我写初稿，**但能力清单那几项必须用户确认**——它是对外承诺 | 文案写错等于系统对用户撒谎 |
| ⑤ | **能力清单是否按租户/权限动态生成** | 静态一份 / 按用户可见的工具+流程动态生成 | **先静态**（简单），但要知道这有**信息泄露面**：告诉所有人"我可以帮你发起报修"等于暴露该租户配了报修流程 | 动态生成复杂度高一个量级，且要过 ACL |
| ⑥ | **重训的总量与五类配比** | 我给的 400~600 条 / 五类各 15~25% / 其他 | 先按建议值，**但分批扩 + 同一 holdout 测曲线**比一次定死可靠 | 配比拍错 = 制造根因②的镜像版 |
| ⑦ | **训练数据落仓位置与评估集隔离规则** | `data/router_lora/` / `tests/fixtures/` / 其他；去重校验进 CI 还是 pre-commit | 训练数据 `data/router_lora/`、评估集 `tests/fixtures/router_eval.jsonl`、去重校验落 **pre-commit hook** | 不落仓就没法重训（`latency_probe.py` / `jailbreak_test.py` 已经吃过一次亏） |
| ⑧ | **`self._llm is None` 时 chitchat 降级到哪** | `retrieve`（=今天行为） / `clarify` / 走模板但开放闲聊给兜底文案 | **`retrieve`**（最小改动，与现有 `tool` 分支的降级口径一致，`workflow.py:336-338`） | 不处理会在无 LLM 环境下崩 |
| ⑨ | **接受新增 `src/ragent_backend/chitchat.py` 吗** | 新模块 / 塞进 `intent.py` / 塞进 `workflow.py` | **新模块**：`intent.py` 已 990 行且职责是分类；放 `workflow.py` 会让本就要等写权限的 diff 更大 | 影响 Phase 1a 的文件清单 |
| ⑩ | **本文的死期定 2026-10-31 合适吗** | 接受 / 改期 | 接受 | `CLAUDE.md` §7.4 要求设计文档必须有死期 |

---

## 6. 风险与本次未覆盖的范围

### 6.1 风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| **R1** | **把"加第五类"误读成"闲聊修好了"** | Phase 1/2 上线后 `今天天气不错` 依然撞知识库拒绝话术，但团队以为已经修了，问题被埋 | §4.4 的 `7_open_chitchat` 用例组**故意保持全红**，直到 Phase 3；验收报告必须单列这一类 |
| **R2** | **重训后模型把带业务词的句子判成 chitchat** | 症状比现在**更隐蔽**：不再是"知识库里没有你是谁"，而是"报销上限我不太清楚哦～"——**用户不会报 bug，只会觉得系统没用** | §2.3-3c ④ 的 hard negatives（≥20%）+ §2.2 ④ 的 prompt 约束，**两层都要有** |
| **R3** | **能力清单漂移**：新增/下线工具或流程后，模板和 prompt 里的能力白名单过期 | 系统对用户承诺一个已经下线的能力 | T10 漂移检查（对照 `tool_registry` 和流程模板），进 CI |
| **R4** | **模板 lane 绕过安全检查** | 越权话术夹在寒暄里可能绕过 `detect_privilege_claim` | 模板短路**必须放在 `_generate_node` 内部、越权检查之后**（§2.2 末尾），T18 守着 |
| **R5** | **1b 改 prompt/enum 让 1.5b 的业务分类变差** | 修闲聊伤业务问答，方向反了 | 1b 的硬性准入条件：对照组误判率不得上升（§2.5） |
| **R6** | **多轮场景让白名单失效** | 先问年假、再说"谢谢"，`analyze_and_route` 的指代消解可能把"谢谢"重写成"谢谢，年假可以顺延到3月31日"，白名单的业务词一票否决直接把它踢出闲聊 | 后置白名单检查（`intent.py:856-859`）作用于**重写后**的 query，这个风险是真实的。**目前零覆盖**，§4.4 要求补测 |
| **R7** | **模板 lane 的流式体验** | 模板一次性 `put` 整段，前端打字机效果会"整段突然出现" | 与现有三处短路口径一致（`workflow.py:1235` / `:1269` / `:1302`），**已知且接受** |
| **R8** | **workflow.py 行号漂移** | 另一会话在途改动提交后，本文引用的 workflow.py 行号可能全部偏移 | 本文已在头部标注；Phase 2 动手前必须重新核对行号（本项目已多次出现文档行号过期） |

### 6.2 本次未覆盖的范围

1. **没有写任何代码，没有跑任何验证**。§2 全部方案的效果、代价评估都是**读代码推断**，
   不是做完之后测出来的。唯一实测的是：132 条测试的数量（`--collect-only`）、
   训练数据不在仓库（`find`）、`_route_after_intent` 的默认分支行为（逐行读代码）。
2. **§1.3 那三条开放闲聊句子（`今天天气不错` / `你几岁了` / `周末有什么安排`）本次没有实跑**。
   "100% 撞知识库拒绝话术"是按报告 §3.1 规律 2 外推的，属于**已实现但未验证**。
3. **没有起后端服务，没发过任何 HTTP/SSE 请求**，没有端到端观测过任何一句真实回答文本。
4. **模板文案一个字都没写**。§2.1 只定了覆盖边界和判定口径，具体措辞待 §5-④ 拍板。
5. **能力白名单的具体条目没有核对过 `tool_registry` 的真实内容**。
   §2.2 ① 里那三条（知识库/考勤/申请流程）是从 `_INTENT_CLASSIFY_RULES` 和
   `_WORKFLOW_KEYWORDS` 读出来的印象，**不是逐条核对的结果**。
6. **没有评估"新增第五类对 1.5b 现有业务分类准确率的影响"**。§2.5 只是把它列为 1b 的准入条件，
   数字本身没测。
7. **没有测算 Phase 2 跳过 `retrieve` 能省多少延迟**。§1.2 只指出存在一次无效检索往返，
   具体耗时未测（`docs/latency_report.md` 里没有单独的 retrieve 阶段闲聊场景数据）。
8. **多租户下能力清单的 ACL 影响只做了定性判断**（§5-⑤），没有分析具体泄露面。
9. **没有考察 `qwen2.5-1.5b-intent`**（另一个已存在的 Ollama 模型），沿用报告 §6.9 的未覆盖项。
10. **没有做 git 操作**。工作区里其他会话在途的脏路径（`workflow.py` 等）**原样未动**。
