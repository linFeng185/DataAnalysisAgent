"""架构整改的静态契约回归测试。"""

from __future__ import annotations

import ast
import logging
from pathlib import Path


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


# 方法作用：解析 src 下的全部 Python 语法树。
# Args: 无。
# Returns: 文件路径和对应 AST 的列表。
def _source_trees() -> list[tuple[Path, ast.AST]]:
    """使用 AST 避免基于文本的误报。"""
    logger.debug("_source_trees 入口")
    result = [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in (ROOT / "src").rglob("*.py")
    ]
    logger.info("_source_trees 完成", extra={"count": len(result)})
    return result


class TestArchitectureRemediation:
    """覆盖异常可观测性、异步 Tool、路由和启动模块化。"""

    # 方法作用：验证所有异常处理分支都具备显式行为。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_exception_handler_contains_only_pass(self) -> None:
        """任何异常都不能再被无声吞掉。"""
        logger.debug("test_no_exception_handler_contains_only_pass 入口")
        offenders: list[str] = []
        for path, tree in _source_trees():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert offenders == []
        logger.info("test_no_exception_handler_contains_only_pass 完成")

    # 方法作用：验证遗留 LangChain Tool 不再创建嵌套事件循环。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_legacy_tools_do_not_call_asyncio_run(self) -> None:
        """异步应用中的 Tool 必须通过 `_arun()` 执行。"""
        logger.debug("test_legacy_tools_do_not_call_asyncio_run 入口")
        targets = {
            Path("src/tools/sql_generator.py"),
            Path("src/tools/schema_explorer.py"),
            Path("src/tools/db_executor.py"),
        }
        offenders: list[str] = []
        for relative_path in targets:
            tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "asyncio"
                    and node.func.attr == "run"
                ):
                    offenders.append(f"{relative_path}:{node.lineno}")
        assert offenders == []
        logger.info("test_legacy_tools_do_not_call_asyncio_run 完成")

    # 方法作用：验证 API 路由已按领域拆分为包。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_api_routes_are_split_by_domain(self) -> None:
        """路由入口应只负责组合各领域 APIRouter。"""
        logger.debug("test_api_routes_are_split_by_domain 入口")
        route_dir = ROOT / "src/api/routes"
        expected = {
            "__init__.py",
            "_helpers.py",
            "chat.py",
            "datasource.py",
            "schema.py",
            "session.py",
            "mcp.py",
            "knowledge.py",
            "skills.py",
            "management.py",
        }
        assert route_dir.is_dir()
        assert expected.issubset({path.name for path in route_dir.iterdir()})
        assert not (ROOT / "src/api/routes.py").exists()
        logger.info("test_api_routes_are_split_by_domain 完成")

    # 方法作用：验证生命周期和 MCP 节点位于各自职责模块。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_bootstrap_and_mcp_node_modules_exist(self) -> None:
        """入口与工作流文件不再承载大段初始化和节点实现。"""
        logger.debug("test_bootstrap_and_mcp_node_modules_exist 入口")
        assert (ROOT / "src/bootstrap.py").is_file()
        assert (ROOT / "src/graph/nodes/mcp_agent.py").is_file()
        assert (ROOT / "src/graph/node_registry.py").is_file()
        logger.info("test_bootstrap_and_mcp_node_modules_exist 完成")
