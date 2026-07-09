"""内置处理器 — 同比/环比/趋势/汇总/分布/排名/占比/异常 + 留存/漏斗/RFM/帕累托/相关性/增长率/季节/贡献度。"""

from src.tools.data_processor import DataProcessor, ProcessorResult, register


@register
class YoYProcessor(DataProcessor):
    name = "yoy"; intents = ["trend", "attribution"]; prefer_sql = True
    def process(self, rows, params):
        vc, tc = params.get("value_col",""), params.get("time_col","")
        if not vc or not tc or len(rows)<2: return ProcessorResult("数据不足",[],"table",rows,"low")
        vals=[self._f(r.get(vc)) for r in rows]; times=[self._s(r.get(tc)) for r in rows]
        ch=[{"period":times[i],"value":vals[i],"prev":vals[i-1],"change_pct":round((vals[i]-vals[i-1])/max(abs(vals[i-1]),1)*100,1)} for i in range(1,len(vals))]
        avg=round(sum(c["change_pct"] for c in ch)/len(ch),1) if ch else 0
        d="上升" if avg>0 else "下降" if avg<0 else "持平"
        return ProcessorResult(f"同比均值{avg}%，{d}",[f"最大增幅{max(c['change_pct'] for c in ch) if ch else 0}%"],"line",ch)


@register
class MoMProcessor(DataProcessor):
    name = "mom"; intents = ["trend"]; prefer_sql = True
    def process(self, rows, params):
        vc, tc = params.get("value_col",""), params.get("time_col","")
        if not vc or not tc or len(rows)<2: return ProcessorResult("数据不足",[],"table",rows,"low")
        vals=[self._f(r.get(vc)) for r in rows]; times=[self._s(r.get(tc)) for r in rows]
        ch=[{"period":times[i],"value":vals[i],"prev":vals[i-1],"change_pct":round((vals[i]-vals[i-1])/max(abs(vals[i-1]),1)*100,1)} for i in range(1,len(vals))]
        avg=round(sum(c["change_pct"] for c in ch)/len(ch),1) if ch else 0
        return ProcessorResult(f"环比均值{avg}%",[],"line",ch)


@register
class TrendProcessor(DataProcessor):
    name = "trend"; intents = ["trend"]; prefer_sql = False
    def process(self, rows, params):
        vc, tc = params.get("value_col",""), params.get("time_col","")
        if not vc or len(rows)<3: return ProcessorResult("数据不足",[],"table",rows,"low")
        vals=[self._f(r.get(vc)) for r in rows]; w=min(params.get("window",3),len(vals))
        ma=[round(sum(vals[max(0,i-w+1):i+1])/(i-max(0,i-w+1)+1),2) for i in range(len(vals))]
        fh=sum(vals[:len(vals)//2])/max(len(vals)//2,1); sh=sum(vals[len(vals)//2:])/max(len(vals)-len(vals)//2,1)
        d="上升" if sh>fh*1.05 else "下降" if fh>sh*1.05 else "平稳"
        data=[{"value":vals[i],"moving_avg":ma[i]} for i in range(len(vals))]
        return ProcessorResult(f"{w}期移动均{d}趋势",[f"前均值{round(fh,2)}，后均值{round(sh,2)}"],"line",data)


@register
class AggregationProcessor(DataProcessor):
    name = "aggregation"; intents = ["aggregation","query"]; prefer_sql = True
    def process(self, rows, params):
        gc,vc=params.get("group_col",""),params.get("value_col","")
        if not gc or not vc or not rows: return ProcessorResult("缺少参数",[],"table",rows,"low")
        g=self._group(rows,gc,vc)
        data=[{"group":k,"total":round(sum(v),2),"avg":round(sum(v)/len(v),2),"count":len(v)} for k,v in g.items()]
        data.sort(key=lambda x:x["total"],reverse=True)
        return ProcessorResult(f"{len(data)}组，最高{data[0]['group']}" if data else "", [f"Top3: {', '.join(d['group'] for d in data[:3])}"],"bar",data)


@register
class DistributionProcessor(DataProcessor):
    name = "distribution"; intents = ["aggregation"]; prefer_sql = False
    def process(self, rows, params):
        vc=params.get("value_col",""); nb=params.get("buckets",5)
        if not vc or not rows: return ProcessorResult("缺少参数",[],"table",rows,"low")
        vals=sorted([self._f(r.get(vc)) for r in rows if self._f(r.get(vc))!=0])
        if not vals: return ProcessorResult("无数值数据",[],"table",rows,"low")
        mn,mx=vals[0],vals[-1]
        if mn==mx: return ProcessorResult(f"所有值相同({mn})",[],"table",rows,"low")
        step=(mx-mn)/nb
        data=[{"range":f"{round(mn+step*i,2)}-{round(mn+step*(i+1),2)}","count":sum(1 for v in vals if mn+step*i<=v<mn+step*(i+1))} for i in range(nb)]
        return ProcessorResult(f"范围{mn}~{mx}，中位{self._pct(vals,0.5)}",[f"Q1={self._pct(vals,0.25)} Q3={self._pct(vals,0.75)}"],"bar",data)


@register
class RankingProcessor(DataProcessor):
    name = "ranking"; intents = ["aggregation"]; prefer_sql = True
    def process(self, rows, params):
        vc,nc=params.get("value_col",""),params.get("name_col","")
        if not vc or not rows: return ProcessorResult("缺少参数",[],"table",rows,"low")
        sr=sorted(rows,key=lambda r:self._f(r.get(vc)),reverse=True)
        tn=min(params.get("top_n",5),len(sr))
        total=sum(self._f(r.get(vc)) for r in sr); top_s=sum(self._f(r.get(vc)) for r in sr[:tn])
        conc=round(top_s/total*100,1) if total>0 else 0
        data=[{"rank":i+1,"name":self._s(r.get(nc),f"#{i+1}"),"value":self._f(r.get(vc))} for i,r in enumerate(sr[:tn])]
        return ProcessorResult(f"Top{tn}集中度{conc}%",[],"bar",data)


@register
class ProportionProcessor(DataProcessor):
    name = "proportion"; intents = ["aggregation"]; prefer_sql = True
    def process(self, rows, params):
        vc,nc=params.get("value_col",""),params.get("name_col","")
        if not vc or not rows: return ProcessorResult("缺少参数",[],"table",rows,"low")
        total=sum(self._f(r.get(vc)) for r in rows)
        if total==0: return ProcessorResult("总和为零",[],"table",rows,"low")
        data=[{"name":self._s(r.get(nc)),"value":self._f(r.get(vc)),"pct":round(self._f(r.get(vc))/total*100,1)} for r in rows]
        data.sort(key=lambda x:x["value"],reverse=True)
        return ProcessorResult(f"{len(data)}项，最大{data[0]['pct']}%" if data else "",[f"{d['name']}:{d['pct']}%" for d in data[:3]],"pie",data)


@register
class AnomalyProcessor(DataProcessor):
    name = "anomaly"; intents = ["attribution"]; prefer_sql = False
    def process(self, rows, params):
        vc=params.get("value_col","")
        if not vc or not rows: return ProcessorResult("缺少参数",[],"table",rows,"low")
        vals=[self._f(r.get(vc)) for r in rows]; mean=sum(vals)/len(vals); std=self._std(vals)
        if std==0: return ProcessorResult("无波动",[],"table",rows,"low")
        th=params.get("threshold",2.0)
        anom=[{"index":i,"value":vals[i],"z_score":round(abs(vals[i]-mean)/std,2)} for i in range(len(rows)) if abs(vals[i]-mean)/std>th]
        return ProcessorResult(f"发现{len(anom)}个异常点(Z>{th})",[f"#{a['index']}: z={a['z_score']}" for a in anom[:5]],"scatter",anom,"medium" if std<0.01 else "high")


@register
class RetentionProcessor(DataProcessor):
    """留存/队列分析 — 同一用户群在后续时间段的回访比例。"""
    name = "retention"; intents = ["trend", "aggregation"]; prefer_sql = False

    def process(self, rows, params):
        tc, vc = params.get("time_col", ""), params.get("value_col", "")
        if not tc or len(rows) < 2: return ProcessorResult("数据不足", [], "table", rows, "low")
        periods = [self._s(r.get(tc)) for r in rows]
        vals = [self._f(r.get(vc)) for r in rows] if vc else [1] * len(rows)
        if not vals or vals[0] == 0: return ProcessorResult("基数为零", [], "table", rows, "low")
        base = vals[0]
        data = [{"period": periods[i], "retained": vals[i], "rate_pct": round(vals[i] / base * 100, 1)}
                for i in range(len(periods))]
        end_rate = data[-1]["rate_pct"] if data else 0
        quality = "高" if end_rate > 60 else "中" if end_rate > 30 else "低"
        return ProcessorResult(f"留存率 {end_rate}%（{quality}）", [f"第{i+1}期: {d['rate_pct']}%" for i, d in enumerate(data[:5])], "line", data)


@register
class FunnelProcessor(DataProcessor):
    """漏斗分析 — 多步骤转化率。每行一个步骤，value_col 为人数。"""
    name = "funnel"; intents = ["attribution", "aggregation"]; prefer_sql = False

    def process(self, rows, params):
        nc, vc = params.get("name_col", ""), params.get("value_col", "")
        if not nc or not vc or len(rows) < 2: return ProcessorResult("数据不足", [], "table", rows, "low")
        steps = [{"step": self._s(r.get(nc)), "count": self._f(r.get(vc))} for r in rows]
        if steps[0]["count"] == 0: return ProcessorResult("顶部为零", [], "table", rows, "low")
        base = steps[0]["count"]
        for s in steps:
            s["rate"] = round(s["count"] / base * 100, 1)
        # 计算步骤间转化率
        for i in range(1, len(steps)):
            prev = steps[i - 1]["count"]
            steps[i]["step_rate"] = round(steps[i]["count"] / prev * 100, 1) if prev else 0
        steps[0]["step_rate"] = 100.0
        overall = steps[-1]["rate"]
        leak = steps[0]["count"] - steps[-1]["count"]
        return ProcessorResult(f"整体转化率 {overall}%，流失 {leak} 人", [f"{s['step']}→{steps[i+1]['step']}: {steps[i+1]['step_rate']}%" if i < len(steps) - 1 else "" for i, s in enumerate(steps[:-1])], "bar", steps)


@register
class RFMProcessor(DataProcessor):
    """RFM 用户分层 — 按最近消费/频次/金额评分。需 recency/frequency/monetary 三列。"""
    name = "rfm"; intents = ["aggregation"]; prefer_sql = False

    def process(self, rows, params):
        if not rows: return ProcessorResult("无数据", [], "table", rows, "low")
        rc = params.get("recency_col", "")
        fc = params.get("frequency_col", "")
        mc = params.get("monetary_col", "")
        if not rc or not fc or not mc: return ProcessorResult("缺少 recency/frequency/monetary 列", [], "table", rows, "low")
        r_vals = sorted([self._f(r.get(rc)) for r in rows])
        f_vals = sorted([self._f(r.get(fc)) for r in rows])
        m_vals = sorted([self._f(r.get(mc)) for r in rows])
        r_mid, f_mid, m_mid = self._pct(r_vals, 0.5), self._pct(f_vals, 0.5), self._pct(m_vals, 0.5)
        tiers = {"高价值": 0, "重要发展": 0, "重要保持": 0, "一般价值": 0}
        for r in rows:
            r_score = 1 if self._f(r.get(rc)) <= r_mid else 0  # recency 越小越好
            f_score = 1 if self._f(r.get(fc)) >= f_mid else 0
            m_score = 1 if self._f(r.get(mc)) >= m_mid else 0
            total = r_score + f_score + m_score
            if total == 3: tiers["高价值"] += 1
            elif total == 2: tiers["重要发展" if r_score == 0 else "重要保持"] += 1
            else: tiers["一般价值"] += 1
        data = [{"tier": k, "count": v} for k, v in tiers.items() if v > 0]
        top = max(tiers, key=tiers.get)
        return ProcessorResult(f"最高分层: {top}（{tiers[top]}人）", [f"{k}: {v}人" for k, v in tiers.items()], "pie", data)


@register
class ParetoProcessor(DataProcessor):
    """帕累托分析 — 80/20 贡献度。value_col 累计占比多少达到 80%。"""
    name = "pareto"; intents = ["aggregation"]; prefer_sql = False

    def process(self, rows, params):
        vc, nc = params.get("value_col", ""), params.get("name_col", "")
        if not vc or not rows: return ProcessorResult("缺少参数", [], "table", rows, "low")
        sr = sorted(rows, key=lambda r: self._f(r.get(vc)), reverse=True)
        total = sum(self._f(r.get(vc)) for r in sr)
        if total == 0: return ProcessorResult("总和为零", [], "table", rows, "low")
        cum, p80_idx = 0, len(sr)
        data = []
        for i, r in enumerate(sr):
            v = self._f(r.get(vc)); cum += v
            pct = round(cum / total * 100, 1)
            data.append({"name": self._s(r.get(nc)), "value": v, "cum_pct": pct})
            if pct >= 80 and p80_idx == len(sr):
                p80_idx = i + 1
        ratio = round(p80_idx / len(sr) * 100, 1)
        return ProcessorResult(f"{p80_idx}/{len(sr)} 项（{ratio}%）贡献 80%", [f"Top 1 占比 {round(self._f(sr[0].get(vc))/total*100,1)}%"], "line", data)


@register
class CorrelationProcessor(DataProcessor):
    """相关性分析 — Pearson 相关系数。需两个数值列。"""
    name = "correlation"; intents = ["attribution"]; prefer_sql = False

    def process(self, rows, params):
        c1, c2 = params.get("col1", ""), params.get("col2", "")
        if not c1 or not c2 or len(rows) < 3: return ProcessorResult("数据不足或缺少列", [], "table", rows, "low")
        x = [self._f(r.get(c1)) for r in rows]; y = [self._f(r.get(c2)) for r in rows]
        n = len(x); sx, sy = sum(x), sum(y)
        sxy = sum(x[i] * y[i] for i in range(n)); sx2 = sum(v * v for v in x); sy2 = sum(v * v for v in y)
        denom = ((n * sx2 - sx * sx) * (n * sy2 - sy * sy)) ** 0.5
        if denom == 0: return ProcessorResult("方差为零", [], "table", rows, "low")
        r = round((n * sxy - sx * sy) / denom, 4)
        strength = "强" if abs(r) > 0.7 else "中" if abs(r) > 0.4 else "弱"
        direction = "正" if r > 0 else "负"
        return ProcessorResult(f"{direction}相关，系数 {r}（{strength}）", [f"R² = {round(r*r, 4)}"], "scatter", [{"x": self._f(r.get(c1)), "y": self._f(r.get(c2))} for r in rows[:500]])


@register
class GrowthRateProcessor(DataProcessor):
    """复合增长率 — CAGR 和逐期增长率。"""
    name = "growth_rate"; intents = ["trend"]; prefer_sql = False

    def process(self, rows, params):
        vc, tc = params.get("value_col", ""), params.get("time_col", "")
        if not vc or len(rows) < 2: return ProcessorResult("数据不足", [], "table", rows, "low")
        vals = [self._f(r.get(vc)) for r in rows]; times = [self._s(r.get(tc)) for r in rows]
        if vals[0] == 0: return ProcessorResult("基期为零", [], "table", rows, "low")
        periods = len(vals) - 1
        cagr = round(((vals[-1] / vals[0]) ** (1 / periods) - 1) * 100, 2) if periods > 0 else 0
        data = [{"period": times[i], "value": vals[i], "growth": round((vals[i]-vals[i-1])/max(abs(vals[i-1]),1)*100,2) if i > 0 else 0} for i in range(len(vals))]
        avg_growth = round(sum(d["growth"] for d in data[1:]) / periods, 2) if periods else 0
        vol = round(self._std([d["growth"] for d in data[1:]]) / max(abs(avg_growth), 1), 2) if avg_growth else 0
        return ProcessorResult(f"CAGR {cagr}%，均增 {avg_growth}%，波动 {vol}", [f"首期 {vals[0]} → 末期 {vals[-1]}", f"最大增 {max(d['growth'] for d in data) if data else 0}%"], "line", data)


@register
class SeasonalDecompositionProcessor(DataProcessor):
    """季节分解 — 简单移动平均剥离趋势，残差 = 原始 - 趋势。"""
    name = "seasonal"; intents = ["trend"]; prefer_sql = False

    def process(self, rows, params):
        vc, tc = params.get("value_col", ""), params.get("time_col", "")
        if not vc or len(rows) < 6: return ProcessorResult("数据不足（需≥6期）", [], "table", rows, "low")
        vals = [self._f(r.get(vc)) for r in rows]; times = [self._s(r.get(tc)) for r in rows]
        w = min(params.get("window", 3), len(vals) // 2)
        trend = []
        for i in range(len(vals)):
            lo = max(0, i - w // 2); hi = min(len(vals), i + w // 2 + 1)
            trend.append(round(sum(vals[lo:hi]) / (hi - lo), 2))
        # 残差
        residual = [round(vals[i] - trend[i], 2) for i in range(len(vals))]
        # 趋势方向
        fh = sum(trend[:len(trend)//2]) / max(len(trend)//2, 1)
        sh = sum(trend[len(trend)//2:]) / max(len(trend)-len(trend)//2, 1)
        direction = "上升" if sh > fh * 1.03 else "下降" if fh > sh * 1.03 else "平稳"
        # 季节性检测 —— 周期波动
        volatility = round(self._std(residual), 2)
        seasonality = "有" if volatility > self._std(vals) * 0.3 else "不明显"
        data = [{"period": times[i] if i < len(times) else str(i), "value": vals[i], "trend": trend[i], "residual": residual[i]} for i in range(len(vals))]
        return ProcessorResult(f"{direction}趋势，季节性{seasonality}（波动{volatility}）", [f"趋势均值 {round(sum(trend)/len(trend),2)}", f"残差标准差 {volatility}"], "line", data)


@register
class ContributionProcessor(DataProcessor):
    """贡献度分析 — 各维度对总体变化的贡献百分比。"""
    name = "contribution"; intents = ["attribution"]; prefer_sql = False

    def process(self, rows, params):
        vc, nc = params.get("value_col", ""), params.get("name_col", "")
        if not vc or not nc or len(rows) < 2: return ProcessorResult("数据不足", [], "table", rows, "low")
        changes = []
        for i, r in enumerate(rows):
            val = self._f(r.get(vc))
            # 与前一行比较的变化量
            prev_val = self._f(rows[i-1].get(vc)) if i > 0 else val
            changes.append({"name": self._s(r.get(nc)), "value": val, "change": round(val - prev_val, 2)})
        total_change = sum(abs(c["change"]) for c in changes[1:]) if len(changes) > 1 else 1
        if total_change == 0: return ProcessorResult("无变化", [], "table", rows, "low")
        for c in changes[1:]:
            c["contribution_pct"] = round(c["change"] / total_change * 100, 1)
        top_contributors = sorted(changes[1:], key=lambda x: abs(x["contribution_pct"]), reverse=True)[:3]
        return ProcessorResult(f"最大贡献: {top_contributors[0]['name']} ({top_contributors[0]['contribution_pct']}%)" if top_contributors else "", [f"{c['name']}: {c['contribution_pct']}%" for c in top_contributors], "waterfall", changes, "high" if total_change > 0 else "medium")


@register
class CrossPivotProcessor(DataProcessor):
    """交叉透视 — 多键分组交叉统计。row_col 行键, col_col 列键, value_col 值列。SQL 可做，脚本辅助。"""
    name = "cross_pivot"; intents = ["aggregation"]; prefer_sql = True

    def process(self, rows, params):
        rc, cc, vc = params.get("row_col",""), params.get("col_col",""), params.get("value_col","")
        if not rc or not cc or not vc or not rows: return ProcessorResult("缺少行列参数",[],"table",rows,"low")
        pivot: dict[str, dict[str, list[float]]] = {}
        row_keys, col_keys = set(), set()
        for r in rows:
            rk, ck = self._s(r.get(rc)), self._s(r.get(cc)); v = self._f(r.get(vc))
            row_keys.add(rk); col_keys.add(ck)
            pivot.setdefault(rk, {}).setdefault(ck, []).append(v)
        data = [{"row": rk, **{ck: round(sum(pivot.get(rk,{}).get(ck,[])),2) for ck in sorted(col_keys)}} for rk in sorted(row_keys)]
        return ProcessorResult(f"{len(row_keys)}×{len(col_keys)} 透视表", [f"行维度 {len(row_keys)} 个, 列维度 {len(col_keys)} 个"], "table", data)


@register
class ABTestProcessor(DataProcessor):
    """A/B 对比 — 两组数据的均值差和置信度估算。group_col 组标识, value_col 指标。"""
    name = "ab_test"; intents = ["attribution", "aggregation"]; prefer_sql = False

    def process(self, rows, params):
        gc, vc = params.get("group_col",""), params.get("value_col","")
        if not gc or not vc or len(rows)<4: return ProcessorResult("数据不足",[],"table",rows,"low")
        groups = self._group(rows, gc, vc)
        names = list(groups.keys())
        if len(names) < 2: return ProcessorResult("分组不足2组",[],"table",rows,"low")
        a, b = names[0], names[1]
        va, vb = groups[a], groups[b]
        ma, mb = sum(va)/len(va), sum(vb)/len(vb)
        sa, sb = self._std(va), self._std(vb)
        diff = round(ma - mb, 4); diff_pct = round(diff / max(abs(mb), 1) * 100, 2)
        # Welch t-test 近似
        se = ((sa**2/len(va)) + (sb**2/len(vb))) ** 0.5 if (sa or sb) else 1
        t = abs(diff) / se if se > 0 else 0
        sig = "显著" if t > 2 else "边缘显著" if t > 1 else "不显著"
        data = [{"group": a, "mean": round(ma,2), "std": round(sa,2), "count": len(va)},
                {"group": b, "mean": round(mb,2), "std": round(sb,2), "count": len(vb)}]
        return ProcessorResult(f"{a} vs {b}: 差异 {diff_pct}%, t={round(t,2)}（{sig}）", [f"{a} 均值 {round(ma,2)}, {b} 均值 {round(mb,2)}", f"样本量 {len(va)} vs {len(vb)}"], "bar", data)


@register
class BudgetVarianceProcessor(DataProcessor):
    """预算方差 — 两数值列差异分析（预算 vs 实际）。actual_col 实际, budget_col 预算。"""
    name = "budget_variance"; intents = ["attribution"]; prefer_sql = False

    def process(self, rows, params):
        ac, bc, nc = params.get("actual_col",""), params.get("budget_col",""), params.get("name_col","")
        if not ac or not bc or not rows: return ProcessorResult("缺少列参数",[],"table",rows,"low")
        data = []
        for r in rows:
            actual = self._f(r.get(ac)); budget = self._f(r.get(bc))
            diff = round(actual - budget, 2)
            pct = round(diff / max(abs(budget), 1) * 100, 2)
            data.append({"name": self._s(r.get(nc)), "actual": actual, "budget": budget,
                         "diff": diff, "diff_pct": pct,
                         "status": "超支" if diff > 0 else "节余" if diff < 0 else "持平"})
        total_actual = sum(d["actual"] for d in data); total_budget = sum(d["budget"] for d in data)
        total_diff = round(total_actual - total_budget, 2)
        over_count = sum(1 for d in data if d["status"] == "超支")
        return ProcessorResult(f"总差异 {total_diff}（{round(total_diff/max(abs(total_budget),1)*100,2)}%），{over_count}/{len(data)} 项超支", [f"最大超支: {max(data,key=lambda d:d['diff'])['name']}", f"最大节余: {min(data,key=lambda d:d['diff'])['name']}"], "bar", data)


@register
class GeoDistributionProcessor(DataProcessor):
    """地域分布 — 按地理维度分组统计 + 集中度。"""
    name = "geo_distribution"; intents = ["aggregation"]; prefer_sql = False

    def process(self, rows, params):
        gc, vc = params.get("group_col",""), params.get("value_col","")
        if not gc or not vc or not rows: return ProcessorResult("缺少参数",[],"table",rows,"low")
        groups = self._group(rows, gc, vc)
        data = [{"region": k, "total": round(sum(v),2), "avg": round(sum(v)/len(v),2), "count": len(v)} for k, v in groups.items()]
        data.sort(key=lambda x: x["total"], reverse=True)
        total = sum(d["total"] for d in data)
        for d in data: d["share_pct"] = round(d["total"] / total * 100, 1) if total > 0 else 0
        top3_share = sum(d["share_pct"] for d in data[:3])
        # 集中度 (Gini 简化)
        shares = sorted([d["share_pct"] for d in data])
        gini = round(sum((2*i - len(shares) - 1) * s for i, s in enumerate(shares)) / (len(shares) * sum(shares)) if sum(shares) > 0 else 0, 3)
        concentrated = "高度集中" if gini > 0.6 else "中度集中" if gini > 0.4 else "分散"
        return ProcessorResult(f"{len(data)} 地区，Top3 占比 {top3_share}%，Gini {gini}（{concentrated}）", [f"最高: {data[0]['region']} ({data[0]['share_pct']}%)" if data else ""], "bar", data)


@register
class MarketBasketProcessor(DataProcessor):
    """购物篮关联 — 简化共现分析。找出同时出现超过阈值的物品对。
    入参: id_col(交易ID), item_col(物品名), min_cooccur(最小共现次数, 默认2)"""
    name = "market_basket"; intents = ["attribution"]; prefer_sql = False

    def process(self, rows, params):
        ic, item_col = params.get("id_col",""), params.get("item_col","")
        if not ic or not item_col or len(rows) < 2: return ProcessorResult("数据不足",[],"table",rows,"low")
        # 按交易 ID 分组
        baskets: dict[str, set] = {}
        for r in rows:
            tid = self._s(r.get(ic)); item = self._s(r.get(item_col))
            if tid: baskets.setdefault(tid, set()).add(item)
        if len(baskets) < 2: return ProcessorResult("交易数不足",[],"table",rows,"low")
        # 统计物品对共现
        pairs: dict[tuple, int] = {}
        items: dict[str, int] = {}
        for basket in baskets.values():
            for item in basket: items[item] = items.get(item, 0) + 1
            bl = list(basket)
            for i in range(len(bl)):
                for j in range(i + 1, len(bl)):
                    pair = (bl[i], bl[j]) if bl[i] < bl[j] else (bl[j], bl[i])
                    pairs[pair] = pairs.get(pair, 0) + 1
        mc = params.get("min_cooccur", 2)
        data = [{"item_a": p[0], "item_b": p[1], "co_count": c,
                 "lift": round(c * len(baskets) / max(items.get(p[0],1) * items.get(p[1],1), 1), 2)}
                for p, c in pairs.items() if c >= mc]
        data.sort(key=lambda x: x["lift"], reverse=True)
        top = data[:10]
        return ProcessorResult(f"发现 {len(data)} 个物品对（min_cooccur≥{mc}）",
                               [f"{d['item_a']} + {d['item_b']}: 共现{d['co_count']}次, lift={d['lift']}" for d in top[:5]],
                               "table", top, "medium" if len(baskets) < 100 else "high")


@register
class SimplePredictionProcessor(DataProcessor):
    """简单预测 — 线性回归外推。同一时间序列的线性预测。
    警告: 线性模型只适用于简单趋势，复杂模式预测不可靠。"""
    name = "prediction"; intents = ["trend"]; prefer_sql = False

    def process(self, rows, params):
        vc, tc = params.get("value_col",""), params.get("time_col","")
        if not vc or len(rows) < 3: return ProcessorResult("数据不足",[],"table",rows,"low")
        vals = [self._f(r.get(vc)) for r in rows]; times = [self._s(r.get(tc)) for r in rows] if tc else [str(i) for i in range(len(rows))]
        n = len(vals); x_mean = (n - 1) / 2; y_mean = sum(vals) / n
        sxy = sum((i - x_mean) * (vals[i] - y_mean) for i in range(n))
        sxx = sum((i - x_mean) ** 2 for i in range(n))
        if sxx == 0: return ProcessorResult("方差为零",[],"table",rows,"low")
        slope = round(sxy / sxx, 4); intercept = round(y_mean - slope * x_mean, 4)
        r_squared = round((sxy / ((sxx * sum((v - y_mean)**2 for v in vals))**0.5)) ** 2, 4) if sxx else 0
        steps = params.get("forecast_steps", 3)
        forecast = [round(intercept + slope * (n + i), 2) for i in range(steps)]
        data = [{"index": i, "label": times[i] if i < len(times) else f"预测{i-n+1}", "value": vals[i] if i < n else forecast[i-n]} for i in range(n + steps)]
        for i in range(n, n + steps): data[i]["predicted"] = True
        direction = "增长" if slope > 0 else "下降" if slope < 0 else "持平"
        confidence = "low" if r_squared < 0.5 else "medium" if r_squared < 0.8 else "high"
        return ProcessorResult(f"线性{direction}，R²={round(r_squared,2)}，预测{steps}期",
                               [f"斜率={slope}，截距={intercept}", f"预测值: {', '.join(str(f) for f in forecast)}",
                                "⚠ 线性模型简化，复杂趋势预测不可靠"],
                               "line", data, confidence)
