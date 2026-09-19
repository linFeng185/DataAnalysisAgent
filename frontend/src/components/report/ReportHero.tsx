import { Statistic, Typography } from 'antd';
import { t } from '../../i18n';
import type { ReportBlock, ReportDocument } from '../../types/report';
import type { ReportTemplateTokens } from '../../report/templates';
import { isDarkSurface } from './chartBase';

export const HERO_KPI_LIMIT = 4;

interface ReportHeroProps {
  document: ReportDocument;
  kpis: ReportBlock[];
  tokens: ReportTemplateTokens;
}

// 方法作用：渲染报表首屏横幅及提升后的 KPI 概览。
// Args: document - 当前报表文档；kpis - 按文档顺序提升的 KPI；tokens - 模板令牌。
// Returns: hero 横幅 React 节点。
export default function ReportHero({ document, kpis, tokens }: ReportHeroProps) {
  const dark = isDarkSurface(tokens.heroBg);
  const foreground = dark ? '#ffffff' : tokens.textPrimary;
  const muted = dark ? 'rgba(255,255,255,0.72)' : tokens.textSecondary;
  return <header
    className="report-hero"
    style={{ background: tokens.heroBg, color: foreground }}
  >
    <Typography.Text className="report-hero-eyebrow" style={{ color: muted }}>
      {t('经营分析报表')}
    </Typography.Text>
    <Typography.Title level={1} style={{ color: foreground }}>{document.title}</Typography.Title>
    {document.subtitle ? <Typography.Paragraph style={{ color: muted }}>{document.subtitle}</Typography.Paragraph> : null}
    <Typography.Text className="report-hero-meta" style={{ color: muted }}>
      {t('生成于')} {new Date(document.generated_at).toLocaleString()} {t('· 版本')} {document.version}
    </Typography.Text>
    {kpis.length > 0 ? <div className="report-hero-kpis">
      {kpis.slice(0, HERO_KPI_LIMIT).map(block => <div key={block.id} className="report-hero-kpi">
        <Statistic
          title={<span style={{ color: muted }}>{block.title}</span>}
          value={block.content.value as string | number | undefined}
          suffix={typeof block.content.unit === 'string' ? block.content.unit : undefined}
          valueStyle={{ color: foreground }}
        />
      </div>)}
    </div> : null}
  </header>;
}
