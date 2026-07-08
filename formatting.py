"""Convert Claude's markdown to Telegram HTML and split long messages."""
import html
import re

TG_LIMIT = 4000  # official limit is 4096; leave headroom for tags


def md_to_telegram_html(text: str) -> str:
    """Best-effort markdown -> Telegram HTML. Telegram supports only a small
    tag set (b, i, s, u, code, pre, a, blockquote)."""
    out = []
    # Handle fenced code blocks separately so we don't mangle their contents.
    parts = re.split(r"(```[\s\S]*?```)", text)
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            body = part[3:-3]
            # drop optional language line
            if "\n" in body:
                first, rest = body.split("\n", 1)
                if first.strip() and " " not in first.strip():
                    body = rest
            out.append(f"<pre>{html.escape(body.strip('\n'))}</pre>")
            continue
        t = html.escape(part)
        t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*\n][^*]*?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", t)
        t = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", t)
        t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.MULTILINE)
        t = re.sub(
            r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t
        )
        t = re.sub(r"^(\s*)[-*]\s+", r"\1• ", t, flags=re.MULTILINE)
        out.append(t)
    return "".join(out).strip()


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks, preferring paragraph breaks and
    never splitting inside a <pre> block if avoidable."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while len(text) > limit:
        window = text[:limit]
        # don't cut a <pre> block in half
        if window.count("<pre>") > window.count("</pre>"):
            cut = window.rfind("<pre>")
        else:
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return [c for c in chunks if c]


def tool_summary(tool_name: str, tool_input: dict) -> str:
    """One-line human-readable summary of a tool call."""
    name = tool_name.replace("mcp__telegram__", "telegram:")
    if tool_name == "Bash":
        detail = tool_input.get("command", "")
    elif tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        detail = tool_input.get("file_path", tool_input.get("path", ""))
    elif tool_name in ("Glob", "Grep"):
        detail = tool_input.get("pattern", "")
    elif tool_name in ("WebFetch", "WebSearch"):
        detail = tool_input.get("url", tool_input.get("query", ""))
    elif tool_name == "Task":
        detail = tool_input.get("description", "")
    else:
        detail = ", ".join(f"{k}={v}" for k, v in list(tool_input.items())[:2])
    detail = str(detail).replace("\n", " ")
    if len(detail) > 120:
        detail = detail[:117] + "..."
    return f"{name}: {detail}" if detail else name
