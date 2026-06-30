import { useState, useEffect, useRef } from 'react';
import { Card, Table, Tag, Typography, Empty, Button, message, Space, Switch, Popconfirm, Modal, Descriptions } from 'antd';
import { ToolOutlined, UploadOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons';
import { get } from '../api/client';
import type { SkillInfo } from '../types';

const nodeColors: Record<string, string> = {
  custom_report: 'orange', 'data-quality-check': 'blue',
  'feature-dev': 'purple', 'systematic-debugging': 'green',
  'sales-analysis': 'volcano',
};

export default function SkillsPage() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<Record<string, unknown> | null>(null);
  const [detail, setDetail] = useState<SkillInfo | null>(null);
  const [fileContent, setFileContent] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await get<{ skills: SkillInfo[] }>('/skills');
      setSkills(data.skills || []);
    } catch { message.error('加载 Skills 失败'); }
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  const handleRefresh = async () => {
    try {
      await fetch('/api/v1/skills/refresh', { method: 'POST' });
      message.success('刷新完成'); load();
    } catch { message.error('刷新失败'); }
  };

  const handleToggle = async (name: string, enabled: boolean) => {
    await fetch(`/api/v1/skills/${encodeURIComponent(name)}/toggle?enabled=${enabled}`, { method: 'PUT' });
    load();
  };

  const handleDelete = async (name: string) => {
    const res = await fetch(`/api/v1/skills/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (res.ok) { message.success(`${name} 已删除`); load(); }
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
      const res = await fetch('/api/v1/skills/upload', { method: 'POST', body: form });
      const data = await res.json();
      setUploadResult(data);
      if (data.total > 0) { message.success(`已导入 ${data.total} 个`); load(); }
      else { message.warning('未找到 SKILL.md'); }
      data.errors?.forEach((e: { file: string; error: string }) => message.error(`${e.file}: ${e.error}`));
    } catch { message.error('上传失败'); }
    setUploading(false);
  };

  const openDetail = async (s: SkillInfo) => {
    setDetail(s);
    setFileContent('加载中...');
    try {
      const res = await fetch(`/api/v1/skills/${encodeURIComponent(s.name)}/content`);
      if (res.ok) {
        const data = await res.json();
        setFileContent(data.content || '');
      } else {
        setFileContent('// 无法读取文件内容');
      }
    } catch { setFileContent('// 读取失败'); }
  };

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Card title="Skills 管理" extra={
        <Space>
          <Typography.Text type="secondary">共 {skills.length} 个</Typography.Text>
          <Button icon={<ReloadOutlined />} size="small" onClick={handleRefresh}>刷新</Button>
          <input ref={fileRef} type="file" multiple
            // @ts-ignore
            webkitdirectory="" directory=""
            style={{ display: 'none' }}
            onChange={e => handleUpload(e.target.files)} />
          <Button icon={<UploadOutlined />} loading={uploading}
            onClick={() => fileRef.current?.click()}>批量导入</Button>
          <input type="file" multiple accept=".md" style={{ display: 'none' }}
            id="skill-file-single" onChange={e => handleUpload(e.target.files)} />
          <Button icon={<UploadOutlined />}
            onClick={() => document.getElementById('skill-file-single')?.click()}>导入 SKILL.md</Button>
        </Space>
      }>
        <Table<SkillInfo> dataSource={skills} rowKey="name" loading={loading}
          locale={{ emptyText: <Empty description="暂无 Skill" /> }}
          size="small"
          onRow={r => ({ onClick: () => openDetail(r), style: { cursor: 'pointer' } })}
          columns={[
            { title: '名称', dataIndex: 'name', width: 130,
              render: (v: string) => <Tag color={nodeColors[v] || 'default'}>{v}</Tag> },
            { title: '状态', dataIndex: 'enabled', width: 70,
              render: (v: boolean, r: SkillInfo) => (
                <Switch checked={v} size="small" onClick={(_, e) => e.stopPropagation()}
                  onChange={checked => handleToggle(r.name, checked)} />
              ) },
            { title: '描述', dataIndex: 'description', ellipsis: true },
            { title: '触发词', dataIndex: 'triggers', width: 200, ellipsis: true,
              render: (v: string[]) => v?.length ? v.slice(0, 3).join(', ') + (v.length > 3 ? '...' : '') : '—' },
            { title: '工具', dataIndex: 'tools', width: 80,
              render: (v: string[]) => v?.length ? <Tag color="cyan">{v.length} 个</Tag> : '—' },
            { title: '', key: 'actions', width: 36,
              render: (_: unknown, r: SkillInfo) => (
                r.is_builtin ? (
                  <Button size="small" danger disabled icon={<DeleteOutlined />}
                    onClick={e => e.stopPropagation()} title="内置不可删除" />
                ) : (
                  <Popconfirm title="确定删除？" onConfirm={() => handleDelete(r.name)}
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
              <Descriptions.Item label="名称"><Tag color={nodeColors[detail.name] || 'default'}>{detail.name}</Tag></Descriptions.Item>
              <Descriptions.Item label="版本">{detail.version}</Descriptions.Item>
              <Descriptions.Item label="状态">{detail.enabled
                ? <Tag color="success">启用</Tag> : <Tag color="error">禁用</Tag>}</Descriptions.Item>
              <Descriptions.Item label="类型">{detail.is_builtin ? <Tag color="blue">内置</Tag> : <Tag color="orange">用户上传</Tag>}</Descriptions.Item>
              <Descriptions.Item label="描述">{detail.description || '—'}</Descriptions.Item>
              <Descriptions.Item label="触发词">
                {detail.triggers?.length ? detail.triggers.map(k => <Tag key={k} color="blue">{k}</Tag>) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="意图">
                {detail.intents?.length ? detail.intents.map(i => <Tag key={i}>{i}</Tag>) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="工具">
                {detail.tools?.length ? detail.tools.map(t => <Tag key={t} icon={<ToolOutlined />} color="cyan">{t}</Tag>) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="依赖">
                {detail.dependencies?.length ? detail.dependencies.map(d => <Tag key={d}>{d}</Tag>) : '—'}
              </Descriptions.Item>
            </Descriptions>
            <Typography.Title level={5}>SKILL.md 文件内容</Typography.Title>
            <div style={{
              background: '#1e1e1e', color: '#d4d4d4', padding: 16, borderRadius: 8,
              maxHeight: 400, overflow: 'auto', fontFamily: 'Consolas, Monaco, monospace',
              fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.6,
            }}>
              {fileContent || '文件内容需通过本地文件系统查看: skills/' + detail.name + '/SKILL.md'}
            </div>
          </>
        )}
      </Modal>

      <Modal title="导入结果" open={!!uploadResult}
        onOk={() => setUploadResult(null)} onCancel={() => setUploadResult(null)}>
        {uploadResult && (
          <div>
            <Typography.Paragraph>导入 <Tag color="green">{uploadResult.total as number}</Tag> 个 Skill</Typography.Paragraph>
            {((uploadResult.imported as unknown[]) || []).map((s: unknown, i: number) => (
              <Tag key={i} color="blue">{(s as Record<string, string>).name}</Tag>
            ))}
          </div>
        )}
      </Modal>
    </div>
  );
}
