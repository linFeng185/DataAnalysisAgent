import { t, useLanguage } from '../i18n';
import { useState, useEffect, useRef } from 'react';
import { Card, Table, Input, Tag, Drawer, Typography, message, Space, Select, Button } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { get } from '../api/client';
import type { TableInfo, ColumnInfo, DatasourceConfig } from '../types';
import { fetchDatasourceCatalog } from '../api/datasourceCatalog';

// 方法作用：只读浏览表结构，所有同步及终止操作统一放在数据源管理页面。
// Args: 无。
// Returns: 表结构列表和详情抽屉。
export default function SchemaPage() {
  useLanguage();
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [datasource, setDatasource] = useState('');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [selectedTable, setSelectedTable] = useState<TableInfo | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [expandedColumns, setExpandedColumns] = useState<Record<string, ColumnInfo[]>>({});
  const [reloadKey, setReloadKey] = useState(0);
  const sequence = useRef(0);

  // 方法作用：读取当前身份可见的数据源目录。
  // Args: 无。
  // Returns: 卸载后忽略迟到响应。
  useEffect(() => {
    let active = true;
    fetchDatasourceCatalog().then(items => {
      if (!active) return;
      setDatasources(items);
      setDatasource(items[0]?.name || '');
    }).catch(() => { if (active) message.warning(t("无法加载数据源列表")); });
    return () => { active = false; };
  }, []);

  // 方法作用：手动触发一次表结构重新拉取，用于同步完成后刷新页面数据。
  // Args: 无。
  // Returns: 无返回值。
  const reload = (): void => setReloadKey(key => key + 1);

  // 方法作用：展开表行时按需加载该表的列详情，已加载过的不重复请求。
  // Args: record - 被展开的表摘要。
  // Returns: 请求完成后无返回值。
  const loadColumns = async (record: TableInfo): Promise<void> => {
    if (expandedColumns[record.name]) return;
    try {
      const params = new URLSearchParams({ datasource });
      const detail = await get<TableInfo>(`/schema/tables/${encodeURIComponent(record.name)}?${params}`);
      setExpandedColumns(prev => ({ ...prev, [record.name]: detail.columns || [] }));
    } catch { message.error(t("加载字段失败")); }
  };

  // 方法作用：按服务端分页读取表摘要；列详情延迟到展开时再取。
  // Args: 无。
  // Returns: 卸载或切换时使旧请求失效。
  useEffect(() => {
    const current = ++sequence.current;
    setSelectedTable(null);
    setTables([]);
    setExpandedColumns({});
    if (!datasource) { setLoading(false); return; }
    setLoading(true);
    const params = new URLSearchParams({
      datasource, search: query, cached_only: 'true',
      page: String(page), page_size: String(pageSize),
    });
    get<{ tables: TableInfo[]; total: number }>(`/schema/tables?${params}`).then(result => {
      if (sequence.current !== current) return;
      setTables(result.tables || []);
      setTotal(result.total || 0);
    }).catch(() => {
      if (sequence.current === current) message.error(t("加载表结构失败"));
    }).finally(() => {
      if (sequence.current === current) setLoading(false);
    });
    return () => { sequence.current += 1; };
  }, [datasource, query, page, pageSize, reloadKey]);

  const columns = [
    { title: t("字段"), dataIndex: 'name', width: 150 },
    { title: t("类型"), dataIndex: 'type', width: 120, render: (value: string) => <Tag>{value}</Tag> },
    { title: t("注释"), dataIndex: 'comment', ellipsis: true },
    { title: t("可空"), dataIndex: 'is_nullable', width: 70, render: (value: boolean) => value ? <Tag color="orange">{t("是")}</Tag> : <Tag>{t("否")}</Tag> },
    { title: t("主键"), dataIndex: 'is_primary_key', width: 70, render: (value: boolean) => value && <Tag color="blue">PK</Tag> },
    { title: t("索引"), dataIndex: 'is_indexed', width: 70, render: (value: boolean) => value && <Tag color="green">{t("索引")}</Tag> },
  ];

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Card title={t("表结构浏览")} extra={
        <Space wrap>
          <Select style={{ width: 180 }} value={datasource}
            onChange={value => { setPage(1); setDatasource(value); }}
            options={datasources.map(source => ({ value: source.name, label: `${source.name} (${source.dialect})` }))}
            placeholder={t("选择数据源")} />
          <Input.Search placeholder={t("搜索表名")} value={search} onChange={event => setSearch(event.target.value)} onSearch={value => { setPage(1); setQuery(value); }} style={{ width: 200 }} />
          <Button icon={<ReloadOutlined />} loading={loading} onClick={reload}>{t("刷新")}</Button>
        </Space>
      }>
        <Typography.Paragraph type="secondary">{t("请在数据源管理页面同步表结构，此处仅供查看")}</Typography.Paragraph>
        <Table<TableInfo> dataSource={tables} rowKey="name" loading={loading}
          locale={{ emptyText: t("该数据源还没有可浏览的表结构，请先在数据源管理页面完成一次同步") }}
          pagination={{
            current: page, pageSize, total, showSizeChanger: true,
            onChange: (nextPage, nextSize) => { setPage(nextPage); setPageSize(nextSize); },
          }}
          onRow={record => ({ onClick: () => { setSelectedTable(record); void loadColumns(record); }, style: { cursor: 'pointer' } })}
          expandable={{
            onExpand: (expanded: boolean, record: TableInfo) => { if (expanded) void loadColumns(record); },
            expandedRowRender: (record: TableInfo) => (
              <Table<ColumnInfo> dataSource={expandedColumns[record.name] || []} rowKey="name" size="small"
                pagination={false} columns={columns}
                locale={{ emptyText: t("正在加载字段…") }} />
            ),
          }} columns={[
            { title: t("表名"), dataIndex: 'name', key: 'name', ellipsis: true },
            { title: t("描述"), dataIndex: 'description', key: 'description', ellipsis: true },
            { title: t("字段数"), dataIndex: 'column_count', key: 'cols', width: 80 },
          ]} />
      </Card>
      <Drawer title={selectedTable ? t("表详情: {value0}", { value0: selectedTable.name }) : ''}
        open={selectedTable !== null} onClose={() => setSelectedTable(null)} width={720}>
        {selectedTable && <>
          <Typography.Title level={5}>{t("描述")}</Typography.Title>
          <Typography.Paragraph>{selectedTable.description || t("无描述")}</Typography.Paragraph>
          {(selectedTable.relations || []).map((relation, index) => <Tag key={index} color="purple">→ {relation.target_table} ON {relation.join_key}</Tag>)}
          <Typography.Title level={5}>{t("字段列表")}</Typography.Title>
          <Table<ColumnInfo> dataSource={expandedColumns[selectedTable.name] || []} rowKey="name" size="small"
            pagination={false} columns={columns} locale={{ emptyText: t("正在加载字段…") }} />
        </>}
      </Drawer>
    </div>
  );
}
