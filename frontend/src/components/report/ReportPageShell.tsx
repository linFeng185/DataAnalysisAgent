// 报表页面外壳：私有查看页与公开分享页共用同一套整页框架。
// 页面底色铺满视口并取模板变量 --report-bg，内容限宽居中；
// 顶栏与正文同处一个 TemplateScope，按钮、标签、边框、圆角全部消费同一套模板 token。

import type { ReactNode } from 'react';
import TemplateScope from './TemplateScope';

interface ReportPageShellProps {
  /** 当前生效的模板标识；加载完成前可为空，由 TemplateScope 回退默认模板 */
  templateId: string | null | undefined;
  /** 可选顶栏节点；渲染在 TemplateScope 内部，因此与正文观感一致 */
  toolbar?: ReactNode;
  children: ReactNode;
}

// 方法作用：为报表页面提供整页模板底色、限宽居中容器与主题作用域。
// Args: templateId - 当前模板标识；toolbar - 可选顶栏节点；children - 页面正文。
// Returns: 已注入模板变量的整页外壳节点。
export default function ReportPageShell({ templateId, toolbar, children }: ReportPageShellProps) {
  return <TemplateScope templateId={templateId}>
    <div className="report-page">
      <div className="report-page-body">
        {toolbar ? <div className="report-page-toolbar">{toolbar}</div> : null}
        {children}
      </div>
    </div>
  </TemplateScope>;
}
