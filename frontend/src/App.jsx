import { useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'
import { message, Modal, Drawer } from 'antd'
import {
  Sparkles, Plus, History, ChevronDown, RefreshCw, MessageCircle, MessageSquare, Trash2,
  Files as FilesIcon, UploadCloud, Box, FileText, BarChart2, LayoutDashboard, Link as LinkIcon,
  Activity, Settings, User, Bot, Send, XCircle, Info, Lock, UserRound,
} from 'lucide-react'
import AppShell from './components/shell/AppShell.jsx'
import TracePanel from './components/TracePanel.jsx'
import AdminPanel from './components/admin/AdminPanel.jsx'
import WorkflowPanel from './components/workflow/WorkflowPanel.jsx'
import WorkflowStatusPill from './components/workflow/WorkflowStatusPill.jsx'
import OpsPlaceholder from './components/ops/OpsPlaceholder.jsx'
import './App.css'

const ADMIN_ROLE_NAMES = new Set(['admin', 'super_admin'])

// 页面刷新时（authToken 的初始值来自 localStorage）要在任何组件挂载之前就把
// token 挂到 axios 默认头上——如果指望下面组件里的 useEffect 去做这件事，
// NotificationBell 这类"一挂载就发请求"的子组件的 effect
// 会比 App 自己那个设置 Authorization header 的 effect 先跑（React 里子组件
// 的 effect 先于父组件的 effect 提交），第一个请求会因为没带 token 收到 401，
// 401 拦截器又会立刻把人登出——看起来就是"登录成功了但进不去"。
const _storedToken = localStorage.getItem('ragent_token')
if (_storedToken) {
  axios.defaults.headers.common['Authorization'] = `Bearer ${_storedToken}`
}

const DEFAULT_SETTINGS = {
  apiBase: '/api/v1', // 相对路径，让 Vite 代理生效
  topK: 5,
  streaming: true, // 默认开启真流式
  model: 'qwen3.5-omni-flash',
}

const QUICK_ACTIONS = ['总结一下这份文档', '文档的主要观点是什么？', '找出关键数据', '翻译这段内容']

const FILE_ICONS = {
  pdf: FileText, doc: FileText, docx: FileText,
  txt: FileText, md: FileText,
  csv: BarChart2, xlsx: BarChart2, xls: BarChart2,
  pptx: LayoutDashboard,
  html: LinkIcon, htm: LinkIcon,
  json: FileText, yaml: FileText, yml: FileText,
}

const STATUS_LABEL = { pending: '等待中', ingesting: '处理中', ready: '就绪', error: '错误' }
const STATUS_CLASS = { pending: 'warning', ingesting: 'primary', ready: 'success', error: 'danger' }

function loadStoredSettings() {
  const saved = localStorage.getItem('ragAgentSettings')
  if (!saved) return { ...DEFAULT_SETTINGS }
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(saved) }
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

function confirmDialog(content, title = '提示') {
  return new Promise((resolve) => {
    Modal.confirm({
      title,
      content,
      okText: '确定',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

function formatTime(date) {
  return new Date(date).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function formatFileSize(bytes) {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

function getFileIcon(filename) {
  const ext = filename.split('.').pop().toLowerCase()
  return FILE_ICONS[ext] || FileText
}

function renderMarkdown(text) {
  return (text || '')
    .replace(/\n/g, '<br>')
    .replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
}

export default function App() {
  // ==================== 登录 / 鉴权 ====================
  const [authToken, setAuthToken] = useState(localStorage.getItem('ragent_token') || '')
  const [currentUsername, setCurrentUsername] = useState(localStorage.getItem('ragent_username') || '')
  // 本地开发方便：默认填好测试账号，省得每次手动输入
  const [loginForm, setLoginForm] = useState({ username: 'alice', password: 'alice123' })
  const [loginLoading, setLoginLoading] = useState(false)

  // ==================== 个人信息 / 头像 ====================
  const [meProfile, setMeProfile] = useState(null)
  const [view, setView] = useState('chat') // 'chat' | 'admin' | 'workflow'
  const isAdmin = meProfile?.roles?.some((r) => ADMIN_ROLE_NAMES.has(r.name)) ?? false

  // ==================== 工作流 ====================
  // 工作流只靠自然语言触发（说"我想请假"这类），不再提供手动选类型的入口；
  // activeWorkflow：后端每轮聊天响应里如实上报的"是否还在多轮收集中"，驱动
  // chat-header 的常驻状态胶囊（work-flow-web.md 5 节）。
  const [activeWorkflow, setActiveWorkflow] = useState(null)
  const [workflowDeepLinkId, setWorkflowDeepLinkId] = useState(null)
  const [changePasswordVisible, setChangePasswordVisible] = useState(false)
  const [changePasswordLoading, setChangePasswordLoading] = useState(false)
  const [changePasswordForm, setChangePasswordForm] = useState({ old_password: '', new_password: '', confirm_password: '' })

  const [filesDrawerVisible, setFilesDrawerVisible] = useState(false)

  const [messages, setMessages] = useState([])
  const [inputMessage, setInputMessage] = useState('')
  const [isTyping, setIsTyping] = useState(false)
  const [conversationId, setConversationId] = useState(null)
  const [files, setFiles] = useState([])
  const [settingsVisible, setSettingsVisible] = useState(false)
  const [conversations, setConversations] = useState([])
  const [isHistoryCollapsed, setIsHistoryCollapsed] = useState(true)
  const [settings, setSettings] = useState(DEFAULT_SETTINGS)
  const [traceEvents, setTraceEvents] = useState([])
  const [showTracePanel, setShowTracePanel] = useState(true)

  const messagesContainerRef = useRef(null)
  const fileInputRef = useRef(null)
  const abortControllerRef = useRef(null)
  const traceWsRef = useRef(null)
  const conversationIdRef = useRef(null)
  const settingsRef = useRef(settings)
  const authTokenRef = useRef(authToken)
  const logoutRef = useRef(() => {})

  useEffect(() => { conversationIdRef.current = conversationId }, [conversationId])
  useEffect(() => { settingsRef.current = settings }, [settings])
  useEffect(() => { authTokenRef.current = authToken }, [authToken])

  // axios 全局带上 token；token 变化（登录/登出）时同步更新
  useEffect(() => {
    if (authToken) {
      axios.defaults.headers.common['Authorization'] = `Bearer ${authToken}`
    } else {
      delete axios.defaults.headers.common['Authorization']
    }
  }, [authToken])

  // token 过期/失效时后端返回 401，统一在这里处理，不用每个调用点各自判断
  useEffect(() => {
    const interceptorId = axios.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response?.status === 401) {
          logoutRef.current()
        }
        return Promise.reject(error)
      }
    )
    return () => axios.interceptors.response.eject(interceptorId)
  }, [])

  // WorkflowDetailDrawer 里"在原对话中查看"链接跳回聊天用（跨组件树，用一个
  // 轻量的 DOM 事件桥接，不为这一个链接把 switchConversation 整条链路往下传）
  useEffect(() => {
    function handleOpenConversation(e) {
      const convId = e.detail?.conversationId
      if (!convId) return
      setView('chat')
      switchConversation(convId)
    }
    window.addEventListener('ragent:open-conversation', handleOpenConversation)
    return () => window.removeEventListener('ragent:open-conversation', handleOpenConversation)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 按轮次（turn）分组消息，用于 checkpoint 时间线
  const messageTurns = useMemo(() => {
    const turns = []
    let currentTurn = null
    for (const msg of messages) {
      if (msg.role === 'user') {
        currentTurn = { user: msg, messages: [msg] }
        turns.push(currentTurn)
      } else if (currentTurn) {
        currentTurn.messages.push(msg)
        if (msg.role === 'assistant') currentTurn.assistant = msg
      } else {
        turns.push({ user: null, assistant: msg, messages: [msg] })
      }
    }
    return turns
  }, [messages])

  function scrollToBottom() {
    requestAnimationFrame(() => {
      if (messagesContainerRef.current) {
        messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight
      }
    })
  }

  useEffect(() => { scrollToBottom() }, [messages])

  useEffect(() => {
    const merged = loadStoredSettings()
    setSettings(merged)
    settingsRef.current = merged

    return () => disconnectTraceWs()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 登录成功（或带着已保存的 token 刷新页面）之后才去加载账号相关的数据
  useEffect(() => {
    if (!authToken) return
    loadMe()
    loadConversations()

    const savedConvId = localStorage.getItem('currentConversationId')
    if (savedConvId) {
      setConversationId(savedConvId)
      conversationIdRef.current = savedConvId
      loadHistory(savedConvId)
      loadFiles(savedConvId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authToken])

  // ==================== 登录 / 登出 ====================
  async function login() {
    if (!loginForm.username || !loginForm.password) {
      message.warning('请输入用户名和密码')
      return
    }
    setLoginLoading(true)
    try {
      const response = await axios.post(`${settingsRef.current.apiBase}/auth/login`, {
        username: loginForm.username,
        password: loginForm.password,
      })
      const token = response.data.access_token
      const username = response.data.username
      localStorage.setItem('ragent_token', token)
      localStorage.setItem('ragent_username', username)
      // 同步设置，不等下面的 useEffect——登录成功这一刻马上要渲染带
      // NotificationBell 的主界面，这个子组件挂载时就会
      // 发请求，必须保证这时候 header 已经带上 token 了（见顶部注释）。
      axios.defaults.headers.common['Authorization'] = `Bearer ${token}`
      setAuthToken(token)
      setCurrentUsername(username)
      setLoginForm((prev) => ({ ...prev, password: '' }))
      message.success('登录成功')
    } catch (error) {
      message.error('登录失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoginLoading(false)
    }
  }

  function logout() {
    disconnectTraceWs()
    setTraceEvents([])
    setAuthToken('')
    setCurrentUsername('')
    setMeProfile(null)
    localStorage.removeItem('ragent_token')
    localStorage.removeItem('ragent_username')
    localStorage.removeItem('currentConversationId')
    setConversations([])
    setMessages([])
    setFiles([])
    setConversationId(null)
    conversationIdRef.current = null
    setActiveWorkflow(null)
    setWorkflowDeepLinkId(null)
    setView('chat')
  }
  useEffect(() => { logoutRef.current = logout })

  // ==================== 个人信息 / 修改密码 ====================
  async function loadMe() {
    try {
      const response = await axios.get(`${settingsRef.current.apiBase}/auth/me`)
      setMeProfile(response.data)
    } catch (error) {
      console.error('加载个人信息失败:', error)
    }
  }

  function openChangePassword() {
    setChangePasswordForm({ old_password: '', new_password: '', confirm_password: '' })
    setChangePasswordVisible(true)
  }

  async function submitChangePassword() {
    if (!changePasswordForm.old_password || !changePasswordForm.new_password) {
      message.warning('请填写完整')
      return
    }
    if (changePasswordForm.new_password.length < 6) {
      message.warning('新密码至少 6 位')
      return
    }
    if (changePasswordForm.new_password !== changePasswordForm.confirm_password) {
      message.warning('两次输入的新密码不一致')
      return
    }
    setChangePasswordLoading(true)
    try {
      await axios.post(`${settingsRef.current.apiBase}/auth/change-password`, {
        old_password: changePasswordForm.old_password,
        new_password: changePasswordForm.new_password,
      })
      message.success('密码修改成功')
      setChangePasswordVisible(false)
    } catch (error) {
      message.error('修改失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setChangePasswordLoading(false)
    }
  }

  // ==================== 对话列表 ====================
  async function loadConversations() {
    try {
      const response = await axios.get(`${settingsRef.current.apiBase}/conversations`)
      setConversations(response.data.conversations || [])
    } catch (error) {
      console.error('加载对话列表失败:', error)
    }
  }

  async function refreshConversations() {
    await loadConversations()
    message.success('已刷新')
  }

  async function switchConversation(convId) {
    if (convId === conversationIdRef.current) return
    disconnectTraceWs()
    setConversationId(convId)
    conversationIdRef.current = convId
    setMessages([])
    setFiles([])
    setTraceEvents([])
    setActiveWorkflow(null)
    localStorage.setItem('currentConversationId', convId)

    await loadHistory(convId)
    await loadFiles(convId)
    message.success('已切换对话')
  }

  async function deleteConversation(convId) {
    const ok = await confirmDialog('确定要删除这个对话吗？相关的文件也会被删除。')
    if (!ok) return
    try {
      await axios.delete(`${settingsRef.current.apiBase}/conversations/${convId}`)
      setConversations((prev) => prev.filter((c) => c.conversation_id !== convId))

      if (conversationIdRef.current === convId) {
        setConversationId(null)
        conversationIdRef.current = null
        setMessages([])
        setFiles([])
        localStorage.removeItem('currentConversationId')
      }
      message.success('对话已删除')
    } catch (error) {
      message.error('删除失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  async function startNewChat() {
    try {
      const response = await axios.post(`${settingsRef.current.apiBase}/conversations`, { title: null })
      const newConv = response.data
      setConversations((prev) => [newConv, ...prev])

      setConversationId(newConv.conversation_id)
      conversationIdRef.current = newConv.conversation_id
      setMessages([])
      setFiles([])
      localStorage.setItem('currentConversationId', newConv.conversation_id)
      message.success('已创建新对话')
    } catch (error) {
      message.error('创建对话失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  // ==================== 回溯 ====================
  async function rollbackToMessage(messageId) {
    if (!conversationIdRef.current || !messageId) return
    const ok = await confirmDialog('回溯后，该消息及之后的所有记录将被永久删除。是否继续？', '确认回溯')
    if (!ok) return
    try {
      const response = await axios.post(
        `${settingsRef.current.apiBase}/conversations/${conversationIdRef.current}/rollback`,
        { target_message_id: messageId }
      )
      if (response.data.success) {
        message.success('已回溯到选定位置')
        setTraceEvents([])
        await loadHistory(conversationIdRef.current)
      }
    } catch (error) {
      console.error('回溯失败:', error)
      message.error('回溯失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  // ==================== 发送消息 ====================
  async function doSend(query) {
    if (!query || isTyping) return
    setMessages((prev) => [...prev, { role: 'user', content: query, time: new Date() }])
    setInputMessage('')
    scrollToBottom()

    if (settingsRef.current.streaming) {
      await sendStreamMessage(query)
    } else {
      await sendNormalMessage(query)
    }
  }

  function sendMessage() {
    const query = inputMessage.trim()
    doSend(query)
  }

  function sendQuickAction(action) {
    setInputMessage(action)
    doSend(action)
  }

  async function sendNormalMessage(query) {
    setIsTyping(true)
    try {
      const response = await axios.post(`${settingsRef.current.apiBase}/chat`, {
        query,
        conversation_id: conversationIdRef.current,
        top_k: settingsRef.current.topK,
        model: settingsRef.current.model,
      })
      const data = response.data

      if (!conversationIdRef.current && data.conversation_id) {
        setConversationId(data.conversation_id)
        conversationIdRef.current = data.conversation_id
        localStorage.setItem('currentConversationId', data.conversation_id)
        loadFiles(data.conversation_id)
        loadConversations()
      }

      setActiveWorkflow(data.active_workflow || null)
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: data.answer, time: new Date(), sources: data.retrieval_sources },
      ])
    } catch (error) {
      message.error('发送失败: ' + (error.response?.data?.error?.message || error.message))
      console.error('Error:', error)
    } finally {
      setIsTyping(false)
      scrollToBottom()
    }
  }

  function stopGeneration() {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      abortControllerRef.current = null
      setIsTyping(false)
    }
  }

  // ==================== WebSocket 追踪 ====================
  function connectTraceWs(convId) {
    if (!convId || convId.startsWith('temp_')) return
    disconnectTraceWs()
    setTraceEvents([])
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws/trace/${convId}`
    const ws = new WebSocket(wsUrl)
    traceWsRef.current = ws

    ws.onopen = () => console.log('[TraceWS] connected', convId)
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'trace') {
          setTraceEvents((prev) => [...prev, data])
        }
      } catch {
        // ignore
      }
    }
    ws.onerror = (e) => console.error('[TraceWS] error', e)
    ws.onclose = () => { traceWsRef.current = null }
  }

  function disconnectTraceWs() {
    if (traceWsRef.current) {
      traceWsRef.current.close()
      traceWsRef.current = null
    }
  }

  // ==================== 流式消息发送 ====================
  async function sendStreamMessage(query) {
    setIsTyping(true)
    setTraceEvents([])
    if (conversationIdRef.current) connectTraceWs(conversationIdRef.current)

    const assistantMessage = { role: 'assistant', content: '', time: new Date(), sources: [] }
    setMessages((prev) => [...prev, assistantMessage])

    abortControllerRef.current = new AbortController()

    try {
      const response = await fetch(`${settingsRef.current.apiBase}/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${authTokenRef.current}`,
        },
        signal: abortControllerRef.current.signal,
        body: JSON.stringify({
          query,
          conversation_id: conversationIdRef.current,
          top_k: settingsRef.current.topK,
          model: settingsRef.current.model,
        }),
      })

      if (response.status === 401) {
        logoutRef.current()
        throw new Error('登录已过期，请重新登录')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let newConvId = null

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        const chunk = decoder.decode(value)
        const lines = chunk.split('\n')

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6))

              if (data.conversation_id && !conversationIdRef.current) {
                newConvId = data.conversation_id
                connectTraceWs(newConvId)
              }

              if (data.content) {
                assistantMessage.content += data.content
                setMessages((prev) => [...prev])
                scrollToBottom()
              }

              if (data.type === 'done') {
                setActiveWorkflow(data.active_workflow || null)
              }

              if (data.error) throw new Error(data.error)
            } catch {
              // 忽略解析错误
            }
          }
        }
      }

      if (newConvId) {
        setConversationId(newConvId)
        conversationIdRef.current = newConvId
        localStorage.setItem('currentConversationId', newConvId)
        loadFiles(newConvId)
        loadConversations()
      }
    } catch (error) {
      if (error.name === 'AbortError') {
        assistantMessage.content += '\n\n[已停止生成]'
      } else {
        assistantMessage.content += '\n\n[错误: ' + error.message + ']'
        message.error('流式输出失败: ' + error.message)
      }
      setMessages((prev) => [...prev])
    } finally {
      abortControllerRef.current = null
      setIsTyping(false)
      scrollToBottom()
    }
  }

  async function loadHistory(convId) {
    try {
      const response = await axios.get(`${settingsRef.current.apiBase}/history/${convId}`)
      if (response.data.messages) {
        setMessages(
          response.data.messages.map((m) => ({
            role: m.role,
            content: m.content,
            time: new Date(m.timestamp),
            message_id: m.message_id,
          }))
        )
      }
    } catch (error) {
      console.error('加载历史失败:', error)
    }
  }

  // ==================== 文件处理 ====================
  function handleFileSelect(event) {
    const file = event.target.files?.[0]
    if (file) uploadFile(file)
    event.target.value = ''
  }

  async function uploadFile(file) {
    let convId = conversationIdRef.current
    if (!convId) {
      convId = 'temp_' + Date.now()
      setConversationId(convId)
      conversationIdRef.current = convId
    }

    const formData = new FormData()
    formData.append('file', file)

    try {
      const response = await axios.post(
        `${settingsRef.current.apiBase}/conversations/${convId}/files`,
        formData,
        { headers: { 'Content-Type': 'multipart/form-data' } }
      )
      const data = response.data

      if (data.conversation_id && convId.startsWith('temp_')) {
        setConversationId(data.conversation_id)
        conversationIdRef.current = data.conversation_id
        localStorage.setItem('currentConversationId', data.conversation_id)
      }

      message.success('上传成功，正在处理...')
      setFiles((prev) => [...prev, data])
      pollFileStatus(data.file_id)
    } catch (error) {
      message.error('上传失败: ' + (error.response?.data?.error?.message || error.message))
    }
  }

  async function loadFiles(convId) {
    if (!convId || convId.startsWith('temp_')) return
    try {
      const response = await axios.get(`${settingsRef.current.apiBase}/conversations/${convId}/files`)
      setFiles(response.data.files || [])
    } catch (error) {
      console.error('加载文件失败:', error)
    }
  }

  async function deleteFile(fileId) {
    const ok = await confirmDialog('确定要删除这个文件吗？')
    if (!ok) return
    try {
      await axios.delete(
        `${settingsRef.current.apiBase}/conversations/${conversationIdRef.current}/files/${fileId}`
      )
      setFiles((prev) => prev.filter((f) => f.file_id !== fileId))
      message.success('文件已删除')
    } catch {
      message.error('删除失败')
    }
  }

  function pollFileStatus(fileId) {
    const maxAttempts = 20
    const pollInterval = 3000
    let attempts = 0

    const interval = setInterval(async () => {
      attempts++
      if (attempts > maxAttempts) {
        clearInterval(interval)
        return
      }

      try {
        const response = await axios.get(
          `${settingsRef.current.apiBase}/conversations/${conversationIdRef.current}/files`
        )
        const file = response.data.files.find((f) => f.file_id === fileId)
        if (file) {
          setFiles((prev) => prev.map((f) => (f.file_id === fileId ? file : f)))

          if (file.status === 'ready' || file.status === 'error') {
            clearInterval(interval)
            if (file.status === 'ready') {
              message.success('文件处理完成，可以提问了！')
            } else {
              message.error('文件处理失败: ' + (file.error_message || '未知错误'))
            }
          }
        }
      } catch (error) {
        console.error('[FilePoll] 轮询失败:', error)
      }
    }, pollInterval)
  }

  // ==================== 设置 ====================
  function saveSettings() {
    localStorage.setItem('ragAgentSettings', JSON.stringify(settings))
    setSettingsVisible(false)
    message.success('设置已保存')
  }

  function updateSetting(key, value) {
    setSettings((prev) => ({ ...prev, [key]: value }))
  }

  function handleKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  if (!authToken) {
    return (
      <div className="login-screen">
        <div className="login-card">
          <div className="login-header">
            <div className="login-icon-badge">
              <Sparkles size={26} color="var(--accent)" />
            </div>
            <div>
              <h2>RAG Agent</h2>
              <p className="login-subtitle">登录以继续</p>
            </div>
          </div>
          <form className="login-form" onSubmit={(e) => { e.preventDefault(); login() }}>
            <div className="login-input-wrap">
              <UserRound size={16} className="login-input-icon" />
              <input
                type="text"
                placeholder="用户名"
                value={loginForm.username}
                onChange={(e) => setLoginForm((prev) => ({ ...prev, username: e.target.value }))}
              />
            </div>
            <div className="login-input-wrap">
              <Lock size={16} className="login-input-icon" />
              <input
                type="password"
                placeholder="密码"
                value={loginForm.password}
                onChange={(e) => setLoginForm((prev) => ({ ...prev, password: e.target.value }))}
              />
            </div>
            <button type="submit" className="login-submit-btn" disabled={loginLoading}>
              {loginLoading ? '登录中…' : '登录'}
            </button>
          </form>
        </div>
      </div>
    )
  }

  return (
    <>
    <AppShell
      view={view}
      onNavigate={setView}
      isAdmin={isAdmin}
      currentUsername={currentUsername}
      meProfile={meProfile}
      onLogout={logout}
      onOpenChangePassword={openChangePassword}
      onWorkflowDeepLink={(instanceId) => {
        setWorkflowDeepLinkId(instanceId)
        setView('workflow')
      }}
    >
      {view === 'admin' && <AdminPanel meProfile={meProfile} />}

      {view === 'workflow' && (
        <WorkflowPanel
          meUserId={meProfile?.user_id}
          onGoToChat={(convId) => {
            setView('chat')
            if (convId) switchConversation(convId)
            else startNewChat()
          }}
          deepLinkInstanceId={workflowDeepLinkId}
          onDeepLinkConsumed={() => setWorkflowDeepLinkId(null)}
        />
      )}

      {view === 'ops' && <OpsPlaceholder />}

      {view === 'chat' && (
      <>
      <div className="chat-layout">
        {/* 左侧边栏 */}
        <aside className="sidebar">
          <div className="sidebar-actions">
            <button className="new-chat-btn" onClick={startNewChat}>
              <Plus size={16} />
              新建对话
            </button>
          </div>

          {/* 对话列表区域 */}
          <div className={`conversation-section ${isHistoryCollapsed ? 'is-collapsed' : ''}`}>
            <div
              className="section-title"
              onClick={() => setIsHistoryCollapsed((v) => !v)}
              style={{ cursor: 'pointer', userSelect: 'none', display: 'flex', justifyContent: 'space-between' }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <History size={14} />
                <span>历史对话</span>
                <ChevronDown
                  size={14}
                  className="collapse-icon"
                  style={{ transform: isHistoryCollapsed ? 'rotate(-90deg)' : 'rotate(0deg)' }}
                />
              </div>
              {conversations.length > 0 && (
                <button
                  className="refresh-btn"
                  onClick={(e) => { e.stopPropagation(); refreshConversations() }}
                >
                  <RefreshCw size={14} />
                </button>
              )}
            </div>

            <div className={`conversation-list-wrap ${isHistoryCollapsed ? 'collapsed' : ''}`}>
              <div className="conversation-list">
                {conversations.length === 0 ? (
                  <div className="empty-conversations">
                    <MessageCircle size={24} color="var(--text-tertiary)" />
                    <p>暂无历史记录</p>
                  </div>
                ) : (
                  conversations.map((conv) => (
                    <div
                      key={conv.conversation_id}
                      className={`conversation-item ${conversationId === conv.conversation_id ? 'active' : ''}`}
                      onClick={() => switchConversation(conv.conversation_id)}
                    >
                      <div className="conversation-icon">
                        <MessageSquare size={16} />
                      </div>
                      <div className="conversation-info">
                        <div className="conversation-title" title={conv.title}>{conv.title}</div>
                        <div className="conversation-meta">
                          <span>{formatTime(conv.updated_at)}</span>
                          {conv.message_count > 0 && (
                            <span className="message-badge">{conv.message_count} 条消息</span>
                          )}
                        </div>
                      </div>
                      <button
                        className="delete-conv-btn"
                        onClick={(e) => { e.stopPropagation(); deleteConversation(conv.conversation_id) }}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>

          {/* 知识库文件入口（移到抽屉里，平时不占地方）
             不依赖 conversationId 是否存在——点击上传时如果还没有对话，
             uploadFile() 会自动新建一个临时对话，所以入口应该一直可见，
             不然用户在发第一条消息之前根本看不到上传在哪。 */}
          <div className="file-section-entry" onClick={() => setFilesDrawerVisible(true)}>
            <div className="section-title" style={{ marginBottom: 0 }}>
              <FilesIcon size={14} />
              <span>知识库文件</span>
              {files.length > 0 && <span className="tag file-count-badge">{files.length}</span>}
            </div>
            <ChevronDown size={14} className="collapse-icon" style={{ transform: 'rotate(-90deg)' }} />
          </div>

          <div className="system-info">
            <hr />
            <div className="info-item">
              <span>API 地址:</span>
              <a onClick={() => setSettingsVisible(true)}>{settings.apiBase}</a>
            </div>
          </div>
        </aside>

        {/* 主聊天区域 */}
        <main className="chat-area">
          <div className="chat-header">
            <div className="header-left">
              <span className="chat-title">💬 当前对话</span>
              {conversationId ? (
                <span className="tag tag--info conv-tag">{conversationId.substring(0, 16)}...</span>
              ) : (
                <span className="tag tag--warning">新对话</span>
              )}
            </div>
            <div className="header-right">
              <WorkflowStatusPill
                activeWorkflow={activeWorkflow}
                onCancel={() => doSend('取消')}
                onUpload={() => fileInputRef.current?.click()}
              />
              <button
                className={showTracePanel ? 'header-btn active' : 'header-btn'}
                title="LangGraph 追踪"
                onClick={() => setShowTracePanel((v) => !v)}
              >
                <Activity size={16} />
              </button>
              <button className="header-btn" title="设置" onClick={() => setSettingsVisible(true)}>
                <Settings size={16} />
              </button>
            </div>
          </div>

          <div ref={messagesContainerRef} className="messages-container">
            {messages.length === 0 && (
              <div className="welcome-message">
                <Sparkles size={64} color="var(--border-hover)" />
                <h2>你好！我是 RAG Agent</h2>
                <p>你可以上传文件，然后向我提问关于文件内容的问题</p>
                <div className="quick-actions">
                  {QUICK_ACTIONS.map((action) => (
                    <button key={action} className="quick-action-btn" onClick={() => sendQuickAction(action)}>
                      {action}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messageTurns.map((turn, tIndex) => (
              <div key={tIndex} className="turn-group">
                {tIndex > 0 && (
                  <div className="turn-divider">
                    <div className="turn-divider-line" />
                    {turn.user && turn.user.message_id ? (
                      <div
                        title="回溯到此处"
                        className={`turn-divider-dot ${
                          tIndex === messageTurns.length - 1 && !isTyping ? 'turn-divider-dot--active' : ''
                        }`}
                        onClick={() => rollbackToMessage(turn.user.message_id)}
                      />
                    ) : (
                      <div className="turn-divider-dot turn-divider-dot--plain" />
                    )}
                  </div>
                )}

                <div className="turn-messages">
                  {turn.messages.map((msg, mIndex) => (
                    <div key={mIndex} className={`message ${msg.role}`}>
                      <div className="message-avatar">
                        <div className={`avatar ${msg.role}`}>
                          {msg.role === 'user' ? <User size={18} /> : <Bot size={18} />}
                        </div>
                      </div>
                      <div className="message-content-wrapper">
                        <div className="message-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }} />
                        <div className="message-time">{formatTime(msg.time)}</div>

                        {msg.sources && msg.sources.length > 0 && (
                          <div className="message-sources">
                            <details>
                              <summary>检索来源</summary>
                              {msg.sources.map((source, idx) => (
                                <div key={idx} className="source-item">
                                  <span className="tag">{source.doc_id}</span>
                                  <span className="source-score">得分: {(source.score * 100).toFixed(1)}%</span>
                                  <p className="source-content">{source.content.substring(0, 100)}...</p>
                                </div>
                              ))}
                            </details>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}

            {isTyping && (
              <div className="message assistant typing">
                <div className="message-avatar">
                  <div className="avatar assistant"><Bot size={18} /></div>
                </div>
                <div className="typing-indicator">
                  <span></span><span></span><span></span>
                </div>
              </div>
            )}
          </div>

          <div className="input-area">
            <div className="input-wrapper">
              <textarea
                className="message-input"
                rows={1}
                placeholder="输入消息... (Enter 发送, Shift+Enter 换行)"
                value={inputMessage}
                onChange={(e) => setInputMessage(e.target.value)}
                onKeyDown={handleKeydown}
              />
              {!isTyping ? (
                <button className="send-btn" disabled={!inputMessage.trim()} onClick={sendMessage}>
                  <Send size={18} />
                </button>
              ) : (
                <button className="send-btn send-btn--danger" onClick={stopGeneration}>
                  <XCircle size={18} />
                </button>
              )}
            </div>
            <div className="input-hint">
              <Info size={12} />
              <span>AI 生成内容仅供参考</span>
            </div>
          </div>
        </main>

        {showTracePanel && (
          <aside className="trace-sidebar">
            <TracePanel traces={traceEvents} />
          </aside>
        )}
      </div>

      {/* 设置对话框 */}
      <Modal
        title="设置"
        open={settingsVisible}
        onCancel={() => setSettingsVisible(false)}
        width={500}
        footer={[
          <button key="cancel" className="dialog-btn" onClick={() => setSettingsVisible(false)}>取消</button>,
          <button key="save" className="dialog-btn dialog-btn--primary" onClick={saveSettings}>保存</button>,
        ]}
        destroyOnHidden
      >
        <div className="settings-form">
          <div className="form-row">
            <label>API 基础地址</label>
            <input
              type="text"
              value={settings.apiBase}
              onChange={(e) => updateSetting('apiBase', e.target.value)}
            />
          </div>

          <div className="form-row">
            <label>检索数量 (Top K)</label>
            <input
              type="range"
              min={1}
              max={20}
              value={settings.topK}
              onChange={(e) => updateSetting('topK', Number(e.target.value))}
            />
            <span className="range-value">{settings.topK}</span>
          </div>

          <div className="form-row">
            <label>流式输出</label>
            <label className="switch">
              <input
                type="checkbox"
                checked={settings.streaming}
                onChange={(e) => updateSetting('streaming', e.target.checked)}
              />
              <span className="switch-slider" />
            </label>
          </div>

          <div className="form-row">
            <label>模型</label>
            <select value={settings.model} onChange={(e) => updateSetting('model', e.target.value)}>
              <option value="gpt-4o">GPT-4o</option>
              <option value="gpt-4o-mini">GPT-4o Mini</option>
            </select>
          </div>
        </div>
      </Modal>

      {/* 隐藏的文件选择框，故意放在抽屉外层、常驻挂载——抽屉是
         destroyOnHidden，装在里面的话关闭抽屉时这个 input 会被卸载，
         WorkflowStatusPill 的"上传材料"按钮（不依赖抽屉打开）点了会拿到
         null ref，直接失效。 */}
      <input
        ref={fileInputRef}
        type="file"
        style={{ display: 'none' }}
        accept=".pdf,.docx,.doc,.txt,.md,.csv,.xlsx,.xls,.pptx,.html,.htm,.json,.yaml,.yml"
        onChange={handleFileSelect}
      />

      {/* 知识库文件抽屉 */}
      <Drawer
        title="知识库文件"
        open={filesDrawerVisible}
        onClose={() => setFilesDrawerVisible(false)}
        width={360}
        destroyOnHidden
      >
        <div className="upload-area" onClick={() => fileInputRef.current?.click()}>
          <UploadCloud className="upload-icon" size={28} />
          <div className="upload-text">
            <div>拖拽或点击上传文件</div>
            <div className="upload-hint">支持 PDF, Word, Excel, PPT, Markdown, HTML等</div>
          </div>
        </div>

        <div className="file-list">
          {files.length === 0 ? (
            <div className="empty-files">
              <Box size={32} color="var(--text-tertiary)" />
              <p>知识库为空</p>
            </div>
          ) : (
            files.map((file) => {
              const Icon = getFileIcon(file.filename)
              return (
                <div key={file.file_id} className="file-item">
                  <div className="file-icon">
                    <Icon size={20} />
                  </div>
                  <div className="file-info">
                    <div className="file-name" title={file.filename}>{file.filename}</div>
                    <div className="file-size">{formatFileSize(file.size)}</div>
                  </div>
                  <div className="file-badges">
                    {file.extract_method === 'vlm_ocr' && <span className="tag tag--success">OCR</span>}
                    <span className={`tag tag--${STATUS_CLASS[file.status] || 'info'}`}>
                      {STATUS_LABEL[file.status] || file.status}
                    </span>
                  </div>
                  <button className="delete-conv-btn" onClick={() => deleteFile(file.file_id)}>
                    <Trash2 size={14} />
                  </button>
                </div>
              )
            })
          )}
        </div>
      </Drawer>
      </>
      )}
    </AppShell>

    {/* 修改密码——顶部导航头像弹层触发，任意视图下都可能打开，所以放在 AppShell 外层 */}
    <Modal
      title="修改密码"
      open={changePasswordVisible}
      onCancel={() => setChangePasswordVisible(false)}
      width={380}
      footer={[
        <button key="cancel" className="dialog-btn" onClick={() => setChangePasswordVisible(false)}>取消</button>,
        <button
          key="submit"
          className="dialog-btn dialog-btn--primary"
          disabled={changePasswordLoading}
          onClick={submitChangePassword}
        >
          {changePasswordLoading ? '提交中…' : '确认修改'}
        </button>,
      ]}
      destroyOnHidden
    >
      <div className="settings-form">
        <div className="form-row form-row--stacked">
          <label>当前密码</label>
          <input
            type="password"
            value={changePasswordForm.old_password}
            onChange={(e) => setChangePasswordForm((prev) => ({ ...prev, old_password: e.target.value }))}
          />
        </div>
        <div className="form-row form-row--stacked">
          <label>新密码</label>
          <input
            type="password"
            value={changePasswordForm.new_password}
            onChange={(e) => setChangePasswordForm((prev) => ({ ...prev, new_password: e.target.value }))}
          />
        </div>
        <div className="form-row form-row--stacked">
          <label>确认新密码</label>
          <input
            type="password"
            value={changePasswordForm.confirm_password}
            onChange={(e) => setChangePasswordForm((prev) => ({ ...prev, confirm_password: e.target.value }))}
            onKeyDown={(e) => { if (e.key === 'Enter') submitChangePassword() }}
          />
        </div>
      </div>
    </Modal>
    </>
  )
}
