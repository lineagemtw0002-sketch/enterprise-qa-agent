import { useEffect, useState } from 'react'
import { Table, Button, Modal, Select, Tag, Space, message, Empty } from 'antd'
import { Pencil } from 'lucide-react'
import * as workflowApi from '../../api/workflow.js'
import * as adminApi from '../../api/admin.js'

export default function WorkflowTemplateManagement() {
  const [templates, setTemplates] = useState([])
  const [roles, setRoles] = useState([])
  const [loading, setLoading] = useState(false)

  const [editVisible, setEditVisible] = useState(false)
  const [editTarget, setEditTarget] = useState(null)
  const [editRoleId, setEditRoleId] = useState(null)
  const [editLoading, setEditLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    try {
      const [templateList, roleList] = await Promise.all([
        workflowApi.adminListWorkflowTemplates(),
        adminApi.listRoles(),
      ])
      setTemplates(templateList)
      setRoles(roleList)
    } catch (error) {
      message.error('加载工作流模板失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  function roleDisplayName(roleId) {
    if (!roleId) return null
    return roles.find((r) => r.role_id === roleId)?.display_name || roleId
  }

  function openEdit(template) {
    setEditTarget(template)
    setEditRoleId(template.approver_role_id || null)
    setEditVisible(true)
  }

  async function submitEdit() {
    if (!editRoleId) {
      message.warning('请选择审批角色')
      return
    }
    setEditLoading(true)
    try {
      await workflowApi.adminUpdateWorkflowTemplate(editTarget.template_id, { approver_role_id: editRoleId })
      message.success('审批角色已更新')
      setEditVisible(false)
      await loadAll()
    } catch (error) {
      message.error('更新失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setEditLoading(false)
    }
  }

  const columns = [
    {
      title: '工作流',
      dataIndex: 'display_name',
      key: 'display_name',
      render: (text, template) => (
        <Space>
          <span style={{ fontWeight: 500 }}>{text}</span>
          {template.is_system && <Tag color="blue">内置</Tag>}
        </Space>
      ),
    },
    { title: '内部类型', dataIndex: 'workflow_type', key: 'workflow_type', render: (t) => <code>{t}</code> },
    { title: '必填字段数', dataIndex: 'required_fields', key: 'required_fields', render: (fields) => (fields || []).length },
    {
      title: '审批角色',
      dataIndex: 'approver_role_id',
      key: 'approver_role_id',
      render: (roleId) =>
        roleId ? (
          <Tag color="green">{roleDisplayName(roleId)}</Tag>
        ) : (
          <Tag color="orange">未配置</Tag>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 160,
      render: (_, template) => (
        <Button size="small" icon={<Pencil size={14} />} onClick={() => openEdit(template)}>修改审批角色</Button>
      ),
    },
  ]

  return (
    <div>
      <p style={{ margin: '0 0 16px', color: 'var(--text-tertiary, #888)' }}>
        每个工作流的审批人由所选角色的成员承担——成员发起该类工作流后，持有对应角色的用户会在"待我审批"里看到它。未配置审批角色的工作流无法被发起。
      </p>

      <Table
        rowKey="template_id"
        columns={columns}
        dataSource={templates}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无工作流模板" /> }}
      />

      <Modal
        title={`修改审批角色 · ${editTarget?.display_name || ''}`}
        open={editVisible}
        onCancel={() => setEditVisible(false)}
        onOk={submitEdit}
        confirmLoading={editLoading}
        okText="保存"
        cancelText="取消"
      >
        <Select
          style={{ width: '100%' }}
          placeholder="选择审批角色"
          value={editRoleId}
          onChange={setEditRoleId}
          options={roles.map((r) => ({ label: r.display_name, value: r.role_id }))}
        />
      </Modal>
    </div>
  )
}
