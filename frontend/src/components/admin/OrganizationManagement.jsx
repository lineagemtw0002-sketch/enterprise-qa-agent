import { useEffect, useState } from 'react'
import { Table, Button, Modal, Drawer, Input, Select, Switch, Tag, Space, message, Empty } from 'antd'
import { Plus, Plug } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 两个能力目前各自支持的连接器类型，跟后端 tenant_connector_store.py 里的
// CONNECTOR_TYPE_* 常量、knowledge-base-tenant-federation.md /
// attendance-tenant-federation.md 保持一致。
const CAPABILITIES = [
  {
    key: 'knowledge_base',
    label: '知识库检索',
    connectorTypes: [
      { value: 'internal_chroma', label: 'internal_chroma（内置示例库，默认）' },
      { value: 'http_api', label: 'http_api（委托到企业自己的知识库微服务）' },
    ],
    endpointHint: '企业知识库微服务的 base URL，如 http://localhost:9101',
    showFieldMapping: false,
  },
  {
    key: 'attendance',
    label: '考勤查询',
    connectorTypes: [
      { value: 'internal_postgres', label: 'internal_postgres（内置示例数据，默认）' },
      { value: 'http_webhook', label: 'http_webhook（委托到企业自己的考勤系统）' },
    ],
    endpointHint: '企业考勤系统 webhook 的 base URL，如 http://localhost:9201',
    showFieldMapping: true,
  },
]

function emptyForm(capability) {
  return {
    capability,
    connector_type: CAPABILITIES.find((c) => c.key === capability).connectorTypes[0].value,
    endpoint: '',
    token: '',
    field_mapping: '{}',
    is_active: true,
  }
}

// 单个能力（知识库/考勤）的连接器状态展示 + 编辑表单。
function ConnectorCapabilityCard({ orgId, capability, meta, connector, onSaved }) {
  const [form, setForm] = useState(() => connectorToForm(capability, connector))
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setForm(connectorToForm(capability, connector))
  }, [connector, capability])

  function connectorToForm(cap, c) {
    if (!c) return emptyForm(cap)
    return {
      capability: cap,
      connector_type: c.connector_type,
      endpoint: c.endpoint || '',
      token: '',
      field_mapping: JSON.stringify(c.field_mapping || {}, null, 2),
      is_active: c.is_active,
    }
  }

  async function handleSave() {
    let fieldMapping = {}
    try {
      fieldMapping = form.field_mapping.trim() ? JSON.parse(form.field_mapping) : {}
    } catch {
      message.error('字段映射不是合法的 JSON')
      return
    }
    setSaving(true)
    try {
      await adminApi.upsertTenantConnector(orgId, capability, {
        connector_type: form.connector_type,
        endpoint: form.endpoint.trim() || null,
        token: form.token.trim() || null,
        field_mapping: fieldMapping,
        is_active: form.is_active,
      })
      message.success(`${meta.label} 连接器已保存`)
      setForm((prev) => ({ ...prev, token: '' }))
      await onSaved()
    } catch (error) {
      message.error('保存失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ border: '1px solid var(--border-color, #e5e5e5)', borderRadius: 8, padding: 16, marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <strong>{meta.label}</strong>
        <Space>
          {connector ? (
            <>
              <Tag color={connector.connector_type.startsWith('internal') ? 'default' : 'blue'}>
                {connector.connector_type}
              </Tag>
              <Tag color={connector.has_token ? 'green' : 'orange'}>
                {connector.has_token ? '已配置凭证' : '未配置凭证'}
              </Tag>
              <Tag color={connector.is_active ? 'success' : 'default'}>
                {connector.is_active ? '启用中' : '已停用'}
              </Tag>
            </>
          ) : (
            <Tag>未配置连接器（走内置默认实现）</Tag>
          )}
        </Space>
      </div>

      <div style={{ display: 'grid', gap: 8 }}>
        <div>
          <div style={{ marginBottom: 4, fontSize: 12, color: 'var(--text-tertiary, #888)' }}>连接器类型</div>
          <Select
            style={{ width: '100%' }}
            value={form.connector_type}
            onChange={(v) => setForm((prev) => ({ ...prev, connector_type: v }))}
            options={meta.connectorTypes}
          />
        </div>
        <div>
          <div style={{ marginBottom: 4, fontSize: 12, color: 'var(--text-tertiary, #888)' }}>Endpoint</div>
          <Input
            placeholder={meta.endpointHint}
            value={form.endpoint}
            onChange={(e) => setForm((prev) => ({ ...prev, endpoint: e.target.value }))}
          />
        </div>
        <div>
          <div style={{ marginBottom: 4, fontSize: 12, color: 'var(--text-tertiary, #888)' }}>
            Bearer Token（留空表示不修改现有凭证）
          </div>
          <Input.Password
            value={form.token}
            onChange={(e) => setForm((prev) => ({ ...prev, token: e.target.value }))}
          />
        </div>
        {meta.showFieldMapping && (
          <div>
            <div style={{ marginBottom: 4, fontSize: 12, color: 'var(--text-tertiary, #888)' }}>
              字段映射（企业自己系统的字段名 → 我方规范字段名，JSON）
            </div>
            <Input.TextArea
              rows={4}
              value={form.field_mapping}
              onChange={(e) => setForm((prev) => ({ ...prev, field_mapping: e.target.value }))}
            />
          </div>
        )}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Switch
            checked={form.is_active}
            onChange={(v) => setForm((prev) => ({ ...prev, is_active: v }))}
          />
          <span style={{ fontSize: 12, color: 'var(--text-tertiary, #888)' }}>启用</span>
        </div>
        <Button type="primary" loading={saving} onClick={handleSave} style={{ justifySelf: 'start' }}>
          保存
        </Button>
      </div>
    </div>
  )
}

function ConnectorDrawer({ org, open, onClose }) {
  const [connectors, setConnectors] = useState([])
  const [loading, setLoading] = useState(false)

  async function load() {
    if (!org) return
    setLoading(true)
    try {
      setConnectors(await adminApi.listTenantConnectors(org.org_id))
    } catch (error) {
      message.error('加载连接器失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (open) load() }, [open, org])

  return (
    <Drawer
      title={org ? `连接器配置 — ${org.name}` : '连接器配置'}
      width={480}
      open={open}
      onClose={onClose}
      loading={loading}
    >
      {org && CAPABILITIES.map((meta) => (
        <ConnectorCapabilityCard
          key={meta.key}
          orgId={org.org_id}
          capability={meta.key}
          meta={meta}
          connector={connectors.find((c) => c.capability === meta.key) || null}
          onSaved={load}
        />
      ))}
    </Drawer>
  )
}

export default function OrganizationManagement() {
  const [organizations, setOrganizations] = useState([])
  const [loading, setLoading] = useState(false)

  const [createVisible, setCreateVisible] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createLoading, setCreateLoading] = useState(false)

  const [connectorOrg, setConnectorOrg] = useState(null)

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
    {
      title: '操作',
      key: 'actions',
      width: 140,
      render: (_, org) =>
        org.is_platform ? null : (
          <Button icon={<Plug size={14} />} size="small" onClick={() => setConnectorOrg(org)}>
            连接器
          </Button>
        ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          新建的企业员工需要在"用户与角色分配"里手动改派过去；点击"连接器"可以给企业配置知识库/考勤
          委托（把检索委托到企业自己的微服务，见 knowledge-base-tenant-federation.md / attendance-tenant-federation.md）。
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

      <ConnectorDrawer org={connectorOrg} open={!!connectorOrg} onClose={() => setConnectorOrg(null)} />
    </div>
  )
}
