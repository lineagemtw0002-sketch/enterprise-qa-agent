import { useEffect, useState } from 'react'
import { Table, Tag, Space, message, Empty } from 'antd'
import * as workflowApi from '../../api/workflow.js'

// 平台管理员这里只管工作流的表单结构（哪些字段、附件提醒文案）——跨企业
// 共用，不含审批人信息。2026-08-23 起"这类流程谁来批"改成企业内部的事，
// 由各企业管理员在自己的「审批设置」页面配置（CompanyWorkflowApprovers.jsx），
// 平台这边不再管，也看不到每家企业具体配了谁。
export default function WorkflowTemplateManagement() {
  const [templates, setTemplates] = useState([])
  const [loading, setLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    try {
      const templateList = await workflowApi.adminListWorkflowTemplates()
      setTemplates(templateList)
    } catch (error) {
      message.error('加载工作流模板失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

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
    { title: '说明', dataIndex: 'description', key: 'description' },
    { title: '必填字段数', dataIndex: 'required_fields', key: 'required_fields', render: (fields) => (fields || []).length },
  ]

  return (
    <div>
      <p style={{ margin: '0 0 16px', color: 'var(--text-tertiary, #888)' }}>
        工作流模板：跨企业共用的表单结构（哪些字段、附件提醒文案），不含审批人——每家企业给这些流程配置谁来审批，由各自的企业管理员在自己的「审批设置」页面管理，互不影响。
      </p>

      <Table
        rowKey="template_id"
        columns={columns}
        dataSource={templates}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无工作流模板" /> }}
      />
    </div>
  )
}
