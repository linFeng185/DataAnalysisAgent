import { useState, useEffect } from 'react';
import { Card, Table, Button, Modal, Form, Input, Select, message, Popconfirm, Tag, Typography } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import { get, post, del } from '../api/client';
import type { DatasourceConfig } from '../types';

const DIALECTS = [
  { value: 'mysql', label: 'MySQL' },
  { value: 'postgres', label: 'PostgreSQL' },
  { value: 'clickhouse', label: 'ClickHouse' },
  { value: 'oracle', label: 'Oracle' },
  { value: 'mssql', label: 'SQL Server' },
  { value: 'sqlite', label: 'SQLite' },
];

const VERSIONS: Record<string, string[]> = {
  mysql: ['5.7', '8.0', '8.1', '8.4'],
  postgres: ['14', '15', '16', '17'],
  clickhouse: ['23.x', '24.x', '25.x'],
  oracle: ['19c', '21c', '23ai'],
  mssql: ['2019', '2022'],
  sqlite: [],
};

function dialectFields(d: string): string[] {
  switch (d) {
    case 'oracle': return ['host', 'port', 'service_name', 'tablespace', 'username', 'password'];
    case 'mssql': return ['host', 'port', 'instance', 'database', 'schema', 'username', 'password'];
    case 'postgres': return ['host', 'port', 'database', 'schema', 'username', 'password'];
    case 'sqlite': return ['file_path'];
    default: return ['host', 'port', 'database', 'username', 'password']; // mysql, clickhouse
  }
}

const LABELS: Record<string, string> = {
  host: '主机', port: '端口', database: '数据库名', username: '用户名',
  password: '密码', schema: 'Schema', tablespace: '表空间',
  service_name: '服务名/SID', instance: '实例名', file_path: '文件路径',
};

const PLACEHOLDERS: Record<string, Record<string, string>> = {
  port: { mysql: '3306', postgres: '5432', clickhouse: '9000', oracle: '1521', mssql: '1433', sqlite: '' },
  schema: { postgres: 'public', mssql: 'dbo' },
  service_name: { oracle: 'XEPDB1' },
  file_path: { sqlite: './data.db' },
};

function placeholder(f: string, d: string): string {
  return (PLACEHOLDERS[f] || {})[d] || '';
}

export default function DatasourcePage() {
  const [dss, setDss] = useState<DatasourceConfig[]>([]);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [dialect, setDialect] = useState('mysql');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try { const data = await get<{ datasources: DatasourceConfig[] }>('/datasources'); setDss(data.datasources || []); }
    catch { message.error('加载失败'); }
    finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  const handleAdd = async () => {
    try {
      const values = await form.validateFields();
      const payload: Record<string, unknown> = { ...values, dialect };
      if (dialect === 'sqlite') { payload.host = ''; payload.port = 0; payload.database = values.file_path; }
      payload.version = values.version?.[0] || values.version || '';
      await post('/datasources', payload);
      message.success('数据源已添加');
      setOpen(false); form.resetFields(); setDialect('mysql'); load();
    } catch { /* validation */ }
  };

  const handleDelete = async (name: string) => {
    try { await del(`/datasources/${encodeURIComponent(name)}`); message.success('已删除'); load(); }
    catch { message.error('删除失败'); }
  };

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Card title="数据源管理" extra={
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>添加</Button>
      }>
        <Table dataSource={dss} rowKey="name" loading={loading}
          columns={[
            { title: '名称', dataIndex: 'name', key: 'name', width: 120 },
            { title: '数据库类型', dataIndex: 'dialect', key: 'dialect', width: 100,
              render: (d: string) => DIALECTS.find(o => o.value === d)?.label || d },
            { title: '版本', dataIndex: 'version', key: 'version', width: 70,
              render: (v: string) => v ? <Tag>{v}</Tag> : <Typography.Text type="secondary">-</Typography.Text> },
            { title: '主机', dataIndex: 'host', key: 'host', width: 130, ellipsis: true },
            { title: '端口', dataIndex: 'port', key: 'port', width: 60 },
            { title: '数据库', dataIndex: 'database', key: 'database', width: 100, ellipsis: true },
            { title: '用户', dataIndex: 'username', key: 'username', width: 80 },
            { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
            { title: '操作', key: 'action', width: 80, render: (_: unknown, r: DatasourceConfig) => (
              <Popconfirm title="确定删除?" onConfirm={() => handleDelete(r.name)}>
                <Button danger icon={<DeleteOutlined />} size="small">删除</Button>
              </Popconfirm>
            )},
          ]} />
      </Card>

      <Modal title="添加数据源" open={open} onOk={handleAdd}
        onCancel={() => { setOpen(false); setDialect('mysql'); }} width={520} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 mysql_prod" /></Form.Item>

          <Form.Item label="数据库类型" rules={[{ required: true }]}>
            <Select value={dialect} onChange={v => {
              setDialect(v); form.resetFields(['host','port','database','username','password','schema','tablespace','service_name','instance','file_path']);
            }} options={DIALECTS} /></Form.Item>

          <Form.Item name="version" label="版本">
            <Select allowClear placeholder="选择或输入版本号" showSearch
              mode="tags" maxCount={1}
              options={(VERSIONS[dialect] || []).map(v => ({ value: v, label: v }))} />
          </Form.Item>

          {dialectFields(dialect).map(f => (
            <Form.Item key={f} name={f} label={LABELS[f] || f}
              rules={['password','schema','tablespace','instance'].includes(f) ? [] : [{ required: dialect !== 'sqlite', message: '必填' }]}>
              {f === 'password'
                ? <Input.Password placeholder={placeholder(f, dialect)} />
                : <Input placeholder={placeholder(f, dialect)} />}
            </Form.Item>
          ))}

          <Form.Item name="description" label="描述">
            <Input.TextArea placeholder="用途说明（可选）" rows={2} /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
