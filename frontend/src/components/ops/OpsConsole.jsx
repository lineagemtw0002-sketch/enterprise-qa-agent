import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Button, Card, Empty, Form, Input, InputNumber, Modal, Popconfirm,
  Segmented, Select, Space, Spin, Table, Tag, Typography, message,
} from 'antd'
import { Plug, KeyRound, ShieldCheck, ClipboardCheck, RefreshCw, Copy } from 'lucide-react'
import * as opsApi from '../../api/ops.js'
import './OpsConsole.css'

const { Paragraph, Text } = Typography

// 四类修复动作的白名单表单——**每类的 scope_config schema 不同**，见
// docs/aiops_module_design.md §10.3。故意做成有类型的表单而不是一个 JSON 文本框：
// 这份配置是"AI 提议的修复动作允许打到哪里"的唯一边界，让管理员手写 JSON 等于
// 把一道安全闸的正确性押在他不会打错字上。
//
// ⚠️ 数值字段必须以**数字**提交，不能是字符串。后端 2026-08-26 修完
// aiops_scope.py 的类型校验之后，字符串会被拒为 InvalidScopeConfig
// （在那之前它会漏出一个裸 TypeError）。InputNumber 天然给数字，这里额外用
// Number() 兜一道，防止将来有人把控件换成 Input 之后静默退化。
const ACTION_TYPES = [
  {
    value: 'restart_service',
    label: '重启服务',
    help: '只有列在这里的服务允许被重启。留空等于不允许任何重启。',
    fields: [{ name: 'allowed_targets', label: '允许重启的服务名', kind: 'list',
               placeholder: '一行一个，如 order-service' }],
  },
  {
    value: 'scale_instances',
    label: '扩缩容',
    help: '上界 = 基线实例数 × 倍数。⚠️ 当前基线来自 AI 提议自身（已知设计缺口，'
        + '见设计文档 §10.3 的待评审项），所以倍数不要设得太宽松。',
    fields: [
      { name: 'min_instances', label: '最少实例数（下界）', kind: 'number', min: 0 },
      { name: 'max_multiplier_of_baseline', label: '相对基线的最大倍数', kind: 'number', min: 1, step: 0.5 },
    ],
  },
  {
    value: 'clean_disk',
    label: '清理磁盘',
    help: '⚠️ 同一路径同时命中「允许」和「排除」时，排除优先。'
        + '另外含 ".." 的路径会被直接拒绝（防路径穿越），不要用相对路径写规则。',
    fields: [
      { name: 'allowed_path_patterns', label: '允许清理的路径模式', kind: 'list',
        placeholder: '一行一个，如 /var/log/app/*.log' },
      { name: 'excluded_path_patterns', label: '排除的路径模式（优先级更高）', kind: 'list',
        placeholder: '一行一个，如 /var/lib/postgresql/*' },
    ],
  },
  {
    value: 'rollback_deployment',
    label: '回滚版本',
    help: '最多允许往回滚几个版本。',
    fields: [{ name: 'max_versions_back', label: '最多回滚版本数', kind: 'number', min: 1 }],
  },
]

const ACTION_LABEL = Object.fromEntries(ACTION_TYPES.map((a) => [a.value, a.label]))

// 状态取值见 ops_store.py 的 STATUS_* 常量与 _STATUS_TRANSITIONS。
const STATUS_META = {
  proposed: { color: 'default', label: '已提议' },
  pending_approval: { color: 'processing', label: '待审批' },
  // 「已批准」不等于「已执行」——批准端点只把状态推到 approved，真正下发是另一条
  // 链路（execute_approved_remediation 工具，由智能运维在对话里发起，见设计 §10.2
  // 「触发链路」与「审批/执行链路」分离）。标签直接把这层说破，省得管理员看到
  // 「已批准」就以为事情办完了。
  approved: { color: 'blue', label: '已批准 · 待执行' },
  rejected: { color: 'red', label: '已拒绝' },
  rejected_pre: { color: 'red', label: '越界拒绝' },
  executing: { color: 'gold', label: '执行中' },
  completed: { color: 'green', label: '已完成' },
  failed: { color: 'red', label: '执行失败' },
  expired: { color: 'default', label: '已超时' },
  rolled_back: { color: 'purple', label: '已回滚' },
}

function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

function StatusTag({ status }) {
  const meta = STATUS_META[status] || { color: 'default', label: status }
  return <Tag color={meta.color}>{meta.label}</Tag>
}

/** 整个页面共用的"模块未开通"态。
 *
 * 后端的模块开关是叠加在 ACL 之前的独立一层，未开通时这些端点一律 403。
 * 把它渲染成一句人话，而不是把 403 原样抛给用户——"没有权限"会让企业管理员
 * 去查自己的角色配置，而真正要做的是找平台管理员开通。
 */
function ModuleDisabled() {
  return (
    <Alert
      className="ops-console-disabled"
      type="info"
      showIcon
      message="本企业尚未开通智能运维模块"
      description="这个模块需要由平台管理员为你所在的企业开通后才能使用。开通之后刷新本页即可。"
    />
  )
}

// ============================================================ 连接器

function ConnectorsSection({ onModuleDisabled, onConnectorsLoaded }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm()
  const [tokenInfo, setTokenInfo] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const list = await opsApi.listConnectors()
      setRows(list)
      onConnectorsLoaded?.(list)
    } catch (error) {
      if (opsApi.isModuleDisabledError(error)) { onModuleDisabled(); return }
      setErr(opsApi.errorText(error))
    } finally {
      setLoading(false)
    }
  }, [onModuleDisabled, onConnectorsLoaded])

  useEffect(() => { load() }, [load])

  async function handleCreate() {
    const values = await form.validateFields()
    setCreating(true)
    try {
      await opsApi.registerConnector({
        name: values.name,
        system_type: values.system_type,
        approval_timeout_minutes: Number(values.approval_timeout_minutes),
      })
      message.success('连接器已登记')
      setCreateOpen(false)
      form.resetFields()
      load()
    } catch (error) {
      message.error(opsApi.errorText(error))
    } finally {
      setCreating(false)
    }
  }

  async function handleToken(row) {
    try {
      const data = await opsApi.generateRegisterToken(row.connection_id)
      setTokenInfo({ ...data, name: row.name })
    } catch (error) {
      message.error(opsApi.errorText(error))
    }
  }

  const columns = [
    { title: '名称', dataIndex: 'name' },
    { title: '系统类型', dataIndex: 'system_type' },
    {
      title: '连接状态',
      dataIndex: 'connector_status',
      render: (s, row) => (
        <Space size={4}>
          <Tag color={s === 'online' ? 'green' : 'default'}>{s === 'online' ? '在线' : '离线'}</Tag>
          <Text type="secondary" className="ops-console-hint">
            最近心跳 {fmtTime(row.last_heartbeat_at)}
          </Text>
        </Space>
      ),
    },
    { title: '审批超时', dataIndex: 'approval_timeout_minutes', render: (m) => `${m} 分钟` },
    { title: '登记时间', dataIndex: 'created_at', render: fmtTime },
    {
      title: '操作',
      render: (_, row) => (
        <Button size="small" icon={<KeyRound size={14} />} onClick={() => handleToken(row)}>
          生成握手凭证
        </Button>
      ),
    },
  ]

  return (
    <>
      <Card
        size="small"
        title="已登记的运维系统连接器"
        extra={
          <Space>
            <Button size="small" icon={<RefreshCw size={14} />} onClick={load}>刷新</Button>
            <Button size="small" type="primary" icon={<Plug size={14} />} onClick={() => setCreateOpen(true)}>
              登记连接器
            </Button>
          </Space>
        }
      >
        <Paragraph type="secondary" className="ops-console-hint">
          连接器是部署在<Text strong>你自己环境里</Text>的一个进程，由它主动向平台发起出站连接。
          你的运维系统凭证只存在你的环境里，平台任何时候都不持有；
          你的防火墙也不需要为此开放任何入站端口。
        </Paragraph>
        {err && <Alert type="error" showIcon message={err} className="ops-console-alert" />}
        <Table
          rowKey="connection_id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有登记任何连接器" /> }}
        />
      </Card>

      <Modal
        title="登记新的连接器"
        open={createOpen}
        onOk={handleCreate}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        okText="登记"
        cancelText="取消"
      >
        <Form form={form} layout="vertical" initialValues={{ approval_timeout_minutes: 30 }}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请填写名称' }]}>
            <Input placeholder="例如：生产环境 Prometheus" />
          </Form.Item>
          <Form.Item name="system_type" label="系统类型" rules={[{ required: true, message: '请填写系统类型' }]}>
            <Input placeholder="例如：prometheus / datadog / 自建" />
          </Form.Item>
          {/* 范围 5–1440 来自 aiops_scope.validate_approval_timeout_minutes，
              后端越界直接拒绝、不静默夹紧，所以这里也不夹紧，让用户看到自己填错了。 */}
          <Form.Item
            name="approval_timeout_minutes"
            label="审批超时（分钟）"
            extra="超过这个时间还没人审批，修复建议自动作废。允许 5 – 1440 分钟。"
            rules={[{ required: true, message: '请填写审批超时' }]}
          >
            <InputNumber min={5} max={1440} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 握手凭证只在这一次响应里明文出现，平台只存哈希——所以必须做成
          "必须当场复制"的形态，不能塞进列表当普通字段。关掉之后平台自己
          也查不出来它是什么。 */}
      <Modal
        title="连接器握手凭证"
        open={!!tokenInfo}
        onCancel={() => setTokenInfo(null)}
        footer={<Button onClick={() => setTokenInfo(null)}>我已复制，关闭</Button>}
      >
        {tokenInfo && (
          <>
            <Alert
              type="warning"
              showIcon
              message="这串凭证只显示这一次"
              description="平台只保存它的哈希值，关闭本窗口后无法再次查看，也无法找回。请立即复制并配置到连接器进程里；弄丢了就重新生成一个。"
            />
            <Paragraph className="ops-console-token" copyable={{ text: tokenInfo.register_token, icon: <Copy size={14} /> }}>
              {tokenInfo.register_token}
            </Paragraph>
            <Text type="secondary">
              适用连接器：{tokenInfo.name}　|　有效期至 {fmtTime(tokenInfo.expires_at)}
            </Text>
          </>
        )}
      </Modal>
    </>
  )
}

// ============================================================ 修复范围白名单

function ScopesSection({ connectors, onModuleDisabled }) {
  const [connectionId, setConnectionId] = useState(null)
  const [scopes, setScopes] = useState([])
  const [loading, setLoading] = useState(false)
  const [actionType, setActionType] = useState(ACTION_TYPES[0].value)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!connectionId && connectors.length) setConnectionId(connectors[0].connection_id)
  }, [connectors, connectionId])

  const load = useCallback(async () => {
    if (!connectionId) return
    setLoading(true)
    try {
      setScopes(await opsApi.listRemediationScopes(connectionId))
    } catch (error) {
      if (opsApi.isModuleDisabledError(error)) { onModuleDisabled(); return }
      message.error(opsApi.errorText(error))
    } finally {
      setLoading(false)
    }
  }, [connectionId, onModuleDisabled])

  useEffect(() => { load() }, [load])

  const spec = ACTION_TYPES.find((a) => a.value === actionType)
  const existing = scopes.find((s) => s.action_type === actionType)

  // 切换动作类型/连接器时，把已存的配置回填进表单——没有回填的话，管理员每次
  // 保存都是在覆盖一份自己没看见的旧配置。
  useEffect(() => {
    const cfg = existing?.scope_config || {}
    const values = {}
    for (const f of spec.fields) {
      values[f.name] = f.kind === 'list' ? (cfg[f.name] || []).join('\n') : cfg[f.name]
    }
    form.setFieldsValue(values)
  }, [existing, spec, form])

  async function handleSave() {
    const values = await form.validateFields()
    const config = {}
    for (const f of spec.fields) {
      if (f.kind === 'list') {
        config[f.name] = String(values[f.name] || '')
          .split('\n').map((s) => s.trim()).filter(Boolean)
      } else {
        // 显式转数字：后端对字符串数值会直接拒为配置错误（见文件顶部说明）。
        config[f.name] = Number(values[f.name])
      }
    }
    setSaving(true)
    try {
      await opsApi.upsertRemediationScope(connectionId, actionType, config)
      message.success('允许范围已保存')
      load()
    } catch (error) {
      message.error(opsApi.errorText(error))
    } finally {
      setSaving(false)
    }
  }

  if (!connectors.length) {
    return <Empty description="先登记至少一个连接器，才能配置修复范围" />
  }

  return (
    <Card size="small" title="修复动作的允许范围" loading={loading}>
      <Alert
        type="info"
        showIcon
        className="ops-console-alert"
        message="没有配置 = 一律不允许"
        description="某类动作没有登记允许范围时，AI 连提议都提不了（不是「先提议再人工把关」）。这是刻意的默认值：如果反过来设计成「没配置就放行」，忘记配置就等于全部放开。"
      />
      <Space className="ops-console-toolbar" wrap>
        <Select
          value={connectionId}
          onChange={setConnectionId}
          style={{ minWidth: 220 }}
          options={connectors.map((c) => ({ value: c.connection_id, label: c.name }))}
        />
        <Segmented
          value={actionType}
          onChange={setActionType}
          options={ACTION_TYPES.map((a) => ({ value: a.value, label: a.label }))}
        />
      </Space>

      <Paragraph type="secondary" className="ops-console-hint">{spec.help}</Paragraph>

      <Form form={form} layout="vertical" className="ops-console-form">
        {spec.fields.map((f) => (
          <Form.Item
            key={f.name}
            name={f.name}
            label={f.label}
            rules={[{ required: true, message: `请填写${f.label}` }]}
          >
            {f.kind === 'list'
              ? <Input.TextArea rows={4} placeholder={f.placeholder} />
              : <InputNumber min={f.min} step={f.step} style={{ width: '100%' }} />}
          </Form.Item>
        ))}
        <Button type="primary" loading={saving} onClick={handleSave} icon={<ShieldCheck size={14} />}>
          保存允许范围
        </Button>
        {existing && (
          <Text type="secondary" className="ops-console-hint">
            　当前配置由 {existing.configured_by} 于 {fmtTime(existing.updated_at)} 更新
          </Text>
        )}
      </Form>
    </Card>
  )
}

// ============================================================ 审批队列

const POLL_INTERVAL_MS = 10000

function ApprovalsSection({ onModuleDisabled }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [acting, setActing] = useState('')
  const timer = useRef(null)

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      setRows(await opsApi.listRemediationActions())
    } catch (error) {
      if (opsApi.isModuleDisabledError(error)) { onModuleDisabled(); return }
      if (!silent) message.error(opsApi.errorText(error))
    } finally {
      if (!silent) setLoading(false)
    }
  }, [onModuleDisabled])

  // 轮询而不是 WebSocket：审批是分钟级的事，10 秒的延迟对它没有意义上的影响，
  // 而这一条链路为此单开一条 WS 连接不划算。
  useEffect(() => {
    load()
    timer.current = setInterval(() => load(true), POLL_INTERVAL_MS)
    return () => clearInterval(timer.current)
  }, [load])

  async function act(actionId, kind) {
    setActing(actionId)
    try {
      if (kind === 'approve') await opsApi.approveRemediationAction(actionId)
      else await opsApi.rejectRemediationAction(actionId)
      message.success(kind === 'approve' ? '已批准' : '已拒绝')
      load()
    } catch (error) {
      // 409 是并发冲突：别人刚刚已经处理过这条了（后端用条件 UPDATE 保证
      // 只有一个人能生效）。这不是报错，是需要刷新看最新状态。
      message.warning(opsApi.errorText(error))
      load()
    } finally {
      setActing('')
    }
  }

  const columns = [
    { title: '意图', dataIndex: 'intent', width: 220 },
    {
      title: '动作',
      dataIndex: 'plan',
      render: (plan, row) => (
        <div>
          <Tag>{ACTION_LABEL[row.plan?.action_type] || row.plan?.action_type || '—'}</Tag>
          <code className="ops-console-plan">{JSON.stringify(plan)}</code>
        </div>
      ),
    },
    { title: '影响范围', dataIndex: 'impact_radius', render: (v) => v || '—' },
    { title: '提议人', dataIndex: 'proposed_by' },
    { title: '状态', dataIndex: 'status', render: (s) => <StatusTag status={s} /> },
    { title: '提议时间', dataIndex: 'created_at', render: fmtTime },
    {
      title: '审批',
      render: (_, row) => {
        if (row.status !== 'pending_approval') {
          return row.approver_user_id
            ? <Text type="secondary">{row.approver_user_id} · {fmtTime(row.approved_at)}</Text>
            : <Text type="secondary">—</Text>
        }
        return (
          <Space>
            <Popconfirm
              title="确认批准这个修复动作？"
              description="批准 = 授予执行资格，本身不会立刻下发；真正执行由智能运维在对话里另行发起。请先确认上面的动作参数。"
              okText="确认批准"
              cancelText="再想想"
              onConfirm={() => act(row.action_id, 'approve')}
            >
              <Button size="small" type="primary" loading={acting === row.action_id}>批准</Button>
            </Popconfirm>
            <Button size="small" danger onClick={() => act(row.action_id, 'reject')}>拒绝</Button>
          </Space>
        )
      },
    },
  ]

  const pending = rows.filter((r) => r.status === 'pending_approval')

  return (
    <Card
      size="small"
      title={`修复动作审批（待处理 ${pending.length} 条）`}
      extra={<Button size="small" icon={<RefreshCw size={14} />} onClick={() => load()}>刷新</Button>}
    >
      {/* 如实反映当前权限粒度：role_ops_systems（can_view/can_approve）只建了表、
          CRUD 未实现，所以现在**任何本企业管理员都能批准**，不存在"指定审批人"
          这一层。UI 上不能暗示有——那会让人以为有一层其实不存在的管控。 */}
      <Alert
        type="warning"
        showIcon
        className="ops-console-alert"
        message="当前本企业任何管理员都可以审批"
        description="精细的审批权限（区分「能查看」和「能审批」）尚未实现，因此这里没有指定审批人的概念。另外注意：批准只是授予执行资格，动作不会因为你点了批准就自动跑起来——真正下发是独立的一步。"
      />
      <Table
        rowKey="action_id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        locale={{ emptyText: <Empty description="还没有任何修复动作" /> }}
      />
    </Card>
  )
}

// ============================================================ 页面

const SECTIONS = [
  { value: 'connectors', label: '连接器', icon: Plug },
  { value: 'scopes', label: '允许范围', icon: ShieldCheck },
  { value: 'approvals', label: '审批队列', icon: ClipboardCheck },
]

export default function OpsConsole() {
  const [section, setSection] = useState('connectors')
  const [moduleDisabled, setModuleDisabled] = useState(false)
  const [connectors, setConnectors] = useState([])
  const [booting, setBooting] = useState(true)

  const onModuleDisabled = useCallback(() => setModuleDisabled(true), [])
  const onConnectorsLoaded = useCallback((list) => { setConnectors(list); setBooting(false) }, [])

  useEffect(() => {
    // 连接器列表是这个页面的第一个请求，它同时充当"模块开没开"的探测。
    // ⚠️ 这是权宜之计：真正该做的是让导航入口本身按开关显示/隐藏，而不是让
    // 用户点进来才发现。做不了的原因是 aiops_module_enabled 目前**没有任何
    // GET 接口暴露**（OrganizationSummary / AdminOrganizationResponse 都没有
    // 这个字段），前端无从提前知道。已反馈给后端那条线，字段加上之后：
    // AdminPanel.jsx 里按 meProfile.organization.aiops_module_enabled 决定
    // 要不要 push 这个 tab，这里的探测逻辑可以保留当兜底。
    const t = setTimeout(() => setBooting(false), 3000)
    return () => clearTimeout(t)
  }, [])

  if (moduleDisabled) return <ModuleDisabled />

  return (
    <div className="ops-console">
      <Segmented
        className="ops-console-nav"
        value={section}
        onChange={setSection}
        options={SECTIONS.map((s) => ({
          value: s.value,
          label: (
            <Space size={6}>
              <s.icon size={14} />
              {s.label}
            </Space>
          ),
        }))}
      />

      {section === 'connectors' && (
        <ConnectorsSection onModuleDisabled={onModuleDisabled} onConnectorsLoaded={onConnectorsLoaded} />
      )}
      {section === 'scopes' && (
        booting ? <Spin /> : <ScopesSection connectors={connectors} onModuleDisabled={onModuleDisabled} />
      )}
      {section === 'approvals' && <ApprovalsSection onModuleDisabled={onModuleDisabled} />}
    </div>
  )
}
