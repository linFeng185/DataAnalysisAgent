# 9. Skills 技能系统

## 9. Skills 技能系统 `[P0:4 P1:6 P2:5 P3:3]`

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

### 9.2 示例 Skill — 数据质量检查

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.2.1 | SKILL.md | `skills/data_quality_check/SKILL.md` | Skill 清单 (YAML frontmatter + 指令) | 开发完成 |
| 9.2.2 | check_null_rate Tool | `skills/data_quality_check/tools.py` | 检查指定列的空值率 | 开发完成 |
| 9.2.3 | check_duplicates Tool | 同上 | 检查指定列的重复值 | 开发完成 |
| 9.2.4 | detect_outliers Tool | 同上 | Z-Score 异常值检测 | 开发完成 |
| 9.2.5 | PROMPTS 定义 | `skills/data_quality_check/SKILL.md` | Skill 专属 Prompt 模板 (集成在 SKILL.md body 中) | 开发完成 |

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

---
