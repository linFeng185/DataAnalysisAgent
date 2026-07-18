import { useState, useEffect, useRef, useMemo, type ReactNode } from 'react';
import { Card, Table, Tag, Tabs, Typography, Empty, message, Input, Button, Space, Select, InputNumber, Modal, notification, Popconfirm, Descriptions, Segmented, Switch, Tooltip } from 'antd';
import { UploadOutlined, FileTextOutlined, DatabaseOutlined, LoadingOutlined, DeleteOutlined, GlobalOutlined, TeamOutlined, UserOutlined, TagsOutlined, PlusOutlined } from '@ant-design/icons';
import { get, post, patch } from '../api/client';
import type { KnowledgeEntry, KnowledgeDoc, KnowledgeScope, KnowledgeTag } from '../types';
import { useAuth } from '../hooks/AuthContext';
import dayjs from 'dayjs';

const STRATEGY_OPTIONS = [
  { value: 'auto', label: '自动检测', desc: '根据文档内容自动选择最佳策略（推荐）' },
  { value: 'sql_ddl', label: '按 DDL 语句', desc: '每个 CREATE TABLE/VIEW/INDEX 独立成块，适合 .sql 建表文件' },
  { value: 'reference', label: '按函数签名', desc: '按 #### 标题或 **函数名()** 切分，适合 MySQL/官方技术文档' },
  { value: 'table', label: '按数据字典表', desc: '按 ### 表: 表名 标记切分，适合项目数据字典 Markdown' },
  { value: 'heading', label: '按标题', desc: '按 Markdown 标题层级 (# ## ###) 切分，适合结构化文档' },
  { value: 'paragraph', label: '按段落', desc: '按空行分段后合并到 chunk_size，适合纯文本' },
  { value: 'fixed', label: '固定长度', desc: '固定大小滑动窗口切分，无结构文档兜底' },
];
const CAT_COLORS = ['blue', 'green', 'orange', 'purple', 'cyan', 'magenta', 'gold', 'lime', 'geekblue', 'volcano'];
const SCOPE_META: Record<KnowledgeScope, { label: string; color: string; icon: ReactNode }> = {
  system: { label: '系统知识', color: 'gold', icon: <GlobalOutlined /> },
  tenant: { label: '租户公共', color: 'blue', icon: <TeamOutlined /> },
  private: { label: '个人知识', color: 'green', icon: <UserOutlined /> },
};
const TAG_GROUP_OPTIONS = [
  { value: 'knowledge_type', label: '知识类型' },
  { value: 'technology', label: '技术平台' },
  { value: 'business_domain', label: '业务领域' },
  { value: 'custom', label: '其他' },
];

export default function KnowledgePage() {
  const { user, authRequired } = useAuth();
  const role = user?.role || 'anonymous';
  const [entries, setEntries] = useState<KnowledgeEntry[]>([]);
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [tags, setTags] = useState<KnowledgeTag[]>([]);
  const [tagLoading, setTagLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [catFilter, setCatFilter] = useState<string | undefined>();
  const [scopeFilter, setScopeFilter] = useState<'all' | KnowledgeScope>('all');
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [uploadCfg, setUploadCfg] = useState<{
    strategy: string; chunkSize: number; chunkOverlap: number; category: string;
    scope: KnowledgeScope; tagIds: number[]; datasource: string;
  }>({ strategy: 'auto', chunkSize: 800, chunkOverlap: 100, category: '', scope: 'private', tagIds: [], datasource: '' });
  const [cfgOpen, setCfgOpen] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [taskIds, setTaskIds] = useState<string[]>([]);
  const [taskStates, setTaskStates] = useState<Record<string, { status: string; chunks: number; error: string }>>({});
  const [entryDetail, setEntryDetail] = useState<KnowledgeEntry | null>(null);
  const [docDetail, setDocDetail] = useState<KnowledgeDoc | null>(null);
  const [docContent, setDocContent] = useState('');
  const [docType, setDocType] = useState('text');
  const [docRawUrl, setDocRawUrl] = useState('');
  const [docLoading, setDocLoading] = useState(false);
  const [personalTagName, setPersonalTagName] = useState('');
  const [globalTagName, setGlobalTagName] = useState('');
  const [globalTagGroup, setGlobalTagGroup] = useState('custom');
  const fileRef = useRef<HTMLInputElement>(null);
  const tagSearchTimer = useRef<number | null>(null);

  const uploadScopeOptions = useMemo(() => {
    const scopes: KnowledgeScope[] = role === 'super_admin'
      ? ['system', 'tenant', 'private']
      : role === 'tenant_admin' ? ['tenant', 'private'] : ['private'];
    return scopes.map(scope => ({
      value: scope,
      label: <Space size={6}>{SCOPE_META[scope].icon}{SCOPE_META[scope].label}</Space>,
    }));
  }, [role]);

  const selectableTags = useMemo(() => tags.filter(tag => (
    tag.is_active && (uploadCfg.scope === 'private' || tag.scope === 'global')
  )), [tags, uploadCfg.scope]);

  const visibleDocs = useMemo(() => (
    scopeFilter === 'all' ? docs : docs.filter(doc => doc.scope === scopeFilter)
  ), [docs, scopeFilter]);

  const allCategories = useMemo(() => {
    const cats = new Set<string>();
    entries.forEach(e => { if (e.category) cats.add(e.category); });
    return Array.from(cats).sort();
  }, [entries]);

  const catColorMap = useMemo(() => {
    const m: Record<string, string> = {};
    allCategories.forEach((c, i) => { m[c] = CAT_COLORS[i % CAT_COLORS.length]; });
    return m;
  }, [allCategories]);

  const loadEntries = async (p?: number, ps?: number, scope?: 'all' | KnowledgeScope) => {
    setLoading(true);
    try {
      const curPage = p ?? page;
      const curSize = ps ?? pageSize;
      const params = new URLSearchParams();
      if (search) params.set('search', search);
      if (catFilter) params.set('category', catFilter);
      const selectedScope = scope ?? scopeFilter;
      if (selectedScope !== 'all') params.set('knowledge_scope', selectedScope);
      params.set('page', String(curPage));
      params.set('page_size', String(curSize));
      const data = await get<{ entries: KnowledgeEntry[]; total: number; page: number; page_size: number }>(`/knowledge?${params.toString()}`);
      setEntries(data.entries || []);
      setTotal(data.total || 0);
    } catch { message.error('加载知识库失败'); }
    setLoading(false);
  };

  const loadDocs = async () => {
    try {
      const data = await get<{ docs: KnowledgeDoc[] }>('/knowledge/docs');
      setDocs(data.docs || []);
    } catch { /* empty */ }
  };

  const loadTags = async (query = '', includeInactive = role === 'super_admin') => {
    setTagLoading(true);
    try {
      const params = new URLSearchParams({ q: query, limit: '100' });
      if (includeInactive) params.set('include_inactive', 'true');
      const data = await get<{ tags: KnowledgeTag[] }>(`/knowledge/tags?${params.toString()}`);
      setTags(data.tags || []);
    } catch { message.error('加载标签失败'); }
    setTagLoading(false);
  };

  const reloadAll = async () => {
    await Promise.all([loadEntries(), loadDocs(), loadTags('', role === 'super_admin')]);
  };
  useEffect(() => { void reloadAll(); }, [role]);

  const handleDeleteEntry = async (id: string) => {
    const response = await fetch(`/api/v1/knowledge/${encodeURIComponent(id)}`, {
      method: 'DELETE', credentials: 'include',
    });
    if (!response.ok) { message.error('删除失败'); return; }
    message.success('已删除'); void loadEntries();
  };

  const handleDeleteDoc = async (doc: KnowledgeDoc) => {
    const params = new URLSearchParams({ knowledge_scope: doc.scope });
    const response = await fetch(`/api/v1/knowledge/docs/${encodeURIComponent(doc.name)}?${params.toString()}`, {
      method: 'DELETE', credentials: 'include',
    });
    if (!response.ok) { message.error('删除失败'); return; }
    message.success(`${doc.name} 已删除`); void reloadAll();
  };

  const openDocDetail = async (d: KnowledgeDoc) => {
    setDocDetail(d); setDocContent('加载中...'); setDocLoading(true);
    setDocType('text'); setDocRawUrl('');
    try {
      const params = new URLSearchParams({ knowledge_scope: d.scope });
      const res = await fetch(`/api/v1/knowledge/docs/${encodeURIComponent(d.name)}/content?${params.toString()}`, {
        credentials: 'include',
      });
      if (res.ok) {
        const data = await res.json();
        setDocContent(data.content || '');
        setDocType(data.type || 'text');
        setDocRawUrl(data.raw_url || '');
      } else { setDocContent('无法读取文件内容'); }
    } catch { setDocContent('读取失败'); }
    setDocLoading(false);
  };

  // Polling
  useEffect(() => {
    if (taskIds.length === 0) return;
    const doneStatuses = new Set(['done', 'error']);
    const timer = setInterval(async () => {
      try {
        const res = await fetch('/api/v1/knowledge/upload/status', { credentials: 'include' });
        const data = await res.json();
        const states: Record<string, { status: string; chunks: number; error: string }> = {};
        for (const t of (data.tasks || [])) states[t.id] = { status: t.status, chunks: t.chunks_count, error: t.error };
        setTaskStates(states);
        if (taskIds.every(id => doneStatuses.has(states[id]?.status))) {
          const doneCount = taskIds.filter(id => states[id]?.status === 'done').length;
          notification[taskIds.length - doneCount === 0 ? 'success' : 'warning']({
            message: '文档处理完成', description: `${doneCount} 成功, ${taskIds.length - doneCount} 失败`,
          });
          setTaskIds([]); reloadAll();
        }
      } catch { /* polling */ }
    }, 1000);
    return () => clearInterval(timer);
  }, [taskIds]);

  const startUpload = async () => {
    if (pendingFiles.length === 0) return;
    setCfgOpen(false); setUploading(true);
    try {
      const form = new FormData();
      for (const f of pendingFiles) form.append('files', f);
      const params = new URLSearchParams({ strategy: uploadCfg.strategy,
        chunk_size: String(uploadCfg.chunkSize), chunk_overlap: String(uploadCfg.chunkOverlap),
        knowledge_scope: uploadCfg.scope, tag_ids: uploadCfg.tagIds.join(','), datasource: uploadCfg.datasource });
      if (uploadCfg.category) params.set('category', uploadCfg.category);
      const res = await fetch(`/api/v1/knowledge/docs/upload?${params.toString()}`, {
        method: 'POST', body: form, credentials: 'include',
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || '上传失败');
      if (data.tasks?.length > 0) {
        setTaskIds(data.tasks.map((t: { task_id: string }) => t.task_id));
        message.info(`已接收 ${data.tasks.length} 个文件，后台处理中`);
      }
      data.errors?.forEach((e: { file: string; error: string }) => message.error(`${e.file}: ${e.error}`));
      setPendingFiles([]);
    } catch (error) { message.error(error instanceof Error ? error.message : '上传失败'); }
    setUploading(false);
  };

  const handleFileSelect = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setPendingFiles(Array.from(files)); setCfgOpen(true);
  };

  const handleScopeFilterChange = (value: string | number) => {
    const scope = value as 'all' | KnowledgeScope;
    setScopeFilter(scope);
    setPage(1);
    void loadEntries(1, pageSize, scope);
  };

  const handleTagSearch = (value: string) => {
    if (tagSearchTimer.current !== null) window.clearTimeout(tagSearchTimer.current);
    tagSearchTimer.current = window.setTimeout(() => { void loadTags(value, role === 'super_admin'); }, 250);
  };

  const createPersonalTag = async () => {
    const name = personalTagName.trim();
    if (!name) return;
    try {
      const tag = await post<KnowledgeTag>('/knowledge/tags', { name });
      setTags(current => [tag, ...current.filter(item => item.id !== tag.id)]);
      setUploadCfg(current => ({ ...current, tagIds: [...new Set([...current.tagIds, tag.id])] }));
      setPersonalTagName('');
      message.success('个人标签已创建');
    } catch { message.error('创建个人标签失败'); }
  };

  const createGlobalTag = async () => {
    const name = globalTagName.trim();
    if (!name || role !== 'super_admin') return;
    try {
      await post<KnowledgeTag>('/knowledge/tags/global', { name, tag_group: globalTagGroup });
      setGlobalTagName('');
      await loadTags('', true);
      message.success('全局标签已创建');
    } catch { message.error('创建全局标签失败'); }
  };

  const promoteTag = async (tagId: number) => {
    try {
      await post<KnowledgeTag>(`/knowledge/tags/${tagId}/promote`, {});
      await loadTags('', true);
      message.success('已提升为全局标签');
    } catch { message.error('提升标签失败'); }
  };

  const updateTagStatus = async (tagId: number, isActive: boolean) => {
    try {
      await patch<{ status: string }>(`/knowledge/tags/${tagId}`, { is_active: isActive });
      setTags(current => current.map(tag => tag.id === tagId ? { ...tag, is_active: isActive } : tag));
    } catch { message.error('更新标签状态失败'); }
  };

  const processingTasks = taskIds.filter(id => {
    const s = taskStates[id]?.status;
    return s && s !== 'done' && s !== 'error';
  });

  const scopeFilterControl = (
    <Segmented
      value={scopeFilter}
      onChange={handleScopeFilterChange}
      options={[
        { value: 'all', label: '全部' },
        { value: 'system', label: <Space size={4}><GlobalOutlined />系统</Space> },
        { value: 'tenant', label: <Space size={4}><TeamOutlined />租户</Space> },
        { value: 'private', label: <Space size={4}><UserOutlined />个人</Space> },
      ]}
    />
  );

  const tabItems = [
    {
      key: 'entries', label: '知识条目',
      children: (
        <div>
          <Space wrap style={{ marginBottom: 12 }}>
            {scopeFilterControl}
            <Select style={{ width: 130 }} value={catFilter} onChange={v => { setCatFilter(v); setPage(1); loadEntries(1); }}
              allowClear placeholder="全部类别" options={allCategories.map(c => ({ value: c, label: c }))} />
            <Input.Search placeholder="搜索内容" value={search}
              onChange={e => setSearch(e.target.value)} onSearch={() => { setPage(1); loadEntries(1); }} style={{ width: 200 }} />
          </Space>
          <Table<KnowledgeEntry> dataSource={entries} rowKey="id" loading={loading}
            locale={{ emptyText: <Empty description="暂无知识条目" /> }}
            size="small" onRow={r => ({ onClick: () => setEntryDetail(r), style: { cursor: 'pointer' } })}
            pagination={{ current: page, pageSize, total, showSizeChanger: true, showTotal: t => `共 ${t} 条`,
              onChange: (p, ps) => { setPage(p); setPageSize(ps); loadEntries(p, ps); } }}
            columns={[
              { title: '范围', dataIndex: 'scope', width: 105,
                render: (scope: KnowledgeScope) => {
                  const meta = SCOPE_META[scope] || SCOPE_META.private;
                  return <Tag color={meta.color} icon={meta.icon}>{meta.label}</Tag>;
                } },
              { title: '类别', dataIndex: 'category', width: 90,
                render: v => v ? <Tag color={catColorMap[v] || 'default'}>{v}</Tag> : '-' },
              { title: '源文件', dataIndex: 'source_file', width: 140, ellipsis: true },
              { title: '内容', dataIndex: 'content', ellipsis: true,
                render: v => <Typography.Text style={{ fontSize: 13 }}>{v?.slice(0, 60) || '—'}</Typography.Text> },
              { title: '来源', dataIndex: 'source', width: 80,
                render: v => <Tag color={v === 'user_upload' ? 'orange' : 'green'}>{v || '-'}</Tag> },
              { title: '标签', dataIndex: 'tags', width: 150,
                render: (values: string[]) => values?.length
                  ? <Space size={[0, 4]} wrap>{values.slice(0, 3).map(value => <Tag key={value}>{value}</Tag>)}</Space>
                  : '-' },
              { title: '', key: 'actions', width: 36,
                render: (_: unknown, r: KnowledgeEntry) => (
                  !r.can_delete ? (
                    <Tooltip title="无删除权限">
                      <Button size="small" danger disabled icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()} />
                    </Tooltip>
                  ) : (
                    <Popconfirm title="确定删除？" onConfirm={() => handleDeleteEntry(r.id)}
                      onPopupClick={e => e.stopPropagation()}>
                      <Button size="small" danger icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()} />
                    </Popconfirm>
                  )
                ) },
            ]} />
        </div>
      ),
    },
    {
      key: 'docs', label: '已索引文档',
      children: (
        <div>
          <div style={{ marginBottom: 12 }}>
            <Space wrap>
              {scopeFilterControl}
              <input ref={fileRef} type="file" multiple accept=".md,.txt,.pdf,.docx,.doc,.markdown,.csv"
                style={{ display: 'none' }} onChange={e => handleFileSelect(e.target.files)} />
              <Button icon={<UploadOutlined />} loading={uploading}
                disabled={authRequired && !user}
                onClick={() => fileRef.current?.click()}>上传文档</Button>
              {processingTasks.length > 0 && (
                <Space><LoadingOutlined style={{ color: '#1677ff' }} />
                  <Typography.Text type="secondary">
                    {processingTasks.filter(id => taskStates[id]?.status === 'processing').length} 个处理中
                  </Typography.Text>
                </Space>
              )}
            </Space>
          </div>
          <Table<KnowledgeDoc> dataSource={visibleDocs} rowKey={record => `${record.scope}:${record.name}`} size="small"
            locale={{ emptyText: <Empty description="文档目录为空" /> }}
            onRow={r => ({ onClick: () => openDocDetail(r), style: { cursor: 'pointer' } })}
            columns={[
              { title: '文件名', dataIndex: 'name',
                render: (v: string) => <Space><FileTextOutlined /><span>{v}</span></Space> },
              { title: '范围', dataIndex: 'scope', width: 105,
                render: (scope: KnowledgeScope) => {
                  const meta = SCOPE_META[scope] || SCOPE_META.private;
                  return <Tag color={meta.color} icon={meta.icon}>{meta.label}</Tag>;
                } },
              { title: '标签', dataIndex: 'tag_ids', width: 160,
                render: (ids: number[]) => {
                  const names = (ids || []).map(id => tags.find(tag => tag.id === id)?.name).filter(Boolean);
                  return names.length
                    ? <Space size={[0, 4]} wrap>{names.slice(0, 3).map(name => <Tag key={name}>{name}</Tag>)}</Space>
                    : '-';
                } },
              { title: '大小', dataIndex: 'size', width: 80,
                render: v => `${(v / 1024).toFixed(1)} KB` },
              { title: '修改时间', dataIndex: 'modified', width: 140,
                render: v => v ? dayjs.unix(v).format('YYYY-MM-DD HH:mm') : '-' },
              { title: '', key: 'actions', width: 36,
                render: (_: unknown, r: KnowledgeDoc) => (
                  !r.can_delete ? (
                    <Tooltip title="无删除权限">
                      <Button size="small" danger disabled icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()} />
                    </Tooltip>
                  ) : (
                    <Popconfirm title="确定删除？" onConfirm={() => handleDeleteDoc(r)}
                      onPopupClick={e => e.stopPropagation()}>
                      <Button size="small" danger icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()} />
                    </Popconfirm>
                  )
                ) },
            ]} />
        </div>
      ),
    },
    ...(role === 'super_admin' ? [{
      key: 'tags', label: <Space><TagsOutlined />标签治理</Space>,
      children: (
        <div>
          <Space wrap style={{ marginBottom: 12 }}>
            <Input value={globalTagName} placeholder="标签名称" maxLength={128}
              onChange={event => setGlobalTagName(event.target.value)}
              onPressEnter={() => { void createGlobalTag(); }} style={{ width: 180 }} />
            <Select value={globalTagGroup} onChange={setGlobalTagGroup}
              options={TAG_GROUP_OPTIONS} style={{ width: 130 }} />
            <Button type="primary" icon={<PlusOutlined />} onClick={() => { void createGlobalTag(); }}>
              新建全局标签
            </Button>
            <Input.Search placeholder="搜索标签" onSearch={value => { void loadTags(value, true); }}
              style={{ width: 200 }} allowClear />
          </Space>
          <Table<KnowledgeTag> dataSource={tags} rowKey="id" size="small" loading={tagLoading}
            locale={{ emptyText: <Empty description="暂无标签" /> }} pagination={{ pageSize: 20 }}
            columns={[
              { title: '名称', dataIndex: 'name', width: 180,
                render: (value: string, tag: KnowledgeTag) => <Space>
                  <Tag color={tag.scope === 'global' ? 'blue' : 'green'}>{value}</Tag>
                  {tag.is_seed && <Tag>预置</Tag>}
                </Space> },
              { title: '范围', dataIndex: 'scope', width: 90,
                render: (value: string) => value === 'global' ? '全局' : '个人' },
              { title: '分组', dataIndex: 'tag_group', width: 120,
                render: (value: string) => TAG_GROUP_OPTIONS.find(item => item.value === value)?.label || value },
              { title: '说明', dataIndex: 'description', ellipsis: true, render: (value: string) => value || '-' },
              { title: '状态', dataIndex: 'is_active', width: 80,
                render: (active: boolean, tag: KnowledgeTag) => (
                  <Switch size="small" checked={active}
                    onChange={checked => { void updateTagStatus(tag.id, checked); }} />
                ) },
              { title: '', key: 'actions', width: 100,
                render: (_: unknown, tag: KnowledgeTag) => tag.scope === 'private' ? (
                  <Button size="small" onClick={() => { void promoteTag(tag.id); }}>提升为全局</Button>
                ) : null },
            ]} />
        </div>
      ),
    }] : []),
  ];

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Card title={<Space><DatabaseOutlined />知识库</Space>}
        extra={<Typography.Text type="secondary">{entries.length} 条, {docs.length} 份文档</Typography.Text>}>
        <Tabs items={tabItems} />
      </Card>

      {/* 知识条目详情 */}
      <Modal title="知识条目详情" open={!!entryDetail}
        onCancel={() => setEntryDetail(null)} footer={null} width={600} maskClosable>
        {entryDetail && (<>
          <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }}>
            <Descriptions.Item label="范围">
              <Tag color={SCOPE_META[entryDetail.scope]?.color} icon={SCOPE_META[entryDetail.scope]?.icon}>
                {SCOPE_META[entryDetail.scope]?.label || entryDetail.scope}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="类别"><Tag color={catColorMap[entryDetail.category] || 'default'}>{entryDetail.category || '—'}</Tag></Descriptions.Item>
            <Descriptions.Item label="源文件">{entryDetail.source_file || '—'}</Descriptions.Item>
            <Descriptions.Item label="来源"><Tag color={entryDetail.source === 'user_upload' ? 'orange' : 'green'}>{entryDetail.source || '—'}</Tag></Descriptions.Item>
            <Descriptions.Item label="标签">
              {entryDetail.tags?.length ? entryDetail.tags.map(value => <Tag key={value}>{value}</Tag>) : '—'}
            </Descriptions.Item>
          </Descriptions>
          <Typography.Title level={5}>内容</Typography.Title>
          <div style={{ whiteSpace: 'pre-wrap', maxHeight: 400, overflow: 'auto', fontSize: 13, lineHeight: 1.7, background: '#fafafa', padding: 12, borderRadius: 6 }}>
            {entryDetail.content || '—'}
          </div>
        </>)}
      </Modal>

      {/* 文档详情 — 展示完整内容 */}
      <Modal title={`文档: ${docDetail?.name || ''}`} open={!!docDetail}
        onCancel={() => setDocDetail(null)} footer={null} width={700} maskClosable>
        {docDetail && (
          <>
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label="范围">
                <Tag color={SCOPE_META[docDetail.scope]?.color} icon={SCOPE_META[docDetail.scope]?.icon}>
                  {SCOPE_META[docDetail.scope]?.label || docDetail.scope}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="大小">{(docDetail.size / 1024).toFixed(1)} KB</Descriptions.Item>
              <Descriptions.Item label="修改时间">{docDetail.modified ? dayjs.unix(docDetail.modified).format('YYYY-MM-DD HH:mm') : '—'}</Descriptions.Item>
              <Descriptions.Item label="标签">
                {(docDetail.tag_ids || []).map(id => {
                  const name = tags.find(tag => tag.id === id)?.name;
                  return name ? <Tag key={id}>{name}</Tag> : null;
                })}
              </Descriptions.Item>
            </Descriptions>
            <Typography.Title level={5}>文件内容</Typography.Title>
            {docLoading ? (
              <div style={{ textAlign: 'center', padding: 32 }}><LoadingOutlined style={{ fontSize: 24 }} /></div>
            ) : docType === 'pdf' ? (
              docRawUrl ? (
                <iframe src={docRawUrl} style={{ width: '100%', height: 500, border: '1px solid #d9d9d9', borderRadius: 6 }}
                  title={docDetail.name} />
              ) : (
                <div style={{ padding: 16, color: '#999' }}>无法加载 PDF</div>
              )
            ) : docType === 'word' ? (
              <div style={{ maxHeight: 450, overflow: 'auto', background: '#fafafa', padding: 12, borderRadius: 6 }}
                dangerouslySetInnerHTML={{ __html: docContent || '<p>无法渲染</p>' }} />
            ) : (
              <div style={{ whiteSpace: 'pre-wrap', maxHeight: 450, overflow: 'auto', fontSize: 13,
                lineHeight: 1.7, background: '#fafafa', padding: 12, borderRadius: 6 }}>
                {docContent || '无法提取文本内容'}
              </div>
            )}
          </>
        )}
      </Modal>

      {/* 上传配置 */}
      <Modal title="上传文档" open={cfgOpen} onOk={startUpload}
        onCancel={() => { setCfgOpen(false); setPendingFiles([]); }}
        okText="开始处理" cancelText="取消" width={500}>
        <div style={{ marginBottom: 16 }}>
          <Typography.Text type="secondary">即将处理 {pendingFiles.length} 个文件</Typography.Text>
          {pendingFiles.map((f, i) => <Tag key={i} style={{ margin: 2 }}>{f.name}</Tag>)}
        </div>
        <Space direction="vertical" style={{ width: '100%' }}>
          <div><Typography.Text strong>知识范围</Typography.Text>
            <Select style={{ width: '100%' }} value={uploadCfg.scope} options={uploadScopeOptions}
              onChange={(scope: KnowledgeScope) => setUploadCfg(current => ({
                ...current,
                scope,
                tagIds: current.tagIds.filter(id => (
                  scope === 'private' || tags.find(tag => tag.id === id)?.scope === 'global'
                )),
              }))} /></div>
          <div><Typography.Text strong>数据源</Typography.Text>
            <Input placeholder="可选数据源名称" value={uploadCfg.datasource}
              onChange={event => setUploadCfg(current => ({ ...current, datasource: event.target.value }))} /></div>
          <div><Typography.Text strong>标签</Typography.Text>
            <Select mode="multiple" style={{ width: '100%' }} value={uploadCfg.tagIds}
              loading={tagLoading} showSearch filterOption={false} onSearch={handleTagSearch}
              onFocus={() => { void loadTags(); }}
              onChange={(tagIds: number[]) => setUploadCfg(current => ({ ...current, tagIds }))}
              options={selectableTags.map(tag => ({
                value: tag.id,
                label: `${tag.name}${tag.scope === 'private' ? ' · 个人' : ''}`,
              }))} placeholder="搜索并选择标签" /></div>
          <Space.Compact style={{ width: '100%' }}>
            <Input value={personalTagName} maxLength={128} placeholder="新建个人标签"
              disabled={!user}
              onChange={event => setPersonalTagName(event.target.value)}
              onPressEnter={() => { void createPersonalTag(); }} />
            <Button icon={<PlusOutlined />} disabled={!user || !personalTagName.trim()}
              onClick={() => { void createPersonalTag(); }}>新建</Button>
          </Space.Compact>
          <div><Typography.Text strong>知识类别</Typography.Text>
            <Input placeholder="如 sales_metrics" value={uploadCfg.category}
              onChange={e => setUploadCfg(p => ({ ...p, category: e.target.value }))} /></div>
          <div><Typography.Text strong>分割策略</Typography.Text>
            <Select style={{ width: '100%' }} value={uploadCfg.strategy}
              onChange={v => setUploadCfg(p => ({ ...p, strategy: v }))}
              options={STRATEGY_OPTIONS}
              optionRender={(opt) => (
                <div style={{ padding: '4px 0' }}>
                  <div style={{ fontWeight: 500, fontSize: 13 }}>{opt.label}</div>
                  <div style={{ color: '#999', fontSize: 11, marginTop: 1 }}>
                    {(STRATEGY_OPTIONS.find(s => s.value === opt.value) || {}).desc}
                  </div>
                </div>
              )}
            /></div>
          <div><Typography.Text strong>块大小</Typography.Text>
            <InputNumber style={{ width: '100%' }} min={200} max={4000} value={uploadCfg.chunkSize}
              onChange={v => setUploadCfg(p => ({ ...p, chunkSize: v || 800 }))} /></div>
          <div><Typography.Text strong>重叠</Typography.Text>
            <InputNumber style={{ width: '100%' }} min={0} max={500} value={uploadCfg.chunkOverlap}
              onChange={v => setUploadCfg(p => ({ ...p, chunkOverlap: v || 100 }))} /></div>
        </Space>
      </Modal>
    </div>
  );
}
