import { useEffect, useState } from 'react'
import * as adminApi from '../../api/admin.js'

// 运维塔台三个视图（总览 / 审批队列 / 事后复盘）共用的展示辅助。
//
// 抽出来的直接原因：user_id → 用户名 这段逻辑原来只在 OpsOverview 里有一份，
// 另外两处还在直出 36 位 UUID。**与其复制第三遍，不如抽一次**——三处显示同一种
// 东西却各写各的，早晚会出现"总览显示用户名、复盘显示 UUID"这种不一致，
// 而这种不一致没有任何测试会发现。

/** user_id → username 的映射。
 *
 * ⚠️ **拉不到就退回短 id，不是报错**：`listUsers()` 是管理员端点，被授予
 * can_view 的普通员工调它会 403。对他们来说"看不到用户名"只是显示降级，
 * 不该让整个面板不可用。
 */
export function useUserNames(enabled = true) {
  const [names, setNames] = useState({})

  useEffect(() => {
    if (!enabled) return undefined
    let cancelled = false
    adminApi.listUsers()
      .then((list) => {
        if (!cancelled) setNames(Object.fromEntries(list.map((u) => [u.user_id, u.username])))
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [enabled])

  return names
}

/** 把 user_id 渲染成人能认的东西。查不到时给短 id 而不是整串 UUID——
 *  短 id 至少能用来跟日志/审计记录对照，整串只是占地方。 */
export function displayUser(names, userId) {
  if (!userId) return '—'
  return names[userId] || `${String(userId).slice(0, 8)}…`
}

const PLAN_FIELD_LABELS = {
  target: '目标',
  target_instances: '目标实例数',
  baseline_instances: '基线实例数',
  path: '路径',
  versions_back: '回滚版本数',
}

/** 把 plan 渲染成人话，而不是把整个 JSON 甩到表格里。
 *
 * `action_type` 不显示——它已经在旁边的标签上了，重复一遍只会挤占宽度。
 * 认不出的字段原样列出（key=value），**不丢弃**：修复动作的参数决定了它会在
 * 生产环境做什么，为了排版好看而藏掉一个没预料到的字段，是这类界面最不该犯的错。
 */
export function formatPlan(plan) {
  if (!plan || typeof plan !== 'object') return '—'
  const parts = []
  for (const [key, value] of Object.entries(plan)) {
    if (key === 'action_type') continue
    const label = PLAN_FIELD_LABELS[key] || key
    parts.push(`${label} ${typeof value === 'object' ? JSON.stringify(value) : value}`)
  }
  return parts.length ? parts.join(' · ') : '—'
}

/** 执行结果：优先显示 detail 这段人话，没有才退回紧凑 JSON。 */
export function formatResult(result) {
  if (!result || typeof result !== 'object') return null
  if (result.detail) return String(result.detail)
  if (result.error) return String(result.error)
  const compact = JSON.stringify(result)
  return compact === '{}' ? null : compact
}
