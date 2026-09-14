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
  FileTextOutlined, UnorderedListOutlined,
} from '@ant-design/icons';
import { AuthProvider } from './hooks/AuthContext';
import { useAuth } from './hooks/AuthContext';
import { ChatProvider } from './hooks/useChat';
import ErrorBoundary from './components/ErrorBoundary';
import NotificationBell from './components/notifications/NotificationBell';
import { get } from './api/client';
import { ReportPermissionsProvider, useReportPermissions } from './hooks/useReportPermissions';
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
const ReportWorkbenchPage = lazy(() => import('./pages/ReportWorkbenchPage'));
const ReportCreatePage = lazy(() => import('./pages/ReportCreatePage'));
const ReportListPage = lazy(() => import('./pages/ReportListPage'));
const LinkManagementPage = lazy(() => import('./pages/LinkManagementPage'));
const ReportViewerPage = lazy(() => import('./pages/ReportViewerPage'));
const SharedReportPage = lazy(() => import('./pages/SharedReportPage'));
const LoginPage = lazy(() => import('./pages/LoginPage'));
const NotificationsPage = lazy(() => import('./pages/NotificationsPage'));

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
  const { loading, authRequired, isAuthenticated, error, retryAuth, user } = useAuth();
  if (loading) return <Layout style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}><div role="status"><LoadingOutlined /> {t("页面加载中...")}</div></Layout>;
  if (error) return <Layout style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
    <Result status="warning" title={t("暂时无法确认登录状态")} subTitle={error}
      extra={<Button type="primary" onClick={retryAuth}>{t("重试验证")}</Button>} />
  </Layout>;
  if (authRequired && !isAuthenticated) return <Navigate to="/login" replace />;
  return <ChatProvider>
    {/* 报表能力在整个受保护区域共享：菜单、路由守卫与报表页读同一份，避免各自重复查询 */}
    <ReportPermissionsProvider role={user?.role}>
      <AppContent />
    </ReportPermissionsProvider>
  </ChatProvider>;
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
// Args: children - 通过校验后渲染的子页面（创建报表/报表信息/链接管理）；缺省时回退到旧工作台主页。
// Returns: 目标子页面、加载占位或首页重定向。
function ReportRoute({ children }: { children?: ReactNode }) {
  useLanguage();
  const { user } = useAuth();
  const { menuAllowed, loading } = useReportPermissions();

  if (user?.role === 'super_admin') return <Navigate to="/admin" replace />;
  // 结果未就绪时**直接渲染子页**，不再阻塞：能力未到不等于无权限，而所有报表接口
  // 都在服务端按能力键鉴权，前端早渲染一会儿不会放大权限（无权限时下面这一条会立刻重定向）。
  if (loading) return <>{children ?? <ReportWorkbenchPage />}</>;
  return menuAllowed ? <>{children ?? <ReportWorkbenchPage />}</> : <Navigate to="/" replace />;
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
  // 报表能力由 ReportPermissionsProvider 统一拉取一次，这里与路由守卫、报表信息页共用。
  const { menuAllowed: reportMenuAllowed, capabilities: reportCapabilities } = useReportPermissions();

  useEffect(() => {
    get<HealthResponse>('/health')
      .then(h => setHealth(h))
      .catch(() => setHealth(null))
      .finally(() => setHealthChecked(true));
  }, []);

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
    // 报表工作台拆成三个子页：父项沿用 report.menu，子项各按自己的能力键控制可见性。
    // key 与路径一致，menuKey 由 pathname 推导，子页路径变更后仍能高亮。
    reportMenuAllowed === true && user?.role !== 'super_admin'
      ? {
        key: 'report-workbench',
        icon: <ScheduleOutlined />,
        label: <span>{t("报表工作台")}</span>,
        children: [
          reportCapabilities.includes('report.create_page')
            ? { key: 'report-workbench/create', icon: <FileTextOutlined />, label: <NavLink to="/report-workbench/create">{t("创建报表")}</NavLink> }
            : null,
          reportCapabilities.includes('report.list')
            ? { key: 'report-workbench/jobs', icon: <UnorderedListOutlined />, label: <NavLink to="/report-workbench/jobs">{t("报表信息")}</NavLink> }
            : null,
          reportCapabilities.includes('report.link_manage')
            ? { key: 'report-workbench/links', icon: <LinkOutlined />, label: <NavLink to="/report-workbench/links">{t("链接管理")}</NavLink> }
            : null,
        ].filter((item): item is NonNullable<typeof item> => item !== null),
      }
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
      // 深层链接直接落在报表子页时展开父菜单：openKeys 非受控，只在首次挂载生效。
      defaultOpenKeys={location.pathname.startsWith('/report-workbench') ? ['report-workbench'] : []}
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
          {/* 通知铃铛走顶栏而非菜单：收件箱能力已由抽屉覆盖，避免继续挤压侧栏菜单项 */}
          <NotificationBell enabled={Boolean(user)} viewAllPath="/notifications" />
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
                {/* 旧路径重定向到创建报表页；旧「自动化」菜单与路由已按需求 1.8 摘除 */}
                <Route path="/report-workbench" element={<Navigate to="/report-workbench/create" replace />} />
                <Route path="/report-workbench/create" element={<ReportRoute><ReportCreatePage /></ReportRoute>} />
                <Route path="/report-workbench/jobs" element={<ReportRoute><ReportListPage /></ReportRoute>} />
                <Route path="/report-workbench/links" element={<ReportRoute><LinkManagementPage /></ReportRoute>} />
                <Route path="/reports/:reportId" element={<ReportViewerPage />} />
                <Route path="/mcp" element={<McpPage />} />
                <Route path="/admin" element={<AdminRoute />} />
                <Route path="/notifications" element={<NotificationsPage />} />
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
