import { t, useLanguage } from '../i18n';
import { useState, useEffect, useRef } from 'react';
import { Card, Table, Tag, Typography, Empty, Button, message, Space, Switch, Popconfirm, Modal, Descriptions, Select } from 'antd';
import { AppstoreOutlined, CloudDownloadOutlined, ToolOutlined, UploadOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons';
import { get, post } from '../api/client';
import type { RegistrySkillInfo, SkillInfo } from '../types';
import type { KnowledgeScope } from '../types';
import { useAuth } from '../hooks/AuthContext';

const nodeColors: Record<string, string> = {
  custom_report: 'orange', 'data-quality-check': 'blue',
  'feature-dev': 'purple', 'systematic-debugging': 'green',
  'sales-analysis': 'volcano',
};

// 方法作用：管理本地受管 Skills 并浏览安装中心 Registry 审核版本。
// Args: 无。
// Returns: Skills 管理页面。
export default function SkillsPage() {
  useLanguage();
  const { user } = useAuth();
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<Record<string, unknown> | null>(null);
  const [detail, setDetail] = useState<SkillInfo | null>(null);
  const [fileContent, setFileContent] = useState('');
  const [uploadScope, setUploadScope] = useState<KnowledgeScope>(user?.role === 'super_admin' ? 'system' : 'private');
  const [registryOpen, setRegistryOpen] = useState(false);
  const [registryConfigured, setRegistryConfigured] = useState(false);
  const [registrySkills, setRegistrySkills] = useState<RegistrySkillInfo[]>([]);
  const [registryLoading, setRegistryLoading] = useState(false);
  const [installing, setInstalling] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await get<{ skills: SkillInfo[] }>('/skills');
      setSkills(data.skills || []);
    } catch { message.error(t("加载 Skills 失败")); }
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  // 方法作用：打开 Registry 并加载审核通过的兼容 Skill 列表。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const openRegistry = async (): Promise<void> => {
    setRegistryOpen(true);
    setRegistryLoading(true);
    try {
      const data = await get<{
        configured: boolean;
        skills: RegistrySkillInfo[];
      }>('/skills/registry');
      setRegistryConfigured(data.configured);
      setRegistrySkills(data.skills || []);
      if (!data.configured) message.info(t("Skill Registry 未配置"));
    } catch {
      message.error(t("Skill Registry 加载失败"));
    } finally {
      setRegistryLoading(false);
    }
  };

  // 方法作用：把 Registry 审核版本安装到当前选择的可信作用域。
  // Args: registrySkill - 待安装的 Registry Skill。
  // Returns: 请求完成后无返回值。
  const installRegistrySkill = async (registrySkill: RegistrySkillInfo): Promise<void> => {
    const installKey = `${registrySkill.name}@${registrySkill.version}`;
    setInstalling(installKey);
    try {
      await post(`/skills/registry/${encodeURIComponent(registrySkill.name)}/install`, {
        version: registrySkill.version,
        scope: uploadScope,
      });
      message.success(t("{value0} {value1} 已安装", { value0: registrySkill.name, value1: registrySkill.version }));
      await Promise.all([load(), openRegistry()]);
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("安装失败"));
    } finally {
      setInstalling('');
    }
  };

  const handleRefresh = async () => {
    try {
      const res = await fetch('/api/v1/skills/refresh', { method: 'POST', credentials: 'include' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      message.success(t("刷新完成"));
      await load();
    } catch { message.error(t("刷新失败")); }
  };

  const handleToggle = async (skill: SkillInfo, enabled: boolean) => {
    try {
      const res = await fetch(`/api/v1/skills/${encodeURIComponent(skill.name)}/toggle?enabled=${enabled}&skill_scope=${skill.scope}`, {
        method: 'PUT', credentials: 'include',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch { message.error(t("切换失败")); }
  };

  const handleDelete = async (skill: SkillInfo) => {
    try {
      const res = await fetch(`/api/v1/skills/${encodeURIComponent(skill.name)}?skill_scope=${skill.scope}`, {
        method: 'DELETE', credentials: 'include',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      message.success(t("{value0} 已删除", { value0: skill.name }));
      await load();
    } catch { message.error(t("删除失败")); }
  };

  const handleUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      const form = new FormData();
      for (let i = 0; i < files.length; i++) {
        const f = files[i];
        const rp = (f as unknown as { webkitRelativePath?: string }).webkitRelativePath || f.name;
        form.append('files', f, rp);
      }
      const res = await fetch(`/api/v1/skills/upload?skill_scope=${uploadScope}`, {
        method: 'POST', body: form, credentials: 'include',
      });
      const data = await res.json();
      setUploadResult(data);
      if (data.total > 0) { message.success(t("已导入 {value0} 个", { value0: data.total })); load(); }
      else { message.warning(t("未找到 SKILL.md")); }
      data.errors?.forEach((e: { file: string; error: string }) => message.error(`${e.file}: ${e.error}`));
    } catch { message.error(t("上传失败")); }
    setUploading(false);
  };

  const openDetail = async (s: SkillInfo) => {
    setDetail(s);
    setFileContent(t("加载中..."));
    try {
      const res = await fetch(`/api/v1/skills/${encodeURIComponent(s.name)}/content?skill_scope=${s.scope}`, {
        credentials: 'include',
      });
      if (res.ok) {
        const data = await res.json();
        setFileContent(data.content || '');
      } else {
        setFileContent('// 无法读取文件内容');
      }
    } catch { setFileContent('// 读取失败'); }
  };

  const scopeOptions = [
    ...(user?.role === 'super_admin' ? [{ value: 'system', label: t("系统") }] : []),
    ...(user?.role === 'tenant_admin' ? [{ value: 'tenant', label: t("租户") }] : []),
    ...(user?.role !== 'super_admin' ? [{ value: 'private', label: t("个人") }] : []),
  ];

  const scopeColor: Record<KnowledgeScope, string> = {
    system: 'blue', tenant: 'green', private: 'default',
  };
  const emptySkillsDescription = user?.role === 'super_admin'
    ? t("暂无 Skill，可从 Registry 安装或导入 SKILL.md")
    : t("当前作用域暂无可见 Skill，系统 Skill 由平台管理员维护");

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Card title={t("Skills 管理")} extra={
        <Space>
          <Typography.Text type="secondary">{t("共")} {skills.length} {t("个")}</Typography.Text>
          <Button icon={<AppstoreOutlined />} size="small" onClick={() => void openRegistry()}>
            Skill Registry
          </Button>
          <Button icon={<ReloadOutlined />} size="small" onClick={handleRefresh}>{t("刷新")}</Button>
          <Select<KnowledgeScope> value={uploadScope} options={scopeOptions}
            style={{ width: 92 }} onChange={setUploadScope} />
          <input ref={fileRef} type="file" multiple
            // @ts-ignore
            webkitdirectory="" directory=""
            style={{ display: 'none' }}
            onChange={e => handleUpload(e.target.files)} />
          <Button icon={<UploadOutlined />} loading={uploading}
            onClick={() => fileRef.current?.click()}>{t("批量导入")}</Button>
          <input type="file" multiple accept=".md" style={{ display: 'none' }}
            id="skill-file-single" onChange={e => handleUpload(e.target.files)} />
          <Button icon={<UploadOutlined />}
            onClick={() => document.getElementById('skill-file-single')?.click()}>{t("导入 SKILL.md")}</Button>
        </Space>
      }>
        <Table<SkillInfo> dataSource={skills} rowKey={r => `${r.scope}:${r.tenant_id}:${r.owner_user_id}:${r.name}`} loading={loading}
          scroll={{ x: 860 }}
          locale={{ emptyText: <Empty description={emptySkillsDescription} /> }}
          size="small"
          onRow={r => ({ onClick: () => openDetail(r), style: { cursor: 'pointer' } })}
          columns={[
            { title: t("名称"), dataIndex: 'name', width: 130,
              render: (v: string) => <Tag color={nodeColors[v] || 'default'}>{v}</Tag> },
            { title: t("状态"), dataIndex: 'enabled', width: 70,
              render: (v: boolean, r: SkillInfo) => (
                <Switch checked={v} size="small" onClick={(_, e) => e.stopPropagation()}
                  onChange={checked => handleToggle(r, checked)} />
              ) },
            { title: t("范围"), dataIndex: 'scope', width: 76,
              render: (v: KnowledgeScope) => <Tag color={scopeColor[v]}>{v === 'system' ? t("系统") : v === 'tenant' ? t("租户") : t("个人")}</Tag> },
            { title: t("描述"), dataIndex: 'description', ellipsis: true },
            { title: t("触发词"), dataIndex: 'triggers', width: 200, ellipsis: true,
              render: (v: string[]) => v?.length ? v.slice(0, 3).join(', ') + (v.length > 3 ? '...' : '') : '—' },
            { title: t("工具"), dataIndex: 'tools', width: 80,
              render: (v: string[]) => v?.length ? <Tag color="cyan">{v.length} {t("个")}</Tag> : '—' },
            { title: '', key: 'actions', width: 36,
              render: (_: unknown, r: SkillInfo) => (
                r.is_builtin ? (
                  <Button size="small" danger disabled icon={<DeleteOutlined />}
                    onClick={e => e.stopPropagation()} title={t("内置不可删除")} />
                ) : (
                  <Popconfirm title={t("确定删除？")} onConfirm={() => handleDelete(r)}
                    onPopupClick={e => e.stopPropagation()}>
                    <Button size="small" danger icon={<DeleteOutlined />}
                      onClick={e => e.stopPropagation()} />
                  </Popconfirm>
                )
              ) },
          ]} />
      </Card>

      <Modal title={`Skill: ${detail?.name || ''}`} open={!!detail}
        onCancel={() => setDetail(null)} footer={null} width={680} maskClosable>
        {detail && (
          <>
            <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t("名称")}><Tag color={nodeColors[detail.name] || 'default'}>{detail.name}</Tag></Descriptions.Item>
              <Descriptions.Item label={t("版本")}>{detail.version}</Descriptions.Item>
              <Descriptions.Item label={t("状态")}>{detail.enabled
                ? <Tag color="success">{t("启用")}</Tag> : <Tag color="error">{t("禁用")}</Tag>}</Descriptions.Item>
              <Descriptions.Item label={t("类型")}>{detail.is_builtin ? <Tag color="blue">{t("内置")}</Tag> : <Tag color="orange">{t("用户上传")}</Tag>}</Descriptions.Item>
              <Descriptions.Item label={t("范围")}><Tag color={scopeColor[detail.scope]}>{detail.scope}</Tag></Descriptions.Item>
              <Descriptions.Item label={t("描述")}>{detail.description || '—'}</Descriptions.Item>
              <Descriptions.Item label={t("触发词")}>
                {detail.triggers?.length ? detail.triggers.map(k => <Tag key={k} color="blue">{k}</Tag>) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t("意图")}>
                {detail.intents?.length ? detail.intents.map(i => <Tag key={i}>{i}</Tag>) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t("工具")}>
                {detail.tools?.length ? detail.tools.map(t => <Tag key={t} icon={<ToolOutlined />} color="cyan">{t}</Tag>) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t("依赖")}>
                {detail.dependencies?.length ? detail.dependencies.map(d => <Tag key={d}>{d}</Tag>) : '—'}
              </Descriptions.Item>
            </Descriptions>
            <Typography.Title level={5}>{t("SKILL.md 文件内容")}</Typography.Title>
            <div style={{
              background: '#1e1e1e', color: '#d4d4d4', padding: 16, borderRadius: 8,
              maxHeight: 400, overflow: 'auto', fontFamily: 'Consolas, Monaco, monospace',
              fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.6,
            }}>
              {fileContent || t("文件内容需通过本地文件系统查看: skills/") + detail.name + '/SKILL.md'}
            </div>
          </>
        )}
      </Modal>

      <Modal title={t("导入结果")} open={!!uploadResult}
        onOk={() => setUploadResult(null)} onCancel={() => setUploadResult(null)}>
        {uploadResult && (
          <div>
            <Typography.Paragraph>{t("导入")} <Tag color="green">{uploadResult.total as number}</Tag> {t("个 Skill")}</Typography.Paragraph>
            {((uploadResult.imported as unknown[]) || []).map((s: unknown, i: number) => (
              <Tag key={i} color="blue">{(s as Record<string, string>).name}</Tag>
            ))}
          </div>
        )}
      </Modal>

      <Modal
        title="Skill Registry"
        open={registryOpen}
        onCancel={() => setRegistryOpen(false)}
        footer={null}
        width={760}
      >
        {!registryConfigured && !registryLoading ? (
          <Empty description={t("Registry 未配置")} />
        ) : (
          <Table<RegistrySkillInfo>
            rowKey={item => `${item.name}@${item.version}`}
            dataSource={registrySkills}
            loading={registryLoading}
            pagination={false}
            scroll={{ x: 640 }}
            columns={[
              { title: t("名称"), dataIndex: 'name', width: 150 },
              { title: t("版本"), dataIndex: 'version', width: 90 },
              { title: t("描述"), dataIndex: 'description', ellipsis: true },
              { title: 'API', dataIndex: 'api_version', width: 120 },
              {
                title: t("操作"), key: 'action', width: 100,
                render: (_: unknown, item: RegistrySkillInfo) => (
                  <Button
                    size="small"
                    type="primary"
                    icon={<CloudDownloadOutlined />}
                    disabled={item.installed}
                    loading={installing === `${item.name}@${item.version}`}
                    onClick={() => void installRegistrySkill(item)}
                  >
                    {item.installed ? t("已安装") : t("安装")}
                  </Button>
                ),
              },
            ]}
          />
        )}
      </Modal>
    </div>
  );
}
