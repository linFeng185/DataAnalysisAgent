"""覆盖造数 CLI 的默认离线与参数兼容行为。"""

from pathlib import Path

import pytest

from src.testdata.cli import main


class TestDatasetCLI:
    """覆盖功能 16.5.4 的命令行入口。"""

    # 验证原脚本常用参数可离线运行，并输出可校验清单。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    def test_offline_default_and_verify(self, tmp_path: Path) -> None:
        """默认不要求数据库凭证，支持 --db/--scale/--file-only。"""
        output = tmp_path / "cli"
        assert (
            main(["--db", "postgres", "--scale", "100", "--file-only", "--output-dir", str(output)])
            == 0
        )
        assert main(["--verify-only", "--output-dir", str(output)]) == 0

    # 验证危险或矛盾参数不触发生成。
    # Args: self - 测试实例；tmp_path - 临时目录；flags - 非法参数。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "flags",
        [
            ["--import-db"],
            ["--import-db", "--file-only"],
            ["--scale", "0"],
            ["--extend-from", "missing"],
            ["--verify-only", "--extend-from", "missing"],
        ],
    )
    def test_invalid_cli_arguments(self, tmp_path: Path, flags: list[str]) -> None:
        """缺少入库确认或无效规模时返回非零。"""
        output = tmp_path / "missing"
        assert main(["--output-dir", str(output), *flags]) != 0
        assert not output.exists()
