import { lazy, Suspense, useState, useEffect } from 'react';
import type { ReactNode } from 'react';
import { BrowserRouter, Routes, Route, NavLink, Navigate, useLocation } from 'react-router-dom';
import { Layout, Menu, ConfigProvider, Tag, Typography, theme, Grid, Button, Drawer, Result } from 'antd';
import type { MenuProps } from 'antd';
import {
  MessageOutlined, DatabaseOutlined, HistoryOutlined, SettingOutlined,
  CheckCircleOutlined, LoadingOutlined,
  ThunderboltOutlined, ReadOutlined, ApiOutlined,
  SafetyCertificateOutlined, LogoutOutlined, UserOutlined,
  MenuOutlined, ScheduleOutlined, BulbOutlined, LinkOutlined,
} from '@ant-design/icons';
import { AuthProvider } from './hooks/AuthContext';
import { useAuth } from './hooks/AuthContext';
import { ChatProvider } from './hooks/useChat';
import ErrorBoundary from './components/ErrorBoundary';
import { get } from './api/client';
import { fetchReportPermissions } from './api/reportWorkbench';
import type { HealthResponse } from './types';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import 'dayjs/locale/en';
import { t, useLanguage } from './i18n';
import LanguageSwitcher from './components/LanguageSwitcher';

const { Header, Sider, Content } = Layout;

const ChatPage = lazy(() => import('./pages/ChatPage'));
const DatasourcePage = lazy(() => import('./pages/DatasourcePage'));
const SchemaPage = lazy(() => import('./pages/SchemaPage'));
const HistoryPage = lazy(() => import('./pages/HistoryPage'));
const SkillsPage = lazy(() => import('./pages/SkillsPage'));
const KnowledgePage = lazy(() => import('./pages/KnowledgePage'));
const MemoryPage = lazy(() => import('./pages/MemoryPage'));
const MetricsPage = lazy(() => import('./pages/MetricsPage'));
const McpPage = lazy(() => import('./pages/McpPage'));
const AdminPage = lazy(() => import('./pages/AdminPage'));
const AutomationPage = lazy(() => import('./pages/AutomationPage'));
const ReportWorkbenchPage = lazy(() => import('./pages/ReportWorkbenchPage'));
const LinkManagementPage = lazy(() => import('./pages/LinkManagementPage'));
const ReportViewerPage = lazy(() => import('./pages/ReportViewerPage'));
const SharedReportPage = lazy(() => import('./pages/SharedReportPage'));
const LoginPage = lazy(() => import('./pages/LoginPage'));

const pageFallback = (
  <div style={{ minHeight: 240, display: 'grid', placeItems: 'center' }}>
    {t("页面加载中...")}
  </div>
);

// 方法作用：等待认证状态并阻止未登录会话进入工作台。
// Args: 无。
// Returns: 工作台、登录重定向或加载占位。
function ProtectedApp() {
  useLanguage();
  const { loading, authRequired, isAuthenticated, error, retryAuth } = useAuth();
  if (loading) return <Layout style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}><div role="status"><LoadingOutlined /> {t("页面加载中...")}</div></Layout>;
  if (error) return <Layout style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
    <Result status="warning" title={t("暂时无法确认登录状态")} subTitle={error}
      extra={<Button type="primary" onClick={retryAuth}>{t("重试验证")}</Button>} />
  </Layout>;
  if (authRequired && !isAuthenticated) return <Navigate to="/login" replace />;
  return <ChatProvider><AppContent /></ChatProvider>;
}

// 方法作用：允许平台超级管理员或当前租户管理员进入角色化管理工作区。
// Args: 无，读取 AuthContext 当前身份。
// Returns: 超级管理员页面或首页重定向。
function AdminRoute() {
  useLanguage();
  const { user } = useAuth();
  const result = ['super_admin', 'tenant_admin'].includes(user?.role || '')
    ? <AdminPage /> : <Navigate to="/" replace />;
  return result;
}

// 方法作用：在进入报表工作台及其子页面前再次校验服务端菜单能力，防止仅靠前端隐藏绕过权限。
// Args: children - 通过校验后渲染的子页面；缺省时渲染报表工作台主页。
// Returns: 目标子页面、加载占位或首页重定向。
function ReportRoute({ children }: { children?: ReactNode }) {
  useLanguage();
  const { user } = useAuth();
  const [allowed, setAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    let active = true;
    fetchReportPermissions()
      .then(result => { if (active) setAllowed(result.menu_allowed); })
      .catch(() => { if (active) setAllowed(false); });
    return () => { active = false; };
  }, [user?.role]);

  if (user?.role === 'super_admin') return <Navigate to="/admin" replace />;
  if (allowed === null) return pageFallback;
  return allowed ? <>{children ?? <ReportWorkbenchPage />}</> : <Navigate to="/" replace />;
}

// 方法作用：渲染桌面侧栏或移动抽屉导航及当前业务路由。
// Args: 无。
// Returns: 已认证应用工作台。
function AppContent() {
  useLanguage();
  const [collapsed, setCollapsed] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthChecked, setHealthChecked] = useState(false);
  const location = useLocation();
  const screens = Grid.useBreakpoint();
  const isCompact = screens.md === false;
  const { user, logout } = useAuth();
  const [reportMenuAllowed, setReportMenuAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    get<HealthResponse>('/health')
      .then(h => setHealth(h))
      .catch(() => setHealth(null))
      .finally(() => setHealthChecked(true));
  }, []);

  useEffect(() => {
    let active = true;
    fetchReportPermissions()
      .then(result => { if (active) setReportMenuAllowed(result.menu_allowed); })
      .catch(() => { if (active) setReportMenuAllowed(false); });
    return () => { active = false; };
  }, [user?.role]);

  const menuKey = location.pathname === '/' ? 'chat' : location.pathname.replace('/', '') || 'chat';
  const navigationItems: MenuProps['items'] = [
    { key: 'chat', icon: <MessageOutlined />, label: <NavLink to="/">{t("对话分析")}</NavLink> },
    user?.role !== 'viewer'
      ? { key: 'datasource', icon: <DatabaseOutlined />, label: <NavLink to="/datasource">{t("数据源")}</NavLink> }
      : null,
    user?.role !== 'viewer'
      ? { key: 'schema', icon: <SettingOutlined />, label: <NavLink to="/schema">{t("表结构")}</NavLink> }
      : null,
    { key: 'history', icon: <HistoryOutlined />, label: <NavLink to="/history">{t("历史")}</NavLink> },
    ['tenant_admin', 'analyst', 'viewer'].includes(user?.role || '')
      ? { key: 'metric-catalog', icon: <ReadOutlined />, label: <NavLink to="/metric-catalog">{t("指标目录")}</NavLink> }
      : null,
    user?.role !== 'viewer'
      ? { key: 'skills', icon: <ThunderboltOutlined />, label: <NavLink to="/skills">Skills</NavLink> }
      : null,
    user?.role !== 'viewer'
      ? { key: 'knowledge', icon: <ReadOutlined />, label: <NavLink to="/knowledge">{t("知识库")}</NavLink> }
      : null,
    { key: 'memory', icon: <BulbOutlined />, label: <NavLink to="/memory">{t("我的记忆")}</NavLink> },
    reportMenuAllowed === true && user?.role !== 'super_admin'
      ? { key: 'report-workbench', icon: <ScheduleOutlined />, label: <NavLink to="/report-workbench">{t("报表工作台")}</NavLink> }
      : null,
    // 链接管理作为独立菜单项紧跟报表工作台；后续批次合并成子菜单时只需替换这两项。
    // key 与路径一致，menuKey 由 pathname 推导，路径变更后仍能高亮。
    reportMenuAllowed === true && user?.role !== 'super_admin'
      ? { key: 'report-workbench/links', icon: <LinkOutlined />, label: <NavLink to="/report-workbench/links">{t("链接管理")}</NavLink> }
      : null,
    user?.role !== 'viewer'
      ? { key: 'automation', icon: <ScheduleOutlined />, label: <NavLink to="/automation">{t("自动化")}</NavLink> }
      : null,
    ['super_admin', 'tenant_admin'].includes(user?.role || '')
      ? { key: 'mcp', icon: <ApiOutlined />, label: <NavLink to="/mcp">MCP</NavLink> }
      : null,
    ['super_admin', 'tenant_admin'].includes(user?.role || '')
      ? { key: 'admin', icon: <SafetyCertificateOutlined />, label: <NavLink to="/admin">{t("管理工作区")}</NavLink> }
      : null,
  ].filter((item): item is NonNullable<typeof item> => item !== null);
  const navigationMenu = (
    <Menu
      theme="dark"
      mode="inline"
      selectedKeys={[menuKey]}
      items={navigationItems}
      style={{ marginTop: 4 }}
      onClick={() => setMobileNavOpen(false)}
    />
  );

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header className="app-header" style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 24px', background: '#001529',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', minWidth: 0 }}>
          {isCompact && (
            <Button
              aria-label={t("打开导航")}
              type="text"
              icon={<MenuOutlined />}
              onClick={() => setMobileNavOpen(true)}
              style={{ color: '#fff', marginRight: 4 }}
            />
          )}
          <Typography.Text className="app-title" strong style={{ color: '#fff', fontSize: 16 }}>
            {t("数据分析智能体")}
          </Typography.Text>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <LanguageSwitcher />
          {health ? (
            <span className="mobile-health" style={{ display: 'contents' }}>
              <Tag icon={<CheckCircleOutlined />} color="green">{t("服务正常")}</Tag>
            </span>
          ) : healthChecked ? (
            <Tag className="mobile-health" color="red">{t("服务不可用")}</Tag>
          ) : (
            <Tag className="mobile-health" icon={<LoadingOutlined />} color="default">{t("检查连接中")}</Tag>
          )}
          <Tag icon={<UserOutlined />}>{user?.tenant_code ? `${user.tenant_code} / ` : ''}{user?.username || t("用户 {value0}", { value0: user?.user_id ?? '' })}</Tag>
          <Tag className="role-tag" color={user?.role === 'super_admin' ? 'red' : 'default'}>{user?.role || '-'}</Tag>
          <Button type="text" icon={<LogoutOutlined />} onClick={() => void logout()}
            style={{ color: '#fff' }}><span className="logout-label">{t("退出")}</span></Button>
        </div>
      </Header>
      <Layout>
        {!isCompact && (
          <Sider collapsible collapsed={collapsed} collapsedWidth={80}
            onCollapse={setCollapsed} theme="dark" width={200}>
            {navigationMenu}
          </Sider>
        )}
        <Drawer
          className="mobile-nav-drawer"
          title={t("导航")}
          placement="left"
          width={260}
          open={isCompact && mobileNavOpen}
          onClose={() => setMobileNavOpen(false)}
          styles={{ body: { padding: 0, background: '#001529' } }}
        >
          {navigationMenu}
        </Drawer>
        <Content className="app-content" style={{ background: '#f5f5f5', minWidth: 0 }}>
          <ErrorBoundary>
            <Suspense fallback={pageFallback}>
              <Routes>
                <Route path="/" element={<ChatPage />} />
                <Route path="/datasource" element={<DatasourcePage />} />
                <Route path="/schema" element={<SchemaPage />} />
                <Route path="/history" element={<HistoryPage />} />
                <Route path="/skills" element={<SkillsPage />} />
                <Route path="/knowledge" element={<KnowledgePage />} />
                <Route path="/memory" element={<MemoryPage />} />
                <Route path="/metric-catalog" element={<MetricsPage />} />
                <Route path="/report-workbench" element={<ReportRoute />} />
                <Route path="/report-workbench/links" element={<ReportRoute><LinkManagementPage /></ReportRoute>} />
                <Route path="/reports/:reportId" element={<ReportViewerPage />} />
                <Route path="/automation" element={<AutomationPage />} />
                <Route path="/mcp" element={<McpPage />} />
                <Route path="/admin" element={<AdminRoute />} />
              </Routes>
            </Suspense>
          </ErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  );
}

// 方法作用：装配主题、认证 Context 和浏览器路由。
// Args: 无。
// Returns: 应用根组件。
export default function App() {
  const language = useLanguage();
  dayjs.locale(language === 'en-US' ? 'en' : 'zh-cn');
  return (
    <ConfigProvider locale={language === 'en-US' ? enUS : zhCN} theme={{ algorithm: theme.defaultAlgorithm }}>
      <BrowserRouter>
        <AuthProvider>
        <Routes>
          <Route path="/login" element={<Suspense fallback={pageFallback}><LoginPage /></Suspense>} />
          <Route path="/shared/reports/:token" element={<Suspense fallback={pageFallback}><SharedReportPage /></Suspense>} />
          <Route path="/*" element={<ProtectedApp />} />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </ConfigProvider>
  );
}
