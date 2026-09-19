"""报表含负值纯柱图：数值轴以 0 为对称中心的渲染期契约。

本文件真实执行 Node 行为测试（`node --test tests/chartBase.test.mjs`），不靠源码字符串断言：
断言对象是 `withChartBaseline` 在独立进程里跑出来的返回值。

实机取证（主代理，非推测）：「未达成门店缺口金额」与「毛利告警商品分布」各 24 个全负柱值，
`yAxis.inverse=false`，零线 y=38 落在 grid 上沿（grid 上下 38/260），柱子从顶部往下垂。
根因是自动范围在全负数据上退化成 [min, 0]，不是符号写错。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


# 方法作用：定位前端目录，Node 行为测试必须以它为工作目录。
# Args: 无。
# Returns: 仓库内 frontend 目录的绝对路径。
def frontend_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend"


# 方法作用：确认本机可执行 node，并返回其路径。
# Args: 无。
# Returns: node 可执行文件路径；本机没有 node 时跳过用例。
def node_binary() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 node，无法执行前端行为测试")
    return node


class TestReportCenteredZeroAxis:
    """覆盖功能 23.41 本轮增量：含负值纯柱图数值轴以 0 为对称中心（仅渲染期）。"""

    # 方法作用：真实运行 Node 行为套件，确认新增用例真的执行且全部通过。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_centered_zero_axis_node_suite_passes(self) -> None:
        """含负值柱图的居中规则必须由真正调用 withChartBaseline 的用例兜住。

        Arrange：以 TAP 报告运行 frontend/tests/chartBase.test.mjs。
        Act：读取子进程退出码与 TAP 汇总。
        Assert：fail 为 0，且本轮新增的关键用例以 `ok` 出现在报告里（未被跳过）。
        """
        # Arrange
        frontend = frontend_dir()
        completed = subprocess.run(
            [node_binary(), "--test", "--test-reporter=tap", "tests/chartBase.test.mjs"],
            cwd=frontend,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        report = completed.stdout
        ok_titles = [line for line in report.splitlines() if line.startswith("ok ")]

        # Act
        passed = int(next(line.split()[-1] for line in report.splitlines() if line.startswith("# pass ")))
        failed = int(next(line.split()[-1] for line in report.splitlines() if line.startswith("# fail ")))

        # Assert：整套行为测试全绿，且本轮新增场景确实在被执行。
        assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
        assert failed == 0, report[-2000:]
        assert passed >= 27, f"用例数少于预期：{passed}"
        for title in (
            "全负柱图（实机取证形状：24 个全负值 + inverse:false）",
            "堆叠柱图按 samesign 正负分别累计取界：相反符号不抵消（审计实机复现形状）",
            "堆叠柱图：同号多系列按类目累计，既不互相抵消也不退回单点取值",
            "显式范围、反向轴、对数轴与判不出结构的轴：保守不动",
            "系列显式声明轴索引：非 0 索引判不出绑定关系，保守不动；显式 0 仍生效",
            "0 参考线：居中命中时给首系列补一条随主题的零线，且只补一条",
            "分组切换：负值分组切到正值分组后不留 min/max",
        ):
            assert any(title in line for line in ok_titles), f"关键用例未执行或未通过：{title}"

    # 方法作用：用独立 Node 进程复刻实机形状，验证轴范围、符号与入参纯度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_full_negative_real_payload_shape_is_centered(self) -> None:
        """24 个全负柱值 + yAxis.inverse=false 的实机形状必须得到对称轴，且数据一字不改。

        Arrange：在 Node 进程里构造与「未达成门店缺口金额」同形的 option。
        Act：调用 withChartBaseline 并把结果序列化成 JSON。
        Assert：上下界对称、间隔可读、未反转轴、数据逐字保留、入参未被写入 min。
        """
        # Arrange：数据与 token 全部为合成值，不含任何生产数据。
        probe = """
const { withChartBaseline } = await import('./src/components/report/chartBase.ts');
const tokens = {
  accent: '#2563eb', surface: '#ffffff', border: '#e8eaed',
  textPrimary: '#1f2329', textSecondary: '#646a73', fontFamily: 'sans-serif',
  chartPalette: ['#2563eb'], sequentialPalette: ['#cde2fb', '#0d366b'],
};
const values = Array.from({ length: 24 }, (_, index) => -(index + 1) * 100);
const option = {
  xAxis: { type: 'category', data: Array.from({ length: 24 }, (_, index) => `门店${index + 1}`) },
  yAxis: { type: 'value', inverse: false },
  series: [{ type: 'bar', name: '缺口金额', data: values }],
};
const result = withChartBaseline(option, tokens);
process.stdout.write(JSON.stringify({
  min: result.yAxis.min,
  max: result.yAxis.max,
  interval: result.yAxis.interval,
  inverse: result.yAxis.inverse,
  data_intact: JSON.stringify(result.series[0].data) === JSON.stringify(values),
  input_has_min: 'min' in option.yAxis,
}));
"""

        # Act
        completed = subprocess.run(
            [node_binary(), "--input-type=module", "-e", probe],
            cwd=frontend_dir(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        payload = json.loads(completed.stdout)

        # Assert：0 落在中轴，负值继续向下，原值与正负号不动，入参不被就地修改。
        assert payload == {
            "min": -2500,
            "max": 2500,
            "interval": 1250,
            "inverse": False,
            "data_intact": True,
            "input_has_min": False,
        }

    # 方法作用：用独立 Node 进程复现审计给出的堆叠形状，验证负柱不再被轴截断。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_stacked_samesign_extent_is_not_clipped(self) -> None:
        """审计实机形状：三个同 stack 柱系列 [-900]/[-900]/[900] 的真实堆叠边界是 -1800。

        主代理取证：ECharts 6.1 实例的 stackResultDimension 为 [-900, -1800, 900]，
        默认 samesign 策略把正负分别累计；按代数相加只会取到 -900，第二根负柱被截断
        （logs/zero-axis-audit-20260919/stacked-bar-audit.json：extent [-1000,1000]、clipped=true）。

        Arrange：在 Node 进程里构造该形状并调用 withChartBaseline。
        Act：读取轴范围与首系列 0 参考线。
        Assert：下界覆盖 -1800、上界覆盖 +900，且补上了随主题的静默零线。
        """
        # Arrange：合成数据，不含任何生产取值。
        probe = """
const { withChartBaseline } = await import('./src/components/report/chartBase.ts');
const tokens = {
  accent: '#2563eb', surface: '#ffffff', border: '#e8eaed',
  textPrimary: '#1f2329', textSecondary: '#646a73', fontFamily: 'sans-serif',
  chartPalette: ['#2563eb'], sequentialPalette: ['#cde2fb', '#0d366b'],
};
const option = {
  xAxis: { type: 'category', data: ['测试类目'] },
  yAxis: { type: 'value' },
  series: [
    { type: 'bar', name: 'a', stack: 'total', data: [-900] },
    { type: 'bar', name: 'b', stack: 'total', data: [-900] },
    { type: 'bar', name: 'c', stack: 'total', data: [900] },
  ],
};
const result = withChartBaseline(option, tokens);
process.stdout.write(JSON.stringify({
  min: result.yAxis.min,
  max: result.yAxis.max,
  covers_stacked_bottom: result.yAxis.min <= -1800,
  covers_stacked_top: result.yAxis.max >= 900,
  zero_line: result.series[0].markLine.data,
  zero_line_color: result.series[0].markLine.lineStyle.color,
}));
"""

        # Act
        completed = subprocess.run(
            [node_binary(), "--input-type=module", "-e", probe],
            cwd=frontend_dir(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        payload = json.loads(completed.stdout)

        # Assert：真实堆叠边界 -1800 必须落在轴内，零线锚在 yAxis 的 0 位。
        assert payload == {
            "min": -2000,
            "max": 2000,
            "covers_stacked_bottom": True,
            "covers_stacked_top": True,
            "zero_line": [{"yAxis": 0}],
            "zero_line_color": "#646a73",
        }

    # 方法作用：确认修复发生在真实渲染路径上，且渲染期不产生 console 日志。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_renderer_wires_chart_baseline_without_console_logs(self) -> None:
        """渲染器必须继续经 withChartBaseline 出图，轴居中的判定入口才有意义。

        Arrange：读取渲染器与图表基线的源码文本。
        Act：检查调用点与日志调用。
        Assert：withChartBaseline 仍被 ChartBlock 调用；正常渲染路径没有 console 输出。
        """
        # Arrange
        renderer = (frontend_dir() / "src/components/report/ReportDocumentRenderer.tsx").read_text(
            encoding="utf-8"
        )
        source = (frontend_dir() / "src/components/report/chartBase.ts").read_text(encoding="utf-8")

        # Act + Assert
        assert "withChartBaseline(option, tokens)" in renderer
        assert "console." not in source
