import { t, useLanguage } from '../i18n';
import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { message } from 'antd';
import { ApiError, get, post } from '../api/client';
import { clearChatStorage } from './chatStorage';

interface User { user_id: number; tenant_id: number; tenant_code: string; role: string; username: string; }

interface AuthProbe {
  auth_required?: boolean;
  authenticated?: boolean;
  user_id?: number;
  tenant_id?: number;
  tenant_code?: string;
  role?: string;
  username?: string;
}

interface AuthState {
  user: User | null;
  login: (tenantCode: string, username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  isAuthenticated: boolean;
  authRequired: boolean;
  loading: boolean;
  error: string;
  retryAuth: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

// 方法作用：提供认证状态、租户身份和登录退出操作给整个前端应用。
// Args: children - 需要访问认证 Context 的 React 子节点。
// Returns: 包裹认证状态的 React Provider。
export function AuthProvider({ children }: { children: ReactNode }) {
  useLanguage();
  const [user, setUser] = useState<User | null>(null);
  const [authRequired, setAuthRequired] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [probeVersion, setProbeVersion] = useState(0);
  const navigate = useNavigate();

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    get<AuthProbe>('/auth/me')
      .then(data => {
        if (!active) return;
        setAuthRequired(Boolean(data.auth_required));
        setUser(data.authenticated ? {
          user_id: data.user_id || 0,
          tenant_id: data.tenant_id || 0,
          tenant_code: data.tenant_code || '',
          role: data.role || '',
          username: data.username || '',
        } : null);
      })
      .catch((failure: unknown) => {
        if (active) {
          if (failure instanceof ApiError && failure.status === 401) {
            setAuthRequired(true);
            setUser(null);
          } else {
            console.warn('认证服务暂时不可用，保留当前路由等待重试');
            setError(failure instanceof Error ? failure.message : t("认证服务暂时不可用"));
          }
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [probeVersion]);

  // 重新验证浏览器已有会话，不清除 Cookie 或要求用户重新登录。
  // Args: 无。
  // Returns: 无返回值。
  const retryAuth = useCallback(() => {
    setProbeVersion(version => version + 1);
  }, []);

  // 方法作用：使用租户编码、大小写敏感用户名和密码建立认证会话。
  // Args: tenantCode - 全局唯一租户编码；username - 区分大小写的用户名；password - 密码。
  // Returns: 登录成功后无返回值，失败时抛出服务端错误。
  const login = useCallback(async (tenantCode: string, username: string, password: string) => {
    const data = await post<User>('/auth/login', { tenant_code: tenantCode, username, password });
    setError('');
    setAuthRequired(true);
    setUser({
      user_id: data.user_id,
      tenant_id: data.tenant_id,
      tenant_code: data.tenant_code || tenantCode,
      role: data.role,
      username,
    });
    navigate('/');
  }, [navigate]);

  const logout = useCallback(async () => {
    try {
      const response = await fetch('/api/v1/auth/logout', {
        method: 'POST',
        credentials: 'include',
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      clearChatStorage();
      setUser(null);
      navigate('/login');
    } catch (error) {
      console.error('退出登录失败', error);
      message.error(t("退出登录失败"));
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
      error,
      retryAuth,
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
