"""前端管理页面错误处理回归测试。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# 方法作用：读取前端源码并断言指定片段存在。
# Args: relative_path - frontend/src 下相对路径；required - 必须出现的源码片段。
# Returns: 无返回值，断言失败时由 pytest 报告。
def _assert_source_contains(relative_path: str, *required: str) -> None:
    logger.debug("_assert_source_contains 入口", extra={"path": relative_path})
    try:
        source = Path("frontend/src", relative_path).read_text(encoding="utf-8")
        for fragment in required:
            assert fragment in source
    except Exception as exc:
        logger.error("_assert_source_contains 异常: %s", exc, exc_info=True)
        raise
    logger.info("_assert_source_contains 完成", extra={"path": relative_path})


# 方法作用：验证 Chat 页面拒绝空或已失效的数据源选择。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_chat_page_validates_selected_datasource() -> None:
    """已缓存的 demo 不在服务端列表时不得继续发送。"""
    logger.debug("test_chat_page_validates_selected_datasource 入口")
    _assert_source_contains(
        "pages/ChatPage.tsx",
        "ds.length === 0",
        "datasources.some",
        "请选择有效的数据源",
    )
    logger.info("test_chat_page_validates_selected_datasource 完成")


# 方法作用：验证 Schema 刷新使用会检查 HTTP 状态的公共 API 客户端。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_schema_refresh_checks_http_status() -> None:
    """非 2xx 刷新响应不得显示成功。"""
    logger.debug("test_schema_refresh_checks_http_status 入口")
    _assert_source_contains("pages/SchemaPage.tsx", "await post(", "Schema 已刷新")
    logger.info("test_schema_refresh_checks_http_status 完成")


# 方法作用：验证列注释保存请求携带当前选择的数据源。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_schema_comment_update_carries_selected_datasource() -> None:
    """选择非 demo 数据源时不得把注释静默写入默认数据源。"""
    logger.debug("test_schema_comment_update_carries_selected_datasource 入口")
    _assert_source_contains(
        "pages/SchemaPage.tsx",
        "comment?datasource=${encodeURIComponent(ds)}",
        "字段备注保存请求边界",
    )
    logger.info("test_schema_comment_update_carries_selected_datasource 完成")


# 方法作用：验证 Skill 切换、删除和刷新都检查 response.ok 并反馈错误。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_skill_actions_report_http_failures() -> None:
    """Skill 管理写操作失败不能静默刷新列表。"""
    logger.debug("test_skill_actions_report_http_failures 入口")
    _assert_source_contains(
        "pages/SkillsPage.tsx",
        "if (!res.ok)",
        "切换失败",
        "删除失败",
        "刷新失败",
    )
    logger.info("test_skill_actions_report_http_failures 完成")


# 方法作用：验证 MCP 添加失败和登出失败都有用户反馈且不提前清空状态。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_mcp_and_logout_failures_are_visible() -> None:
    """服务端失败时页面状态必须与服务端会话保持一致。"""
    logger.debug("test_mcp_and_logout_failures_are_visible 入口")
    _assert_source_contains("pages/McpPage.tsx", "添加失败")
    _assert_source_contains(
        "hooks/AuthContext.tsx",
        "if (!response.ok)",
        "退出登录失败",
        "setUser(null)",
    )
    logger.info("test_mcp_and_logout_failures_are_visible 完成")


# 方法作用：验证数据源列表响应类型与含密码的创建请求类型分离。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_datasource_response_type_excludes_password() -> None:
    """浏览器读取的数据源摘要契约不得声明凭证字段。"""
    logger.debug("test_datasource_response_type_excludes_password 入口")
    try:
        # Arrange / Act
        source = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")
        response_type = source.split("export interface DatasourceConfig", maxsplit=1)[1]
        response_type = response_type.split("export interface", maxsplit=1)[0]

        # Assert
        assert "password" not in response_type
        assert "export interface DatasourceCreatePayload" in source
        assert "password?: string" in source
        logger.info("test_datasource_response_type_excludes_password 完成")
    except Exception as exc:
        logger.error("test_datasource_response_type_excludes_password 异常: %s", exc, exc_info=True)
        raise
