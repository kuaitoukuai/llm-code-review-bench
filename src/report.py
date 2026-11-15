"""大模型代码检查测评系统 · 报告生成

产出三样东西：
    report.md    人读
    scores.csv   进 Excel
    scatter.html 能力×成本 散点（纯 SVG，无外部依赖）
"""
from __future__ import annotations

import csv

from core import REPORTS_DIR, ensure_run, now_iso, read_json, read_jsonl, write_json


def _fmt(value, digits=1, suffix=""):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def build_report(run_id: str) -> dict:
    base = ensure_run(run_id)
    payload = read_json(base / "scores.json")
    if not payload:
        raise RuntimeError("还没有 scores.json，先跑 score")

    run_meta = read_json(base / "run.json", {}) or {}
    verdicts = read_jsonl(base / "judgments" / "verdicts.jsonl")
    queue = read_jsonl(base / "judgments" / "queue.jsonl")
    rows = payload["rows"]

    out_dir = REPORTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_csv(out_dir / "scores.csv", rows)
    _write_scatter(out_dir / "scatter.html", rows, payload)
    (out_dir / "report.md").write_text(
        _render_md(run_id, run_meta, payload, rows, verdicts, queue), encoding="utf-8"
    )
    write_json(out_dir / "summary.json", {
        "run_id": run_id,
        "generated_at": now_iso(),
        "rows": rows,
        "verdict_stats": _verdict_stats(verdicts),
    })
    return {"dir": str(out_dir), "rows": len(rows)}


def _verdict_stats(verdicts: list[dict]) -> dict:
    stats = {"valid": 0, "invalid": 0, "unsure": 0}
    by_conf: dict[str, int] = {}
    for v in verdicts:
        stats[v["verdict"]] = stats.get(v["verdict"], 0) + 1
        by_conf[v["confidence"]] = by_conf.get(v["confidence"], 0) + 1
    return {"verdict": stats, "confidence": by_conf, "total": len(verdicts)}


def _write_csv(path, rows):
    fields = [
        "rank", "candidate", "vendor", "final_score", "raw_score", "hits",
        "false_positives", "unsure", "unique_hits", "precision", "coverage_proxy",
        "P0", "P1", "P2", "P3", "claims", "tasks",
        "avg_duration_sec", "overtime_count", "token_in", "token_out",
        "time_factor", "shortfall_penalty",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _render_md(run_id, run_meta, payload, rows, verdicts, queue) -> str:
    stats = _verdict_stats(verdicts)
    tasks = payload.get("tasks", [])
    lines = [
        f"# 代码检查能力测评报告　`{run_id}`",
        "",
        f"- 生成时间：{now_iso()}",
        f"- 系统版本：{run_meta.get('system_version', '—')}　"
        f"提示词：{run_meta.get('prompt_version', '—')}　"
        f"评分口径：{run_meta.get('scoring_version', '—')}",
        f"- 题目数：{len(tasks)}　候选数：{len(rows)}",
        f"- 裁决分布：成立 {stats['verdict'].get('valid', 0)}　"
        f"不成立 {stats['verdict'].get('invalid', 0)}　"
        f"存疑 {stats['verdict'].get('unsure', 0)}",
        "",
        "> 分数只在本 run 内可比。换题、换提示词、换评分口径后不要跨 run 直接比排名，",
        "> 但同一批裁决数据可以用新版 scoring 重算——原始层不动，计分层可重跑。",
        "",
        "## 一、排行榜",
        "",
        "| 名次 | 候选 | 终分 | 命中 | 误报 | 独到 | 精确率 | 覆盖度 | 平均耗时 | 时间系数 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['rank']} | `{row['candidate']}` | **{_fmt(row['final_score'], 2)}** | "
            f"{row['hits']} | {row['false_positives']} | {row['unique_hits']} | "
            f"{_fmt(row['precision'], 2)} | {_fmt(row['coverage_proxy'], 2)} | "
            f"{_fmt(row['avg_duration_sec'], 0, ' s')} | {_fmt(row['time_factor'], 3)} |"
        )

    lines += [
        "",
        "## 二、分项明细",
        "",
        "| 候选 | P0 | P1 | P2 | P3 | 报出总数 | 输入 token | 输出 token | 超时次数 | 短缺扣分 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['candidate']}` | {row['P0']} | {row['P1']} | {row['P2']} | {row['P3']} | "
            f"{row['claims']} | {_fmt(row['token_in'])} | {_fmt(row['token_out'])} | "
            f"{row.get('overtime_count', 0)} | {_fmt(row['shortfall_penalty'], 2)} |"
        )

    lines += [
        "",
        "## 三、分数构成",
        "",
        "```",
        "原始分 = Σ(命中 × 严重度权重) − 误报 × 扣分 + 独到命中 × 加成",
        "终　分 = 原始分 × 时间系数",
        "```",
        "",
        "严重度权重：P0=5　P1=3　P2=2　P3=1。独到命中指只有一家提出且被判为真的条目，",
        "额外按权重 ×1 再加一次（即总权重翻倍）。误报每条扣 0.5。",
        "",
        "## 四、裁决状态",
        "",
        "| 来源 | 条数 | 含义 |",
        "|---|---|---|",
    ]
    conf = stats["confidence"]
    lines += [
        f"| 人工 | {conf.get('human', 0)} | 你看过的，优先级最高 |",
        f"| 执行结果 | {conf.get('execution', 0)} | 复现测试实际运行后的结论 |",
        f"| 执行冲突 | {conf.get('execution_split', 0)} | 同一问题同时出现复现与未复现，按存疑处理 |",
        f"| 模型票 | {conf.get('panel', 0)} | 跨族裁判多数票 |",
        f"| 票型分裂 | {conf.get('panel_split', 0)} | 裁判意见不一致，按存疑处理 |",
        f"| 共识推断 | {conf.get('consensus', 0)} | 多家提出但没跑裁判，属弱证据 |",
        f"| 待定 | {conf.get('pending', 0)} | 没有可用结论，进人工队列 |",
        "",
    ]
    cn = ["一", "二", "三", "四", "五", "六", "七", "八"]
    section = 4
    if queue:
        section += 1
        lines += [
            f"## {cn[section - 1]}、待人工复核（{len(queue)} 条）",
            "",
            "详见 `judgments/queue.md`。填完 `judgments/human.jsonl` 后重跑 verdicts 与 score，",
            "全部分数会自动更新，不需要重跑模型。",
            "",
            "按严重度分布：",
            "",
        ]
        dist: dict[str, int] = {}
        for item in queue:
            dist[item["severity"]] = dist.get(item["severity"], 0) + 1
        for sev in ("P0", "P1", "P2", "P3"):
            if dist.get(sev):
                lines.append(f"- {sev}：{dist[sev]} 条")
        lines.append("")

    section += 1
    lines += [
        f"## {cn[section - 1]}、读这份报告时要注意",
        "",
        "1. **分数只在本 run 内可比。** 题目集变了、提示词改了，分数就不可横向比较。",
        "2. **共识推断是弱证据。** 多家都说同一件事也可能是同一个幻觉模式，"
        "真正可靠的只有跑得出复现的那部分。",
        "3. **产品与模型未分离。** 候选写的是 `产品/模型` 全称，测出来的是组合能力，"
        "不能直接说成某个模型强。",
        "4. **题库规模决定分辨率。** 题目越少，能分辨的差距越大；几条题目的排名不要当真。",
        "5. **重复次数。** 同一候选同一题建议至少跑 2 次，只跑 1 次时把排名差异当作噪声看待。",
        "",
    ]
    return "\n".join(lines)


def _write_scatter(path, rows, payload):
    points = [r for r in rows if r.get("avg_duration_sec")]
    if not points:
        path.write_text("<html><body><p>没有耗时数据，无法绘制散点图。</p></body></html>", encoding="utf-8")
        return

    width, height = 760, 440
    left, right, top, bottom = 70, 30, 40, 60
    xs = [r["avg_duration_sec"] for r in points]
    ys = [r["final_score"] for r in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_max == x_min:
        x_max += 1
    if y_max == y_min:
        y_max += 1
    pad_x = (x_max - x_min) * 0.12 or 1
    pad_y = (y_max - y_min) * 0.12 or 1
    x_min, x_max = max(0, x_min - pad_x), x_max + pad_x
    y_min, y_max = y_min - pad_y, y_max + pad_y

    def sx(v):
        return left + (v - x_min) / (x_max - x_min) * (width - left - right)

    def sy(v):
        return height - bottom - (v - y_min) / (y_max - y_min) * (height - top - bottom)

    plot_w, plot_h = width - left - right, height - top - bottom
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg" '
        f'font-family="Microsoft YaHei, sans-serif">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#fafafa" stroke="#e0e0e0"/>',
    ]
    for i in range(6):
        gx = left + plot_w * i / 5
        gy = top + plot_h * i / 5
        parts.append(f'<line x1="{gx:.0f}" y1="{top}" x2="{gx:.0f}" y2="{height-bottom}" stroke="#eeeeee"/>')
        parts.append(f'<line x1="{left}" y1="{gy:.0f}" x2="{width-right}" y2="{gy:.0f}" stroke="#eeeeee"/>')
    parts += [
        f'<text x="{left + plot_w/2:.0f}" y="{height-14}" font-size="13" fill="#616161" text-anchor="middle">平均耗时（秒）→ 越左越好</text>',
        f'<text x="18" y="{top + plot_h/2:.0f}" font-size="13" fill="#616161" text-anchor="middle" transform="rotate(-90 18 {top + plot_h/2:.0f})">终分（越高越好）</text>',
    ]
    colors = ["#1565C0", "#00897B", "#E53935", "#7B1FA2", "#F9A825", "#0288D1", "#5F5E5A"]
    for i, row in enumerate(sorted(points, key=lambda r: r["rank"])):
        cx, cy = sx(row["avg_duration_sec"]), sy(row["final_score"])
        color = colors[i % len(colors)]
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="{color}" fill-opacity="0.85"/>')
        label = row["candidate"].split("/")[-1]
        # 靠右的点把标签放左边，避免溢出画布
        if cx > width - right - 150:
            parts.append(
                f'<text x="{cx - 11:.1f}" y="{cy + 4:.1f}" font-size="12" fill="#212121" '
                f'text-anchor="end">{label}</text>'
            )
        else:
            parts.append(
                f'<text x="{cx + 11:.1f}" y="{cy + 4:.1f}" font-size="12" fill="#212121">{label}</text>'
            )
    parts.append("</svg>")

    table_rows = []
    for row in rows:
        prec = "—" if row.get("precision") is None else f"{row['precision']:.2f}"
        dur = "—" if not row.get("avg_duration_sec") else f"{row['avg_duration_sec']:.0f} s"
        table_rows.append(
            f"<tr><td>{row['rank']}</td><td>{row['candidate']}</td>"
            f"<td>{row['final_score']:.2f}</td><td>{dur}</td><td>{prec}</td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>能力 × 成本 散点</title>
<style>
body{{margin:0;padding:28px;background:#fff;font-family:"Microsoft YaHei",sans-serif;color:#212121}}
h1{{font-size:17px;font-weight:600;margin:0 0 6px}}
p.sub{{font-size:13px;color:#616161;margin:0 0 18px;line-height:1.7}}
.box{{max-width:820px}}
table{{border-collapse:collapse;width:100%;margin-top:20px;font-size:13px}}
th,td{{border-bottom:1px solid #e0e0e0;padding:7px 8px;text-align:left}}
th{{background:#f5f7fa;font-weight:600;color:#1a237e}}
</style></head>
<body><div class="box">
<h1>能力 × 成本</h1>
<p class="sub">横轴是平均单题耗时，纵轴是终分。左上角是最优区。
耗时未经归一化，不同产品的上下文注入量不同，这一轴只作参考，不参与排名。</p>
{''.join(parts)}
<table><thead><tr><th>名次</th><th>候选</th><th>终分</th><th>平均耗时</th><th>精确率</th></tr></thead><tbody>
{''.join(table_rows)}
</tbody></table>
</div></body></html>"""
    path.write_text(html, encoding="utf-8")
