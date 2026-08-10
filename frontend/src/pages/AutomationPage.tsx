import { useEffect, useState } from 'react';
import {
  Alert, Button, Card, Checkbox, Empty, Form, Input, InputNumber, List, message,
  Modal, Popconfirm, Segmented, Select, Space, Switch, Table, Tabs, Tag, Tooltip,
  Typography,
} from 'antd';
import {
  BellOutlined, DeleteOutlined, HistoryOutlined, PlayCircleOutlined, PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { del, get, patch, post } from '../api/client';
import type {
  AutomationChannel, AutomationNotification, AutomationSchedule,
  AutomationRun, DatasourceConfig,
} from '../types';

const FREQUENCY_OPTIONS = [
  { value: 'hourly', label: '每小时' },
  { value: 'daily', label: '每天' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
];

const CHANNEL_OPTIONS = [
  { value: 'in_app', label: '站内' },
  { value: 'email', label: '邮件' },
  { value: 'feishu', label: '飞书' },
  { value: 'slack', label: 'Slack' },
];

const FREQUENCY_LABELS = Object.fromEntries(
  FREQUENCY_OPTIONS.map(option => [option.value, option.label]),
);

// 方法作用：展示当前身份的自动化任务、创建表单和站内通知。
// Args: 无。
// Returns: 主动洞察与定时报告管理页面。
export default function AutomationPage() {
  const [schedules, setSchedules] = useState<AutomationSchedule[]>([]);
  const [notifications, setNotifications] = useState<AutomationNotification[]>([]);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [loading, setLoading] = useState(false);
  const [notificationLoading, setNotificationLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [runningId, setRunningId] = useState('');
  const [runs, setRuns] = useState<AutomationRun[]>([]);
  const [runsOpen, setRunsOpen] = useState(false);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runsSchedule, setRunsSchedule] = useState<AutomationSchedule | null>(null);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const channels = Form.useWatch<AutomationChannel[]>('channels', form) || ['in_app'];

  // 方法作用：加载当前身份可见的任务和数据源选项。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const loadSchedules = async (): Promise<void> => {
    setLoading(true);
    try {
      const [scheduleData, datasourceData] = await Promise.all([
        get<{ schedules: AutomationSchedule[] }>('/automation/schedules'),
        get<{ datasources: DatasourceConfig[] }>('/datasources'),
      ]);
      setSchedules(scheduleData.schedules || []);
      setDatasources(datasourceData.datasources || []);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '自动化任务加载失败');
    } finally {
      setLoading(false);
    }
  };

  // 方法作用：加载当前用户最近的站内通知。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const loadNotifications = async (): Promise<void> => {
    setNotificationLoading(true);
    try {
      const data = await get<{ notifications: AutomationNotification[] }>(
        '/automation/notifications?limit=50',
      );
      setNotifications(data.notifications || []);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '通知加载失败');
    } finally {
      setNotificationLoading(false);
    }
  };

  useEffect(() => {
    void Promise.all([loadSchedules(), loadNotifications()]);
  }, []);

  // 方法作用：打开创建窗口并写入安全默认值。
  // Args: 无。
  // Returns: 无返回值。
  const openCreate = (): void => {
    form.resetFields();
    form.setFieldsValue({
      kind: 'insight', frequency: 'daily', threshold_pct: 10, channels: ['in_app'],
    });
    setOpen(true);
  };

  // 方法作用：校验表单并创建一个只读 SQL 自动化任务。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const createSchedule = async (): Promise<void> => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      await post('/automation/schedules', values);
      message.success('自动化任务已创建');
      setOpen(false);
      form.resetFields();
      await loadSchedules();
    } catch (error) {
      if (error instanceof Error) message.error(error.message || '创建失败');
    } finally {
      setSaving(false);
    }
  };

  // 方法作用：立即运行指定任务并刷新任务时间和通知列表。
  // Args: scheduleId - 待运行任务 UUID。
  // Returns: 请求完成后无返回值。
  const runSchedule = async (scheduleId: string): Promise<void> => {
    setRunningId(scheduleId);
    try {
      const result = await post<{ success: boolean; error?: string }>(
        `/automation/schedules/${scheduleId}/run`,
        {},
      );
      if (!result.success) throw new Error(result.error || '任务执行失败');
      message.success('任务执行完成');
      await Promise.all([loadSchedules(), loadNotifications()]);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '任务执行失败');
    } finally {
      setRunningId('');
    }
  };

  // 方法作用：删除指定任务及其关联运行记录和通知。
  // Args: scheduleId - 待删除任务 UUID。
  // Returns: 请求完成后无返回值。
  const deleteSchedule = async (scheduleId: string): Promise<void> => {
    try {
      await del(`/automation/schedules/${scheduleId}`);
      message.success('自动化任务已删除');
      await Promise.all([loadSchedules(), loadNotifications()]);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '删除失败');
    }
  };

  // 方法作用：暂停或恢复任务调度，明确反馈调度状态变化。
  // Args: scheduleId - 任务 UUID；enabled - 目标启用状态。
  // Returns: 请求完成后无返回值。
  const toggleSchedule = async (scheduleId: string, enabled: boolean): Promise<void> => {
    try {
      await patch(`/automation/schedules/${scheduleId}`, { enabled });
      message.success(enabled ? '任务已恢复' : '任务已暂停');
      await loadSchedules();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '任务状态更新失败');
    }
  };

  // 方法作用：读取指定任务的最近运行记录并打开可追溯详情窗口。
  // Args: schedule - 当前用户可见的自动化任务。
  // Returns: 请求完成后无返回值。
  const showRuns = async (schedule: AutomationSchedule): Promise<void> => {
    setRunsSchedule(schedule);
    setRunsOpen(true);
    setRunsLoading(true);
    try {
      const data = await get<{ runs: AutomationRun[] }>(
        `/automation/schedules/${schedule.id}/runs?limit=20`,
      );
      setRuns(data.runs || []);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '运行记录加载失败');
      setRuns([]);
    } finally {
      setRunsLoading(false);
    }
  };

  return (
    <div className="automation-page" style={{ maxWidth: 1180, margin: '0 auto' }}>
      <Card
        title="自动化"
        extra={(
          <Space>
            <Tooltip title="刷新">
              <Button aria-label="刷新自动化数据" icon={<ReloadOutlined />}
                onClick={() => void Promise.all([loadSchedules(), loadNotifications()])} />
            </Tooltip>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              创建自动化任务
            </Button>
          </Space>
        )}
      >
        <Alert
          type="info"
          showIcon
          message="自动化 = 按计划执行只读 SQL，并在数据变化或报告生成后通知你"
          description="主动洞察会比较本次与上次成功结果，只有变化超过阈值才通知；定时报告会按频率生成 Markdown 摘要。任务会在执行时重新校验数据源权限，可随时暂停。"
          style={{ marginBottom: 16 }}
        />
        <Tabs
          items={[
            {
              key: 'schedules',
              label: '任务',
              children: (
                <Table<AutomationSchedule>
                  dataSource={schedules}
                  rowKey="id"
                  loading={loading}
                  size="small"
                  scroll={{ x: 980 }}
                  locale={{ emptyText: <Empty description="暂无自动化任务" /> }}
                  columns={[
                    { title: '名称', dataIndex: 'name', width: 160, fixed: 'left', ellipsis: true },
                    { title: '类型', dataIndex: 'kind', width: 90,
                      render: value => <Tag color={value === 'insight' ? 'blue' : 'green'}>
                        {value === 'insight' ? '主动洞察' : '定时报告'}
                      </Tag> },
                    { title: '数据源', dataIndex: 'datasource', width: 130, ellipsis: true },
                    { title: '频率', dataIndex: 'frequency', width: 90,
                      render: value => FREQUENCY_LABELS[value] || value },
                    { title: '渠道', dataIndex: 'channels', width: 170,
                      render: (values: AutomationChannel[]) => values.map(value => (
                        <Tag key={value}>{CHANNEL_OPTIONS.find(item => item.value === value)?.label || value}</Tag>
                      )) },
                    { title: '下次运行', dataIndex: 'next_run_at', width: 156,
                      render: value => value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-' },
                    { title: '上次运行', dataIndex: 'last_run_at', width: 156,
                      render: value => value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-' },
                    { title: '调度', dataIndex: 'enabled', width: 80,
                      render: (value: boolean, schedule: AutomationSchedule) => (
                        <Tooltip title={value ? '点击暂停任务' : '点击恢复任务'}>
                          <Switch checked={value} size="small"
                            onChange={next => void toggleSchedule(schedule.id, next)} />
                        </Tooltip>
                      ) },
                    { title: '操作', key: 'actions', width: 132, fixed: 'right',
                      render: (_, schedule) => (
                        <Space size={4}>
                          <Tooltip title="运行记录">
                            <Button aria-label={`查看运行记录 ${schedule.name}`} size="small"
                              icon={<HistoryOutlined />} onClick={() => void showRuns(schedule)} />
                          </Tooltip>
                          <Tooltip title="立即运行">
                            <Button aria-label={`立即运行 ${schedule.name}`} size="small"
                              icon={<PlayCircleOutlined />} loading={runningId === schedule.id}
                              onClick={() => void runSchedule(schedule.id)} />
                          </Tooltip>
                          <Popconfirm title="确定删除此任务？"
                            onConfirm={() => void deleteSchedule(schedule.id)}>
                            <Tooltip title="删除">
                              <Button aria-label={`删除 ${schedule.name}`} size="small" danger
                                icon={<DeleteOutlined />} />
                            </Tooltip>
                          </Popconfirm>
                        </Space>
                      ) },
                  ]}
                />
              ),
            },
            {
              key: 'notifications',
              label: <span><BellOutlined /> 站内通知</span>,
              children: (
                <List<AutomationNotification>
                  loading={notificationLoading}
                  dataSource={notifications}
                  locale={{ emptyText: <Empty description="暂无通知" /> }}
                  renderItem={notification => (
                    <List.Item>
                      <List.Item.Meta
                        title={<Space wrap>
                          <Tag color={notification.kind === 'insight' ? 'blue' : 'green'}>
                            {notification.kind === 'insight' ? '洞察' : '报告'}
                          </Tag>
                          <Typography.Text strong>{notification.title}</Typography.Text>
                          <Typography.Text type="secondary">
                            {dayjs(notification.created_at).format('YYYY-MM-DD HH:mm')}
                          </Typography.Text>
                        </Space>}
                        description={<Typography.Paragraph className="automation-notification-body"
                          ellipsis={{ rows: 8, expandable: true, symbol: '展开' }}>
                          {notification.body}
                        </Typography.Paragraph>}
                      />
                    </List.Item>
                  )}
                />
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={runsSchedule ? `运行记录: ${runsSchedule.name}` : '运行记录'}
        open={runsOpen}
        onCancel={() => { setRunsOpen(false); setRunsSchedule(null); }}
        footer={null}
        width={820}
      >
        <Table<AutomationRun>
          rowKey="id"
          loading={runsLoading}
          dataSource={runs}
          size="small"
          locale={{ emptyText: <Empty description="暂无运行记录" /> }}
          columns={[
            { title: '开始时间', dataIndex: 'started_at', width: 170,
              render: value => value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-' },
            { title: '结束时间', dataIndex: 'finished_at', width: 170,
              render: value => value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-' },
            { title: '状态', dataIndex: 'status', width: 90,
              render: value => <Tag color={value === 'success' ? 'green' : 'red'}>
                {value === 'success' ? '成功' : '失败'}
              </Tag> },
            { title: '错误或摘要', key: 'summary', ellipsis: true,
              render: (_: unknown, run: AutomationRun) => run.error_message
                || String(run.result_payload?.summary || run.result_payload?.message || '-') },
          ]}
        />
      </Modal>

      <Modal
        title="创建自动化任务"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => void createSchedule()}
        okText="创建"
        confirmLoading={saving}
        width={680}
        destroyOnClose
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          <Form.Item name="kind" label="任务类型" rules={[{ required: true }]}>
            <Segmented block options={[
              { value: 'insight', label: '主动洞察' },
              { value: 'report', label: '定时报告' },
            ]} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, max: 128 }]}>
            <Input placeholder="销售日报" />
          </Form.Item>
          <Space className="automation-form-row" align="start">
            <Form.Item name="datasource" label="数据源" rules={[{ required: true }]}>
              <Select showSearch optionFilterProp="label" placeholder="选择数据源" style={{ width: '100%' }}
                options={datasources.map(item => ({ value: item.name, label: item.name }))} />
            </Form.Item>
            <Form.Item name="frequency" label="执行频率" rules={[{ required: true }]}>
              <Select options={FREQUENCY_OPTIONS} style={{ width: '100%' }} />
            </Form.Item>
          </Space>
          <Form.Item name="sql" label="只读 SQL" rules={[{ required: true, max: 20000 }]}>
            <Input.TextArea autoSize={{ minRows: 5, maxRows: 12 }}
              placeholder="SELECT day, SUM(amount) AS sales FROM orders GROUP BY day" />
          </Form.Item>
          <Form.Item name="threshold_pct" label="洞察变化阈值（%）"
            tooltip="仅主动洞察使用；定时报告会忽略此项"
            rules={[{ required: true, type: 'number', min: 0, max: 10000 }]}>
            <InputNumber min={0} max={10000} precision={2} style={{ width: 180 }} />
          </Form.Item>
          <Form.Item name="channels" label="通知渠道" rules={[{ required: true }]}>
            <Checkbox.Group options={CHANNEL_OPTIONS} />
          </Form.Item>
          {channels.includes('email') && (
            <Form.Item name="recipient_email" label="收件邮箱"
              rules={[{ required: true, type: 'email', max: 320 }]}>
              <Input type="email" placeholder="owner@example.com" />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </div>
  );
}
