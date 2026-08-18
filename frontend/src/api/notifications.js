import axios from 'axios'

const BASE = '/api/v1/notifications'

export function listNotifications({ unreadOnly = false, limit = 20, offset = 0 } = {}) {
  return axios
    .get(BASE, { params: { unread_only: unreadOnly, limit, offset } })
    .then((res) => res.data)
}

export function unreadNotificationCount() {
  return axios.get(`${BASE}/unread-count`).then((res) => res.data.count)
}

export function markNotificationRead(notificationId) {
  return axios.post(`${BASE}/${notificationId}/read`).then((res) => res.data)
}

export function markAllNotificationsRead() {
  return axios.post(`${BASE}/mark-all-read`).then((res) => res.data)
}
