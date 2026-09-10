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

  // 方法作用：在数据源或搜索词变化时读取表结构，避免旧响应覆盖当前选择。
  // Args: 无。
  // Returns: 卸载或切换时使旧请求失效。
  // 方法作用：手动触发一次表结构重新拉取，用于同步完成后刷新页面数据。
  // Args: 无。
  // Returns: 无返回值。
  const reload = (): void => setReloadKey(key => key + 1);

  // 方法作用：在数据源、搜索词或手动刷新变化时读取表结构，避免旧响应覆盖当前选择。
  // Args: 无。
  // Returns: 卸载或切换时使旧请求失效。
  useEffect(() => {
    const current = ++sequence.current;
    setSelectedTable(null);
    setTables([]);
    if (!datasource) { setLoading(false); return; }
    setLoading(true);
    const params = new URLSearchParams({ datasource, search: query, cached_only: 'true' });
    get<{ tables: TableInfo[] }>(`/schema/tables?${params}`).then(result => {
      if (sequence.current === current) setTables(result.tables || []);
    }).catch(() => {
      if (sequence.current === current) message.error(t("加载表结构失败"));
    }).finally(() => {
      if (sequence.current === current) setLoading(false);
    });
    return () => { sequence.current += 1; };
  }, [datasource, query, reloadKey]);

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
          <Select style={{ width: 180 }} value={datasource} onChange={setDatasource}
            options={datasources.map(source => ({ value: source.name, label: `${source.name} (${source.dialect})` }))}
            placeholder={t("选择数据源")} />
          <Input.Search placeholder={t("搜索表名")} value={search} onChange={event => setSearch(event.target.value)} onSearch={setQuery} style={{ width: 200 }} />
          <Button icon={<ReloadOutlined />} loading={loading} onClick={reload}>{t("刷新")}</Button>
        </Space>
      }>
        <Typography.Paragraph type="secondary">{t("请在数据源管理页面同步表结构，此处仅供查看")}</Typography.Paragraph>
        <Table<TableInfo> dataSource={tables} rowKey="name" loading={loading}
          locale={{ emptyText: t("该数据源还没有可浏览的表结构，请先在数据源管理页面完成一次同步") }}
          onRow={record => ({ onClick: () => setSelectedTable(record), style: { cursor: 'pointer' } })}
          expandable={{ expandedRowRender: (record: TableInfo) => (
            <Table<ColumnInfo> dataSource={record.columns} rowKey="name" size="small" pagination={false} columns={columns} />
          ) }} columns={[
            { title: t("表名"), dataIndex: 'name', key: 'name', ellipsis: true },
            { title: t("描述"), dataIndex: 'description', key: 'description', ellipsis: true },
            { title: t("字段数"), key: 'cols', width: 80, render: (_: unknown, record: TableInfo) => record.columns?.length || 0 },
          ]} />
      </Card>
      <Drawer title={selectedTable ? t("表详情: {value0}", { value0: selectedTable.name }) : ''}
        open={selectedTable !== null} onClose={() => setSelectedTable(null)} width={720}>
        {selectedTable && <>
          <Typography.Title level={5}>{t("描述")}</Typography.Title>
          <Typography.Paragraph>{selectedTable.description || t("无描述")}</Typography.Paragraph>
          {(selectedTable.relations || []).map((relation, index) => <Tag key={index} color="purple">→ {relation.target_table} ON {relation.join_key}</Tag>)}
          <Typography.Title level={5}>{t("字段列表")}</Typography.Title>
          <Table<ColumnInfo> dataSource={selectedTable.columns} rowKey="name" size="small" pagination={false} columns={columns} />
        </>}
      </Drawer>
    </div>
  );
}
