"""覆盖 QA-28-2：真实执行 TypeScript 响应式布局函数，不依赖源码字符串断言。"""

import json
import subprocess

import pytest


class TestChatChartLayout:
    """覆盖 28.5 的窄屏、全部图表类型和原始选项不变性。"""

    # 验证热力图分类轴及可见色阶在窄屏上不会与图例相互覆盖。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_heatmap_category_labels_and_scale_keep_separate_space(self) -> None:
        """分类轴只处理显示宽度，完整分类和热力点仍保持原值。"""
        # Arrange
        script = r"""
const fs=require('fs'),ts=require('./frontend/node_modules/typescript');
const compiled=ts.transpileModule(fs.readFileSync('frontend/src/components/chatChartLayout.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const container={exports:{}};new Function('module','exports',compiled)(container,container.exports);
const categories=['数据来源: mysql · 地区编码: CN-BJ','数据来源: oracle · 地区编码: CN-SH'];
const option={xAxis:{type:'category',data:['2026-01-01']},yAxis:{type:'category',data:categories},visualMap:{min:0,max:5,orient:'horizontal',bottom:0,show:true},series:[{type:'heatmap',data:[[0,0,5],[0,1,0]]}]};
const layout=container.exports.buildChatChartLayout(option,287,360,'zh-CN');
process.stdout.write(JSON.stringify(layout));
"""
        # Act
        response = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
        result = json.loads(response.stdout)["option"]
        # Assert
        assert result["yAxis"]["axisLabel"]["overflow"] == "truncate"
        assert result["yAxis"]["axisLabel"]["width"] <= 80
        assert result["yAxis"]["data"] == ["数据来源: mysql · 地区编码: CN-BJ", "数据来源: oracle · 地区编码: CN-SH"]
        assert result["visualMap"]["bottom"] > result["legend"]["bottom"]
        assert result["series"][0]["data"] == [[0, 0, 5], [0, 1, 0]]

    # 调用真实 TypeScript 布局函数，保留全部坐标数据并适配实际容器宽度。
    # Args: self - 实例；kind - 图表类型；width - 容器宽度。
    # Returns: 无返回值。
    @pytest.mark.parametrize("kind", ["line", "bar", "scatter", "heatmap", "pie", "table"])
    @pytest.mark.parametrize("width", [287, 460, 852])
    def test_layout_preserves_data_and_adapts_all_chart_types(self, kind: str, width: int) -> None:
        """窄屏减少数值刻度并压缩数字，不修改类别、null、聚合口径或历史选项。"""
        # Arrange
        script = r"""
const fs = require('fs');
const ts = require('./frontend/node_modules/typescript');
const source = fs.readFileSync('frontend/src/components/chatChartLayout.ts','utf8');
const compiled = ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const container={exports:{}};
new Function('module','exports',compiled)(container,container.exports);
const kind=process.argv[1], width=Number(process.argv[2]);
const option={title:[{text:'销售金额'},{text:'订单数量'}],grid:[{top:'8%',height:'32%'},{top:'50%',height:'32%'}],
 xAxis:[{type:'category',name:'日期',data:['2026-01-01','2026-01-02'],gridIndex:0},{type:'category',name:'日期',data:['2026-01-01','2026-01-02'],gridIndex:1}],
 yAxis:[{type:'value',name:'销售金额',gridIndex:0},{type:'value',name:'订单数量',gridIndex:1}],
 legend:{type:'scroll',bottom:0},series:[{type:kind,name:'地区编码: CN-BJ',data:[1800000,null],xAxisIndex:0,yAxisIndex:0},{type:kind,name:'地区编码: CN-SH',data:[20,0],xAxisIndex:1,yAxisIndex:1}]};
if(kind==='pie') {delete option.xAxis;delete option.yAxis;delete option.grid;option.series.forEach(item=>{item.center=['50%','50%'];item.radius=['25%','65%'];item.data=[{name:'CN-BJ',value:20},{name:'CN-SH',value:30}];});}
if(kind==='table') {Object.keys(option).forEach(key=>delete option[key]);option.columns=['date','amount'];option.rows=[{date:'2026-01-01',amount:null}];}
const original=JSON.stringify(option);
const layout=container.exports.buildChatChartLayout(option,width,360,'zh-CN');
const numeric=layout.option.yAxis?.[0];
process.stdout.write(JSON.stringify({unchanged:JSON.stringify(option)===original,height:layout.height,option:layout.option,
 seriesUnchanged:JSON.stringify(option.series?.map(item=>item.data))===JSON.stringify(layout.option.series?.map(item=>item.data)),
 numericLabel:numeric?.axisLabel?.formatter?.(1800000)}));
"""
        # Act
        response = subprocess.run(["node", "-e", script, kind, str(width)], capture_output=True, text=True, encoding="utf-8", check=True)
        result = json.loads(response.stdout)
        # Assert
        assert result["unchanged"] and result["seriesUnchanged"]
        if kind == "table":
            assert result["option"]["rows"][0]["amount"] is None
            return
        assert result["height"] >= 600
        assert result["option"]["legend"]["type"] == "scroll"
        if kind == "pie":
            assert result["option"]["series"][0]["center"] != result["option"]["series"][1]["center"]
            assert result["option"]["series"][0]["avoidLabelOverlap"] is True
            return
        assert all(axis.get("name", "") == "" for axis in result["option"]["yAxis"])
        assert all(axis["axisLabel"]["hideOverlap"] is True for axis in result["option"]["yAxis"])
        assert result["option"]["grid"][1]["top"] > result["option"]["grid"][0]["top"] + result["option"]["grid"][0]["height"]
        if width < 600:
            assert result["option"]["yAxis"][0]["splitNumber"] <= 3
            assert len(result["numericLabel"]) < len("1,800,000")

    # 验证没有尺寸或空配置时安全回退，且不同语言的刻度不混用单位。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_empty_option_and_language_fallback(self) -> None:
        """空输入、零宽和无效尺寸不会破坏布局；英文大数使用英文紧凑格式。"""
        # Arrange
        script = r"""
const fs=require('fs'),ts=require('./frontend/node_modules/typescript');
const compiled=ts.transpileModule(fs.readFileSync('frontend/src/components/chatChartLayout.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const container={exports:{}};new Function('module','exports',compiled)(container,container.exports);
const build=container.exports.buildChatChartLayout;
const empty=build({},0,360,'invalid-language');
const option={xAxis:{type:'value',name:'Revenue'},yAxis:{type:'value',name:'Orders'},series:[{type:'scatter',data:[[1800000,2]]}]};
const english=build(option,287,360,'en-US');
const invalid=build(option,NaN,360,'not_valid');
process.stdout.write(JSON.stringify({empty,english:english.option.xAxis.axisLabel.formatter(1800000),invalid:invalid.height,name:english.option.yAxis.name}));
"""
        # Act
        response = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
        result = json.loads(response.stdout)
        # Assert
        assert result["empty"] == {"option": {}, "height": 360}
        assert result["english"] == "1.8M"
        assert result["invalid"] >= 360
        assert result["name"] == "Orders"
