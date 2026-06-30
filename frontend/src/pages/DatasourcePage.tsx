import { useState, useEffect } from 'react';
import { Card, Table, Button, Modal, Form, Input, Select, message, Popconfirm, Space } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import { get, post, del } from '../api/client';
import type { DatasourceConfig } from '../types';

export default function DatasourcePage() {
  const [dss, setDss] = useState<DatasourceConfig[]>([]);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);

  const load = async () => {
    try { const data = await get<{ datasources: DatasourceConfig[] }>('/datasources'); setDss(data.datasources || []); }
    catch { message.error('加载失败'); }
  };

  useEffect(() => { load(); }, []);

  const handleAdd = async () => {
    try {
      const values = await form.validateFields();
      await post('/datasources', values as Record<string, unknown>);
      message.success('数据源已添加');
      setOpen(false); form.resetFields(); load();
    } catch { /* validation */ }
  };

  const handleDelete = async (name: string) => {
    try { await del(`/datasources/${encodeURIComponent(name)}`); message.success('已删除'); load(); }
    catch { message.error('删除失败'); }
  };

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <Card title="数据源管理" extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>添加</Button>}>
        <Table dataSource={dss} rowKey="name" loading={loading}
          columns={[
            { title: '名称', dataIndex: 'name', key: 'name' },
            { title: '方言', dataIndex: 'dialect', key: 'dialect' },
            { title: '主机', dataIndex: 'host', key: 'host' },
            { title: '端口', dataIndex: 'port', key: 'port' },
            { title: '数据库', dataIndex: 'database', key: 'database' },
            { title: '操作', key: 'action', render: (_: unknown, r: DatasourceConfig) => (
              <Popconfirm title="确定删除?" onConfirm={() => handleDelete(r.name)}>
                <Button danger icon={<DeleteOutlined />} size="small">删除</Button>
              </Popconfirm>
            )},
          ]} />
      </Card>
      <Modal title="添加数据源" open={open} onOk={handleAdd} onCancel={() => setOpen(false)}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="dialect" label="方言" rules={[{ required: true }]}>
            <Select options={['mysql','postgres','clickhouse','sqlite'].map(v=>({value:v,label:v}))} /></Form.Item>
          <Form.Item name="host" label="主机" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="port" label="端口" rules={[{ required: true }]}><Input type="number" /></Form.Item>
          <Form.Item name="database" label="数据库" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="username" label="用户名"><Input /></Form.Item>
          <Form.Item name="password" label="密码"><Input.Password /></Form.Item>
          <Form.Item name="description" label="描述"><Input.TextArea /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
