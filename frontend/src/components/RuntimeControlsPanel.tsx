import { t, useLanguage } from '../i18n';
import { useEffect, useState } from 'react';
import { Alert, Button, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag, Typography, message } from 'antd';
import { get, post, put } from '../api/client';

interface SettingField { key: string; value: unknown; default: unknown; overridden: boolean; schema: { type?: string; enum?: string[]; minimum?: number; maximum?: number } }
interface SettingsCatalog { revision: number; fields: SettingField[]; read_only: string[] }
export interface ConnectorPlugin { dialect: string; label: string; sqlglot_dialect: string; enabled: boolean; external: boolean; fields: string[]; error: string }

// 方法作用：管理平台热配置与可信连接器插件，环境默认值始终可恢复。
// Args: 无。
// Returns: 平台运行时管理界面。
export default function RuntimeControlsPanel() {
  useLanguage();
  const [catalog, setCatalog] = useState<SettingsCatalog>();
  const [plugins, setPlugins] = useState<ConnectorPlugin[]>([]);
  const [changes, setChanges] = useState<Record<string, unknown>>({});
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [changing, setChanging] = useState<ConnectorPlugin>();
  const [reason, setReason] = useState('');

  // 方法作用：加载服务器版本；显式重载才丢弃尚未保存的修改。
  // Args: 无。
  // Returns: 完成后更新配置与插件目录。
  const load = async (): Promise<void> => {
    setBusy(true);
    try {
      const [settings, connectors] = await Promise.all([get<SettingsCatalog>('/admin/runtime-settings'), get<{ items: ConnectorPlugin[] }>('/admin/connector-plugins')]);
      setCatalog(settings); setPlugins(connectors.items); setChanges({}); setError('');
    } catch (failure) { setError(failure instanceof Error ? failure.message : t("加载失败")); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, []);

  // 方法作用：保留版本冲突时的输入，不静默覆盖其他管理员的修改。
  // Args: 无。
  // Returns: 成功后展示实际生效值。
  const save = async (): Promise<void> => {
    if (!catalog) return;
    setBusy(true);
    try { setCatalog(await put<SettingsCatalog>('/admin/runtime-settings', { revision: catalog.revision, values: changes })); setChanges({}); setError(''); message.success(t("配置已保存，后续请求生效")); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t("保存失败")); }
    finally { setBusy(false); }
  };

  // 方法作用：只发现部署环境已经安装的插件，不在线执行安装命令。
  // Args: 无。
  // Returns: 刷新目录。
  const discover = async (): Promise<void> => {
    setBusy(true);
    try { setPlugins((await post<{ items: ConnectorPlugin[] }>('/admin/connector-plugins/discover', {})).items); message.success(t("插件目录已刷新")); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t("加载失败")); }
    finally { setBusy(false); }
  };

  // 方法作用：审计平台启停操作，停止后拒绝新查询但不中断在途查询。
  // Args: 无。
  // Returns: 保存并重载插件状态。
  const togglePlugin = async (): Promise<void> => {
    if (!changing) return;
    setBusy(true);
    try { await put(`/admin/connector-plugins/${encodeURIComponent(changing.dialect)}`, { enabled: !changing.enabled, reason }); setChanging(undefined); setPlugins((await get<{ items: ConnectorPlugin[] }>('/admin/connector-plugins')).items); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t("保存失败")); }
    finally { setBusy(false); }
  };

  // 方法作用：按配置类型生成输入控件，null 仅代表删除后台覆盖。
  // Args: field - 服务端字段元数据。
  // Returns: 类型对应的表单控件。
  const editor = (field: SettingField) => {
    const value = field.key in changes ? changes[field.key] ?? field.default : field.value;
    // 方法作用：暂存输入，提交时执行服务端完整类型与关联约束校验。
    // Args: next - 下一字段值。
    // Returns: 无返回值。
    const change = (next: unknown): void => setChanges(previous => ({ ...previous, [field.key]: next }));
    if (field.schema.type === 'boolean') return <Switch checked={Boolean(value)} onChange={change} />;
    if (field.schema.enum) return <Select value={value as string} options={field.schema.enum.map(item => ({ value: item, label: item }))} onChange={change} />;
    if (field.schema.type === 'integer' || field.schema.type === 'number') return <InputNumber value={Number(value)} min={field.schema.minimum ?? 0} max={field.schema.maximum} precision={field.schema.type === 'integer' ? 0 : undefined} onChange={next => { if (next !== null) change(next); }} style={{ width: 180 }} />;
    if (field.schema.type === 'array') return <Select mode="tags" value={value as string[]} onChange={change} style={{ minWidth: 200 }} />;
    return <Input value={String(value ?? '')} onChange={event => change(event.target.value)} />;
  };

  return <Space direction="vertical" style={{ width: '100%' }}>
    {error && <Alert type="error" message={error} />}
    <Tabs items={[
      { key: 'settings', label: t("动态配置"), children: <>
        <Alert type="info" showIcon message={t("环境变量作为默认值，后台覆盖优先")} description={t("后续请求使用新配置，多 Worker 最迟约 2 秒同步。主数据库、密钥、存储后端及进程启动项仍需部署配置，不支持在线热改。")} />
        <Space style={{ margin: '16px 0' }} wrap><Input.Search placeholder={t("搜索配置名称")} value={query} onChange={event => setQuery(event.target.value)} /><Button onClick={() => void load()} loading={busy}>{t("重新加载")}</Button><Button type="primary" onClick={() => void save()} loading={busy} disabled={!Object.keys(changes).length}>{t("保存配置")}</Button></Space>
        <Table<SettingField> rowKey="key" dataSource={catalog?.fields.filter(field => field.key.includes(query)) || []} pagination={{ pageSize: 15 }} scroll={{ x: 950 }} columns={[
          { title: t("配置名称"), dataIndex: 'key' }, { title: t("当前值"), render: (_, field) => editor(field) },
          { title: t("环境默认值"), render: (_, field) => <Typography.Text code>{JSON.stringify(field.default)}</Typography.Text> },
          { title: t("来源"), render: (_, field) => <Tag>{field.key in changes ? t("待保存") : field.overridden ? t("后台覆盖") : t("环境默认")}</Tag> },
          { title: t("操作"), render: (_, field) => <Button disabled={!field.overridden && !(field.key in changes)} onClick={() => setChanges(previous => ({ ...previous, [field.key]: null }))}>{t("恢复默认")}</Button> },
        ]} />
        <Typography.Paragraph type="secondary">{t("仅启动可配置：")}{catalog?.read_only.join(', ')}</Typography.Paragraph>
      </> },
      { key: 'plugins', label: t("数据库插件"), children: <>
        <Alert type="warning" message={t("仅加载可信环境预安装的连接器插件")} description={t("停用将影响所有使用该类型的数据源；恢复无需重启。驱动依赖由部署人员安装，平台不接受代码上传。")} />
        <Button style={{ margin: '16px 0' }} loading={busy} onClick={() => void discover()}>{t("发现已安装插件")}</Button>
        <Table<ConnectorPlugin> rowKey="dialect" dataSource={plugins} pagination={false} columns={[
          { title: t("数据库类型"), render: (_, item) => `${item.label} (${item.dialect})` }, { title: t("SQL 方言"), dataIndex: 'sqlglot_dialect' },
          { title: t("来源"), render: (_, item) => item.external ? t("外部插件") : t("内置插件") },
          { title: t("状态"), render: (_, item) => <Tag color={item.enabled ? 'green' : 'red'}>{item.error || (item.enabled ? t("启用") : t("停用"))}</Tag> },
          { title: t("操作"), render: (_, item) => <Button disabled={Boolean(item.error)} danger={item.enabled} onClick={() => { setReason(''); setChanging(item); }}>{item.enabled ? t("停用") : t("启用")}</Button> },
        ]} />
      </> },
    ]} />
    <Modal title={t("确认插件状态变更")} open={Boolean(changing)} onCancel={() => setChanging(undefined)} onOk={() => void togglePlugin()} confirmLoading={busy} okButtonProps={{ disabled: reason.trim().length < 3 }}>
      <Input.TextArea placeholder={t("填写变更原因（至少三个字符）")} value={reason} onChange={event => setReason(event.target.value)} maxLength={500} />
    </Modal>
  </Space>;
}
