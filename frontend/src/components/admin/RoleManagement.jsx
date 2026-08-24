import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Tag, Space, Popconfirm, message, Empty } from 'antd'
import { Pencil, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 角色直接携带知识库权限（2026-08-23 起"身份"和"角色"合并成一个概念，见
// role_store.py 顶部说明），分两类：全局角色（这个页面管的，org_id 为空）
// 和企业角色（org_id 非空，各企业管理员自己建的、能配置知识库关联的角色，
// 在企业管理员自己的「角色管理」tab 里管，CompanyRoleManagement.jsx，只在
// 自己企业内可见）。
//
// 全局角色目前固定只有 super_admin/org_admin 两个系统内置角色（2026-08-24
// 起平台侧废弃 admin/user，见 role_store.py 顶部说明），且都不可删除。这个
// 页面原本还带"新建角色"——建一个全局共享的部门身份（如"IT部"），设想是
// 让不同企业的员工都能挂到同一个角色上，工作流模板挂这个全局角色当审批人，
// 实现"角色名字跨企业统一"。但 2026-08-23 工作流审批人分配改成了按企业
// 独立配置（workflow_approver_roles 表按 (workflow_type, org_id) 存，
// app.py 的 PUT /admin/workflow-approvers/{workflow_type} 端点显式要求
// `approver_role.org_id == org.org_id`，全局角色永远过不了这道校验）——
// 工作流是"企业内部的事"，只能用该企业自己建的角色，平台管理员不参与。
// 这条路径没有别的消费方，"新建全局角色"这个功能连同按钮已经一并去掉，
// 平台管理员现在能做的只有给这两个系统角色改展示名。
export default function RoleManagement() {
  const [roles, setRoles] = useState([])
  const [loading, setLoading] = useState(false)

  const [renameVisible, setRenameVisible] = useState(false)
  const [renameTarget, setRenameTarget] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renameLoading, setRenameLoading] = useState(false)

  async function loadAll() {
    setLoading(true)
    try {
      const roleList = await adminApi.listRoles()
      setRoles(roleList)
    } catch (error) {
      message.error('加载角色列表失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

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
      <div style={{ marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          全局角色目录：超级管理员/企业管理员两个系统内置角色，只能改展示名，不可删除、不可新建——工作流审批人现在由各企业管理员在自己的「审批设置」页面配置，全局角色不再服务这个用途；知识库权限相关的角色由各企业管理员在自己的「角色管理」页面管理。
        </p>
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
        title="重命名角色"
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
