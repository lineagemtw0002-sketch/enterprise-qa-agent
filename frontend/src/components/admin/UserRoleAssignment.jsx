import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Select, Tag, Space, Popconfirm, message, Empty } from 'antd'
import { Plus, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// meProfile 从 App.jsx 一路传下来（AdminPanel -> 这里），只用来读
// meProfile.organization.is_platform：平台管理员能看到/改派所有企业的用户，
// 普通企业管理员的列表后端已经按 org 过滤过，这里只是不给他们看到"改派企业"
// 这个跨企业操作的入口（真正的拦截在后端 require_platform_admin，这里不给
// 入口只是不让人以为点了有用）。
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

  async function handleRoleChange(user, roleIds) {
    setSavingUserId(user.user_id)
    try {
      const updated = await adminApi.setUserRoles(user.user_id, roleIds)
      setUsers((prev) => prev.map((u) => (u.user_id === user.user_id ? updated : u)))
      message.success(`已更新 ${user.username} 的角色`)
    } catch (error) {
      message.error('更新失败: ' + (error.response?.data?.detail || error.message))
      await loadAll()
    } finally {
      setSavingUserId(null)
    }
  }

  async function handleOrgChange(user, orgId) {
    setSavingUserId(user.user_id)
    try {
      const updated = await adminApi.setUserOrganization(user.user_id, orgId)
      setUsers((prev) => prev.map((u) => (u.user_id === user.user_id ? updated : u)))
      message.success(`已把 ${user.username} 改派到新企业`)
    } catch (error) {
      message.error('改派失败: ' + (error.response?.data?.detail || error.message))
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

  const roleOptions = roles.map((r) => ({ label: r.display_name, value: r.role_id }))
  const orgOptions = organizations.map((o) => ({ label: o.name, value: o.org_id }))

  const columns = [
    { title: '用户名', dataIndex: 'username', key: 'username' },
    {
      title: '所属企业',
      key: 'organization',
      width: 200,
      render: (_, user) =>
        isPlatformAdmin ? (
          <Select
            style={{ width: '100%' }}
            placeholder="未分配企业"
            value={user.organization?.org_id ?? undefined}
            options={orgOptions}
            loading={savingUserId === user.user_id}
            onChange={(orgId) => handleOrgChange(user, orgId)}
          />
        ) : (
          <span>{user.organization?.name ?? '—'}</span>
        ),
    },
    {
      title: '角色',
      key: 'roles',
      width: 360,
      render: (_, user) => (
        <Select
          mode="multiple"
          style={{ width: '100%' }}
          placeholder="未分配角色"
          value={user.roles.map((r) => r.role_id)}
          options={roleOptions}
          loading={savingUserId === user.user_id}
          onChange={(roleIds) => handleRoleChange(user, roleIds)}
        />
      ),
    },
    {
      title: '可访问知识库',
      dataIndex: 'allowed_collections',
      key: 'allowed_collections',
      render: (names) =>
        names && names.length ? (
          names.includes('*') ? (
            <Tag color="purple">不限</Tag>
          ) : (
            <Space size={[0, 4]} wrap>
              {names.map((n) => <Tag key={n}>{n}</Tag>)}
            </Space>
          )
        ) : (
          <span style={{ color: 'var(--text-tertiary, #999)' }}>仅自己的对话</span>
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
          给用户分配角色，用户能访问的知识库随之自动生效，不需要重新登录。
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
            <div style={{ marginBottom: 4 }}>角色（可选，之后也能改）</div>
            <Select
              mode="multiple"
              style={{ width: '100%' }}
              placeholder="不选则先不分配角色"
              options={roleOptions}
              value={createForm.role_ids}
              onChange={(roleIds) => setCreateForm((prev) => ({ ...prev, role_ids: roleIds }))}
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
