import { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route, NavLink, Navigate, useLocation } from 'react-router-dom';
import { Layout, Menu, ConfigProvider, Tag, Typography, theme, Grid, Button, Drawer } from 'antd';
import {
  MessageOutlined, DatabaseOutlined, HistoryOutlined, SettingOutlined,
  CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined,
  ThunderboltOutlined, ReadOutlined, ApiOutlined,
  SafetyCertificateOutlined, LogoutOutlined, UserOutlined,
  MenuOutlined, ScheduleOutlined,
} from '@ant-design/icons';
import ChatPage from './pages/ChatPage';
import DatasourcePage from './pages/DatasourcePage';
import SchemaPage from './pages/SchemaPage';
import HistoryPage from './pages/HistoryPage';
import SkillsPage from './pages/SkillsPage';
import KnowledgePage from './pages/KnowledgePage';
import McpPage from './pages/McpPage';
import AdminPage from './pages/AdminPage';
import AutomationPage from './pages/AutomationPage';
import LoginPage from './pages/LoginPage';
import { AuthProvider } from './hooks/AuthContext';
import { useAuth } from './hooks/AuthContext';
import ErrorBoundary from './components/ErrorBoundary';
import { get } from './api/client';
import type { HealthResponse } from './types';
import zhCN from 'antd/locale/zh_CN';

const { Header, Sider, Content } = Layout;

// 方法作用：等待认证状态并阻止未登录会话进入工作台。
// Args: 无。
// Returns: 工作台、登录重定向或加载占位。
function ProtectedApp() {
  const { loading, authRequired, isAuthenticated } = useAuth();
  if (loading) return <Layout style={{ minHeight: '100vh' }} />;
  if (authRequired && !isAuthenticated) return <Navigate to="/login" replace />;
  return <AppContent />;
}

// 方法作用：仅允许固定角色的前端会话进入平台管理路由。
// Args: 无，读取 AuthContext 当前身份。
// Returns: 超级管理员页面或首页重定向。
function AdminRoute() {
  const { user } = useAuth();
  console.debug('AdminRoute 入口', { role: user?.role || '' });
  const result = user?.role === 'super_admin' && user.user_id === 1
    ? <AdminPage /> : <Navigate to="/" replace />;
  console.info('AdminRoute 完成', { allowed: user?.role === 'super_admin' && user.user_id === 1 });
  return result;
}

// 方法作用：渲染桌面侧栏或移动抽屉导航及当前业务路由。
// Args: 无。
// Returns: 已认证应用工作台。
function AppContent() {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const location = useLocation();
  const screens = Grid.useBreakpoint();
  const isCompact = screens.md === false;
  const { user, logout } = useAuth();

  useEffect(() => {
    get<HealthResponse>('/health')
      .then(h => setHealth(h))
      .catch(() => setHealth(null));
  }, []);

  const menuKey = location.pathname === '/' ? 'chat' : location.pathname.replace('/', '') || 'chat';
  const navigationMenu = (
    <Menu
      theme="dark"
      mode="inline"
      selectedKeys={[menuKey]}
      style={{ marginTop: 4 }}
      onClick={() => setMobileNavOpen(false)}
    >
      <Menu.Item key="chat" icon={<MessageOutlined />}>
        <NavLink to="/">对话分析</NavLink>
      </Menu.Item>
      {user?.role !== 'viewer' && <Menu.Item key="datasource" icon={<DatabaseOutlined />}>
        <NavLink to="/datasource">数据源</NavLink>
      </Menu.Item>}
      {user?.role !== 'viewer' && <Menu.Item key="schema" icon={<SettingOutlined />}>
        <NavLink to="/schema">表结构</NavLink>
      </Menu.Item>}
      <Menu.Item key="history" icon={<HistoryOutlined />}>
        <NavLink to="/history">历史</NavLink>
      </Menu.Item>
      {user?.role !== 'viewer' && <Menu.Item key="skills" icon={<ThunderboltOutlined />}>
        <NavLink to="/skills">Skills</NavLink>
      </Menu.Item>}
      {user?.role !== 'viewer' && <Menu.Item key="knowledge" icon={<ReadOutlined />}>
        <NavLink to="/knowledge">知识库</NavLink>
      </Menu.Item>}
      {user?.role !== 'viewer' && <Menu.Item key="automation" icon={<ScheduleOutlined />}>
        <NavLink to="/automation">自动化</NavLink>
      </Menu.Item>}
      {['super_admin', 'tenant_admin'].includes(user?.role || '') && <Menu.Item key="mcp" icon={<ApiOutlined />}>
        <NavLink to="/mcp">MCP</NavLink>
      </Menu.Item>}
      {user?.role === 'super_admin' && user.user_id === 1 && <Menu.Item key="admin" icon={<SafetyCertificateOutlined />}>
        <NavLink to="/admin">平台管理</NavLink>
      </Menu.Item>}
    </Menu>
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
              aria-label="打开导航"
              type="text"
              icon={<MenuOutlined />}
              onClick={() => setMobileNavOpen(true)}
              style={{ color: '#fff', marginRight: 4 }}
            />
          )}
          <Typography.Text className="app-title" strong style={{ color: '#fff', fontSize: 16 }}>
            数据分析智能体
          </Typography.Text>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          {health ? (
            <span className="mobile-health" style={{ display: 'contents' }}>
              <Tag icon={health.llm_available ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
                color={health.llm_available ? 'green' : 'red'}>
                LLM {health.llm_available ? '可用' : '不可用'}
              </Tag>
              <Tag color="blue">{health.datasources ?? '-'} 个数据源</Tag>
            </span>
          ) : (
            <Tag className="mobile-health" icon={<LoadingOutlined />} color="default">检查连接中</Tag>
          )}
          <Tag icon={<UserOutlined />}>{user?.username || `用户 ${user?.user_id ?? ''}`}</Tag>
          <Tag className="role-tag" color={user?.role === 'super_admin' ? 'red' : 'default'}>{user?.role || '-'}</Tag>
          <Button type="text" icon={<LogoutOutlined />} onClick={() => void logout()}
            style={{ color: '#fff' }}><span className="logout-label">退出</span></Button>
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
          title="导航"
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
            <Routes>
              <Route path="/" element={<ChatPage />} />
              <Route path="/datasource" element={<DatasourcePage />} />
              <Route path="/schema" element={<SchemaPage />} />
              <Route path="/history" element={<HistoryPage />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/automation" element={<AutomationPage />} />
              <Route path="/mcp" element={<McpPage />} />
              <Route path="/admin" element={<AdminRoute />} />
            </Routes>
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
  return (
    <ConfigProvider locale={zhCN} theme={{ algorithm: theme.defaultAlgorithm }}>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/*" element={<ProtectedApp />} />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </ConfigProvider>
  );
}
