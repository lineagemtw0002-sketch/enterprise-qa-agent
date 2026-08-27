import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Popconfirm, message, Empty, Alert, Space } from 'antd'
import { Plus, Eye, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 企业管理员的「知识库管理」页面：只管知识库本身（collections）——新增/
// 删除/分页查看已摄入的数据。知识库跟角色的关联关系（一个角色可以对应多个
// 知识库）在「角色管理」页面（CompanyRoleManagement.jsx）做，那边才是配置
// "谁能看哪个知识库"的地方，这个页面单纯是知识库的增删查。
// 只对走本地检索的企业开放——像 Acme/Globex 这类把知识库检索委托给自己
// 系统的企业，本地新建/查看/删除的知识库对它们的实际问答没有意义，后端会
// 直接 400，这里捕获后用醒目的提示替换掉整个页面。
const CHUNKS_PAGE_SIZE = 20

export default function CompanyKbManagement() {
  const [collections, setCollections] = useState([])
  const [groups, setGroups] = useState([])
  const [collectionsError, setCollectionsError] = useState('')
  const [loading, setLoading] = useState(false)

  const [createVisible, setCreateVisible] = useState(false)
  const [createForm, setCreateForm] = useState({ collection_name: '', display_name: '' })
  const [createLoading, setCreateLoading] = useState(false)

  const [viewDataVisible, setViewDataVisible] = useState(false)
  const [viewDataTarget, setViewDataTarget] = useState(null)
  const [viewDataChunks, setViewDataChunks] = useState([])
  const [viewDataPage, setViewDataPage] = useState(1)
  const [viewDataLoading, setViewDataLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    setCollectionsError('')
    try {
      const collectionList = await adminApi.listCollections()
      setCollections(collectionList)
    } catch (error) {
      setCollections([])
      setCollectionsError(error.response?.data?.detail || error.message)
      setLoading(false)
      return
    }
    // 只读拉一下角色列表——纯粹用来在删除知识库前提示"哪些角色正引用它"，
    // 不在这个页面展示/编辑角色本身（那是「角色管理」页面的事）。委托模式
    // 企业上面已经 return 了，不会走到这里。
    try {
      const groupList = await adminApi.listRoles()
      setGroups(groupList)
    } catch (error) {
      setGroups([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

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
      message.success('知识库创建成功；文档摄入仍需通过现有摄入脚本完成，创建好之后可以去「角色管理」页面给角色关联它')
      setCreateVisible(false)
      await loadAll()
    } catch (error) {
      message.error('创建失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setCreateLoading(false)
    }
  }

  // 引用这个知识库的角色名字——删除前提示清楚，删了之后这些角色会立刻失去
  // 对应的访问权限（后端 admin_delete_collection 会自动把它从这些角色的
  // 关联里摘掉，见该端点旁的说明，不是留着一个查不到内容的僵尸关联）。
  function groupsReferencing(collectionName) {
    return groups.filter((g) => (g.collection_names || []).includes(collectionName)).map((g) => g.display_name)
  }

  async function handleDeleteCollection(collection) {
    try {
      await adminApi.deleteCollection(collection.collection_name)
      message.success('知识库已删除')
      await loadAll()
    } catch (error) {
      message.error('删除失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  async function openViewData(collection, page = 1) {
    setViewDataTarget(collection)
    setViewDataPage(page)
    setViewDataVisible(true)
    setViewDataLoading(true)
    try {
      const chunks = await adminApi.listCollectionChunks(collection.collection_name, {
        offset: (page - 1) * CHUNKS_PAGE_SIZE,
        limit: CHUNKS_PAGE_SIZE,
      })
      setViewDataChunks(chunks)
    } catch (error) {
      message.error('加载知识库数据失败: ' + (error.response?.data?.detail || error.message))
      setViewDataChunks([])
    } finally {
      setViewDataLoading(false)
    }
  }

  function changeViewDataPage(page) {
    openViewData(viewDataTarget, page)
  }

  const columns = [
    { title: '知识库', dataIndex: 'display_name', key: 'display_name', render: (t) => <span style={{ fontWeight: 500 }}>{t}</span> },
    { title: '内部标识', dataIndex: 'collection_name', key: 'collection_name', render: (t) => <code>{t}</code> },
    {
      title: '已摄入 chunk 数',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 160,
      render: (n) => (n ? n : <span style={{ color: 'var(--text-tertiary, #999)' }}>0（尚未摄入）</span>),
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (_, collection) => {
        const referencingGroups = groupsReferencing(collection.collection_name)
        return (
          <Space>
            <Button size="small" icon={<Eye size={14} />} onClick={() => openViewData(collection, 1)}>查看数据</Button>
            <Popconfirm
              title="删除知识库"
              description={
                referencingGroups.length
                  ? `此操作不可逆，会连同已摄入的文档一起物理删除；${referencingGroups.join('、')} 这 ${referencingGroups.length} 个角色正引用它，删除后会自动从这些角色的关联中移除。`
                  : '此操作不可逆，会连同已摄入的文档一起物理删除。'
              }
              okText="确认删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDeleteCollection(collection)}
            >
              <Button size="small" danger icon={<Trash2 size={14} />}>删除</Button>
            </Popconfirm>
          </Space>
        )
      },
    },
  ]

  if (collectionsError) {
    return (
      <div>
        <Alert type="warning" showIcon message="知识库管理不适用于本企业" description={collectionsError} />
      </div>
    )
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16, gap: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          知识库：只登记名字，文档内容需要通过现有摄入脚本另外导入；可以分页查看已摄入的原始内容，删除是真删（物理数据一起清掉），不可恢复。知识库跟角色的关联关系去「角色管理」页面配置。
        </p>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate} style={{ flexShrink: 0 }}>新增知识库</Button>
      </div>

      <Table
        rowKey="collection_name"
        columns={columns}
        dataSource={collections}
        loading={loading}
        pagination={{ pageSize: 10 }}
        locale={{ emptyText: <Empty description="暂无知识库，先点上方「新增知识库」" /> }}
      />

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

      <Modal
        title={`查看数据 · ${viewDataTarget?.display_name || ''}`}
        open={viewDataVisible}
        onCancel={() => setViewDataVisible(false)}
        footer={null}
        width={720}
      >
        <Table
          rowKey="chunk_id"
          loading={viewDataLoading}
          dataSource={viewDataChunks}
          pagination={{
            current: viewDataPage,
            pageSize: CHUNKS_PAGE_SIZE,
            total: viewDataTarget?.chunk_count || 0,
            onChange: changeViewDataPage,
            showSizeChanger: false,
          }}
          locale={{ emptyText: <Empty description="这个知识库还没有摄入任何内容" /> }}
          columns={[
            {
              title: '来源',
              dataIndex: 'source_path',
              key: 'source_path',
              width: 200,
              render: (t) => <span style={{ fontSize: 12, color: 'var(--text-tertiary, #999)' }}>{t || '—'}</span>,
            },
            {
              title: '内容',
              dataIndex: 'text',
              key: 'text',
              render: (t) => <div style={{ whiteSpace: 'pre-wrap', maxHeight: 120, overflow: 'auto' }}>{t}</div>,
            },
          ]}
        />
      </Modal>
    </div>
  )
}
