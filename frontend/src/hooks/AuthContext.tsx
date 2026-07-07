import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

interface User { user_id: number; tenant_id: number; role: string; username: string; }

interface AuthState {
  user: User | null; token: string | null;
  login: (u: string, p: string) => Promise<void>;
  logout: () => void; isAuthenticated: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

function decodeJWT(token: string): User | null {
  try {
    const p = JSON.parse(atob(token.split('.')[1]));
    return p.exp * 1000 > Date.now()
      ? { user_id: p.user_id, tenant_id: p.tenant_id, role: p.role, username: '' } : null;
  } catch { return null; }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(() => {
    const t = localStorage.getItem('auth_token');
    return t ? decodeJWT(t) : null;
  });
  const [token, setToken] = useState<string | null>(() => localStorage.getItem('auth_token'));
  const navigate = useNavigate();

  useEffect(() => { token ? localStorage.setItem('auth_token', token) : localStorage.removeItem('auth_token'); }, [token]);

  const login = useCallback(async (u: string, p: string) => {
    const r = await fetch('/api/v1/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p }),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({ detail: '失败' })); throw new Error(e.detail); }
    const d = await r.json();
    setToken(d.access_token); setUser({ user_id: d.user_id, tenant_id: d.tenant_id, role: d.role, username: u });
    navigate('/');
  }, [navigate]);

  const logout = useCallback(() => {
    setToken(null); setUser(null); localStorage.removeItem('auth_token'); navigate('/login');
  }, [navigate]);

  return <AuthContext.Provider value={{ user, token, login, logout, isAuthenticated: !!token }}>{children}</AuthContext.Provider>;
}

export function useAuth() { const c = useContext(AuthContext); if (!c) throw new Error('useAuth'); return c; }
