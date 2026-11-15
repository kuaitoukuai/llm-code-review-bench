"""大模型代码检查测评系统 · 流程编排

一条命令一个阶段，全程落盘，任何一步都可重跑。
    new-run   → 开一次评测
    task      → 把项目文件夹冻成题库
    start/done→ 登记一次作答（含耗时与 token）
    normalize → 把各家五花八门的回复压成统一字段
    cluster   → 去重、算支持度
    execute   → 在独立目录运行复现测试
    packets   → 生成裁判包（无来源信息）
    verdicts  → 汇总人工、执行结果、模型票与共识
    score     → 计分
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core import (
    CATEGORIES,
    CONFIG_DIR,
    DEMO_DIR,
    PROMPT_DIR,
    RUNS_DIR,
    SEVERITIES,
    TASKS_DIR,
    EvalError,
    candidate_index,
    char_len,
    ensure_run,
    extract_json_block,
    find_project,
    load_candidates,
    load_projects,
    load_system,
    norm_category,
    norm_line,
    norm_path,
    norm_severity,
    now_iso,
    project_root,
    read_json,
    read_jsonl,
    run_dir,
    sha1_text,
    stamp,
    write_json,
    write_jsonl,
)


# ================================================================ run

def new_run(run_id: str | None = None, note: str = "") -> str:
    cfg = load_system()
    rid = run_id or f"r{stamp()}"
    target = run_dir(rid)
    if target.exists():
        raise EvalError(f"run {rid} 已存在。换个 id，或直接续跑它。")
    target.mkdir(parents=True, exist_ok=True)
    write_json(
        target / "run.json",
        {
            "run_id": rid,
            "created_at": now_iso(),
            "note": note,
            "system_version": cfg["system"]["version"],
            "prompt_version": cfg["system"]["prompt_version"],
            "scoring_version": cfg["system"]["scoring_version"],
            "candidates": [c["id"] for c in load_candidates()],
            "projects": [p["id"] for p in load_projects()],
        },
    )
    return rid


# ================================================================ 启动 TXT 使用的准备命令


def register_candidate(candidate_id: str, product: str, model: str, vendor: str) -> tuple[dict, bool]:
    candidate_id = candidate_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate_id):
        raise EvalError("候选 id 使用 产品/模型 格式，只能含字母、数字、点、下划线和连字符")
    values = [product.strip(), model.strip(), vendor.strip().lower()]
    if not all(values):
        raise EvalError("product、model、vendor 都不能为空")
    for existing in load_candidates(include_disabled=True):
        if existing["id"] == candidate_id:
            if not existing.get("enabled", True):
                raise EvalError(f"候选 {candidate_id} 已存在但被禁用，请先启用")
            return existing, False
    item = {"id": candidate_id, "product": values[0], "model": values[1],
            "vendor": values[2], "enabled": True}
    path = CONFIG_DIR / "candidates.toml"
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n# 由启动 TXT 中的评测模型自动登记\n[[candidate]]\n")
        for key in ("id", "product", "model", "vendor"):
            fh.write(f"{key} = {json.dumps(item[key], ensure_ascii=False)}\n")
        fh.write("enabled = true\n")
    return item, True


def prepare_tasks(expected: int = 0) -> dict:
    projects = load_projects()
    if expected and len(projects) != expected:
        raise EvalError(f"启动评测要求 {expected} 个启用项目，当前为 {len(projects)} 个；请检查 config/projects.toml")
    rows = []
    for project in projects:
        tid = project["id"]
        task_dir = TASKS_DIR / tid
        if task_dir.exists():
            meta = read_json(task_dir / "meta.json")
            if not meta:
                raise EvalError(f"任务 {tid} 已存在但缺少 meta.json，不能自动覆盖")
            old_root = Path(meta.get("source_root", "")).resolve()
            if old_root != project_root(project).resolve():
                raise EvalError(f"任务 {tid} 属于另一个项目路径；为保持可比性，不能自动覆盖")
            status = "reused"
        else:
            build_task(tid, tid)
            meta = read_json(task_dir / "meta.json")
            status = "built"
        rows.append({"project_id": tid, "task_id": tid, "name": project.get("name", tid),
                     "status": status, "file_count": meta["file_count"],
                     "total_lines": meta["total_lines"],
                     "prompt": (task_dir / "prompt.md").as_posix()})
    manifest = {"prepared_at": now_iso(), "count": len(rows), "tasks": rows}
    write_json(TASKS_DIR / "batch.json", manifest)
    return manifest


# ================================================================ 项目扫描

PROJECT_MARKERS = [
    ".git", "package.json", "pyproject.toml", "requirements.txt",
    "go.mod", "pom.xml", "Cargo.toml", "setup.py", "Pipfile", "build.gradle",
]
_SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", ".venv", "dist", "build",
    ".next", "target", "out", ".git", "site-packages", ".idea", ".vscode",
}
_CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rs",
    ".vue", ".sql", ".kt", ".swift", ".cs", ".php", ".rb",
}


def _profile_project(path: Path, markers: list[str]) -> dict:
    files, lines, testish = 0, 0, 0
    scanned = 0
    for item in path.rglob("*"):
        if scanned > 4000:
            break
        if item.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in item.parts):
            continue
        if item.suffix.lower() not in _CODE_EXT:
            continue
        scanned += 1
        files += 1
        if "test" in item.name.lower() or "spec" in item.name.lower():
            testish += 1
        try:
            lines += item.stat().st_size // 32  # 粗估，避免逐行读
        except OSError:
            pass

    newest = 0.0
    try:
        for item in list(path.glob("*"))[:60]:
            if item.is_file():
                newest = max(newest, item.stat().st_mtime)
    except OSError:
        pass

    score = 0
    reasons = []
    if ".git" in markers:
        score += 2
        reasons.append("有 git 历史")
    if files >= 3:
        score += 1
    if 200 <= lines <= 20000:
        score += 2
        reasons.append("体量合适")
    elif lines > 20000:
        score -= 1
        reasons.append("体量偏大")
    if testish >= 2:
        score += 1
        reasons.append("有测试")
    if time.time() - newest < 90 * 86400:
        score += 1
        reasons.append("近期改动")

    return {
        "path": str(path),
        "name": path.name,
        "markers": markers,
        "code_files": files,
        "approx_lines": lines,
        "test_files": testish,
        "score": score,
        "reasons": reasons,
    }


def scan_projects(root: Path, max_depth: int = 2) -> list[dict]:
    found: list[dict] = []

    def walk(directory: Path, depth: int):
        try:
            children = [c for c in sorted(directory.iterdir()) if c.is_dir()]
        except (PermissionError, OSError):
            return
        for child in children:
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            markers = [m for m in PROJECT_MARKERS if (child / m).exists()]
            if markers:
                found.append(_profile_project(child, markers))
            elif depth + 1 < max_depth:
                walk(child, depth + 1)

    walk(root, 0)
    found.sort(key=lambda x: (x["score"], x["approx_lines"]), reverse=True)
    return found


# ================================================================ 题库

def _iter_files(root: Path, project: dict) -> list[Path]:
    includes = project.get("include") or ["**/*"]
    excludes = project.get("exclude") or []
    seen: dict[Path, None] = {}
    for pattern in includes:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(_match_exclude(rel, pat) for pat in excludes):
                continue
            seen[path] = None
    return list(seen)


def _match_exclude(rel: str, pattern: str) -> bool:
    if fnmatch.fnmatch(rel, pattern):
        return True
    try:
        if Path(rel).match(pattern):
            return True
    except ValueError:
        pass
    # `**/node_modules/**` 这类前缀模式退化为段匹配
    segments = [s for s in pattern.split("/") if s and s != "**"]
    if segments:
        parts = rel.split("/")
        for seg in segments:
            if not any(fnmatch.fnmatch(p, seg) for p in parts):
                return False
        return True
    return False


def _read_text(path: Path) -> str | None:
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _lang_of(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "python", "ts": "typescript", "tsx": "tsx", "js": "javascript",
        "jsx": "jsx", "go": "go", "java": "java", "rs": "rust", "sql": "sql",
        "sh": "bash", "yml": "yaml", "yaml": "yaml", "json": "json", "toml": "toml",
    }.get(ext, "")


def _render_code(files: list[dict]) -> str:
    width = max((len(str(f["lines"])) for f in files), default=3)
    blocks = []
    for item in files:
        lang = _lang_of(item["path"])
        note = "　（已截断）" if item.get("clipped") else ""
        lines = item["text"].splitlines() or [""]
        body = "\n".join(
            f"{str(i).rjust(width)}| {line}" for i, line in enumerate(lines, 1)
        )
        blocks.append(
            f"### FILE: {item['path']}　（共 {item['lines']} 行{note}）\n"
            f"```{lang}\n{body}\n```"
        )
    return "\n\n".join(blocks)


def build_task(project_id: str, task_id: str | None = None, force: bool = False) -> str:
    cfg = load_system()
    project = find_project(project_id)
    tid = task_id or project_id
    root = project_root(project)
    if not root.exists():
        raise EvalError(f"项目目录不存在：{root}\n改 config/projects.toml 里的 path")

    max_files = int(project.get("max_files", 10))
    max_lines = int(project.get("max_lines_per_file", 500))

    paths = _iter_files(root, project)
    if not paths:
        raise EvalError(f"{root} 下没有匹配 include 的文件")
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    paths = paths[:max_files]

    entries = []
    for path in paths:
        text = _read_text(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        clipped = False
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            clipped = True
        entries.append(
            {
                "path": rel,
                "text": "\n".join(lines),
                "lines": len(lines),
                "clipped": clipped,
                "sha1": sha1_text(text),
            }
        )
    if not entries:
        raise EvalError("选中的文件都读不出文本，检查 include 是否选到了二进制")

    task_dir = TASKS_DIR / tid
    if task_dir.exists() and not force:
        raise EvalError(f"任务 {tid} 已存在。加 --force 覆盖，或换 task-id。")
    task_dir.mkdir(parents=True, exist_ok=True)

    write_jsonl(task_dir / "files.jsonl", entries)

    template = (PROMPT_DIR / f"{cfg['system']['prompt_version']}.md").read_text(encoding="utf-8")
    prompt = template.replace("{{CODE}}", _render_code(entries))
    (task_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    write_json(
        task_dir / "meta.json",
        {
            "task_id": tid,
            "project_id": project_id,
            "project_name": project.get("name", project_id),
            "source_root": str(root),
            "built_at": now_iso(),
            "file_count": len(entries),
            "total_lines": sum(e["lines"] for e in entries),
            "prompt_chars": len(prompt),
            "prompt_version": cfg["system"]["prompt_version"],
            "files": [e["path"] for e in entries],
        },
    )
    return tid


# ================================================================ 采集

def _raw_dir(run_id: str, candidate: str, task: str) -> Path:
    return run_dir(run_id) / "raw" / candidate / task


def session_start(run_id: str, candidate: str, task: str, repeat: int = 1) -> Path:
    ensure_run(run_id)
    if not (TASKS_DIR / task / "prompt.md").exists():
        raise EvalError(f"任务 {task} 还没建，先跑 task 命令")
    d = _raw_dir(run_id, candidate, task)
    d.mkdir(parents=True, exist_ok=True)
    sess = d / f"r{repeat}.session.json"
    write_json(sess, {"started_at": now_iso(), "ts": time.time()})
    return sess


def session_done(
    run_id: str,
    candidate: str,
    task: str,
    source: Path | None,
    repeat: int = 1,
    token_in: int | None = None,
    token_out: int | None = None,
    duration: float | None = None,
    note: str = "",
) -> dict:
    ensure_run(run_id)
    d = _raw_dir(run_id, candidate, task)
    d.mkdir(parents=True, exist_ok=True)
    sess_path = d / f"r{repeat}.session.json"
    started = read_json(sess_path) or {}

    if duration is None:
        if "ts" in started:
            duration = time.time() - started["ts"]
        else:
            raise EvalError("没有 start 记录，也没给 --duration，无法确定耗时")

    payload = ""
    if source is not None:
        if not source.exists():
            raise EvalError(f"文件不存在：{source}")
        payload = source.read_text(encoding="utf-8", errors="replace")
        (d / f"r{repeat}.md").write_text(payload, encoding="utf-8")
    else:
        existing = d / f"r{repeat}.md"
        if not existing.exists():
            raise EvalError("既没有 --from 文件，也没有已存在的 r{}.md".format(repeat))
        payload = existing.read_text(encoding="utf-8", errors="replace")

    cfg = load_system()
    limit = float(cfg["limits"].get("max_task_seconds", 7200))
    meta = {
        "candidate": candidate,
        "task": task,
        "repeat": repeat,
        "overtime": bool(duration and float(duration) > limit),
        "started_at": started.get("started_at"),
        "ended_at": now_iso(),
        "duration_sec": round(float(duration), 1),
        "token_in": token_in,
        "token_out": token_out,
        "reply_chars": len(payload),
        "note": note,
    }
    write_json(d / f"r{repeat}.meta.json", meta)
    if sess_path.exists():
        sess_path.unlink()
    return meta


# ================================================================ 归一化

def normalize(run_id: str) -> dict:
    cfg = load_system()
    limits = cfg["limits"]
    char_limits = limits["chars"]
    max_findings = int(limits["max_findings_per_task"])
    max_repro_chars = int(cfg.get("execution", {}).get("max_test_chars", 12000))
    base = ensure_run(run_id)
    cands = candidate_index(include_disabled=True)

    stats = {"replies": 0, "claims": 0, "rejected": 0, "parse_error": 0, "reproductions": 0}
    issues: list[dict] = []
    reproduction_root = base / "reproductions"
    if reproduction_root.exists():
        shutil.rmtree(reproduction_root)

    raw_root = base / "raw"
    for raw in sorted(raw_root.rglob("r*.md")) if raw_root.exists() else []:
        rel = raw.relative_to(raw_root)
        parts = rel.parts
        if len(parts) < 3:
            continue
        task = parts[-2]
        candidate = "/".join(parts[:-2])
        repeat = int(raw.stem.lstrip("r") or 1)
        stats["replies"] += 1

        text = raw.read_text(encoding="utf-8", errors="replace")
        try:
            arr = extract_json_block(text)
        except ValueError as exc:
            stats["parse_error"] += 1
            issues.append({"candidate": candidate, "task": task, "repeat": repeat,
                           "level": "parse_error", "detail": str(exc)})
            write_jsonl(base / "normalized" / candidate / task / f"r{repeat}.jsonl", [])
            continue

        if not isinstance(arr, list):
            arr = [arr]

        rows = []
        for idx, item in enumerate(arr):
            if idx >= max_findings:
                continue
            if not isinstance(item, dict):
                stats["rejected"] += 1
                issues.append({"candidate": candidate, "task": task, "repeat": repeat,
                               "level": "not_object", "detail": f"第 {idx} 项不是对象"})
                continue
            claim, reason = _to_claim(
                item, char_limits, candidate, task, repeat, idx, cands, max_repro_chars
            )
            if claim is None:
                stats["rejected"] += 1
                issues.append({"candidate": candidate, "task": task, "repeat": repeat,
                               "level": "field", "detail": reason,
                               "raw": {k: str(v)[:80] for k, v in item.items()}})
                continue
            reproduction = claim.get("reproduction")
            if reproduction:
                target = (
                    reproduction_root / candidate / task / f"r{repeat}"
                    / f"f{idx + 1:02d}-{reproduction['filename']}"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(reproduction["content"], encoding="utf-8")
                claim["reproduction"] = {
                    "path": target.relative_to(base).as_posix(),
                    "filename": reproduction["filename"],
                    "sha1": sha1_text(reproduction["content"]),
                }
                stats["reproductions"] += 1
            rows.append(claim)

        if len(arr) > max_findings:
            issues.append({"candidate": candidate, "task": task, "repeat": repeat,
                           "level": "overflow", "detail": f"报了 {len(arr)} 条，截取前 {max_findings} 条"})
            rows = rows[:max_findings]

        stats["claims"] += len(rows)
        write_jsonl(base / "normalized" / candidate / task / f"r{repeat}.jsonl", rows)

    write_jsonl(base / "normalized" / "_issues.jsonl", issues)
    return stats


def _to_claim(item, char_limits, candidate, task, repeat, idx, cands, max_repro_chars=12000):
    from core import FIELD_ALIASES, _pick

    text_fields = {}
    for field in ("title", "trigger", "impact", "verify_hint"):
        value = _pick(item, field)
        if value is None:
            return None, f"缺少字段 {field}"
        text = str(value).strip()
        if not text:
            return None, f"字段 {field} 为空"
        limit = int(char_limits.get(field, 200))
        if char_len(text) > limit:
            return None, f"字段 {field} 超限 {char_len(text)}/{limit} 字"
        text_fields[field] = text

    path = _pick(item, "file")
    line = _pick(item, "line")
    if path is None:
        return None, "缺少 file"
    if line is None:
        return None, "缺少 line"

    rel = norm_path(path)
    if not rel:
        return None, "file 无法解析"
    lineno = norm_line(line)
    if not lineno:
        return None, "line 无法解析"

    reproduction = item.get("reproduction")
    if reproduction is not None:
        if not isinstance(reproduction, dict):
            return None, "reproduction 必须是对象"
        filename = str(reproduction.get("filename", "")).strip()
        content = reproduction.get("content")
        if not re.fullmatch(r"test_[A-Za-z0-9_.-]*\.py", filename):
            return None, "reproduction.filename 必须是不含目录的 test_*.py"
        if not isinstance(content, str) or not content.strip():
            return None, "reproduction.content 为空"
        if len(content) > max_repro_chars:
            return None, f"复现测试超限 {len(content)}/{max_repro_chars} 字"
        reproduction = {"filename": filename, "content": content}

    sev = norm_severity(_pick(item, "severity")) or "P2"
    cat = norm_category(_pick(item, "category"))

    vendor = cands.get(candidate, {}).get("vendor", candidate)
    claim = {
        "candidate": candidate,
        "vendor": vendor,
        "task": task,
        "repeat": repeat,
        "idx": idx,
        "file": rel,
        "line": lineno,
        "severity": sev,
        "category": cat,
        **text_fields,
    }
    if reproduction is not None:
        claim["reproduction"] = reproduction
    return claim, None


# ================================================================ 聚类

def _similar_file(a: str, b: str) -> bool:
    if a == b:
        return True
    return a.endswith("/" + b) or b.endswith("/" + a)


_SNAKE_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")


def _idents(claim: dict) -> set[str]:
    text = f"{claim.get('title','')} {claim.get('trigger','')} {claim.get('impact','')}"
    return {m.group(1).lower() for m in _SNAKE_RE.finditer(text)}


def _same_issue(head: dict, claim: dict, window: int, hard_window: int) -> bool:
    if not _similar_file(head["file"], claim["file"]):
        return False
    if head["category"] != claim["category"]:
        return False
    diff = abs(claim["line"] - head["line"])
    if diff > window:
        return False
    if diff <= hard_window:
        return True
    # 相隔较远时，要求函数名有交集；两边都提不出函数名则按行号判断
    a, b = _idents(head), _idents(claim)
    if a and b and not (a & b):
        return False
    return True


def cluster(run_id: str) -> dict:
    cfg = load_system()
    window = int(cfg["clustering"]["line_window"])
    hard_window = int(cfg["clustering"].get("hard_line_window", 3))
    base = ensure_run(run_id)

    claims: list[dict] = []
    for path in sorted((base / "normalized").rglob("*.jsonl")):
        if path.name.startswith("_"):
            continue
        claims.extend(read_jsonl(path))
    if not claims:
        raise EvalError("没有可聚类的发现。先跑 normalize，确认 normalized 下有内容。")

    claims.sort(key=lambda c: (c["file"], c["line"], c["candidate"]))

    groups: list[list[dict]] = []
    for claim in claims:
        placed = False
        for group in reversed(groups):
            if _same_issue(group[0], claim, window, hard_window):
                group.append(claim)
                placed = True
                break
        if not placed:
            groups.append([claim])

    pool, origin, clusters = [], [], []
    counter = 0
    sev_rank = {s: i for i, s in enumerate(SEVERITIES)}

    for gi, group in enumerate(groups, 1):
        cid = f"K{gi:03d}"
        members = []
        for claim in group:
            counter += 1
            claim_id = f"C{counter:04d}"
            members.append(claim_id)
            source = {"claim_id": claim_id, "cluster_id": cid,
                      "candidate": claim["candidate"], "task": claim["task"],
                      "repeat": claim["repeat"]}
            if claim.get("reproduction"):
                source["reproduction"] = claim["reproduction"]
            origin.append(source)
            pool.append({
                "claim_id": claim_id,
                "cluster_id": cid,
                **{k: claim[k] for k in ("file", "line", "severity", "category",
                                         "title", "trigger", "impact", "verify_hint")},
            })

        # 代表条目：严重度最高、描述最完整
        rep = sorted(group, key=lambda c: (sev_rank.get(c["severity"], 9), -char_len(c["title"])))[0]
        vendors = sorted({c["vendor"] for c in group})
        candidates = sorted({c["candidate"] for c in group})
        clusters.append({
            "cluster_id": cid,
            "file": rep["file"],
            "line": rep["line"],
            "line_min": min(c["line"] for c in group),
            "line_max": max(c["line"] for c in group),
            "category": rep["category"],
            "severity": rep["severity"],
            "claim_ids": members,
            "support_candidates": candidates,
            "support_vendors": vendors,
            "support_count": len(vendors),
            "rep": {k: rep[k] for k in ("title", "trigger", "impact", "verify_hint")},
        })

    total_vendors = len({c["vendor"] for c in claims}) or 1
    for item in clusters:
        item["support_ratio"] = round(item["support_count"] / total_vendors, 3)

    write_jsonl(base / "claims" / "pool.jsonl", pool)
    write_jsonl(base / "claims" / "origin.jsonl", origin)
    write_jsonl(base / "claims" / "clusters.jsonl", clusters)

    return {
        "claims": len(pool),
        "clusters": len(clusters),
        "vendors": total_vendors,
        "unique_clusters": sum(1 for c in clusters if c["support_count"] == 1),
    }


# ================================================================ 可执行裁决


def _execution_env() -> dict[str, str]:
    keys = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update({"PYTHONNOUSERSITE": "1", "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"})
    return env


def _write_frozen_task(workspace: Path, task: str) -> None:
    files = read_jsonl(TASKS_DIR / task / "files.jsonl")
    if not files:
        raise EvalError(f"任务 {task} 没有冻结代码，无法执行复现")
    for item in files:
        rel = Path(item["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise EvalError(f"任务 {task} 含不安全路径：{item['path']}")
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["text"], encoding="utf-8")


def _execution_status(returncode: int, output: str) -> str:
    if "unittest.loader._FailedTest" in output or "Failed to import test module" in output:
        return "error"
    ran = re.search(r"Ran ([1-9]\d*) tests?", output)
    if not ran:
        return "error"
    if returncode == 0 and re.search(r"^OK\b", output, re.MULTILINE):
        return "not_reproduced"
    if returncode != 0 and re.search(r"^FAILED \(", output, re.MULTILINE):
        return "reproduced"
    return "error"


def execute_reproductions(run_id: str, allow_untrusted: bool = False) -> dict:
    base = ensure_run(run_id)
    origin = read_jsonl(base / "claims" / "origin.jsonl")
    pending = [item for item in origin if item.get("reproduction")]
    out = base / "executions"
    stats = {"total": len(pending), "reproduced": 0, "not_reproduced": 0,
             "error": 0, "timeout": 0, "clusters": 0}
    if pending and not allow_untrusted:
        raise EvalError(
            "复现测试是模型生成的代码。请先审查 runs/<run>/reproductions，"
            "确认后加 --allow-untrusted 再执行；该功能不是安全沙箱。"
        )
    if out.exists():
        shutil.rmtree(out)
    if not pending:
        write_jsonl(out / "results.jsonl", [])
        return stats

    cfg = load_system().get("execution", {})
    timeout = float(cfg.get("timeout_seconds", 15))
    results = []
    for item in pending:
        repro = item["reproduction"]
        workspace = out / "workspaces" / item["claim_id"]
        workspace.mkdir(parents=True, exist_ok=True)
        _write_frozen_task(workspace, item["task"])
        test_path = workspace / repro["filename"]
        source = base / repro["path"]
        if not source.exists():
            raise EvalError(f"复现测试不存在：{source}。先重跑 normalize")
        shutil.copyfile(source, test_path)

        command = [sys.executable, "-m", "unittest", "discover", "-s", ".",
                   "-p", repro["filename"], "-v"]
        started = time.monotonic()
        output = ""
        returncode = None
        status = "error"
        try:
            proc = subprocess.run(
                command, cwd=workspace, env=_execution_env(), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                check=False,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            returncode = proc.returncode
            status = _execution_status(returncode, output)
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            output = stdout + stderr + f"\n执行超过 {timeout:g} 秒"
        except OSError as exc:
            output = str(exc)

        stats[status] += 1
        log_path = out / "logs" / f"{item['claim_id']}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        results.append({
            "claim_id": item["claim_id"],
            "cluster_id": item["cluster_id"],
            "task": item["task"],
            "status": status,
            "returncode": returncode,
            "duration_sec": round(time.monotonic() - started, 3),
            "workspace": workspace.relative_to(base).as_posix(),
            "log": log_path.relative_to(base).as_posix(),
            "output_tail": output[-2000:],
            "ran_at": now_iso(),
        })

    write_jsonl(out / "results.jsonl", results)
    stats["clusters"] = len({r["cluster_id"] for r in results
                             if r["status"] in ("reproduced", "not_reproduced")})
    return stats


# ================================================================ 裁判包

def make_packets(run_id: str, only_ids: list[str] | None = None) -> int:
    cfg = load_system()
    ctx_lines = int(cfg["judge"]["context_lines"])
    base = ensure_run(run_id)
    clusters = read_jsonl(base / "claims" / "clusters.jsonl")
    if not clusters:
        raise EvalError("还没有 clusters，先跑 cluster")

    template = (PROMPT_DIR / "judge_v1.md").read_text(encoding="utf-8")
    out = base / "judgments" / "packets"
    out.mkdir(parents=True, exist_ok=True)

    count = 0
    for item in clusters:
        if only_ids and item["cluster_id"] not in only_ids:
            continue
        claim_block = "\n".join([
            f"文件：{item['file']}",
            f"行号：{item['line']}",
            f"严重度：{item['severity']}",
            f"类别：{item['category']}",
            f"标题：{item['rep']['title']}",
            f"触发条件：{item['rep']['trigger']}",
            f"影响：{item['rep']['impact']}",
            f"验证方法：{item['rep']['verify_hint']}",
        ])
        code_block = _code_context(base, item, ctx_lines)
        text = template.replace("{{CLAIM}}", claim_block).replace("{{CODE}}", code_block)
        (out / f"{item['cluster_id']}.md").write_text(text, encoding="utf-8")
        count += 1
    return count


def _code_context(base: Path, item: dict, ctx_lines: int) -> str:
    snippets = []
    for task_dir in sorted(TASKS_DIR.iterdir()) if TASKS_DIR.exists() else []:
        files = read_jsonl(task_dir / "files.jsonl")
        for f in files:
            if not _similar_file(f["path"], item["file"]):
                continue
            lines = f["text"].splitlines()
            lo = max(1, item["line_min"] - ctx_lines)
            hi = min(len(lines), item["line_max"] + ctx_lines)
            width = len(str(hi))
            body = "\n".join(
                f"{str(i).rjust(width)}| {lines[i - 1]}"
                for i in range(lo, hi + 1)
                if i - 1 < len(lines)
            )
            snippets.append(f"### {f['path']} 第 {lo}-{hi} 行\n```{_lang_of(f['path'])}\n{body}\n```")
    if not snippets:
        return "（未找到对应代码片段，请在 judgments/packets 中手工补入）"
    return "\n\n".join(snippets)


# ================================================================ 裁决

def collect_votes(run_id: str, judge_id: str, directory: Path) -> int:
    """把某个裁判对各 cluster 的回复批量收进来。

    文件名规范：<cluster_id>.json 或 <cluster_id>.md（内含 JSON 对象）
    """
    base = ensure_run(run_id)
    out = base / "judgments" / "votes"
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(directory.glob("*")):
        if path.suffix.lower() not in (".json", ".md", ".txt"):
            continue
        cid = path.stem
        text = path.read_text(encoding="utf-8", errors="replace")
        payload = None
        try:
            payload = __import__("json").loads(text)
        except Exception:
            for chunk in __import__("re").findall(r"\{[\s\S]*?\}", text):
                try:
                    payload = __import__("json").loads(chunk)
                    break
                except Exception:
                    continue
        if not isinstance(payload, dict):
            continue
        verdict = str(payload.get("verdict", "")).strip().lower()
        if verdict not in ("valid", "invalid", "unsure"):
            continue
        write_json(out / f"{cid}__{judge_id}.json", {
            "cluster_id": cid,
            "judge": judge_id,
            "verdict": verdict,
            "reason": str(payload.get("reason", ""))[:120],
            "severity_ok": payload.get("severity_ok"),
            "at": now_iso(),
        })
        count += 1
    return count


def adjudicate(run_id: str) -> dict:
    """四级裁决：人工 > 执行结果 > 模型票 > 共识推断。"""
    cfg = load_system()
    consensus_ratio = float(cfg["judge"]["consensus_ratio"])
    queue_sev = set(cfg["judge"]["human_queue_severities"])
    base = ensure_run(run_id)
    clusters = read_jsonl(base / "claims" / "clusters.jsonl")
    if not clusters:
        raise EvalError("还没有 clusters")

    human = {r["cluster_id"]: r for r in read_jsonl(base / "judgments" / "human.jsonl")}
    executions: dict[str, list[dict]] = {}
    for result in read_jsonl(base / "executions" / "results.jsonl"):
        executions.setdefault(result["cluster_id"], []).append(result)
    votes: dict[str, list[dict]] = {}
    for path in sorted((base / "judgments" / "votes").glob("*.json")):
        vote = read_json(path)
        if vote:
            votes.setdefault(vote["cluster_id"], []).append(vote)

    results, queue = [], []
    tally = {"human": 0, "execution": 0, "execution_split": 0,
             "panel": 0, "consensus": 0, "pending": 0}

    for item in clusters:
        cid = item["cluster_id"]
        if cid in human:
            verdict, confidence, reason = human[cid]["verdict"], "human", human[cid].get("reason", "")
            tally["human"] += 1
        elif cid in executions and any(
            r["status"] in ("reproduced", "not_reproduced") for r in executions[cid]
        ):
            panel = executions[cid]
            reproduced = sum(1 for r in panel if r["status"] == "reproduced")
            not_reproduced = sum(1 for r in panel if r["status"] == "not_reproduced")
            if reproduced and not_reproduced:
                verdict, confidence = "unsure", "execution_split"
                reason = f"执行结果冲突：{reproduced} 个复现，{not_reproduced} 个未复现"
                tally["execution_split"] += 1
            elif reproduced:
                verdict, confidence = "valid", "execution"
                reason = f"{reproduced} 个复现测试失败（符合缺陷预期）"
                tally["execution"] += 1
            else:
                verdict, confidence = "invalid", "execution"
                reason = f"{not_reproduced} 个复现测试通过（未复现缺陷）"
                tally["execution"] += 1
        elif cid in votes:
            panel = votes[cid]
            valid = sum(1 for v in panel if v["verdict"] == "valid")
            invalid = sum(1 for v in panel if v["verdict"] == "invalid")
            unsure = sum(1 for v in panel if v["verdict"] == "unsure")
            if valid > invalid:
                verdict = "valid"
            elif invalid > valid:
                verdict = "invalid"
            else:
                verdict = "unsure"
            confidence = "panel" if verdict != "unsure" else "panel_split"
            reason = f"票型 v{valid}/i{invalid}/u{unsure}"
            tally["panel"] += 1
        elif item["support_ratio"] >= consensus_ratio:
            verdict, confidence = "valid", "consensus"
            reason = f"共识推断：{item['support_count']} 家提出"
            tally["consensus"] += 1
        else:
            verdict, confidence = "unsure", "pending"
            reason = "无人裁决"
            tally["pending"] += 1

        record = {
            "cluster_id": cid,
            "file": item["file"],
            "line": item["line"],
            "severity": item["severity"],
            "category": item["category"],
            "title": item["rep"]["title"],
            "support_count": item["support_count"],
            "support_ratio": item["support_ratio"],
            "verdict": verdict,
            "confidence": confidence,
            "reason": reason,
            "claim_ids": item["claim_ids"],
            "support_candidates": item["support_candidates"],
        }
        results.append(record)

        needs_human = (
            confidence in ("pending", "panel_split", "execution_split")
            or (confidence == "consensus" and item["severity"] in queue_sev)
        )
        if needs_human and cid not in human:
            queue.append(record)

    write_jsonl(base / "judgments" / "verdicts.jsonl", results)
    write_jsonl(base / "judgments" / "queue.jsonl", queue)

    # 人审队列导出成 markdown，方便对着代码看
    lines = ["# 人工复核队列", "",
             f"生成时间：{now_iso()}　共 {len(queue)} 条", "",
             "在 `judgments/human.jsonl` 里逐行填 `{\"cluster_id\":\"K001\",\"verdict\":\"valid\",\"reason\":\"...\"}`", ""]
    for r in queue:
        lines += [
            f"## {r['cluster_id']}　{r['severity']}　{r['file']}:{r['line']}",
            f"- 提出者：{', '.join(r['support_candidates'])}（{r['support_count']} 家）",
            f"- 标题：{r['title']}",
            f"- 当前状态：{r['confidence']}　{r['reason']}",
            "",
        ]
    (base / "judgments" / "queue.md").write_text("\n".join(lines), encoding="utf-8")

    return {**tally, "total_clusters": len(clusters), "queue": len(queue)}


# ================================================================ 计分

def score(run_id: str) -> dict:
    cfg = load_system()
    weights = cfg["weights"]
    sev_weight = {k: float(v) for k, v in weights["severity"].items()}
    fp_penalty = float(weights["false_positive_penalty"])
    unique_bonus = float(weights["unique_bonus"])
    min_findings = int(weights["min_findings"])
    tp = cfg["time_penalty"]

    base = ensure_run(run_id)
    verdicts = {r["cluster_id"]: r for r in read_jsonl(base / "judgments" / "verdicts.jsonl")}
    if not verdicts:
        raise EvalError("还没有裁决结果，先跑 verdicts")
    origin = read_jsonl(base / "claims" / "origin.jsonl")
    claims = read_jsonl(base / "claims" / "pool.jsonl")
    claim_index = {c["claim_id"]: c for c in claims}

    tasks = sorted({o["task"] for o in origin})
    per_candidate: dict[str, dict] = {}

    for cand in load_candidates():
        cid = cand["id"]
        # 完全没作答的候选不进榜，避免把"没参加"算成"沉默"
        cand_raw = base / "raw" / cid
        if not (cand_raw.exists() and any(cand_raw.rglob("*.md"))):
            continue
        mine = [o for o in origin if o["candidate"] == cid]
        hits = fps = unsures = 0
        unique_hits = 0
        raw = 0.0
        sev_hits: dict[str, int] = {s: 0 for s in SEVERITIES}

        for item in mine:
            verdict = verdicts.get(item["cluster_id"])
            if verdict is None:
                continue
            claim = claim_index.get(item["claim_id"], {})
            if verdict["verdict"] == "valid":
                hits += 1
                weight = sev_weight.get(verdict["severity"], 1.0)
                raw += weight
                sev_hits[verdict["severity"]] += 1
                if verdict["support_count"] == 1:
                    unique_hits += 1
                    raw += weight * (unique_bonus - 1.0)
            elif verdict["verdict"] == "invalid":
                fps += 1
                raw -= fp_penalty
            else:
                unsures += 1

        # 报得太少：缺额按误报计，防止"沉默"成为最优策略
        shortfall = max(0, min_findings - (hits + fps + unsures))
        if shortfall:
            raw -= fp_penalty * shortfall

        metas = []
        for task in tasks:
            for path in sorted((base / "raw" / cid / task).rglob("r*.meta.json")):
                metas.append(read_json(path))
        durations = [m["duration_sec"] for m in metas if m.get("duration_sec")]
        overtime = sum(1 for m in metas if m.get("overtime"))
        token_in = sum(m.get("token_in") or 0 for m in metas)
        token_out = sum(m.get("token_out") or 0 for m in metas)

        per_candidate[cid] = {
            "candidate": cid,
            "vendor": cand.get("vendor", ""),
            "hits": hits,
            "false_positives": fps,
            "unsure": unsures,
            "unique_hits": unique_hits,
            "raw_score": round(raw, 2),
            "precision": round(hits / (hits + fps), 3) if (hits + fps) else None,
            "P0": sev_hits["P0"], "P1": sev_hits["P1"],
            "P2": sev_hits["P2"], "P3": sev_hits["P3"],
            "avg_duration_sec": round(sum(durations) / len(durations), 1) if durations else None,
            "overtime_count": overtime,
            "token_in": token_in or None,
            "token_out": token_out or None,
            "shortfall_penalty": round(fp_penalty * shortfall, 2),
        }

    # 时间惩罚：以每题的跨候选中位耗时为基准
    if tp.get("enabled", True):
        medians = {}
        for task in tasks:
            values = []
            for cid, row in per_candidate.items():
                for path in sorted((base / "raw" / cid / task).rglob("r*.meta.json")):
                    meta = read_json(path)
                    if meta.get("duration_sec"):
                        values.append(meta["duration_sec"])
            if values:
                values.sort()
                medians[task] = values[len(values) // 2]
        grace = float(tp.get("grace_ratio", 2.0))
        cap = float(tp.get("max_penalty", 0.05))
        for cid, row in per_candidate.items():
            if not row["avg_duration_sec"] or not medians:
                row["time_factor"] = 1.0
                continue
            baseline = sum(medians.values()) / len(medians)
            ratio = row["avg_duration_sec"] / baseline if baseline else 1.0
            over = max(0.0, ratio - grace)
            row["time_factor"] = round(1.0 - min(cap, cap * over / max(grace, 1e-6)), 4)
    else:
        for row in per_candidate.values():
            row["time_factor"] = 1.0

    # 覆盖度：以该题所有候选命中簇的并集为分母
    union_hits = {v["cluster_id"] for v in verdicts.values() if v["verdict"] == "valid"}
    union_per_task: dict[str, set] = {}
    cluster_task = {}
    for item in origin:
        cluster_task.setdefault(item["cluster_id"], item["task"])
    for cid in union_hits:
        union_per_task.setdefault(cluster_task.get(cid, "?"), set()).add(cid)

    for cid, row in per_candidate.items():
        mine_valid = {
            o["cluster_id"] for o in origin
            if o["candidate"] == cid and verdicts.get(o["cluster_id"], {}).get("verdict") == "valid"
        }
        denominator = sum(len(s) for s in union_per_task.values())
        row["coverage_proxy"] = round(len(mine_valid) / denominator, 3) if denominator else None
        row["final_score"] = round(row["raw_score"] * row["time_factor"], 2)
        row["tasks"] = len({o["task"] for o in origin if o["candidate"] == cid})
        row["claims"] = sum(1 for o in origin if o["candidate"] == cid)

    rows = sorted(per_candidate.values(), key=lambda r: r["final_score"], reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank

    write_jsonl(base / "scores.jsonl", rows)
    write_json(base / "scores.json", {
        "run_id": run_id,
        "computed_at": now_iso(),
        "scoring_version": cfg["system"]["scoring_version"],
        "tasks": tasks,
        "union_valid_clusters": len(union_hits),
        "rows": rows,
    })
    return {"rows": rows, "tasks": tasks, "union_hits": len(union_hits)}
