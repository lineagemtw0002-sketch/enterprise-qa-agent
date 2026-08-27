# 知识库权限设计文档

> **状态：时点快照（2026-08-23）。不描述当前状态。** 标注于 2026-08-26。
>
> 🔴 **权限模型的唯一正本是 `CLAUDE.md` §3，冲突时以 §3 为准。**
> 本文头部原先写的是「截至 2026-08-23 的**当前实现**」—— 那是一个**自称当前状态**
> 的标记，而权限模型此后仍在演进。按 `CLAUDE.md` §7.4：
> 「报告类保留但**标死日期**，不假装描述当前状态」「标着『已实现』的废弃架构文档，
> 比没有文档更危险」，故降级为时点快照。
>
> **已知冲突**：`docs/review_2026-08-24/review_process_retro.md` §603 记录过
> 本文与 `role.md`（现已归档至 `docs/archive/role.md`）描述的模型互相矛盾，
> 且当时两份都在仓库里、没有任何交叉标注。这条交叉标注就是补那个缺口的。
>
> **本文仍然有价值的部分**：记录了 2026-08-23 当时「权限是怎么设计、怎么拦截的」
> 的完整推理，不涉及安全测试发现的问题（另见
> `docs/security_prompt_injection_test_report.md`）。

---

## 一、核心设计思路

一句话概括：**"角色"直接携带知识库权限，一个用户一个角色，权限判定 = 这个用户持有哪个角色 → 这个角色关联了哪些 collection → 再跟"这个 collection 归不归这家企业"做一次收窄。**

不存在独立于角色的"知识库分组"实体——这是 2026-08-23 当天的架构调整：更早版本里"身份角色"（super_admin/org_admin/部门角色）和"知识库分组"（kb_group）是两张分开的表、两层配置，用户反馈"身份和角色其实是一回事"之后合并成了现在这套单一实体模型。

判定粒度是 **collection**（不做文档级 ACL）——跟检索/摄取的物理隔离粒度一致。

---

## 二、数据模型

```
organizations                 企业主表；is_platform 区分"平台自己" vs "客户企业"

roles                         角色表（身份 + 知识库权限合一）
├─ org_id (nullable)          NULL = 全局角色，固定只有 super_admin/org_admin 两个内置
│                              系统角色（2026-08-24 起平台侧不再支持新建全局角色，见
│                              role_store.py 顶部说明）；非空 = 某家企业自己建的角色，
│                              只有该企业的 org_admin 能管
├─ is_system                  内置两个身份角色的标记：super_admin/org_admin，不允许
│                              配置知识库、不允许删除，可以改展示名
└─ 两条独立的唯一索引：
   roles_name_global_uniq     org_id IS NULL 范围内 name 唯一
   roles_org_name_uniq        (org_id, name) 组合唯一（不同企业可以起同名角色）

role_collections               角色 <-> collection 的多对多关联
├─ role_id, org_id, collection_name
└─ 按 (role_id, org_id) 隔离——同一个全局角色（比如"IT部"）在不同企业名下可以
   关联完全不同的 collection，互不影响

user_roles                     用户 <-> 角色，业务规则限定"一人最多一个角色"
                                （表结构支持多对多，但 API 写入时只接受 0~1 个 role_id）

org_collections                 collection 的"归属登记表"——记录一个 collection 名字
├─ collection_name (PK)         归哪家企业、展示名叫什么。物理创建是摄入文档时自然
├─ org_id                       发生的（Chroma 的 get_or_create 语义），这张表只管
└─ display_name                 "这个名字存在、归谁"，用来做管理页的过滤和归属校验

tenant_connectors               企业的"知识库能力"由谁承接
├─ org_id, capability='knowledge_base'
└─ connector_type: internal_chroma（本地存储，平台自己管） /
                    http_api（委托模式，查询转发到企业自己的微服务，历史遗留架构，
                    Acme/Globex 已于 2026-08-23 全部转为本地存储，目前无生产企业
                    使用委托模式，但代码路径还在）
```

---

## 三、权限判定链路（谁能查到什么）

### 3.1 一个用户"能查哪些知识库" —— `role_store.get_allowed_collections_for_user()`

```
1. 查这个用户持有的角色（0或1个）
2. 如果角色名是 org_admin           → 直接返回 ["*"]（通配符）
3. 如果没有角色 / 用户没有 org       → 返回 []（什么都查不到）
4. 否则：SELECT collection_name FROM role_collections
         WHERE org_id = 用户所属企业 AND role_id = 用户的角色id
         → 返回具体的 collection 名字列表
```

**运营商角色（super_admin/admin）为什么天生没有知识库权限**：这两个角色从来不会出现在任何 `role_collections` 记录里（管理页压根没给它们配置知识库的入口），走到上面第4步自然查出空列表——不是靠一条 if 特判挡住的，是"没有配置入口"这个事实本身决定的。

**org_admin 的通配符不等于"看所有企业"**：`["*"]` 只是"这个人在自己企业范围内不受具体角色关联限制"，真正决定"自己企业范围"是下面 3.2 的另一道收窄。

### 3.2 通配符要收窄到"这家企业自己名下的库" —— `query_knowledge_hub.py::_org_owned_collections()`

```
org 是平台自己（org_platform）        → 返回 []（平台不代表任何具体企业，
                                         自己没有本地业务知识库）
org 是某个本地检索企业                → 返回 org_collections 表里登记的、
                                         这家企业自己创建的 collection 全集
没有 user_id / 查不到 org             → 返回 []
```

检索时的候选集 = `_org_owned_collections(org)` ∩ `用户的 allowed_collections`（org_admin 的通配符在这一步等价于"该企业全部"）。这一步保证了即使某个角色的 `role_collections` 里意外配错、混进了别的企业创建的 collection 名字，最终候选集依然会被"这家企业自己名下"这道边界收紧回去——两层判断都要过，不是靠单一一处代码兜底。

### 3.3 极简 ACL 判断函数 —— `acl.py::is_collection_allowed()`

```python
def is_collection_allowed(collection, allowed_collections):
    if collection.startswith("conv_"):      # 每个对话私有 collection，天然只有
        return True                          # 使用者自己知道 ID，不受 ACL 约束
    return "*" in allowed_collections or collection in allowed_collections
```

纯函数、不做 I/O，方便单元测试；"allowed_collections 从哪查出来"完全交给调用方。

---

## 四、各类操作的具体拦截点

### 4.1 知识库管理（新建/列表/删除/查看数据）—— `admin_list_collections` / `admin_create_collection` / `admin_delete_collection` / `admin_list_collection_chunks`

- 依赖 `_require_org_admin`（`require_role(ROLE_ORG_ADMIN)`）——必须持有企业管理员角色
- 再依赖 `_require_local_retrieval_org(current_user)`：
  - 查调用者所属企业 → 查这家企业 `knowledge_base` 能力的连接器类型
  - 如果是 `http_api`（委托模式）→ 直接 400 拒绝，"该企业的知识库检索已委托给企业自己的系统管理"（目前生产环境所有企业都已是本地模式，这条分支暂时打不到，但代码保留）
- 所有操作都以 `org.org_id` 为过滤条件查/写 `org_collections` 表，不会读到/删到别的企业登记的 collection
- 新建时额外拦截一批**保留名**：`hr_admin_kb` 等 6 个历史部门名、`default`、`conv_*` 前缀、`tenant_*_kb` 形状——防止企业自建库和平台/委托模式的命名约定混淆

### 4.2 角色的知识库配置 —— `admin_set_role_collections`（`PUT /admin/roles/{role_id}/collections`）

- 依赖 `_require_org_admin`
- 角色本身是内置系统角色（`is_system`）→ 拒绝配置（这四个角色天生不该有知识库权限，是业务规则，见 3.1）
- 角色 `org_id` 非空且不等于调用者所属企业 → 拒绝（"只能给本企业的角色配置知识库"）
- 全局角色（`org_id` 为空，比如部门角色模板）允许配置，但写入时会带上调用者的 `org_id`，只影响"调用方企业名下持有这个角色的人"，不会波及其他企业同名角色

### 4.3 角色的新建/改名/删除 —— `_authorize_role_mutation`

```
角色.org_id 为空（全局角色）  → 必须是 super_admin
角色.org_id 非空（企业角色）  → 调用者所属企业必须等于角色.org_id，且必须持有 org_admin
```

### 4.4 用户-角色分配的边界 —— `_validate_role_assignment`

给用户分配角色时逐个校验每个 `role_id`（不是走 `list_roles()` 全表比对，因为企业自建角色不在那张全局列表里），拦住的越权路径包括：
- 把系统角色（super_admin 等）分给普通企业用户
- 跨企业分配别的企业建的角色（`role.org_id != target_user.org_id`）

### 4.5 检索时的三层收紧 —— `query_knowledge_hub.py::execute()`

```
1. 查 allowed_collections（3.1）
2. 没指定具体 collection（绝大多数场景，LLM 从不主动填这个参数）：
   候选集 = _org_owned_collections(org) ∩ allowed_collections（org_admin 通配符时取全部）
   候选集为空 → 直接返回"未检索到相关内容"，不发起任何向量检索
3. 显式指定了 collection（老的单库路径）：
   a. acl.is_collection_allowed() 校验一次
   b. 硬编码拦截 tenant_*_kb 形状的名字——委托模式企业专属命名，本地库
      物理上根本没有这些数据，防止未来存储布局变了之后这条防线悄悄失效
   c. 再拿 _org_owned_collections() 校验一次"这个 collection 是不是我自己
      企业名下的"——即使 ACL 判断给了通配符 "*"，也不能凭空点名查到别的
      企业的 collection
```

委托模式（`http_api`，历史遗留架构）走完全独立的 `_execute_remote` 路径：查询转发给企业自己的微服务，本地这套 ACL 不适用；这一分支下改用"知识库分组名 → 请求类目白名单"的方式做二次过滤（`DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES`），但目前没有生产企业在用这个模式。

### 4.6 员工上传文档 —— `upload_collection_document`（`POST /collections/{collection_name}/documents`）

```
1. _require_local_retrieval_org（同 4.1，委托模式企业整体拒绝）
2. collection_name 必须在 _local_org_owned_collections(org) 里
   （不属于自己企业 → 404，不暴露"这个名字存不存在"）
3. allowed_collections 里没有通配符、且 collection_name 不在其中 → 403
```

代码注释里特别强调了一点：前端会把没权限的知识库选项置灰，但那只是 UX 提示，后端必须重新校验一遍——不能假设"前端不给选=安全"，防止直接拼请求改 `collection_name` 绕过置灰。

### 4.7 工作流审批角色 —— 同一套"企业内部管理"模式（附带说明，非知识库本身）

2026-08-23 起工作流审批人也从"角色/知识库"这套企业自治模型里复用同一个边界逻辑：`admin_set_workflow_approver` 校验 `approver_role.org_id == 调用者的 org.org_id`，只能选本企业自己建的角色当审批人，全局角色不再作为审批人来源。

---

## 五、几条容易忽略但很关键的隔离规则

1. **平台运营方自己没有任何本地业务知识库**——`_org_owned_collections` 对 `is_platform` 的组织直接返回空列表，super_admin/admin 即便拿到通配符也查不到任何东西（他们也从来拿不到通配符，见 3.1）。
2. **企业管理员的"通配符"永远收窄在自己企业范围内**，不存在"企业管理员能看别的企业知识库"的路径——3.1 的通配符和 3.2 的 `_org_owned_collections` 是两道独立的门，缺一不可。
3. **collection 名字全平台唯一**（`org_collections.collection_name` 是主键），新建时如果名字已被别的企业占用，报错信息不会告诉你是"被自己占用"还是"被别人占用"，防止探测出别的企业注册过哪些名字。
4. **系统内置的四个角色永远没有知识库配置入口**，不是运行时判断出来的"没权限"，而是管理页/API 层面压根不提供配置能力——这是"运营商角色无知识库权限"这条业务规则最上游的落地方式。
5. **对话私有 collection（`conv_*`）不走这整套 ACL**——天然只有对话的使用者自己知道这个 ID，设计上就不需要额外的权限表。

---

## 六、和安全测试报告的关系

`docs/security_prompt_injection_test_report.md` 里验证过的几条边界（跨企业检索、跨企业上传、跨企业删除/查看chunk、平台管理员通配符收窄），对应的正是上面 4.5 / 4.6 / 4.1 里描述的这几层拦截——测试结果显示这几层在"直接调用 API/正常提问"场景下都生效；测试里唯一绕过的两个点（英文越狱泄露 Prompt 模板、知识库文档投毒导致跨话题话术注入）**不发生在这套 ACL 判断本身**，而是发生在"检索到的内容/上下文被喂给 LLM 之后，LLM 自己怎么处理这些内容"这一层——现有的 ACL 设计管的是"能检索到哪些 collection"，管不到"检索到的内容里如果藏着指令，模型会不会照做"。
