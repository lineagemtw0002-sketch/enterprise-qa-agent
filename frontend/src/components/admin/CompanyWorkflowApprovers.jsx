import { useEffect, useState } from 'react'
import { Table, Select, Tag, message, Empty } from 'antd'
import * as workflowApi from '../../api/workflow.js'
import * as adminApi from '../../api/admin.js'

// 企业管理员的「审批设置」页面："这类流程谁来批"是企业内部的事——同一个
// 工作流类型（比如"请假申请"）在不同企业可以配不同的审批角色，只能用本
// 企业自己建的角色（全局共用的部门/系统角色不再作为审批人来源，跟角色/
// 知识库一样收归到"企业内部管理"），见 app.py 工作流审批人分配 API 旁的
// 说明。没配置审批人的流程类型，员工在聊天里发起时会被提示联系管理员配置，
// 发不出去。
export default function CompanyWorkflowApprovers() {
  const [assignments, setAssignments] = useState([])
  const [roles, setRoles] = useState([])
  const [loading, setLoading] = useState(false)
  const [savingType, setSavingType] = useState(null)

  async function loadAll() {
    setLoading(true)
    try {
      const [assignmentList, roleList] = await Promise.all([
        workflowApi.adminListWorkflowApprovers(),
        adminApi.listRoles(),
      ])
      setAssignments(assignmentList)
      // 只能选本企业自己建的角色（org_id 非空且是自己的）——全局角色（部门/
      // 系统身份）后端会直接拒绝，这里提前过滤掉，不让管理员选了白选。
      setRoles(roleList.filter((r) => !!r.org_id))
    } catch (error) {
      message.error('加载审批设置失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  async function handleChange(workflowType, roleId) {
    setSavingType(workflowType)
    try {
      const updated = await workflowApi.adminSetWorkflowApprover(workflowType, roleId || null)
      setAssignments((prev) => prev.map((a) => (a.workflow_type === workflowType ? updated : a)))
      message.success(`已更新「${updated.display_name}」的审批角色`)
    } catch (error) {
      message.error('更新失败: ' + (error.response?.data?.detail || error.message))
      await loadAll()
    } finally {
      setSavingType(null)
    }
  }

  const roleOptions = roles.map((r) => ({ label: r.display_name, value: r.role_id }))

  const columns = [
    {
      title: '工作流',
      dataIndex: 'display_name',
      key: 'display_name',
      render: (t) => <span style={{ fontWeight: 500 }}>{t}</span>,
    },
    { title: '内部类型', dataIndex: 'workflow_type', key: 'workflow_type', render: (t) => <code>{t}</code> },
    {
      title: '审批角色',
      key: 'approver_role_id',
      width: 320,
      render: (_, record) => (
        <div>
          <Select
            allowClear
            style={{ width: '100%' }}
            placeholder="未配置，员工发起不了这类申请"
            value={record.approver_role_id || undefined}
            options={roleOptions}
            loading={savingType === record.workflow_type}
            onChange={(roleId) => handleChange(record.workflow_type, roleId)}
          />
          {!record.approver_role_id && (
            <div style={{ marginTop: 4 }}>
              <Tag color="orange">未配置</Tag>
            </div>
          )}
        </div>
      ),
    },
  ]

  return (
    <div>
      <p style={{ margin: '0 0 16px', color: 'var(--text-tertiary, #888)' }}>
        给每类工作流选一个本企业自己的角色作为审批人——员工发起该类申请后，持有这个角色的同事会在"待我审批"里看到，不需要重新登录立即生效。没有可选角色的话，先去「角色管理」页面新建一个。
      </p>

      <Table
        rowKey="workflow_type"
        columns={columns}
        dataSource={assignments}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无工作流模板" /> }}
      />
    </div>
  )
}
