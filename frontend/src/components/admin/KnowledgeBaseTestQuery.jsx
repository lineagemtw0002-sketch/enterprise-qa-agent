import { useCallback, useEffect, useState } from 'react'
import { Select, Input, Button, Card, Tag, Empty, message, Alert, Table, Drawer, Popconfirm } from 'antd'
import { Search, Eye, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 【测试专用，正式上线前删除】管理员知识库超权测试查询页——选一家企业 + 输入
// 查询词，直接看这家企业的知识库能查到什么、命中的是哪几个知识库，绕过任何
// 用户级 ACL（不代表任何真实用户的实际可见范围），只用来验证企业知识库摄入/
// 检索质量。后端端点见 app.py `admin_test_query_knowledge_base`，同一份说明
// 里列了正式上线前需要一并删除的所有文件。
export default function KnowledgeBaseTestQuery() {
  const [orgs, setOrgs] = useState([])
  const [orgId, setOrgId] = useState(undefined)
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)

  // 临时测试便利功能：某企业名下各知识库的 chunk 统计 + 查看/清空，方便反复
  // 测试"导入知识库->查询知识库"这条链路，不用每次都手动清数据库或者猜有没有
  // 摄入成功。上线前跟本页其余"超权测试查询"部分一起删除。
  const [collections, setCollections] = useState([])
  const [collectionsLoading, setCollectionsLoading] = useState(false)
  const [clearingCollection, setClearingCollection] = useState(null)
  const [chunkDrawer, setChunkDrawer] = useState(null) // { collection, displayName }
  const [chunks, setChunks] = useState([])
  const [chunksLoading, setChunksLoading] = useState(false)

  useEffect(() => {
    adminApi.listOrganizations()
      .then(setOrgs)
      .catch((error) => message.error('加载企业列表失败: ' + (error.response?.data?.detail || error.message)))
  }, [])

  const loadCollections = useCallback((id) => {
    if (!id) { setCollections([]); return }
    setCollectionsLoading(true)
    adminApi.adminTestListKbCollections(id)
      .then(setCollections)
      .catch((error) => message.error('加载知识库列表失败: ' + (error.response?.data?.detail || error.message)))
      .finally(() => setCollectionsLoading(false))
  }, [])

  useEffect(() => { loadCollections(orgId) }, [orgId, loadCollections])

  async function handleViewChunks(record) {
    setChunkDrawer({ collection: record.collection_name, displayName: record.display_name })
    setChunks([])
    setChunksLoading(true)
    try {
      const data = await adminApi.adminTestListKbChunks({ org_id: orgId, collection: record.collection_name, limit: 50 })
      setChunks(data)
    } catch (error) {
      message.error('加载 chunk 列表失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setChunksLoading(false)
    }
  }

  async function handleClearCollection(record) {
    setClearingCollection(record.collection_name)
    try {
      const data = await adminApi.adminTestClearKbCollection({ org_id: orgId, collection: record.collection_name })
      message.success(`已清空「${record.display_name}」，共清除 ${data.cleared_chunks} 条`)
      loadCollections(orgId)
    } catch (error) {
      message.error('清空失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setClearingCollection(null)
    }
  }

  const collectionColumns = [
    { title: '知识库', dataIndex: 'display_name', key: 'display_name' },
    {
      title: '类型', dataIndex: 'source', key: 'source', width: 100,
      render: (source) => <Tag color={source === 'delegated' ? 'purple' : 'blue'}>{source === 'delegated' ? '委托模式' : '本地'}</Tag>,
    },
    { title: 'Chunk 数', dataIndex: 'chunk_count', key: 'chunk_count', width: 100 },
    {
      title: '操作', key: 'actions', width: 180,
      render: (_, record) => (
        <div style={{ display: 'flex', gap: 8 }}>
          <Button size="small" icon={<Eye size={13} />} onClick={() => handleViewChunks(record)}>查看</Button>
          <Popconfirm
            title="确认清空这个知识库？"
            description="会清除向量库、BM25 索引和摄入历史记录，不可恢复。"
            okText="清空" okType="danger" cancelText="取消"
            onConfirm={() => handleClearCollection(record)}
          >
            <Button size="small" danger icon={<Trash2 size={13} />} loading={clearingCollection === record.collection_name}>清空</Button>
          </Popconfirm>
        </div>
      ),
    },
  ]

  const chunkColumns = [
    { title: 'Chunk ID', dataIndex: 'chunk_id', key: 'chunk_id', width: 160, ellipsis: true },
    { title: '内容', dataIndex: 'text', key: 'text' },
    { title: '来源文件', dataIndex: 'source_path', key: 'source_path', width: 220, ellipsis: true },
  ]

  async function handleQuery() {
    if (!orgId) { message.warning('请先选择企业'); return }
    if (!query.trim()) { message.warning('请输入查询内容'); return }
    setLoading(true)
    setResult(null)
    try {
      const data = await adminApi.adminTestQueryKnowledgeBase({ org_id: orgId, query: query.trim(), top_k: 5 })
      setResult(data)
    } catch (error) {
      message.error('查询失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message="测试专用工具"
        description="这个查询绕过任何用户级知识库权限，直接返回指定企业的全部知识库内容，不代表任何真实用户实际能看到的范围。正式上线前会删除。"
      />

      <div style={{ display: 'flex', gap: 12, marginBottom: 16 }}>
        <Select
          placeholder="选择企业"
          style={{ width: 240 }}
          value={orgId}
          onChange={setOrgId}
          options={orgs.map((o) => ({ value: o.org_id, label: o.name }))}
        />
        <Input
          placeholder="输入查询内容"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onPressEnter={handleQuery}
          style={{ flex: 1 }}
        />
        <Button type="primary" icon={<Search size={14} />} loading={loading} onClick={handleQuery}>
          查询
        </Button>
      </div>

      {result && (
        <Card
          size="small"
          title="查询结果"
          style={{ marginBottom: 16 }}
          extra={
            result.collections.length > 0 ? (
              <span>
                来自知识库：
                {result.collections.map((c) => <Tag key={c} color="blue">{c}</Tag>)}
              </span>
            ) : (
              <Tag>未命中任何知识库</Tag>
            )
          }
        >
          {result.is_empty ? (
            <Empty description="没有查到相关内容" />
          ) : (
            <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>{result.content}</pre>
          )}
        </Card>
      )}

      {orgId && (
        <Card size="small" title="该企业知识库一览（清空 / 查看 chunk，方便测试导入->查询）" loading={collectionsLoading}>
          <Table
            rowKey="collection_name"
            size="small"
            columns={collectionColumns}
            dataSource={collections}
            pagination={false}
            locale={{ emptyText: <Empty description="该企业还没有任何知识库" /> }}
          />
        </Card>
      )}

      <Drawer
        title={chunkDrawer ? `Chunk 列表 - ${chunkDrawer.displayName}` : ''}
        open={!!chunkDrawer}
        onClose={() => setChunkDrawer(null)}
        width={640}
      >
        <Table
          rowKey="chunk_id"
          size="small"
          loading={chunksLoading}
          columns={chunkColumns}
          dataSource={chunks}
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: <Empty description="没有 chunk" /> }}
        />
      </Drawer>
    </div>
  )
}
