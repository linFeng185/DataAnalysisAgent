import { t, useLanguage } from '../i18n';
import { useState, useEffect } from 'react';
import { Card, Table, Button, Modal, Form, Input, Select, message, Popconfirm, Tag, Space } from 'antd';
import { PlusOutlined, DeleteOutlined, ApiOutlined } from '@ant-design/icons';
import { get, post, del } from '../api/client';
import { useAuth } from '../hooks/AuthContext';
import type { KnowledgeScope } from '../types';

interface McpServer { name: string; scope: KnowledgeScope; tenant_id: number; owner_user_id: number; transport: string; command: string; args: string; url: string; description: string; is_builtin: boolean; enabled: boolean; }

export default function McpPage() {
  useLanguage();
  const { user } = useAuth();
  const [servers, setServers] = useState<McpServer[]>([]);
  const [open, setOpen] = useState(false); const [form] = Form.useForm(); const [loading, setLoading] = useState(false);

  const ld = async () => { setLoading(true); try { const d=await get<{servers:McpServer[]}>('/mcp/servers'); setServers(d.servers||[]); } catch { message.error(t("加载失败")); } finally { setLoading(false); } };
  useEffect(() => { ld(); }, []);

  // 方法作用：校验并创建 MCP Server，同时将服务端拒绝原因反馈给用户。
  // Args: 无，读取当前表单字段。
  // Returns: 无返回值；成功关闭弹窗，失败展示后端错误详情。
  const add = async (): Promise<void> => {
    try {
      const v = await form.validateFields();
      await post('/mcp/servers', v as Record<string, unknown>);
      message.success(t("已添加")); setOpen(false); form.resetFields(); void ld();
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("添加失败"));
    }
  };
  const test = async (server:McpServer) => { try { const r=await post<{ok:boolean;error?:string}>(`/mcp/servers/${encodeURIComponent(server.name)}/test?scope=${server.scope}`,{}); message.success(r.ok?t("连接成功"):t("失败: ")+(r.error||'')); } catch { message.error(t("测试失败")); } };
  const delSrv = async (server:McpServer) => { try { await del(`/mcp/servers/${encodeURIComponent(server.name)}?scope=${server.scope}`); message.success(t("已删除")); ld(); } catch { message.error(t("删除失败")); } };

  const scopeOptions = [
    ...(user?.role === 'super_admin' ? [{ value: 'system', label: t("系统") }] : []),
    ...(user?.role === 'tenant_admin' ? [{ value: 'tenant', label: t("租户") }, { value: 'private', label: t("个人") }] : []),
  ];
  const scopeColor: Record<KnowledgeScope, string> = { system: 'blue', tenant: 'green', private: 'default' };

  return (
    <div style={{maxWidth:900,margin:'0 auto'}}>
      <Card title={t("MCP Server 管理")} extra={<Button type="primary" icon={<PlusOutlined/>} onClick={()=>setOpen(true)}>{t("添加")}</Button>}>
        <Table dataSource={servers} rowKey={r => `${r.scope}:${r.tenant_id}:${r.owner_user_id}:${r.name}`} loading={loading} columns={[
          {title:t("名称"),dataIndex:'name',width:140},
          {title:t("范围"),dataIndex:'scope',width:76,render:(v:KnowledgeScope)=><Tag color={scopeColor[v]}>{v==='system'?t("系统"):v==='tenant'?t("租户"):t("个人")}</Tag>},
          {title:t("传输"),dataIndex:'transport',width:80,render:(v:string)=><Tag>{v}</Tag>},
          {title:t("命令/URL"),key:'cmd',ellipsis:true,render:(_:unknown,r:McpServer)=>r.transport==='stdio'?r.command+' '+r.args:r.url},
          {title:t("描述"),dataIndex:'description',ellipsis:true},
          {title:t("内置"),dataIndex:'is_builtin',width:70,render:(v:boolean)=>v?<Tag color="blue">{t("内置")}</Tag>:<Tag>{t("自定义")}</Tag>},
          {title:t("操作"),width:160,render:(_:unknown,r:McpServer)=>(<Space>
            <Button size="small" icon={<ApiOutlined/>} onClick={()=>test(r)}>{t("测试")}</Button>
            {!r.is_builtin && <Popconfirm title={t("确定删除?")} onConfirm={()=>delSrv(r)}><Button size="small" danger icon={<DeleteOutlined/>}>{t("删除")}</Button></Popconfirm>}
          </Space>)},
        ]} />
      </Card>
      <Modal title={t("添加 MCP Server")} open={open} onOk={add} onCancel={()=>setOpen(false)} width={520}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t("名称")} rules={[{required:true}]}><Input placeholder="my_server"/></Form.Item>
          <Form.Item name="scope" label={t("范围")} initialValue={user?.role === 'super_admin' ? 'system' : 'private'} rules={[{required:true}]}>
            <Select options={scopeOptions}/></Form.Item>
          <Form.Item name="transport" label={t("传输")} rules={[{required:true}]} initialValue="sse">
            <Select options={[{value:'sse',label:'sse (HTTP)'}]}/></Form.Item>
          <Form.Item name="url" label="URL (sse)"><Input placeholder="http://localhost:3000/sse"/></Form.Item>
          <Form.Item name="description" label={t("描述")}><Input placeholder={t("用途说明")}/></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
