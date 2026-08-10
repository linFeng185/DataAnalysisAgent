#!/usr/bin/env bash
set -Eeuo pipefail

# 一键初始化项目、校验 Docker Compose 配置并启动服务。
# Args: 命令行参数 --repo/--ref/--env-file/--skip-health/--app-port。
# Returns: 启动成功返回 0，依赖、配置或健康检查失败返回非 0。

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPO_URL=""
REPO_REF="main"
ENV_FILE="${PROJECT_DIR}/.env"
SKIP_HEALTH="false"
APP_PORT="8080"
FRONTEND_PORT="8080"
ENV_CREATED="false"

# 方法作用：输出失败原因并终止部署，避免半配置状态被误认为成功。
# Args: message - 面向部署操作者的错误信息。
# Returns: 不返回。
fail() {
  printf '%s\n' "[ERROR] $1" >&2
  exit 1
}

# 方法作用：解析有限且显式的部署参数。
# Args: $@ - install.sh 命令行参数。
# Returns: 无，更新脚本级参数。
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) REPO_URL="${2:-}"; shift 2 ;;
      --ref) REPO_REF="${2:-}"; shift 2 ;;
      --env-file) ENV_FILE="${2:-}"; shift 2 ;;
      --app-port) APP_PORT="${2:-}"; shift 2 ;;
      --skip-health) SKIP_HEALTH="true"; shift ;;
      -h|--help)
        printf '%s\n' '用法: scripts/install.sh [--repo URL] [--ref REF] [--env-file PATH] [--app-port PORT] [--skip-health]'
        exit 0
        ;;
      *) fail "不支持的参数: $1" ;;
    esac
  done
  [[ "${REPO_REF}" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "--ref 包含非法字符"
  [[ "${APP_PORT}" =~ ^[0-9]+$ ]] && (( APP_PORT >= 1 && APP_PORT <= 65535 )) || fail "--app-port 必须是 1-65535"
  FRONTEND_PORT="${APP_PORT}"
}

# 方法作用：检查 Docker、Compose 和必要的辅助命令是否存在。
# Args: 无。
# Returns: 依赖齐全时返回 0，否则终止。
check_prerequisites() {
  command -v docker >/dev/null 2>&1 || fail "未找到 docker，请先安装 Docker Engine"
  docker compose version >/dev/null 2>&1 || fail "未找到 docker compose 插件"
  command -v curl >/dev/null 2>&1 || fail "未找到 curl，无法执行健康检查"
  command -v openssl >/dev/null 2>&1 || fail "未找到 openssl，无法生成随机密钥"
}

# 方法作用：在指定仓库为空目录时按显式版本克隆项目。
# Args: 无，使用 REPO_URL、REPO_REF、PROJECT_DIR。
# Returns: 无。
prepare_repository() {
  [[ -z "${REPO_URL}" ]] && return 0
  [[ -d "${PROJECT_DIR}/.git" ]] && return 0
  [[ -e "${PROJECT_DIR}" && -n "$(find "${PROJECT_DIR}" -mindepth 1 -maxdepth 1 -print -quit)" ]] && fail "目标目录非空，拒绝覆盖"
  git clone --branch "${REPO_REF}" --depth 1 -- "${REPO_URL}" "${PROJECT_DIR}" || fail "仓库克隆失败"
}

# 方法作用：创建被 .gitignore 忽略的本地 Compose 与只读配置副本。
# Args: 无，使用 PROJECT_DIR 下的 example 文件。
# Returns: 无。
initialize_runtime_files() {
  [[ -f "${PROJECT_DIR}/docker-compose.yml" ]] || cp -- "${PROJECT_DIR}/docker-compose.example.yml" "${PROJECT_DIR}/docker-compose.yml"
  [[ -f "${PROJECT_DIR}/config/app.yaml" ]] || cp -- "${PROJECT_DIR}/config/app.example.yaml" "${PROJECT_DIR}/config/app.yaml"
  [[ -f "${PROJECT_DIR}/config/datasources.yaml" ]] || cp -- "${PROJECT_DIR}/config/datasources.example.yaml" "${PROJECT_DIR}/config/datasources.yaml"
}

# 方法作用：设置新建环境文件中的键值，同时避免覆盖用户已有环境。
# Args: key/value - 环境变量名和值；overwrite - 是否替换新建文件中的示例值。
# Returns: 无。
set_env_value() {
  local key="$1"
  local value="$2"
  local overwrite="${3:-false}"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    [[ "${overwrite}" == "true" ]] || return 0
    sed -i.bak -E "s|^${key}=.*$|${key}=${value}|" "${ENV_FILE}"
    rm -f -- "${ENV_FILE}.bak"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

# 方法作用：幂等初始化环境文件并只补充缺失的随机密钥。
# Args: 无，使用 ENV_FILE 和 .env.example。
# Returns: 无。
initialize_env() {
  mkdir -p "$(dirname -- "${ENV_FILE}")"
  if [[ ! -f "${ENV_FILE}" ]]; then
    [[ -f "${PROJECT_DIR}/.env.example" ]] || fail "缺少 .env.example"
    cp -- "${PROJECT_DIR}/.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}" 2>/dev/null || true
    ENV_CREATED="true"
  fi
  local pg_password redis_password admin_password
  pg_password="$(openssl rand -hex 24)"
  redis_password="$(openssl rand -hex 24)"
  admin_password="$(openssl rand -hex 16)"
  if [[ "${ENV_CREATED}" == "true" ]]; then
    set_env_value ENV prod true
    set_env_value POSTGRES_DB data_agent true
    set_env_value POSTGRES_USER data_agent true
    set_env_value POSTGRES_PASSWORD "${pg_password}" true
    set_env_value DATABASE_URL "postgresql+asyncpg://data_agent:${pg_password}@postgres:5432/data_agent" true
    set_env_value REDIS_PASSWORD "${redis_password}" true
    set_env_value REDIS_URL "redis://:${redis_password}@redis:6379/0" true
    set_env_value SUPER_ADMIN_PASSWORD "${admin_password}" true
    set_env_value FRONTEND_PORT "${FRONTEND_PORT}" true
    set_env_value APP_PORT "${APP_PORT}" true
    set_env_value JWT_SECRET "$(openssl rand -hex 32)" true
    set_env_value CREDENTIAL_ENCRYPTION_KEY "$(openssl rand -hex 32)" true
    printf '%s\n' "[INFO] 已生成初始超级管理员密码，请从 ${ENV_FILE} 读取并立即轮换。"
  else
    set_env_value APP_PORT "${APP_PORT}"
    set_env_value FRONTEND_PORT "${FRONTEND_PORT}"
    set_env_value JWT_SECRET "$(openssl rand -hex 32)"
    set_env_value CREDENTIAL_ENCRYPTION_KEY "$(openssl rand -hex 32)"
  fi
}

# 方法作用：校验 Compose 展开后的配置，不启动未通过校验的服务。
# Args: 无。
# Returns: 配置合法时返回 0，否则终止。
validate_compose() {
  docker compose --env-file "${ENV_FILE}" -f "${PROJECT_DIR}/docker-compose.yml" config >/dev/null || fail "docker compose config 校验失败"
}

# 方法作用：启动前后端容器并可选轮询健康端点。
# Args: 无。
# Returns: 服务启动并健康时返回 0，否则终止。
start_services() {
  docker compose --env-file "${ENV_FILE}" -f "${PROJECT_DIR}/docker-compose.yml" up -d --build || fail "容器启动失败"
  [[ "${SKIP_HEALTH}" == "true" ]] && return 0
  local health_url="http://127.0.0.1:${FRONTEND_PORT}/api/v1/health"
  local attempt
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error --max-time 5 "${health_url}" >/dev/null 2>&1; then
      printf '%s\n' "[OK] 服务已就绪: ${health_url}"
      return 0
    fi
    sleep 2
  done
  docker compose --env-file "${ENV_FILE}" -f "${PROJECT_DIR}/docker-compose.yml" ps
  fail "健康检查超时: ${health_url}"
}

# 方法作用：执行一键部署主流程并输出可复核结果。
# Args: $@ - 命令行参数。
# Returns: 成功返回 0。
main() {
  parse_args "$@"
  check_prerequisites
  prepare_repository
  initialize_runtime_files
  initialize_env
  validate_compose
  start_services
  printf '%s\n' '[OK] 部署完成，已有 .env 未被覆盖。'
}

main "$@"
