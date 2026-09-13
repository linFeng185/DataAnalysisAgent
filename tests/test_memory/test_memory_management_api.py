"""长期记忆管理 API 测试，覆盖 spec/44 §5.2。

事实依据：pytest 经 tests/conftest.py 与 tests/test_memory/__init__.py 收集；
tests/test_memory/ 既有 12 个文件覆盖 long_term_store / session_archive 等主题，
均无记忆管理 API 主题；本文件不读写数据文件，只做序列化纯函数与源码契约断言。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.api.routes.memory import _serialize_memory  # noqa: E402


class TestMemorySerialization:
    """验证记忆响应的字段契约与过期时间推导。"""

    # 方法作用：验证 payload 为字符串时被解析为对象。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_payload_string_is_parsed(self) -> None:
        """驱动返回 JSONB 为字符串，响应必须是对象。"""
        # Arrange
        row = {
            "id": "m1", "memory_type": "sql_template", "scope": "private:3:3",
            "content": "问题: 统计各月销售额", "payload": '{"question": "统计各月销售额"}',
            "access_count": 2, "confidence": 0.5, "ttl_days": 180,
        }

        # Act
        result = _serialize_memory(row)

        # Assert
        assert result["payload"] == {"question": "统计各月销售额"}
        assert result["memory_type"] == "sql_template"

    # 方法作用：验证 payload 为对象时原样保留。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_payload_dict_is_kept(self) -> None:
        """驱动返回 dict 时不得二次解析。"""
        # Arrange
        row = {"id": "m2", "payload": {"sql": "SELECT 1"}, "ttl_days": None,
               "memory_type": "user_preference", "scope": "private:3:3",
               "content": "", "access_count": 0, "confidence": 1.0}

        # Act
        result = _serialize_memory(row)

        # Assert
        assert result["payload"]["sql"] == "SELECT 1"
        assert result["expires_at"] is None

    # 方法作用：验证损坏的 payload 不导致接口 500。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_broken_payload_degrades_to_empty(self) -> None:
        """历史脏数据必须降级为空对象。"""
        # Arrange
        row = {"id": "m3", "payload": "{not-json", "ttl_days": None,
               "memory_type": "sql_template", "scope": "private:3:3",
               "content": "", "access_count": 0, "confidence": 0.5}

        # Act
        result = _serialize_memory(row)

        # Assert
        assert result["payload"] == {}

    # 方法作用：验证缺失字段时响应结构完整不抛异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_fields_survive(self) -> None:
        """空行数据不得让详情接口报错。"""
        # Act
        result = _serialize_memory({})

        # Assert
        assert result["id"] == ""
        assert result["content"] == ""
        assert result["access_count"] == 0
        assert result["confidence"] == 0.0

    # 方法作用：验证过期时间由创建时间与 TTL 推导。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_expires_at_derived_from_ttl(self) -> None:
        """前端需要展示有效期而非让用户心算。"""
        # Arrange
        from datetime import datetime, timezone

        row = {"id": "m4", "payload": {}, "ttl_days": 180,
               "created_at": datetime(2026, 9, 10, 17, 51, 24, tzinfo=timezone.utc),
               "memory_type": "sql_template", "scope": "private:3:3",
               "content": "", "access_count": 0, "confidence": 0.5}

        # Act
        result = _serialize_memory(row)

        # Assert
        assert result["expires_at"] is not None
        assert result["expires_at"].startswith("2027-03-09")


class TestMemoryRouteContract:
    """验证端点与归属强制的静态契约。"""

    # 方法作用：验证管理端点不接受所有者参数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_owner_override_parameter(self) -> None:
        """归属只能来自登录身份，禁止请求参数覆盖。"""
        # Arrange
        source = Path("src/api/routes/memory.py").read_text(encoding="utf-8")

        # Act / Assert
        assert "owner_user_id: int" not in source
        assert "tenant_id: int = Query" not in source

    # 方法作用：验证越权访问返回 404 而不是 403。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unauthorized_returns_404(self) -> None:
        """避免探测他人记忆的存在性。"""
        # Arrange
        source = Path("src/api/routes/memory.py").read_text(encoding="utf-8")

        # Act / Assert
        assert 'HTTPException(404, "记忆不存在")' in source

    # 方法作用：验证三个端点均已注册。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_three_endpoints_registered(self) -> None:
        """列表、详情、删除缺一不可。"""
        # Arrange
        source = Path("src/api/routes/memory.py").read_text(encoding="utf-8")

        # Act / Assert
        assert '@router.get("/memory")' in source
        assert '@router.get("/memory/{memory_id}")' in source
        assert '@router.delete("/memory/{memory_id}")' in source


class TestMemoryStoreContract:
    """验证存储层归属强制与双后端一致性。"""

    # 方法作用：验证列表查询恒定绑定当前用户与 private 范围。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_store_query_binds_owner(self) -> None:
        """SQL 条件必须写死租户与属主，不留参数化入口。"""
        # Arrange
        source = Path("src/memory/long_term_store.py").read_text(encoding="utf-8")
        section = source.split("async def list_memories")[1].split("async def ")[0]

        # Assert
        assert "tenant_id = $1" in section
        assert "owner_user_id = $2" in section
        assert "visibility = 'private'" in section

    # 方法作用：验证删除会同步清理向量库。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_delete_syncs_vector_store(self) -> None:
        """PG 删除后必须走补偿队列保证向量一致。"""
        # Arrange
        source = Path("src/memory/long_term_store.py").read_text(encoding="utf-8")
        section = source.split("async def delete_memory")[1].split("async def ")[0]

        # Assert
        assert "_delete_vector_or_queue" in section
