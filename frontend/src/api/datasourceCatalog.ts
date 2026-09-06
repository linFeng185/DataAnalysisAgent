import { get } from './client';
import type { DatasourceConfig } from '../types';

// 方法作用：分页加载当前身份可见数据源，避免下拉框遗漏第二页以后的资源。
// Args: 无。
// Returns: 当前租户可见的数据源列表。
export async function fetchDatasourceCatalog(): Promise<DatasourceConfig[]> {
  const items: DatasourceConfig[] = [];
  let page = 1;
  while (true) {
    const response = await get<{ datasources: DatasourceConfig[]; total?: number }>(`/datasources?page=${page}&page_size=100`);
    const batch = response.datasources || [];
    items.push(...batch);
    if (batch.length < 100 || (response.total !== undefined && items.length >= response.total)) return items;
    page += 1;
  }
}
