import { useState, useEffect } from 'react';
import { Card, Table, Button, Modal, Form, Input, Select, message, Popconfirm, Tag, Space } from 'antd';
import { PlusOutlined, DeleteOutlined, ApiOutlined } from '@ant-design/icons';
import { get, post, del } from '../api/client';
import { useAuth } from '../hooks/AuthContext';
import type { KnowledgeScope } from '../types';

interface McpServer { name: string; scope: KnowledgeScope; tenant_id: number; owner_user_id: number; transport: string; command: string; args: string; url: string; description: string; is_builtin: boolean; enabled: boolean; }

export default function McpPage() {
  const { user } = useAuth();
  const [servers, setServers] = useState<McpServer[]>([]);
  const [open, setOpen] = useState(false); const [form] = Form.useForm(); const [loading, setLoading] = useState(false);

  const ld = async () => { setLoading(true); try { const d=await get<{servers:McpServer[]}>('/mcp/servers'); setServers(d.servers||[]); } catch { message.error('加载失败'); } finally { setLoading(false); } };
  useEffect(() => { ld(); }, []);

  const add = async () => { try { const v=await form.validateFields(); await post('/mcp/servers',v as Record<string,unknown>); message.success('已添加'); setOpen(false); form.resetFields(); ld(); } catch {/* */} };
  const test = async (server:McpServer) => { try { const r=await post<{ok:boolean;error?:string}>(`/mcp/servers/${encodeURIComponent(server.name)}/test?scope=${server.scope}`,{}); message.success(r.ok?'连接成功':'失败: '+(r.error||'')); } catch { message.error('测试失败'); } };
  const delSrv = async (server:McpServer) => { try { await del(`/mcp/servers/${encodeURIComponent(server.name)}?scope=${server.scope}`); message.success('已删除'); ld(); } catch { message.error('删除失败'); } };

  const scopeOptions = [
    ...(user?.role === 'super_admin' ? [{ value: 'system', label: '系统' }] : []),
    ...(['super_admin', 'tenant_admin'].includes(user?.role || '') ? [{ value: 'tenant', label: '租户' }] : []),
    { value: 'private', label: '个人' },
  ];
  const scopeColor: Record<KnowledgeScope, string> = { system: 'blue', tenant: 'green', private: 'default' };

  return (
    <div style={{maxWidth:900,margin:'0 auto'}}>
      <Card title="MCP Server 管理" extra={<Button type="primary" icon={<PlusOutlined/>} onClick={()=>setOpen(true)}>添加</Button>}>
        <Table dataSource={servers} rowKey={r => `${r.scope}:${r.tenant_id}:${r.owner_user_id}:${r.name}`} loading={loading} columns={[
          {title:'名称',dataIndex:'name',width:140},
          {title:'范围',dataIndex:'scope',width:76,render:(v:KnowledgeScope)=><Tag color={scopeColor[v]}>{v==='system'?'系统':v==='tenant'?'租户':'个人'}</Tag>},
          {title:'传输',dataIndex:'transport',width:80,render:(v:string)=><Tag>{v}</Tag>},
          {title:'命令/URL',key:'cmd',ellipsis:true,render:(_:unknown,r:McpServer)=>r.transport==='stdio'?r.command+' '+r.args:r.url},
          {title:'描述',dataIndex:'description',ellipsis:true},
          {title:'内置',dataIndex:'is_builtin',width:70,render:(v:boolean)=>v?<Tag color="blue">内置</Tag>:<Tag>自定义</Tag>},
          {title:'操作',width:160,render:(_:unknown,r:McpServer)=>(<Space>
            <Button size="small" icon={<ApiOutlined/>} onClick={()=>test(r)}>测试</Button>
            {!r.is_builtin && <Popconfirm title="确定删除?" onConfirm={()=>delSrv(r)}><Button size="small" danger icon={<DeleteOutlined/>}>删除</Button></Popconfirm>}
          </Space>)},
        ]} />
      </Card>
      <Modal title="添加 MCP Server" open={open} onOk={add} onCancel={()=>setOpen(false)} width={520}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{required:true}]}><Input placeholder="my_server"/></Form.Item>
          <Form.Item name="scope" label="范围" initialValue="private" rules={[{required:true}]}>
            <Select options={scopeOptions}/></Form.Item>
          <Form.Item name="transport" label="传输" rules={[{required:true}]} initialValue="stdio">
            <Select options={[{value:'stdio',label:'stdio (命令行)'},{value:'sse',label:'sse (HTTP)'}]}/></Form.Item>
          <Form.Item name="command" label="命令"><Input placeholder="python"/></Form.Item>
          <Form.Item name="args" label="参数"><Input placeholder="-m my_mcp"/></Form.Item>
          <Form.Item name="url" label="URL (sse)"><Input placeholder="http://localhost:3000/sse"/></Form.Item>
          <Form.Item name="description" label="描述"><Input placeholder="用途说明"/></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
