import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Tag, Space, Popconfirm, message, Empty } from 'antd'
import { Plus, Pencil, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 平台运营方不管理任何企业的知识库内容（跟 UserRoleAssignment.jsx"可访问
// 知识库"列同一个结论），所以这个页面只管角色目录本身（建/改名/删），不出现
// 知识库的选择和展示——"给角色配知识库"是企业管理员在自己的「知识库权限」
// 页面（CompanyKbPermissions.jsx）做的事，两边职责不重叠。
export default function RoleManagement() {
  const [roles, setRoles] = useState([])
  const [loading, setLoading] = useState(false)

  const [createVisible, setCreateVisible] = useState(false)
  const [createForm, setCreateForm] = useState({ name: '', display_name: '' })
  const [createLoading, setCreateLoading] = useState(false)

  const [renameVisible, setRenameVisible] = useState(false)
  const [renameTarget, setRenameTarget] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renameLoading, setRenameLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    try {
      const roleList = await adminApi.listRoles()
      // "全部知识库"（内部标识 all_kb）不是一个真正的角色——它是老数据模型
      // 迁移时为了把 allowed_collections=["*"] 的用户接到新角色表上，临时
      // 造的一个系统角色壳子（见 scripts/migrate_to_roles.py），本质是
      // "知识库通配符权限"，不是一个有身份含义的角色，不该跟 IT部/法务部
      // 这类真正的角色混在一张表里管理。
      setRoles(roleList.filter((r) => r.name !== 'all_kb'))
    } catch (error) {
      message.error('加载角色列表失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  function openCreate() {
    setCreateForm({ name: '', display_name: '' })
    setCreateVisible(true)
  }

  async function submitCreate() {
    if (!createForm.name.trim() || !createForm.display_name.trim()) {
      message.warning('请填写完整')
      return
    }
    setCreateLoading(true)
    try {
      await adminApi.createRole({ name: createForm.name.trim(), display_name: createForm.display_name.trim() })
      message.success('角色创建成功')
      setCreateVisible(false)
      await loadAll()
    } catch (error) {
      message.error('创建失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setCreateLoading(false)
    }
  }

  function openRename(role) {
    setRenameTarget(role)
    setRenameValue(role.display_name)
    setRenameVisible(true)
  }

  async function submitRename() {
    if (!renameValue.trim()) {
      message.warning('展示名不能为空')
      return
    }
    setRenameLoading(true)
    try {
      await adminApi.updateRole(renameTarget.role_id, renameValue.trim())
      message.success('已更新')
      setRenameVisible(false)
      await loadAll()
    } catch (error) {
      message.error('更新失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setRenameLoading(false)
    }
  }

  async function handleDelete(role) {
    try {
      await adminApi.deleteRole(role.role_id)
      message.success('角色已删除')
      await loadAll()
    } catch (error) {
      message.error('删除失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  const columns = [
    {
      title: '角色',
      dataIndex: 'display_name',
      key: 'display_name',
      render: (text, role) => (
        <Space>
          <span style={{ fontWeight: 500 }}>{text}</span>
          {role.is_system && <Tag color="blue">内置</Tag>}
        </Space>
      ),
    },
    { title: '内部标识', dataIndex: 'name', key: 'name', render: (t) => <code>{t}</code> },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_, role) => (
        <Space>
          <Button size="small" icon={<Pencil size={14} />} onClick={() => openRename(role)}>重命名</Button>
          <Popconfirm
            title="删除角色"
            description="删除后，绑定该角色的用户会立刻失去这个角色，此操作不可逆。"
            okText="确认删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            disabled={role.is_system}
            onConfirm={() => handleDelete(role)}
          >
            <Button size="small" danger icon={<Trash2 size={14} />} disabled={role.is_system}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          角色目录：新建/重命名/删除角色。角色关联哪些知识库，由各企业的企业管理员在自己的「知识库权限」页面配置，平台不管理知识库内容。
        </p>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>新建角色</Button>
      </div>

      <Table
        rowKey="role_id"
        columns={columns}
        dataSource={roles}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无角色" /> }}
      />

      <Modal
        title="新建角色"
        open={createVisible}
        onCancel={() => setCreateVisible(false)}
        onOk={submitCreate}
        confirmLoading={createLoading}
        okText="创建"
        cancelText="取消"
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <div style={{ marginBottom: 4 }}>展示名</div>
            <Input
              placeholder="如：IT部"
              value={createForm.display_name}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, display_name: e.target.value }))}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>内部标识（英文，创建后不可修改）</div>
            <Input
              placeholder="如：it_dept"
              value={createForm.name}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, name: e.target.value }))}
            />
          </div>
        </div>
      </Modal>

      <Modal
        title={`重命名角色`}
        open={renameVisible}
        onCancel={() => setRenameVisible(false)}
        onOk={submitRename}
        confirmLoading={renameLoading}
        okText="保存"
        cancelText="取消"
      >
        <Input value={renameValue} onChange={(e) => setRenameValue(e.target.value)} />
      </Modal>
    </div>
  )
}
