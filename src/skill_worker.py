"""隔离 Skill 子进程入口，仅通过标准输入输出交换 JSON。"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import importlib.util
import io
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any


def _is_within(path: Path, roots: list[Path]) -> bool:
    """判断文件路径是否位于只读授权根目录。"""
    return any(path == root or root in path.parents for root in roots)


def _install_resource_limits(limits: dict[str, Any]) -> None:
    """在支持 resource 的部署系统限制 CPU、地址空间和文件大小。"""
    try:
        import resource
    except ImportError:
        return
    cpu_seconds = int(limits.get("cpu_seconds", 10))
    memory_bytes = int(limits.get("memory_mb", 256)) * 1024 * 1024
    output_bytes = int(limits.get("max_output_bytes", 512 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))


def _install_audit_policy(request: dict[str, Any]) -> None:
    """通过审计钩子阻断未授权文件、网络和子进程访问。"""
    skill_root = Path(request["skill_root"]).resolve()
    project_src_root = Path(request["project_src_root"]).resolve()
    allowed_roots = [skill_root, project_src_root, Path(sys.prefix).resolve()]
    allowed_roots.extend(Path(path).resolve() for path in request.get("allowed_asset_paths", []))
    network_hosts = {
        str(host).strip().lower()
        for host in (request.get("permissions", {}).get("network", []) or [])
        if str(host).strip()
    }
    original_open = builtins.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if isinstance(file, (str, bytes, os.PathLike)):
            path = Path(file).resolve()
            if any(flag in mode for flag in "wax+") or not _is_within(path, allowed_roots):
                raise PermissionError("Skill 文件访问未授权")
        return original_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(args[0]).resolve()
            mode = str(args[1] or "r") if len(args) > 1 else "r"
            flags = int(args[2] or 0) if len(args) > 2 and isinstance(args[2], int) else 0
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if any(flag in mode for flag in "wax+") or flags & write_flags or not _is_within(path, allowed_roots):
                raise PermissionError("Skill 文件访问未授权")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
            raise PermissionError("Skill 禁止创建子进程")
        if event in {"socket.getaddrinfo", "socket.connect"}:
            host = ""
            if event == "socket.getaddrinfo" and args:
                host = str(args[0]).lower()
            elif len(args) > 1 and isinstance(args[1], tuple) and args[1]:
                host = str(args[1][0]).lower()
            if "*" not in network_hosts and host not in network_hosts:
                raise PermissionError("Skill 网络访问未授权")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return
            if (address.is_private or address.is_loopback or address.is_link_local) and host not in network_hosts:
                raise PermissionError("Skill 禁止访问私网地址")

    sys.addaudithook(audit)


async def _execute(request: dict[str, Any]) -> Any:
    """加载指定 Skill 工具并执行一次异步调用。"""
    skill_root = Path(request["skill_root"]).resolve()
    tools_path = (skill_root / "tools.py").resolve()
    if skill_root not in tools_path.parents or not tools_path.is_file():
        raise ValueError("Skill tools.py 不存在或路径越界")
    project_src_root = Path(request["project_src_root"]).resolve()
    sys.path.insert(0, str(project_src_root.parent))
    _install_resource_limits(request.get("limits", {}))
    _install_audit_policy(request)
    spec = importlib.util.spec_from_file_location("isolated_skill_tools", tools_path)
    if spec is None or spec.loader is None:
        raise ValueError("Skill 工具模块无法加载")
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        spec.loader.exec_module(module)
        tool = module.get_tool(request["tool_name"]) if hasattr(module, "get_tool") else None
        if tool is None:
            raise ValueError("Skill 未导出目标工具")
        return await tool.ainvoke(request.get("payload", {}))


def _json_default(value: Any) -> Any:
    """把工具返回的少量非 JSON 标量转换为字符串。"""
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)


def main() -> int:
    """读取单次请求并输出单个 JSON 结果。"""
    sys.dont_write_bytecode = True
    try:
        raw = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("Skill Worker 输入超过上限")
        request = json.loads(raw.decode("utf-8"))
        result = asyncio.run(_execute(request))
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "error": type(exc).__name__}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, default=_json_default))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
