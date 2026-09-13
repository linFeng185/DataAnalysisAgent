/** 报表额度「本周期已用量重置」的字段清单与结果提示，是四个可重置字段在前端的唯一来源。 */
import { t } from '../../i18n';
import type { ReportQuotaResetField, ReportQuotaUsageResetResponse } from '../../types/report';

/** 可按自然月清零已用量的额度字段与中文标签；标签用 getter 延迟求值，切换语言后重绘即为新文案。 */
export const REPORT_QUOTA_RESET_FIELDS: Array<{ value: ReportQuotaResetField; label: string }> = [
  { value: 'jobs_created', get label() { return t("每月可创建任务数"); } },
  { value: 'runs_started', get label() { return t("每月可运行次数"); } },
  { value: 'llm_tokens', get label() { return t("每月 Token 预算"); } },
  { value: 'share_links', get label() { return t("每月分享链接上限"); } },
];

// 方法作用：把服务端回传的字段键转换成当前语言下的中文标签，用于结果提示。
// Args: fields - 实际被清零的字段键数组。
// Returns: 以顿号连接的中文标签文本；未识别的键回退为原始键名，不吞掉信息。
export function quotaResetFieldLabels(fields: readonly ReportQuotaResetField[]): string {
  return fields
    .map(field => REPORT_QUOTA_RESET_FIELDS.find(option => option.value === field)?.label ?? field)
    .join('、');
}

// 方法作用：把重置结果转成用户可读提示，实际清零人数少于请求人数时说明差异。
// Args: response - 服务端返回的实际清零人数与字段；requestedCount - 本次请求覆盖的用户数。
// Returns: 一行中文提示文本，已包含实际清零人数与实际字段清单。
export function quotaResetResultMessage(response: ReportQuotaUsageResetResponse, requestedCount: number): string {
  const summary = t("已重置 {value0} 个用户的本月已用量：{value1}", {
    value0: response.reset_users,
    value1: quotaResetFieldLabels(response.fields),
  });
  // 后端只清零存在用量行的用户，实际人数可能小于请求人数，必须如实说明而不是报请求数。
  if (response.reset_users >= requestedCount) return summary;
  return t("{value0}；另有 {value1} 个用户本期没有已用量，未发生变更。", {
    value0: summary,
    value1: requestedCount - response.reset_users,
  });
}
