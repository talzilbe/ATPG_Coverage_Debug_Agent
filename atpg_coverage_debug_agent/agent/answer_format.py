"""Turn the agent's Markdown answer into a layered, foldable view.

The model still writes the whole analysis; this module only decides what the
reader sees first. Nothing is dropped: every line of the answer lands in some
section, text before the first recognised heading is kept as its own section,
and an answer with no recognised structure is rendered whole.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

#: Takes HTML-escaped text and returns it with anchors added (fault links).
Linkify = Callable[[str], str]

GUARDRAIL_MARKER = "**Guardrail check on the answer above:**"

#: Display order. The model is asked for the same order, but the view does
#: not depend on it.
SECTION_ORDER = ["verdict", "actions", "notes", "corrections", "gaps",
                 "patterns", "review", "preface"]

SECTION_TITLES = {
    "verdict": "Verdict",
    "actions": "Next actions",
    "notes": "Findings",
    "corrections": "Corrections to the computed analysis",
    "gaps": "Evidence gaps that change the answer",
    "patterns": "Cross-cutting patterns",
    "review": "Fix plan review",
    "preface": "Text before the verdict",
}

_COUNT_NOUN = {
    "notes": ("finding", "findings"),
    "corrections": ("correction", "corrections"),
    "gaps": ("evidence gap", "evidence gaps"),
    "patterns": ("pattern", "patterns"),
    "review": ("plan-review item", "plan-review items"),
}

_KEYWORDS = [
    ("verdict", r"verdict"),
    ("actions", r"(?:next\s+|top\s+)?actions?"),
    ("notes", r"(?:detailed\s+)?debug\s+notes|findings"),
    ("corrections", r"corrections?(?:\s+to\s+the\s+computed\s+analysis)?"),
    ("gaps", r"evidence\s+gaps(?:\s+that\s+change\s+the\s+answer)?"),
    ("patterns", r"cross[-\s]cutting\s+patterns"),
    ("review", r"fix[-\s]plan\s+review"),
]

_HEADING_RE = re.compile(
    r"^\s*(?P<hash>#{1,3}\s+)?(?P<star>\*\*)?\s*"
    r"(?:(?P<letter>[A-F])[.)]\s*)?"
    r"(?P<kw>" + "|".join(f"(?P<{k}>{p})" for k, p in _KEYWORDS) + r")"
    r"(?![\w-])(?P<tail>[^:\n\u2014\u2013*]{0,40}?)"
    r"\s*(?:\*\*)?\s*(?:[:\u2014\u2013]\s*(?:\*\*)?\s*(?P<rest>.*?))?\s*$",
    re.IGNORECASE)

_ITEM_RE = re.compile(
    r"^\s*(?:#{3,4}\s+(?:\*\*)?|\*\*)\s*(?P<id>E\d+)\b\s*\**\s*"
    r"[:.\u2014\u2013-]?\s*(?P<head>.*?)\s*(?:\*\*)?\s*$")
_EVIDENCE_RE = re.compile(
    r"^\s*(?:#{3,5}\s*|\*\*)\s*Evidence\b[^\n]*$", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(
    r"^\s*[-*]?\s*\**\s*confidence\s*\**\s*[:=]\s*\**\s*"
    r"(?P<level>high|medium|reduced|insufficient|low)\b.*$", re.IGNORECASE)
_EMPTY_RE = re.compile(
    r"^\s*(?:none|n/?a|nothing(?:\s+to\s+report)?|no\s+(?:corrections?|"
    r"(?:additional\s+|new\s+|further\s+)?(?:evidence\s+)?gaps?|"
    r"(?:cross[-\s]cutting\s+)?patterns?|changes?|issues?|disagreements?|"
    r"re-?rankings?))\b[^\n]{0,120}$", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s{0,1}(?:[-*+]|\d+[.)])\s+\S")


@dataclass
class Item:
    """One finding under the debug notes: a headline that unfolds."""

    key: str
    headline: str
    body: str = ""
    evidence: str = ""


@dataclass
class Section:
    key: str
    title: str
    body: str = ""
    items: List[Item] = field(default_factory=list)
    empty: bool = False

    @property
    def count(self) -> Optional[int]:
        if self.key == "notes":
            return len(self.items) or None
        return _count_entries(self.body) or None


@dataclass
class StructuredAnswer:
    text: str
    sections: Dict[str, Section] = field(default_factory=dict)
    confidence: str = ""
    guardrail: List[str] = field(default_factory=list)

    @property
    def structured(self) -> bool:
        return "verdict" in self.sections

    def ordered(self) -> List[Section]:
        return [self.sections[k] for k in SECTION_ORDER if k in self.sections]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _match_heading(line: str) -> Tuple[Optional[str], str]:
    m = _HEADING_RE.match(line)
    if not m or not (m.group("hash") or m.group("star") or m.group("letter")):
        return None, ""
    # "**Findings** from x: ..." is prose; only a numbered/# heading may run on.
    if (m.group("tail") or "").strip() and not (m.group("hash")
                                                or m.group("letter")):
        return None, ""
    for key, _pattern in _KEYWORDS:
        if m.group(key):
            return key, (m.group("rest") or "").strip()
    return None, ""


def _split_guardrail(text: str) -> Tuple[str, List[str]]:
    idx = text.find(GUARDRAIL_MARKER)
    if idx < 0:
        return text, []
    head = text[:idx].rstrip()
    if head.endswith("---"):
        head = head[:-3].rstrip()
    lines = [ln.strip()[2:].strip() if ln.strip().startswith("- ")
             else ln.strip()
             for ln in text[idx + len(GUARDRAIL_MARKER):].splitlines()]
    return head, [ln for ln in lines if ln]


def _count_entries(body: str) -> int:
    lines = body.splitlines()
    bullets = sum(1 for ln in lines if _LIST_ITEM_RE.match(ln))
    rows = [ln for ln in lines if ln.strip().startswith("|")]
    table_rows = max(0, len([r for r in rows if not _TABLE_SEP.match(r)]) - 1)
    return bullets + table_rows


def _split_items(body: str) -> Tuple[str, List[Item]]:
    intro: List[str] = []
    items: List[Item] = []
    current: Optional[Item] = None
    buf: List[str] = []
    in_evidence = False

    def close() -> None:
        if current is None:
            return
        text = "\n".join(buf).strip()
        if in_evidence:
            current.evidence = text
        else:
            current.body = text

    for line in body.splitlines():
        m = _ITEM_RE.match(line)
        if m:
            close()
            current = Item(key=m.group("id").upper(),
                           headline=m.group("head").strip(" *:"))
            items.append(current)
            buf, in_evidence = [], False
            continue
        if current is not None and not in_evidence and _EVIDENCE_RE.match(line):
            current.body = "\n".join(buf).strip()
            buf, in_evidence = [], True
            continue
        (buf if current is not None else intro).append(line)
    close()
    return "\n".join(intro).strip(), items


def parse_answer(text: str) -> StructuredAnswer:
    """Split *text* into the sections the view folds; never loses a line."""
    body, guardrail = _split_guardrail(text or "")
    answer = StructuredAnswer(text=text or "", guardrail=guardrail)
    raw: Dict[str, List[str]] = {}
    current = "preface"
    for line in body.splitlines():
        key, rest = _match_heading(line)
        if key:
            current = key
            raw.setdefault(key, [])
            if rest:
                raw[key].append(rest)
            continue
        raw.setdefault(current, []).append(line)

    for key, lines in raw.items():
        if key == "verdict":
            kept = []
            for ln in lines:
                m = _CONFIDENCE_RE.match(ln)
                if m and not answer.confidence:
                    answer.confidence = m.group("level").lower()
                else:
                    kept.append(ln)
            lines = kept
        text_block = "\n".join(lines).strip()
        if key == "preface" and not text_block:
            continue
        section = Section(key=key, title=SECTION_TITLES[key], body=text_block)
        if key == "notes":
            section.body, section.items = _split_items(text_block)
        section.empty = (key not in ("verdict", "preface") and not section.items
                         and (not text_block or (
                             "\n" not in text_block
                             and _EMPTY_RE.match(text_block) is not None)))
        answer.sections[key] = section
    return answer


def default_expanded(answer: StructuredAnswer) -> Set[str]:
    """What is unfolded when a new answer arrives."""
    keys = {"notes", "corrections"}
    notes = answer.sections.get("notes")
    if notes and notes.items:
        keys.add(f"note:{notes.items[0].key}")
    return keys


def all_keys(answer: StructuredAnswer) -> Set[str]:
    keys = set(answer.sections)
    notes = answer.sections.get("notes")
    for item in (notes.items if notes else []):
        keys.add(f"note:{item.key}")
        keys.add(f"ev:{item.key}")
    return keys


# ---------------------------------------------------------------------------
# Markdown -> Qt rich-text HTML
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![\w*])")
_CODE_STYLE = "font-family:monospace;background-color:#f2f2f2;"


def _escape(text: str, linkify: Optional[Linkify]) -> str:
    esc = html.escape(text)
    return linkify(esc) if linkify else esc


def inline(text: str, linkify: Optional[Linkify] = None) -> str:
    out = []
    for part in re.split(r"(`[^`\n]+`)", text):
        if len(part) >= 2 and part[0] == "`" and part[-1] == "`":
            out.append(f'<span style="{_CODE_STYLE}">'
                       f'{_escape(part[1:-1], linkify)}</span>')
        else:
            esc = _escape(part, linkify)
            esc = _BOLD.sub(r"<b>\1</b>", esc)
            out.append(_ITALIC.sub(r"<i>\1</i>", esc))
    return "".join(out)


def _cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _list_html(items: List[Tuple[int, str, str]],
               linkify: Optional[Linkify]) -> str:
    out: List[str] = []
    stack: List[Tuple[int, str]] = []
    for depth, tag, text in items:
        while stack and depth < stack[-1][0]:
            out.append(f"</{stack.pop()[1]}>")
        if stack and depth == stack[-1][0] and tag != stack[-1][1]:
            out.append(f"</{stack.pop()[1]}>")
        if not stack or depth > stack[-1][0]:
            out.append(f"<{tag}>")
            stack.append((depth, tag))
        out.append(f"<li>{inline(text, linkify)}</li>")
    while stack:
        out.append(f"</{stack.pop()[1]}>")
    return "".join(out)


def markdown_to_html(md: str, linkify: Optional[Linkify] = None) -> str:
    """Render the Markdown subset models write (headings, lists, tables,
    code, emphasis) as rich text a QTextBrowser displays."""
    lines = (md or "").splitlines()
    out: List[str] = []
    para: List[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + "<br>".join(inline(p, linkify) for p in para)
                       + "</p>")
            para.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _FENCE.match(line):
            flush()
            i += 1
            code = []
            while i < n and not _FENCE.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f'<pre style="{_CODE_STYLE}">'
                       f'{_escape(chr(10).join(code), linkify)}</pre>')
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        if _HR.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            tag = {1: "h3", 2: "h3", 3: "h4"}.get(len(m.group(1)), "h5")
            out.append(f"<{tag}>{inline(m.group(2), linkify)}</{tag}>")
            i += 1
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            flush()
            head = _cells(line)
            rows = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            cells = "".join(f"<th>{inline(c, linkify)}</th>" for c in head)
            body = "".join(
                "<tr>" + "".join(f"<td>{inline(c, linkify)}</td>" for c in r)
                + "</tr>" for r in rows)
            out.append('<table border="1" cellspacing="0" cellpadding="3">'
                       f"<tr>{cells}</tr>{body}</table>")
            continue
        if _BULLET.match(line):
            flush()
            items: List[Tuple[int, str, str]] = []
            while i < n and lines[i].strip():
                bm = _BULLET.match(lines[i])
                if bm:
                    tag = "ol" if bm.group(2)[0].isdigit() else "ul"
                    items.append((len(bm.group(1).expandtabs(4)) // 2, tag,
                                  bm.group(3)))
                elif items and lines[i][:1].isspace():
                    d, t, txt = items[-1]
                    items[-1] = (d, t, txt + " " + lines[i].strip())
                else:
                    break
                i += 1
            out.append(_list_html(items, linkify))
            continue
        para.append(line)
        i += 1
    flush()
    return "".join(out)


def is_single_paragraph(md: str) -> bool:
    """True when *md* has no block structure and can sit inline after a label."""
    lines = [ln for ln in (md or "").strip().splitlines()]
    if any(not ln.strip() for ln in lines):
        return False
    for ln in lines:
        if (_FENCE.match(ln) or _HEADING.match(ln) or _BULLET.match(ln)
                or _HR.match(ln) or ln.strip().startswith("|")):
            return False
    return True


def inline_lines(md: str, linkify: Optional[Linkify] = None) -> str:
    return "<br>".join(inline(ln, linkify) for ln in (md or "").strip()
                       .splitlines())


# ---------------------------------------------------------------------------
# The layered view
# ---------------------------------------------------------------------------
_CONF_COLOURS = {"high": "#1e8449", "medium": "#b9770e", "low": "#c0392b",
                 "reduced": "#c0392b", "insufficient": "#7f8c8d"}
_LINK = "text-decoration:none;color:#036;"


def _toggle(key: str, label: str, expanded: bool, extra: str = "") -> str:
    arrow = "\u25be" if expanded else "\u25b8"
    return (f'<a name="sec-{key}"></a><a href="toggle:{key}" style="{_LINK}">'
            f"{arrow} {label}</a>{extra}")


def _plural(section: Section) -> str:
    n = section.count
    if not n:
        return ""
    one, many = _COUNT_NOUN.get(section.key, ("item", "items"))
    return f"{n} {one if n == 1 else many}"


def render_answer_html(answer: StructuredAnswer, expanded: Set[str],
                       linkify: Optional[Linkify] = None) -> str:
    """Summary card first, then one foldable line per remaining section."""
    md = lambda s: markdown_to_html(s, linkify)  # noqa: E731
    out: List[str] = []

    # -- summary card ----------------------------------------------------
    card: List[str] = []
    verdict = answer.sections.get("verdict")
    badge = ""
    if answer.confidence:
        colour = _CONF_COLOURS.get(answer.confidence, "#555")
        badge = (f' &nbsp;<span style="color:white;background-color:{colour};">'
                 f"&nbsp;confidence: {answer.confidence}&nbsp;</span>")
    card.append(f'<p><b style="font-size:large;">Verdict</b>{badge}</p>')
    if verdict is not None and verdict.body:
        card.append(md(verdict.body))
    actions = answer.sections.get("actions")
    if actions is not None and actions.body and not actions.empty:
        card.append("<p><b>Next actions</b></p>")
        card.append(md(actions.body))
    counters = []
    for section in answer.ordered():
        if section.key in ("verdict", "actions", "preface") or section.empty:
            continue
        label = _plural(section) or section.title.lower()
        counters.append(f'<a href="goto:{section.key}" style="{_LINK}">'
                        f"{label}</a>")
    nothing = [s.title.lower() for s in answer.ordered() if s.empty]
    line = " &middot; ".join(counters)
    if nothing:
        line += ((" &nbsp;|&nbsp; " if line else "")
                 + '<span style="color:#777;">nothing to report: '
                 + html.escape(", ".join(nothing)) + "</span>")
    if line:
        card.append(f"<p>{line}</p>")
    if answer.guardrail:
        rows = "".join(f"<li>{inline(g, linkify)}</li>"
                       for g in answer.guardrail)
        card.append('<p style="color:#c0392b;"><b>\u26a0 Guardrail check on '
                    f"the answer above</b> &mdash; treat these as unverified:"
                    f"</p><ul>{rows}</ul>")
    out.append('<table width="100%" cellpadding="8" cellspacing="0" '
               'style="background-color:#eef4fb;"><tr><td>'
               + "".join(card) + "</td></tr></table>")

    # -- foldable sections ---------------------------------------------
    for section in answer.ordered():
        if section.key in ("verdict", "actions") or section.empty:
            continue
        is_open = section.key in expanded
        count = _plural(section)
        extra = (f' <span style="color:#777;">({count})</span>'
                 if count else "")
        out.append("<p>" + _toggle(section.key, f"<b>{section.title}</b>",
                                   is_open, extra) + "</p>")
        if not is_open:
            continue
        block: List[str] = []
        if section.body:
            block.append(md(section.body))
        for item in section.items:
            item_open = f"note:{item.key}" in expanded
            head = (f"<b>{html.escape(item.key)}</b> &mdash; "
                    f"{inline(item.headline, linkify)}")
            block.append("<p>" + _toggle(f"note:{item.key}", head, item_open)
                         + "</p>")
            if not item_open:
                continue
            inner = [md(item.body)] if item.body else []
            if item.evidence:
                ev_open = f"ev:{item.key}" in expanded
                inner.append("<p>" + _toggle(
                    f"ev:{item.key}", "<i>Evidence</i>", ev_open) + "</p>")
                if ev_open:
                    inner.append('<div style="margin-left:14px;color:#333;">'
                                 + md(item.evidence) + "</div>")
            block.append('<div style="margin-left:18px;">' + "".join(inner)
                         + "</div>")
        out.append('<div style="margin-left:14px;">' + "".join(block)
                   + "</div>")
    return "".join(out)
