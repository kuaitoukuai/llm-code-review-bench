"""大模型代码检查测评系统 · 核心工具

零第三方依赖，只用标准库。Python 3.11+（用到 tomllib）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPT_DIR = ROOT / "prompts"
TASKS_DIR = ROOT / "tasks"
RUNS_DIR = ROOT / "runs"
REPORTS_DIR = ROOT / "reports"
DEMO_DIR = ROOT / "demo"

SEVERITIES = ["P0", "P1", "P2", "P3"]
CATEGORIES = [
    "correctness",
    "security",
    "reliability",
    "performance",
    "maintainability",
]

# 字段别名：各家模型习惯不同，统一收口
FIELD_ALIASES = {
    "file": ["file", "path", "filename", "文件", "路径", "文件路径"],
    "line": ["line", "lineno", "line_number", "行", "行号"],
    "severity": ["severity", "level", "priority", "等级", "严重度", "严重程度", "优先级"],
    "category": ["category", "type", "kind", "类型", "类别", "分类"],
    "title": ["title", "summary", "issue", "name", "标题", "问题", "摘要"],
    "trigger": ["trigger", "condition", "repro", "when", "触发", "触发条件", "复现"],
    "impact": ["impact", "consequence", "result", "影响", "后果"],
    "verify_hint": ["verify_hint", "verify", "validation", "how_to_verify", "验证", "验证方法"],
}

SEVERITY_MAP = {
    "p0": "P0", "critical": "P0", "blocker": "P0", "致命": "P0", "严重": "P0", "紧急": "P0",
    "p1": "P1", "high": "P1", "major": "P1", "高": "P1", "重要": "P1",
    "p2": "P2", "medium": "P2", "normal": "P2", "中": "P2", "中等": "P2", "一般": "P2",
    "p3": "P3", "low": "P3", "minor": "P3", "trivial": "P3", "info": "P3",
    "低": "P3", "轻微": "P3", "提示": "P3",
}

CATEGORY_MAP = {
    "correctness": "correctness", "correct": "correctness", "logic": "correctness",
    "bug": "correctness", "正确性": "correctness", "逻辑": "correctness", "功能": "correctness",
    "security": "security", "sec": "security", "安全": "security",
    "reliability": "reliability", "robustness": "reliability", "error_handling": "reliability",
    "稳定性": "reliability", "可靠性": "reliability", "健壮性": "reliability",
    "performance": "performance", "perf": "performance", "性能": "performance",
    "maintainability": "maintainability", "style": "maintainability", "readability": "maintainability",
    "可维护性": "maintainability", "可读性": "maintainability", "规范": "maintainability",
}


class EvalError(Exception):
    """可读的流程错误，CLI 捕获后只打印消息。"""


# ---------------------------------------------------------------- 配置


def _load_toml(path: Path) -> dict:
    if tomllib is None:
        raise EvalError(
            "需要 Python 3.11 或更高版本（用到了标准库 tomllib）。"
            f"当前版本 {sys.version.split()[0]}。"
        )
    if not path.exists():
        raise EvalError(f"配置文件不存在：{path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_system() -> dict:
    return _load_toml(CONFIG_DIR / "system.toml")


def load_candidates(include_disabled: bool = False) -> list[dict]:
    data = _load_toml(CONFIG_DIR / "candidates.toml")
    items = data.get("candidate", [])
    if not include_disabled:
        items = [c for c in items if c.get("enabled", True)]
    if not items:
        raise EvalError("config/candidates.toml 里没有启用的候选。")
    return items


def load_projects(include_disabled: bool = False) -> list[dict]:
    data = _load_toml(CONFIG_DIR / "projects.toml")
    items = data.get("project", [])
    if not include_disabled:
        items = [p for p in items if p.get("enabled", True)]
    if not items:
        raise EvalError("config/projects.toml 里没有启用的项目。")
    return items


def candidate_index(include_disabled: bool = False) -> dict[str, dict]:
    return {c["id"]: c for c in load_candidates(include_disabled)}


def find_project(project_id: str) -> dict:
    for p in load_projects(include_disabled=True):
        if p["id"] == project_id:
            return p
    raise EvalError(f"没有找到项目 {project_id}，检查 config/projects.toml")


def project_root(project: dict) -> Path:
    raw = Path(project["path"])
    return raw if raw.is_absolute() else (ROOT / raw)


# ---------------------------------------------------------------- I/O


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                raise EvalError(f"{path} 第 {n} 行不是合法 JSON")
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 归一化


def _pick(raw: dict, field: str):
    for key in FIELD_ALIASES[field]:
        for k, v in raw.items():
            if str(k).strip().lower() == key.lower():
                return v
    return None


def norm_severity(value) -> str | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    if key.upper() in SEVERITIES:
        return key.upper()
    return SEVERITY_MAP.get(key)


def norm_category(value) -> str:
    if value is None:
        return "correctness"
    key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    return CATEGORY_MAP.get(key, "correctness")


def norm_line(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def norm_path(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip("`\"'")
    text = text.replace("\\", "/")
    text = re.sub(r":\d+(-\d+)?$", "", text)  # 去掉尾部 :148
    text = re.sub(r"^\./", "", text)
    return text or None


def char_len(text: str) -> int:
    return len(text.strip())


def extract_json_block(text: str):
    """从模型回复里抠出 JSON 数组。容忍 markdown 包裹与前后废话。"""
    if not text or not text.strip():
        raise ValueError("空回复")

    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidates = list(fenced) + [text]

    for chunk in candidates:
        chunk = chunk.strip()
        if not chunk:
            continue
        # 直接就是一个数组
        start = chunk.find("[")
        end = chunk.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(chunk[start : end + 1])
            except json.JSONDecodeError:
                pass
        # 退化：单个对象包装成数组
        start = chunk.find("{")
        end = chunk.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(chunk[start : end + 1])
                return [obj] if isinstance(obj, dict) else obj
            except json.JSONDecodeError:
                pass
    raise ValueError("没有找到可解析的 JSON 数组")


# ---------------------------------------------------------------- 路径小工具


def rel_to_root(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


def ensure_run(run_id: str) -> Path:
    target = run_dir(run_id)
    if not target.exists():
        raise EvalError(f"run 不存在：{run_id}。先执行 new-run。")
    return target
