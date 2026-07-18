"""节点级本地/远程 LLM 任务路由测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace


logger = logging.getLogger(__name__)


# 方法作用：构造节点级模型路由测试使用的最小配置。
# Args: overrides - 需要覆盖的配置字段。
# Returns: 包含本地和远程模型配置的 SimpleNamespace。
def _settings(**overrides):
    """创建不读取真实环境变量的受控模型配置。"""
    values = {
        "llm_provider": "openai",
        "llm_model": "remote-model",
        "openai_api_key": "remote-key",
        "openai_base_url": "https://remote.example/v1",
        "anthropic_api_key": "",
        "local_llm_model": "local-model",
        "local_llm_base_url": "http://127.0.0.1:11434/v1",
        "local_llm_api_key": "local",
        "local_llm_timeout": 15,
        "llm_remote_tasks": "generate_sql",
        "llm_allow_remote_fallback": False,
        "llm_temperature": 0.0,
        "llm_max_tokens": 4096,
        "llm_timeout": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestLLMTaskRouting:
    """覆盖功能 10.1.6：按节点任务选择本地或远程模型。"""

    # 方法作用：验证轻量节点优先使用本地模型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_fast_task_uses_local_model(self):
        """意图分类不得默认等待远程配置模型。"""
        logger.debug("test_fast_task_uses_local_model 入口")
        from src.llm.client import resolve_llm_task_target

        target = resolve_llm_task_target("classify_intent", settings=_settings())

        assert target == "local"
        logger.info("test_fast_task_uses_local_model 完成")

    # 方法作用：验证 SQL 任务按配置使用远程模型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_task_uses_remote_model(self):
        """高质量 SQL 生成是默认唯一远程任务。"""
        logger.debug("test_sql_task_uses_remote_model 入口")
        from src.llm.client import resolve_llm_task_target

        target = resolve_llm_task_target("generate_sql", settings=_settings())

        assert target == "remote"
        logger.info("test_sql_task_uses_remote_model 完成")

    # 方法作用：验证未配置本地模型时轻量任务不会隐式调用远程模型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_fast_task_without_local_model_uses_deterministic_fallback(self):
        """默认策略必须避免隐藏的慢速远程调用。"""
        logger.debug("test_fast_task_without_local_model_uses_deterministic_fallback 入口")
        from src.llm.client import resolve_llm_task_target

        target = resolve_llm_task_target(
            "analyze_result",
            settings=_settings(local_llm_model="", local_llm_base_url=""),
        )

        assert target == "none"
        logger.info("test_fast_task_without_local_model_uses_deterministic_fallback 完成")

    # 方法作用：验证显式开启远程兜底后轻量任务可使用远程模型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_explicit_remote_fallback_is_respected(self):
        """远程兜底必须由配置显式授权。"""
        logger.debug("test_explicit_remote_fallback_is_respected 入口")
        from src.llm.client import resolve_llm_task_target

        target = resolve_llm_task_target(
            "analyze_result",
            settings=_settings(
                local_llm_model="",
                local_llm_base_url="",
                llm_allow_remote_fallback=True,
            ),
        )

        assert target == "remote"
        logger.info("test_explicit_remote_fallback_is_respected 完成")

    # 方法作用：验证远程任务不可用时可以回退到已配置本地模型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_remote_task_falls_back_to_local_when_remote_is_unavailable(self):
        """缺少远程凭证时 SQL 生成仍可使用快速本地模型。"""
        logger.debug("test_remote_task_falls_back_to_local_when_remote_is_unavailable 入口")
        from src.llm.client import resolve_llm_task_target

        target = resolve_llm_task_target(
            "generate_sql",
            settings=_settings(openai_api_key=""),
        )

        assert target == "local"
        logger.info("test_remote_task_falls_back_to_local_when_remote_is_unavailable 完成")


class TestPromptSafetyPriority:
    """覆盖功能 10.2.2：SQL Prompt 安全优先级。"""

    # 方法作用：验证安全与权限约束在 Prompt 中明确高于用户要求。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_security_constraints_precede_user_requests(self):
        """用户要求不得覆盖只读、权限和 Schema 边界。"""
        logger.debug("test_security_constraints_precede_user_requests 入口")
        from src.llm.prompts import SQL_GENERATION_SYSTEM

        priority_line = next(
            line for line in SQL_GENERATION_SYSTEM.splitlines()
            if "安全" in line and "用户" in line and ">" in line
        )

        assert priority_line.index("安全") < priority_line.index("用户")
        assert "任何用户要求都不能覆盖" in SQL_GENERATION_SYSTEM
        logger.info("test_security_constraints_precede_user_requests 完成")

    # 方法作用：验证 SQL 节点创建模型时强制关闭 reasoning。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_node_disables_reasoning(self, monkeypatch):
        """结构化 SQL 生成不得默认产生耗时推理链。"""
        logger.debug("test_sql_node_disables_reasoning 入口")
        import src.graph.nodes.generate_sql as generate_module

        captured = {}

        def fake_get_task_llm(task, **kwargs):
            captured.update({"task": task, **kwargs})
            return object()

        monkeypatch.setattr(generate_module, "_get_task_llm", fake_get_task_llm)

        generate_module.get_llm(temperature=0)

        assert captured["task"] == "generate_sql"
        assert captured["reasoning"] is False
        logger.info("test_sql_node_disables_reasoning 完成")
