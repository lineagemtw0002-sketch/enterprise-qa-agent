import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Select, Tag, Space, Popconfirm, message, Empty } from 'antd'
import { Plus, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// meProfile 从 App.jsx 一路传下来（AdminPanel -> 这里），只用来读
// meProfile.organization.is_platform：平台管理员能看到/新建所有企业的用户
// （新建时可选所属企业），企业管理员的列表后端已经按 org 过滤过，新建的用户
// 也强制落在自己企业下。用户一旦创建，所属企业不能再改派（没有对应的操作
// 入口，也没有对应的后端端点）——见 app.py 里 admin_create_user 旁的注释。
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

  // "全部知识库"（内部标识 all_kb）不是一个真正的角色——它是老数据模型迁移
  // 时为了把 allowed_collections=["*"] 的用户接到新角色表上，临时造的一个
  // 系统角色壳子（见 scripts/migrate_to_roles.py），本质是"知识库通配符权限"
  // 而不是"这个人是什么身份"。留着它出现在角色选择框里，管理员会误以为可以
  // 把"全部知识库"当角色发给人，所以这里过滤掉，不让它进新的分配操作。
  //
  // hr_admin_kb/finance_kb 等 6 个角色 2026-08-22 起不再过滤——它们原来是
  // 平台自己固定部门知识库的专属角色壳子，跟 all_kb 是同一类问题；现在平台
  // 那 6 个本地部门库已经下线，这几个角色改成委托模式企业（Acme/Globex）的
  // 类目过滤角色（见 query_knowledge_hub.py DEPARTMENT_ROLE_TO_REMOTE_CATEGORIES），
  // 是委托企业员工需要被正常分配的真实角色，不能再挡在分配入口外面——之前
  // 这条过滤规则没跟着一起改，会导致管理员在 UI 上完全没法把这些角色分给
  // 委托企业员工，只能绕过前端直接改数据库。
  const NON_ASSIGNABLE_ROLE_NAMES = new Set(['all_kb'])
  // 系统角色（决定能不能进后台、是平台管理员还是企业管理员）跟"客户公司
  // 内部角色"（IT部/考勤部/后勤部/法务部这类部门角色，决定能看哪些知识库）
  // 是两类不同性质的东西，分组展示，不要混在一个平铺列表里让管理员分不清
  // 该选哪个。
  const SYSTEM_ROLE_NAMES = new Set(['super_admin', 'admin', 'org_admin', 'user'])

  // 按"系统角色 / 客户公司内部角色"分组——调用方负责先把不该出现的角色
  // （比如 all_kb）过滤掉，这个函数本身不做过滤，只负责分组。
  function groupRoleOptions(roleList) {
    const systemOptions = roleList
      .filter((r) => SYSTEM_ROLE_NAMES.has(r.name))
      .map((r) => ({ label: r.display_name, value: r.role_id }))
    const companyOptions = roleList
      .filter((r) => !SYSTEM_ROLE_NAMES.has(r.name))
      .map((r) => ({ label: r.display_name, value: r.role_id }))
    const groups = []
    if (systemOptions.length) groups.push({ label: '系统角色', options: systemOptions })
    if (companyOptions.length) groups.push({ label: '客户公司内部角色', options: companyOptions })
    return groups
  }

  const roleOptions = groupRoleOptions(roles.filter((r) => !NON_ASSIGNABLE_ROLE_NAMES.has(r.name)))

  function getAssignableRoleOptions(user) {
    const currentRoleIds = new Set(user.roles.map((r) => r.role_id))
    // all_kb 本来不进选项，但如果这个用户历史遗留就持有它（老迁移数据），
    // 还是要让它出现在选项里，否则 Select 显示不出这个历史值的标签。
    const visibleRoles = roles.filter(
      (r) => !NON_ASSIGNABLE_ROLE_NAMES.has(r.name) || currentRoleIds.has(r.role_id),
    )
    return groupRoleOptions(visibleRoles)
  }

  const orgOptions = organizations.map((o) => ({ label: o.name, value: o.org_id }))

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
      width: 360,
      // 一人一角色：Select 是单选（没有 mode="multiple"），value 只取第一个
      // 角色。历史遗留的多角色用户（角色系统改造前建的账号）在下面额外提示，
      // 不静默丢弃信息——保存前管理员能看到"这个人原来还有别的角色"。
      render: (_, user) => (
        <div>
          <Select
            allowClear
            style={{ width: '100%' }}
            placeholder="未分配角色"
            value={user.roles[0]?.role_id}
            options={getAssignableRoleOptions(user)}
            loading={savingUserId === user.user_id}
            onChange={(roleId) => handleRoleChange(user, roleId)}
          />
          {user.roles.length > 1 && (
            <div style={{ marginTop: 4, fontSize: 12, color: 'var(--text-tertiary, #999)' }}>
              历史遗留多角色：{user.roles.map((r) => r.display_name).join('、')}；保存后只保留上面选中的一个
            </div>
          )}
        </div>
      ),
    },
    // 平台管理员不管理任何企业的知识库内容（跟「角色管理」页面去掉知识库
    // 配置是同一个结论），这一列只在企业管理员自己的视角下出现——企业管理员
    // 在这张表看到的本来就只有自己企业的员工，一一列出可访问知识库有意义；
    // 平台管理员这里看到的是跨企业的用户列表，压根不该出现知识库信息。
    ...(!isPlatformAdmin ? [{
      title: '可访问知识库',
      dataIndex: 'allowed_collections',
      key: 'allowed_collections',
      render: (names, user) => {
        const roleNames = user.roles.map((r) => r.name)
        if (roleNames.includes('org_admin')) {
          return <Tag color="blue">企业内全部知识库</Tag>
        }
        return names && names.length ? (
          names.includes('*') ? (
            <Tag color="purple">不限</Tag>
          ) : (
            <Space size={[0, 4]} wrap>
              {names.map((n) => <Tag key={n}>{n}</Tag>)}
            </Space>
          )
        ) : (
          <span style={{ color: 'var(--text-tertiary, #999)' }}>仅自己的对话</span>
        )
      },
    }] : []),
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
          给用户分配一个角色（一人一角色），用户能访问的知识库随之自动生效，不需要重新登录。
        </p>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>新建用户</Button>
      </div>

      <Table
        rowKey="user_id"
        columns={columns}
        dataSource={users}
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
              options={roleOptions}
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
