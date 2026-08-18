import { useCallback, useEffect, useState } from 'react'
import { Popover, Badge, Empty, Spin } from 'antd'
import { Bell } from 'lucide-react'
import * as notificationsApi from '../../api/notifications.js'
import './NotificationBell.css'

// 铃铛入口 + 下拉列表（work-flow-web.md 第 6.3 节）。v1 用轮询而不是推送——
// 未读数每 30 秒重新拉一次，不新开一条 WebSocket 通道（避免跟现有
// /ws/trace/{conversation_id} 的单一职责混在一起，见 work-flow-web.md 6.3 节
// 最后一段的取舍说明）。
export default function NotificationBell({ onNavigate }) {
  const [unreadCount, setUnreadCount] = useState(0)
  const [notifications, setNotifications] = useState([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)

  const refreshUnread = useCallback(() => {
    notificationsApi.unreadNotificationCount().then(setUnreadCount).catch(() => {})
  }, [])

  useEffect(() => {
    refreshUnread()
    const timer = setInterval(refreshUnread, 30000)
    return () => clearInterval(timer)
  }, [refreshUnread])

  async function handleOpenChange(next) {
    setOpen(next)
    if (!next) return
    setLoading(true)
    try {
      const data = await notificationsApi.listNotifications({ limit: 10 })
      setNotifications(data)
    } catch {
      // 静默失败，下拉框展示空态即可，不打断使用
    } finally {
      setLoading(false)
    }
  }

  function handleClickNotification(n) {
    if (!n.is_read) {
      setUnreadCount((c) => Math.max(c - 1, 0))
      notificationsApi.markNotificationRead(n.notification_id).catch(() => {})
    }
    setOpen(false)
    if (n.link) onNavigate?.(n.link)
  }

  const content = (
    <div className="notification-dropdown">
      {loading ? (
        <div style={{ textAlign: 'center', padding: 20 }}><Spin size="small" /></div>
      ) : notifications.length === 0 ? (
        <Empty description="暂无通知" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        notifications.map((n) => (
          <div
            key={n.notification_id}
            className={`notification-item ${n.is_read ? '' : 'notification-item--unread'}`}
            onClick={() => handleClickNotification(n)}
          >
            <div className="notification-item-title">{n.title}</div>
            <div className="notification-item-body">{n.body}</div>
            <div className="notification-item-time">{new Date(n.created_at * 1000).toLocaleString('zh-CN')}</div>
          </div>
        ))
      )}
    </div>
  )

  return (
    <Popover
      content={content}
      title="通知"
      trigger="click"
      open={open}
      onOpenChange={handleOpenChange}
      placement="bottomRight"
      overlayClassName="notification-popover"
    >
      <button type="button" className="header-btn" title="通知">
        <Badge count={unreadCount} size="small" offset={[-2, 2]}>
          <Bell size={16} />
        </Badge>
      </button>
    </Popover>
  )
}
