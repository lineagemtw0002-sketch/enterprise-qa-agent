import { useState } from 'react'
import { message } from 'antd'
import { KeyRound, Lock, UserRound } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 账号激活（docs/account_lifecycle_design.md §4.1b）。
//
// 为什么它长在登录页里：调用它的人正是"还没有密码所以登不进来"的那个员工。
// 放在任何需要登录的地方都是死循环。这也是全系统唯一一个不带鉴权的写端点
// （设计 §6 风险 R-4）。
//
// ⚠️ **失败提示必须原样透传后端那一句，不要在前端按情况细化。**
// 后端把"用户不存在 / 码不对 / 已过期 / 已激活"四种失败塌缩成同一句话，
// 就是为了不让人拿一份姓名表在这里探测某家企业有哪些员工。前端如果好心加一句
// "该用户名不存在，请检查"，后端那层防护就白做了。
export default function ActivateAccountForm({ onDone, onCancel }) {
  const [form, setForm] = useState({ username: '', activation_code: '', new_password: '', confirm: '' })
  const [loading, setLoading] = useState(false)

  function update(key, value) {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  async function submit() {
    if (!form.username.trim() || !form.activation_code.trim()) {
      message.warning('请填写用户名和激活码')
      return
    }
    if (form.new_password.length < 6) {
      message.warning('密码至少 6 位')
      return
    }
    // 两次输入一致只在前端校验——它防的是手滑，不是攻击。后端没有这个概念
    // （它只收一个 new_password），所以这里不能指望后端兜底。
    if (form.new_password !== form.confirm) {
      message.warning('两次输入的密码不一致')
      return
    }
    setLoading(true)
    try {
      await adminApi.activateAccount({
        username: form.username.trim(),
        activation_code: form.activation_code.trim(),
        new_password: form.new_password,
      })
      message.success('激活成功，请用新密码登录')
      onDone?.(form.username.trim())
    } catch (error) {
      message.error(error.response?.data?.detail || error.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <form className="login-form" onSubmit={(e) => { e.preventDefault(); submit() }}>
      <p style={{ margin: '0 0 4px', fontSize: 13, color: 'var(--text-tertiary, #888)' }}>
        管理员会给你一个一次性激活码（7 天内有效，只能用一次）。
      </p>
      <div className="login-input-wrap">
        <UserRound size={16} className="login-input-icon" />
        <input
          type="text"
          placeholder="用户名"
          value={form.username}
          onChange={(e) => update('username', e.target.value)}
        />
      </div>
      <div className="login-input-wrap">
        <KeyRound size={16} className="login-input-icon" />
        <input
          type="text"
          placeholder="激活码"
          value={form.activation_code}
          onChange={(e) => update('activation_code', e.target.value)}
        />
      </div>
      <div className="login-input-wrap">
        <Lock size={16} className="login-input-icon" />
        <input
          type="password"
          placeholder="设置密码（至少 6 位）"
          value={form.new_password}
          onChange={(e) => update('new_password', e.target.value)}
        />
      </div>
      <div className="login-input-wrap">
        <Lock size={16} className="login-input-icon" />
        <input
          type="password"
          placeholder="再输一次"
          value={form.confirm}
          onChange={(e) => update('confirm', e.target.value)}
        />
      </div>
      <button type="submit" className="login-submit-btn" disabled={loading}>
        {loading ? '激活中…' : '激活并设置密码'}
      </button>
      <button
        type="button"
        onClick={onCancel}
        style={{
          background: 'none', border: 'none', cursor: 'pointer', marginTop: 4,
          color: 'var(--accent)', fontSize: 13,
        }}
      >
        返回登录
      </button>
    </form>
  )
}
