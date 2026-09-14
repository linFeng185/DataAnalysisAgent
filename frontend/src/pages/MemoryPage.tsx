import { t, useLanguage } from '../i18n';
import { useEffect, useMemo, useState } from 'react';
import { Card, Table, Tag, Typography, Empty, message, Select, Input, Space, Modal, Popconfirm, Descriptions, Button } from 'antd';
import { BulbOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons';
import { get, del } from '../api/client';
import dayjs from 'dayjs';

const MEMORY_TYPE_OPTIONS = [
  { value: '', get label() { return t("全部类型"); } },
  { value: 'sql_template', get label() { return t("SQL 模板"); } },
  { value: 'user_preference', get label() { return t("用户偏好"); } },
  { value: 'learned_pattern', get label() { return t("学习模式"); } },
  { value: 'correction', get label() { return t("纠正记录"); } },
  { value: 'project_rule', get label() { return t("项目规则"); } },
];
const TYPE_COLORS: Record<string, string> = {
  sql_template: 'blue',
  user_preference: 'green',
  learned_pattern: 'purple',
  correction: 'orange',
  project_rule: 'cyan',
};

interface LongTermMemory {
  id: string;
  memory_type: string;
  scope: string;
  content: string;
  payload: { question?: string; sql?: string; datasource?: string; [key: string]: unknown };
  access_count: number;
  confidence: number;
  ttl_days: number | null;
  created_at: string | null;
  last_accessed_at: string | null;
  expires_at: string | null;
}

export default function MemoryPage() {
  const language = useLanguage();
  const [items, setItems] = useState<LongTermMemory[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [typeFilter, setTypeFilter] = useState('');
  const [search, setSearch] = useState('');
  const [detail, setDetail] = useState<LongTermMemory | null>(null);

  const typeLabel = useMemo(() => {
    const map: Record<string, string> = {};
    MEMORY_TYPE_OPTIONS.forEach(option => { if (option.value) map[option.value] = option.label; });
    return map;
  }, [language]);

  // 方法作用：按当前筛选与分页拉取本人长期记忆。
  // Args: p - 可选页码；ps - 可选每页条数。
  // Returns: 无返回值，失败时提示错误。
  const loadMemories = async (p?: number, ps?: number) => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        memory_type: typeFilter,
        search,
        page: String(p ?? page),
        page_size: String(ps ?? pageSize),
      });
      const data = await get<{ items: LongTermMemory[]; total: number }>(`/memory?${params.toString()}`);
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch { message.error(t("加载记忆失败")); }
    setLoading(false);
  };

  useEffect(() => { void loadMemories(); }, [typeFilter, page, pageSize, language]);

  // 方法作用：删除一条本人长期记忆并刷新列表。
  // Args: id - 记忆 ID。
  // Returns: 无返回值，失败时提示错误。
  const handleDelete = async (id: string) => {
    try {
      await del(`/memory/${encodeURIComponent(id)}`);
      message.success(t("已删除"));
      void loadMemories();
    } catch { message.error(t("删除失败")); }
  };

  return (
    <div className="app-page">
      <Card
        title={<Space><BulbOutlined />{t("我的记忆")}</Space>}
        extra={<Typography.Text type="secondary">{t("共 {value0} 条", { value0: total })}</Typography.Text>}>
        <Space wrap style={{ marginBottom: 12 }}>
          <Select style={{ width: 150 }} value={typeFilter}
            onChange={value => { setTypeFilter(value); setPage(1); }}
            options={MEMORY_TYPE_OPTIONS} />
          <Input.Search placeholder={t("搜索内容")} value={search} style={{ width: 220 }}
            onChange={event => setSearch(event.target.value)}
            onSearch={() => { setPage(1); void loadMemories(1); }} allowClear />
          <Button icon={<ReloadOutlined />} onClick={() => { void loadMemories(); }}>
            {t("刷新")}
          </Button>
        </Space>
        <Table<LongTermMemory> dataSource={items} rowKey="id" size="small" loading={loading}
          locale={{ emptyText: <Empty description={t("暂无长期记忆。成功执行的 SQL 和你的偏好会自动沉淀到这里。")} /> }}
          onRow={record => ({ onClick: () => setDetail(record), style: { cursor: 'pointer' } })}
          pagination={{
            current: page, pageSize, total, showSizeChanger: true,
            showTotal: count => t("共 {value0} 条", { value0: count }),
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          }}
          columns={[
            { title: t("类型"), dataIndex: 'memory_type', width: 120,
              render: (value: string) => (
                <Tag color={TYPE_COLORS[value] || 'default'}>{typeLabel[value] || value}</Tag>
              ) },
            { title: t("内容"), dataIndex: 'content', ellipsis: true,
              render: (value: string) => (
                <Typography.Text style={{ fontSize: 13 }}>
                  {(value || '').replace(/\s+/g, ' ').slice(0, 80) || '—'}
                </Typography.Text>
              ) },
            { title: t("访问次数"), dataIndex: 'access_count', width: 90 },
            { title: t("置信度"), dataIndex: 'confidence', width: 90,
              render: (value: number) => (value ?? 0).toFixed(2) },
            { title: t("创建时间"), dataIndex: 'created_at', width: 150,
              render: (value: string) => value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-' },
            { title: t("最后访问时间"), dataIndex: 'last_accessed_at', width: 150,
              render: (value: string) => value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-' },
            { title: '', key: 'actions', width: 36,
              render: (_: unknown, record: LongTermMemory) => (
                <Popconfirm title={t("确定删除？")} onConfirm={() => handleDelete(record.id)}
                  onPopupClick={event => event.stopPropagation()}>
                  <Button size="small" danger icon={<DeleteOutlined />}
                    onClick={event => event.stopPropagation()} />
                </Popconfirm>
              ) },
          ]} />
      </Card>

      {/* 记忆详情：完整内容与 payload，不支持在线编辑。 */}
      <Modal title={t("记忆详情")} open={!!detail} onCancel={() => setDetail(null)}
        footer={null} width={720} maskClosable>
        {detail && (<>
          <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
            <Descriptions.Item label={t("类型")}>
              <Tag color={TYPE_COLORS[detail.memory_type] || 'default'}>
                {typeLabel[detail.memory_type] || detail.memory_type}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t("访问次数")}>{detail.access_count}</Descriptions.Item>
            <Descriptions.Item label={t("置信度")}>{(detail.confidence ?? 0).toFixed(2)}</Descriptions.Item>
            <Descriptions.Item label={t("有效期（天）")}>{detail.ttl_days ?? '—'}</Descriptions.Item>
            <Descriptions.Item label={t("创建时间")}>
              {detail.created_at ? dayjs(detail.created_at).format('YYYY-MM-DD HH:mm') : '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t("过期时间")}>
              {detail.expires_at ? dayjs(detail.expires_at).format('YYYY-MM-DD HH:mm') : '—'}
            </Descriptions.Item>
          </Descriptions>
          {detail.payload?.question && (
            <>
              <Typography.Title level={5}>{t("问题")}</Typography.Title>
              <div style={{ fontSize: 13, lineHeight: 1.7, marginBottom: 12 }}>
                {String(detail.payload.question)}
              </div>
            </>
          )}
          {detail.payload?.sql && (
            <>
              <Typography.Title level={5}>SQL</Typography.Title>
              <pre style={{ maxHeight: 320, overflow: 'auto', fontSize: 12, lineHeight: 1.6,
                background: '#fafafa', padding: 12, borderRadius: 6 }}>
                {String(detail.payload.sql)}
              </pre>
            </>
          )}
          <Typography.Title level={5}>{t("内容")}</Typography.Title>
          <div style={{ whiteSpace: 'pre-wrap', maxHeight: 320, overflow: 'auto', fontSize: 13,
            lineHeight: 1.7, background: '#fafafa', padding: 12, borderRadius: 6 }}>
            {detail.content || '—'}
          </div>
        </>)}
      </Modal>
    </div>
  );
}
