import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

interface User { user_id: number; tenant_id: number; role: string; username: string; }

interface AuthState {
  user: User | null;
  login: (u: string, p: string) => Promise<void>;
  logout: () => void;
  isAuthenticated: boolean;
  authRequired: boolean;
  loading: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
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
          role: data.role,
          username: data.username || '',
        } : null);
      })
      .catch(() => {
        if (active) setUser(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
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
    navigate('/');
  }, [navigate]);

  const logout = useCallback(() => {
    void fetch('/api/v1/auth/logout', {
      method: 'POST',
      credentials: 'include',
    });
    setUser(null);
    navigate('/login');
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

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth');
  return context;
}
