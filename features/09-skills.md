# 9. Skills 技能系统

## 9. Skills 技能系统 `[P0:4 P1:7 P2:5 P3:3]`

### 9.1 Skill 引擎

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.1.1 | Skill dataclass | `src/skill_manager.py` | name / version / description / triggers / depends_on / tools / system_prompt_override / output_schema_extension / source_path / enabled | 开发完成 |
| 9.1.2 | SkillManager 类 | `src/skill_manager.py` | Skill 发现、加载、激活与生命周期管理 | 开发完成 |
| 9.1.3 | discover() | 同上 | 启动时扫描 skills/ 目录，发现所有 SKILL.md | 开发完成 |
| 9.1.4 | _parse_skill_manifest() | 同上 | 解析 SKILL.md 的 YAML frontmatter + Markdown body | 开发完成 |
| 9.1.5 | _check_dependencies() | 同上 | 检查依赖 (mcp_servers / skills / python_packages) 是否满足 | 开发完成 |
| 9.1.6 | match_skills() | 同上 | 根据用户输入匹配激活 Skill: 关键词 + 意图 + 表名三重 OR 匹配 | 开发完成 |
| 9.1.7 | get_active_tools() | 同上 | 动态加载激活 Skill 的 tools.py 模块，获取 BaseTool 列表 | 开发完成 |
| 9.1.8 | build_skill_prompt() | 同上 | 组装激活 Skill 的 system_prompt_override 追加到 System Prompt | 开发完成 |
| 9.1.9 | _load_skill_module() | 同上 | 动态 import Skill 的 tools.py 模块 | 开发完成 |
| 9.1.10 | Skill Manifest v2 与请求授权 | `src/skill_manager.py` | 解析 capabilities/accepts/permissions/resources，并在执行前校验资产类型、网络域名和工具调用预算 | 单测完成 | P1 |
| 9.1.11 | Skill 三级作用域隔离 `[P0]` | `src/skill_manager.py` | system/tenant/private 目录发现、复合标识和 tenant_id/user_id 请求级过滤 | 单测完成 |

### 9.2 示例 Skill — 数据质量检查

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.2.1 | SKILL.md | `skills/data_quality_check/SKILL.md` | 仅明确质量关键词触发，不按所有 query/aggregation 宽泛激活 | 单测完成 |
| 9.2.2 | check_null_rate Tool | `skills/data_quality_check/tools.py` | 检查指定列的空值率 | 开发完成 |
| 9.2.3 | check_duplicates Tool | 同上 | 检查指定列的重复值 | 开发完成 |
| 9.2.4 | detect_outliers Tool | 同上 | Z-Score 异常值检测 | 开发完成 |
| 9.2.5 | PROMPTS 定义 | `skills/data_quality_check/SKILL.md` | Skill 专属 Prompt 模板 (集成在 SKILL.md body 中) | 开发完成 |
| 9.2.6 | 确定性质量报告 `[P1]` | `src/graph/nodes/analyze_result.py` | 激活后真实输出空值率、重复行和异常值，不依赖 LLM 自述 | 单测完成 |

### 9.3 示例 Skill — 自定义报告

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.3.1 | SKILL.md | `skills/custom_report/SKILL.md` | Skill 清单 | 开发完成 |
| 9.3.2 | 周报模板 | `skills/custom_report/templates/weekly_report.jinja2` | Jinja2 模板 — 周度数据报告 | 开发完成 |
| 9.3.3 | 月报模板 | 同上 | Jinja2 模板 — 月度数据报告 | 开发完成 |
| 9.3.4 | 报告渲染工具 | `skills/custom_report/tools.py` | render_report(template_name, data) → 渲染 Markdown | 开发完成 |

### 9.4 Skill 分发

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.4.1 | 本地 Skill 扫描 | `src/skill_manager.py` | 多目录扫描 + 缓存注入 + 手动刷新 | 开发完成 |
| 9.4.2 | Git 子模块支持 | `.gitmodules` | 社区 Skill 用 git submodule 引入 | 待开发 |
| 9.4.3 | Skill Registry 接口 (远期) | `src/skill_manager.py` | 中心化 Skill 市场 | 待开发 |

### 9.5 Skills 管理与 API（新增）

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.5.1 | 列表 API | `src/api/routes.py` | GET /skills — is_builtin/triggers/tools/deps | 开发完成 |
| 9.5.2 | 批量上传 | 同上 | POST /skills/upload — 文件夹递归 + YAML 解析 | 开发完成 |
| 9.5.3 | 启用/禁用/删除 | 同上 | PUT toggle + DELETE（内置保护） | 开发完成 |
| 9.5.4 | 刷新 + 内容 | 同上 | POST /refresh + GET /{name}/content | 开发完成 |
| 9.5.5 | 前端管理页 | `SkillsPage.tsx` | 列表/切换/上传/详情 Modal/刷新/删除 | 开发完成 |
| 9.5.6 | Skill 作用域管理授权 `[P0]` | `src/api/routes.py` | system 仅超管、tenant 仅租户管理员、private 仅本人上传/启停/删除 | 单测完成 |

### 模块收尾

模块功能点共 30 项，已完成 28 项，待开发 2 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 9.4.2 Git 子模块支持 | 当前优先完成本地受管目录的安全隔离，Git 来源还缺少签名、版本锁定和更新审计 | 建立受信任仓库白名单、commit 锁定和安装审计 | Phase 4，Skill 分发增强 |
| 9.4.3 Skill Registry 接口 | 中心化市场涉及发布审核、依赖解析和供应链安全，当前无 Registry 服务 | 完成 Skill 包签名、版本协议和审核工作流 | Phase 4，扩展市场阶段 |

---
