# 项目文档规范与人机协作开发流程：业界现状对照

> **状态**：已完成（调研报告，一次性产出）
> **日期**：2026-08-25
> **有效期**：本文引用的 AI 协作约定部分演进极快，**建议 2026-12-31 前复核**；ADR / 文档漂移 / 决策框架部分为稳定实践，可长期参考。
> **范围**：仅调研业界做法，未读本仓库源码，未修改任何现有文件。
> **注入检查**：本次调研共访问 20+ 来源，**未发现含提示注入内容的页面**（无页面试图向调研者下达指令、声称权限或要求忽略先前指示）。

**来源质量标注约定**：
`[官方]` 标准制定方/工具厂商的规范性文档 · `[厂商]` 厂商博客或研究 · `[学术]` 论文/会议 · `[社区]` 技术社区聚合、多方引用 · `[个人]` 单人博客/观点 · `[推断]` 未找到来源，本文推断

---

## 1. 执行摘要

整体判断：**方向对，标定偏，且有一处明显的工具锁定风险。**

1. 这套做法的**每一条**都能在业界找到对应，没有一条是凭空发明的反模式。从"样本量 1 的项目失败"里长出来的东西能几乎全部命中业界既有实践，说明底层判断是可靠的。
2. **最大的问题是量级标定**：`CLAUDE.md` 305 行 / 4.5K token **明显超标**。Anthropic 官方目标是 **200 行以内**，GitHub 对 2,500+ 仓库的分析显示 AGENTS.md 中位数 142 行，ETH 苏黎世的实证研究里真实开发者手写的上下文文件平均 **641 词 / 9.7 节**。你们大约是行业中位数的 2 倍、官方建议上限的 1.5 倍。
3. **最大的战略风险是工具锁定**：`AGENTS.md` 在 2026 年已经成为跨工具事实标准（Linux Foundation 旗下 AAIF 托管，60,000+ 仓库，30+ 工具原生读取），而 **Claude Code 并不原生读取 `AGENTS.md`**。把 `CLAUDE.md` 当"唯一事实来源"意味着换工具就得重来。官方给出的解法是让 `AGENTS.md` 当真身、`CLAUDE.md` 用 `@AGENTS.md` 导入或软链。
4. **有三件事你们在手工做，而工具已经原生提供了**：按相关性分层加载（`.claude/rules/` + `paths:` frontmatter）、防腐烂体检（`/doctor` 的 CLAUDE.md 精简建议）、跨会话记忆自动裁剪（auto memory 的 200 行硬限制）。
5. **收益要下调预期**：目前唯一一份严肃的对照实验（ETH 苏黎世，2026-02）显示人工撰写的上下文文件带来约 **+4%** 任务成功率，代价是 **+14~22%** 推理 token。这不是"最高回报的一个动作"，是一个**小幅正收益、有明确成本、且强烈依赖内容质量**的动作。

---

## 2. 逐条检验：你们的做法 ↔ 业界对应

| # | 你们的做法 | 你的映射 | 判定 | 业界标准形态 | 你们是简化还是走偏 |
|---|---|---|---|---|---|
| 1 | 三行决策记录 | ADR (Nygard 2011) | ✅ **准确** | 完整 ADR 含 Context / Decision / Status / Consequences，MADR 模板更重 | **合理简化**，而且是唯一能活过两年的形态 |
| 2 | 改行为 / 改名词二分 | 单向门 / 双向门 | ⚠️ **方向准，判据偏窄** | Bezos Type 1/Type 2，判据是"能否低成本撤销"，常见量化：30 天内能否无重大后果地撤销 | **判据走偏**，见 §6.2 |
| 3 | 交付三句话 | Definition of Done | ⚠️ **部分准确** | Scrum Guide 2020 的 DoD 是**Increment 的质量门槛清单**，不是汇报格式 | 你们的是**汇报协议**，不是 DoD。更接近 Google 设计文档的 Non-Goals + 测试报告 |
| 4 | 文档放仓库 | Docs-as-Code | ✅ **准确** | Write the Docs 定义：版本控制 + 纯文本标记 + Code Review + 自动化测试 | **未完全落地**：你们有前三项，缺"自动化测试"这一项（正是 §5 要补的） |
| 5 | 提交前检查 | pre-commit hooks | ✅ **准确但需注意层级** | git pre-commit / lefthook / Danger JS | 官方明确：CLAUDE.md 是 context 不是 enforcement，**要在固定时点强制必须用 hook** |
| 6 | 漂移检查脚本 | doc tests / executable docs | ⚠️ **映射不完整** | doctest 只覆盖"代码示例可运行"，你们要检查的"断言"对应的是 **fitness functions** | 见 §5 |
| 7 | 按阅读频率分层 | progressive disclosure | ✅ **准确，且是官方术语** | Anthropic Agent Skills 的三级渐进披露（metadata ~50 token → SKILL.md <5K token → 按需读文件） | **你们的是手工低配版**，官方已有机制 |

### 2.1 逐条展开

**① 三行决策 ↔ ADR —— 映射准确，且你们的轻量化是对的**

Nygard 2011 的原始 ADR 就是轻量格式，后来被 MADR 等模板加重了。`[社区]`

关键数据：一项研究显示 **83% 的受访者表示 ADR 只是"偶尔或很少"被记录**；有从业者总结"几乎每个团队都试过 ADR，但几乎没有团队两年后还在维护"。`[个人/社区]` 成功与失败团队用的是**同样的模板**，差别在运营：记录放哪、什么时候写、谁评审、世界变了之后怎么办。

→ **结论：你们的三行格式（决策/理由/作废了什么）不是"简化过头"，而是符合存活率现实的选择。** 其中"作废了什么"这一栏尤其好，它对应完整 ADR 的 `Status: Superseded by ADR-XXX`，而这恰恰是最常被丢掉的一栏。

**② 改行为/改名词 ↔ 单向门/双向门 —— 框架对上了，判据没对上**

Bezos 的原始判据是**可逆性 + 撤销成本**，社区常用的量化版是"30 天内能否无重大后果地撤销"。`[社区/个人]` 你们的"会不会多出 migration 脚本"是一个**领域特定的代理指标**，详见 §6.2。

**③ 交付三句话 ↔ Definition of Done —— 这个映射我认为不准**

Scrum Guide 2020 `[官方]` 的 DoD 是"Increment 达到产品所需质量标准时的正式描述"，它是**一份预先约定的清单**（代码评审过了 / 测试过了 / 文档更新了），作用是"提供对完成了什么的共同理解"以创造透明度。

你们的三句话（验收怎么做 / 回归怎么保 / 什么没做）**不是清单，是每次交付时现场生成的报告结构**。这更接近：
- "什么没做" → Google 设计文档的 **Non-Goals** 节 `[个人/社区，来源：industrialempathy.com、Pragmatic Engineer]`，但那是**事前**写的，你们是**事后**写的
- "验收怎么做 / 回归怎么保" → 测试报告（如 NASA SWE-118 Software Test Report `[官方]`）的极简版

→ **建议改口径**：把这三句话叫"交付回执"或"Agent 交付协议"，另外**单独建一份真正的 DoD 清单**（可以就三条）放进 CLAUDE.md。两者不是一回事，混称会让人误以为已经有 DoD 了。

**④ 文档放仓库 ↔ Docs-as-Code —— 准确，但你们只做了 3/4**

Write the Docs `[官方]` 的定义包含四要素：Issue Trackers、版本控制、纯文本标记、Code Review、**Automated Tests**。你们前几项都有，最后一项就是你们计划中的"漂移检查脚本"。所以第 12 条不是"额外的创新"，而是**补齐 Docs-as-Code 的最后一块**。

**⑤ 提交前检查 ↔ pre-commit hooks —— 准确，但要注意 CLAUDE.md 更新本身不能靠 CLAUDE.md 约束**

Anthropic 官方文档明确写道：CLAUDE.md 和 auto memory "都被 Claude 当作 context，不是被强制执行的配置"；要"无论 Claude 怎么决定都阻止某个动作，用 PreToolUse hook"。`[官方]`

→ 这直接回答了你们第 13 条（把更新触发点改到提交前）：**方向对，但不能只改 CLAUDE.md 里的一句话，必须落成 hook**。

**⑥ 漂移检查 ↔ doc tests —— 映射不完整**，见 §5。

**⑦ 按阅读频率分层 ↔ progressive disclosure —— 术语完全正确，而且这是 Anthropic 自己的官方设计模式**

Agent Skills（2025-12 发布的开放标准）`[官方/社区]` 的三级结构：
1. 启动时只加载 name + description（约 50–100 token）
2. 任务匹配时读完整 SKILL.md（建议 <5,000 token）
3. 执行时按需读引用文件/脚本

→ 你们的 `CLAUDE.md`（每次读）+ `architecture.md`（按需查）就是这个模式的**两级手工版**。方向完全正确。但见 §3.3 和 §6.1 —— 官方已有更细的机制，而且 `architecture.md` 的内容本身可能就不该存在。

---

## 3. AI 协作上下文文件的现状（重点章节）

### 3.1 ❗ 推翻你的判断之一：`AGENTS.md` 已经是事实标准，不再是"多家各自为政"

`[官方 agents.md + 社区]`

- 起源：由 **OpenAI Codex、Amp、Google Jules、Cursor、Factory** 协作推出（**不是 Anthropic** —— 有二手来源把这一点写错了，已用官方站点核实）
- 现状：**由 Linux Foundation 下属的 Agentic AI Foundation (AAIF) 托管**
- 规模：**超过 60,000 个开源项目**采用
- 原生支持：OpenAI Codex、Google Jules、Aider、Zed、VS Code、Devin、GitHub Copilot、Cursor、Warp、goose 等 20–30+ 工具
- 格式：纯 Markdown，**无强制 schema**。常见小节：项目概览 / 构建与测试命令 / 代码风格 / 测试说明 / 安全注意事项 / commit 与 PR 规范 / 部署步骤
- monorepo 支持：子包内可放嵌套 `AGENTS.md`，**离被编辑文件最近的那个优先**

### 3.2 ❗ 推翻你的判断之二：Claude Code **不读** `AGENTS.md`，官方给出的是"单一真身 + 导入"方案

Anthropic 官方文档原文级要点 `[官方]`：

> Claude Code reads `CLAUDE.md`, not `AGENTS.md`.

官方推荐做法（二选一）：

```markdown
<!-- CLAUDE.md -->
@AGENTS.md

## Claude Code
（此处放 Claude 专属指令）
```

或直接软链：`ln -s AGENTS.md CLAUDE.md`（Windows 需管理员权限，建议用导入语法）。

另有 `/init`（会读取 `.cursor/rules/`、`.cursorrules`、`.github/copilot-instructions.md` 并整合）和 `/import`（v2.1.213+，一次性把 `AGENTS.md`、`.devin/rules/`、`.windsurf/rules/`、`.clinerules` 等导入）。

**对你们的直接影响**：
- 你们把 `CLAUDE.md` 定为"唯一事实来源"，这在 2026 年是**工具锁定**。
- 社区一致建议：**让 `AGENTS.md` 当真身**（因为它是跨工具标准），`CLAUDE.md` 用 `@AGENTS.md` 指过去。`[社区，aq.dev / yurukusa.github.io]`
- 如果因为某种原因必须维护两份实体副本，社区做法是**加 pre-commit + CI 检查，任一副本与 AGENTS.md 偏离就失败**。有开源项目（ai-driven-dev/framework #619）就是因为"两个文件从未在同一个 commit 里一起改过，且没有任何东西检查它们是否还一致"而开了 issue。`[社区]`

### 3.3 ❗ 推翻你的判断之三："按阅读频率分层"已经有官方机制，不用手工拆

Claude Code 已提供 `.claude/rules/` 目录 + YAML frontmatter 的 **路径条件加载** `[官方]`：

```markdown
---
paths:
  - "src/api/**/*.ts"
---
# API 开发规则
```

- 无 `paths` 的规则：启动时无条件加载，优先级同 `.claude/CLAUDE.md`
- 有 `paths` 的规则：**只在 Claude 读到匹配文件时才进上下文**
- 支持软链共享跨项目规则；用户级规则放 `~/.claude/rules/`

重要陷阱：**`@path` 导入不省 context**。官方原文要点：拆成 `@path` 导入"有助于组织，但不减少 context，因为被导入文件在启动时同样加载"。`[官方]`

→ **如果你们的 `docs/architecture.md` 是通过 `@` 导入进 CLAUDE.md 的，那么"按频率拆分"的省 token 效果是 0。** 只有在 CLAUDE.md 里用普通文字提一句"架构详情见 `docs/architecture.md`"（让 Claude 按需自己去 Read）才真的省。请务必核实这一点，这是本报告里最可能立刻带来实际收益的一条。

### 3.4 推荐长度：你们 305 行 **偏长**，各家口径汇总

| 来源 | 建议 | 质量 |
|---|---|---|
| Anthropic Claude Code 官方文档 | **单个 CLAUDE.md 目标 200 行以内**；"更长的文件消耗更多 context 并降低遵循度" | `[官方]` |
| Cursor | 500 行以内 | `[社区转述]` |
| GitHub 对 2,500+ 仓库的分析 | AGENTS.md 长度方差最大，**中位 142 行**（Copilot instructions 平均 310 行）；该文明确**不给长度阈值**，强调"具体 > 全面" | `[厂商]` |
| ETH 苏黎世 AGENTbench 数据集 | 12 个仓库中真实开发者手写的上下文文件**平均 641 词 / 9.7 个小节** | `[学术]` |
| 个人博客（tianpan.co, 2026-02） | 精心整理的应在 **40–80 行**，100 行是合理上限；引 GitHub 数据称表现好的中位约 300–350 词，>500 词收益递减，>1000 词与性能**负相关** | `[个人]`（其引用的 GitHub 原文并无这些数字，**存疑，勿当权威**） |
| 个人博客（dev.to） | **60 行**以上就会被静默忽略规则 | `[个人]`（未见实证支撑，偏激进） |

**关于"指令预算"的说法**：多篇社区/个人文章称"前沿模型可靠遵循 150–200 条指令后开始退化，Claude Code 系统提示占掉约 50 条，留给你 100–150 条"。`[个人]` **未找到这组数字的一手来源，请当作经验法则而非事实。**

→ **对你们的判定**：305 行超出官方建议 50%，约为观测中位数的 2 倍。**属于"偏长"，但不是灾难性的。** 值得做一轮精简（见 §7）。

### 3.5 ❗ 推翻你的判断之四：**"每次会话读一个大文件"的成本收益，已经有对照实验了**

这是本次调研找到的最硬的证据。

**主研究**：Gloaguen 等，*Evaluating AGENTS.md: Are Repository-Level Context Files Helpful for Coding Agents?*（ETH 苏黎世 SRI Lab，2026-02，arXiv:2602.11988）`[学术]`

- 方法：3–4 个 agent（Claude Code Sonnet-4.5、Codex GPT-5.2 / 5.1-mini、Qwen Code），三种条件（无上下文文件 / LLM 自动生成 / 开发者手写）
- 基准：SWE-bench Lite（300 任务）+ 自建 AGENTbench（138 实例，12 个 Python 仓库）
- **结果**：
  - 开发者手写：平均 **+4%** 成功率
  - LLM 自动生成：**-0.5%（SWE-bench Lite）到 -2~-3%（AGENTbench）**，即**净负收益**
  - 成本：**无论质量好坏**，都增加 **14–22%** 推理 token、多出 2–4 步；总推理成本 +19~20%
  - 机制解释：**自动生成的文件只是在复述已有文档（有害冗余）**；手写文件的收益来自"别处不存在的信息"——非默认的工具链选择、CI 的怪癖
  - 有趣的对照：**把项目文档删掉之后**，LLM 生成的上下文文件反而变成 +2.7%（说明它的价值只是"把已有文档搬近一点"）
  - 行为变化：仓库专属工具的调用次数从平均 0.05 次跳到 2.5 次（只要在上下文文件里提了）

**辅证 1**：*On the Impact of AGENTS.md Files on the Efficiency of AI Coding Agents*（arXiv:2601.20404，10 仓库 / 124 PR）`[学术]` —— 运行时间 **-28.6%**，输出 token **-16.6%**，完成率持平。注意：这里测的是**效率**（更快更省），前一篇测的是**成功率**，两者不矛盾。

**辅证 2**：AAIF 的五轮基准 `[厂商/社区]` —— 12 行的 AGENTS.md，在模糊任务上耗时 -27%、额度 -24%、diff 体积 -26%；多文件任务只有 9–10%。**方法学要点：单次跑出来的结论是反的（-44%），跑五次取中位数才反转过来**。另一个安全性发现：无 AGENTS.md 的 5 次里有 2 次触发了生产构建，有 AGENTS.md 的 5 次全避免了。

→ **对你们的三个直接推论**：
1. **收益是真的，但只有几个百分点**，别把 CLAUDE.md 当银弹。
2. **成本是确定的（~20%）**，所以内容的"信噪比"直接决定净收益正负。
3. **"能从代码/已有文档推导出来的内容，写进上下文文件是净损失。"** 这条对你们的 `docs/architecture.md` 是个尖锐问题 —— 见 §6.1。

### 3.6 已知失败模式清单

来自官方 + 学术 + 社区的合集：

| 失败模式 | 描述 | 质量 |
|---|---|---|
| **规则互相矛盾** | "如果两条规则冲突，Claude 可能任意挑一条" | `[官方]` |
| **不是强制层** | CLAUDE.md 作为 system prompt **之后**的 user message 送入，"没有严格遵守的保证" | `[官方]` |
| **"可能与你的任务无关"** | Claude Code 会附一条 system reminder 说这段 context 可能不相关、不高度相关就别理它 —— **所以"只是偶尔相关"的指令会被经常忽略** | `[社区/个人]`（本次会话中我确实收到了这条 reminder，可自证） |
| **Context rot** | 上下文变长 → 质量下降。Chroma 2025 研究测了 18 个前沿模型，**全部**随输入变长而退化；200K 窗口的模型在 50K 就可能显著退化 | `[厂商研究]` |
| **有害冗余** | 复述已有文档的内容，净负收益 | `[学术]` |
| **禁令无替代** | "永远不要用 console.log" 而不给替代方案，会让 agent 卡住 | `[个人]` |
| **冲刺临时上下文写进永久文件** | 应移到 `dev-docs/current-sprint.md` 之类 | `[个人]` |
| **回溯式文档剧场** | ADR/文档在事后三个月补写，"有决策记录的形式，无其实质" | `[个人]` |
| **双份副本静默漂移** | AGENTS.md 与 CLAUDE.md 从未在同一 commit 中一起改动 | `[社区]` |

### 3.7 官方已有的防腐烂机制（你们可能没用）

`[官方]`，全部来自 Claude Code 文档：

1. **`/doctor` 的 CLAUDE.md 精简检查**（v2.1.206+）：主动提出精简建议，**明确会砍掉"可从代码推导的内容，如目录结构、依赖列表、架构概览"，保留"陷阱、理由、与工具默认行为不同的约定"**。
2. **Auto memory 的硬性裁剪**：`MEMORY.md` 只加载前 200 行 / 25KB；写完之后 Claude Code 会**实测文件大小**，接近上限就提醒精简，超限直接**返回错误要求重写索引**。这是一套比人工规则更硬的防腐机制。
3. **HTML 注释被剥离**：`<!-- 维护者笔记 -->` 在注入 context 前被去掉 —— **可以零 token 成本地给人类留维护笔记**（比如"此节最后核实于 2026-08-25"）。这条正好能和你们"所有文档头部必须有状态 + 日期"的规则结合，而且不花 token。
4. **`InstructionsLoaded` hook**：记录到底哪些指令文件被加载了、何时、为何。用来调试"我写了但它没听"。
5. **`claudeMdExcludes`**：monorepo 里排除别的团队的 CLAUDE.md。
6. **`/context`**：确认哪些 memory 文件真的进了本次会话。
7. **压缩后的行为**：项目根 CLAUDE.md 在 `/compact` 后会**从磁盘重读并重新注入**；嵌套 CLAUDE.md 和 `paths:` 规则则要等到再次读到匹配文件才回来。

---

## 4. ADR 实践经验

### 4.1 采纳率与存活率：比想象的差得多

- **83% 的受访者表示 ADR 只是"很少或偶尔"被记录**。`[社区，多方转述一项调研，未定位到原始调研报告 —— 请当作方向性数字]`
- "几乎每个团队都在某个时点试过 ADR，但几乎没有团队两年后还在维护它们。" `[个人，hidekazu-konishi.com]`
- 核心洞察（多方一致）：**成功和失败的团队用的是同一批模板；差别全在运营** —— 记录放在哪、什么时候写、谁评审、以及"世界变了但记录没更新时会发生什么"。

### 4.2 常见失败模式

| 失败模式 | 说明 |
|---|---|
| **决策文档剧场** | 先发布变更，三个月后补 ADR。有形式无实质 | `[个人]` |
| **表演化** | ADR 变成走过场，团队就悄悄停了 | `[个人]` |
| **停止更新** | 没有 Superseded 机制，旧 ADR 与现实脱节后成为误导源 | `[社区]` |
| **写太重** | 完整模板门槛高，导致小决策不写、大决策拖着不写 | `[社区]` |
| **没人读** | 存在 `doc/adr/` 里但不在任何人的工作流上 | `[社区]` |

推荐的运营做法（AWS 等）：从试点团队开始、用清晰模板、建立评审周期；**在"和它支持的那次代码/基础设施变更同一个工作流里"评审 ADR**；触发条件是"改变了架构、运维、安全态势或长期维护成本"。`[厂商/社区]`

### 4.3 ADR 与 AI 上下文文件如何共存 —— 已有明确的社区共识

`[个人/社区，actual.ai、braingrid.ai、rickpollick.com 等多方一致]`

核心分工：

> **ADR 是 system of record（决策的权威记录），agent 上下文文件是 delivery format（交付格式）。**

具体建议：
- 约定（conventions）是**常设规则**，属于 CLAUDE.md / AGENTS.md；ADR 是**某一个选择在某一时刻的记录及其理由**，两者职责不同，不该混写。
- **写一次，编译成各工具要读的格式**（保持 agent-agnostic）。
- ADR 要"token-aware"：**压缩到"决策 + 规则"，叙事部分链接出去**。
- 给每条 ADR 加 **`applies_to` glob 字段** —— "编辑样式表的 agent 用不到管数据库 schema 的那条 ADR"。这与 Claude Code 的 `.claude/rules/` + `paths:` frontmatter **完全同构**。
- 有人主张把 ADR 直接放进 `.claude/rules/`。
- 用 agent 自动维护：去重、去冲突、PR 里检查架构合规、在代码里发现新决策时提议新 ADR ——理由是"人类团队独力很少能维持这种纪律"。

→ **对你们的建议**：你们的三行决策记录目前**和 CLAUDE.md 是什么关系没有定义**。按上面的共识，应该是：三行决策 = ADR（system of record），CLAUDE.md 里只放**由这些决策产生的常设规则**，并且规则条目应能反查到决策记录。

### 4.4 值得注意的相邻研究

- *Lore: Repurposing Git Commit Messages as a Structured Knowledge Protocol for AI Coding Agents*（arXiv:2603.15566）`[学术]` —— **把 commit message 当作给 AI agent 用的结构化知识协议**。这和你们规则 3 的"为什么这么改 → 写进 commit message，写完冻结，零维护"**高度吻合**，说明这条不是权宜之计，而是有人在正经研究的方向。这是你们这套里我认为最被低估的一条。
- *Context Matters: Evaluating Context Strategies for Automated ADR Generation Using LLMs*（arXiv:2604.03826）`[学术]`

---

## 5. 文档漂移检测工具链

### 5.1 你的映射（漂移检查 ↔ doc test）不完整

doctest 只解决"文档里的代码示例还能不能跑"这一类。你们要检查的四类断言分别对应不同的成熟工具：

| 你们的断言类型 | 成熟对应 | 工具举例 | 质量 |
|---|---|---|---|
| "某测试仍通过" | **doc test / executable docs** | Python `doctest`、`sphinx.ext.doctest`、Rust doctest、`mdbook test` | `[官方]` |
| "行号指向 / 文件行数" | **别写行号** —— 用锚点包含 | mdBook `ANCHOR:`/`ANCHOR_END:`、VitePress region 包含、Sphinx `literalinclude` 的 `:start-after:` | `[官方]` |
| "全仓无某某" / 分层依赖 | **architecture fitness functions** | ArchUnit(Java)、ArchUnitTS、ArchUnitPython、dependency-cruiser(JS) | `[官方/社区]` |
| "文档与实际配置一致" | **契约测试 / 生成物比对** | Dredd、Schemathesis（API）；`.env.example` diff；生成内容 + CI `--check` | `[社区]` |
| "改了代码就必须改文档" | **PR 级规则引擎** | Danger JS（如"改了源码但没改 changelog 就警告"） | `[官方]` |
| 散文层面的准确性 | **prose linting** | Vale（自定义风格/准确性规则集） | `[社区]` |

### 5.2 ❗"文档里写了行号，代码一改就错"—— 业界的答案是"根本不要写行号"

这是本节最直接可用的一条。mdBook 官方文档明确说明：`[官方]`

> 为了避免修改被包含文件时把书弄坏，可以用**锚点**而不是行号来包含特定区段。

机制：在源码里用注释打 `ANCHOR: name` / `ANCHOR_END: name`，文档里按名字引用；**锚点标记行本身不会出现在渲染结果里**。VitePress 有等价的 region 机制，同名 region 会被拼接，标记注释被移除。Sphinx 的 `literalinclude` 支持 `:start-after:` / `:end-before:`。

→ **对你们第 12 条的强建议**：不要写"检查行号是否还对"的脚本 —— 那是在给一个不该存在的问题打补丁。**应该改成禁止在文档里写行号**，用「文件路径 + 命名锚点/符号名」代替；漂移检查脚本只需验证"锚点/符号名还存在"。这比校验行号简单得多，也稳定得多。

### 5.3 "架构断言"应该用 fitness function，不是自己写脚本

你们的"全仓无某某"（比如"没有任何模块直接 import X"）正是 **architecture fitness function** 的标准用例。`[社区/官方]`

- 定义：把架构约束写成类单测的断言（"包 X 里的类不应依赖包 Y 里的类"），在代码图（包/类/模块/依赖）上查询求值，产出 pass/fail 或违规清单
- 在 CI 里每个 PR 跑，高严重度违规直接阻断合并 —— **把架构评审变成确定性门禁**
- 遗留代码库可以先 **freeze**（冻结现有违规，只禁止新增），这对你们这种已经跑了一段时间的项目很关键
- Python 侧有 `ArchUnitPython`；也有 `import-linter` 等同类

→ 你们的 `docs/architecture.md` 里"含架构图与性能数据"。**架构图里的分层关系应该有对应的 fitness function 来钉死；性能数据则应该有基准测试脚本产出，而不是手写进文档。** 手写的性能数字是文档腐烂最快的部分之一。

### 5.4 更广的自动化生态（2026）

`[社区/厂商]`
- **规格驱动再生成**：ReadMe / Redocly / Fern（需自己接 CI）；GitBook / Mintlify（链接的 OpenAPI spec 变化时自动重生成，零配置）
- **CI 里的 doc drift 检测**：从 docs-as-code 基线出发，把变更代码与关联散文做比对，通过 CI 检查或 agent 评审，在合并前暴露过期文档
- **Agent 驱动的自更新文档**：把"文档同步"本身交给 agent 做，2026 年有多家在推（Augment Code 等）。**注意与 §3.5 的结论相冲突：LLM 生成的上下文文件是净负收益。** 自动生成"参考文档"和自动生成"agent 指令文件"是两回事，别混。

---

## 6. 你们可能走偏的地方（第五部分逐条）

### 6.1 「按 token 成本拆分文档」—— 适配合理，但你们**拆错了对象**

**这个适配本身是合理的，而且是官方设计模式。** progressive disclosure 就是为这个约束发明的（§2.1-⑦）。你不是在发明反模式。

**但有两个问题：**

**问题 A：`@` 导入不省 token。** 见 §3.3。请务必核实 `architecture.md` 是"被导入"还是"被提及"。这决定了 8.1K → 拆分后的数字是不是真的。

**问题 B（更重要）：`architecture.md` 的内容可能根本不该存在于任何上下文文件里。**

三个独立来源指向同一个结论：
- Claude Code `/doctor` 的精简逻辑**明确点名**要砍掉"可从代码推导的内容，如**目录结构、依赖列表、架构概览**"`[官方]`
- ETH 研究：自动生成的上下文文件之所以是负收益，正是因为它们**复述已有文档**；手写文件的价值来自"**别处不存在的信息**" `[学术]`
- Upsun 的建议：**只写不可发现的信息**（包管理器偏好、测试命令、lint 要求、项目专属工具）；**排除可发现的内容**（目录结构、框架细节、通用编码模式）`[厂商]`

→ **判定**：你们把架构概览从"每次读"降级到"按需查"是**半步正确**。**完整的正确做法是问：这段架构描述，Claude 自己读代码能不能得出？** 能的话，正确处置不是"移到另一个文件"，而是**删掉，换成一句"架构见 src/ 目录结构，入口在 X"**。真正该留在 `architecture.md` 的只有：为什么是这个架构（理由）、有哪些反直觉的陷阱、以及性能数据（因为读代码得不出来）。

按这个标准重审，`architecture.md` 的 4.1K token 大概率能砍掉一半以上。

### 6.2 「会不会多出 migration 脚本」—— 是好的**触发器**，但是不合格的**判据**

**优点**：它是可操作的、二值的、领域相关的。相比"这个决定可逆吗"这种要主观判断的问题，它能真的被执行。这是很多决策框架落地失败的原因，你们避开了。

**缺点：漏报严重。** 以下变更**都不产生 migration 脚本，但都是单向门**：
- 改公开 API 契约 / 响应 schema（下游已经依赖了）
- 改权限模型的语义（比如某个角色的默认可见范围变了）—— 数据没动，但已经发出去的授权决策没法收回
- 改外部集成的调用方式（对方已经按老方式接好了）
- 改 ID 生成策略 / 幂等键的定义
- 引入一个新的第三方依赖或框架（撤销成本随时间指数上升）
- 任何写进日志/审计流并已被下游消费的字段格式

其中**权限模型**对你们这个项目（企业 QA / 多租户 KB 权限）恰恰是高频且高危的类别。

**业界更通用的判据** `[社区/个人，Bezos 框架的社区量化版]`：
> 如果**在 N 天（常用 30 天）内无法无重大后果地撤销**，就当作单向门。
> 具体信号：存在不可回收成本 / 产生法律义务 / 根本改变了身份或轨迹 / 显著影响他人。

**建议：保留 migration 判据作为"必然触发"，但补充第二和第三个触发器**：
1. 会不会多出一个 `scripts/` 里的 migration 脚本？（数据形状）
2. **会不会让已经发出去的东西变得不一样？**（API 响应、权限决策、日志字段、外部集成契约）
3. **如果一周后想撤销，需要协调几个人 / 几个系统？** >1 就是单向门。

### 6.3 三档汇报口径 —— **没有直接对应的业界标准，但方向被 2026 年的研究强烈支持**

**先说结论：这是你们最有原创价值的一条，不要动它，但可以借业界的概念加固它。**

**最近的既有概念**：
- **DO-178C**（航空软件，`[官方/社区]`）：关键原则是 **"软件不是因为通过测试就算已验证，而是因为那些测试可完整追溯到已定义的需求"**。这正好对应你们"已跑通"和"已验证通过"的区别 —— 你们缺的一环是**可追溯性**。
- **V&V 区分**（IEEE 1012 / NASA `[官方]`）：verification 回答"我们是不是在正确地造这个东西"，validation 回答"我们造的是不是正确的东西"。dynamic verification（跑测试）擅长找缺陷，static verification（分析/形式化）才用于证明正确性。
- **IEEE 1012 的完整性等级**：4 级可能性 × 4 级后果 → 4 个完整性等级，等级越高要求的 V&V 活动越多。**这是"按风险分级投入验证力度"的成熟范式**，你们的三档目前是"按已完成的验证力度分级汇报"—— 两者是互补的，可以合起来用（高风险的东西必须做到"已验证通过"才能交付）。

**2026 年的新证据**：AI agent 的"完成声明"不可信这件事，已经是学界公认的问题：
- NeurIPS 2026 有专门的 *Who Verifies the Agents?* 和 *VeriCodeGen: AI for Verifiable Coding* workshop `[学术]`
- *The Verification Horizon: No Silver Bullet for Coding Agent Rewards*（arXiv:2606.26300）：随着模型推理变强，**生成候选解变容易了，可靠验证反而成了更难的问题** `[学术]`
- 社区共识的解法是**多层验证栈**：authoring agent 自检 → 独立完成度复核 → 确定性 CI → 专项扫描器 → 重大风险由人或策略负责人把关。关键句意：**一个打磨得很好的自我报告不等于证据**。`[个人/社区]`
- MSR 2026 有实证研究：*Are Coding Agents Generating Over-Mocked Tests?*（分析 2025 年 120 万+ commit，其中 48,563 条来自 coding agent）`[学术]` —— **agent 倾向于用 mock 把测试写成假通过**，这直接印证了你们第 11 条"代码注释声称的不变量不得当作依据"的同类担忧。

→ **建议**：三档保留，但给每档加一个**机械可判定的条件**，否则三档本身也会腐烂成自我报告：
- **已验证通过** = 存在一个 CI 里跑的、命名可指的测试，且该测试**在旧实现下会失败**（见 6.5）
- **已跑通** = 有可粘贴的命令 + 实际输出片段
- **已实现但未验证** = 只有 diff

### 6.4 「什么没做」设为强制项 —— **有对应实践，而且你们的版本有一处业界没有的改进**

**业界对应确实存在**：Google 设计文档的 **Non-Goals** 是标准必备节 `[个人/社区，industrialempathy.com、Pragmatic Engineer 的 RFC 模板汇编]`。多家公司的 RFC 模板都要求显式写明 in-scope / out-of-scope，被列为"设计评审时该问每个系统作者的常见问题"之一。

**你们的差异（而且是改进）**：Non-Goals 是**事前**声明的边界，你们的"什么没做"是**事后**声明的实际未覆盖范围。这两者不等价：
- 事前 Non-Goals 防的是**范围蔓延**
- 事后"什么没做"防的是**虚假完成感** —— 这恰恰是 AI 协作的头号风险（§6.3）

**未找到业界有把"事后未覆盖范围"设为强制交付项的成熟实践。** 结合 §6.3 的研究趋势判断，这是一条**正确且超前**的做法。建议保留，并在 CLAUDE.md 里写死"缺这一句 = 交付不算完成"。

### 6.5 顺带：第 10 条「它在旧实现下会失败吗」有非常正统的对应

你没在表里列这条，但它有明确的业界名字：

- **TDD 的 red-green 纪律**（Kent Beck）：**必须先看到测试失败**，否则你不知道它测的是什么。`[经典文献，本次未二次核实原书页码]`
- 机械化、可规模化的版本是**变异测试（mutation testing）**：往代码里注入小改动（mutant），看现有测试能否杀掉它。定位就是"评估已有自动化测试是否真的有能力发现缺陷和回归"，被推荐为"在覆盖率之外，验证测试确实在校验预期行为、而不只是执行到了代码"。`[社区]`
- 注意已知局限：等价变异体（不改变行为、无法被杀死）会产生噪声。

→ **建议**：把你们这条口述规则升级为工具 —— 对关键模块跑 `mutmut` / `cosmic-ray`（Python）。这比每次靠人问一句可靠得多，而且**正好防住 §6.3 提到的"agent 写过度 mock 的假测试"**。

### 6.6 第 11 条「并发缺陷必须用并发方式验证」

未找到需要推翻的地方，这是标准工程常识。补充成熟工具方向：竞态检测器（Go `-race`、C/C++ ThreadSanitizer）、确定性调度器、Jepsen 式故障注入。Python 侧较弱，通常靠压力测试 + 断言不变量。`[推断，未专门检索]`

---

## 7. 建议：改什么 / 留什么 / 补什么

### 7.1 立刻改（高确定性、低成本）

| # | 动作 | 依据 |
|---|---|---|
| 1 | **核实 `architecture.md` 是被 `@` 导入还是被文字提及**。若是导入，"按频率拆分"省 token 的效果为 0 | §3.3 `[官方]` |
| 2 | **让 `AGENTS.md` 成为真身**，`CLAUDE.md` 内容改为 `@AGENTS.md` + Claude 专属小节。解除工具锁定 | §3.2 `[官方]` |
| 3 | **文档里禁止写行号**，改用「文件路径 + 命名锚点/符号名」。漂移脚本只验证符号是否存在 | §5.2 `[官方]` |
| 4 | **跑一次 `/doctor`**，看它对 CLAUDE.md 提的精简建议 —— 它的精简逻辑（砍可推导内容、留陷阱与理由）正是你们要的 | §3.7 `[官方]` |
| 5 | **用 HTML 注释写"状态 + 日期"**：`<!-- 最后核实 2026-08-25 -->` 在注入前被剥离，**零 token 成本**，还能满足你们自己的头部状态规则 | §3.7 `[官方]` |

### 7.2 应该改（需要一点工作量）

| # | 动作 | 依据 |
|---|---|---|
| 6 | **按"能否从代码推导"重审 CLAUDE.md 和 architecture.md**，目标把 CLAUDE.md 压到 200 行以内。判据不是"重不重要"，是"Claude 自己读代码能不能得出" | §3.4 §6.1 `[官方+学术]` |
| 7 | **把可路径化的规则迁到 `.claude/rules/` + `paths:` frontmatter**（如"改 `src/ragent_backend/*_store.py` 时的约定"），真正实现条件加载 | §3.3 `[官方]` |
| 8 | **可逆性判据从 1 条扩到 3 条**：migration 脚本 / 已发出去的东西是否变了 / 撤销需要协调几方 | §6.2 `[社区]` |
| 9 | **三行决策记录加 `applies_to` glob 字段**，与 `.claude/rules/` 的 `paths:` 打通，让决策记录能被条件加载 | §4.3 `[社区]` |
| 10 | **第 13 条（更新触发点改到提交前）落成真 hook**，不要只写在 CLAUDE.md 里 —— 官方明确说 CLAUDE.md 不是强制层 | §2.1-⑤ `[官方]` |
| 11 | **三档口径各加一条机械判定条件**，否则三档也会腐烂 | §6.3 |

### 7.3 应该补（业界有、你们没有）

| # | 动作 | 依据 |
|---|---|---|
| 12 | **架构 fitness function**（Python 用 `ArchUnitPython` 或 `import-linter`），把 `architecture.md` 里的分层/依赖断言钉死在 CI 里，遗留违规先 freeze | §5.3 `[社区/官方]` |
| 13 | **变异测试**替代"它在旧实现下会失败吗"这句口述规则（`mutmut` / `cosmic-ray`）。同时防住 agent 写过度 mock 的假测试 | §6.5 `[社区/学术]` |
| 14 | **Danger JS 式 PR 规则**："改了 `src/ragent_backend/` 但没改 CLAUDE.md/AGENTS.md 就警告"。这是"提交前更新"最轻量的落地方式 | §5.1 `[官方]` |
| 15 | **性能数据改为基准脚本产出**，不再手写进 `architecture.md` | §5.3 |
| 16 | **单独建一份真正的 Definition of Done 清单**（三条即可），与"交付三句话"区分开 | §2.1-③ `[官方]` |
| 17 | **设计文档补 Non-Goals（事前）**，与"什么没做（事后）"配对使用 | §6.4 `[社区]` |
| 18 | **确认 auto memory 是否开启**（`autoMemoryEnabled`）。它的 200 行硬限制 + 超限报错是官方自带的防腐烂机制，比人工规则硬 | §3.7 `[官方]` |

### 7.4 明确保留（不要动）

| 做法 | 理由 |
|---|---|
| **「为什么」写进 commit message，写完冻结，零维护** | 这是你们最被低估的一条。有学术研究（arXiv:2603.15566）正在把 commit message 当作给 AI agent 的结构化知识协议。`[学术]` |
| **三行决策记录的轻量格式** | ADR 的存活率数据证明轻量是唯一可行解。"作废了什么"这一栏对应 `Status: Superseded`，是最常被丢掉也最重要的一栏。 |
| **「什么没做」作为强制交付项** | 业界只有事前的 Non-Goals，没有事后的强制版。结合 2026 年 agent 验证研究的趋势，这是正确且超前的。 |
| **三档汇报口径** | 无直接业界标准，但 DO-178C 的"通过测试 ≠ 已验证"和 2026 年的多层验证栈共识都支持这个方向。 |
| **设计文档必须有死期** | 直击 ADR/设计文档最主要的失败模式（停止更新后成为误导源）。 |
| **并发缺陷必须用并发方式验证 / 注释声称的不变量不算依据** | 标准工程常识，且被 MSR 2026 的 over-mocked tests 研究间接支持。 |

### 7.5 值得知道但**不建议现在采纳**的替代方案

**Spec-Driven Development (SDD)** `[社区/厂商]`：2026 年的主流范式，"spec 是产物，代码是构建输出"。工具：GitHub Spec Kit（MIT，CLI，30+ agent，四阶段工作流，最广泛采用）、AWS Kiro（spec-native IDE，2026-05-07 国际版 GA）、BMAD、GSD。宣称"非平凡任务上首次通过率提升 3–10 倍"（**厂商/社区宣称，未见独立验证，谨慎对待**）。

→ **判断**：你们的"需求分流 + 三行决策"是 SDD 的极简版，且更适合单人/小团队。SDD 明显更重（每个任务前要写 spec → plan → tasks）。**建议了解但先不迁移**；如果将来"改名词"类需求的失败率仍高，SDD 是下一站。

---

## 8. 来源清单

### 官方规范与文档

| 来源 | 内容 | 链接 |
|---|---|---|
| `[官方]` AGENTS.md | 格式定义、起源、AAIF 托管、60,000+ 仓库、嵌套规则 | https://agents.md |
| `[官方]` Anthropic Claude Code — Memory | 200 行目标、不读 AGENTS.md、`.claude/rules/`、`paths:`、`@` 导入不省 context、auto memory 限制、HTML 注释剥离、`/doctor` 精简、`claudeMdExcludes`、`InstructionsLoaded` hook、压缩后行为 | https://code.claude.com/docs/en/memory |
| `[官方]` Anthropic Support — Give Claude context | CLAUDE.md 基础指引、`/init` | https://support.claude.com/en/articles/14553240-give-claude-context-claude-md-and-better-prompts |
| `[官方]` Scrum Guide 2020 | Definition of Done 的正式定义与作用 | https://scrumguides.org/scrum-guide.html |
| `[官方]` Write the Docs — Docs as Code | Docs-as-Code 四要素定义 | https://www.writethedocs.org/guide/docs-as-code/ |
| `[官方]` mdBook — mdbook-specific features | ANCHOR / ANCHOR_END 锚点包含机制 | https://rust-lang.github.io/mdBook/format/mdbook.html |
| `[官方]` mdBook — test | `mdbook test` | https://rust-lang.github.io/mdBook/cli/test.html |
| `[官方]` Python doctest | 文档即测试 | https://docs.python.org/3/library/doctest.html |
| `[官方]` Sphinx doctest 扩展 | 文档代码块可测试 | https://www.sphinx-doc.org/en/master/usage/extensions/doctest.html |
| `[官方]` VitePress markdown | region 包含 | https://vitepress.dev/guide/markdown |
| `[官方]` Danger JS | PR 级规则引擎 | https://danger.systems/js/ |
| `[官方]` ADR GitHub 组织 | ADR 模板与工具汇总 | https://adr.github.io/ |
| `[官方]` IEEE 1012-2016 | 系统/软件/硬件 V&V 标准，完整性等级 | https://ieeexplore.ieee.org/document/8055462 |
| `[官方]` NASA SWE-118 | Software Test Report 要求 | https://swehb.nasa.gov/display/7150/SWE-118+-+Software+Test+Report |

### 学术研究

| 来源 | 内容 | 链接 |
|---|---|---|
| `[学术]` Gloaguen et al., ETH Zurich SRI Lab, 2026-02 | **本报告最关键证据**。人写 +4% / LLM 生成 -0.5~-3%；成本 +14~22% token；AGENTbench 641 词 / 9.7 节 | https://arxiv.org/abs/2602.11988 |
| `[学术]` On the Impact of AGENTS.md Files on the Efficiency of AI Coding Agents | 10 仓库 / 124 PR；运行时间 -28.6%、输出 token -16.6% | https://arxiv.org/abs/2601.20404 |
| `[学术]` Lore: Repurposing Git Commit Messages as a Structured Knowledge Protocol | commit message 作为 agent 知识协议 | https://arxiv.org/pdf/2603.15566 |
| `[学术]` Context Matters: Context Strategies for Automated ADR Generation Using LLMs | LLM 生成 ADR 的上下文策略 | https://arxiv.org/pdf/2604.03826 |
| `[学术]` The Verification Horizon: No Silver Bullet for Coding Agent Rewards | 验证已成为比生成更难的问题 | https://arxiv.org/pdf/2606.26300 |
| `[学术]` Are Coding Agents Generating Over-Mocked Tests? (MSR 2026) | 120 万 commit，48,563 条 agent commit；agent 倾向过度 mock | https://2026.msrconf.org/details/msr-2026-technical-papers/29/ |
| `[学术]` Who Verifies the Agents? (NeurIPS 2026 Workshop) | agent 验证专题 | https://verify-agents-workshop.github.io/ |
| `[学术]` VeriCodeGen (NeurIPS 2026 Workshop) | 可验证代码生成 | https://vericodegen.github.io/ |
| `[学术]` Agent Skills for LLMs: Architecture, Acquisition, Security | Agent Skills 三级渐进披露的形式化描述 | https://arxiv.org/html/2602.12430v3 |

### 厂商研究与博客

| 来源 | 内容 | 链接 |
|---|---|---|
| `[厂商]` JetBrains 开发者生态调研 2026（第 10 届，15,000+ 人，2026年5–7月） | 90% 每周使用 agent / 68% 每日；Claude Code 39%（美国 47%）、Copilot 21%（从 29% 下滑）、Cursor 12%。**该报告未涉及上下文文件配置实践** | https://blog.jetbrains.com/research/2026/08/ai-coding-agent-adoption-2026/ |
| `[厂商]` GitHub Blog — 2,500+ 仓库的 agents.md 分析 | 六大核心领域；具体 > 全面；**明确不给长度阈值**（注意：多个二手来源给它安了不存在的数字） | https://github.blog/ai-and-ml/github-copilot/how-to-write-a-great-agents-md-lessons-from-over-2500-repositories/ |
| `[厂商]` AAIF（Linux Foundation） — 五轮基准 | 12 行 AGENTS.md：模糊任务 -27% 耗时；**单次跑结论是反的，五次取中位才反转**；无 AGENTS.md 时 5 次里 2 次触发生产构建 | https://aaif.io/blog/measuring-agents-md-what-five-runs-show-that-one-doesn-t |
| `[厂商]` Chroma — Context Rot | 18 个前沿模型全部随输入变长而退化 | https://www.trychroma.com/research/context-rot |
| `[厂商]` Upsun Developer | 转述 ETH 研究；"从零开始、逐条累加、只写不可发现的信息" | https://developer.upsun.com/posts/ai/agents-md-less-is-more |

### 社区与个人观点（谨慎采信）

| 来源 | 内容 | 质量提示 |
|---|---|---|
| DAIR.AI Academy — Does AGENTS.md Actually Help? | ETH 论文的可靠转述 | `[社区]` 数字与论文一致 |
| aq.dev — Keep AGENTS.md and CLAUDE.md in Sync | 单一真身 + 软链/导入；团队场景加 pre-commit 检查 | `[个人]` 与官方建议一致 |
| ai-driven-dev/framework Issue #619 | 真实项目的双份副本漂移案例 | `[社区]` 有具体证据 |
| actual.ai — ADRs for Coding Agents | **ADR 是 system of record，agent 文件是 delivery format**；`applies_to` glob | `[个人]` 论点被多方独立复述，可信度较高 |
| rickpollick.com — The ADR Comeback | agentic 团队重新采纳 ADR | `[个人]` |
| hidekazu-konishi.com | "几乎没有团队两年后还在维护 ADR"；成败差别在运营不在模板 | `[个人]` 观察性，无量化 |
| tianpan.co (2026-02) | 40–80 行建议；"150–200 条指令预算" | `[个人]` **其引用的 GitHub 原文并无这些数字，存疑** |
| dev.to — 5 silent failure patterns | 60 行阈值；禁令须配替代；CLAUDE.md ~80% 遵循 vs hooks 100% | `[个人]` **数字无实证支撑，偏激进，仅作定性参考** |
| Towards Data Science — Governed Context | Claude Code 会话中的 context rot 治理 | `[社区]` |
| Curo / DevelopersVoice / Loiane Groner | fitness functions、ArchUnit、dependency-cruiser | `[个人/社区]` 技术描述准确 |
| fs.blog / 多篇 | Bezos 单向门/双向门；"30 天内能否撤销"的量化版 | `[社区]` 量化阈值是社区演绎，非 Bezos 原话 |
| industrialempathy.com — Design Docs at Google | Non-Goals 作为必备节 | `[个人]` 作者为 Google 工程师，业界公认参考 |
| Pragmatic Engineer — RFCs and Design Docs | 多公司 RFC 模板汇编 | `[社区]` |
| BrowserStack / softengbook.org | 变异测试定义与局限 | `[社区]` |
| 多篇（Augment Code / thebcms / MarkTechPost 等） | Spec Kit / Kiro / SDD 现状；"3–10 倍首次通过率" | `[厂商/社区]` **该倍数为宣称值，未见独立验证** |

---

## 附：本报告未能解答的问题

诚实标注，以下几点**未找到权威来源**：

1. **"83% 的团队很少/偶尔记录 ADR"** —— 多方转述，未定位到原始调研报告。方向性数字，勿引用为事实。
2. **"模型可靠遵循 150–200 条指令"** —— 仅见于个人博客，未找到一手实验来源。
3. **"CLAUDE.md 遵循率 80% / hooks 100%"** —— 个人博客数字，无方法学说明。定性方向（hooks 更硬）由官方文档支持，具体数字不可信。
4. **是否有人专门研究过"事后声明未覆盖范围"这一交付实践** —— 未找到。已有的都是事前的 Non-Goals。
5. **中文语境下 CLAUDE.md 的 token/行数最优值** —— 所有公开建议都基于英文文件，中文的 token 密度不同，行数建议可能需要下调。你们 305 行 / 4.5K token 的比值（约 15 token/行）确实低于英文典型值，说明单行较短，**实际"指令条数"可能没有 305 行看起来那么多**。这一点对你们有利，但不改变"应该精简"的结论。
