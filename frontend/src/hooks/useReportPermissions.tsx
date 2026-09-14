// 报表能力上下文：把 /report-workbench/permissions/me 的查询收敛成一份共享结果。
//
// 背景（2026-09-14 浏览器实测）：同一个接口在进入报表页时被打了三次——App 挂载一次、
// ReportRoute 守卫一次、报表信息页一次；而守卫还会**阻塞渲染**（结果回来前只显示占位），
// 于是每次在创建报表 / 报表信息 / 链接管理之间点击都要等一次网络往返，表现为"卡一下"。
// 现在只在应用挂载（与角色变化）时取一次，菜单、守卫与页面共用同一份。

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { fetchReportPermissions } from '../api/reportWorkbench';

interface ReportPermissionState {
  /** 当前身份可见的 report.* 能力键 */
  capabilities: string[];
  /** 是否显示「报表工作台」父菜单；未取到结果前为 false */
  menuAllowed: boolean;
  /** 首次结果是否还没回来。**注意**：未就绪不等于无权限，守卫不得据此拦截渲染 */
  loading: boolean;
  /** 主动重取（例如管理员刚改完权限） */
  refresh: () => void;
}

// 默认值代表"尚未取到"，而不是"无权限"：menuAllowed=false 只由真实响应写入。
const ReportPermissionsContext = createContext<ReportPermissionState>({
  capabilities: [],
  menuAllowed: false,
  loading: true,
  refresh: () => undefined,
});

// 方法作用：提供报表能力上下文，并在挂载与角色变化时拉取一次。
// Args: role - 当前用户角色，用于在角色切换后重取；children - 子节点。
// Returns: 包裹子节点的上下文提供者。
export function ReportPermissionsProvider({ role, children }: { role?: string; children: ReactNode }) {
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [menuAllowed, setMenuAllowed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchReportPermissions()
      .then(result => {
        if (!active) return;
        setMenuAllowed(result.menu_allowed);
        setCapabilities(result.capabilities || []);
      })
      .catch(() => {
        // 取不到能力时按"菜单不可见"处理，但不影响其它页面；用户可在角色变化或刷新后重试。
        if (active) { setMenuAllowed(false); setCapabilities([]); }
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [role, version]);

  return <ReportPermissionsContext.Provider
    value={{ capabilities, menuAllowed, loading, refresh: () => setVersion(current => current + 1) }}>
    {children}
  </ReportPermissionsContext.Provider>;
}

// 方法作用：读取报表能力上下文。
// Args: 无。
// Returns: 能力集合、菜单可见性、加载状态与重取函数。
export function useReportPermissions(): ReportPermissionState {
  return useContext(ReportPermissionsContext);
}
