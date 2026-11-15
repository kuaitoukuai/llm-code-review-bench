"""文本处理工具（示例）"""


def split_sections(text):
    parts = text.split("\n## ")
    return [parts[0]] + ["## " + p for p in parts[1:]]


def safe_int(value, default=0):
    try:
        return int(value)
    except ValueError:
        return default


def join_lines(lines, sep="\n"):
    return sep.join(str(x).strip() for x in lines if x is not None)


def truncate(text, limit=100):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
