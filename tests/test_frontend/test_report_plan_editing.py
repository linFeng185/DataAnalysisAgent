"""计划单元的可编辑性契约：任何来源的单元都必须由用户决定去留。

事故背景（2026-09-14）：预览页与报表详情页**各写了一份**单元列表渲染，两份都只给
diverged 分组加了勾选框，user 分组是只读的。第一次修复只改了预览页，用户从报表列表
的详情抽屉里看仍然是只读，现象就是"改了没生效"。此后勾选框只允许在共享组件
`ReportPlanUnitRow` 里定义一次，新增页面一律复用。

另一条约定：unit_id 是程序内部标识（英文），不得当标题展示，否则用户会以为
"生成的单元标题全是英文"——展示一律以 question 为主。
"""

from __future__ import annotations

import re
from pathlib import Path

_SHARED_ROW = Path("frontend/src/components/report/ReportPlanUnitRow.tsx")
_PAGES = (
    Path("frontend/src/pages/ReportWorkbenchPage.tsx"),
    Path("frontend/src/pages/ReportListPage.tsx"),
)


# 方法作用：读取文件源码。
# Args: path - 文件路径。
# Returns: 源码文本。
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestPlanUnitEditing:
    """覆盖计划单元的勾选入口与空选集保护。"""

    # 方法作用：验证勾选框只在共享组件里定义一次。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_checkbox_is_defined_once_in_shared_row(self) -> None:
        """页面若各自写勾选框，就会重演"改了一处漏一处"。"""
        # Arrange
        shared = _read(_SHARED_ROW)

        # Act / Assert
        assert "<Checkbox" in shared
        assert "excludedUnitIds.includes(unit.unit_id)" in shared
        for page in _PAGES:
            source = _read(page)
            # 页面可以按标识过滤（提交时算 accepted 列表），但不得自己算勾选态
            assert "excludedUnitIds.includes(unit.unit_id)" not in source, (
                f"{page.name} 又自己实现了一份勾选逻辑"
            )

    # 方法作用：验证两个页面都复用共享单元行。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_both_pages_use_shared_row_for_every_group(self) -> None:
        """预览页与详情页的 user / diverged 两组都必须走共享行。"""
        # Arrange / Act / Assert
        for page in _PAGES:
            source = _read(page)
            assert "import ReportPlanUnitRow from" in source, f"{page.name} 未引入共享单元行"
            # 每个页面两个分组各一处
            assert source.count("<ReportPlanUnitRow") == 2, f"{page.name} 的分组数量与共享行调用数不一致"

    # 方法作用：验证两个分组仍按单元来源正确分组。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_groups_still_split_by_origin(self) -> None:
        """复用渲染器不能把两组的数据源筛选也一起合并掉。"""
        # Arrange
        source = _read(_PAGES[0])

        # Act / Assert
        assert "unit.origin ?? 'user') === 'user'" in source
        assert "unit.origin === 'diverged'" in source

    # 方法作用：验证展示以 question 为主而非 unit_id。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_question_is_the_visible_title(self) -> None:
        """unit_id 是英文内部标识，当标题展示会让用户以为"标题生成错了语种"。"""
        # Arrange
        shared = _read(_SHARED_ROW)

        # Act / Assert
        assert "<Typography.Text strong>{unit.question}</Typography.Text>" in shared
        assert "strong>{unit.unit_id}" not in shared

    # 方法作用：验证全部取消勾选时禁止提交。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_empty_selection_disables_confirm(self) -> None:
        """所有单元都可取消后，空选集成为可能；服务端会 422 拒绝空数组。"""
        # Arrange / Act / Assert
        for page in _PAGES:
            source = _read(page)
            assert re.search(r"acceptedUnitCount\s*===\s*0", source), f"{page.name} 缺少空选集保护"
            # 禁用必须给出原因：一个没有解释的灰按钮同样是死路
            assert "至少保留一个分析单元" in source
