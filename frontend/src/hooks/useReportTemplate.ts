// 报表模板选择：以页面文档记录的模板为初值，允许本次浏览临时覆盖。
// 切换只改本地状态，不写回服务端、不改报表版本、不触发重新查询。

import { useEffect, useState } from 'react';
import { DEFAULT_TEMPLATE_ID } from '../report/templates';
import type { ReportDocument } from '../types/report';

// 方法作用：解析页面文档记录的模板作为初始值，并允许本次浏览临时覆盖。
// Args: document - 当前报表页面文档；加载完成前可为空。
// Returns: 当前模板 id 与切换函数。
export function useReportTemplate(document: ReportDocument | null | undefined): [string, (id: string) => void] {
  const recorded = document?.theme?.template_id || DEFAULT_TEMPLATE_ID;
  const [templateId, setTemplateId] = useState<string>(recorded);
  // 切换报表或版本时回到该版本记录的模板：阅读期选择不该跨越报表。
  useEffect(() => { setTemplateId(recorded); }, [recorded]);
  return [templateId, setTemplateId];
}
