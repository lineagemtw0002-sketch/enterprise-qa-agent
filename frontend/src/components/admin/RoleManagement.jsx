import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Checkbox, Tag, Space, Popconfirm, message, Empty } from 'antd'
import { Plus, Database, Pencil, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

export default function RoleManagement() {
  const [roles, setRoles] = useState([])
  const [collections, setCollections] = useState([])
  const [loading, setLoading] = useState(false)

  const [createVisible, setCreateVisible] = useState(false)
  const [createForm, setCreateForm] = useState({ name: '', display_name: '' })
  const [createLoading, setCreateLoading] = useState(false)

  const [renameVisible, setRenameVisible] = useState(false)
  const [renameTarget, setRenameTarget] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renameLoading, setRenameLoading] = useState(false)

  const [collectionsVisible, setCollectionsVisible] = useState(false)
  const [collectionsTarget, setCollectionsTarget] = useState(null)
  const [selectedCollections, setSelectedCollections] = useState([])
  const [collectionsLoading, setCollectionsLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    try {
      const [roleList, collectionList] = await Promise.all([
        adminApi.listRoles(),
        adminApi.listCollections(),
      ])
      setRoles(roleList)
      setCollections(collectionList)
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

  function openCollections(role) {
    setCollectionsTarget(role)
    setSelectedCollections(role.collection_names || [])
    setCollectionsVisible(true)
  }

  async function submitCollections() {
    setCollectionsLoading(true)
    try {
      await adminApi.setRoleCollections(collectionsTarget.role_id, selectedCollections)
      message.success('知识库权限已保存')
      setCollectionsVisible(false)
      await loadAll()
    } catch (error) {
      message.error('保存失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setCollectionsLoading(false)
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
      title: '关联知识库',
      dataIndex: 'collection_names',
      key: 'collection_names',
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
          <span style={{ color: 'var(--text-tertiary, #999)' }}>未配置</span>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (_, role) => (
        <Space>
          <Button size="small" icon={<Pencil size={14} />} onClick={() => openRename(role)}>重命名</Button>
          <Button size="small" icon={<Database size={14} />} onClick={() => openCollections(role)}>配置知识库</Button>
          <Popconfirm
            title="删除角色"
            description="删除后，绑定该角色的用户将立刻失去其关联的知识库访问权限，此操作不可逆。"
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
          角色是知识库授权的分组单元：给角色配知识库，再把角色分配给用户，用户能访问的知识库是他所有角色的并集。
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

      <Modal
        title={`配置知识库 · ${collectionsTarget?.display_name || ''}`}
        open={collectionsVisible}
        onCancel={() => setCollectionsVisible(false)}
        onOk={submitCollections}
        confirmLoading={collectionsLoading}
        okText="保存"
        cancelText="取消"
      >
        {collections.length === 0 ? (
          <Empty description="暂无可用知识库" />
        ) : (
          <Checkbox.Group
            style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
            value={selectedCollections}
            onChange={setSelectedCollections}
            options={collections.map((c) => ({ label: c, value: c }))}
          />
        )}
      </Modal>
    </div>
  )
}
