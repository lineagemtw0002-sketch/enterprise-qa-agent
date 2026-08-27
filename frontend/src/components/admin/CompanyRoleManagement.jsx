import { useEffect, useState } from 'react'
import { Table, Button, Modal, Input, Checkbox, Tag, Space, Popconfirm, message, Empty, Alert } from 'antd'
import { Database, Plus, Pencil, Trash2 } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 企业管理员的「角色管理」页面：管理本企业能用的角色——既包括自己企业建的
// 角色，也包括跨企业共用的部门角色（如 IT部，只读名字，用来给它们配置本企业
// 的知识库关联）。角色直接携带知识库权限（2026-08-23 起"身份"和"角色"合并
// 成一个概念，见 role_store.py 顶部说明），建/改名/删只对自己企业建的角色
// 开放（部门角色是跨企业共用的词表，只有平台管理员能建/改名/删，这里对应
// 按钮会置灰）；"配置知识库"对两类角色都开放——配置全局角色时，只影响本
// 企业名下持有这个角色的员工，不会波及其他企业。角色分配给员工在「用户
// 管理」页面做。
//
// 新建角色时强制选择关联知识库（真实知识库，或者显式选"无"）——不强制的话
// 容易出现"建了角色但忘了配知识库，权限跟预期不一致"的情况，且没法区分
// "忘了配"和"就是故意不给任何知识库权限"这两种状态；显式的"无"选项就是
// 用来表达后一种意图的。
export default function CompanyRoleManagement() {
  const [roles, setRoles] = useState([])
  const [collections, setCollections] = useState([])
  const [collectionsError, setCollectionsError] = useState('')
  const [loading, setLoading] = useState(false)

  const [collectionsVisible, setCollectionsVisible] = useState(false)
  const [collectionsTarget, setCollectionsTarget] = useState(null)
  const [selectedCollections, setSelectedCollections] = useState([])
  const [collectionsLoading, setCollectionsLoading] = useState(false)

  const [createVisible, setCreateVisible] = useState(false)
  const [createForm, setCreateForm] = useState({ name: '', display_name: '' })
  const [createSelectedCollections, setCreateSelectedCollections] = useState([])
  const [createLoading, setCreateLoading] = useState(false)

  const [renameVisible, setRenameVisible] = useState(false)
  const [renameTarget, setRenameTarget] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renameLoading, setRenameLoading] = useState(false)

  // Checkbox.Group 里的一个特殊选项——选它表示"我确认这个角色就是不关联
  // 任何知识库"，跟真实知识库互斥（勾了它其他选项会被清空，反之亦然），
  // 用来在提交前强制管理员做一个明确选择，而不是留一片空白让人猜是忘了
  // 选还是故意的。
  const NONE_VALUE = '__none__'

  async function loadAll() {
    setLoading(true)
    setCollectionsError('')
    try {
      const roleList = await adminApi.listRoles()
      // 四个系统内置角色（超级管理员/管理员/企业管理员/普通用户）是平台运营方
      // 层级的身份档位，企业管理员既管不了（重命名/删除/配置知识库这几个
      // 操作后端本来就会 403，见 app.py _authorize_role_mutation / is_system
      // 判断），也不该在这个页面里看到——这个页面是企业管理员管"自己企业内部
      // 的角色"的地方，不是平台角色目录，混进来只会让人误以为能管，之前只是
      // 禁用按钮，容易让人误会成"暂时用不了"而不是"压根不归你管"，改成直接
      // 不展示。真正需要在企业内部给员工标记"普通用户"身份的地方是「用户
      // 管理」页面，那边走的是另一份不受这个过滤影响的选项列表。
      setRoles(roleList.filter((r) => !r.is_system))
    } catch (error) {
      message.error('加载角色失败: ' + (error.response?.data?.detail || error.message))
    }
    // 单独 try/catch——委托模式企业这里会 400，不能让它连累上面角色列表也加载不出来。
    try {
      const collectionList = await adminApi.listCollections()
      setCollections(collectionList)
    } catch (error) {
      setCollections([])
      setCollectionsError(error.response?.data?.detail || error.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  const collectionLabel = (name) => collections.find((c) => c.collection_name === name)?.display_name || name

  const collectionCheckboxOptions = [
    { label: '无（不关联任何知识库）', value: NONE_VALUE },
    ...collections.map((c) => ({ label: c.display_name, value: c.collection_name })),
  ]

  // 「无」跟真实知识库互斥：选中「无」时清空其他勾选，选中任意真实知识库时
  // 自动取消「无」——两个 modal（新建/配置知识库）共用同一套互斥逻辑。
  function handleCollectionCheckboxChange(setter) {
    return (values) => {
      const noneJustChecked = values.includes(NONE_VALUE)
      if (noneJustChecked && values.length > 1) {
        // 上一次没有「无」，这次勾了「无」——说明是新点的「无」，清空其他项
        setter([NONE_VALUE])
      } else {
        setter(values)
      }
    }
  }

  function openCreate() {
    setCreateForm({ name: '', display_name: '' })
    setCreateSelectedCollections([])
    setCreateVisible(true)
  }

  async function submitCreate() {
    if (!createForm.name.trim() || !createForm.display_name.trim()) {
      message.warning('请填写完整')
      return
    }
    if (createSelectedCollections.length === 0) {
      message.warning('请选择关联的知识库，不需要关联的话选"无"')
      return
    }
    setCreateLoading(true)
    try {
      const role = await adminApi.createRole({ name: createForm.name.trim(), display_name: createForm.display_name.trim() })
      const collectionNames = createSelectedCollections.includes(NONE_VALUE) ? [] : createSelectedCollections
      await adminApi.setRoleCollections(role.role_id, collectionNames)
      message.success('角色创建成功')
      setCreateVisible(false)
      await loadAll()
    } catch (error) {
      message.error('创建失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setCreateLoading(false)
    }
  }

  function openRename(role) {
    setRenameTarget(role)
    setRenameValue(role.display_name)
    setRenameVisible(true)
  }

  async function submitRename() {
    if (!renameValue.trim()) {
      message.warning('展示名不能为空')
      return
    }
    setRenameLoading(true)
    try {
      await adminApi.updateRole(renameTarget.role_id, renameValue.trim())
      message.success('已更新')
      setRenameVisible(false)
      await loadAll()
    } catch (error) {
      message.error('更新失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setRenameLoading(false)
    }
  }

  async function handleDeleteRole(role) {
    try {
      await adminApi.deleteRole(role.role_id)
      message.success('角色已删除')
      await loadAll()
    } catch (error) {
      message.error('删除失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  function openCollections(role) {
    setCollectionsTarget(role)
    setSelectedCollections(role.collection_names?.length ? role.collection_names : [NONE_VALUE])
    setCollectionsVisible(true)
  }

  async function submitCollections() {
    if (selectedCollections.length === 0) {
      message.warning('请选择关联的知识库，不需要关联的话选"无"')
      return
    }
    setCollectionsLoading(true)
    try {
      const collectionNames = selectedCollections.includes(NONE_VALUE) ? [] : selectedCollections
      await adminApi.setRoleCollections(collectionsTarget.role_id, collectionNames)
      message.success('知识库关联已保存')
      setCollectionsVisible(false)
      await loadAll()
    } catch (error) {
      message.error('保存失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setCollectionsLoading(false)
    }
  }

  const columns = [
    {
      title: '角色',
      dataIndex: 'display_name',
      key: 'display_name',
      render: (t, role) => (
        <Space>
          <span style={{ fontWeight: 500 }}>{t}</span>
          {/* org_id 为空说明是跨企业共用的部门角色（只有平台管理员能建/改名/
              删），不是本企业自己建的——用标签提示一下，避免管理员点了重命名/
              删除却被后端 403 搞不懂为什么。 */}
          {!role.org_id && <Tag color="default">全局角色</Tag>}
        </Space>
      ),
    },
    { title: '内部标识', dataIndex: 'name', key: 'name', render: (t) => <code>{t}</code> },
    {
      title: '关联知识库',
      dataIndex: 'collection_names',
      key: 'collection_names',
      render: (names) =>
        names && names.length ? (
          names.includes('*') ? (
            <Tag color="purple">不限</Tag>
          ) : (
            <Space size={[0, 4]} wrap>
              {names.map((n) => <Tag key={n}>{collectionLabel(n)}</Tag>)}
            </Space>
          )
        ) : (
          <Tag>无</Tag>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 260,
      render: (_, role) => (
        <Space>
          <Button size="small" icon={<Database size={14} />} onClick={() => openCollections(role)} disabled={!!collectionsError || role.is_system}>配置知识库</Button>
          <Button size="small" icon={<Pencil size={14} />} onClick={() => openRename(role)} disabled={!role.org_id}>重命名</Button>
          <Popconfirm
            title="删除角色"
            description="删除后，持有该角色的员工会立刻失去这个角色关联的知识库访问权限，此操作不可逆。"
            okText="确认删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => handleDeleteRole(role)}
            disabled={!role.org_id}
          >
            <Button size="small" danger icon={<Trash2 size={14} />} disabled={!role.org_id}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16, gap: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          角色：给角色关联一批知识库（一个角色可以对应多个知识库，一个知识库也可以对应多个角色），再去「用户管理」页面把角色分配给员工，不需要重新登录立即生效。列表里还包含跨企业共用的部门角色（标"全局角色"），只能给它们配置本企业的知识库关联，不能重命名/删除；平台运营方的系统角色（超级管理员/管理员/企业管理员/普通用户）不属于本企业管理范围，这里不展示。
        </p>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate} style={{ flexShrink: 0 }}>新建角色</Button>
      </div>

      {collectionsError && (
        <Alert
          style={{ marginBottom: 16 }}
          type="warning"
          showIcon
          message="本地知识库不适用于本企业"
          description={`${collectionsError}——角色本身仍可正常建/改名/删，只是「配置知识库」暂时用不了。`}
        />
      )}

      <Table
        rowKey="role_id"
        columns={columns}
        dataSource={roles}
        loading={loading}
        pagination={{ pageSize: 10 }}
        locale={{ emptyText: <Empty description="暂无角色，先点上方「新建角色」" /> }}
      />

      <Modal
        title="新建角色"
        open={createVisible}
        onCancel={() => setCreateVisible(false)}
        onOk={submitCreate}
        confirmLoading={createLoading}
        okText="创建"
        cancelText="取消"
        width={520}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <div style={{ marginBottom: 4 }}>展示名</div>
            <Input
              placeholder="如：后勤"
              value={createForm.display_name}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, display_name: e.target.value }))}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>内部标识（英文，创建后不可修改）</div>
            <Input
              placeholder="如：logistics"
              value={createForm.name}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, name: e.target.value }))}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>关联知识库（必选，不需要就选"无"）</div>
            {collectionsError ? (
              <Alert type="warning" showIcon message={collectionsError} />
            ) : collections.length === 0 ? (
              <Checkbox.Group
                value={createSelectedCollections}
                onChange={handleCollectionCheckboxChange(setCreateSelectedCollections)}
                options={[{ label: '无（不关联任何知识库）', value: NONE_VALUE }]}
              />
            ) : (
              <Checkbox.Group
                style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
                value={createSelectedCollections}
                onChange={handleCollectionCheckboxChange(setCreateSelectedCollections)}
                options={collectionCheckboxOptions}
              />
            )}
          </div>
        </div>
      </Modal>

      <Modal
        title="重命名角色"
        open={renameVisible}
        onCancel={() => setRenameVisible(false)}
        onOk={submitRename}
        confirmLoading={renameLoading}
        okText="保存"
        cancelText="取消"
      >
        <Input value={renameValue} onChange={(e) => setRenameValue(e.target.value)} />
      </Modal>

      <Modal
        title={`配置知识库 · ${collectionsTarget?.display_name || ''}`}
        open={collectionsVisible}
        onCancel={() => setCollectionsVisible(false)}
        onOk={submitCollections}
        confirmLoading={collectionsLoading}
        okText="保存"
        cancelText="取消"
        width={520}
      >
        {collections.length === 0 ? (
          <Checkbox.Group
            value={selectedCollections}
            onChange={handleCollectionCheckboxChange(setSelectedCollections)}
            options={[{ label: '无（不关联任何知识库）', value: NONE_VALUE }]}
          />
        ) : (
          <Checkbox.Group
            style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
            value={selectedCollections}
            onChange={handleCollectionCheckboxChange(setSelectedCollections)}
            options={collectionCheckboxOptions}
          />
        )}
      </Modal>
    </div>
  )
}
