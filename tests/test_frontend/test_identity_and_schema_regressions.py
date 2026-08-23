"""前端身份切换、数据源编辑和 Schema 默认选择回归契约。"""

from pathlib import Path


class TestIdentityAndSchemaRegressions:
    """覆盖用户切换、数据源回显和不存在 demo 数据源的缺陷。"""

    # 方法作用：验证聊天仅保留在内存且身份变化会清空状态，避免浏览器持久化敏感对话。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_storage_is_memory_only_and_resets_by_identity(self) -> None:
        """不同用户不能复用内存会话，也不能从浏览器存储恢复敏感对话。"""
        # Arrange
        source = Path("frontend/src/hooks/useChat.ts").read_text(encoding="utf-8")
        storage_source = Path("frontend/src/hooks/chatStorage.ts").read_text(encoding="utf-8")

        # Act
        has_identity_key = "tenant_id" in source and "user_id" in source
        resets_on_identity_change = (
            "setTurns([]);" in source
            and "setSessionId('');" in source
            and "}, [identityKey]);" in source
        )

        # Assert
        assert has_identity_key
        assert resets_on_identity_change
        assert "clearChatStorage" in source
        assert "sessionStorage" not in source
        assert "loadChatStorage" not in source
        assert "saveChatStorage" not in source
        assert "sessionStorage.removeItem" in storage_source
        assert "sessionStorage.getItem" not in storage_source
        assert "sessionStorage.setItem" not in storage_source

    # 方法作用：验证退出登录会清理浏览器中的会话缓存，避免下一账号看到旧内容。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_logout_clears_chat_storage(self) -> None:
        """登出动作必须清除聊天缓存和正在进行的页面状态。"""
        # Arrange
        source = Path("frontend/src/hooks/AuthContext.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "clearChatStorage" in source

    # 方法作用：验证 Schema 页面无数据源时不请求 demo，并在加载后自动选择可访问源。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_schema_does_not_use_demo_default(self) -> None:
        """Schema 页面和 API 不得把不存在的 demo 当作默认数据源。"""
        # Arrange
        page = Path("frontend/src/pages/SchemaPage.tsx").read_text(encoding="utf-8")
        route = Path("src/api/routes/schema.py").read_text(encoding="utf-8")

        # Act / Assert
        assert "useState('demo')" not in page
        assert 'Query(default="demo")' not in route

    # 方法作用：验证数据源编辑表单在弹窗挂载后再回填完整配置字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_datasource_edit_rehydrates_after_modal_mount(self) -> None:
        """编辑时应通过挂载后的 effect 回填，而不是在 destroyOnClose 前写入。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "useEffect" in source
        assert "!open || !editing" in source
        assert "preserve={false}" not in source
