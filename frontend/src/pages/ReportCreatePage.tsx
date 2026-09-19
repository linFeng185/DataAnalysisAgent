import { t, useLanguage } from '../i18n';
import { useEffect, useRef, useState } from 'react';
import { Button, Card, Col, Input, List, Row, Segmented, Select, Space, Tag, Typography, message } from 'antd';
import { BulbOutlined, FileTextOutlined, SearchOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { get } from '../api/client';
import { createReport } from '../api/reportWorkbench';
import { randomId } from '../utils/randomId';
import { DEFAULT_TEMPLATE_ID, reportTemplates, resolveTemplate } from '../report/templates';
import { reportPromptExamples } from '../report/promptExamples';
import type { DatasourceConfig } from '../types';
import type { ReportDivergenceLevel } from '../types/report';

type ModelOption = {
  id: string;
  name: string;
  connection_id?: number;
  connection_name?: string;
  capabilities?: Record<string, unknown>;
};

// 方法作用：生成连接与模型组合的稳定选择值，避免同一模型跨连接重名。
// Args: option - 当前租户模型选项。
// Returns: 可用于 Select value 的组合字符串。
function modelOptionValue(option: ModelOption): string {
  return `${option.connection_id ?? 0}:${option.id}`;
}

// 方法作用：展示报表创建表单与静态案例引导，提交后跳转报表信息页查看计划进度。
// Args: 无。
// Returns: 创建报表页面。
export default function ReportCreatePage() {
  useLanguage();
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState('');
  const [divergence, setDivergence] = useState<ReportDivergenceLevel>('standard');
  // 模板不进计划哈希，因此切换它不需要重新生成计划。
  const [templateId, setTemplateId] = useState<string>(DEFAULT_TEMPLATE_ID);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [selectedDatasources, setSelectedDatasources] = useState<string[]>([]);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelId, setModelId] = useState('');
  const [llmConnectionId, setLlmConnectionId] = useState<number | undefined>();
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  // 幂等键在提交时生成：重复点击不会创建两条报表记录。
  const idempotencyKey = useRef('');
  const creatingInFlight = useRef(false);

  // 方法作用：加载数据源与可用模型，初始化创建表单的选项。
  // Args: 无。
  // Returns: 异步加载完成后无返回值。
  const loadOptions = async (): Promise<void> => {
    setLoading(true);
    try {
      const datasourceResult = await get<{ datasources: DatasourceConfig[] }>('/datasources');
      setDatasources(datasourceResult.datasources || []);
      // 经营全景这类报表横跨多个业务域，默认只勾第一个数据源会让所有单元都在
      // 单源上取数并因缺表失败。默认全选，由用户在提交前自行收窄。
      setSelectedDatasources((datasourceResult.datasources || []).map(item => item.name));
      // 模型列表单独加载：失败只提示，不影响表单其余部分；成功时预选租户默认命名连接。
      get<{ models: ModelOption[]; default: string | { connection_id?: number; model_id?: string } }>('/models')
        .then(data => {
          setModels((data.models || []).filter(item => Number(item.connection_id) > 0));
          const selectedDefault = typeof data.default === 'string' ? undefined : data.default;
          if (selectedDefault?.connection_id && selectedDefault.model_id) {
            setLlmConnectionId(selectedDefault.connection_id);
            setModelId(selectedDefault.model_id);
          }
        })
        .catch(() => message.warning(t("无法加载模型列表")));
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("报表工作台加载失败"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadOptions(); }, []);

  // 方法作用：创建报表记录并触发后台计划生成，成功后跳转报表信息页。
  // Args: 无，读取表单状态。
  // Returns: 异步提交完成后无返回值。
  const generatePlan = async (): Promise<void> => {
    if (!prompt.trim()) {
      message.warning(t("请输入报表描述"));
      return;
    }
    if (creatingInFlight.current) return;
    creatingInFlight.current = true;
    setCreating(true);
    try {
      if (!idempotencyKey.current) idempotencyKey.current = randomId();
      await createReport({
        prompt: prompt.trim(),
        datasources: selectedDatasources,
        divergence,
        template_id: templateId,
        model_id: modelId,
        llm_connection_id: llmConnectionId ?? null,
        idempotency_key: idempotencyKey.current,
      });
      // 计划在后台生成：本次提交不预占额度，用户到报表信息页看进度、点「生成」才预占。
      idempotencyKey.current = '';
      message.success(t("报表已创建，正在后台生成计划，完成后会通知你"));
      navigate('/report-workbench/jobs');
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("创建报表失败"));
    } finally {
      creatingInFlight.current = false;
      setCreating(false);
    }
  };

  // 方法作用：把选中的示例提示词填入左侧输入框，纯前端行为不请求后端。
  // Args: examplePrompt - 示例提示词原文。
  // Returns: 无返回值。
  const applyExample = (examplePrompt: string): void => {
    setPrompt(examplePrompt);
  };

  return <div className="report-workbench-page">
    <div className="report-workbench-toolbar">
      <div>
        <Typography.Title level={2} style={{ margin: 0 }}>{t("创建报表")}</Typography.Title>
        <Typography.Text type="secondary">{t("用自然语言描述要生成的报表，系统会在后台解析成可执行的分析计划。")}</Typography.Text>
      </div>
    </div>

    {/* Row/Col 的 lg 断点即 992px：窄屏时案例栏自动落回表单下方，不挤压左侧表单 */}
    <Row gutter={[20, 20]} align="top">
      <Col xs={24} lg={16}>
        <Card title={<Space><FileTextOutlined />{t("报表需求")}</Space>} loading={loading}>
          <Input.TextArea
            value={prompt}
            onChange={event => setPrompt(event.target.value)}
            placeholder={t("输入要分析的业务内容")}
            autoSize={{ minRows: 5, maxRows: 10 }}
            maxLength={20000}
            showCount
          />
          <Select
            mode="multiple"
            allowClear
            value={selectedDatasources}
            onChange={values => setSelectedDatasources(values)}
            placeholder={t("选择数据源")}
            options={datasources.map(item => ({ value: item.name, label: item.name }))}
            style={{ width: '100%', marginTop: 12 }}
          />
          {models.length > 0 && (
            <Select
              value={models.find(m => m.id === modelId && m.connection_id === llmConnectionId)
                ? modelOptionValue(models.find(m => m.id === modelId && m.connection_id === llmConnectionId)!)
                : undefined}
              onChange={value => {
                const selected = models.find(option => modelOptionValue(option) === value);
                setLlmConnectionId(selected?.connection_id);
                setModelId(selected?.id || '');
              }}
              placeholder={t("选择规划模型")}
              options={models.map(m => ({ value: modelOptionValue(m), label: `${m.connection_name || t("连接")} / ${m.name}` }))}
              style={{ width: '100%', marginTop: 8 }}
            />
          )}
          <div style={{ marginTop: 12 }}>
            <Typography.Text type="secondary">{t("发散档位")}</Typography.Text>
            <Segmented
              block
              value={divergence}
              onChange={value => setDivergence(value as ReportDivergenceLevel)}
              options={[
                { value: 'conservative', label: t("保守") },
                { value: 'standard', label: t("标准") },
                { value: 'deep', label: t("深度") },
              ]}
              style={{ marginTop: 6 }}
            />
            <Typography.Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 0, fontSize: 12 }}>
              {t("档位决定系统可自主追加的分析单元总数（计划期与运行期共享）：保守 0 个、标准 3 个、深度 6 个。")}
            </Typography.Paragraph>
          </div>
          <div style={{ marginTop: 12 }}>
            <Typography.Text type="secondary">{t("报表模板")}</Typography.Text>
            <Select
              value={templateId}
              onChange={setTemplateId}
              options={reportTemplates.map(template => {
                const tokens = resolveTemplate(template.id).tokens;
                return {
                  value: template.id,
                  label: <span className="report-template-option">
                    <span className="report-template-swatch" style={{ background: tokens.accent, borderColor: tokens.border }} />
                    {t(template.name)} · {t(template.description)}
                  </span>,
                };
              })}
              style={{ width: '100%', marginTop: 6 }}
            />
            <Typography.Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 0, fontSize: 12 }}>
              {t("模板只影响呈现样式，不改变取数范围与成本，因此随时切换都不会要求重新预览。")}
            </Typography.Paragraph>
          </div>
          <Button type="primary" block icon={<SearchOutlined />} loading={creating} onClick={() => void generatePlan()} style={{ marginTop: 12 }}>
            {t("生成计划")}
          </Button>
          <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0, fontSize: 12 }}>
            {t("提交后计划在后台生成，不会预占额度；计划就绪后可在「报表信息」里核对计划并点「生成」执行。")}
          </Typography.Paragraph>
        </Card>
      </Col>
      <Col xs={24} lg={8}>
        <Card
          title={<Space><BulbOutlined />{t("案例")}</Space>}
          extra={<Typography.Text type="secondary">{reportPromptExamples.length} {t("条")}</Typography.Text>}
        >
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            {t("点击任一案例可把提示词填入左侧输入框，再按需要改写。")}
          </Typography.Paragraph>
          <List
            size="small"
            dataSource={reportPromptExamples}
            renderItem={example => <List.Item
              className="report-prompt-example"
              onClick={() => applyExample(example.prompt)}
              style={{ cursor: 'pointer' }}
            >
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Tag color="blue">{t(example.dimension)}</Tag>
                {/* 提示词是可直接复用的内容文本，原样展示不经过翻译通道 */}
                <Typography.Text strong>{example.prompt}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>{t(example.note)}</Typography.Text>
              </Space>
            </List.Item>}
          />
        </Card>
      </Col>
    </Row>
  </div>;
}
