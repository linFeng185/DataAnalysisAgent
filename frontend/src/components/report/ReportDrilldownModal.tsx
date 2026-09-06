import { t, useLanguage } from '../../i18n';
import { useEffect, useMemo } from 'react';
import { Empty, Modal, Table, Typography } from 'antd';
import type { ReportDrilldownResponse } from '../../types/report';

interface ReportDrilldownModalProps {
  open: boolean;
  loading: boolean;
  title: string;
  result: ReportDrilldownResponse | null;
  error: string;
  onClose: () => void;
  onPageChange?: (page: number, pageSize: number) => void;
}
// 方法作用：展示服务端返回的有界 Drilldown 明细，并保持分页请求由父组件控制。
// Args: props - 弹窗开关、加载状态、结果和分页回调。
// Returns: Drilldown 明细弹窗。
export default function ReportDrilldownModal({ open, loading, title, result, error, onClose, onPageChange }: ReportDrilldownModalProps) {
  useLanguage();
  const columns = useMemo(() => (result?.columns || []).map(column => ({
    title: column,
    dataIndex: column,
    key: column,
    ellipsis: true,
    render: (value: unknown) => value === null || value === undefined ? '—' : String(value),
  })), [result?.columns]);

  useEffect(() => {
    if (!open) return;
    // 弹窗打开时将滚动位置交给表格自身，避免页面背景跳动。
    document.body.classList.add('report-drilldown-open');
    return () => document.body.classList.remove('report-drilldown-open');
  }, [open]);

  return <Modal title={title} open={open} onCancel={onClose} footer={null} width={900} destroyOnHidden>
    {error && <Typography.Text type="danger">{error}</Typography.Text>}
    {!error && result && result.rows.length > 0 ? <Table
      size="small"
      loading={loading}
      dataSource={result.rows}
      columns={columns}
      rowKey={(_, index) => String(index)}
      scroll={{ x: 'max-content' }}
      pagination={{
        current: result.page,
        pageSize: result.page_size,
        total: result.total,
        showSizeChanger: true,
        onChange: (page, pageSize) => onPageChange?.(page, pageSize),
      }}
    /> : !error && <Empty description={loading ? t("明细加载中") : t("暂无符合条件的明细")} />}
  </Modal>;
}
