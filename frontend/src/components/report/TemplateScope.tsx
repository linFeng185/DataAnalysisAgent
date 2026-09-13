// 报表模板作用域：把 token 注入为 CSS 变量与 antd 主题，作用域严格限定在报表容器内。
// 应用级主题（App.tsx 的 ConfigProvider）一行不动，模板不得污染报表之外的界面。

import type { CSSProperties, ReactNode } from 'react';
import { ConfigProvider, theme } from 'antd';
import { cssVariables, resolveTemplate } from '../../report/templates';

interface TemplateScopeProps {
  /** 未解析的原始模板标识，可能为空或未知值 */
  templateId: string | null | undefined;
  children: ReactNode;
}

// 方法作用：解析模板并把样式限定在报表容器内。
// Args: templateId - 原始模板标识；children - 报表内容。
// Returns: 带 CSS 变量与嵌套 antd 主题的容器节点。
export default function TemplateScope({ templateId, children }: TemplateScopeProps) {
  const resolved = resolveTemplate(templateId);
  const tokens = resolved.tokens;
  // 只映射报表实际用到的组件字段，不整体替换 antd 主题：
  // 避免模板承担它没有设计过的组件（Modal、Drawer、Form 等）的样式责任。
  return <div className="report-theme" style={cssVariables(tokens) as CSSProperties}>
    <ConfigProvider
      theme={{
        algorithm: resolved.scheme === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: tokens.accent,
          colorBgContainer: tokens.surface,
          colorBgLayout: tokens.bg,
          colorText: tokens.textPrimary,
          colorTextSecondary: tokens.textSecondary,
          colorBorder: tokens.border,
          colorBorderSecondary: tokens.border,
          borderRadius: parseInt(tokens.radius, 10) || 8,
          fontFamily: tokens.fontFamily,
          fontSize: tokens.density === 'compact' ? 13 : 14,
        },
        components: {
          Card: {
            headerBg: 'transparent',
            bodyPadding: tokens.density === 'compact' ? 12 : 16,
          },
          Table: {
            headerBg: tokens.accentSoft,
            borderColor: tokens.border,
            cellPaddingBlock: tokens.density === 'compact' ? 6 : 10,
          },
          Statistic: {
            contentFontSize: tokens.density === 'compact' ? 20 : 24,
          },
        },
      }}
    >
      {children}
    </ConfigProvider>
  </div>;
}
