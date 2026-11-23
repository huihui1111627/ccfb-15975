"""单文件 HTML 报告生成: 操作序列/同步点、问题重排、修复建议、验收对比。

不依赖任何外部资源(无网络、无框架), 数据以 JSON 内嵌, 双击即可打开。
"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, List

from .checker import AppReport

_CSS = """
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei",
       "Segoe UI", sans-serif; background:#f4f6fa; color:#1f2733; }
header { background:#14213d; color:#fff; padding:22px 32px; }
header h1 { margin:0 0 6px; font-size:22px; }
header .sub { color:#aebbd6; font-size:13px; }
.wrap { max-width:1180px; margin:0 auto; padding:24px 28px 60px; }
.cards { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }
.card { background:#fff; border-radius:10px; padding:16px 20px; min-width:150px;
        box-shadow:0 1px 3px rgba(20,33,61,.08); flex:1; }
.card .num { font-size:28px; font-weight:700; }
.card .lbl { color:#6b7689; font-size:12px; margin-top:4px; }
.card.good .num { color:#1a8f4d; }
.card.bad .num { color:#c0392b; }
.card.warn .num { color:#d97706; }
.tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:18px; }
.tab { padding:9px 16px; background:#e8ecf4; border-radius:8px 8px 0 0;
       cursor:pointer; font-size:13px; border:1px solid transparent;
       border-bottom:none; user-select:none; }
.tab.active { background:#fff; border-color:#d6dce8; font-weight:600; }
.tab .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
            margin-right:7px; }
.dot.r { background:#c0392b; } .dot.g { background:#1a8f4d; }
.panel { display:none; background:#fff; border:1px solid #d6dce8; border-radius:0 10px 10px 10px;
         padding:24px 26px; }
.panel.active { display:block; }
h2 { font-size:17px; margin:0 0 6px; }
h3 { font-size:14px; margin:22px 0 8px; color:#33405c; }
.desc { color:#5b6678; font-size:13px; line-height:1.7; background:#f7f9fc;
        border-left:3px solid #6c8cff; padding:10px 14px; border-radius:0 6px 6px 0; }
table { border-collapse:collapse; width:100%; font-size:12.5px; margin-top:8px; }
th,td { border:1px solid #e2e7f0; padding:7px 10px; text-align:left; }
th { background:#f0f3f9; font-weight:600; color:#44506b; }
tr:hover td { background:#fafbfe; }
.seq-table td.seq { color:#8a94a8; width:42px; text-align:right; font-variant-numeric: tabular-nums; }
.op { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px; }
.kind { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px;
        font-weight:600; }
.k-DATA { background:#e0edff; color:#1d56c2; }
.k-DIRENT { background:#f3e6ff; color:#7a2fc0; }
.k-SYNC { background:#d9f5e4; color:#127a3c; }
.k-ACK { background:#fff0d6; color:#b06b00; }
.barrier { height:5px; background:repeating-linear-gradient(45deg,#34c77b,#34c77b 6px,
           #b8ebcf 6px,#b8ebcf 12px); border-radius:3px; margin:2px 0; }
.fail { border:1px solid #f3c6c0; border-radius:10px; margin:14px 0; overflow:hidden; }
.fail .head { background:#fdecea; padding:11px 16px; font-weight:600; color:#922b21;
              font-size:13.5px; display:flex; justify-content:space-between; }
.fail .body { padding:14px 18px; }
.tagchip { display:inline-block; background:#c0392b; color:#fff; font-size:11px;
           padding:2px 9px; border-radius:11px; margin-right:6px; }
.advice { background:#f0f7ff; border:1px solid #c9ddff; border-radius:8px;
          padding:12px 16px; margin-top:10px; }
.advice li { margin:6px 0; line-height:1.65; font-size:13px; }
.miss { font-family:ui-monospace,Menlo,monospace; color:#922b21; font-size:12px; }
.disk { background:#1e2530; color:#d6e2f5; border-radius:8px; padding:12px 16px;
        font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
        white-space:pre; overflow-x:auto; margin-top:8px; }
.ok-banner { background:#e7f8ee; border:1px solid #a9e2c2; color:#146c3a;
             border-radius:10px; padding:16px 20px; font-weight:600; }
.src { font-size:11px; color:#7a8499; }
.pill { display:inline-block; padding:1px 8px; border-radius:9px; font-size:11px; }
.pill.exhaustive { background:#e6f0ff; color:#1d56c2; }
.pill.danger { background:#ffe8e6; color:#b3352a; }
.pill.sampled { background:#f0ecff; color:#5d3fb8; }
.pill.all-durable,.pill.all-lost { background:#eceff5; color:#55607a; }
.pill.naive-all,.pill.naive-barrier { background:#fff2d9; color:#9a6a12; }
.summary-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.muted { color:#8a94a8; font-size:12px; }
.legend span { margin-right:16px; font-size:12px; color:#55607a; }
"""

_JS = """
function showApp(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.app===name));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.id==='panel-'+name));
}
"""


def _kind_class(kind: str) -> str:
    from .model import DATA_OPS, DIRENT_OPS, SYNC_OPS, ACK
    if kind in DATA_OPS:
        return "k-DATA"
    if kind in DIRENT_OPS:
        return "k-DIRENT"
    if kind in SYNC_OPS:
        return "k-SYNC"
    return "k-ACK"


def _render_ops(ops: List[Dict[str, Any]]) -> str:
    rows = []
    for op in ops:
        barrier = '<div class="barrier" title="同步屏障: 此前同平面操作保证落盘"></div>' \
            if op["is_sync"] else ""
        rows.append(
            '<tr><td class="seq">%d</td><td><span class="kind %s">%s</span></td>'
            '<td class="op">%s</td><td>%s</td></tr>'
            % (op["seq"], _kind_class(op["kind"]), op["kind"],
               html.escape(op["text"]), barrier))
    return ('<table class="seq-table"><tr><th>#</th><th>类型</th><th>操作</th>'
            '<th style="width:120px">同步屏障</th></tr>%s</table>' % "".join(rows))


def _render_failure(f: Dict[str, Any], ops: List[Dict[str, Any]]) -> str:
    chips = "".join('<span class="tagchip">%s</span>' % html.escape(t)
                    for t in f["tags"])
    drop_text = ", ".join("#%d %s" % (s, ops[s]["text"]) for s in f["minimal_dropped"])
    keep_text = ", ".join("#%d" % s for s in f["minimal_replay"])
    disk = html.escape(json.dumps(f["disk_view"], ensure_ascii=False, indent=2))
    vios = "".join("<li>%s</li>" % html.escape(v["message"])
                   for v in f["violations"])
    advice = "".join("<li>%s</li>" % html.escape(a) for a in f["advice"])
    miss = "".join('<div class="miss">▸ %s</div>' % html.escape(m)
                   for m in f["missing_syncs"])
    return """
<div class="fail">
  <div class="head"><span>%s <span class="src">[崩溃点 #%d · 枚举来源: %s %s]</span></span>
       <span>最小丢失操作 %d 个</span></div>
  <div class="body">
    <h3 style="margin-top:0">触发的一致性违规</h3><ul>%s</ul>
    <h3>最小出问题的操作重排</h3>
    <table><tr><th>丢失(未落盘)的操作</th><td class="op">%s</td></tr>
           <tr><th>仍落盘的操作</th><td class="op">%s</td></tr></table>
    <h3>崩溃后磁盘上的实际内容</h3><div class="disk">%s</div>
    <h3>缺失的同步调用位置</h3>%s
    <div class="advice"><b>修复建议</b><ul>%s</ul></div>
  </div>
</div>""" % (chips, f["crash_at"], f["source"],
             " ".join(f.get("danger_tags", [])),
             len(f["minimal_dropped"]), vios,
             html.escape(drop_text) or "—", html.escape(keep_text),
             disk, miss or '<div class="muted">无</div>', advice)


def render(reports: List[AppReport], summary: Dict[str, Any],
           out_path: str) -> None:
    tabs, panels = [], []
    for r in reports:
        d = r.to_dict()
        has_fail = bool(d["failures"])
        dot = "r" if has_fail else "g"
        tabs.append('<div class="tab" data-app="%s" onclick="showApp(\'%s\')">'
                    '<span class="dot %s"></span>%s</div>'
                    % (d["app_name"], d["app_name"], dot, html.escape(d["title"])))
        if has_fail:
            fails = "".join(_render_failure(f, d["operations"])
                            for f in d["failures"])
        else:
            fails = ('<div class="ok-banner">✓ 枚举了 %d 个崩溃后磁盘状态, '
                     '恢复逻辑在所有状态下都满足应用声明的一致性条件, 零误报。</div>'
                     % d["states_explored"])
        src_line = " · ".join("%s=%d" % (k, v)
                              for k, v in sorted(d["sources"].items()))
        panels.append("""
<div class="panel" id="panel-%s">
  <h2>%s</h2>
  <p class="desc">%s</p>
  <div class="cards">
    <div class="card %s"><div class="num">%d</div><div class="lbl">探索的崩溃状态数</div></div>
    <div class="card"><div class="num">%.1f ms</div><div class="lbl">检测耗时</div></div>
    <div class="card %s"><div class="num">%d</div><div class="lbl">发现缺陷类别</div></div>
    <div class="card"><div class="num">%d</div><div class="lbl">朴素边界法状态数</div></div>
    <div class="card %s"><div class="num">%d</div><div class="lbl">朴素边界法发现数</div></div>
  </div>
  <p class="muted">枚举来源分布: %s%s</p>
  <h3>操作序列与同步点</h3>
  <p class="legend">
    <span><span class="kind k-DATA">数据面</span> write/append/truncate</span>
    <span><span class="kind k-DIRENT">目录面</span> create/rename/delete</span>
    <span><span class="kind k-SYNC">同步</span> fsync/fsyncdir</span>
    <span><span class="kind k-ACK">确认</span> 应用 ack</span></p>
  %s
  <h3>出问题的重排与修复建议</h3>%s
</div>""" % (
            d["app_name"], html.escape(d["title"]), html.escape(d["description"]),
            "", d["states_explored"], d["elapsed_ms"],
            "bad" if has_fail else "good", len(d["failures"]),
            d["naive_states"],
            "bad" if d["naive_failures"] else "good",
            len({v["tag"] for f in d["naive_failures"] for v in f["violations"]}),
            html.escape(src_line),
            (" · 预算剪枝跳过组合 %d 个" % d["skipped_by_budget"])
            if d["skipped_by_budget"] else "",
            _render_ops(d["operations"]), fails))

    # 总览表
    rows = []
    for row in summary["per_app"]:
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>%d</td>"
            "<td>%d</td><td>%d</td><td>%d</td><td>%.1f ms</td><td>%s</td></tr>"
            % (html.escape(row["title"]), html.escape(row["pattern"]),
               row["known"], row["found"], row["missed"],
               row["false_positives"], row["states"], row["naive_states"],
               row["elapsed_ms"],
               '<span style="color:#1a8f4d;font-weight:600">%d%%</span>'
               % row["detection_rate"] if row["known"] else
               '<span class="muted">无预埋缺陷</span>'))
    overview = """
<div class="panel active" id="panel-overview">
  <h2>验收总览</h2>
  <div class="cards">
    <div class="card %s"><div class="num">%d/%d</div><div class="lbl">预埋缺陷检出(检出率 %d%%)</div></div>
    <div class="card good"><div class="num">%d</div><div class="lbl">正确写法误报数(要求为 0)</div></div>
    <div class="card warn"><div class="num">%d</div><div class="lbl">朴素边界断电法漏检数</div></div>
    <div class="card"><div class="num">%d</div><div class="lbl">总崩溃状态数</div></div>
    <div class="card"><div class="num">%.1f ms</div><div class="lbl">总耗时</div></div>
  </div>
  <table><tr><th>被测应用</th><th>更新模式</th><th>预埋缺陷</th><th>检出</th><th>漏检</th>
    <th>误报</th><th>本工具状态数</th><th>朴素法状态数</th><th>耗时</th><th>检出率</th></tr>%s</table>
  <p class="desc" style="margin-top:16px">朴素方法只在操作边界处断电、不考虑数据面与目录面之间的重排,
  因此无法发现"rename 先于数据落盘"和"WAL COMMIT 晚于主文件数据落盘"两类跨平面缺陷;
  本工具通过组间独立持久化枚举 + 危险重排优先覆盖这些场景。</p>
</div>""" % (
        "good" if summary["total_missed"] == 0 else "bad",
        summary["total_found"], summary["total_known"],
        summary["detection_rate"], summary["false_positives"],
        summary["naive_missed"], summary["total_states"],
        summary["total_elapsed_ms"], "".join(rows))

    doc = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>应用文件更新崩溃一致性检测报告</title><style>%s</style></head><body>
<header><h1>应用文件更新 · 崩溃一致性检测报告</h1>
<div class="sub">仿真文件系统(页缓存/数据面-目录面分离/fsync 屏障语义) · 崩溃状态枚举 · 恢复逻辑校验 · delta-debug 最小重排</div>
</header><div class="wrap">
<div class="tabs"><div class="tab active" data-app="overview"
onclick="showApp('overview')"><span class="dot g"></span>验收总览</div>%s</div>
%s%s
</div><script>%s</script></body></html>""" % (
        _CSS, "".join(tabs), overview, "".join(panels), _JS)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
