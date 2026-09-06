import { Select } from 'antd';
import { setLanguage, useLanguage } from '../i18n';

// 方法作用：在登录页和工作台右上角提供双语切换。
// Args: 无。
// Returns: 持久化语言选择器。
export default function LanguageSwitcher() {
  const language = useLanguage();
  return <Select aria-label="Language / 语言" value={language} onChange={setLanguage} style={{ width: 110 }} options={[{ value: 'zh-CN', label: '中文' }, { value: 'en-US', label: 'English' }]} />;
}
