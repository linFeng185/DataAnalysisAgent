#!/usr/bin/env python3
"""快速端到端验证脚本 — 不依赖外部数据库，仅使用内存SQLite验证核心流程。

使用方式:
    pytest tests/test_e2e_quick.py -v
    python tests/test_e2e_quick.py  # 直接运行，输出验证结果

验证内容:
    1. 关键模块可导入（17个模块）
    2. 配置文件加载（MCP + 数据源 + Settings）
    3. DDL生成正确（5种方言 × 4张核心表）
    4. 小规模数据生成（100行验证）
    5. 知识库测试文件存在且格式正确
    6. MCP测试文件存在
    7. LangGraph工作流路由函数可调用
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def green(s: str) -> str:
    return f"\033[92m{s}\033[0m"


def red(s: str) -> str:
    return f"\033[91m{s}\033[0m"


def check(name: str, result: bool, detail: str = "") -> bool:
    """输出检查结果并返回是否通过。"""
    status = green("PASS") if result else red("FAIL")
    msg = f"  [{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return result


def _check_01_imports() -> bool:
    """验证关键模块可导入。"""
    print("\n1. 模块导入检查")
    modules = [
        ("配置模块", "src.config", "get_settings"),
        ("日志模块", "src.logging_config", "get_logger"),
        ("LLM Provider", "src.llm.provider", "LLMProvider"),
        ("LLM Client", "src.llm.client", "get_llm"),
        ("LLM Model Registry", "src.llm.model_registry", "ModelRegistry"),
        ("VectorStore", "src.memory.vector_store", "get_vector_store"),
        ("API路由", "src.api.routes", "router"),
        ("Auth模块", "src.api.auth", "AuthMiddleware"),
        ("Schema管理", "src.knowledge.schema_manager", "get_schema_manager"),
        ("Checkpointer", "src.memory.checkpointer", "get_checkpointer"),
        ("CredentialManager", "src.datasource.credential_manager", "CredentialManager"),
        ("Workflow", "src.graph.workflow", "build_workflow"),
        ("DataProcessor", "src.tools.data_processor", "DataProcessor"),
        ("SessionStore", "src.memory.session_store", "get_session_store"),
        ("HistoryStore", "src.memory.history_store", "get_history_store"),
        ("MCP Client", "src.mcp_client.client_manager", "get_mcp_client_manager"),
        ("SkillManager", "src.skill_manager", "get_skill_manager"),
    ]
    all_ok = True
    for name, module, attr in modules:
        try:
            mod = __import__(module, fromlist=[attr])
            getattr(mod, attr)
            all_ok &= check(name, True)
        except Exception as e:
            all_ok &= check(name, False, str(e))
    return all_ok


def _check_02_config_loading() -> bool:
    """验证配置文件加载。"""
    print("\n2. 配置文件加载检查")
    all_ok = True

    try:
        import yaml
        mcp_config = yaml.safe_load(open("config/mcp_servers.yaml", encoding="utf-8"))
        servers = mcp_config.get("mcp_servers", {})
        active_k = [k for k, v in servers.items() if isinstance(v, dict)]
        all_ok &= check("MCP配置加载", len(active_k) > 0, f"活跃: {active_k}")
    except Exception as e:
        all_ok &= check("MCP配置加载", False, str(e))

    try:
        import yaml
        ds_config = yaml.safe_load(open("config/datasources.yaml", encoding="utf-8"))
        datasources = ds_config.get("datasources", {})
        all_ok &= check("数据源配置加载", len(datasources) >= 5,
                        f"共{len(datasources)}个")
    except Exception as e:
        all_ok &= check("数据源配置加载", False, str(e))

    try:
        from src.config import get_settings
        s = get_settings()
        all_ok &= check("Settings加载", s.env in ("dev", "prod"),
                        f"env={s.env}, llm={s.llm_provider}")
    except Exception as e:
        all_ok &= check("Settings加载", False, str(e))

    return all_ok


def _check_03_ddl_generation() -> bool:
    """验证各方言DDL生成正确。"""
    print("\n3. DDL生成检查")
    all_ok = True

    from tests.import_test_data import get_tables, build_ddl

    schemas = get_tables(1000)
    dialects = ["clickhouse", "mysql", "postgres", "oracle", "mssql"]

    for table_name in ["customers", "orders", "products", "order_items"]:
        for dialect in dialects:
            try:
                ddl = build_ddl(table_name, schemas[table_name]["cols"], dialect)
                has_create = "CREATE TABLE" in ddl.upper()
                has_pk = "PRIMARY KEY" in ddl.upper() or (dialect == "clickhouse" and "ORDER BY" in ddl.upper())
                all_ok &= check(f"DDL {dialect}/{table_name}",
                                has_create and has_pk, f"{len(ddl)} bytes")
            except Exception as e:
                all_ok &= check(f"DDL {dialect}/{table_name}", False, str(e))

    return all_ok


def _check_04_small_data_generation() -> bool:
    """验证小规模数据生成（用categories表，行数少）。"""
    print("\n4. 小规模数据生成检查")
    all_ok = True

    from tests.import_test_data import get_tables
    from tests.import_test_data import generate_table_file as gen_table

    schemas = get_tables(100)
    output_dir = Path("data/generated")

    # 用categories表验证（行数少），并且期望行数与schema一致
    for dialect in ["mysql", "postgres"]:
        try:
            expected = schemas["categories"]["row_count"]
            count = gen_table("categories", schemas["categories"], dialect, output_dir, "csv")
            all_ok &= check(f"生成 {dialect}/categories",
                            count == expected, f"期望{expected}行, 实际{count}行")
        except Exception as e:
            all_ok &= check(f"生成 {dialect}/categories", False, str(e))

    # 验证输出文件存在且内容正确
    for dialect in ["mysql", "postgres"]:
        found = False
        for ext in [".csv.gz", ".sql.gz"]:
            fpath = output_dir / dialect / f"categories{ext}"
            if fpath.exists():
                import gzip
                with gzip.open(fpath, "rt", encoding="utf-8") as f:
                    content = f.read()
                found = True
                all_ok &= check(f"文件 {dialect}", True, f"{len(content)} bytes")
                break
        if not found:
            all_ok &= check(f"文件 {dialect}", False, "无输出文件")

    return all_ok


def _check_05_knowledge_files() -> bool:
    """验证知识库测试文件存在且非空。"""
    print("\n5. 知识库文件检查")
    all_ok = True

    kb_dir = Path("tests/fixtures/knowledge")
    files = ["business_rules.txt", "product_catalog.md",
             "sales_policy.txt", "data_dictionary.csv"]

    for fname in files:
        fpath = kb_dir / fname
        if fpath.exists():
            size = fpath.stat().st_size
            all_ok &= check(f"文件: {fname}", True, f"{size} bytes")
        else:
            all_ok &= check(f"文件: {fname}", False, "不存在")

    # 验证数据字典CSV格式
    dd_path = kb_dir / "data_dictionary.csv"
    if dd_path.exists():
        import csv
        with open(dd_path, encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)
        all_ok &= check("数据字典格式",
                        "表名" in header and "数据源" in header,
                        f"{len(rows)}个字段定义")

    return all_ok


def _check_06_mcp_test_files() -> bool:
    """验证MCP测试文件存在。"""
    print("\n6. MCP测试文件检查")
    all_ok = True

    mcp_dir = Path("tests/fixtures/mcp")
    for fname in ["sample_sales.csv", "sample_inventory.csv"]:
        fpath = mcp_dir / fname
        if fpath.exists():
            all_ok &= check(f"MCP文件: {fname}", True, f"{fpath.stat().st_size} bytes")
        else:
            all_ok &= check(f"MCP文件: {fname}", False, "不存在")

    return all_ok


def _check_07_workflow_structure() -> bool:
    """验证LangGraph工作流路由函数。"""
    print("\n7. 工作流结构检查")
    all_ok = True

    from src.graph.workflow import route_by_intent, after_layer3
    from src.graph.state import AnalysisState

    # 构造最小state
    state: AnalysisState = {
        "user_query": "各品类销售额排名",
        "selected_datasources": [],
        "intent": "",
        "validation_errors": [],
        "explain_errors": [],
        "execution_error": "",
        "retry_count": 0,
    }

    # 路由函数可调用
    try:
        r = route_by_intent(state)
        valid = r in ("retrieve_schema", "mcp_agent",
                       "llm_direct_answer", "multi_source_dispatch")
        all_ok &= check("route_by_intent可调用", valid, f"返回: {r}")
    except Exception as e:
        all_ok &= check("route_by_intent", False, str(e))

    # 安全阻断路由
    try:
        sec_state = {**state, "validation_errors": [{"type": "security_block"}]}
        r = after_layer3(sec_state)
        all_ok &= check("安全阻断→build_response", r == "build_response")
    except Exception as e:
        all_ok &= check("安全阻断路由", False, str(e))

    # 语法错误重试
    try:
        syn_state = {**state, "validation_errors": [{"type": "syntax_error"}]}
        r = after_layer3(syn_state)
        all_ok &= check("语法错误→generate_sql", r == "generate_sql")
    except Exception as e:
        all_ok &= check("语法错误路由", False, str(e))

    return all_ok


def test_01_imports() -> None:
    """pytest 入口：关键模块均应可导入。"""
    assert _check_01_imports()


def test_02_config_loading() -> None:
    """pytest 入口：项目配置均应可加载。"""
    assert _check_02_config_loading()


def test_03_ddl_generation() -> None:
    """pytest 入口：多方言 DDL 应生成成功。"""
    assert _check_03_ddl_generation()


def test_04_small_data_generation() -> None:
    """pytest 入口：小规模测试数据应生成成功。"""
    assert _check_04_small_data_generation()


def test_05_knowledge_files() -> None:
    """pytest 入口：知识库夹具应存在且格式正确。"""
    assert _check_05_knowledge_files()


def test_06_mcp_test_files() -> None:
    """pytest 入口：MCP 测试文件应存在。"""
    assert _check_06_mcp_test_files()


def test_07_workflow_structure() -> None:
    """pytest 入口：工作流路由函数应符合契约。"""
    assert _check_07_workflow_structure()


def main():
    """运行全部快速验证。"""
    print("=" * 60)
    print("Data Analysis Agent — 快速端到端验证")
    print("=" * 60)

    results = {
        "01_imports": _check_01_imports(),
        "02_config": _check_02_config_loading(),
        "03_ddl": _check_03_ddl_generation(),
        "04_data_gen": _check_04_small_data_generation(),
        "05_knowledge": _check_05_knowledge_files(),
        "06_mcp": _check_06_mcp_test_files(),
        "07_workflow": _check_07_workflow_structure(),
    }

    print(f"\n{'=' * 60}")
    print("验证结果汇总")
    print(f"{'=' * 60}")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        status = green("PASS") if ok else red("FAIL")
        print(f"  [{status}] {name}")

    print(f"\n通过: {passed}/{total}")
    if passed == total:
        print(green("全部通过! 系统基本链路正常。"))
        print("\n下一步操作:")
        print("  1. 导入测试数据:")
        print("     python tests/import_test_data.py --db all --scale 1000000")
        print("  2. 生成DDL:")
        print("     python tests/import_test_data.py --db all --ddl-only")
        print("  3. 启动服务:")
        print("     python -m src.main")
        print("  4. 运行API测试:")
        print("     pytest tests/test_api/ -v")
    else:
        print(red(f"有 {total - passed} 项未通过，请检查FAIL项。"))

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
