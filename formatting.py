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
        for seg, is_table in _split_tables(part):
            out.append(_render_table(seg) if is_table else _inline(seg))
    return "".join(out).strip()


def _inline(part: str, block: bool = True) -> str:
    """Render a non-code, non-table stretch of markdown. block=False (table
    cells) skips line-level syntax: headings, bullets, rules, quotes."""
    t = html.escape(part)
    if block:
        t = re.sub(r"^\s*([-*_])(\s*\1){2,}\s*$", "──────────", t, flags=re.MULTILINE)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*\n][^*]*?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", t)
    t = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", t)
    # Markdown links become "text: url" with the bare URL visible. A
    # hidden-text anchor from a bot makes every Telegram client show an
    # "Open this link?" prompt; a plain URL opens directly. If the text
    # already is the URL, print it once.
    t = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)}: {m.group(2)}",
        t,
    )
    if not block:
        return t
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.MULTILINE)
    t = re.sub(r"^(\s*)[-*]\s+\[ \]\s+", r"\1☐ ", t, flags=re.MULTILINE)
    t = re.sub(r"^(\s*)[-*]\s+\[[xX]\]\s+", r"\1☑ ", t, flags=re.MULTILINE)
    t = re.sub(r"^(\s*)[-*]\s+", r"\1• ", t, flags=re.MULTILINE)
    # consecutive "> " lines become one blockquote
    t = re.sub(
        r"(?:^&gt; ?.*(?:\n|$))+",
        lambda m: "<blockquote>" + re.sub(r"^&gt; ?", "", m.group(0).rstrip("\n"),
                                          flags=re.MULTILINE) + "</blockquote>\n",
        t, flags=re.MULTILINE)
    return t


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
TABLE_PRE_WIDTH = 36  # widest table rendered as an aligned <pre> on a phone


def _split_tables(text: str) -> list[tuple[str, bool]]:
    """Split text into (segment, is_table) runs. A table is a header row,
    a |---| separator row, and any following | rows."""
    lines = text.split("\n")
    segs, buf, i = [], [], 0
    while i < len(lines):
        if (_TABLE_ROW.match(lines[i]) and i + 1 < len(lines)
                and _TABLE_SEP.match(lines[i + 1])):
            j = i + 2
            while j < len(lines) and _TABLE_ROW.match(lines[j]):
                j += 1
            if buf:
                segs.append(("\n".join(buf) + "\n", False))
                buf = []
            segs.append(("\n".join([lines[i]] + lines[i + 2:j]), True))
            i = j
            if i < len(lines):
                buf.append("")  # keep the newline after the table
            continue
        buf.append(lines[i])
        i += 1
    if buf:
        segs.append(("\n".join(buf), False))
    return segs


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", row)]


def _plain(cell: str) -> str:
    """Strip inline markdown from a cell for monospace rendering."""
    cell = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1", cell)
    cell = re.sub(r"(\*\*|__|~~|`)", "", cell)
    return cell.replace("\\|", "|")


def _render_table(table: str) -> str:
    """Telegram has no tables. Narrow ones become an aligned <pre> block;
    wider ones become one bullet per row: first cell bold, then the other
    cells labelled with their header."""
    rows = [_cells(r) for r in table.split("\n") if r.strip()]
    header, body = rows[0], rows[1:]
    ncol = len(header)
    body = [(r + [""] * ncol)[:ncol] for r in body]
    plain = [[_plain(c) for c in r] for r in [header] + body]
    widths = [max(len(r[k]) for r in plain) for k in range(ncol)]
    if sum(widths) + 2 * (ncol - 1) <= TABLE_PRE_WIDTH:
        lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
                 for r in plain]
        lines.insert(1, "  ".join("─" * w for w in widths))
        return f"<pre>{html.escape(chr(10).join(lines))}</pre>"
    out = []
    for r in body:
        first = _inline(r[0], block=False).strip()
        rest = [f"{_inline(h, block=False)}: {_inline(c, block=False)}"
                for h, c in zip(header[1:], r[1:]) if c]
        line = f"• <b>{first}</b>" if first else "•"
        if rest:
            line += "\n    " + "\n    ".join(rest)
        out.append(line)
    return "\n".join(out)


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
