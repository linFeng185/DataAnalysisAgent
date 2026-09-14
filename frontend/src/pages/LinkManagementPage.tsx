import { t, useLanguage } from '../i18n';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Drawer, Input, Popconfirm, Select, Space, Table, Tag, Typography, message } from 'antd';
import type { TableColumnsType } from 'antd';
import { EyeOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { ApiError } from '../api/client';
import { batchRevokeReportShares, fetchReportShareLinks, fetchReportShareVisits } from '../api/reportWorkbench';
import type { ReportShareLinkStatus, ReportShareLinkSummary, ReportShareVisit } from '../types/report';

/** 报表 ID 形态（uuid）；用于判断筛选输入能否下推成服务端 report_id 过滤。 */
const REPORT_ID_PATTERN = /^[0-9a-f][0-9a-f-]{7,}$/i;

// 方法作用：把秒数格式化为「N 分 M 秒」，不足一分钟只显示「M 秒」。
// Args: totalSeconds - 累计停留秒数。
// Returns: 用户可读的时长文本。
function formatDuration(totalSeconds: number): string {
  const safeSeconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
  const minutes = Math.floor(safeSeconds / 60);
  if (minutes <= 0) return t("{value0} 秒", { value0: safeSeconds });
  return t("{value0} 分 {value1} 秒", { value0: minutes, value1: safeSeconds % 60 });
}

// 方法作用：把后端时间戳格式化为本地时间，缺失时用占位符避免出现空白单元格。
// Args: value - ISO 时间字符串或空值。
// Returns: 本地时间文本或占位符。
function formatMoment(value: string | null | undefined): string {
  if (!value) return '—';
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return '—';
  return new Date(timestamp).toLocaleString();
}

// 方法作用：把链接管理接口错误转换为用户可读文案，403 单独说明权限不足。
// Args: error - 捕获到的异常；fallback - 无可用消息时的回退文案。
// Returns: 展示给用户的中文错误消息。
function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.status === 403) return t("当前账号没有链接管理权限");
  return error instanceof Error && error.message ? error.message : fallback;
}

// 方法作用：展示并管理本租户的分享链接，含访问量、停留时长与批量取消分享。
// Args: 无。
// Returns: 链接管理页面。
export default function LinkManagementPage() {
  useLanguage();
  const [shares, setShares] = useState<ReportShareLinkSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [status, setStatus] = useState<'all' | ReportShareLinkStatus>('all');
  const [reportFilter, setReportFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [revoking, setRevoking] = useState(false);
  const [visitsOpen, setVisitsOpen] = useState(false);
  const [visitsShare, setVisitsShare] = useState<ReportShareLinkSummary | null>(null);
  const [visits, setVisits] = useState<ReportShareVisit[]>([]);
  const [visitsLoading, setVisitsLoading] = useState(false);
  const [visitsError, setVisitsError] = useState('');

  // 关联报表筛选的取舍：本页只有文本输入，没有「标题 → 报表 ID」的查询接口。
  // 因此输入形如报表 ID 时下推为服务端 report_id 过滤；否则退化为按报表标题过滤当前页，
  // 并在筛选区明确提示这是本地过滤，避免用户误以为过滤是全量的。
  const serverReportId = REPORT_ID_PATTERN.test(reportFilter.trim()) ? reportFilter.trim() : '';
  const titleKeyword = serverReportId ? '' : reportFilter.trim().toLowerCase();

  // 方法作用：按当前状态、报表筛选与分页读取分享链接列表。
  // Args: 无，读取组件内筛选状态。
  // Returns: 异步读取完成后无返回值。
  const loadShares = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError('');
    try {
      const result = await fetchReportShareLinks({
        report_id: serverReportId || undefined,
        status: status === 'all' ? undefined : status,
        page,
        page_size: pageSize,
      });
      setShares(result.shares || []);
      setTotal(result.total || 0);
    } catch (loadError) {
      setShares([]);
      setTotal(0);
      setError(errorText(loadError, t("链接列表加载失败")));
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, serverReportId, status]);

  useEffect(() => { void loadShares(); }, [loadShares]);

  // 方法作用：读取单条分享链接的最近访问明细，供抽屉展示系统用户与停留时长。
  // Args: share - 目标分享链接摘要。
  // Returns: 异步读取完成后无返回值。
  const loadVisits = async (share: ReportShareLinkSummary | null): Promise<void> => {
    if (!share) return;
    setVisitsLoading(true);
    setVisitsError('');
    try {
      const result = await fetchReportShareVisits(share.id);
      setVisits(result.visits || []);
    } catch (loadError) {
      setVisits([]);
      setVisitsError(errorText(loadError, t("最近访问加载失败")));
    } finally {
      setVisitsLoading(false);
    }
  };

  // 方法作用：打开最近访问抽屉并加载该链接的访问明细。
  // Args: share - 目标分享链接摘要。
  // Returns: 异步读取完成后无返回值。
  const openVisits = async (share: ReportShareLinkSummary): Promise<void> => {
    setVisitsShare(share);
    setVisitsOpen(true);
    await loadVisits(share);
  };

  // 方法作用：批量（含单条）取消分享链接，并回显服务端返回的取消与跳过数量。
  // Args: shareIds - 要取消的分享链接 ID 列表。
  // Returns: 异步撤销完成后无返回值。
  const revokeShares = async (shareIds: string[]): Promise<void> => {
    if (shareIds.length === 0) return;
    setRevoking(true);
    try {
      // 单条取消同样走批量接口：可撤销性判定只有一套语义，重试也不会报错。
      const result = await batchRevokeReportShares(shareIds);
      message.success(t("已取消 {value0} 条分享，跳过 {value1} 条", { value0: result.revoked, value1: result.skipped }));
      setSelectedKeys([]);
      await loadShares();
    } catch (revokeError) {
      message.error(errorText(revokeError, t("批量取消失败")));
    } finally {
      setRevoking(false);
    }
  };

  // 标题过滤只在服务端过滤未生效时对本页数据生效。
  const visibleShares = useMemo(
    () => (titleKeyword ? shares.filter(item => `${item.report_title || ''}${item.report_id}`.toLowerCase().includes(titleKeyword)) : shares),
    [shares, titleKeyword],
  );

  const statusOptions = [
    { value: 'all', label: t("全部") },
    { value: 'active', label: t("有效") },
    { value: 'revoked', label: t("已撤销") },
    { value: 'expired', label: t("已过期") },
  ];

  // 表格列覆盖用户要求的七类语义：报表标题、版本号、作用范围、授权码、有效时间、访问量与访问时长、操作。
  const columns: TableColumnsType<ReportShareLinkSummary> = [
    {
      title: t("关联的报表与页面信息"),
      dataIndex: 'report_title',
      render: (_, share) => <Space direction="vertical" size={2}>
        <Typography.Text strong>{share.report_title || share.report_id}</Typography.Text>
        <Space wrap size={4}>
          <Tag>{t("版本")} {share.version}</Tag>
          <Tag color={share.scope === 'public' ? 'blue' : 'purple'}>{share.scope === 'public' ? t("公开") : t("指定用户")}</Tag>
          <Tag color={share.require_code ? 'orange' : 'default'}>{share.require_code ? t("需要授权码") : t("无需授权码")}</Tag>
        </Space>
      </Space>,
    },
    {
      title: t("有效时间"),
      dataIndex: 'created_at',
      render: (_, share) => <Space direction="vertical" size={2}>
        <Typography.Text type="secondary">{t("生效时间")} {formatMoment(share.created_at)}</Typography.Text>
        {share.revoked_at ? <Tag color="red">{t("已撤销")} {formatMoment(share.revoked_at)}</Tag>
          : share.expires_at
            ? (new Date(share.expires_at).getTime() <= Date.now()
              ? <Tag color="red">{t("已过期")} {formatMoment(share.expires_at)}</Tag>
              : <Typography.Text>{t("失效时间")} {formatMoment(share.expires_at)}</Typography.Text>)
            : <Tag color="green">{t("永久有效")}</Tag>}
      </Space>,
    },
    {
      title: t("访问量与访问时长"),
      dataIndex: 'visitor_count',
      render: (_, share) => <Space direction="vertical" size={2}>
        {/* 系统用户与非系统用户访问量必须分开显示，租户关闭身份记录时前者恒为 0 */}
        <Typography.Text>{t("系统用户 {value0} · 非系统用户 {value1}", { value0: share.system_visitor_count, value1: share.anonymous_visitor_count })}</Typography.Text>
        <Typography.Text type="secondary">{t("总时长")} {formatDuration(share.total_duration_seconds)} / {t("平均时长")} {formatDuration(share.average_duration_seconds)}</Typography.Text>
      </Space>,
    },
    {
      title: t("操作"),
      key: 'actions',
      width: 220,
      render: (_, share) => <Space wrap onClick={event => event.stopPropagation()}>
        <Button type="link" size="small" icon={<EyeOutlined />} onClick={() => void openVisits(share)}>{t("查看最近访问")}</Button>
        <Popconfirm title={t("确认取消此分享？")} okText={t("撤销")} cancelText={t("取消")} onConfirm={() => void revokeShares([share.id])}>
          <Button type="link" size="small" danger disabled={Boolean(share.revoked_at)}>{t("取消分享")}</Button>
        </Popconfirm>
      </Space>,
    },
  ];

  const visitColumns: TableColumnsType<ReportShareVisit> = [
    { title: t("系统用户"), dataIndex: 'username', render: (_, visit) => visit.user_id === null ? t("匿名访客") : (visit.username || `#${visit.user_id}`) },
    { title: t("开始访问时间"), dataIndex: 'started_at', render: value => formatMoment(value as string | null) },
    { title: t("最后活跃时间"), dataIndex: 'last_seen_at', render: value => formatMoment(value as string | null) },
    { title: t("停留时长"), dataIndex: 'duration_seconds', render: value => formatDuration(Number(value) || 0) },
  ];

  return <Space direction="vertical" size={16} className="app-page" style={{ width: '100%' }}>
    <Card>
      <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start" wrap>
        <Space direction="vertical" size={4}>
          <Typography.Title level={4} style={{ margin: 0 }}>{t("链接管理")}</Typography.Title>
          <Typography.Text type="secondary">{t("管理本租户的分享链接：查看访问量与停留时长、查看最近访问，并批量取消分享。")}</Typography.Text>
        </Space>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadShares()}>{t("刷新")}</Button>
      </Space>
    </Card>
    <Card size="small">
      <Space wrap align="center">
        <Select<'all' | ReportShareLinkStatus>
          style={{ width: 150 }}
          value={status}
          options={statusOptions}
          onChange={next => { setStatus(next); setPage(1); }}
        />
        <Input
          allowClear
          style={{ width: 260 }}
          prefix={<SearchOutlined />}
          value={reportFilter}
          placeholder={t("输入报表标题或报表 ID")}
          onChange={event => { setReportFilter(event.target.value); setPage(1); }}
        />
        <Select<number>
          style={{ width: 130 }}
          value={pageSize}
          options={[10, 20, 50].map(size => ({ value: size, label: t("每页 {value0} 条", { value0: size }) }))}
          onChange={next => { setPageSize(next); setPage(1); }}
        />
        <Popconfirm
          title={t("确认取消所选 {value0} 条分享链接？", { value0: selectedKeys.length })}
          okText={t("撤销")}
          cancelText={t("取消")}
          disabled={selectedKeys.length === 0}
          onConfirm={() => void revokeShares(selectedKeys)}
        >
          <Button danger disabled={selectedKeys.length === 0} loading={revoking}>{t("批量取消分享")}{selectedKeys.length > 0 ? ` (${selectedKeys.length})` : ''}</Button>
        </Popconfirm>
        <Typography.Text type="secondary">{t("共 {value0} 条", { value0: total })}</Typography.Text>
      </Space>
      {titleKeyword && <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
        {t("当前按报表标题在本页内过滤；输入报表 ID 可精确筛选全量链接")}
      </Typography.Text>}
    </Card>
    {error && <Alert
      type="error"
      showIcon
      message={error}
      action={<Button icon={<ReloadOutlined />} onClick={() => void loadShares()}>{t("重试")}</Button>}
    />}
    <Table<ReportShareLinkSummary>
      size="small"
      rowKey="id"
      loading={loading}
      scroll={{ x: 'max-content' }}
      dataSource={visibleShares}
      columns={columns}
      locale={{ emptyText: t("暂无分享链接") }}
      rowSelection={{
        selectedRowKeys: selectedKeys,
        // 已撤销的链接不再允许选择，避免批量取消时把无效项混进结果提示。
        getCheckboxProps: share => ({ disabled: Boolean(share.revoked_at) }),
        onChange: keys => setSelectedKeys(keys.map(key => String(key))),
      }}
      pagination={{
        current: page,
        pageSize,
        total,
        showSizeChanger: false,
        onChange: (nextPage, nextPageSize) => { setPage(nextPage); setPageSize(nextPageSize); },
      }}
      onRow={share => ({ onClick: () => void openVisits(share), style: { cursor: 'pointer' } })}
    />
    <Drawer
      title={t("最近访问：{value0}", { value0: visitsShare?.report_title || visitsShare?.report_id || '' })}
      width={620}
      open={visitsOpen}
      destroyOnHidden
      onClose={() => { setVisitsOpen(false); setVisitsShare(null); setVisits([]); setVisitsError(''); }}
    >
      {visitsError
        ? <Alert type="error" showIcon message={visitsError} action={<Button icon={<ReloadOutlined />} onClick={() => void loadVisits(visitsShare)}>{t("重试")}</Button>} />
        : <Table<ReportShareVisit>
          size="small"
          rowKey={(visit, index) => `${visit.user_id ?? 'anonymous'}-${visit.started_at ?? ''}-${index ?? 0}`}
          loading={visitsLoading}
          dataSource={visits}
          columns={visitColumns}
          locale={{ emptyText: t("暂无访问记录") }}
          pagination={false}
        />}
    </Drawer>
  </Space>;
}
