import { Button, Checkbox, List, Space, Typography } from 'antd';
import { t } from '../../i18n';
import type { ReportAnalysisUnit } from '../../types/report';

/** 计划单元行的渲染契约。 */
export interface ReportPlanUnitRowProps {
  /** 待渲染的分析单元。 */
  unit: ReportAnalysisUnit;
  /** 是否可编辑：计划锁定后只展示，不提供勾选框（勾选没有可提交的落点）。 */
  editable: boolean;
  /** 被用户取消勾选的单元标识。 */
  excludedUnitIds: string[];
  /** 勾选状态变化回调。 */
  onToggle: (unitId: string, checked: boolean) => void;
  /** 单元尚未绑定数据源时的兜底展示名。 */
  fallbackDatasource?: string;
  /** 重跑本单元回调；未提供时不渲染按钮（例如从未运行过的报表）。 */
  onRerun?: (unitId: string) => void;
  /** 查看最近一次执行快照回调；未提供时不渲染按钮。 */
  onViewSnapshot?: (unitId: string) => void;
  /** 正在重跑的单元标识，用于对应按钮的 loading 态。 */
  rerunningUnitId?: string;
}

/**
 * 计划单元行：全站唯一定义单元勾选框的地方。
 *
 * 工作台预览页与报表详情页曾各写一份渲染，于是同一种「某个分组只读、单元删不掉」
 * 的缺陷修了一处漏一处——用户看到的现象就是"改了没生效"。此后新增页面一律复用本组件。
 *
 * 两个约定：
 * 1. 任何来源（用户要求 / 系统发散）的单元都必须能由用户决定去留，差异只在 editable 门控；
 * 2. 展示以 question 为主——unit_id 是程序内部标识（英文），当标题展示会让用户以为
 *    "生成的标题全是英文"，因此不进列表正文。
 */
export default function ReportPlanUnitRow({
  unit,
  editable,
  excludedUnitIds,
  onToggle,
  fallbackDatasource = '',
  onRerun,
  onViewSnapshot,
  rerunningUnitId = '',
}: ReportPlanUnitRowProps) {
  return <List.Item className="report-plan-unit">
    {editable && <Checkbox
      checked={!excludedUnitIds.includes(unit.unit_id)}
      onChange={event => onToggle(unit.unit_id, event.target.checked)}
    >{t("纳入本次报表")}</Checkbox>}
    <Typography.Text strong>{unit.question}</Typography.Text>
    {unit.origin === 'diverged' && <Typography.Text type="secondary">{t("建议理由：")}{unit.rationale || t("未说明")}</Typography.Text>}
    <Typography.Text type="secondary">{t("数据源：")}{unit.datasource || fallbackDatasource || t("待绑定")}</Typography.Text>
    {(onRerun || onViewSnapshot) && <Space size="small">
      {onRerun && <Button
        size="small"
        loading={rerunningUnitId === unit.unit_id}
        onClick={() => onRerun(unit.unit_id)}
      >{t("重跑本单元")}</Button>}
      {onViewSnapshot && <Button size="small" onClick={() => onViewSnapshot(unit.unit_id)}>{t("查看快照")}</Button>}
    </Space>}
  </List.Item>;
}
