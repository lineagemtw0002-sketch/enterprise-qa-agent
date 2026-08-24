import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Select, Tag, Popconfirm, message, Empty } from 'antd'
import { Plus, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// meProfile 从 App.jsx 一路传下来（AdminPanel -> 这里），只用来读
// meProfile.organization.is_platform：平台管理员能看到/新建所有企业的用户
// （新建时可选所属企业），企业管理员的列表后端已经按 org 过滤过，新建的用户
// 也强制落在自己企业下。用户一旦创建，所属企业不能再改派（没有对应的操作
// 入口，也没有对应的后端端点）——见 app.py 里 admin_create_user 旁的注释。
//
// 2026-08-23 起"身份"和"角色"合并成一个概念：角色直接携带知识库权限（见
// role_store.py 顶部说明），这张表只有一列"角色"——一人一个角色（暂定，
// 后端 user_roles 表结构仍是多对多，只是当前业务规则限定成单选，见 app.py
// _validate_role_assignment 旁的说明）。角色列表（adminApi.listRoles）本身
// 已经按调用者身份返回了合适的范围：平台管理员看到全部全局角色；企业管理员
// 看到全局角色 + 自己企业建的角色（带各自的知识库关联，但那部分信息在「角色
// 管理」页面维护，这里只管"给谁分配哪个角色"）。
export default function UserRoleAssignment({ meProfile }) {
  const isPlatformAdmin = !!meProfile?.organization?.is_platform

  const [users, setUsers] = useState([])
  const [roles, setRoles] = useState([])
  const [organizations, setOrganizations] = useState([])
  const [loading, setLoading] = useState(false)
  const [savingUserId, setSavingUserId] = useState(null)

  const [createVisible, setCreateVisible] = useState(false)
  const [createForm, setCreateForm] = useState({ username: '', password: '', role_ids: [], org_id: null })
  const [createLoading, setCreateLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    try {
      const [userList, roleList, orgList] = await Promise.all([
        adminApi.listUsers(),
        adminApi.listRoles(),
        adminApi.listOrganizations(),
      ])
      setUsers(userList)
      setRoles(roleList)
      setOrganizations(orgList)
    } catch (error) {
      message.error('加载用户列表失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  function openCreate() {
    setCreateForm({ username: '', password: '', role_ids: [], org_id: null })
    setCreateVisible(true)
  }

  async function submitCreate() {
    if (!createForm.username.trim() || createForm.password.length < 6) {
      message.warning('用户名必填，密码至少 6 位')
      return
    }
    setCreateLoading(true)
    try {
      await adminApi.createUser({
        username: createForm.username.trim(),
        password: createForm.password,
        role_ids: createForm.role_ids,
        org_id: createForm.org_id || undefined,
      })
      message.success('用户创建成功')
      setCreateVisible(false)
      await loadAll()
    } catch (error) {
      message.error('创建失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setCreateLoading(false)
    }
  }

  async function handleRoleChange(user, roleId) {
    setSavingUserId(user.user_id)
    try {
      const updated = await adminApi.setUserRoles(user.user_id, roleId ? [roleId] : [])
      setUsers((prev) => prev.map((u) => (u.user_id === user.user_id ? updated : u)))
      message.success(`已更新 ${user.username} 的角色`)
    } catch (error) {
      message.error('更新失败: ' + (error.response?.data?.detail || error.message))
      await loadAll()
    } finally {
      setSavingUserId(null)
    }
  }

  async function handleDelete(user) {
    try {
      await adminApi.deleteUser(user.user_id)
      message.success('用户已删除')
      await loadAll()
    } catch (error) {
      message.error('删除失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  // 企业管理员永远不可能新授予这两个角色——后端 _validate_role_assignment
  // 里，super_admin 只有超级管理员能发，org_admin（任命企业管理员）只有
  // 平台层能发，企业管理员发了必然 403。既然选了也用不了，企业管理员视角下
  // 这两个"系统角色"就不展示，只留跨企业共用的部门角色、以及自己企业建的
  // 角色（带知识库权限）供选择。2026-08-24 起平台侧废弃 admin/user 两个
  // 系统角色，运营方只保留 super_admin/org_admin（见 role_store.py 顶部
  // 说明）。
  const PLATFORM_ONLY_ROLE_NAMES = new Set(['super_admin', 'org_admin'])
  const SYSTEM_ROLE_NAMES = new Set(['super_admin', 'org_admin'])

  // 平台管理员视角下分组展示（系统身份 vs 部门身份），方便在一长串角色里
  // 快速分清哪几个是权限档位；企业管理员视角下不分组，平铺一个列表——见
  // getRoleOptionsForUser 旁的说明。
  function groupRoleOptions(roleList) {
    const systemOptions = roleList
      .filter((r) => SYSTEM_ROLE_NAMES.has(r.name))
      .map((r) => ({ label: r.display_name, value: r.role_id }))
    const companyOptions = roleList
      .filter((r) => !SYSTEM_ROLE_NAMES.has(r.name))
      .map((r) => ({ label: r.display_name, value: r.role_id }))
    const groups = []
    if (systemOptions.length) groups.push({ label: '系统身份', options: systemOptions })
    if (companyOptions.length) groups.push({ label: '部门/企业角色', options: companyOptions })
    return groups
  }

  function flatRoleOptions(roleList) {
    return roleList.map((r) => ({ label: r.display_name, value: r.role_id }))
  }

  // 平台管理员（super_admin）给客户企业（非平台组织）的员工分配角色时，
  // 后端 _validate_role_assignment 只放行 org_admin 这一个系统身份——
  // 平台管理员不了解客户企业内部架构，不能替企业分配具体的部门/自建角色，
  // 那是该企业 org_admin 自己的事。选项里如果还列出这些，管理员选了会被
  // 后端 403 拒绝，白白让人以为能选，所以平台管理员视角下这里直接不展示；
  // 企业管理员反过来：能自由分配部门/自建角色，但 super_admin/org_admin
  // 这两个系统角色发了必然被后端拒绝，所以这几个不展示（需求 5）。
  function getRoleOptionsForUser(user) {
    const currentRoleIds = new Set(user.roles.map((r) => r.role_id))
    if (!isPlatformAdmin) {
      // 历史遗留：如果这个员工本来就持有一个平台层系统角色（改造前建的账号，
      // 或者由平台管理员任命的 org_admin），还是要让它在下拉框里显示得出
      // 名字，不能显示成空白——只是不让它出现在"可以新选"的选项里。
      const visibleRoles = roles.filter(
        (r) => !PLATFORM_ONLY_ROLE_NAMES.has(r.name) || currentRoleIds.has(r.role_id),
      )
      return flatRoleOptions(visibleRoles)
    }
    const visibleRoles = roles.filter((r) => SYSTEM_ROLE_NAMES.has(r.name) || currentRoleIds.has(r.role_id))
    const groups = groupRoleOptions(visibleRoles)

    // 平台管理员能看到全平台所有用户，但企业自己建的角色（org_id 非空）从
    // 不出现在 `roles` 里——GET /admin/roles 平台视角只查全局角色（见
    // app.py::admin_list_roles），压根不拉取任何企业角色。某个员工本来就
    // 持有一个企业自建角色时（比如该企业 org_admin 把他分到了自己建的部门
    // 角色），这个 Select 的 value 在 options 里找不到匹配项，antd 没有
    // label 可显示，会把 role_id 原样显示成一串 UUID——之前真的这样露出来
    // 过。平台管理员本来就不该管理这类角色（不了解企业内部架构，见上面的
    // 大段说明），但"看得到叫什么名字"这个最基本的展示需求不该因为管不了
    // 就一起丢掉：这里直接从 user.roles 自身取 display_name（每个用户的
    // 角色列表本来就带这个字段，不依赖平台视角那份只含全局角色的 `roles`
    // 数据源），补一个单独分组的只读选项，并标 disabled——防止被误当成
    // "可以重新指派给别人"的正常选项（提交这个值不会有任何效果，后端也
    // 只认新选中的角色，不会把这个只读展示项错当成变更）。
    const knownRoleIds = new Set(roles.map((r) => r.role_id))
    const readOnlyRoles = user.roles.filter((r) => !knownRoleIds.has(r.role_id))
    if (readOnlyRoles.length) {
      groups.push({
        label: '企业自建角色（只读，平台管理员不可分配/修改）',
        options: readOnlyRoles.map((r) => ({ label: r.display_name, value: r.role_id, disabled: true })),
      })
    }
    return groups
  }

  // 「新建用户」弹窗没有"历史遗留角色"这回事：平台管理员这里是系统身份单选；
  // 企业管理员这里过滤掉 super_admin/org_admin（需求 5），平铺展示。
  const createRoleOptions = isPlatformAdmin
    ? groupRoleOptions(roles.filter((r) => SYSTEM_ROLE_NAMES.has(r.name)))
    : flatRoleOptions(roles.filter((r) => !PLATFORM_ONLY_ROLE_NAMES.has(r.name)))
  const orgOptions = organizations.map((o) => ({ label: o.name, value: o.org_id }))

  // 这张表是管理员用来管"别人"的角色的，不包括自己——管理员改自己的角色
  // 容易造成自我锁死或者一堆自我保护的特判代码（改造前 super_admin 自我
  // 保护那段就是典型），干脆在列表层面就不展示自己这一行，想改自己的角色
  // 得找另一个更高权限的管理员操作，跟"不能删除自己"（app.py
  // admin_delete_user）是同一个思路，只是这里提前到前端列表就做掉，而不是
  // 等点了按钮才 400。
  const otherUsers = users.filter((u) => u.user_id !== meProfile?.user_id)

  const columns = [
    { title: '用户名', dataIndex: 'username', key: 'username' },
    {
      title: '所属企业',
      key: 'organization',
      width: 160,
      // 只读——员工归属在创建时定死，任何管理员（包括平台管理员）都不能事后
      // 改派，见组件顶部注释。
      render: (_, user) => <span>{user.organization?.name ?? '—'}</span>,
    },
    {
      title: '角色',
      key: 'roles',
      width: 320,
      // 一人一角色：Select 是单选（没有 mode="multiple"），value 只取第一个。
      // 历史遗留的多角色用户（合并改造前建的账号）在下面额外提示，不静默
      // 丢弃信息——保存前管理员能看到"这个人原来还有别的角色"。
      render: (_, user) => (
        <div>
          <Select
            allowClear
            style={{ width: '100%' }}
            placeholder="未分配角色"
            value={user.roles[0]?.role_id}
            options={getRoleOptionsForUser(user)}
            loading={savingUserId === user.user_id}
            onChange={(roleId) => handleRoleChange(user, roleId)}
          />
          {user.roles.length > 1 && (
            <div style={{ marginTop: 4, fontSize: 12, color: 'var(--text-tertiary, #999)' }}>
              历史遗留多角色：{user.roles.map((r) => r.display_name).join('、')}；保存后只保留上面选中的一个
            </div>
          )}
          {user.roles.some((r) => r.name === 'org_admin') && (
            <div style={{ marginTop: 4 }}>
              <Tag color="blue">企业管理员，知识库访问不受角色限制</Tag>
            </div>
          )}
        </div>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 100,
      render: (_, user) => (
        <Popconfirm
          title="删除用户"
          description="此操作不可逆，确定删除该用户吗？"
          okText="确认删除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => handleDelete(user)}
        >
          <Button size="small" danger icon={<Trash2 size={14} />}>删除</Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          给用户分配一个角色（一人一个，决定权限档位；企业角色还自带知识库权限），变更不需要重新登录，立即生效。
        </p>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>新建用户</Button>
      </div>

      <Table
        rowKey="user_id"
        columns={columns}
        dataSource={otherUsers}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无用户" /> }}
      />

      <Modal
        title="新建用户"
        open={createVisible}
        onCancel={() => setCreateVisible(false)}
        onOk={submitCreate}
        confirmLoading={createLoading}
        okText="创建"
        cancelText="取消"
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <div style={{ marginBottom: 4 }}>用户名</div>
            <Input
              value={createForm.username}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, username: e.target.value }))}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>密码（至少 6 位）</div>
            <Input.Password
              value={createForm.password}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, password: e.target.value }))}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>角色（可选，之后也能改；一人只能有一个角色）</div>
            <Select
              allowClear
              style={{ width: '100%' }}
              placeholder="不选则先不分配角色"
              options={createRoleOptions}
              value={createForm.role_ids[0]}
              onChange={(roleId) => setCreateForm((prev) => ({ ...prev, role_ids: roleId ? [roleId] : [] }))}
            />
          </div>
          {isPlatformAdmin && (
            <div>
              <div style={{ marginBottom: 4 }}>所属企业（不选则默认归到你自己的企业）</div>
              <Select
                allowClear
                style={{ width: '100%' }}
                placeholder="默认所属自己的企业"
                options={orgOptions}
                value={createForm.org_id}
                onChange={(orgId) => setCreateForm((prev) => ({ ...prev, org_id: orgId ?? null }))}
              />
            </div>
          )}
        </div>
      </Modal>
    </div>
  )
}
