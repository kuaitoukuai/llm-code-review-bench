"""大模型代码检查测评系统 · 命令行入口

用法总览：
    python src/cli.py demo                     # 一键跑通示例
    python src/cli.py new-run r01              # 开一次评测
    python src/cli.py task t05-shadow          # 把项目冻成题库
    python src/cli.py start r01 <cand> <task>  # 开始计时（打开产品前提）
    python src/cli.py done  r01 <cand> <task> --from out.md --token-in 40000
    python src/cli.py all r01 --allow-untrusted # 归一化→聚类→执行→裁决→计分→报告
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
import pipeline
import report
from core import DEMO_DIR, REPORTS_DIR, RUNS_DIR, TASKS_DIR, EvalError


def _ok(msg):
    print(f"  ✓ {msg}")


def _head(msg):
    print(f"\n== {msg}")


# ---------------------------------------------------------------- 命令


def cmd_new_run(args):
    rid = pipeline.new_run(args.id, args.note or "")
    _ok(f"已开 run：{rid}")
    print(f"    目录：{RUNS_DIR / rid}")
    print(f"    下一步：python src/cli.py task <project-id>")


def cmd_register_candidate(args):
    item, created = pipeline.register_candidate(args.id, args.product, args.model, args.vendor)
    _ok(f"{'已登记' if created else '已存在'}候选：{item['id']}（vendor={item['vendor']}）")


def cmd_prepare(args):
    manifest = pipeline.prepare_tasks(args.expected)
    _ok(f"已准备 {manifest['count']} 个任务")
    for row in manifest["tasks"]:
        print(f"    {row['task_id']:<24} {row['file_count']} 文件 / "
              f"{row['total_lines']} 行 / {'新建' if row['status'] == 'built' else '复用'}")
    print(f"    清单：{TASKS_DIR / 'batch.json'}")


def cmd_task(args):
    tid = pipeline.build_task(args.project, args.task_id, args.force)
    meta = json.loads((TASKS_DIR / tid / "meta.json").read_text(encoding="utf-8"))
    _ok(f"题库已生成：{tid}")
    print(f"    文件 {meta['file_count']} 个，共 {meta['total_lines']} 行，prompt {meta['prompt_chars']} 字符")
    print(f"    复制这个文件的全文去问模型：{TASKS_DIR / tid / 'prompt.md'}")


def cmd_start(args):
    sess = pipeline.session_start(args.run, args.candidate, args.task, args.repeat)
    _ok(f"计时开始：{args.candidate} / {args.task} / 第 {args.repeat} 次")
    print(f"    现在打开产品，把 prompt.md 全文贴进去。回来跑 done 收工。")


def cmd_done(args):
    meta = pipeline.session_done(
        args.run, args.candidate, args.task,
        Path(args.source) if args.source else None,
        repeat=args.repeat, token_in=args.token_in, token_out=args.token_out,
        duration=args.duration, note=args.note or "",
    )
    _ok(f"已登记：{meta['candidate']} / {meta['task']} / 第 {meta['repeat']} 次")
    print(f"    耗时 {meta['duration_sec']} 秒，回复 {meta['reply_chars']} 字符")
    if meta.get("token_in") is None and meta.get("token_out") is None:
        print("    提示：token 没记，散点图仍可画，但成本对比会缺失")


def cmd_normalize(args):
    stats = pipeline.normalize(args.run)
    _ok("归一化完成")
    print(f"    回复 {stats['replies']} 份 → 合规发现 {stats['claims']} 条，"
          f"复现测试 {stats['reproductions']} 个")
    if stats["rejected"]:
        print(f"    拒收 {stats['rejected']} 条（字段缺失/超字数），详见 normalized/_issues.jsonl")
    if stats["parse_error"]:
        print(f"    解析失败 {stats['parse_error']} 份（模型没按 JSON 输出）")


def cmd_cluster(args):
    stats = pipeline.cluster(args.run)
    _ok("聚类完成")
    print(f"    发现 {stats['claims']} 条 → 去重后 {stats['clusters']} 处问题")
    print(f"    参与厂商 {stats['vendors']} 家，其中只有一家提出的 {stats['unique_clusters']} 处")
    print(f"    查看：runs/{args.run}/claims/clusters.jsonl")


def cmd_execute(args):
    stats = pipeline.execute_reproductions(args.run, args.allow_untrusted)
    if not stats["total"]:
        _ok("没有复现测试，跳过执行")
        return
    _ok(f"执行 {stats['total']} 个复现测试")
    print(f"    复现 {stats['reproduced']}　未复现 {stats['not_reproduced']}　"
          f"错误 {stats['error']}　超时 {stats['timeout']}")
    print(f"    结果：runs/{args.run}/executions/results.jsonl")


def cmd_packets(args):
    ids = args.only.split(",") if args.only else None
    n = pipeline.make_packets(args.run, ids)
    _ok(f"生成裁判包 {n} 个")
    print(f"    目录：runs/{args.run}/judgments/packets/")
    print("    每个包里已经装好 claim 与上下文代码，不含来源信息。拿去问裁判模型。")


def cmd_votes(args):
    n = pipeline.collect_votes(args.run, args.judge, Path(args.dir))
    _ok(f"收到 {args.judge} 的 {n} 张票")


def cmd_verdicts(args):
    stats = pipeline.adjudicate(args.run)
    _ok("裁决完成")
    print(f"    问题总数 {stats['total_clusters']}")
    print(f"    人工 {stats['human']}　执行 {stats['execution']}　模型票 {stats['panel']}　"
          f"共识 {stats['consensus']}　待定 {stats['pending']}")
    if stats["execution_split"]:
        print(f"    执行结果冲突 {stats['execution_split']} 条，按存疑处理")
    if stats["queue"]:
        print(f"    待人工复核 {stats['queue']} 条 → runs/{args.run}/judgments/queue.md")


def cmd_score(args):
    result = pipeline.score(args.run)
    _ok("计分完成")
    for row in result["rows"]:
        print(f"    {row['rank']}. {row['candidate']:<34} 终分 {row['final_score']:>7.2f}　"
              f"命中 {row['hits']:>2}　误报 {row['false_positives']:>2}　独到 {row['unique_hits']:>2}")


def cmd_report(args):
    info = report.build_report(args.run)
    _ok(f"报告已生成：{info['dir']}")
    print(f"    report.md　scores.csv　scatter.html")


def cmd_all(args):
    _head("归一化")
    cmd_normalize(args)
    _head("聚类")
    cmd_cluster(args)
    _head("执行复现")
    origin = core.read_jsonl(RUNS_DIR / args.run / "claims" / "origin.jsonl")
    if any(item.get("reproduction") for item in origin) and not args.allow_untrusted:
        print("    发现模型生成的测试。先审查 runs/<run>/reproductions，再用 --allow-untrusted 执行；本轮已跳过。")
    else:
        cmd_execute(args)
    _head("裁决")
    cmd_verdicts(args)
    _head("计分")
    cmd_score(args)
    _head("报告")
    cmd_report(args)


def cmd_status(args):
    base = core.ensure_run(args.run)
    run_meta = core.read_json(base / "run.json", {}) or {}
    _head(f"run {args.run}")
    print(f"    建立于 {run_meta.get('created_at', '—')}")
    raw = list((base / "raw").rglob("r*.md")) if (base / "raw").exists() else []
    norm = list((base / "normalized").rglob("r*.jsonl")) if (base / "normalized").exists() else []
    norm = [p for p in norm if not p.name.startswith("_")]
    claims = core.read_jsonl(base / "claims" / "pool.jsonl")
    clusters = core.read_jsonl(base / "claims" / "clusters.jsonl")
    verdicts = core.read_jsonl(base / "judgments" / "verdicts.jsonl")
    queue = core.read_jsonl(base / "judgments" / "queue.jsonl")
    executions = core.read_jsonl(base / "executions" / "results.jsonl")
    scores = core.read_jsonl(base / "scores.jsonl")
    reproductions = sum(1 for item in core.read_jsonl(base / "claims" / "origin.jsonl")
                        if item.get("reproduction"))
    print(f"    原始作答 {len(raw)} 份")
    print(f"    已归一化 {len(norm)} 份 → {len(claims)} 条发现，复现测试 {reproductions} 个")
    print(f"    聚类后 {len(clusters)} 处问题，已执行 {len(executions)} 个测试")
    print(f"    已裁决 {len(verdicts)} 处，待人工 {len(queue)} 条")
    print(f"    已计分 {len(scores)} 个候选")
    if scores and not (REPORTS_DIR / args.run / "report.md").exists():
        print(f"    提示：还没出报告，跑 report {args.run}")
    report_dir = REPORTS_DIR / args.run / "report.md"
    if report_dir.exists():
        print(f"    报告：{report_dir}")


def cmd_scan(args):
    root = Path(args.path)
    if not root.exists():
        raise EvalError(f"路径不存在：{root}")
    _head(f"扫描 {root}")
    rows = pipeline.scan_projects(root, args.depth)
    if not rows:
        print("    没找到像项目的文件夹。可加大 --depth，或直接手写 config/projects.toml")
        return
    print(f"    找到 {len(rows)} 个候选，按适合做题库的程度排序：\n")
    print(f"    {'推荐':<4}{'项目':<28}{'代码文件':>8}{'约行数':>9}{'测试':>5}  说明")
    print("    " + "-" * 78)
    for row in rows:
        print(f"    {row['score']:>3} {row['name'][:26]:<27}{row['code_files']:>6}"
              f"{row['approx_lines']:>9}{row['test_files']:>5}  {','.join(row['reasons'])}")

    out = core.CONFIG_DIR / "projects.suggested.toml"
    lines = ["# 由 scan 生成的候选清单。挑需要的复制进 projects.toml", ""]
    for row in rows:
        lines += [
            "[[project]]",
            f'id = "{row["name"]}"',
            f'name = "{row["name"]}"',
            f'path = "{row["path"].replace(chr(92), "/")}"',
            'include = ["**/*.py", "**/*.ts", "**/*.tsx"]',
            'exclude = ["**/node_modules/**", "**/__pycache__/**", "**/*.test.*"]',
            "max_files = 8",
            "max_lines_per_file = 400",
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    _ok(f"已写出建议配置：{out}")
    print("    挑前 3 个复制进 config/projects.toml 即可开跑，不要一次全上。")


# ---------------------------------------------------------------- demo

def cmd_demo(args):
    rid = "demo-001"
    _head("清理旧的 demo")
    for target in (RUNS_DIR / rid, TASKS_DIR / "demo-project", REPORTS_DIR / rid):
        if target.exists():
            shutil.rmtree(target)
    _ok("已清空")

    _head("造示例项目")
    project_dir = DEMO_DIR / "demo-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "order_service.py").write_text(_DEMO_ORDER, encoding="utf-8")
    (project_dir / "text_utils.py").write_text(_DEMO_UTILS, encoding="utf-8")
    _ok(f"{project_dir} 已写入 2 个文件，内含 4 个真实缺陷")

    _head("建题库")
    tid = pipeline.build_task("demo-project", "demo-project", force=True)
    _ok(f"任务 {tid} 已生成")

    _head("模拟三次作答")
    cands = ["kimicode/kimi-k2.5", "workbuddy/claude-sonnet-5.5", "workbuddy/deepseek-v4.1-flash"]
    samples = [
        (_SAMPLE_A, 412.5, 38200, 2100),
        (_SAMPLE_B, 638.0, 41500, 2650),
        (_SAMPLE_C, 3000.0, 44100, 3300),
    ]
    rid = pipeline.new_run(rid, "内置演示数据")
    for cand, (text, dur, tin, tout) in zip(cands, samples):
        pipeline.session_start(rid, cand, tid, 1)
        d = RUNS_DIR / rid / "raw" / cand / tid
        (d / "r1.md").write_text(text, encoding="utf-8")
        pipeline.session_done(rid, cand, tid, None, repeat=1,
                              token_in=tin, token_out=tout, duration=dur, note="demo")
        _ok(f"{cand} 已登记（{dur:.0f}s，{tin}+{tout} token）")

    args.run = rid
    _head("归一化")
    cmd_normalize(args)
    _head("聚类")
    cmd_cluster(args)

    _head("执行复现")
    stats = pipeline.execute_reproductions(rid, allow_untrusted=True)
    _ok(f"执行 {stats['total']} 个内置复现测试：复现 {stats['reproduced']}，"
        f"未复现 {stats['not_reproduced']}，错误 {stats['error']}，超时 {stats['timeout']}")

    _head("模拟人工裁决")
    clusters = core.read_jsonl(RUNS_DIR / rid / "claims" / "clusters.jsonl")
    human = []
    for item in clusters:
        key = (item["file"], item["line"])
        if key in _DEMO_VERDICTS and key != ("order_service.py", 14):
            verdict, reason = _DEMO_VERDICTS[key]
            human.append({"cluster_id": item["cluster_id"], "verdict": verdict,
                          "reason": reason, "source": "demo-preset"})
    core.write_jsonl(RUNS_DIR / rid / "judgments" / "human.jsonl", human)
    _ok(f"已写入 {len(human)} 条预设裁决；除零问题留给执行通道判定")

    _head("裁决")
    cmd_verdicts(args)
    _head("计分")
    cmd_score(args)
    _head("报告")
    cmd_report(args)

    _head("演示完成")
    print(f"    报告：{REPORTS_DIR / rid / 'report.md'}")
    print(f"    散点：{REPORTS_DIR / rid / 'scatter.html'}")
    print(f"    数据：{RUNS_DIR / rid}")
    print("\n    这三份模拟数据各有特点：")
    print("      · kimi 报得少但准，命中两个真 bug，其中一个别人没提（拿到独到加成）")
    print("      · claude 命中两个，但夹了一条命名风格当缺陷（扣误报分）")
    print("      · deepseek 报得最多、命中最多，也误报最多，还慢了一个数量级（吃时间系数）")
    print("    看 report.md 里的分项，能对上这三句话。")


_DEMO_ORDER = '''"""订单结算服务（示例，故意留了 4 个缺陷）"""

from dataclasses import dataclass, field


@dataclass
class Order:
    items: list = field(default_factory=list)
    total: float = 0.0


def average_price(items):
    total = sum(it["price"] for it in items)
    return total / len(items)


def first_item_name(order):
    return order.items[0]["name"]


def add_item(order, name, price, qty):
    order.items.append({"name": name, "price": price, "qty": qty})
    order.total += price * qty
    return order


def is_free(order):
    return order.total == 0.0


def settle(order):
    name = first_item_name(order)
    avg = average_price(order.items)
    return {"first": name, "avg": avg, "free": is_free(order)}


def refund(order):
    return order.total * -1
'''

_DEMO_UTILS = '''"""文本处理工具（示例）"""


def split_sections(text):
    parts = text.split("\\n## ")
    return [parts[0]] + ["## " + p for p in parts[1:]]


def safe_int(value, default=0):
    try:
        return int(value)
    except ValueError:
        return default


def join_lines(lines, sep="\\n"):
    return sep.join(str(x).strip() for x in lines if x is not None)


def truncate(text, limit=100):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
'''

_SAMPLE_A = r'''```json
[
  {
    "file": "order_service.py",
    "line": 14,
    "severity": "P1",
    "category": "correctness",
    "title": "空列表时除以零",
    "trigger": "order.items 为空时调用 average_price，len(items) 为 0",
    "impact": "抛 ZeroDivisionError，结算接口整体失败",
    "verify_hint": "传入空 items 调用 average_price",
    "reproduction": {
      "filename": "test_average_price.py",
      "content": "import unittest\nfrom order_service import average_price\n\nclass Reproduction(unittest.TestCase):\n    def test_empty_items_return_zero(self):\n        self.assertEqual(average_price([]), 0)\n"
    }
  },
  {
    "file": "order_service.py",
    "line": 18,
    "severity": "P1",
    "category": "correctness",
    "title": "未判空直接取首个元素",
    "trigger": "order.items 为空列表时调用 first_item_name",
    "impact": "抛 IndexError，settle 中断",
    "verify_hint": "new Order() 后直接调 first_item_name"
  },
  {
    "file": "order_service.py",
    "line": 22,
    "severity": "P2",
    "category": "reliability",
    "title": "数量未校验负数",
    "trigger": "qty 传负数时，total 会被扣减",
    "impact": "订单总额可能变成负数，账目失真",
    "verify_hint": "add_item(order, 'x', 10, -5) 后看 total"
  }
]
```'''

_SAMPLE_B = '''```json
[
  {
    "file": "order_service.py",
    "line": 15,
    "severity": "P1",
    "category": "correctness",
    "title": "平均价格在空列表上除零",
    "trigger": "items 为空时 sum 为 0，len 也为 0",
    "impact": "ZeroDivisionError 导致结算崩溃",
    "verify_hint": "average_price([]) 直接调用"
  },
  {
    "file": "order_service.py",
    "line": 28,
    "severity": "P2",
    "category": "correctness",
    "title": "浮点数用等于号比较",
    "trigger": "total 由多次浮点累加得到，可能不精确等于 0.0",
    "impact": "免单判断失效，本该免单的订单仍然收钱",
    "verify_hint": "累加 0.1+0.2-0.3 后判断 is_free"
  },
  {
    "file": "text_utils.py",
    "line": 15,
    "severity": "P3",
    "category": "maintainability",
    "title": "变量命名过短不易读",
    "trigger": "阅读 join_lines 时难以理解 x 的语义",
    "impact": "增加维护成本",
    "verify_hint": "人工阅读"
  }
]
```'''

_SAMPLE_C = '''```json
[
  {
    "file": "order_service.py",
    "line": 14,
    "severity": "P1",
    "category": "correctness",
    "title": "average_price 未防空列表",
    "trigger": "order.items 为空时 len(items)=0，触发除零",
    "impact": "ZeroDivisionError，整个 settle 流程抛异常",
    "verify_hint": "构造空订单后调用 settle"
  },
  {
    "file": "order_service.py",
    "line": 18,
    "severity": "P1",
    "category": "correctness",
    "title": "first_item_name 直接下标取元素",
    "trigger": "items 为空时 order.items[0] 越界",
    "impact": "IndexError，且发生在 settle 第一步",
    "verify_hint": "空订单调用 settle"
  },
  {
    "file": "order_service.py",
    "line": 28,
    "severity": "P2",
    "category": "correctness",
    "title": "浮点相等比较不可靠",
    "trigger": "total 为浮点累加结果，== 0.0 判断不稳",
    "impact": "免单条件误判",
    "verify_hint": "用 0.1+0.2-0.3 累加后判断"
  },
  {
    "file": "order_service.py",
    "line": 33,
    "severity": "P0",
    "category": "security",
    "title": "settle 未做权限校验",
    "trigger": "任意调用方可直接结算他人订单",
    "impact": "越权操作，可能导致资金损失",
    "verify_hint": "用其他用户身份调用 settle"
  },
  {
    "file": "text_utils.py",
    "line": 4,
    "severity": "P2",
    "category": "reliability",
    "title": "split 结果首元素可能为空",
    "trigger": "文本以 ## 开头时 parts[0] 是空串",
    "impact": "返回列表含空字符串，下游解析出错",
    "verify_hint": "传入以 ## 开头的文本"
  },
  {
    "file": "order_service.py",
    "line": 8,
    "severity": "P3",
    "category": "performance",
    "title": "dataclass 默认值建议用 tuple",
    "trigger": "无",
    "impact": "性能影响可忽略",
    "verify_hint": "无"
  }
]
```'''


_DEMO_VERDICTS = {
    ("order_service.py", 8): ("invalid", "属性写法建议，不是缺陷"),
    ("order_service.py", 14): ("valid", "空列表时 len 为 0，除零成立"),
    ("order_service.py", 18): ("valid", "空列表时下标越界，成立"),
    ("order_service.py", 22): ("valid", "qty 为负会让总额变小，成立"),
    ("order_service.py", 28): ("valid", "浮点累加后等值比较不可靠，成立"),
    ("order_service.py", 33): ("invalid", "示例代码没有权限模型，该声明不成立"),
    ("text_utils.py", 4): ("valid", "以 ## 开头时首元素是空串，成立"),
    ("text_utils.py", 15): ("invalid", "命名偏好，不构成缺陷"),
}


# ---------------------------------------------------------------- 入口

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="eval", description="大模型代码检查测评系统 v0.3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("new-run", help="开一次评测")
    p.add_argument("id", nargs="?")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_new_run)

    p = sub.add_parser("register-candidate", help="登记当前评测模型")
    p.add_argument("id", help="产品/模型，只含字母数字及 ._-")
    p.add_argument("--product", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--vendor", required=True)
    p.set_defaults(func=cmd_register_candidate)

    p = sub.add_parser("prepare", help="批量冻结启用的项目")
    p.add_argument("--expected", type=int, default=0)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("task", help="把项目冻成题库")
    p.add_argument("project")
    p.add_argument("--task-id")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("start", help="开始计时")
    p.add_argument("run"); p.add_argument("candidate"); p.add_argument("task")
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("done", help="收工登记")
    p.add_argument("run"); p.add_argument("candidate"); p.add_argument("task")
    p.add_argument("--from", dest="source")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--token-in", type=int)
    p.add_argument("--token-out", type=int)
    p.add_argument("--duration", type=float)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("normalize", help="统一字段")
    p.add_argument("run"); p.set_defaults(func=cmd_normalize)

    p = sub.add_parser("cluster", help="去重并算支持度")
    p.add_argument("run"); p.set_defaults(func=cmd_cluster)

    p = sub.add_parser("execute", help="执行模型给出的复现测试")
    p.add_argument("run")
    p.add_argument("--allow-untrusted", action="store_true",
                   help="确认已审查测试并允许执行；这不是安全沙箱")
    p.set_defaults(func=cmd_execute)

    p = sub.add_parser("packets", help="生成裁判包")
    p.add_argument("run"); p.add_argument("--only")
    p.set_defaults(func=cmd_packets)

    p = sub.add_parser("votes", help="回收裁判票")
    p.add_argument("run"); p.add_argument("judge"); p.add_argument("dir")
    p.set_defaults(func=cmd_votes)

    p = sub.add_parser("verdicts", help="汇总裁决")
    p.add_argument("run"); p.set_defaults(func=cmd_verdicts)

    p = sub.add_parser("score", help="计分")
    p.add_argument("run"); p.set_defaults(func=cmd_score)

    p = sub.add_parser("report", help="出报告")
    p.add_argument("run"); p.set_defaults(func=cmd_report)

    p = sub.add_parser("all", help="除采集外一键跑完")
    p.add_argument("run")
    p.add_argument("--allow-untrusted", action="store_true",
                   help="确认已审查并执行复现测试；不是安全沙箱")
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("status", help="看当前进度")
    p.add_argument("run"); p.set_defaults(func=cmd_status)

    p = sub.add_parser("scan", help="扫描目录找候选项目")
    p.add_argument("path")
    p.add_argument("--depth", type=int, default=2)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("demo", help="一键跑通内置演示")
    p.set_defaults(func=cmd_demo, run="demo-001")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    try:
        args.func(args)
    except EvalError as exc:
        print(f"\n[出错] {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"\n[文件不存在] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
