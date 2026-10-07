"""Every keyboard shortcut in one table: the menus and the User Guide read it."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import List

WINDOW = "Anywhere in the main window"


@dataclass(frozen=True)
class Shortcut:
    id: str
    keys: str
    action: str
    where: str = WINDOW


SHORTCUTS: List[Shortcut] = [
    Shortcut("analyze", "Ctrl+R", "Analyze the inputs (\u25b6 Analyze)"),
    Shortcut("cancel", "Esc", "Cancel the running analysis"),
    Shortcut("load_report", "Ctrl+O", "Load a saved report"),
    Shortcut("save_report", "Ctrl+S", "Save the report (JSON)"),
    Shortcut("export_md", "Ctrl+E", "Export the Markdown report"),
    Shortcut("export_csv", "Ctrl+Shift+E", "Export the CSV table"),
    Shortcut("quit", "Ctrl+Q", "Quit"),
    Shortcut("undo", "Ctrl+Z", "Undo the last waiver / exclusion"),
    Shortcut("find", "Ctrl+F",
             "Find: the filter box on the Coverage Loss Table, a find bar on "
             "the Summary, Triage and AI agent text"),
    Shortcut("palette", "Ctrl+Shift+P",
             "Command palette: run any command by name; type @ to jump to a "
             "fault"),
    Shortcut("preferences", "Ctrl+,", "Preferences (theme, text size, ...)"),
    Shortcut("tab1", "Ctrl+1", "Go to Summary"),
    Shortcut("tab2", "Ctrl+2", "Go to Triage & Fix Plan"),
    Shortcut("tab3", "Ctrl+3", "Go to Coverage Loss Table"),
    Shortcut("tab4", "Ctrl+4", "Go to AI Debug Agent"),
    Shortcut("tab5", "Ctrl+5", "Go to Tessent Visualizer"),
    Shortcut("toggle_theme", "Ctrl+K, Ctrl+T",
             "Switch between the Light and Dark theme"),
    Shortcut("zoom_in", "Ctrl+=", "Larger text (Ctrl++ works too)"),
    Shortcut("zoom_out", "Ctrl+-", "Smaller text"),
    Shortcut("zoom_reset", "Ctrl+0", "Default text size"),
    Shortcut("maximize", "Ctrl+M", "Maximize the window"),
    Shortcut("fullscreen", "F11", "Full screen on / off"),
    Shortcut("restore", "Ctrl+Shift+M", "Restore the window to normal size"),
    Shortcut("help", "F1", "Open this User Guide"),
    Shortcut("find_next", "Enter / Shift+Enter", "Next / previous match",
             "Find bar"),
    Shortcut("find_close", "Esc", "Close the find bar", "Find bar"),
    Shortcut("palette_keys", "\u2191 / \u2193, Enter, Esc",
             "Move through the list, run the selection, close",
             "Command palette"),
    Shortcut("popout", "Ctrl+M, F11, Esc",
             "Maximize, full screen, leave full screen",
             "A popped-out agent window"),
]

_BY_ID = {s.id: s for s in SHORTCUTS}


def keys(shortcut_id: str) -> str:
    return _BY_ID[shortcut_id].keys


def window_shortcuts() -> List[Shortcut]:
    return [s for s in SHORTCUTS if s.where == WINDOW]


def help_table_html() -> str:
    """The shortcut table shown in the User Guide."""
    rows = ["<table>", "<tr><th>Keys</th><th>What it does</th>"
            "<th>Where</th></tr>"]
    for s in SHORTCUTS:
        rows.append(f"<tr><td><code>{html.escape(s.keys)}</code></td>"
                    f"<td>{html.escape(s.action)}</td>"
                    f"<td>{html.escape(s.where)}</td></tr>")
    rows.append("</table>")
    return "\n".join(rows)
