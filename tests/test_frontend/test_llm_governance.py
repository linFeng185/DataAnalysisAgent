"""LLM 目录动态表单、物理删除和对话推理控件前端契约测试。"""

from __future__ import annotations

from pathlib import Path


class TestLLMGovernanceFrontend:
    """覆盖功能 18.12.4-18.12.5 的平台与对话界面。"""

    # 方法作用：验证厂商能力字段由可视化列表维护且模型表单动态渲染。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_uses_dynamic_capability_forms_without_json_textarea(self) -> None:
        """超级管理员不应再手工编辑模型能力 JSON。"""
        # Arrange
        source = Path("frontend/src/pages/AdminPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "capability_schema" in source
        assert 'Form.List name="capability_fields"' in source
        assert "renderCapabilityField" in source
        assert "能力 JSON" not in source
        assert "JSON.parse(values.capabilities" not in source

    # 方法作用：验证厂商和模型列表提供带确认的物理删除操作。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_can_delete_provider_and_model_catalog_entries(self) -> None:
        """删除按钮必须调用真实 DELETE 端点而不是切换 is_active。"""
        # Arrange
        source = Path("frontend/src/pages/AdminPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "deleteProvider" in source
        assert "deleteModel" in source
        assert "`/admin/llm/providers/${provider.id}`" in source
        assert "`/admin/llm/models/${model.id}`" in source
        assert "删除模型厂商" in source
        assert "删除模型" in source

    # 方法作用：验证对话页按模型能力启用推理开关和深度选项。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_page_exposes_capability_aware_reasoning_controls(self) -> None:
        """不支持推理的模型必须禁用开关，深度选项来自模型 capabilities。"""
        # Arrange
        page = Path("frontend/src/pages/ChatPage.tsx").read_text(encoding="utf-8")
        hook = Path("frontend/src/hooks/useChat.ts").read_text(encoding="utf-8")
        client = Path("frontend/src/api/client.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "reasoningEnabled" in page
        assert "reasoningEffort" in page
        assert "reasoning_efforts" in page
        assert "当前模型不支持推理" in page
        assert "reasoningEnabled" in hook
        assert "reasoningEffort" in hook
        assert "reasoning_enabled: reasoningEnabled" in client
        assert "reasoning_effort: reasoningEffort" in client

    # 方法作用：验证移动端输入区和推理工具栏采用可换行的响应式布局。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_reasoning_controls_have_mobile_layout_contract(self) -> None:
        """390px 宽度下输入框、模型、推理开关和深度选择不得相互挤压。"""
        # Arrange
        page = Path("frontend/src/pages/ChatPage.tsx").read_text(encoding="utf-8")
        styles = Path("frontend/src/index.css").read_text(encoding="utf-8")

        # Act / Assert
        assert 'className="chat-composer"' in page
        assert 'className="chat-query-input"' in page
        assert 'className="chat-toolbar"' in page
        assert 'className="chat-toolbar-controls"' in page
        assert "wrap" in page
        assert ".chat-composer" in styles
        assert "grid-template-columns: minmax(0, 1fr) auto" in styles
        assert ".chat-toolbar-controls" in styles
        assert "flex-wrap: wrap" in styles

    # 方法作用：验证移动端厂商与模型表保持可读列宽并允许容器内横向滚动。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_llm_catalog_tables_have_mobile_scroll_contract(self) -> None:
        """厂商编码和协议不得在窄屏表格中被压缩成逐字竖排。"""
        # Arrange
        source = Path("frontend/src/pages/AdminPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert source.count("scroll={{ x: 990 }}") >= 2
        assert "{ title: '编码', dataIndex: 'code', width: 130 }" in source
        assert "{ title: '协议', dataIndex: 'protocol', width: 180 }" in source
        assert "{ title: '模型 ID', dataIndex: 'model_id', width: 180 }" in source

    # 方法作用：验证命名连接表单与默认模型表单使用独立的 FormInstance。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tenant_connection_and_default_forms_are_isolated(self) -> None:
        """新增连接时不得触发默认连接和默认模型的必填校验。"""
        # Arrange
        source = Path("frontend/src/pages/AdminPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "const [defaultConnectionForm] = Form.useForm();" in source
        assert source.count("form={connectionForm}") == 1
        assert source.count("form={defaultConnectionForm}") == 1
        assert "defaultConnectionForm.validateFields" in source
