"""自定义异常体系。"""

from __future__ import annotations


class DataAnalysisAgentError(Exception):
    """所有自定义异常的基类。"""


class DataSourceNotFoundError(DataAnalysisAgentError):
    """数据源未找到。"""

    def __init__(self, datasource: str) -> None:
        self.datasource = datasource
        super().__init__(f"数据源 '{datasource}' 未找到")


class SQLValidationError(DataAnalysisAgentError):
    """SQL 校验失败。"""

    def __init__(self, errors: list[dict], warnings: list[dict] | None = None) -> None:
        self.errors = errors
        self.warnings = warnings or []
        super().__init__(f"SQL 校验失败: {len(errors)} 个错误")


class SQLSecurityError(DataAnalysisAgentError):
    """SQL 安全拦截异常。"""

    def __init__(self, reason: str, violated_operation: str) -> None:
        self.reason = reason
        self.violated_operation = violated_operation
        super().__init__(f"SQL 安全拦截: {reason} (操作: {violated_operation})")


class ExecutionError(DataAnalysisAgentError):
    """SQL 执行失败。"""

    def __init__(self, message: str, retry_count: int = 0, sql: str | None = None) -> None:
        self.message = message
        self.retry_count = retry_count
        self.sql = sql
        super().__init__(message)


class RateLimitError(DataAnalysisAgentError):
    """请求频率超限。"""

    def __init__(self, user_id: str, limit: int) -> None:
        self.user_id = user_id
        self.limit = limit
        super().__init__(f"用户 '{user_id}' 每小时查询次数已达上限 ({limit})")


class KnowledgeNotFoundError(DataAnalysisAgentError):
    """知识库未找到相关知识。"""

    def __init__(self, query: str) -> None:
        self.query = query
        super().__init__(f"未找到与 '{query}' 相关的知识条目")


class MCPConnectionError(DataAnalysisAgentError):
    """MCP Server 连接失败。"""

    def __init__(self, server_name: str, detail: str = "") -> None:
        self.server_name = server_name
        self.detail = detail
        super().__init__(f"MCP Server '{server_name}' 连接失败: {detail}")
