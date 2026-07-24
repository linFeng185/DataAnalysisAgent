import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { message } from 'antd';

interface User { user_id: number; tenant_id: number; role: string; username: string; }

interface AuthState {
  user: User | null;
  login: (u: string, p: string) => Promise<void>;
  register: (u: string, p: string) => Promise<void>;
  logout: () => Promise<void>;
  isAuthenticated: boolean;
  authRequired: boolean;
  registrationEnabled: boolean;
  loading: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [authRequired, setAuthRequired] = useState(true);
  const [registrationEnabled, setRegistrationEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    let active = true;
    fetch('/api/v1/auth/me', { credentials: 'include' })
      .then(async response => {
        const data = await response.json().catch(() => ({}));
        if (!active) return;
        setAuthRequired(Boolean(data.auth_required));
        setRegistrationEnabled(Boolean(data.registration_enabled));
        setUser(data.authenticated ? {
          user_id: data.user_id,
          tenant_id: data.tenant_id,
          role: data.role,
          username: data.username || '',
        } : null);
      })
      .catch(() => {
        if (active) { setAuthRequired(true); setUser(null); }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    console.debug('login 入口', { username });
    const response = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: '登录失败' }));
      throw new Error(error.detail || '登录失败');
    }
    const data = await response.json();
    setAuthRequired(true);
    setUser({
      user_id: data.user_id,
      tenant_id: data.tenant_id,
      role: data.role,
      username,
    });
    console.info('login 完成', { userId: data.user_id });
    navigate('/');
  }, [navigate]);

  // 方法作用：通过服务端公开注册开关创建默认租户 analyst 并建立登录态。
  // Args: username - 用户名；password - 密码。
  // Returns: 注册成功后无返回值。
  const register = useCallback(async (username: string, password: string) => {
    console.debug('register 入口', { username });
    const response = await fetch('/api/v1/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: '注册失败' }));
      console.error('register 异常', { status: response.status });
      throw new Error(error.detail || '注册失败');
    }
    const data = await response.json();
    setUser({
      user_id: data.user_id,
      tenant_id: data.tenant_id,
      role: data.role,
      username,
    });
    console.info('register 完成', { userId: data.user_id });
    navigate('/');
  }, [navigate]);

  const logout = useCallback(async () => {
    try {
      const response = await fetch('/api/v1/auth/logout', {
        method: 'POST',
        credentials: 'include',
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setUser(null);
      navigate('/login');
    } catch (error) {
      console.error('退出登录失败', error);
      message.error('退出登录失败');
    }
  }, [navigate]);

  return (
    <AuthContext.Provider value={{
      user,
      login,
      register,
      logout,
      isAuthenticated: user !== null,
      authRequired,
      registrationEnabled,
      loading,
    }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth');
  return context;
}
