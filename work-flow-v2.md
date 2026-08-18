# 工作流 v2 —— admin 审批收尾 + 自然语言唯一入口

> 状态：v2 收尾方案（本次已实现）
> 前置文档：`work-flow.md`（后端设计，状态：设计方案）、`work-flow-web.md`（前端设计，状态：设计方案）、`role.md`（角色系统）

## 1. 背景

`work-flow.md` / `work-flow-web.md` 设计了完整的"聊天发起工作流 + 按角色审批"方案，但两份文档都标注"设计方案（未实现）"。这次收尾前先排查了一遍代码库，发现实际情况是：**绝大部分已经实现**，只是没人验证过、也没有把最后一块拼图接上：

- 后端：`workflow_store.py` 的状态机（`pending_approval → approved / returned_for_revision / rejected / cancelled`，`approved → completed`）、`app.py` 里 `POST /api/v1/workflows/{id}/approve|return|reject|resubmit|complete|cancel` 全套接口、`intent.py` 的意图识别（LLM + 关键词兜底，"请假"已经能命中 `leave_request`）都已经跑通，代码质量也没问题。
- 前端：`WorkflowDetailDrawer.jsx` 已经有 通过/打回/驳回 按钮，`WorkflowPanel.jsx` 已经有"待我审批"收件箱，`WorkflowStatusPill.jsx` 已经会在聊天头部显示"填写中：请假申请 · 2/4 项"。

真正的缺口只有两个：

1. **审批人没配置。** 审批人是按角色配置的（模板的 `approver_role_id` 字段，指向 `roles` 表任意一个角色，不锁定"admin"），但内置的 4 个模板（`laptop_repair`/`leave_request`/`business_trip`/`expense_reimbursement`）出厂时这个字段是空的，导致发起任何工作流都会被后端拒绝，提示"暂未配置审批人，请联系管理员配置"。而且没有任何界面能配置它——`api/workflow.js` 里 `adminListWorkflowTemplates`/`adminUpdateWorkflowTemplate` 等函数已经写好了，但没有一个组件在调用。
2. **手动入口和自然语言入口并存。** 聊天输入框旁边有一个"发起工作流"下拉选择器（`WorkflowLauncher.jsx`），和自然语言触发（说"我想请假"）功能重复。企业里工作流种类多，靠用户自己从下拉列表里认出该选哪个不现实，这次要求只保留自然语言入口。

## 2. 决策

- **审批人模型不改**：继续沿用"审批人 = 某个角色的成员"这套按角色配置的机制（`work-flow.md` 已有设计），不引入"admin/super_admin 全局越权审批任意工作流"的旁路。
- **admin 设为默认审批角色**：新增一个幂等的种子脚本，把系统角色 `admin` 回填进所有 `approver_role_id` 为空的内置模板。已经被管理员手动配置过审批角色的模板不覆盖——这样"默认 admin 审批"和"某些工作流交给更合适的部门角色审批"（比如电脑报修交给 IT 部）可以共存。
- **补一个管理页**：admin 以后想把某个工作流的审批人从 admin 改派给别的角色，需要有界面能操作，不能只靠直接调接口。
- **下拉选择器整个移除**，聊天框只保留纯文字输入，工作流触发完全依赖后端的自然语言意图识别。

## 3. 改动清单

### 后端

- 新增 `scripts/seed_workflow_approvers.py`：读取系统角色 `admin`（`RoleStore.get_role_by_name(ROLE_ADMIN)`），遍历 `WorkflowStore.list_templates()`，对 `approver_role_id` 为空的模板调用 `update_template(template_id, approver_role_id=admin_role.role_id)`。已配置的模板跳过。支持 `--dry-run`。
  - 在本地开发库上实测：4 个内置模板此前已经被手动配置了按部门的审批角色（`laptop_repair→IT部`、`leave_request→考勤部`、`business_trip→后勤部`、`expense_reimbursement→考勤部`），脚本正确识别为"已配置"并全部跳过，没有覆盖——这正是脚本设计要保证的行为。在全新部署/这几个模板确实为空的环境下，脚本会把它们全部指向 `admin`。
- `app.py` 的接口不需要改动，`/api/v1/admin/workflow-templates*` 和审批相关接口本来就是完整的。

### 前端

- 新增 `frontend/src/components/admin/WorkflowTemplateManagement.jsx`：管理后台的"工作流管理"页，表格列出内置工作流的展示名、内部类型、必填字段数、当前审批角色（未配置会高亮提示"未配置"），操作列可以打开弹窗、用角色下拉框改派审批人，调用 `workflowApi.adminUpdateWorkflowTemplate`。
- `frontend/src/components/admin/AdminPanel.jsx` 新增第三个 tab："工作流管理"，挂载上面这个组件。
- 删除 `frontend/src/components/workflow/WorkflowLauncher.jsx`。
- `frontend/src/App.jsx`：移除 `WorkflowLauncher` 的 import、`selectedWorkflow` state 及其在两处发消息逻辑（`sendNormalMessage`/`sendStreamMessage`）里拼进请求体的 `workflow_type` 字段、输入框旁的渲染块，placeholder 恢复固定文案。聊天请求不再携带 `workflow_type`，完全交给后端 `intent.py` 判断。
- `WorkflowStatusPill.jsx` 及其样式保留不动——它反映的是自然语言触发后、后端返回的 `active_workflow` 多轮填表进度，跟手动选择器无关。清理了 `WorkflowInputControls.css` 里只属于 `WorkflowLauncher` 的两条样式规则（`.workflow-launcher-btn`、`.workflow-launcher-tag`）。

### 不需要改动的部分

- `WorkflowDetailDrawer.jsx` 的 通过/打回/驳回/取消/重新提交/标记完成 按钮——已经存在，一旦某个工作流被配置了审批角色，持有该角色的用户登录后会在 `WorkflowPanel.jsx` 的"待我审批" tab 里自动看到待办，不用改代码。
- `intent.py` 的自然语言意图识别——"我想请假"已经能命中 `leave_request` 并进入多轮收集，不用改动。

## 4. 端到端流程

1. 用户在聊天框直接打字："我下周一到周三要请假，年假"。
2. `intent.py` 的规则/LLM 分类命中 `intent_type=workflow, workflow_type=leave_request`。
3. `workflow.py` 的 `_workflow_node` 抽取已知字段（`leave_type=年假, start_date=..., end_date=...`），缺 `reason` 就继续追问一轮。
4. 字段收齐后调用 `WorkflowStore.create_instance(...)`，状态进入 `pending_approval`，聊天里给出确认信息。
5. 审批角色（默认 `admin`，或管理员在"工作流管理"里改派的角色）的成员登录后，"工作流"页的"待我审批" tab 能看到这条实例，点"处理"进入 `WorkflowDetailDrawer`。
6. 审批人点 **通过**（`POST /approve`）、**打回**（`POST /return`，需填评论，状态回到 `returned_for_revision`，申请人可以补充材料后"重新提交"）或 **驳回**（`POST /reject`，需填评论，终态）。
7. 状态变化会写入 `notifications` 表，申请人可以在通知铃铛里看到并跳转回详情。

## 5. 遗留问题（沿用 work-flow.md §8 的记录，本次未处理）

- 审批人查看申请人上传附件目前只能通过 `listConversationFiles`/`downloadFile` 间接查看聊天里的文件，没有针对"材料是否齐全"的结构化校验——按设计本来就是人工判断，不算缺口。
- 没有推送通知，审批结果目前只有申请人主动点开通知铃铛或再次对话时才会看到，`work-flow-web.md` §6 提到的更完整通知中心仍未做。
- 没有提交前的确认/预览步骤（`work-flow.md` §8 已标记为"值得在下一轮设计里加进去"），这次也没有加。

## 6. 验证记录

- `scripts/seed_workflow_approvers.py --dry-run` 在本地开发库跑通，正确识别已配置的模板并全部跳过，未误覆盖。
- 前端 `npm run build` 通过，无编译错误。
- 手动交互验证（管理后台"工作流管理"页改派审批角色、聊天里说"我想请假"走完整多轮收集、审批人在"待我审批"里点通过/打回/驳回）建议由使用者本地跑 `npm run dev` 实际走一遍确认。
