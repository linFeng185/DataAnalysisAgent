import { useState, useEffect, useRef, useMemo } from 'react';
import { Card, Table, Tag, Tabs, Typography, Empty, message, Input, Button, Space, Select, InputNumber, Modal, notification, Popconfirm, Descriptions } from 'antd';
import { UploadOutlined, FileTextOutlined, DatabaseOutlined, LoadingOutlined, DeleteOutlined } from '@ant-design/icons';
import { get } from '../api/client';
import type { KnowledgeEntry, KnowledgeDoc } from '../types';
import dayjs from 'dayjs';

const STRATEGY_OPTIONS = [
  { value: 'auto', label: '自动' }, { value: 'heading', label: '按标题' },
  { value: 'paragraph', label: '按段落' }, { value: 'fixed', label: '固定长度' },
];
const CAT_COLORS = ['blue', 'green', 'orange', 'purple', 'cyan', 'magenta', 'gold', 'lime', 'geekblue', 'volcano'];

export default function KnowledgePage() {
  const [entries, setEntries] = useState<KnowledgeEntry[]>([]);
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [search, setSearch] = useState('');
  const [catFilter, setCatFilter] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadCfg, setUploadCfg] = useState({ strategy: 'auto', chunkSize: 800, chunkOverlap: 100, category: '' });
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
  const fileRef = useRef<HTMLInputElement>(null);

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

  const loadEntries = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (search) params.set('search', search);
      if (catFilter) params.set('category', catFilter);
      const data = await get<{ entries: KnowledgeEntry[] }>(`/knowledge?${params.toString()}`);
      setEntries(data.entries || []);
    } catch { message.error('加载知识库失败'); }
    setLoading(false);
  };

  const loadDocs = async () => {
    try {
      const data = await get<{ docs: KnowledgeDoc[] }>('/knowledge/docs');
      setDocs(data.docs || []);
    } catch { /* empty */ }
  };

  const reloadAll = async () => { await loadEntries(); await loadDocs(); };
  useEffect(() => { reloadAll(); }, []);

  const handleDeleteEntry = async (id: string) => {
    await fetch(`/api/v1/knowledge/${encodeURIComponent(id)}`, { method: 'DELETE' });
    message.success('已删除'); loadEntries();
  };

  const handleDeleteDoc = async (name: string) => {
    await fetch(`/api/v1/knowledge/docs/${encodeURIComponent(name)}`, { method: 'DELETE' });
    message.success(`${name} 已删除`); reloadAll();
  };

  const openDocDetail = async (d: KnowledgeDoc) => {
    setDocDetail(d); setDocContent('加载中...'); setDocLoading(true);
    setDocType('text'); setDocRawUrl('');
    try {
      const res = await fetch(`/api/v1/knowledge/docs/${encodeURIComponent(d.name)}/content`);
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
        const res = await fetch('/api/v1/knowledge/upload/status');
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
        chunk_size: String(uploadCfg.chunkSize), chunk_overlap: String(uploadCfg.chunkOverlap) });
      if (uploadCfg.category) params.set('category', uploadCfg.category);
      const res = await fetch(`/api/v1/knowledge/docs/upload?${params.toString()}`, { method: 'POST', body: form });
      const data = await res.json();
      if (data.tasks?.length > 0) {
        setTaskIds(data.tasks.map((t: { task_id: string }) => t.task_id));
        message.info(`已接收 ${data.tasks.length} 个文件，后台处理中`);
      }
      data.errors?.forEach((e: { file: string; error: string }) => message.error(`${e.file}: ${e.error}`));
      setPendingFiles([]);
    } catch { message.error('上传失败'); }
    setUploading(false);
  };

  const handleFileSelect = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setPendingFiles(Array.from(files)); setCfgOpen(true);
  };

  const processingTasks = taskIds.filter(id => {
    const s = taskStates[id]?.status;
    return s && s !== 'done' && s !== 'error';
  });

  const tabItems = [
    {
      key: 'entries', label: '知识条目',
      children: (
        <div>
          <Space style={{ marginBottom: 12 }}>
            <Select style={{ width: 130 }} value={catFilter} onChange={v => setCatFilter(v)}
              allowClear placeholder="全部类别" options={allCategories.map(c => ({ value: c, label: c }))} />
            <Input.Search placeholder="搜索内容" value={search}
              onChange={e => setSearch(e.target.value)} onSearch={loadEntries} style={{ width: 200 }} />
          </Space>
          <Table<KnowledgeEntry> dataSource={entries} rowKey="id" loading={loading}
            locale={{ emptyText: <Empty description="暂无知识条目" /> }}
            size="small" onRow={r => ({ onClick: () => setEntryDetail(r), style: { cursor: 'pointer' } })}
            columns={[
              { title: '类别', dataIndex: 'category', width: 90,
                render: v => v ? <Tag color={catColorMap[v] || 'default'}>{v}</Tag> : '-' },
              { title: '源文件', dataIndex: 'source_file', width: 140, ellipsis: true },
              { title: '内容', dataIndex: 'content', ellipsis: true,
                render: v => <Typography.Text style={{ fontSize: 13 }}>{v?.slice(0, 60) || '—'}</Typography.Text> },
              { title: '来源', dataIndex: 'source', width: 80,
                render: v => <Tag color={v === 'user_upload' ? 'orange' : 'green'}>{v || '-'}</Tag> },
              { title: '', key: 'actions', width: 36,
                render: (_: unknown, r: KnowledgeEntry) => (
                  r.is_builtin ? (
                    <Button size="small" danger disabled icon={<DeleteOutlined />}
                      onClick={e => e.stopPropagation()} title="系统条目不可删除" />
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
            <Space>
              <input ref={fileRef} type="file" multiple accept=".md,.txt,.pdf,.docx,.doc,.markdown"
                style={{ display: 'none' }} onChange={e => handleFileSelect(e.target.files)} />
              <Button icon={<UploadOutlined />} loading={uploading}
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
          <Table<KnowledgeDoc> dataSource={docs} rowKey="name" size="small"
            locale={{ emptyText: <Empty description="文档目录为空" /> }}
            onRow={r => ({ onClick: () => openDocDetail(r), style: { cursor: 'pointer' } })}
            columns={[
              { title: '文件名', dataIndex: 'name',
                render: (v: string) => <Space><FileTextOutlined /><span>{v}</span></Space> },
              { title: '大小', dataIndex: 'size', width: 80,
                render: v => `${(v / 1024).toFixed(1)} KB` },
              { title: '修改时间', dataIndex: 'modified', width: 140,
                render: v => v ? dayjs.unix(v).format('YYYY-MM-DD HH:mm') : '-' },
              { title: '', key: 'actions', width: 36,
                render: (_: unknown, r: KnowledgeDoc) => (
                  r.is_builtin ? (
                    <Button size="small" danger disabled icon={<DeleteOutlined />}
                      onClick={e => e.stopPropagation()} title="系统文档不可删除" />
                  ) : (
                    <Popconfirm title="确定删除？" onConfirm={() => handleDeleteDoc(r.name)}
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
            <Descriptions.Item label="类别"><Tag color={catColorMap[entryDetail.category] || 'default'}>{entryDetail.category || '—'}</Tag></Descriptions.Item>
            <Descriptions.Item label="源文件">{entryDetail.source_file || '—'}</Descriptions.Item>
            <Descriptions.Item label="来源"><Tag color={entryDetail.source === 'user_upload' ? 'orange' : 'green'}>{entryDetail.source || '—'}</Tag></Descriptions.Item>
            <Descriptions.Item label="类型">{entryDetail.is_builtin ? <Tag color="blue">系统</Tag> : <Tag color="orange">用户上传</Tag>}</Descriptions.Item>
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
              <Descriptions.Item label="大小">{(docDetail.size / 1024).toFixed(1)} KB</Descriptions.Item>
              <Descriptions.Item label="修改时间">{docDetail.modified ? dayjs.unix(docDetail.modified).format('YYYY-MM-DD HH:mm') : '—'}</Descriptions.Item>
              <Descriptions.Item label="类型">{docDetail.is_builtin ? <Tag color="blue">系统</Tag> : <Tag color="orange">用户上传</Tag>}</Descriptions.Item>
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
      <Modal title="文档分块配置" open={cfgOpen} onOk={startUpload}
        onCancel={() => { setCfgOpen(false); setPendingFiles([]); }}
        okText="开始处理" cancelText="取消" width={500}>
        <div style={{ marginBottom: 16 }}>
          <Typography.Text type="secondary">即将处理 {pendingFiles.length} 个文件</Typography.Text>
          {pendingFiles.map((f, i) => <Tag key={i} style={{ margin: 2 }}>{f.name}</Tag>)}
        </div>
        <Space direction="vertical" style={{ width: '100%' }}>
          <div><Typography.Text strong>知识类别</Typography.Text>
            <Input placeholder="如 sales_metrics" value={uploadCfg.category}
              onChange={e => setUploadCfg(p => ({ ...p, category: e.target.value }))} /></div>
          <div><Typography.Text strong>分割策略</Typography.Text>
            <Select style={{ width: '100%' }} value={uploadCfg.strategy}
              onChange={v => setUploadCfg(p => ({ ...p, strategy: v }))} options={STRATEGY_OPTIONS} /></div>
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
