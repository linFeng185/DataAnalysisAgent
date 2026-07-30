"""Linux 前后端分离容器部署契约测试。"""

from pathlib import Path

import yaml


class TestContainerDeployment:
    """覆盖功能 17.4.1、17.4.2、17.4.3。"""

    # 方法作用：验证后端镜像使用多阶段构建、非 root 用户和容器健康检查。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_backend_dockerfile_is_production_ready(self) -> None:
        """后端镜像应固定 Python 版本并以单进程 Uvicorn 安全运行。"""
        # Arrange
        source = Path("Dockerfile").read_text(encoding="utf-8")

        # Act
        normalized = source.lower()

        # Assert
        assert "from python:3.14.0-slim as builder" in normalized
        assert "from python:3.14.0-slim as runtime" in normalized
        assert normalized.count('arg install_extras="connectors,documents,structured"') == 2
        assert '"data-analysis-agent[${install_extras}]"' in normalized
        assert "user app" in normalized
        assert "healthcheck" in normalized
        assert '"src.main:app"' in source
        assert "--reload" not in source

    # 方法作用：验证前端镜像独立构建并通过 Nginx 代理 API 和 SSE。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_frontend_dockerfile_and_nginx_proxy_are_production_ready(self) -> None:
        """前端应产出静态镜像，并保持流式响应不被 Nginx 缓冲。"""
        # Arrange
        dockerfile = Path("frontend/Dockerfile").read_text(encoding="utf-8")
        nginx = Path("frontend/nginx.conf").read_text(encoding="utf-8")

        # Act / Assert
        assert "FROM node:22-alpine AS builder" in dockerfile
        assert "FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime" in dockerfile
        assert "npm ci" in dockerfile
        assert "COPY public ./public" not in dockerfile
        assert "location /api/" in nginx
        assert "proxy_pass http://backend;" in nginx
        assert "proxy_pass http://backend:8000" not in nginx
        assert "proxy_buffering off" in nginx
        assert "try_files $uri $uri/ /index.html" in nginx

    # 方法作用：验证 Compose 将前后端分离，并持久化配置、日志和状态数据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_compose_separates_services_and_mounts_runtime_state(self) -> None:
        """生产编排应包含前端、后端、PostgreSQL、Redis 及明确健康依赖。"""
        # Arrange
        compose_path = Path("docker-compose.example.yml")
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

        # Act
        services = compose["services"]
        backend = services["backend"]
        frontend = services["frontend"]

        # Assert
        assert {"frontend", "backend", "postgres", "redis"} <= set(services)
        assert frontend["depends_on"]["backend"]["condition"] == "service_healthy"
        assert backend["depends_on"]["postgres"]["condition"] == "service_healthy"
        assert backend["depends_on"]["redis"]["condition"] == "service_healthy"
        assert "8000" in [str(port) for port in backend.get("expose", [])]
        assert not backend.get("ports")

        backend_volumes = "\n".join(str(item) for item in backend["volumes"])
        assert "./config/app.yaml:/app/config/app.yaml:ro" in backend_volumes
        assert "./config/datasources.yaml:/app/config/datasources.yaml:ro" in backend_volumes
        assert "./data/backend:/app/data" in backend_volumes
        assert "./logs/backend:/app/logs" in backend_volumes
        assert "./data/postgres:/var/lib/postgresql/data" in str(
            services["postgres"]["volumes"]
        )
        assert "./data/redis:/data" in str(services["redis"]["volumes"])

    # 方法作用：验证生产 Compose 不保存明文凭证且只发布前端端口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_compose_keeps_secrets_external_and_backend_private(self) -> None:
        """密钥只能来自部署环境，数据库、缓存和后端不得直接暴露公网端口。"""
        # Arrange
        source = Path("docker-compose.example.yml").read_text(encoding="utf-8")
        compose = yaml.safe_load(source)

        # Act
        services = compose["services"]
        secret_values = [
            str(value)
            for service in services.values()
            for key, value in service.get("environment", {}).items()
            if any(marker in key for marker in ("PASSWORD", "SECRET", "DATABASE_URL", "REDIS_URL"))
        ]

        # Assert
        assert secret_values
        assert all(value.startswith("${") for value in secret_values)
        assert "env_file" in services["backend"]
        assert services["frontend"]["ports"] == [
            "${FRONTEND_BIND_ADDRESS:-127.0.0.1}:${FRONTEND_PORT:-8080}:8080"
        ]
        for service_name in ("backend", "postgres", "redis"):
            assert not services[service_name].get("ports")

    # 方法作用：验证 Docker 构建上下文排除虚拟环境、缓存、测试和本地数据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_dockerignore_excludes_local_and_sensitive_files(self) -> None:
        """构建上下文不得携带 Git、IDE、密钥、运行数据和本地依赖。"""
        # Arrange
        backend_rules = Path(".dockerignore").read_text(encoding="utf-8")
        frontend_rules = Path("frontend/.dockerignore").read_text(encoding="utf-8")

        # Act / Assert
        for rule in (".git", ".venv", ".env", "tests", "logs", "data"):
            assert rule in backend_rules
        for rule in ("node_modules", "dist", ".env"):
            assert rule in frontend_rules

        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        for rule in (
            "/docker-compose.yml",
            "/config/app.yaml",
            "/config/datasources.yaml",
        ):
            assert rule in gitignore

    # 方法作用：验证挂载的生产配置能通过应用自身的安全启动门禁。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 环境变量补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mounted_backend_config_passes_production_validation(self, monkeypatch) -> None:
        """部署 YAML 与必要密钥注入后应生成可启动的生产 Settings。"""
        # Arrange
        monkeypatch.setenv("APP_CONFIG_PATH", "config/app.example.yaml")
        monkeypatch.setenv("ENV", "prod")
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+asyncpg://data_agent:strong-secret@postgres:5432/data_agent",
        )
        monkeypatch.setenv("JWT_SECRET", "j" * 32)
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "c" * 32)
        monkeypatch.setenv("CHROMA_PERSIST_DIR", "/app/data/chroma")
        from src.config import Settings, validate_production_settings

        # Act
        settings = Settings(_env_file=None)
        result = validate_production_settings(settings)

        # Assert
        assert result is None
        assert settings.env == "prod"
        assert settings.chroma_persist_dir == "/app/data/chroma"
        assert settings.api_access.trusted_proxy_cidrs == ["172.30.0.0/24"]
