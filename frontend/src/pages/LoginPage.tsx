import { useState } from 'react';
import { Card, Form, Input, Button, Typography, message, Tabs } from 'antd';
import { UserOutlined, LockOutlined } from '@ant-design/icons';
import { useAuth } from '../hooks/AuthContext';

export default function LoginPage() {
  const { login, register, registrationEnabled } = useAuth();
  const [loading, setLoading] = useState(false);

  const handleLogin = async (v: { username: string; password: string }) => {
    console.debug('handleLogin 入口', { username: v.username });
    setLoading(true);
    try { await login(v.username, v.password); message.success('登录成功'); }
    catch (e: unknown) { message.error(e instanceof Error ? e.message : '登录失败'); }
    finally { setLoading(false); console.info('handleLogin 完成'); }
  };

  // 方法作用：提交公开注册表单并沿用服务端 Cookie 登录态。
  // Args: v - 用户名、密码与确认密码。
  // Returns: 注册流程完成后无返回值。
  const handleRegister = async (v: { username: string; password: string; confirm: string }) => {
    console.debug('handleRegister 入口', { username: v.username });
    setLoading(true);
    try { await register(v.username, v.password); message.success('注册成功'); }
    catch (e: unknown) {
      console.error('handleRegister 异常', e);
      message.error(e instanceof Error ? e.message : '注册失败');
    } finally { setLoading(false); console.info('handleRegister 完成'); }
  };

  const loginForm = (
    <Form onFinish={handleLogin} size="large" layout="vertical">
      <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
        <Input prefix={<UserOutlined />} autoComplete="username" /></Form.Item>
      <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}>
        <Input.Password prefix={<LockOutlined />} autoComplete="current-password" /></Form.Item>
      <Form.Item><Button type="primary" htmlType="submit" loading={loading} block>登录</Button></Form.Item>
    </Form>
  );

  const registerForm = (
    <Form onFinish={handleRegister} size="large" layout="vertical">
      <Form.Item name="username" label="用户名" rules={[{ required: true, min: 1, max: 64 }]}>
        <Input prefix={<UserOutlined />} autoComplete="username" /></Form.Item>
      <Form.Item name="password" label="密码" rules={[{ required: true, min: 8, max: 72 }]}>
        <Input.Password prefix={<LockOutlined />} autoComplete="new-password" /></Form.Item>
      <Form.Item name="confirm" label="确认密码" dependencies={['password']} rules={[
        { required: true },
        ({ getFieldValue }) => ({
          validator(_, value) { return !value || getFieldValue('password') === value
            ? Promise.resolve() : Promise.reject(new Error('两次密码不一致')); },
        }),
      ]}>
        <Input.Password prefix={<LockOutlined />} autoComplete="new-password" /></Form.Item>
      <Form.Item><Button type="primary" htmlType="submit" loading={loading} block>注册</Button></Form.Item>
    </Form>
  );

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#f5f5f5' }}>
      <Card style={{ width: 400, maxWidth: 'calc(100vw - 32px)', borderRadius: 8 }}>
        <Typography.Title level={4} style={{ textAlign: 'center', marginBottom: 24 }}>数据智能体</Typography.Title>
        {registrationEnabled ? <Tabs centered items={[
          { key: 'login', label: '登录', children: loginForm },
          { key: 'register', label: '注册', children: registerForm },
        ]} /> : loginForm}
      </Card>
    </div>
  );
}
