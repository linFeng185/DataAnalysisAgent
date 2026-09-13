// 报表模板切换控件：私有查看页与公开分享页共用。
// 切换只影响本机这次浏览，不写回服务端、不改报表版本。

import { Segmented } from 'antd';
import { t } from '../../i18n';
import { reportTemplates, resolveTemplate } from '../../report/templates';

interface TemplateSwitcherProps {
  /** 当前模板 id */
  value: string;
  /** 切换回调 */
  onChange: (id: string) => void;
}

// 方法作用：渲染带色卡的模板选项。
// Args: 无。
// Returns: Segmented 选项数组，label 前用色点预览主色。
function options() {
  return reportTemplates.map(template => {
    const tokens = resolveTemplate(template.id).tokens;
    return {
      value: template.id,
      label: <span className="report-template-option">
        <span
          className="report-template-swatch"
          style={{ background: tokens.accent, borderColor: tokens.border }}
        />
        {t(template.name)}
      </span>,
    };
  });
}

// 方法作用：渲染报表模板切换控件。
// Args: value - 当前模板 id；onChange - 切换回调。
// Returns: Segmented 节点。
export default function TemplateSwitcher({ value, onChange }: TemplateSwitcherProps) {
  return <Segmented
    className="report-template-switcher"
    value={value}
    onChange={next => onChange(String(next))}
    options={options()}
  />;
}
