import { useState } from 'react'
import { Modal, Button, Upload, Table, Alert, Typography, Space, Tag, message } from 'antd'
import { Upload as UploadIcon, Download, AlertTriangle } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 员工账号批量导入（docs/account_lifecycle_design.md §4.1）。
//
// 这个界面的形状由两条设计决定，改动前先看那两条：
//
// 1. **必须先预演。** 这是一个能一次创建上万账号的操作，没有预演等于没有
//    安全网。所以这里是**两步式**：选文件 -> 自动预演 -> 看报告 -> 才出现
//    「确认导入」按钮。不提供"跳过预演直接导"的入口。
//    后端 validate_only 默认也是 true，两层都兜着。
//
// 2. **CSV 里没有密码列。** 用户已定不做邮件短信（O-1），凭证分发只能是人工的，
//    所以让被分发的东西尽可能不值钱：导入后系统给出一次性激活码（7 天过期、
//    单次使用），而不是初始密码。
//    ⚠️ **激活码明文全系统只在导入响应里出现这一次**，库里只有哈希，
//    刷新页面就没了 —— 所以下面给了「下载」和「复制」，并且用红色警告写死
//    这件事。管理员关掉弹窗前没保存，只能把这些账号删了重导。
const TEMPLATE = 'username,role_name,display_name\nzhangsan,HR专员,张三\nlisi,IT运维,李四\n'

export default function BulkUserImport({ open, onClose, onImported }) {
  const [file, setFile] = useState(null)
  const [plan, setPlan] = useState(null)      // 预演结果
  const [result, setResult] = useState(null)  // 真跑结果（含激活码）
  const [loading, setLoading] = useState(false)

  function reset() {
    setFile(null); setPlan(null); setResult(null); setLoading(false)
  }

  function handleClose() {
    // 有激活码还没保存时，关闭是不可逆的——库里只有哈希，拿不回来了。
    if (result?.credentials?.length) {
      Modal.confirm({
        title: '激活码尚未保存',
        icon: <AlertTriangle size={20} color="#faad14" />,
        content: `本次生成的 ${result.credentials.length} 个激活码关闭后无法再次查看（系统只存哈希）。确定关闭吗？`,
        okText: '我已保存，关闭',
        okButtonProps: { danger: true },
        cancelText: '返回保存',
        onOk: () => { reset(); onClose() },
      })
      return
    }
    reset(); onClose()
  }

  async function runDryRun(selected) {
    setFile(selected); setResult(null); setLoading(true)
    try {
      setPlan(await adminApi.bulkImportUsers(selected, true))
    } catch (error) {
      message.error('预演失败: ' + (error.response?.data?.detail || error.message))
      setPlan(null)
    } finally {
      setLoading(false)
    }
  }

  async function runImport() {
    setLoading(true)
    try {
      const res = await adminApi.bulkImportUsers(file, false)
      setResult(res)
      message.success(`导入完成：新建 ${res.to_create}，更新 ${res.to_update}`)
      onImported?.()
    } catch (error) {
      message.error('导入失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  function download(text, filename) {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url; a.download = filename; a.click()
    URL.revokeObjectURL(url)
  }

  const credentialsCsv = () =>
    'username,activation_code,expires_at\n' +
    result.credentials
      .map((c) => `${c.username},${c.activation_code},${new Date(c.expires_at * 1000).toISOString()}`)
      .join('\n')

  return (
    <Modal
      title="批量导入员工账号"
      open={open}
      onCancel={handleClose}
      width={760}
      footer={null}
      destroyOnClose
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message="CSV 里不要放密码"
          description={
            <span>
              必填列 <code>username</code>、<code>role_name</code>，可选 <code>display_name</code>。
              含密码列的文件会被整份拒收——导入后系统为每个新账号生成一次性激活码
              （7 天过期、只能用一次），由你分发给员工，他们凭码自己设密码。
            </span>
          }
          action={
            <Button size="small" icon={<Download size={14} />} onClick={() => download(TEMPLATE, '员工导入模板.csv')}>
              下载模板
            </Button>
          }
        />

        {!result && (
          <Upload.Dragger
            accept=".csv"
            maxCount={1}
            showUploadList={!!file}
            beforeUpload={(f) => { runDryRun(f); return false }}
            onRemove={() => { setFile(null); setPlan(null) }}
          >
            <p style={{ margin: '12px 0' }}><UploadIcon size={28} /></p>
            <p>点击或拖拽 CSV 文件到这里</p>
            <p style={{ color: 'var(--text-tertiary, #999)', fontSize: 12 }}>
              选择后会先做一次预演，不会立刻写入任何数据
            </p>
          </Upload.Dragger>
        )}

        {plan && !result && (
          <>
            {plan.fatal_error && <Alert type="error" showIcon message="整份文件未通过校验" description={plan.fatal_error} />}
            {!plan.fatal_error && (
              <Alert
                type={plan.seat_ok ? (plan.errors.length ? 'warning' : 'success') : 'error'}
                showIcon
                message="预演结果（尚未写入）"
                description={<pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>{plan.summary}</pre>}
              />
            )}
            {plan.errors.length > 0 && (
              <Table
                size="small"
                rowKey={(r) => `${r.line_no}-${r.username}`}
                dataSource={plan.errors}
                pagination={{ pageSize: 8, size: 'small' }}
                columns={[
                  { title: '行号', dataIndex: 'line_no', width: 70 },
                  { title: '用户名', dataIndex: 'username', width: 160 },
                  { title: '问题', dataIndex: 'reason' },
                ]}
              />
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <Button onClick={() => { setFile(null); setPlan(null) }}>重新选择文件</Button>
              <Button
                type="primary"
                loading={loading}
                // 有致命错误或席位不足时不给真跑的入口——不是禁用一个"其实点了
                // 也会失败"的按钮，而是这一步本来就没有可执行的东西。
                disabled={!!plan.fatal_error || !plan.seat_ok || plan.to_create + plan.to_update === 0}
                onClick={runImport}
              >
                确认导入（新建 {plan.to_create}，更新 {plan.to_update}）
              </Button>
            </div>
          </>
        )}

        {result && (
          <>
            <Alert type="success" showIcon message="导入完成" description={result.summary} />
            {result.credentials.length > 0 && (
              <>
                <Alert
                  type="error"
                  showIcon
                  message="请立即保存激活码——关闭后无法再次查看"
                  description="系统只保存激活码的哈希值，这个列表刷新或关闭后就拿不回来了。未保存的话，只能把这些账号删掉重新导入。"
                  action={
                    <Button size="small" danger icon={<Download size={14} />}
                            onClick={() => download(credentialsCsv(), '激活码.csv')}>
                      下载
                    </Button>
                  }
                />
                <Table
                  size="small"
                  rowKey="username"
                  dataSource={result.credentials}
                  pagination={{ pageSize: 10, size: 'small' }}
                  columns={[
                    { title: '用户名', dataIndex: 'username', width: 180 },
                    {
                      title: '激活码',
                      dataIndex: 'activation_code',
                      render: (code) => <Typography.Text copyable code>{code}</Typography.Text>,
                    },
                    {
                      title: '有效期至',
                      dataIndex: 'expires_at',
                      width: 180,
                      render: (t) => new Date(t * 1000).toLocaleString(),
                    },
                  ]}
                />
              </>
            )}
            {result.errors.length > 0 && (
              <Alert type="warning" showIcon
                     message={`${result.errors.length} 行未导入`}
                     description={result.errors.map((e) => `第 ${e.line_no} 行（${e.username}）：${e.reason}`).join('\n')}
                     style={{ whiteSpace: 'pre-wrap' }} />
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <Button onClick={handleClose}>完成</Button>
            </div>
          </>
        )}
      </Space>
    </Modal>
  )
}
