import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Checkbox, Tag, Space, message, Empty, Alert } from 'antd'
import { Database, Plus } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 企业管理员的「知识库权限」页面：轻量版角色-知识库配置，只对「本企业员工
// 已经持有的角色」开放勾选/取消关联知识库，不能新建、重命名、删除角色——
// 角色是全平台共享的词表，没有按企业隔离，增删这类操作留给平台管理员在
// 「角色管理」里做（见 app.py admin_list_company_roles 旁的说明）。
// 「新增知识库」只登记名字和归属企业（见 app.py admin_create_collection /
// collection_store.py），文档摄入仍走现有的摄入脚本，这里不做文件上传。
// 只对走本地检索的企业开放——像 Acme/Globex 这类把知识库检索委托给自己系统
// 的企业，本地新建/关联的知识库对它们的实际问答没有意义，后端会直接 400，
// 这里捕获后用醒目的提示替换掉整个页面，而不是让企业管理员对着一个空列表
// 摸不着头脑。
export default function CompanyKbPermissions() {
  const [roles, setRoles] = useState([])
  const [collections, setCollections] = useState([])
  const [collectionsError, setCollectionsError] = useState('')
  const [loading, setLoading] = useState(false)

  const [collectionsVisible, setCollectionsVisible] = useState(false)
  const [collectionsTarget, setCollectionsTarget] = useState(null)
  const [selectedCollections, setSelectedCollections] = useState([])
  const [collectionsLoading, setCollectionsLoading] = useState(false)

  const [createVisible, setCreateVisible] = useState(false)
  const [createForm, setCreateForm] = useState({ collection_name: '', display_name: '' })
  const [createLoading, setCreateLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    setCollectionsError('')
    try {
      const roleList = await adminApi.listCompanyRoles()
      setRoles(roleList)
    } catch (error) {
      message.error('加载角色列表失败: ' + (error.response?.data?.detail || error.message))
    }
    // 单独 try/catch——委托模式企业这里会 400，不能让它连累上面角色列表也加载不出来。
    try {
      const collectionList = await adminApi.listCollections()
      setCollections(collectionList)
    } catch (error) {
      setCollections([])
      setCollectionsError(error.response?.data?.detail || error.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  const collectionLabel = (name) => collections.find((c) => c.collection_name === name)?.display_name || name

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

  function openCreate() {
    setCreateForm({ collection_name: '', display_name: '' })
    setCreateVisible(true)
  }

  async function submitCreate() {
    if (!createForm.collection_name.trim() || !createForm.display_name.trim()) {
      message.warning('请填写完整')
      return
    }
    setCreateLoading(true)
    try {
      await adminApi.createCollection({
        collection_name: createForm.collection_name.trim(),
        display_name: createForm.display_name.trim(),
      })
      message.success('知识库创建成功，可以去「用户与角色分配」/上面表格给角色配置访问权限了；文档摄入仍需通过现有摄入脚本完成')
      setCreateVisible(false)
      await loadAll()
    } catch (error) {
      message.error('创建失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setCreateLoading(false)
    }
  }

  const columns = [
    { title: '角色', dataIndex: 'display_name', key: 'display_name', render: (t) => <span style={{ fontWeight: 500 }}>{t}</span> },
    {
      title: '可访问知识库',
      dataIndex: 'collection_names',
      key: 'collection_names',
      render: (names) =>
        names && names.length ? (
          names.includes('*') ? (
            <Tag color="purple">不限</Tag>
          ) : (
            <Space size={[0, 4]} wrap>
              {names.map((n) => <Tag key={n}>{collectionLabel(n)}</Tag>)}
            </Space>
          )
        ) : (
          <span style={{ color: 'var(--text-tertiary, #999)' }}>未配置</span>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 140,
      render: (_, role) => (
        <Button size="small" icon={<Database size={14} />} onClick={() => openCollections(role)} disabled={!!collectionsError}>配置知识库</Button>
      ),
    },
  ]

  if (collectionsError) {
    return (
      <div>
        <Alert type="warning" showIcon message="知识库权限不适用于本企业" description={collectionsError} />
      </div>
    )
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16, gap: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          给「用户与角色分配」里已经分配给本企业员工的角色配置可访问的知识库，员工能访问的知识库是他所有角色关联知识库的并集，不需要重新登录立即生效。
          新增部门角色请联系平台管理员；新增知识库只登记名字，文档内容需要通过现有摄入脚本另外导入。
        </p>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate} style={{ flexShrink: 0 }}>新增知识库</Button>
      </div>

      <Table
        rowKey="role_id"
        columns={columns}
        dataSource={roles}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无本企业员工持有的角色，先去「用户与角色分配」给员工分配角色" /> }}
      />

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
          <Empty description="暂无可用知识库，先点右上角「新增知识库」建一个" />
        ) : (
          <Checkbox.Group
            style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
            value={selectedCollections}
            onChange={setSelectedCollections}
            options={collections.map((c) => ({ label: c.display_name, value: c.collection_name }))}
          />
        )}
      </Modal>

      <Modal
        title="新增知识库"
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
              placeholder="如：产品需求文档库"
              value={createForm.display_name}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, display_name: e.target.value }))}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>内部标识（英文小写+下划线，创建后不可修改）</div>
            <Input
              placeholder="如：product_req_kb"
              value={createForm.collection_name}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, collection_name: e.target.value }))}
            />
          </div>
          <div style={{ color: 'var(--text-tertiary, #999)', fontSize: 12 }}>
            创建后知识库是空的，需要通过现有的文档摄入脚本把内容导入到这个内部标识对应的知识库里。
          </div>
        </div>
      </Modal>
    </div>
  )
}
