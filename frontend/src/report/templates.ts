// 报表视觉模板注册表。前端是报表视觉的唯一真源：后端只承载 template_id，
// 不持有任何颜色或密度取值，避免两处各存一份必然脱钩。

export interface ReportTemplateTokens {
  /** 主色：KPI 数值、强调元素、图表首色 */
  accent: string;
  /** 主色浅底：标签、高亮行 */
  accentSoft: string;
  /** 页面底色 */
  bg: string;
  /** 卡片 / 区段底色 */
  surface: string;
  /** 分隔线与边框 */
  border: string;
  textPrimary: string;
  textSecondary: string;
  /** 正文与控件字体族 */
  fontFamily: string;
  /** 标题字体族（纸感模板与正文不同） */
  headingFontFamily: string;
  /** 圆角，如 '10px' */
  radius: string;
  /** 卡片阴影；'none' 表示无阴影 */
  shadow: string;
  /** 基础间距刻度，如 '16px' */
  spaceUnit: string;
  /** 控件密度：决定 antd 组件尺寸与图表高度 */
  density: 'comfortable' | 'compact';
  /** 图表分类色板 */
  chartPalette: string[];
}

export interface ReportTemplate {
  id: string;
  /** 中文原文，直接进 t() */
  name: string;
  /** 中文原文，直接进 t() */
  description: string;
  scheme: 'light' | 'dark';
  /** 只写差异，其余继承默认模板。新增模板因此只需约 10 行 */
  tokens: Partial<ReportTemplateTokens>;
}

export interface ResolvedTemplate {
  /** 实际生效的模板 id（回退后） */
  id: string;
  /** 是否发生了回退；供测试与调试使用，不用于界面提示 */
  fallback: boolean;
  scheme: 'light' | 'dark';
  tokens: ReportTemplateTokens;
}

const SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif";

/** 默认模板的完整 token 集，也是其余模板的继承基线。 */
export const insightTokens: ReportTemplateTokens = {
  accent: '#2563eb',
  accentSoft: '#eff6ff',
  bg: '#f7f8fa',
  surface: '#ffffff',
  border: '#e8eaed',
  textPrimary: '#1f2329',
  textSecondary: '#646a73',
  fontFamily: SANS,
  headingFontFamily: SANS,
  radius: '10px',
  shadow: '0 1px 2px rgba(15,23,42,0.04), 0 4px 12px rgba(15,23,42,0.06)',
  spaceUnit: '16px',
  density: 'comfortable',
  chartPalette: ['#2563eb', '#14b8a6', '#f59e0b', '#8b5cf6', '#ef4444', '#0ea5e9', '#84cc16', '#ec4899'],
};

export const DEFAULT_TEMPLATE_ID = 'insight';

/** 内置模板。展示顺序即此数组顺序，insight 恒为第一项与默认项。 */
export const reportTemplates: ReportTemplate[] = [
  {
    id: 'insight',
    name: '洞察',
    description: '专业分析风，紧凑清晰，适合日常经营分析',
    scheme: 'light',
    tokens: {},
  },
  {
    id: 'ink',
    name: '墨玉',
    description: '深色看板，适合投屏与大屏展示',
    scheme: 'dark',
    tokens: {
      accent: '#38bdf8',
      accentSoft: '#0c4a6e',
      bg: '#0b1220',
      surface: '#111c2e',
      border: '#1e293b',
      textPrimary: '#e2e8f0',
      textSecondary: '#94a3b8',
      shadow: '0 1px 2px rgba(0,0,0,0.4), 0 8px 24px rgba(0,0,0,0.5)',
      chartPalette: ['#38bdf8', '#34d399', '#fbbf24', '#a78bfa', '#f87171', '#22d3ee', '#a3e635', '#f472b6'],
    },
  },
  {
    id: 'paper',
    name: '纸感',
    description: '米白底与衬线标题，适合长文叙事型报告',
    scheme: 'light',
    tokens: {
      accent: '#9a3412',
      accentSoft: '#fef6ee',
      bg: '#faf8f3',
      surface: '#fffdf9',
      border: '#e7e0d3',
      textPrimary: '#2b2620',
      textSecondary: '#6b6357',
      headingFontFamily: "Georgia, 'Songti SC', 'SimSun', serif",
      radius: '4px',
      shadow: 'none',
      spaceUnit: '20px',
      chartPalette: ['#9a3412', '#0f766e', '#b45309', '#4d7c0f', '#7c2d12', '#0369a1', '#a16207', '#be185d'],
    },
  },
  {
    id: 'business',
    name: '商务',
    description: '中性灰蓝，克制方正，适合对外汇报',
    scheme: 'light',
    tokens: {
      accent: '#1e3a5f',
      accentSoft: '#eef2f7',
      bg: '#f4f5f7',
      surface: '#ffffff',
      border: '#dfe3e8',
      textPrimary: '#1a1f27',
      textSecondary: '#5c6672',
      radius: '2px',
      shadow: '0 1px 2px rgba(16,24,40,0.06)',
      spaceUnit: '14px',
      density: 'compact',
      chartPalette: ['#1e3a5f', '#5c7a99', '#8aa4bf', '#c4d2e0', '#2f6f4e', '#7a9e7e', '#a8763e', '#c9a97a'],
    },
  },
];

// 注册表自检：id 唯一。这是开发期错误，必须在模块加载时吵出来，而不是在界面静默降级。
const _ids = new Set<string>();
for (const template of reportTemplates) {
  if (_ids.has(template.id)) throw new Error(`报表模板 id 重复：${template.id}`);
  _ids.add(template.id);
}

// 方法作用：把模板 id 解析为完整模板，未知 id 静默回退默认模板。
// Args: id - 页面文档或用户选择中的模板标识，可能为空或未知值。
// Returns: token 已补全的模板对象，绝不抛错。
export function resolveTemplate(id: string | null | undefined): ResolvedTemplate {
  const found = reportTemplates.find(item => item.id === id);
  const template = found ?? reportTemplates[0];
  return {
    id: template.id,
    fallback: found === undefined,
    scheme: template.scheme,
    // 缺键一律继承默认模板，保证任何模板都不可能缺键。
    tokens: { ...insightTokens, ...template.tokens },
  };
}

// 方法作用：把 token 映射为报表容器上的 CSS 变量。
// Args: tokens - 已补全的模板 token。
// Returns: 以 --report- 为前缀的内联样式对象，作用域限定在报表容器内。
export function cssVariables(tokens: ReportTemplateTokens): Record<string, string> {
  return {
    '--report-accent': tokens.accent,
    '--report-accent-soft': tokens.accentSoft,
    '--report-bg': tokens.bg,
    '--report-surface': tokens.surface,
    '--report-border': tokens.border,
    '--report-text-primary': tokens.textPrimary,
    '--report-text-secondary': tokens.textSecondary,
    '--report-font': tokens.fontFamily,
    '--report-heading-font': tokens.headingFontFamily,
    '--report-radius': tokens.radius,
    '--report-shadow': tokens.shadow,
    '--report-space-unit': tokens.spaceUnit,
  };
}

// 方法作用：把 token 映射为图表与卡片的尺寸取值。
// Args: tokens - 已补全的模板 token。
// Returns: 图表高度与卡片内边距。
export function densityMetrics(tokens: ReportTemplateTokens): { chartHeight: number; cardPadding: number } {
  return tokens.density === 'compact'
    ? { chartHeight: 280, cardPadding: 12 }
    : { chartHeight: 320, cardPadding: 16 };
}
