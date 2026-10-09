import { t, useLanguage } from '../i18n';
import { useState, useEffect, useRef, useMemo, type ReactNode } from 'react';
import { Card, Table, Tag, Tabs, Typography, Empty, message, Input, Button, Space, Select, InputNumber, Modal, notification, Popconfirm, Descriptions, Segmented, Switch, Tooltip, Slider, Checkbox } from 'antd';
import { UploadOutlined, FileTextOutlined, DatabaseOutlined, LoadingOutlined, DeleteOutlined, GlobalOutlined, TeamOutlined, UserOutlined, TagsOutlined, PlusOutlined, CheckOutlined } from '@ant-design/icons';
import { get, post, patch } from '../api/client';
import { fetchDatasourceCatalog } from '../api/datasourceCatalog';
import type { KnowledgeEntry, KnowledgeDoc, KnowledgeScope, KnowledgeTag } from '../types';
import { useAuth } from '../hooks/AuthContext';
import dayjs from 'dayjs';
import DOMPurify from 'dompurify';
import EmergencyKnowledgePanel from '../components/EmergencyKnowledgePanel';

const STRATEGY_OPTIONS = [
  { value: 'auto', get label() { return t("自动检测"); }, get desc() { return t("根据文档内容自动选择最佳策略（推荐）"); } },
  { value: 'sql_ddl', get label() { return t("按 DDL 语句"); }, get desc() { return t("每个 CREATE TABLE/VIEW/INDEX 独立成块，适合 .sql 建表文件"); } },
  { value: 'reference', get label() { return t("按函数签名"); }, get desc() { return t("按 #### 标题或 **函数名()** 切分，适合 MySQL/官方技术文档"); } },
  { value: 'table', get label() { return t("按数据字典表"); }, get desc() { return t("按 ### 表: 表名 标记切分，适合项目数据字典 Markdown"); } },
  { value: 'heading', get label() { return t("按标题"); }, get desc() { return t("按 Markdown 标题层级 (# ## ###) 切分，适合结构化文档"); } },
  { value: 'paragraph', get label() { return t("按段落"); }, get desc() { return t("按空行分段后合并到 chunk_size，适合纯文本"); } },
  { value: 'fixed', get label() { return t("固定长度"); }, get desc() { return t("固定大小滑动窗口切分，无结构文档兜底"); } },
];
const CAT_COLORS = ['blue', 'green', 'orange', 'purple', 'cyan', 'magenta', 'gold', 'lime', 'geekblue', 'volcano'];
const SCOPE_META: Record<KnowledgeScope, { label: string; color: string; icon: ReactNode }> = {
  system: { get label() { return t("系统知识"); }, color: 'gold', icon: <GlobalOutlined /> },
  tenant: { get label() { return t("租户公共"); }, color: 'blue', icon: <TeamOutlined /> },
  private: { get label() { return t("个人知识"); }, color: 'green', icon: <UserOutlined /> },
};
const TAG_GROUP_OPTIONS = [
  { value: 'knowledge_type', get label() { return t("知识类型"); } },
  { value: 'technology', get label() { return t("技术平台"); } },
  { value: 'business_domain', get label() { return t("业务领域"); } },
  { value: 'custom', get label() { return t("其他"); } },
];

/** 上传配置弹窗的表单状态结构。 */
type UploadCfg = {
  strategy: string; chunkSize: number; chunkOverlap: number; category: string;
  scope: KnowledgeScope; tagIds: number[]; datasource: string;
};

// 方法作用：构造上传配置表单的默认值，首次挂载与每次打开弹窗都据此重置
// Args: role - 当前用户角色，用于决定默认知识范围
// Returns: 全新的上传配置对象，保证多次打开弹窗之间不共享残留状态
const createDefaultUploadCfg = (role: string): UploadCfg => ({
  strategy: 'auto', chunkSize: 800, chunkOverlap: 100, category: '',
  scope: role === 'super_admin' ? 'system' : 'private', tagIds: [], datasource: '',
});

export default function KnowledgePage() {
  const language = useLanguage();
  const { user, authRequired } = useAuth();
  const role = user?.role || 'anonymous';
  const [entries, setEntries] = useState<KnowledgeEntry[]>([]);
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [tags, setTags] = useState<KnowledgeTag[]>([]);
  const [tagLoading, setTagLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [catFilter, setCatFilter] = useState<string | undefined>();
  const [scopeFilter, setScopeFilter] = useState<'all' | KnowledgeScope>('all');
  const [entryKind, setEntryKind] = useState<'document' | 'introspect' | 'all'>('document');
  // 系统知识默认隐藏，勾选后仅只读查看，且状态不持久化。
  const [includeSystem, setIncludeSystem] = useState(false);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [uploadCfg, setUploadCfg] = useState<UploadCfg>(() => createDefaultUploadCfg(role));
  const [cfgOpen, setCfgOpen] = useState(false);
  const [datasourceOptions, setDatasourceOptions] = useState<{ value: string; label: string }[]>([]);
  const [tagPanelOpen, setTagPanelOpen] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [taskIds, setTaskIds] = useState<string[]>([]);
  const [taskStates, setTaskStates] = useState<Record<string, { status: string; chunks: number; error: string }>>({});
  const [entryDetail, setEntryDetail] = useState<KnowledgeEntry | null>(null);
  const [docDetail, setDocDetail] = useState<KnowledgeDoc | null>(null);
  const [docContent, setDocContent] = useState('');
  const sanitizedDocContent = useMemo(
    () => DOMPurify.sanitize(docContent || '<p>无法渲染</p>'),
    [docContent],
  );
  const [docType, setDocType] = useState('text');
  const [docRawUrl, setDocRawUrl] = useState('');
  const [docLoading, setDocLoading] = useState(false);
  const [personalTagName, setPersonalTagName] = useState('');
  const [globalTagName, setGlobalTagName] = useState('');
  const [globalTagGroup, setGlobalTagGroup] = useState('custom');
  const fileRef = useRef<HTMLInputElement>(null);
  const tagSearchTimer = useRef<number | null>(null);

  // 上传配置弹窗打开时才拉取数据源候选，避免页面加载时的额外请求。
  useEffect(() => {
    if (!cfgOpen) return;
    let cancelled = false;
    void (async () => {
      try {
        const items = await fetchDatasourceCatalog();
        if (!cancelled) setDatasourceOptions(items.map(item => ({ value: item.name, label: item.name })));
      } catch { if (!cancelled) message.error(t("无法加载数据源列表")); }
    })();
    return () => { cancelled = true; };
  }, [cfgOpen]);

  const uploadScopeOptions = useMemo(() => {
    const scopes: KnowledgeScope[] = role === 'super_admin'
      ? ['system']
      : role === 'tenant_admin' ? ['tenant', 'private'] : ['private'];
    return scopes.map(scope => ({
      value: scope,
      label: <Space size={6}>{SCOPE_META[scope].icon}{SCOPE_META[scope].label}</Space>,
    }));
  }, [role, language]);

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
      params.set('entry_kind', entryKind);
      if (includeSystem) params.set('include_system', 'true');
      params.set('page', String(curPage));
      params.set('page_size', String(curSize));
      const data = await get<{ entries: KnowledgeEntry[]; total: number; page: number; page_size: number }>(`/knowledge?${params.toString()}`);
      setEntries(data.entries || []);
      setTotal(data.total || 0);
    } catch { message.error(t("加载知识库失败")); }
    setLoading(false);
  };

  const loadDocs = async () => {
    try {
      const data = await get<{ docs: KnowledgeDoc[] }>(
        includeSystem ? '/knowledge/docs?include_system=true' : '/knowledge/docs');
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
    } catch { message.error(t("加载标签失败")); }
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
    if (!response.ok) { message.error(t("删除失败")); return; }
    message.success(t("已删除")); void loadEntries();
  };

  const handleDeleteDoc = async (doc: KnowledgeDoc) => {
    const params = new URLSearchParams({ knowledge_scope: doc.scope });
    const response = await fetch(`/api/v1/knowledge/docs/${encodeURIComponent(doc.name)}?${params.toString()}`, {
      method: 'DELETE', credentials: 'include',
    });
    if (!response.ok) { message.error(t("删除失败")); return; }
    message.success(t("{value0} 已删除", { value0: doc.name })); void reloadAll();
  };

  const openDocDetail = async (d: KnowledgeDoc) => {
    setDocDetail(d); setDocContent(t("加载中...")); setDocLoading(true);
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
      } else { setDocContent(t("无法读取文件内容")); }
    } catch { setDocContent(t("读取失败")); }
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
        const finished = taskIds.every(id => {
          const status = states[id]?.status;
          // 任务从响应中消失时按终止处理，否则 every 恒为假会导致永不停止的轮询。
          return status === undefined || doneStatuses.has(status);
        });
        if (finished) {
          const doneCount = taskIds.filter(id => states[id]?.status === 'done').length;
          notification[doneCount === taskIds.length ? 'success' : 'warning']({
            message: t("文档处理完成"), description: t("{value0} 成功, {value1} 失败", { value0: doneCount, value1: taskIds.length - doneCount }),
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
      if (!res.ok) throw new Error(data.detail || t("上传失败"));
      if (data.tasks?.length > 0) {
        setTaskIds(data.tasks.map((t: { task_id: string }) => t.task_id));
        message.info(t("已接收 {value0} 个文件，后台处理中", { value0: data.tasks.length }));
      }
      data.errors?.forEach((e: { file: string; error: string }) => message.error(`${e.file}: ${e.error}`));
      setPendingFiles([]);
    } catch (error) { message.error(error instanceof Error ? error.message : t("上传失败")); }
    setUploading(false);
  };

  // 方法作用：重置上传弹窗的全部表单状态，保证每次打开都是干净状态
  // Args: 无
  // Returns: 无返回值，直接写回各 useState
  const resetUploadForm = () => {
    // 打开时重置而非关闭时重置，避免关闭动画期间字段闪变
    setUploadCfg(createDefaultUploadCfg(role));
    setPendingFiles([]);
    setPersonalTagName('');
    setTagPanelOpen(false);
  };

  // 方法作用：用户选择文件后先重置表单再打开上传配置弹窗
  // Args: files - 浏览器文件选择框返回的文件列表，可能为 null
  // Returns: 无返回值，直接更新弹窗与待上传文件状态
  const handleFileSelect = (files: FileList | null) => {
    // 必须先把活 FileList 快照成数组：e.target.files 与 input 绑定，
    // 清空 value 会把它一并清空，清空后再读长度恒为 0，弹窗就永远打不开。
    const selected = files ? Array.from(files) : [];
    // 无论本次是否真的选到文件都先清空 input.value：浏览器只在 value 变化时派发
    // change，取消后重选同一个文件本来不会触发，弹窗就再也不会打开。
    if (fileRef.current) fileRef.current.value = '';
    if (selected.length === 0) return;
    resetUploadForm();
    setPendingFiles(selected);
    setCfgOpen(true);
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
      message.success(t("个人标签已创建"));
    } catch { message.error(t("创建个人标签失败")); }
  };

  const createGlobalTag = async () => {
    const name = globalTagName.trim();
    if (!name || role !== 'super_admin') return;
    try {
      await post<KnowledgeTag>('/knowledge/tags/global', { name, tag_group: globalTagGroup });
      setGlobalTagName('');
      await loadTags('', true);
      message.success(t("全局标签已创建"));
    } catch { message.error(t("创建全局标签失败")); }
  };

  const updateTagStatus = async (tagId: number, isActive: boolean) => {
    try {
      await patch<{ status: string }>(`/knowledge/tags/${tagId}`, { is_active: isActive });
      setTags(current => current.map(tag => tag.id === tagId ? { ...tag, is_active: isActive } : tag));
    } catch { message.error(t("更新标签状态失败")); }
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
        { value: 'all', label: t("全部") },
        { value: 'system', label: <Space size={4}><GlobalOutlined />{t("系统")}</Space> },
        { value: 'tenant', label: <Space size={4}><TeamOutlined />{t("租户")}</Space> },
        { value: 'private', label: <Space size={4}><UserOutlined />{t("个人")}</Space> },
      ]}
    />
  );

  const tabItems = [
    {
      key: 'entries', label: t("知识条目"),
      children: (
        <div>
          <Space wrap style={{ marginBottom: 12 }}>
            {scopeFilterControl}
            <Segmented value={entryKind}
              onChange={value => { setEntryKind(value as 'document' | 'introspect' | 'all'); setPage(1); void loadEntries(1); }}
              options={[
                { value: 'document', label: t("文档知识") },
                { value: 'introspect', label: t("Schema 内省") },
                { value: 'all', label: t("全部") },
              ]} />
            <Checkbox checked={includeSystem}
              onChange={event => { setIncludeSystem(event.target.checked); setPage(1); void loadEntries(1); }}>
              {t("显示系统知识")}
            </Checkbox>
            <Select style={{ width: 130 }} value={catFilter} onChange={v => { setCatFilter(v); setPage(1); loadEntries(1); }}
              allowClear placeholder={t("全部类别")} options={allCategories.map(c => ({ value: c, label: c }))} />
            <Input.Search placeholder={t("搜索内容")} value={search}
              onChange={e => setSearch(e.target.value)} onSearch={() => { setPage(1); loadEntries(1); }} style={{ width: 200 }} />
          </Space>
          {/* 窄屏下表格在容器内横向滚动，避免整页被列宽撑破 */}
          <Table<KnowledgeEntry> dataSource={entries} rowKey="id" loading={loading}
            scroll={{ x: 'max-content' }}
            locale={{ emptyText: <Empty description={t("暂无知识条目")} /> }}
            size="small" onRow={r => ({ onClick: () => setEntryDetail(r), style: { cursor: 'pointer' } })}
            pagination={{ current: page, pageSize, total, showSizeChanger: true, showTotal: count => t("共 {value0} 条", { value0: count }),
              onChange: (p, ps) => { setPage(p); setPageSize(ps); loadEntries(p, ps); } }}
            columns={[
              { title: t("范围"), dataIndex: 'scope', width: 105,
                render: (scope: KnowledgeScope) => {
                  const meta = SCOPE_META[scope] || SCOPE_META.private;
                  return <Tag color={meta.color} icon={meta.icon}>{meta.label}</Tag>;
                } },
              { title: t("创建人"), dataIndex: 'owner_username', width: 100,
                render: (value: string) => value || t("系统") },
              { title: t("类别"), dataIndex: 'category', width: 90,
                render: v => v ? <Tag color={catColorMap[v] || 'default'}>{v}</Tag> : '-' },
              { title: t("源文件"), dataIndex: 'source_file', width: 140, ellipsis: true },
              { title: t("内容"), dataIndex: 'content', ellipsis: true,
                render: v => <Typography.Text style={{ fontSize: 13 }}>{v?.slice(0, 60) || '—'}</Typography.Text> },
              { title: t("来源"), dataIndex: 'source', width: 80,
                render: v => <Tag color={v === 'user_upload' ? 'orange' : 'green'}>{v || '-'}</Tag> },
              { title: t("标签"), dataIndex: 'tags', width: 150,
                render: (values: string[]) => {
                  const cleanValues = (values || []).map(value => String(value).trim()).filter(Boolean);
                  return cleanValues.length
                  ? <Space size={[0, 4]} wrap>{cleanValues.slice(0, 3).map(value => <Tag key={value}>{value}</Tag>)}</Space>
                  : '-';
                } },
              { title: '', key: 'actions', width: 36,
                render: (_: unknown, r: KnowledgeEntry) => (
                  r.scope === 'system' || r.source === 'auto_introspect' ? (
                    <Tooltip title={t("随表结构自动维护")}>
                      <Button size="small" danger disabled icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()} />
                    </Tooltip>
                  ) : !r.can_delete ? (
                    <Tooltip title={t("无删除权限")}>
                      <Button size="small" danger disabled icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()} />
                    </Tooltip>
                  ) : (
                    <Popconfirm title={t("确定删除？")} onConfirm={() => handleDeleteEntry(r.id)}
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
      key: 'docs', label: t("已索引文档"),
      children: (
        <div>
          <div style={{ marginBottom: 12 }}>
            <Space wrap>
              {scopeFilterControl}
              <input ref={fileRef} type="file" multiple accept=".md,.txt,.pdf,.docx,.doc,.markdown,.csv"
                style={{ display: 'none' }} onChange={e => handleFileSelect(e.target.files)} />
              <Button icon={<UploadOutlined />} loading={uploading}
                disabled={authRequired && !user}
                onClick={() => fileRef.current?.click()}>{t("上传文档")}</Button>
              {processingTasks.length > 0 && (
                <Space><LoadingOutlined style={{ color: '#1677ff' }} />
                  <Typography.Text type="secondary">
                    {processingTasks.filter(id => taskStates[id]?.status === 'processing').length} {t("个处理中")}
                  </Typography.Text>
                </Space>
              )}
            </Space>
          </div>
          <Table<KnowledgeDoc> dataSource={visibleDocs} rowKey={record => `${record.scope}:${record.name}`} size="small"
            scroll={{ x: 'max-content' }}
            locale={{ emptyText: <Empty description={t("文档目录为空")} /> }}
            onRow={r => ({ onClick: () => openDocDetail(r), style: { cursor: 'pointer' } })}
            columns={[
              { title: t("文件名"), dataIndex: 'name',
                render: (v: string) => <Space><FileTextOutlined /><span>{v}</span></Space> },
              { title: t("范围"), dataIndex: 'scope', width: 105,
                render: (scope: KnowledgeScope) => {
                  const meta = SCOPE_META[scope] || SCOPE_META.private;
                  return <Tag color={meta.color} icon={meta.icon}>{meta.label}</Tag>;
                } },
              { title: t("创建人"), dataIndex: 'owner_username', width: 100,
                render: (value: string) => value || t("系统") },
              { title: t("标签"), dataIndex: 'tag_ids', width: 160,
                render: (ids: number[]) => {
                  const names = (ids || []).map(id => tags.find(tag => tag.id === id)?.name).filter(Boolean);
                  return names.length
                    ? <Space size={[0, 4]} wrap>{names.slice(0, 3).map(name => <Tag key={name}>{name}</Tag>)}</Space>
                    : '-';
                } },
              { title: t("大小"), dataIndex: 'size', width: 80,
                render: v => `${(v / 1024).toFixed(1)} KB` },
              { title: t("修改时间"), dataIndex: 'modified', width: 140,
                render: v => v ? dayjs.unix(v).format('YYYY-MM-DD HH:mm') : '-' },
              { title: '', key: 'actions', width: 36,
                render: (_: unknown, r: KnowledgeDoc) => (
                  !r.can_delete ? (
                    <Tooltip title={t("无删除权限")}>
                      <Button size="small" danger disabled icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()} />
                    </Tooltip>
                  ) : (
                    <Popconfirm title={t("确定删除？")} onConfirm={() => handleDeleteDoc(r)}
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
      key: 'tags', label: <Space><TagsOutlined />{t("标签治理")}</Space>,
      children: (
        <div>
          <Space wrap style={{ marginBottom: 12 }}>
            <Input value={globalTagName} placeholder={t("标签名称")} maxLength={128}
              onChange={event => setGlobalTagName(event.target.value)}
              onPressEnter={() => { void createGlobalTag(); }} style={{ width: 180 }} />
            <Select value={globalTagGroup} onChange={setGlobalTagGroup}
              options={TAG_GROUP_OPTIONS} style={{ width: 130 }} />
            <Button type="primary" icon={<PlusOutlined />} onClick={() => { void createGlobalTag(); }}>
              {t("新建全局标签")}
            </Button>
            <Input.Search placeholder={t("搜索标签")} onSearch={value => { void loadTags(value, true); }}
              style={{ width: 200 }} allowClear />
          </Space>
          <Table<KnowledgeTag> dataSource={tags} rowKey="id" size="small" loading={tagLoading}
            scroll={{ x: 'max-content' }}
            locale={{ emptyText: <Empty description={t("暂无标签")} /> }} pagination={{ pageSize: 20 }}
            columns={[
              { title: t("名称"), dataIndex: 'name', width: 180,
                render: (value: string, tag: KnowledgeTag) => <Space>
                  <Tag color={tag.scope === 'global' ? 'blue' : 'green'}>{value}</Tag>
                  {tag.is_seed && <Tag>{t("预置")}</Tag>}
                </Space> },
              { title: t("范围"), dataIndex: 'scope', width: 90,
                render: (value: string) => value === 'global' ? t("全局") : t("个人") },
              { title: t("分组"), dataIndex: 'tag_group', width: 120,
                render: (value: string) => TAG_GROUP_OPTIONS.find(item => item.value === value)?.label || value },
              { title: t("说明"), dataIndex: 'description', ellipsis: true, render: (value: string) => value || '-' },
              { title: t("状态"), dataIndex: 'is_active', width: 80,
                render: (active: boolean, tag: KnowledgeTag) => (
                  <Switch size="small" checked={active}
                    onChange={checked => { void updateTagStatus(tag.id, checked); }} />
                ) },
            ]} />
        </div>
      ),
    }] : []),
  ];

  return (
    <div className="app-page">
      <Card title={<Space><DatabaseOutlined />{t("知识库")}</Space>}
        extra={<Typography.Text type="secondary">{total} {t("条,")} {docs.length} {t("份文档")}</Typography.Text>}>
        <Tabs items={[...tabItems, { key: 'emergency', label: t("应急授权"), children: <EmergencyKnowledgePanel docs={docs} platform={role === 'super_admin'} /> }]} />
      </Card>

      {/* 知识条目详情 */}
      <Modal title={t("知识条目详情")} open={!!entryDetail}
        onCancel={() => setEntryDetail(null)} footer={null} width={600} maskClosable>
        {entryDetail && (<>
          <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }}>
            <Descriptions.Item label={t("范围")}>
              <Tag color={SCOPE_META[entryDetail.scope]?.color} icon={SCOPE_META[entryDetail.scope]?.icon}>
                {SCOPE_META[entryDetail.scope]?.label || entryDetail.scope}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t("类别")}><Tag color={catColorMap[entryDetail.category] || 'default'}>{entryDetail.category || '—'}</Tag></Descriptions.Item>
            <Descriptions.Item label={t("创建人")}>{entryDetail.owner_username || t("系统")}</Descriptions.Item>
            <Descriptions.Item label={t("源文件")}>{entryDetail.source_file || '—'}</Descriptions.Item>
            <Descriptions.Item label={t("来源")}><Tag color={entryDetail.source === 'user_upload' ? 'orange' : 'green'}>{entryDetail.source || '—'}</Tag></Descriptions.Item>
            <Descriptions.Item label={t("标签")}>
              {entryDetail.tags?.map(value => String(value).trim()).filter(Boolean).length
                ? entryDetail.tags.map(value => String(value).trim()).filter(Boolean).map(value => <Tag key={value}>{value}</Tag>)
                : '—'}
            </Descriptions.Item>
          </Descriptions>
          <Typography.Title level={5}>{t("内容")}</Typography.Title>
          <div style={{ whiteSpace: 'pre-wrap', maxHeight: 400, overflow: 'auto', fontSize: 13, lineHeight: 1.7, background: '#fafafa', padding: 12, borderRadius: 6 }}>
            {entryDetail.content || '—'}
          </div>
        </>)}
      </Modal>

      {/* 文档详情 — 展示完整内容 */}
      <Modal title={t("文档: {value0}", { value0: docDetail?.name || '' })} open={!!docDetail}
        onCancel={() => setDocDetail(null)} footer={null} width={700} maskClosable>
        {docDetail && (
          <>
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t("范围")}>
                <Tag color={SCOPE_META[docDetail.scope]?.color} icon={SCOPE_META[docDetail.scope]?.icon}>
                  {SCOPE_META[docDetail.scope]?.label || docDetail.scope}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t("创建人")}>{docDetail.owner_username || t("系统")}</Descriptions.Item>
              <Descriptions.Item label={t("大小")}>{(docDetail.size / 1024).toFixed(1)} KB</Descriptions.Item>
              <Descriptions.Item label={t("修改时间")}>{docDetail.modified ? dayjs.unix(docDetail.modified).format('YYYY-MM-DD HH:mm') : '—'}</Descriptions.Item>
              <Descriptions.Item label={t("标签")}>
                {(docDetail.tag_ids || []).map(id => {
                  const name = tags.find(tag => tag.id === id)?.name;
                  return name ? <Tag key={id}>{name}</Tag> : null;
                })}
              </Descriptions.Item>
            </Descriptions>
            <Typography.Title level={5}>{t("文件内容")}</Typography.Title>
            {docLoading ? (
              <div style={{ textAlign: 'center', padding: 32 }}><LoadingOutlined style={{ fontSize: 24 }} /></div>
            ) : docType === 'pdf' ? (
              docRawUrl ? (
                <iframe src={docRawUrl} style={{ width: '100%', height: 500, border: '1px solid #d9d9d9', borderRadius: 6 }}
                  title={docDetail.name} sandbox="" referrerPolicy="no-referrer" />
              ) : (
                <div style={{ padding: 16, color: '#999' }}>{t("无法加载 PDF")}</div>
              )
            ) : docType === 'word' ? (
              <div style={{ maxHeight: 450, overflow: 'auto', background: '#fafafa', padding: 12, borderRadius: 6 }}
                dangerouslySetInnerHTML={{ __html: sanitizedDocContent }} />
            ) : (
              <div style={{ whiteSpace: 'pre-wrap', maxHeight: 450, overflow: 'auto', fontSize: 13,
                lineHeight: 1.7, background: '#fafafa', padding: 12, borderRadius: 6 }}>
                {docContent || t("无法提取文本内容")}
              </div>
            )}
          </>
        )}
      </Modal>

      {/* 上传配置 */}
      <Modal title={t("上传文档")} open={cfgOpen} onOk={startUpload}
        onCancel={() => { setCfgOpen(false); setPendingFiles([]); }}
        okText={t("开始处理")} cancelText={t("取消")} width={500}>
        <div style={{ marginBottom: 16 }}>
          <Typography.Text type="secondary">{t("即将处理")} {pendingFiles.length} {t("个文件")}</Typography.Text>
          {pendingFiles.map((f, i) => <Tag key={i} style={{ margin: 2 }}>{f.name}</Tag>)}
        </div>
        <Space direction="vertical" style={{ width: '100%' }}>
          <div><Typography.Text strong>{t("知识范围")}</Typography.Text>
            <Select style={{ width: '100%' }} value={uploadCfg.scope} options={uploadScopeOptions}
              onChange={(scope: KnowledgeScope) => setUploadCfg(current => ({
                ...current,
                scope,
                tagIds: current.tagIds.filter(id => (
                  scope === 'private' || tags.find(tag => tag.id === id)?.scope === 'global'
                )),
              }))} /></div>
          <div><Typography.Text strong>{t("数据源")}</Typography.Text>
            <Select style={{ width: '100%' }} value={uploadCfg.datasource || undefined}
              options={datasourceOptions} showSearch allowClear
              placeholder={t("可选：绑定数据源")}
              filterOption={(input, option) => String(option?.value || '').toLowerCase().includes(input.toLowerCase())}
              onChange={value => setUploadCfg(current => ({ ...current, datasource: value || '' }))} /></div>
          <div><Typography.Text strong>{t("标签")}</Typography.Text>
            <Select mode="multiple" style={{ width: '100%' }} value={uploadCfg.tagIds}
              open={tagPanelOpen} onOpenChange={setTagPanelOpen}
              loading={tagLoading} showSearch filterOption={false} onSearch={handleTagSearch}
              onFocus={() => { void loadTags(); }}
              onChange={(tagIds: number[]) => setUploadCfg(current => ({ ...current, tagIds }))}
              options={selectableTags.map(tag => ({ value: tag.id, label: tag.name }))}
              placeholder={t("搜索并选择标签")}
              dropdownRender={() => (
                <div style={{ padding: 8, maxWidth: 420 }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, maxHeight: 240, overflow: 'auto' }}>
                    {selectableTags.map(tag => {
                      const checked = uploadCfg.tagIds.includes(tag.id);
                      return (
                        <div key={tag.id} role="button" tabIndex={0}
                          onClick={() => setUploadCfg(current => ({
                            ...current,
                            tagIds: checked
                              ? current.tagIds.filter(id => id !== tag.id)
                              : [...current.tagIds, tag.id],
                          }))}
                          onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.click(); }}
                          style={{
                            position: 'relative', cursor: 'pointer', userSelect: 'none',
                            padding: '4px 10px', borderRadius: 6, fontSize: 12,
                            border: `1px solid ${checked ? '#91caff' : '#d9d9d9'}`,
                            background: checked ? '#e6f4ff' : '#fff',
                            color: checked ? '#1677ff' : '#333',
                          }}>
                          {tag.name}{tag.scope === 'private' ? t(" · 个人") : ''}
                          {checked && (
                            <CheckOutlined style={{ position: 'absolute', right: 2, bottom: 2, fontSize: 9 }} />
                          )}
                        </div>
                      );
                    })}
                  </div>
                  <Space.Compact style={{ width: '100%', marginTop: 8 }}>
                    <Input value={personalTagName} maxLength={128} placeholder={t("新建个人标签")}
                      disabled={!user}
                      onChange={event => setPersonalTagName(event.target.value)}
                      onPressEnter={() => { void createPersonalTag(); }} />
                    <Button icon={<PlusOutlined />} disabled={!user || !personalTagName.trim()}
                      onClick={() => { void createPersonalTag(); }}>{t("新建")}</Button>
                  </Space.Compact>
                </div>
              )} /></div>
          <div><Typography.Text strong>{t("知识类别")}</Typography.Text>
            <Input placeholder={t("如 sales_metrics")} value={uploadCfg.category}
              onChange={e => setUploadCfg(p => ({ ...p, category: e.target.value }))} /></div>
          <div><Typography.Text strong>{t("分割策略")}</Typography.Text>
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
          <div><Typography.Text strong>{t("块大小")}</Typography.Text>
            {/* antd Slider 根节点没有宽度声明，轴条与手柄均为绝对定位不贡献内容宽度；
                若直接放进 flex 容器，flex 项按内容宽度计算会被压成 0（只剩一个圆点），
                因此必须由 flex:1 + minWidth:0 的包裹层承担宽度，Slider 自身再撑满 100%。 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
              <div style={{ flex: '1 1 0', minWidth: 0 }}>
                <Slider style={{ width: '100%' }} min={200} max={4000} step={100} value={uploadCfg.chunkSize}
                  onChange={value => setUploadCfg(p => {
                    const maxOverlap = Math.min(500, value - 1);
                    return { ...p, chunkSize: value, chunkOverlap: Math.min(p.chunkOverlap, maxOverlap) };
                  })} />
              </div>
              <InputNumber min={200} max={4000} step={100} style={{ width: 96, flexShrink: 0 }} value={uploadCfg.chunkSize}
                onChange={value => setUploadCfg(p => {
                  const chunkSize = value || 800;
                  const maxOverlap = Math.min(500, chunkSize - 1);
                  return { ...p, chunkSize, chunkOverlap: Math.min(p.chunkOverlap, maxOverlap) };
                })} /></div></div>
          <div><Typography.Text strong>{t("重叠")}</Typography.Text>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
              <div style={{ flex: '1 1 0', minWidth: 0 }}>
                <Slider style={{ width: '100%' }} min={0} max={Math.min(500, uploadCfg.chunkSize - 1)} step={10}
                  value={uploadCfg.chunkOverlap}
                  onChange={value => setUploadCfg(p => ({ ...p, chunkOverlap: value }))} />
              </div>
              <InputNumber min={0} max={Math.min(500, uploadCfg.chunkSize - 1)} step={10} style={{ width: 96, flexShrink: 0 }}
                value={uploadCfg.chunkOverlap}
                onChange={value => setUploadCfg(p => ({ ...p, chunkOverlap: value || 0 }))} /></div></div>
        </Space>
      </Modal>
    </div>
  );
}
