"""工作流节点注册加载边界测试。"""

from __future__ import annotations

import subprocess
import sys


class TestNodeRegistryLoading:
    """覆盖工作流冷启动不得提前加载 LLM 重依赖。"""

    # 方法作用：验证节点目录加载不会导入 LLM 客户端、Transformers 或 Torch。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_default_nodes_keep_llm_dependencies_lazy(self) -> None:
        """工作流装配只注册 handler，模型依赖应在节点真正执行时加载。"""
        # Arrange
        script = """
import contextlib
import io
import sys
with contextlib.redirect_stdout(io.StringIO()):
    from src.graph.node_registry import get_node_definitions
    get_node_definitions()
loaded = [name for name in ('src.llm.client', 'transformers', 'torch') if name in sys.modules]
print('LOADED=' + ','.join(loaded))
"""

        # Act
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )

        # Assert
        assert result.stdout.strip().splitlines()[-1] == "LOADED="
