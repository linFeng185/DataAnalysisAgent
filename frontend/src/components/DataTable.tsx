import { Table, Empty } from 'antd';

export default function DataTable({ data, maxRows = 100, pageSize = 20 }: {
  data: Record<string, unknown>[]; maxRows?: number; pageSize?: number;
}) {
  if (!data || data.length === 0) return <Empty description="无数据" />;
  const sliced = data.slice(0, maxRows);
  const columns = Object.keys(sliced[0] || {}).map(k => ({ title: k, dataIndex: k, key: k, ellipsis: true }));
  return (
    <Table dataSource={sliced} columns={columns} size="small" scroll={{ x: 'max-content' }}
      pagination={data.length > pageSize ? { pageSize, showSizeChanger: true, pageSizeOptions: ['10', '20', '50'] } : false}
      rowKey={(_, i) => String(i)} />
  );
}
