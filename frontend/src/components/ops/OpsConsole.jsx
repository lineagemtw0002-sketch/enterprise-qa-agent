import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Button, Card, Checkbox, ConfigProvider, Empty, Form, Input, InputNumber,
  Modal, Popconfirm, Segmented, Select, Space, Spin, Table, Tag, Typography, message, theme,
} from 'antd'
import { Plug, KeyRound, ShieldCheck, ClipboardCheck, RefreshCw, Copy, Users, Trash2, LayoutDashboard, History } from 'lucide-react'
import OpsOverview from './OpsOverview.jsx'
import OpsPostmortems from './OpsPostmortems.jsx'
import { displayUser, formatPlan, useUserNames } from './opsDisplay.jsx'
import * as adminApi from '../../api/admin.js'
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
  const [deleting, setDeleting] = useState('')

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

  async function handleDelete(row) {
    setDeleting(row.connection_id)
    try {
      await opsApi.deleteConnector(row.connection_id)
      message.success(`已删除连接器「${row.name}」及其名下全部数据`)
      load()
    } catch (error) {
      message.error(opsApi.errorText(error))
    } finally {
      setDeleting('')
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
        <Space>
          <Button size="small" icon={<KeyRound size={14} />} onClick={() => handleToken(row)}>
            生成握手凭证
          </Button>
          {/* 删除是硬删除且级联——把"会一起消失什么"逐条列出来再让人点。
              只写"确定删除吗"的话，管理员不会意识到审批历史也跟着没了。 */}
          <Popconfirm
            title="删除这个连接器？"
            description={
              <div style={{ maxWidth: 320 }}>
                会<b>一并永久删除</b>它名下的全部数据：
                <ul style={{ margin: '6px 0 0 16px', padding: 0 }}>
                  <li>修复范围白名单</li>
                  <li>角色授权（谁能看 / 谁能批）</li>
                  <li><b>全部修复动作记录</b>——谁在什么时候批准了什么，会一起消失</li>
                  <li><b>全部分析摘要与依据引用</b></li>
                  <li>握手/会话令牌</li>
                </ul>
                这些属于审计材料，删了无法恢复。
              </div>
            }
            okText="确认删除" cancelText="取消" okButtonProps={{ danger: true }}
            onConfirm={() => handleDelete(row)}
          >
            <Button size="small" danger icon={<Trash2 size={14} />} loading={deleting === row.connection_id}>
              删除
            </Button>
          </Popconfirm>
        </Space>
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


// ============================================================ 服务健康阈值

const THRESHOLD_FIELDS = [
  { name: 'error_rate_warning', label: '错误率 · 观察中起点', step: 0.001, suffix: '（0.01 = 1%）' },
  { name: 'error_rate_critical', label: '错误率 · 异常起点', step: 0.001, suffix: '（0.05 = 5%）' },
  { name: 'p95_warning_ms', label: 'P95 延迟 · 观察中起点', step: 50, suffix: 'ms' },
  { name: 'p95_critical_ms', label: 'P95 延迟 · 异常起点', step: 50, suffix: 'ms' },
  { name: 'queue_warning_ms', label: '队列延迟 · 观察中起点', step: 500, suffix: 'ms' },
  { name: 'queue_critical_ms', label: '队列延迟 · 异常起点', step: 500, suffix: 'ms' },
]

function ThresholdsSection({ connectors, onModuleDisabled }) {
  const [connectionId, setConnectionId] = useState(null)
  const [rows, setRows] = useState([])
  const [service, setService] = useState('*')
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!connectionId && connectors.length) setConnectionId(connectors[0].connection_id)
  }, [connectors, connectionId])

  const load = useCallback(async () => {
    if (!connectionId) return
    try {
      setRows(await opsApi.listServiceThresholds(connectionId))
    } catch (error) {
      if (opsApi.isModuleDisabledError(error)) { onModuleDisabled(); return }
      message.error(opsApi.errorText(error))
    }
  }, [connectionId, onModuleDisabled])

  useEffect(() => { load() }, [load])

  const current = rows.find((r) => r.service === service)
  useEffect(() => {
    // 只回填**管理员实际填过**的字段，没填的留空 —— 留空表示"跟随上一层"。
    // 把生效值预填进去会让用户一保存就把六个数字全冻结成当天的值，
    // 以后平台默认值改了它们也不会跟着动，而用户完全不知道自己做了这件事。
    form.setFieldsValue(
      THRESHOLD_FIELDS.reduce((acc, f) => ({ ...acc, [f.name]: current?.thresholds?.[f.name] ?? null }), {}),
    )
  }, [current, form])

  async function handleSave() {
    const values = await form.validateFields()
    const filled = Object.fromEntries(
      Object.entries(values).filter(([, v]) => v !== null && v !== undefined && v !== ''),
    )
    if (!Object.keys(filled).length) {
      message.warning('一个字段都没填 —— 想恢复默认请用下面的「恢复默认」')
      return
    }
    setSaving(true)
    try {
      await opsApi.setServiceThresholds(connectionId, service.trim(), filled)
      message.success('阈值已保存')
      load()
    } catch (error) {
      // 400 = 阈值本身不合法（写错字段名 / 非正数 / warning 大于 critical）。
      // 后端当场报错而不是夹紧成一个看起来正常的值，这里如实转述给用户。
      message.error(opsApi.errorText(error))
    } finally {
      setSaving(false)
    }
  }

  async function handleReset() {
    try {
      await opsApi.deleteServiceThresholds(connectionId, service.trim())
      message.success('已恢复默认')
      load()
    } catch (error) {
      message.error(opsApi.errorText(error))
    }
  }

  return (
    <Card size="small" title="服务健康判定阈值" style={{ marginTop: 12 }}>
      <Alert
        type="info"
        showIcon
        className="ops-console-alert"
        message="留空 = 跟随上一层，不是 0"
        description="解析顺序是逐字段回退：这个服务的配置 → 该连接器的默认（服务名填 *）→ 平台内置默认值。只想改一个数就只填那一个，其余留空 —— 全填等于把没打算改的那几个也冻结在今天的值上。"
      />
      <Space className="ops-console-toolbar" wrap>
        <Select
          value={connectionId}
          onChange={setConnectionId}
          style={{ minWidth: 220 }}
          options={connectors.map((c) => ({ value: c.connection_id, label: c.name }))}
        />
        <Input
          value={service}
          onChange={(e) => setService(e.target.value)}
          style={{ width: 220 }}
          placeholder="服务名，* 表示该连接器默认"
        />
      </Space>

      <Form form={form} layout="vertical" className="ops-console-form">
        {THRESHOLD_FIELDS.map((f) => (
          <Form.Item key={f.name} name={f.name} label={`${f.label} ${f.suffix}`}>
            <InputNumber min={0} step={f.step} style={{ width: '100%' }} placeholder="留空 = 跟随上一层" />
          </Form.Item>
        ))}
        <Space>
          <Button type="primary" loading={saving} onClick={handleSave}>保存阈值</Button>
          {current && <Button danger onClick={handleReset}>恢复默认</Button>}
        </Space>
      </Form>

      {current && (
        <Paragraph type="secondary" className="ops-console-hint">
          实际生效：{THRESHOLD_FIELDS.map((f) => `${f.label.split(' · ')[0]}${f.label.includes('观察中') ? '观察' : '异常'} ${current.effective[f.name]}`).join('　')}
        </Paragraph>
      )}
      {rows.length > 0 && (
        <Paragraph type="secondary" className="ops-console-hint">
          已配置：{rows.map((r) => (r.service === '*' ? '连接器默认' : r.service)).join('、')}
        </Paragraph>
      )}
    </Card>
  )
}

// ============================================================ 修复范围白名单

function ScopesSection({ connectors, onModuleDisabled }) {
  // ⚠️ 用共享的 `useUserNames`/`displayUser`，**不要在这里另写一份**。
  // 总览/审批队列/事后复盘三处早前已经统一过 UUID→用户名，这里是第四处漏网：
  // 界面上显示的是 `5a5cb2cb-a870-...` 而不是人名。各写各的正是当初要消灭的模式。
  const userNames = useUserNames()
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
            　当前配置由 {displayUser(userNames, existing.configured_by)} 于 {fmtTime(existing.updated_at)} 更新
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
  const [marking, setMarking] = useState('')
  const userNames = useUserNames()
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

  /** 事后标注"这次修复到底有没有解决问题"。
   *
   * §10.5 四个指标里唯一需要人工输入的一项——其余三个都能从状态机自己算出来。
   * 没有它，「执行成功」只代表命令跑通了，不代表问题解决了：服务重启成功、
   * 错误率照旧，在状态机看来仍然是 completed。
   *
   * 允许改主意（后端 set_outcome_effective 对终态之后不做状态限制，是有意的）：
   * 事后复盘经常会推翻当时的判断。
   */
  async function mark(actionId, effective) {
    setMarking(actionId)
    try {
      await opsApi.setActionOutcome(actionId, effective)
      message.success(effective ? '已标注为有效' : '已标注为无效')
      load()
    } catch (error) {
      message.error(opsApi.errorText(error))
    } finally {
      setMarking('')
    }
  }

  async function act(actionId, kind) {
    setActing(actionId)
    try {
      if (kind === 'approve') await opsApi.approveRemediationAction(actionId)
      else if (kind === 'execute') await opsApi.executeRemediationAction(actionId)
      else await opsApi.rejectRemediationAction(actionId)
      message.success({ approve: '已批准', execute: '已下发执行', reject: '已拒绝' }[kind])
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
          {/* 原来这里是 JSON.stringify 直出。改成人话，但**不丢字段**——
              认不出的键原样列成 key=value（见 formatPlan）：修复动作的参数
              决定它在生产环境做什么，为了排版好看藏掉一个没预料到的字段，
              是这类界面最不该犯的错。 */}
          <div className="ops-console-plan">{formatPlan(plan)}</div>
        </div>
      ),
    },
    { title: '影响范围', dataIndex: 'impact_radius', render: (v) => v || '—' },
    { title: '提议人', dataIndex: 'proposed_by', render: (v) => displayUser(userNames, v) },
    {
      title: '状态',
      dataIndex: 'status',
      render: (s, row) => {
        // ⚠️ **失败必须说清为什么失败。**
        // 之前这里只有一个红色的"执行失败"标签，探针回的失败原因
        //（`result.detail`，比如"目标进程无响应，操作超时"）**在界面上任何地方
        // 都看不到**——运维人员只知道失败了，得去翻后端日志才知道发生了什么，
        // 而这正是这个控制台本该替他做的事。成功时的详情同样显示：
        // "重启了但健康检查没过"和"重启后一切正常"对下一步决策完全不同。
        const detail = (row.result || {}).detail
        return (
          <Space direction="vertical" size={2}>
            <StatusTag status={s} />
            {detail && (
              <Text type={s === 'failed' ? 'danger' : 'secondary'} style={{ fontSize: 11 }}>
                {detail}
              </Text>
            )}
          </Space>
        )
      },
    },
    { title: '提议时间', dataIndex: 'created_at', render: fmtTime },
    {
      title: '事后有效性',
      width: 150,
      render: (_, row) => {
        // 只有跑完了才谈得上"有没有效"——待审批/执行中的动作还没有结果可评。
        if (!['completed', 'failed'].includes(row.status)) return <Text type="secondary">—</Text>
        if (row.outcome_effective === true) return <Tag color="green">已标注有效</Tag>
        if (row.outcome_effective === false) return <Tag color="red">已标注无效</Tag>
        return (
          <Space size={4}>
            <Button size="small" loading={marking === row.action_id}
                    onClick={() => mark(row.action_id, true)}>有效</Button>
            <Button size="small" danger loading={marking === row.action_id}
                    onClick={() => mark(row.action_id, false)}>无效</Button>
          </Space>
        )
      },
    },
    {
      title: '审批',
      render: (_, row) => {
        // 已批准但还没执行的，这里给出真正的下发入口。
        // **不做成"批准即执行"**：批准是授权决定，执行是动作本身，把两者合成
        // 一步会让"我只是想先授权、等窗口期再动手"变得不可能。
        if (row.status === 'approved') {
          return (
            <Space>
              <Popconfirm
                title="现在就在客户环境里执行这个动作？"
                description="这一步会真正下发到探针/连接器并立即执行，不是模拟。执行前会再查一次目标是否仍在允许范围内。"
                okText="确认执行"
                okButtonProps={{ danger: true }}
                cancelText="再想想"
                onConfirm={() => act(row.action_id, 'execute')}
              >
                <Button size="small" danger loading={acting === row.action_id}>执行</Button>
              </Popconfirm>
              <Text type="secondary">
                {displayUser(userNames, row.approver_user_id)} 已批准
              </Text>
            </Space>
          )
        }
        if (row.status !== 'pending_approval') {
          return row.approver_user_id
            ? <Text type="secondary">{displayUser(userNames, row.approver_user_id)} · {fmtTime(row.approved_at)}</Text>
            : <Text type="secondary">—</Text>
        }
        return (
          <Space>
            <Popconfirm
              title="确认批准这个修复动作？"
              description="批准 = 授予执行资格，本身不会立刻下发；批准之后这一列会出现「执行」按钮，由人决定何时下发。请先确认上面的动作参数。"
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
        message="批准 ≠ 已执行"
        description="批准只是授予执行资格，动作不会因为你点了批准就自动跑起来——真正下发是独立的一步。审批权限按角色授予（见「授权管理」），不是所有管理员默认都能批。"
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

// ============================================================ 授权管理

function PermissionsSection({ connectors, onModuleDisabled }) {
  const [connectionId, setConnectionId] = useState(null)
  const [roles, setRoles] = useState([])
  const [perms, setPerms] = useState({})   // role_id -> {can_view, can_approve}
  const [loading, setLoading] = useState(false)
  const [savingRole, setSavingRole] = useState('')

  useEffect(() => {
    if (!connectionId && connectors.length) setConnectionId(connectors[0].connection_id)
  }, [connectors, connectionId])

  const load = useCallback(async () => {
    if (!connectionId) return
    setLoading(true)
    try {
      const [roleList, permList] = await Promise.all([
        adminApi.listRoles(),
        opsApi.listConnectorPermissions(connectionId),
      ])
      // 只列本企业自建角色：系统角色（org_admin/super_admin 那几个）后端拒绝配置，
      // 列出来只会让人点了才发现不行。org_admin 的通配符是隐式的，不在这里配。
      setRoles(roleList.filter((r) => !r.is_system))
      setPerms(Object.fromEntries(
        permList.map((p) => [p.role_id, { can_view: p.can_view, can_approve: p.can_approve }])))
    } catch (error) {
      if (opsApi.isModuleDisabledError(error)) { onModuleDisabled(); return }
      message.error(opsApi.errorText(error))
    } finally {
      setLoading(false)
    }
  }, [connectionId, onModuleDisabled])

  useEffect(() => { load() }, [load])

  async function update(roleId, next) {
    setSavingRole(roleId)
    try {
      if (!next.can_view && !next.can_approve) {
        await opsApi.revokeRoleOpsPermission(roleId, connectionId)
      } else {
        await opsApi.setRoleOpsPermission(roleId, connectionId, next)
      }
      await load()
    } catch (error) {
      message.error(opsApi.errorText(error))
      await load()   // 失败时也重新拉，避免界面停在一个没真正生效的勾选状态
    } finally {
      setSavingRole('')
    }
  }

  if (!connectors.length) {
    return <Empty description="先登记至少一个连接器，才能给角色授权" />
  }

  const columns = [
    { title: '角色', dataIndex: 'display_name', render: (v, r) => v || r.name },
    {
      title: '能查看',
      width: 120,
      render: (_, role) => {
        const p = perms[role.role_id] || {}
        return (
          <Checkbox
            checked={!!p.can_view}
            // 能批准的人必然要能看见他在批什么，所以 approve 打开时 view 强制勾上
            // 且不可单独取消——把这条约束做进控件，而不是等用户取消后被后端悄悄改回来。
            disabled={!!p.can_approve || savingRole === role.role_id}
            onChange={(e) => update(role.role_id, { can_view: e.target.checked, can_approve: false })}
          />
        )
      },
    },
    {
      title: '能审批',
      width: 120,
      render: (_, role) => {
        const p = perms[role.role_id] || {}
        return (
          <Checkbox
            checked={!!p.can_approve}
            disabled={savingRole === role.role_id}
            onChange={(e) => update(role.role_id,
              { can_view: true, can_approve: e.target.checked })}
          />
        )
      },
    },
    {
      title: '',
      render: (_, role) => {
        const p = perms[role.role_id] || {}
        if (!p.can_view && !p.can_approve) return null
        return (
          <Popconfirm
            title="撤销这个角色对该连接器的全部权限？"
            okText="撤销" cancelText="取消"
            onConfirm={() => update(role.role_id, { can_view: false, can_approve: false })}
          >
            <Button size="small" danger type="text">撤销</Button>
          </Popconfirm>
        )
      },
    },
  ]

  return (
    <Card
      size="small"
      title="谁能查看 / 谁能审批"
      loading={loading}
      extra={
        <Select
          value={connectionId}
          onChange={setConnectionId}
          style={{ minWidth: 220 }}
          options={connectors.map((c) => ({ value: c.connection_id, label: c.name }))}
        />
      }
    >
      <Alert
        type="info"
        showIcon
        className="ops-console-alert"
        message="权限挂在角色上，不是挂在人上"
        description="给角色授权后，该角色的全部成员立即生效，不需要逐人配置、也不需要重新登录。企业管理员对本企业连接器天然拥有全部权限，不在这里配置；平台管理员不会自动获得任何企业的运维权限。"
      />
      <Table
        rowKey="role_id"
        size="small"
        columns={columns}
        dataSource={roles}
        pagination={false}
        locale={{ emptyText: <Empty description="本企业还没有自建角色。系统角色不能在这里授权。" /> }}
      />
    </Card>
  )
}


// ============================================================ 页面

// 设计稿只有三个 tab（总览/白名单配置/连接器管理），这里多留两个：
// - 「审批队列」：总览上的审批卡片只列**待审批**，历史（已完成/失败/已拒绝）在这里。
//   总览是大屏、看当下；队列是台账、看过去。砍掉它等于丢掉审批历史的入口。
// - 「授权管理」：设计稿制作时 role_ops_systems 还没落地，不是设计上不要。
const SECTIONS = [
  { value: 'overview', label: '总览', icon: LayoutDashboard, everyone: true },
  { value: 'approvals', label: '审批队列', icon: ClipboardCheck, everyone: true },
  // 「事后复盘」给所有能看的人，不只管理员：后端那个端点走的是
  // viewable_connection_ids_for_user（跟审批队列同一套 can_view），
  // 被授权的普通员工本来就能看到自己权限内的动作，复盘同理。
  { value: 'postmortems', label: '事后复盘', icon: History, everyone: true },
  { value: 'scopes', label: '策略配置', icon: ShieldCheck },
  { value: 'connectors', label: '连接器管理', icon: Plug },
  { value: 'permissions', label: '授权管理', icon: Users },
]

export default function OpsConsole({ canManage = true }) {
  // 白名单/连接器/授权三个分段是 org_admin 专属（后端也只对 org_admin 开放）。
  // 被授予 can_view/can_approve 的普通员工只看总览和审批队列——给他看点进去
  // 必然 403 的分段，跟"导航入口本身要按权限藏起来"是同一个道理。
  const sections = canManage ? SECTIONS : SECTIONS.filter((s) => s.everyone)
  const [section, setSection] = useState('overview')
  const [moduleDisabled, setModuleDisabled] = useState(false)
  const [connectors, setConnectors] = useState([])
  const [booting, setBooting] = useState(true)

  const onModuleDisabled = useCallback(() => setModuleDisabled(true), [])
  const onConnectorsLoaded = useCallback((list) => { setConnectors(list); setBooting(false) }, [])

  // 连接器列表在**控制台这一层**拉，不在「连接器管理」分段里拉——默认落在
  // 「总览」时那个分段根本不会挂载，而「白名单配置」「授权管理」都需要这份列表
  // 来渲染下拉框。放在分段里拉会让它们误显示"先登记至少一个连接器"，
  // 而实际上连接器是有的、只是没人去拉过。
  useEffect(() => {
    let cancelled = false
    if (!canManage) { setBooting(false); return undefined }
    opsApi.listConnectors()
      .then((list) => { if (!cancelled) { setConnectors(list); setBooting(false) } })
      .catch((error) => {
        if (cancelled) return
        // 这个请求同时充当"模块开没开"的探测：后端未开通时统一 403。
        if (opsApi.isModuleDisabledError(error)) setModuleDisabled(true)
        setBooting(false)
      })
    return () => { cancelled = true }
  }, [canManage])

  useEffect(() => {
    // 兜底：万一上面的请求既没成功也没失败（网络挂起），别让两个分段永远转圈。
    // ⚠️ 这是权宜之计：真正该做的是让导航入口本身按开关显示/隐藏，而不是让
    // 用户点进来才发现。做不了的原因是 aiops_module_enabled 目前**没有任何
    // GET 接口暴露**（OrganizationSummary / AdminOrganizationResponse 都没有
    // 这个字段），前端无从提前知道。已反馈给后端那条线，字段加上之后：
    // AdminPanel.jsx 里按 meProfile.organization.aiops_module_enabled 决定
    // 要不要 push 这个 tab，这里的探测逻辑可以保留当兜底。
    const t = setTimeout(() => setBooting(false), 8000)
    return () => clearTimeout(t)
  }, [])

  if (moduleDisabled) return <ModuleDisabled />

  return (
    // antd 组件的深色化走 darkAlgorithm，不手写覆盖 antd 内部类名——那种覆盖
    // 在版本升级时必然碎掉，而且弹窗/气泡这类挂在 portal 上的组件也盖不全
    // （ConfigProvider 的 context 能穿透 portal，CSS 选择器不能）。
    // token 取自 OpsConsole.css 里那套（设计稿的深色 NOC 配色），两边保持一致。
    <ConfigProvider
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorBgBase: '#12131a',
          colorPrimary: '#8b7ffb',
          colorSuccess: '#34d399',
          colorWarning: '#fbbf24',
          colorError: '#f8717a',
          borderRadius: 8,
        },
      }}
    >
    <div className="ops-console">
      <nav className="ops-tabnav" role="tablist" aria-label="运维塔台导航">
        {sections.map((s) => (
          <button
            key={s.value}
            role="tab"
            aria-selected={section === s.value}
            className={`ops-tabbtn ${section === s.value ? 'active' : ''}`}
            onClick={() => setSection(s.value)}
          >
            <s.icon size={14} />
            {s.label}
          </button>
        ))}
      </nav>

      {section === 'overview' && (
        <OpsOverview canManage={canManage} onModuleDisabled={onModuleDisabled} />
      )}

      {section === 'connectors' && canManage && (
        <ConnectorsSection onModuleDisabled={onModuleDisabled} onConnectorsLoaded={onConnectorsLoaded} />
      )}
      {section === 'scopes' && canManage && (
        booting ? <Spin /> : (
          <>
            <ScopesSection connectors={connectors} onModuleDisabled={onModuleDisabled} />
            <ThresholdsSection connectors={connectors} onModuleDisabled={onModuleDisabled} />
          </>
        )
      )}
      {section === 'approvals' && <ApprovalsSection onModuleDisabled={onModuleDisabled} />}
      {section === 'postmortems' && <OpsPostmortems onModuleDisabled={onModuleDisabled} />}
      {section === 'permissions' && canManage && (
        booting ? <Spin /> : <PermissionsSection connectors={connectors} onModuleDisabled={onModuleDisabled} />
      )}
    </div>
    </ConfigProvider>
  )
}
