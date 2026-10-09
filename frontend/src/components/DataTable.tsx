import { t, useLanguage } from '../i18n';
import { Table, Empty, Tag, Tooltip } from 'antd';
import type { MaskingMetadata } from '../types';

// 方法作用：按后端脱敏元数据展示表头标识，不从掩码内容推断。
// Args: data - 展示数据；masking - 脱敏元数据；maxRows/pageSize - 展示上限。
// Returns: 只读结果表格。
export default function DataTable({ data, masking, columnLabels = {}, maxRows = 100, pageSize = 20 }: {
  data: Record<string, unknown>[]; masking?: MaskingMetadata; columnLabels?: Record<string, string>; maxRows?: number; pageSize?: number;
}) {
  useLanguage();
  if (!data || data.length === 0) return <Empty description={t("无数据")} />;
  const sliced = data.slice(0, maxRows);
  const seen = new Set<string>();
  for (const row of sliced) {
    for (const key of Object.keys(row)) seen.add(key);
  }
  const maskedFields = new Map((masking?.fields || []).map(field => [field.column, field]));
  const columns = Array.from(seen).map(column => ({
    title: <><Tooltip title={column}>{columnLabels[column] || column}</Tooltip>{maskedFields.has(column) && <Tooltip title={t("已脱敏：{value0}", { value0: maskedFields.get(column)?.categories.join('、') })}><Tag color="gold" style={{ marginLeft: 6 }}>{t("已脱敏")}</Tag></Tooltip>}</>,
    dataIndex: column,
    key: column,
    ellipsis: true,
    render: (value: unknown) => value === null || value === undefined ? '—' : String(value),
  }));
  return (
    <Table dataSource={sliced} columns={columns} size="small" scroll={{ x: 'max-content' }}
      pagination={data.length > pageSize ? { pageSize, showSizeChanger: true, pageSizeOptions: ['10', '20', '50'] } : false}
      rowKey={(_, i) => String(i)} />
  );
}
