# 社区 Skill Git 子模块发现与固定校验

日期：2026-10-14。对应功能点：9.4.2 Git 子模块支持（features/09-skills.md）。上层设计见 spec/03-core-components.md §3.10.4 的「Git 子模块」渠道。

## 1. 目标与范围

- 只做**发现**：识别运维已经用 `git submodule` 显式安装、且由父仓库 gitlink 固定 commit 的本地社区 Skill，把它们作为 system 作用域 Skill 加载进 SkillManager。
- 不做**获取**：运行期不执行 `clone` / `fetch` / `pull` / `submodule update/init` / 任何安装动作，不联网、不写父仓库、不改 `.gitmodules`、不改子模块工作树。仓库内不新增真实子模块。
- 默认关闭：`skill_community_submodules_enabled=false` 时完全不读 `.gitmodules`、不调用 `git`，既有行为逐字节不变。
- 社区技能来源不可信：它们位于 `skills/` 之下，但**不因此成为内置技能**，必须走与受管目录相同的摘要与签名边界。
- 失败只影响本身：任一子模块或 git 调用失败，只跳过该来源，不中断内置、extra、受管目录与其它子模块的发现（对应「刷新失败」不扩散）。

## 2. 信任边界与威胁模型

| 主体 | 信任级别 | 说明 |
|------|----------|------|
| 父仓库 gitlink（`.gitmodules` + index/HEAD 中的 160000 条目） | 受信（运维维护） | 唯一允许的版本来源；commit 是唯一权威 |
| 子模块远程仓库 URL | 需显式白名单 | 只用于比对，运行期不访问 |
| 子模块工作树内容（SKILL.md/tools.py） | 不可信 | 必须通过摘要 + 签名 + 干净度校验 |
| 子模块内 Manifest 自述字段 | 不可信 | 不得决定作用域、租户、所有者或资源标识 |

防御的越权手法：未初始化子模块、HEAD 漂移、脏工作树、未跟踪可执行文件、远程 URL 被替换、`.gitmodules` 声明越权路径、symlink/junction 逃逸、伪装内置、同名覆盖内置、非 HTTPS 来源。

## 3. 配置

`src/config.py`（`Settings` 字段，可被环境变量 / `config/app.yaml` 覆盖）：

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `skill_community_submodules_enabled` | `false` | 是否启用社区子模块发现；关闭时零副作用 |
| `skill_community_allowed_repo_urls` | `""` | 允许的**完整仓库 URL** 白名单，逗号分隔，仅接受 HTTPS |

`CommunitySubmoduleConfig` 另有 `git_timeout_seconds`（默认 15 秒），只由构造参数注入，不进入 Settings，避免扩大配置面；测试用它注入短超时。

白名单语义（`src/skill_submodules.py::parse_allowed_repo_urls` / `validate_repository_url`）：

1. 逐条去除首尾空白与末尾 `/` 后**完整字符串比对**，不做域名通配、不做前缀匹配、不把带不带 `.git` 视为等价。
2. 每条白名单项自身必须合法，否则在启用状态下构造即失败（fail closed，启动期暴露配置错误）。
3. 合法 URL 必须：scheme 恰好为 `https`；含主机名；**无 userinfo**（`user:pass@`）、**无 query**、**无 fragment**。
4. 拒绝：`http`、`file`、`ssh`、`git`、`ext::`、scp 风格（`git@host:org/repo`，无 scheme）、Windows 盘符（`C:/x` 被解析为 scheme `c`）、相对路径。

日志只记录 scheme 与拒绝原因，不记录 URL 全文。

## 4. 发现与校验算法

### 4.1 定位父仓库与声明

| 步骤 | 规则 | 失败处理 |
|------|------|----------|
| 1 | 社区根 = `builtin_dir/community`；不存在则返回空列表 | 不调用 git |
| 2 | 社区根是 symlink/junction | 整体拒绝，error 日志 |
| 3 | 从社区根向上查找含 `.git` 的目录，再以 `git rev-parse --show-toplevel` 确认 | 不是 Git 仓库 → 空列表 |
| 4 | 读取父仓库根 `.gitmodules`（INI 解析，不调用 `git config`） | 缺失/无 `[submodule]` → 空列表 |
| 5 | 只取 path 恰好等于 `<社区根相对路径>/<一层目录名>` 且 url 通过白名单的子模块 | 其余声明直接忽略 |

`path` 必须是单层：相对路径、POSIX 分隔、无 `..`、无绝对路径、无 `\`；层级多于一层或含逃逸片段即拒绝。

### 4.2 单个子模块的固定校验（按序执行，任一步失败即拒绝该子模块）

| # | 校验 | 判定依据 | 拒绝语义 |
|---|------|----------|----------|
| 1 | 目录存在且不是 symlink/junction | `is_dir()` 且非 `is_symlink()`/`is_junction()` | 路径逃逸 |
| 2 | 真实路径仍在社区根内 | `resolve()` 后包含关系 | 路径逃逸 |
| 3 | 已初始化 | 存在 `.git`（文件或目录），且 `.git` 不是 symlink/junction | 未初始化 |
| 4 | gitdir 归属 | `.git` 为文件时，`gitdir:` 目标解析后必须位于父仓库 git 目录的 `modules/` 下 | 路径逃逸 |
| 5 | gitlink 存在 | `git ls-tree HEAD -- <relpath>` 输出恰好一行、模式 `160000`、类型 `commit` | 未固定 |
| 6 | 版本一致 | `git rev-parse HEAD` 与 gitlink commit **完全相同** | 版本漂移 |
| 7 | 远程未被替换 | 子模块 `remote.origin.url` 归一化后等于 `.gitmodules` 声明 URL | 远程 URL 替换 |
| 8 | 工作树干净 | 确认无 Git clean/smudge/process 过滤器配置后，`git status --porcelain --untracked-files=no` 输出为空（含暂存、修改、删除、重命名） | 脏代码或不安全过滤器 |
| 9 | 无未跟踪文件 | 用不排除 ignored 的文件清单检查全部工作树文件，除仓库自身 `.git` 元数据外均须被父 gitlink 所指提交跟踪；拒绝所有未跟踪或 ignored 文件及链接 | 未固定内容 |

工作树不允许依赖 `.gitignore` 隐藏额外代码，尤其不得忽略 `.pyc/.pyo/.pth/.pyd` 或脚本。社区技能隔离执行必须禁写字节码（沿用工作进程环境或 `-B`），防止自身运行产生未固定文件；已有缓存需由安装者清理后再启用。固定检查失败只禁用该社区来源。

### 4.3 子模块内技能清单定位

- 允许根 `SKILL.md`，或**恰好一层**子目录下的 `SKILL.md`（`sub/*/SKILL.md`）：存在根清单时以根清单为准，忽略一层子目录，禁止无界递归（不使用 `rglob`/`**`）。
- 清单或其父目录为 symlink/junction，或解析后逃逸子模块根：拒绝该子模块。
- 单个子模块可承载多个技能（一层子目录形态）。

## 5. 加载与信任身份

- 作用域**固定 `system`**，`tenant_id=0`、`owner_user_id=0`：Manifest 中任何 `scope`/租户/所有者自述一律忽略，`resource_id` 只由 `_resource_key(name, "system", 0, 0)` 生成，请求级可见性与启停沿用既有机制。
- **不因路径在内置目录下而成为内置**：`builtin_dir/community` 及其子路径从内置判定中排除，因此：
  - `verify_skill_package` 的 `require_signature` 对社区技能生效（`skill_require_signatures=true` 时未签名即拒绝）；
  - `get_active_tools` 中 `trusted_builtin=False`，工具走 `SkillRuntimeTool` 的隔离执行路径；
  - `SkillManager.is_builtin()` 对社区技能返回 `False`（列表接口如实标注），删除接口因社区技能不在受管目录下而拒绝落盘删除。
- 摘要（`compute_skill_package_digest`）对所有社区技能**始终**计算；签名信封校验沿用 `verify_skill_package`，不新增第二套校验器。
- **同名不覆盖**：加载前若缓存中已存在同名的 **system 作用域**技能（内置、extra、上传到 system、或更早载入的社区技能），跳过该社区技能并告警。tenant/private 同名不影响（各自资源标识不同，按既有 private > tenant > system 优先级隔离）。
- 启停状态按 `resource_id` 持久化，刷新与重启后恢复，行为与其它来源一致。
- 依赖缺失（`depends_on.python_packages`）沿用既有逻辑：加载后置 `enabled=False`。

## 6. Git 调用加固

只用只读子命令：`rev-parse --show-toplevel`、`ls-tree HEAD -- <path>`、`rev-parse HEAD`、`config --get remote.origin.url`、过滤器配置查询、`status --porcelain --untracked-files=no`、不排除 ignored 的文件清单查询。父仓库 `.gitmodules` 的实际内容必须与 HEAD 中的版本一致，不能用未提交 URL 或路径替换固定配置。

- 参数数组调用，`shell=False`，绝不拼接字符串、绝不用 `shell=True`。
- 每次调用带**有限超时**（`git_timeout_seconds`，默认 15 秒）；超时按失败拒绝该来源。
- 受控环境：复制必要进程环境并移除全部以 `GIT_` 开头的继承项，特别包括 `GIT_CONFIG_COUNT`、`GIT_CONFIG_PARAMETERS` 和配置注入键，再注入
  `GIT_TERMINAL_PROMPT=0`、`GIT_ASKPASS=`、`SSH_ASKPASS=`、`GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL=<devnull>`、`GIT_OPTIONAL_LOCKS=0`、`GIT_PAGER=cat`。
- 固定 `-c` 覆盖：`core.hooksPath=<devnull>`（不存在即无 hook）、`core.fsmonitor=false`、`core.untrackedCache=false`、`core.attributesFile=<devnull>`、`core.pager=cat`、`credential.helper=`（清空助手链）。
- 在任何会读取工作树并计算干净度的命令前，先只读检查有效 Git 配置；存在非空 `filter.*.clean`、`filter.*.smudge` 或 `filter.*.process` 即拒绝来源，绝不执行过滤器。只读不自动等于安全；测试必须放入写标记文件的恶意过滤器并证明扫描未执行。
- 父 `.gitmodules` 与子模块工作树均不得依赖 `git status` 单独证明固定：`assume-unchanged` 和 `skip-worktree` 会隐藏实际改动。首期拒绝相关索引标记，或使用不受标记影响且不会执行过滤器的内容校验；必须有真实 Git 回归覆盖这两类绕过。
- `git` 不可用（`FileNotFoundError`）或超时：整体跳过社区来源并告警，不抛出到调用方。

## 7. 异步与失败隔离

- 文件与 git 校验全部经 `asyncio.to_thread` 执行，不阻塞事件循环；`SkillManager.discover()` 因此不引入同步阻塞点。
- 每个子模块的校验独立捕获异常：拒绝原因写入 warning（含子模块名与原因，不含 URL 与文件内容），其它子模块与其它目录来源继续。
- 扫描级异常（父仓库不可用、git 缺失）记为一次 warning/error，社区来源整体跳过。
- 日志级别：拒绝走 `warning`，扫描完成只记一条汇总 INFO；不打印仓库 URL、凭证、文件正文。

## 8. 接口

```python
# src/skill_submodules.py
@dataclass(frozen=True)
class CommunitySubmoduleConfig:
    enabled: bool = False
    allowed_repo_urls: tuple[str, ...] = ()
    git_timeout_seconds: float = 15.0

    @classmethod
    def from_settings(cls, enabled: bool, allowed_repo_urls: str | Sequence[str], *, git_timeout_seconds: float = 15.0) -> "CommunitySubmoduleConfig": ...

@dataclass(frozen=True)
class CommunitySubmodule:
    name: str                     # 子模块一层目录名
    root: Path                    # 绝对路径
    revision: str                 # gitlink 固定的 commit
    manifest_paths: tuple[Path, ...]

class CommunitySubmoduleScanner:
    def __init__(self, community_root: str | Path, config: CommunitySubmoduleConfig) -> None: ...
    async def scan(self) -> list[CommunitySubmodule]: ...   # 永不抛出

def validate_repository_url(value: str) -> str: ...
def parse_allowed_repo_urls(raw: str | Sequence[str] | None) -> tuple[str, ...]: ...
```

`SkillManager` 构造新增关键字参数 `community_config: CommunitySubmoduleConfig | None = None`（缺省即关闭）；`SkillManager.discover()` 在受管 system 目录扫描之后、租户目录扫描之前追加社区来源。

## 9. 测试映射（tests/test_skill_submodules.py）

| 用例 | 覆盖点 |
|------|--------|
| `test_community_discovery_disabled_by_default` | 默认关闭：不读 `.gitmodules`、不调用 git、结果与关闭前一致 |
| `test_community_submodule_skill_is_discovered_and_pinned` | 正确固定：scope=system、commit 与 gitlink 一致、来源路径正确 |
| `test_allowed_url_must_match_whitelist_exactly` | 白名单拒绝：未列出的 HTTPS、大小写/`.git` 差异均拒绝 |
| `test_repository_url_shape_rules` | 恶意 URL：file/ssh/ext/scp/http/userinfo/query/fragment/相对路径 |
| `test_gitlink_missing_or_head_drift_is_rejected` | gitlink 缺失、HEAD 漂移 |
| `test_dirty_worktree_is_rejected` | 脏代码（已跟踪文件被修改） |
| `test_untracked_executable_is_rejected` | 未跟踪可执行文件 |
| `test_path_escape_and_symlink_are_rejected` | 路径逃逸、`.gitmodules` 越权路径、symlink |
| `test_uninitialized_submodule_is_rejected` | 未初始化 |
| `test_remote_url_replacement_is_rejected` | 远程 URL 替换 |
| `test_unsigned_community_skill_fails_signature_gate` | 签名失败（社区不享受内置豁免） |
| `test_community_skill_never_counts_as_builtin` | 伪装内置：`is_builtin=False`、内置豁免不外溢 |
| `test_community_skill_does_not_override_builtin_same_name` | 重名不覆盖 |
| `test_failure_of_one_submodule_does_not_affect_others` | 刷新失败隔离（含内置与受管来源不受影响） |
| `test_git_unavailable_or_timeout_keeps_other_sources` | Git 不可用/超时 |
| `test_git_call_hardening_arguments` | 参数数组、有限超时、受控环境、`shell=False` |
| `test_discovery_does_not_block_event_loop` | `asyncio.to_thread` 非阻塞 |

测试全部使用 `tmp_path` 下真实本地 Git 仓库 fixture（`git init` + 本地 `submodule add` + 改写 `.gitmodules`/`origin` 为白名单 HTTPS URL），不联网、不依赖远程模型与数据库；环境不支持创建符号链接时该用例 `skip`。

## 10. 残余风险与限制

- 校验与加载之间存在 TOCTOU 窗口：本地运维场景下不额外做加载后二次校验，接受该窗口（与 Registry 安装路径一致的取舍）。
- 白名单是精确字符串匹配，运维必须在 `.gitmodules` 与配置中写完全一致的 URL；写错即拒绝（fail closed），不自动纠正。
- 子模块内容仍会在 `SkillRuntimeTool` 中执行，本设计只保证「固定版本 + 已验证来源 + 已签名」，不提供代码级沙箱；隔离与资源限制见 9.1.12。
- 首期拒绝含二级子模块（嵌套 gitlink）的社区来源：外层提交固定 gitlink 并不能证明内层实际工作树与固定版本一致，不能绕过逐层校验加载。
- 未跟踪及 ignored 内容一律拒绝，不依赖扩展名、POSIX 可执行位或 Windows 文件类型；普通文本与字节码同样可能影响技能行为。
- 根清单和一层子目录清单均必须支持签名。包摘要排除技能根的 `.git` 元数据文件或目录，使签名在普通仓库与子模块安装之间保持稳定；不能排除实际技能文件，也不能借此放宽固定校验、签发者验证或代码篡改检查。
