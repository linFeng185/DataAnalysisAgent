import { useState } from 'react';
import { Card, Form, Input, Button, Typography, message } from 'antd';
import { UserOutlined, LockOutlined } from '@ant-design/icons';
import { useAuth } from '../hooks/AuthContext';

export default function LoginPage() {
  const { login } = useAuth();
  const [loading, setLoading] = useState(false);

  const handleLogin = async (v: { username: string; password: string }) => {
    setLoading(true);
    try { await login(v.username, v.password); message.success('登录成功'); }
    catch (e: unknown) { message.error(e instanceof Error ? e.message : '登录失败'); }
    finally { setLoading(false); }
  };

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#f5f5f5' }}>
      <Card style={{ width: 400, borderRadius: 12 }}>
        <Typography.Title level={4} style={{ textAlign: 'center', marginBottom: 24 }}>数据智能体</Typography.Title>
        <Form onFinish={handleLogin} size="large">
          <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input prefix={<UserOutlined />} placeholder="用户名" /></Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="密码" /></Form.Item>
          <Form.Item><Button type="primary" htmlType="submit" loading={loading} block>登录</Button></Form.Item>
        </Form>
      </Card>
    </div>
  );
}
