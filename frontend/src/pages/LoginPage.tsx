import { t, useLanguage } from '../i18n';
import { useState } from 'react';
import { Card, Form, Input, Button, Typography, message } from 'antd';
import { UserOutlined, LockOutlined } from '@ant-design/icons';
import { useAuth } from '../hooks/AuthContext';
import LanguageSwitcher from '../components/LanguageSwitcher';

// 方法作用：渲染租户编码、用户名和密码登录页面。
// Args: 无。
// Returns: 登录 React 页面。
export default function LoginPage() {
  useLanguage();
  const { login } = useAuth();
  const [loading, setLoading] = useState(false);

  // 方法作用：提交租户编码、用户名和密码并建立登录态。
  // Args: v - 登录表单字段。
  // Returns: 登录流程完成后无返回值。
  const handleLogin = async (v: { tenant_code: string; username: string; password: string }) => {
    console.debug('handleLogin 入口', { tenantCode: v.tenant_code, username: v.username });
    setLoading(true);
    try { await login(v.tenant_code, v.username, v.password); message.success(t("登录成功")); }
    catch (e: unknown) { message.error(e instanceof Error ? e.message : t("登录失败")); }
    finally { setLoading(false); console.info('handleLogin 完成'); }
  };

  const loginForm = (
    <Form onFinish={handleLogin} size="large" layout="vertical">
      <Form.Item name="tenant_code" label={t("租户编码")} rules={[{ required: true, pattern: /^[a-z0-9][a-z0-9-]{0,31}$/, message: t("请输入有效租户编码") }]}>
        <Input autoComplete="organization" /></Form.Item>
      <Form.Item name="username" label={t("用户名")} rules={[{ required: true, message: t("请输入用户名") }]}>
        <Input prefix={<UserOutlined />} autoComplete="username" /></Form.Item>
      <Form.Item name="password" label={t("密码")} rules={[{ required: true, message: t("请输入密码") }]}>
        <Input.Password prefix={<LockOutlined />} autoComplete="current-password" /></Form.Item>
      <Form.Item><Button type="primary" htmlType="submit" loading={loading} block>{t("登录")}</Button></Form.Item>
    </Form>
  );

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#f5f5f5' }}>
      <div style={{ position: 'absolute', top: 20, right: 24 }}><LanguageSwitcher /></div>
      <Card style={{ width: 400, maxWidth: 'calc(100vw - 32px)', borderRadius: 8 }}>
        <Typography.Title level={4} style={{ textAlign: 'center', marginBottom: 24 }}>{t("数据智能体")}</Typography.Title>
        {loginForm}
      </Card>
    </div>
  );
}
