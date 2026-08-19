import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Tag, message, Empty } from 'antd'
import { Plus } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 只做组织本身的增/查——给每个组织配置考勤数据源（连接器）是
// attendance-tenant-federation.md 第 5 节已经设计过的另一块，这里不重复。
export default function OrganizationManagement() {
  const [organizations, setOrganizations] = useState([])
  const [loading, setLoading] = useState(false)

  const [createVisible, setCreateVisible] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createLoading, setCreateLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    try {
      setOrganizations(await adminApi.listOrganizations())
    } catch (error) {
      message.error('加载组织列表失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  function openCreate() {
    setCreateName('')
    setCreateVisible(true)
  }

  async function submitCreate() {
    if (!createName.trim()) {
      message.warning('请填写企业名称')
      return
    }
    setCreateLoading(true)
    try {
      await adminApi.createOrganization(createName.trim())
      message.success('企业创建成功')
      setCreateVisible(false)
      await loadAll()
    } catch (error) {
      message.error('创建失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setCreateLoading(false)
    }
  }

  const columns = [
    { title: '企业名称', dataIndex: 'name', key: 'name' },
    {
      title: '类型',
      dataIndex: 'is_platform',
      key: 'is_platform',
      width: 140,
      render: (isPlatform) =>
        isPlatform ? <Tag color="purple">平台运营方</Tag> : <Tag>客户企业</Tag>,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (ts) => new Date(ts * 1000).toLocaleString('zh-CN'),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          新建的企业员工需要在"用户与角色分配"里手动改派过去；给企业接入自己的考勤等数据源是另一项设置，还没上线。
        </p>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>新建企业</Button>
      </div>

      <Table
        rowKey="org_id"
        columns={columns}
        dataSource={organizations}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无企业" /> }}
      />

      <Modal
        title="新建企业"
        open={createVisible}
        onCancel={() => setCreateVisible(false)}
        onOk={submitCreate}
        confirmLoading={createLoading}
        okText="创建"
        cancelText="取消"
      >
        <div style={{ marginBottom: 4 }}>企业名称</div>
        <Input value={createName} onChange={(e) => setCreateName(e.target.value)} />
      </Modal>
    </div>
  )
}
