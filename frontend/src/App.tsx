import { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route, NavLink, useLocation } from 'react-router-dom';
import { Layout, Menu, ConfigProvider, Tag, Typography, theme } from 'antd';
import {
  MessageOutlined, DatabaseOutlined, HistoryOutlined, SettingOutlined,
  CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined,
  ThunderboltOutlined, ReadOutlined,
} from '@ant-design/icons';
import ChatPage from './pages/ChatPage';
import DatasourcePage from './pages/DatasourcePage';
import SchemaPage from './pages/SchemaPage';
import HistoryPage from './pages/HistoryPage';
import SkillsPage from './pages/SkillsPage';
import KnowledgePage from './pages/KnowledgePage';
import ErrorBoundary from './components/ErrorBoundary';
import { get } from './api/client';
import type { HealthResponse } from './types';
import zhCN from 'antd/locale/zh_CN';

const { Header, Sider, Content } = Layout;

function AppContent() {
  const [collapsed, setCollapsed] = useState(false);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const location = useLocation();

  useEffect(() => {
    get<HealthResponse>('/health')
      .then(h => setHealth(h))
      .catch(() => setHealth(null));
  }, []);

  const menuKey = location.pathname === '/' ? 'chat' : location.pathname.replace('/', '') || 'chat';

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 24px', background: '#001529',
      }}>
        <Typography.Text strong style={{ color: '#fff', fontSize: 16 }}>
          数据分析智能体
        </Typography.Text>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          {health ? (
            <>
              <Tag icon={health.llm_available ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
                color={health.llm_available ? 'green' : 'red'}>
                LLM {health.llm_available ? '可用' : '不可用'}
              </Tag>
              <Tag color="blue">{health.datasources ?? '-'} 个数据源</Tag>
            </>
          ) : (
            <Tag icon={<LoadingOutlined />} color="default">检查连接中</Tag>
          )}
        </div>
      </Header>
      <Layout>
        <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed} theme="dark" width={200}>
          <Menu theme="dark" mode="inline" selectedKeys={[menuKey]} style={{ marginTop: 4 }}>
            <Menu.Item key="chat" icon={<MessageOutlined />}>
              <NavLink to="/">对话分析</NavLink>
            </Menu.Item>
            <Menu.Item key="datasource" icon={<DatabaseOutlined />}>
              <NavLink to="/datasource">数据源</NavLink>
            </Menu.Item>
            <Menu.Item key="schema" icon={<SettingOutlined />}>
              <NavLink to="/schema">表结构</NavLink>
            </Menu.Item>
            <Menu.Item key="history" icon={<HistoryOutlined />}>
              <NavLink to="/history">历史</NavLink>
            </Menu.Item>
            <Menu.Item key="skills" icon={<ThunderboltOutlined />}>
              <NavLink to="/skills">Skills</NavLink>
            </Menu.Item>
            <Menu.Item key="knowledge" icon={<ReadOutlined />}>
              <NavLink to="/knowledge">知识库</NavLink>
            </Menu.Item>
          </Menu>
        </Sider>
        <Content style={{ background: '#f5f5f5' }}>
          <ErrorBoundary>
            <Routes>
              <Route path="/" element={<ChatPage />} />
              <Route path="/datasource" element={<DatasourcePage />} />
              <Route path="/schema" element={<SchemaPage />} />
              <Route path="/history" element={<HistoryPage />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
            </Routes>
          </ErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  return (
    <ConfigProvider locale={zhCN} theme={{ algorithm: theme.defaultAlgorithm }}>
      <BrowserRouter>
        <AppContent />
      </BrowserRouter>
    </ConfigProvider>
  );
}
