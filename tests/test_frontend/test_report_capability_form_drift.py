"""报表能力清单防漂移契约测试。

事实依据：管理端保存用户额度是「整体替换」语义——`PUT /report-workbench/admin/users/{id}/policy`
会用表单提交的能力列表**覆盖**该用户在 `report_user_permissions` 里的授权。因此表单清单里
只要缺一个后端已定义的能力键，管理员保存一次就会把这个权限从用户身上抹掉。

2026-09-13 实际故障：子页键 `report.create_page` / `report.list` / `report.link_manage`
加入后端 `_ROLE_CAPABILITIES` 后未同步到管理表单，导致任何被保存过额度的用户点开
「报表工作台」父菜单时三个子项全被前端过滤掉（父菜单点开是空的），且退出重登、重启进程
都不会恢复，只能靠迁移 `035_report_capability_page_keys.sql` 回填。

本测试只读源码做静态断言，不读写任何数据文件。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND_QUOTA = Path("src/reporting/quota.py").read_text(encoding="utf-8")
ADMIN_FORM = Path("frontend/src/components/report/ReportPolicyEditor.tsx").read_text(encoding="utf-8")


# 方法作用：从后端 _ROLE_CAPABILITIES 里取出全部 report.* 能力键。
# Args: 无。
# Returns: 能力键集合。
def backend_capability_keys() -> set[str]:
    """用 AST 解析字面量，避免正则误吞注释里的键名。

    注意后端写的是带类型注解的赋值（`_ROLE_CAPABILITIES: dict[...] = {...}`），
    对应 ast.AnnAssign 而不是 ast.Assign，两者都要处理。
    """
    tree = ast.parse(BACKEND_QUOTA)
    keys: set[str] = set()
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign):
            names = [item for item in node.targets if isinstance(item, ast.Name)]
            target = names[0].id if names else None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target != "_ROLE_CAPABILITIES" or node.value is None:
            continue
        for value in ast.walk(node.value):
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.startswith("report."):
                keys.add(value.value)
    return keys


# 方法作用：从管理表单的能力常量里取出全部 report.* 键。
# Args: 无。
# Returns: 能力键集合。
def admin_form_keys() -> set[str]:
    """表单是 [[键, 标签], ...] 结构，直接匹配中括号里的首个字符串字面量。"""
    return set(re.findall(r"\['(report\.[a-z_]+)'", ADMIN_FORM))


class TestReportCapabilityFormDrift:
    """验证管理表单的能力清单不会落后于后端的角色默认能力。"""

    # 方法作用：验证后端能力键可被解析（防止解析失败导致后续断言空转）。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_backend_keys_are_discoverable(self) -> None:
        """解析结果必须非空，否则本文件其余断言没有意义。"""
        # Act / Assert
        keys = backend_capability_keys()
        assert len(keys) >= 10, f"后端能力键解析异常：{sorted(keys)}"
        assert "report.menu" in keys

    # 方法作用：验证三个子页键都已被后端定义。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_backend_defines_page_keys(self) -> None:
        """父子菜单拆分后的三个子页键是本次故障的主角。"""
        # Act / Assert
        keys = backend_capability_keys()
        assert {"report.create_page", "report.list", "report.link_manage"} <= keys

    # 方法作用：验证管理表单覆盖后端的每一个能力键。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_form_covers_every_backend_key(self) -> None:
        """表单缺键 = 保存一次就抹掉该权限，因此必须是超集。"""
        # Arrange
        missing = backend_capability_keys() - admin_form_keys()
        # Act / Assert
        assert missing == set(), (
            "管理表单缺少这些能力键，保存额度会把它们从用户身上抹掉：" + ", ".join(sorted(missing))
        )

    # 方法作用：验证表单没有引用后端未定义的能力键。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_form_has_no_unknown_key(self) -> None:
        """表单多出的键会被后端原样存库，但永远不会被任何校验读取，属静默失效。"""
        # Arrange
        unknown = admin_form_keys() - backend_capability_keys()
        # Act & Assert
        assert unknown == set(), f"管理表单引用了后端未定义的键：{sorted(unknown)}"
