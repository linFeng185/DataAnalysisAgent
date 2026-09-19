"""通知跳转契约：后端写的 link 必须是前端真实存在的路由，未知路径不能白屏。

事故（2026-09-14）：报表计划通知的 link 写的是 `/report-workbench/reports/{id}`，
而路由表里只有 `/report-workbench/jobs`。点击后 `navigate()` 匹配不到任何路由，
`ProtectedApp` 内部的 `<Routes>` 渲染 null，整个内容区一片空白（用户描述为"白屏"）。

两条契约：
1. 后端写入的站内 link 必须能被前端路由表覆盖——本文件用真实路由表交叉校验；
2. 路由表必须有兜底分支，任何未知路径都重定向而不是渲染空白。
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path("frontend/src/App.tsx")
PLAN_WORKER = Path("src/reporting/plan_worker.py")
LIST_PAGE = Path("frontend/src/pages/ReportListPage.tsx")
# 后端写站内通知链接的文件；新增写入点要一并登记，否则校验不到。
LINK_SOURCES = (PLAN_WORKER,)

_ROUTE_RE = re.compile(r'<Route\s+path="([^"]+)"')
# 后端 f-string 里的站内路径字面量，例如 f"/report-workbench/jobs?report={report_id}"。
_INTERNAL_LINK_RE = re.compile(r'f?"(/[A-Za-z0-9/_\-.{}$?=&]*)"')


# 方法作用：抽取 App.tsx 声明的全部路由模板。
# Args: 无。
# Returns: 路由模板列表，如 ["/report-workbench/jobs", "/reports/:reportId"]。
def _route_templates() -> list[str]:
    return _ROUTE_RE.findall(APP.read_text(encoding="utf-8"))


# 方法作用：把具体路径归一成可比较的段列表，去掉查询串。
# Args: path - 具体路径或路由模板。
# Returns: 段列表。
def _segments(path: str) -> list[str]:
    cleaned = path.split("?", 1)[0]
    return [segment for segment in cleaned.split("/") if segment]


# 方法作用：判断具体路径是否被路由表里的具体路由覆盖。
# Args: path - 具体路径（可含 f-string 占位符）；templates - 路由模板列表。
# Returns: 被某条具体路由覆盖时返回 True；通配兜底不算覆盖。
def _covered_by_routes(path: str, templates: list[str]) -> bool:
    target = _segments(path)
    for template in templates:
        parts = _segments(template)
        # 通配兜底只保证"不白屏"，不保证"到对地方"，因此不算命中——
        # 否则 /* 会让这条交叉校验永远通过，等于没测。
        if "*" in parts:
            continue
        if len(parts) != len(target):
            continue
        if all(part.startswith(":") or part == actual for part, actual in zip(parts, target)):
            return True
    return False


class TestNotificationNavigation:
    """覆盖通知跳转的路由一致性与兜底。"""

    # 方法作用：验证后端写的站内 link 都落在真实路由上。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_backend_notification_links_match_real_routes(self) -> None:
        """link 指向不存在的路由时，点击就是一片空白。"""
        # Arrange
        templates = _route_templates()
        assert templates, "未解析到任何路由，测试自身失效"

        # Act
        links: list[str] = []
        for source in LINK_SOURCES:
            links.extend(_INTERNAL_LINK_RE.findall(source.read_text(encoding="utf-8")))

        # Assert
        assert links, f"未在 {[source.name for source in LINK_SOURCES]} 中找到站内 link"
        for link in links:
            assert _covered_by_routes(link, templates), (
                f"后端写的 link {link} 在前端路由表里不存在，点击会白屏；"
                f"现有路由：{templates}"
            )

    # 方法作用：验证路由表存在兜底分支。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_path_has_fallback_route(self) -> None:
        """没有兜底分支时，任何失效链接都会渲染空白而不是给出落点。"""
        # Arrange
        templates = _route_templates()

        # Act / Assert
        assert "*" in templates, "路由表缺少通配兜底，失效链接会渲染空白"

    # 方法作用：验证报表列表页支持按链接直达某条报表。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_list_page_opens_report_from_query(self) -> None:
        """通知把用户带到列表页还不够，必须直接打开那条报表的详情。"""
        # Arrange
        page = LIST_PAGE.read_text(encoding="utf-8")

        # Act / Assert
        assert "useSearchParams" in page
        assert "searchParams.get('report')" in page

    # 方法作用：验证消费 ?report= 的 effect 只依赖引用稳定的函数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_report_query_effect_keeps_stable_dependencies(self) -> None:
        """依赖里混入每渲染重建的函数时，effect 会在低优先级导航被吸收前反复重跑。

        事故（2026-09-16）：openDetail 曾是普通函数，消费 ?report= 的 effect 因此每次渲染
        都重跑一遍 setSearchParams + openDetail。React Router 的 setSearchParams 走
        startTransition 低优先级更新，被不断到来的高优先级 setState 反复打断、迟迟不提交，
        地址栏参数清不掉，effect 下一轮又读到旧值——实测点击通知后 1.5 秒内详情接口被打
        132 次，详情抽屉关掉又被立刻拉起（用户描述为"关不掉、侧栏一直刷新"）。
        """
        # Arrange
        page = LIST_PAGE.read_text(encoding="utf-8")

        # Act
        stable_definition = "const openDetail = useCallback(" in page
        consume_effect = "}, [searchParams, setSearchParams, openDetail]);" in page

        # Assert
        assert stable_definition, "openDetail 必须用 useCallback 固定引用，否则消费 ?report= 的 effect 会每渲染重跑"
        assert consume_effect, "消费 ?report= 的 effect 依赖数组发生变化，需重新确认其中每个依赖是否引用稳定"
