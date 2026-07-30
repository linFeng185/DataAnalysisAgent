import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { message } from 'antd';

interface User { user_id: number; tenant_id: number; tenant_code: string; role: string; username: string; }

interface AuthState {
  user: User | null;
  login: (tenantCode: string, username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  isAuthenticated: boolean;
  authRequired: boolean;
  loading: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

// 方法作用：提供认证状态、租户身份和登录退出操作给整个前端应用。
// Args: children - 需要访问认证 Context 的 React 子节点。
// Returns: 包裹认证状态的 React Provider。
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [authRequired, setAuthRequired] = useState(true);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    let active = true;
    fetch('/api/v1/auth/me', { credentials: 'include' })
      .then(async response => {
        const data = await response.json().catch(() => ({}));
        if (!active) return;
        setAuthRequired(Boolean(data.auth_required));
        setUser(data.authenticated ? {
          user_id: data.user_id,
          tenant_id: data.tenant_id,
          tenant_code: data.tenant_code || '',
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

  // 方法作用：使用租户编码、大小写敏感用户名和密码建立认证会话。
  // Args: tenantCode - 全局唯一租户编码；username - 区分大小写的用户名；password - 密码。
  // Returns: 登录成功后无返回值，失败时抛出服务端错误。
  const login = useCallback(async (tenantCode: string, username: string, password: string) => {
    console.debug('login 入口', { tenantCode, username });
    const response = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ tenant_code: tenantCode, username, password }),
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
      tenant_code: data.tenant_code || tenantCode,
      role: data.role,
      username,
    });
    console.info('login 完成', { userId: data.user_id });
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
      logout,
      isAuthenticated: user !== null,
      authRequired,
      loading,
    }}>
      {children}
    </AuthContext.Provider>
  );
}

// 方法作用：读取当前组件树中的认证 Context。
// Args: 无。
// Returns: 当前认证状态和认证操作。
export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth');
  return context;
}
