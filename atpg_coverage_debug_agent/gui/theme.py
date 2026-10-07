"""Light / Dark appearance for the GUI.

The widgets and the HTML they display were written with light-theme colours.
Rather than threading a palette through every renderer, :func:`adapt` rewrites
the colours of a stylesheet or HTML fragment for the active theme: light
backgrounds become dark, dark text becomes light, and hues are kept so that
green still means "ok" and red still means "loss". In the light theme it is the
identity, and exported / browser HTML never passes through it.
"""

from __future__ import annotations

import colorsys
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Dict, Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QLabel, QStyleFactory, QTableView,
                               QTextBrowser, QTreeView, QWidget)

LIGHT, DARK = "light", "dark"
THEMES = (LIGHT, DARK)
#: Font size offsets (points) allowed by View -> Zoom.
MIN_FONT_DELTA, MAX_FONT_DELTA = -3, 8

_current = LIGHT
_font_delta = 0
_base_point_size: Optional[float] = None


@dataclass(frozen=True)
class Palette:
    window: str
    base: str
    alt_base: str
    text: str
    muted: str
    button: str
    accent: str
    accent_text: str
    accent_disabled: str
    border: str
    link: str
    highlight: str
    ok: str
    warn: str
    err: str
    info: str
    tooltip_bg: str


PALETTES: Dict[str, Palette] = {
    LIGHT: Palette(
        window="#efefef", base="#ffffff", alt_base="#f7f7f7", text="#1f2933",
        muted="#555555", button="#efefef", accent="#0b5394",
        accent_text="#ffffff", accent_disabled="#9fb6cc", border="#d0e2f2",
        link="#0b5394", highlight="#308cc6", ok="#1a7f37", warn="#a05000",
        err="#c62828", info="#0b5394", tooltip_bg="#ffffdc"),
    # Modelled on VS Code's "Dark+": lighter chrome, darker content areas.
    DARK: Palette(
        window="#252526", base="#1b1b1c", alt_base="#2a2e34", text="#e2e2e2",
        muted="#a8a8a8", button="#3a3d41", accent="#0e639c",
        accent_text="#ffffff", accent_disabled="#3a4a5a", border="#555b63",
        link="#4fc1ff", highlight="#264f78", ok="#4ec970", warn="#e2a54a",
        err="#f47067", info="#4fc1ff", tooltip_bg="#2d2d30"),
}

#: Header / grid colours used by the dark stylesheets.
_DARK_HEADER_BG = "#343a42"
_DARK_GRID = "#454b53"


def _dark_qss(p: "Palette", icons: str) -> str:
    """Widget borders Fusion draws too faintly on a dark window."""
    return f"""
QAbstractScrollArea {{ border: 1px solid {p.border}; }}
QScrollArea {{ border: none; }}
QTableView, QTreeView, QListView {{
    gridline-color: {_DARK_GRID}; alternate-background-color: {p.alt_base};
    selection-background-color: {p.highlight}; selection-color: #ffffff; }}
QHeaderView::section {{
    background-color: {_DARK_HEADER_BG}; color: #f0f0f0; padding: 4px 6px;
    border: none; border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border}; font-weight: bold; }}
QTableCornerButton::section {{ background-color: {_DARK_HEADER_BG};
    border: none; border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border}; }}
QLineEdit, QPlainTextEdit, QSpinBox {{ border: 1px solid {p.border};
    border-radius: 3px; background-color: {p.base}; }}
QLineEdit {{ padding: 2px 4px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {p.accent}; }}
QGroupBox {{ border: 1px solid {p.border}; border-radius: 4px;
    margin-top: 12px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
QTabWidget::pane {{ border: 1px solid {p.border}; top: -1px; }}
QTabBar::tab {{ background-color: #2d2d30; color: #c8c8c8;
    border: 1px solid {p.border}; border-bottom: none; padding: 5px 12px;
    margin-right: 1px; }}
QTabBar::tab:selected {{ background-color: {p.base}; color: #ffffff;
    border-top: 2px solid {p.accent}; }}
QTabBar::tab:hover:!selected {{ background-color: #3a3d41; }}
QSplitter::handle {{ background-color: {p.border}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
QStatusBar {{ border-top: 1px solid {p.border}; }}
QMenu {{ border: 1px solid {p.border}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}
QToolTip {{ color: {p.text}; background-color: {p.tooltip_bg};
    border: 1px solid {p.border}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 13px; height: 13px;
    border: 1px solid #8a9099; background-color: {p.base}; }}
QCheckBox::indicator {{ border-radius: 2px; }}
QRadioButton::indicator {{ border-radius: 7px; }}
QCheckBox::indicator:checked {{ image: url({icons}/check.png); }}
QCheckBox::indicator:indeterminate {{ image: url({icons}/partial.png); }}
QRadioButton::indicator:checked {{ image: url({icons}/dot.png); }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p.link}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: #4a4f57; background-color: {p.window}; }}
"""


def _indicator_icons(colour: str) -> str:
    """Write the tick / dot images the dark stylesheet points at; return the dir.

    Styling an indicator in a stylesheet drops the style's own tick, so one is
    drawn here, in the same shape Fusion uses in the light theme.
    """
    folder = os.path.join(tempfile.gettempdir(),
                          f"atpg_debug_theme_{os.getuid()}")
    os.makedirs(folder, exist_ok=True)
    size = 11

    def _draw(name: str, paint) -> None:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        paint(painter)
        painter.end()
        pixmap.save(os.path.join(folder, name), "PNG")

    def _tick(painter: QPainter) -> None:
        painter.setPen(QPen(QColor(colour), 1.8, Qt.SolidLine, Qt.RoundCap,
                            Qt.RoundJoin))
        painter.drawPolyline([QPointF(2.0, 5.6), QPointF(4.4, 8.2),
                              QPointF(9.0, 2.6)])

    def _dash(painter: QPainter) -> None:
        painter.setPen(QPen(QColor(colour), 1.8, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(2.5, 5.5), QPointF(8.5, 5.5))

    def _dot(painter: QPainter) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(QPointF(5.5, 5.5), 2.8, 2.8)

    _draw("check.png", _tick)
    _draw("partial.png", _dash)
    _draw("dot.png", _dot)
    return folder.replace(os.sep, "/")


#: Default rules for rich-text views: Qt draws HTML tables with no visible
#: grid otherwise, and they melt into a dark page.
_DARK_HTML_CSS = f"""
table {{ border-collapse: collapse; }}
th {{ background-color: {_DARK_HEADER_BG}; color: #f0f0f0; }}
td, th {{ border-width: 1px; border-style: solid; border-color: {_DARK_GRID};
    padding: 3px 6px; }}
"""


def current() -> str:
    return _current


def is_dark() -> bool:
    return _current == DARK


def palette() -> Palette:
    return PALETTES[_current]


def color(token: str) -> str:
    """A named colour of the active theme, e.g. ``color("ok")``."""
    return getattr(palette(), token)


def set_current(name: str) -> str:
    """Make *name* the active theme without touching any widget."""
    global _current
    _current = name if name in THEMES else LIGHT
    return _current


def clamp_font_delta(delta) -> int:
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        delta = 0
    return max(MIN_FONT_DELTA, min(MAX_FONT_DELTA, delta))


def font_delta() -> int:
    return _font_delta


# ---------------------------------------------------------------------------
# Colour adaptation
# ---------------------------------------------------------------------------
_NAMED = {"white": "#ffffff", "black": "#000000"}
_HEX = r"(?<![&\w])#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])"
_COLOUR_RE = re.compile(_HEX + r"|\b(?:white|black)\b", re.IGNORECASE)
# One CSS declaration: property name, then its value up to ; " ' } or <.
_DECL_RE = re.compile(r"([A-Za-z-]+)(\s*:\s*)([^;\"'}<>]+)")
_ATTR_RE = re.compile(r"\b(bgcolor|color)(\s*=\s*[\"'])([^\"']+)", re.IGNORECASE)


def _to_rgb(value: str):
    value = _NAMED.get(value.lower(), value).lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)


def luminance(value: str) -> float:
    """HLS lightness of a ``#rgb`` / ``#rrggbb`` / named colour, 0..1."""
    r, g, b = _to_rgb(value)
    return colorsys.rgb_to_hls(r, g, b)[1]


def adapt_color(value: str, role: str = "fg") -> str:
    """*value* as it should look in the active theme.

    ``role`` is ``bg`` (a fill), ``fg`` (text) or ``border``. The hue is kept;
    only lightness (and, for fills, saturation) changes.
    """
    if not is_dark():
        return value
    try:
        r, g, b = _to_rgb(value)
    except ValueError:
        return value
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if role == "bg":
        if l < 0.5:
            return value
        # Tinted fills stay a step lighter than the page so boxes still show.
        l, s = min(0.30, 0.11 + (1.0 - l) * 1.6), s * 0.5
    elif role == "border":
        if l < 0.5:
            l = max(l, 0.42)
        else:
            l = 0.32 + (1.0 - l) * 0.5
    else:
        l = 0.62 + (0.5 - l) * 0.6 if l < 0.5 else max(l, 0.62)
    return _to_hex(colorsys.hls_to_rgb(h, l, s))


def _role_for(prop: str) -> str:
    prop = prop.lower()
    if "background" in prop or prop == "bgcolor":
        return "bg"
    if "border" in prop or "outline" in prop:
        return "border"
    return "fg"


def adapt(text: str) -> str:
    """Rewrite every colour in a stylesheet / HTML string for the active theme."""
    if not text or not is_dark() or not _COLOUR_RE.search(text):
        return text

    def _value(role: str, value: str) -> str:
        return _COLOUR_RE.sub(lambda m: adapt_color(m.group(0), role), value)

    def _decl(m: "re.Match") -> str:
        prop, sep, value = m.group(1), m.group(2), m.group(3)
        return prop + sep + _value(_role_for(prop), value)

    out = _DECL_RE.sub(_decl, text)
    return _ATTR_RE.sub(
        lambda m: m.group(1) + m.group(2) + _value(_role_for(m.group(1)),
                                                   m.group(3)), out)


def qcolor(value: str, role: str = "fg") -> QColor:
    return QColor(adapt_color(value, role))


# ---------------------------------------------------------------------------
# Applying to widgets
# ---------------------------------------------------------------------------
_RAW_HTML, _RAW_CSS, _CSS_SET = "_theme_raw_html", "_theme_raw_css", "_theme_css"
_HTML_THEME, _HTML_REV = "_theme_html_theme", "_theme_html_rev"
_RAW_TEXT, _TEXT_SET = "_theme_raw_text", "_theme_text"


def apply_document_css(view: QTextBrowser) -> None:
    """Give *view*'s document the theme's default table rules."""
    view.document().setDefaultStyleSheet(_DARK_HTML_CSS if is_dark() else "")


def set_html(view: QTextBrowser, html: str) -> None:
    """``view.setHtml(html)`` in the active theme; re-applied by :func:`refresh`."""
    apply_document_css(view)
    view.setHtml(adapt(html))
    view.setProperty(_RAW_HTML, html)
    view.setProperty(_HTML_THEME, _current)
    view.setProperty(_HTML_REV, view.document().revision())


def set_css(widget: QWidget, css: str) -> None:
    """``widget.setStyleSheet(css)`` in the active theme."""
    adapted = adapt(css)
    widget.setProperty(_RAW_CSS, css)
    widget.setProperty(_CSS_SET, adapted)
    widget.setStyleSheet(adapted)


def set_label(label: QLabel, text: str) -> None:
    """``label.setText(text)`` (rich text) in the active theme."""
    adapted = adapt(text)
    label.setProperty(_RAW_TEXT, text)
    label.setProperty(_TEXT_SET, adapted)
    label.setText(adapted)


def _refresh_widget(widget: QWidget) -> None:
    if isinstance(widget, (QTableView, QTreeView)) and \
            not widget.alternatingRowColors():
        widget.setAlternatingRowColors(True)
    css = widget.styleSheet()
    if css or widget.property(_RAW_CSS):
        # Code may have replaced the stylesheet since it was last adapted.
        raw = (widget.property(_RAW_CSS) if css == widget.property(_CSS_SET)
               else css)
        if raw:
            set_css(widget, raw)
    if isinstance(widget, QLabel):
        text = widget.text()
        if "#" in text or widget.property(_RAW_TEXT):
            raw = (widget.property(_RAW_TEXT)
                   if text == widget.property(_TEXT_SET) else text)
            set_label(widget, raw or "")
    elif isinstance(widget, QTextBrowser):
        raw = widget.property(_RAW_HTML)
        if raw is None or widget.property(_HTML_THEME) == _current:
            return
        if widget.property(_HTML_REV) != widget.document().revision():
            # Other code replaced the content; it is no longer ours to redo.
            widget.setProperty(_RAW_HTML, None)
            return
        bar = widget.verticalScrollBar()
        pos = bar.value()
        set_html(widget, raw)
        bar.setValue(pos)


def refresh(root: QWidget) -> None:
    """Re-apply the active theme to *root* and everything inside it."""
    _refresh_widget(root)
    for child in root.findChildren(QWidget):
        _refresh_widget(child)


def _qt_palette(p: Palette) -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.Window: p.window, QPalette.WindowText: p.text,
        QPalette.Base: p.base, QPalette.AlternateBase: p.alt_base,
        QPalette.Text: p.text, QPalette.Button: p.button,
        QPalette.ButtonText: p.text, QPalette.BrightText: "#ffffff",
        QPalette.Highlight: p.highlight, QPalette.HighlightedText: "#ffffff",
        QPalette.Link: p.link, QPalette.LinkVisited: p.link,
        QPalette.ToolTipBase: p.tooltip_bg, QPalette.ToolTipText: p.text,
        QPalette.PlaceholderText: p.muted,
    }
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#6d6d6d"))
    pal.setColor(QPalette.Disabled, QPalette.Button, QColor(p.alt_base))
    # Fusion derives bevels and frame edges from these.
    for role, value in ((QPalette.Light, "#5a6068"), (QPalette.Midlight, "#454b53"),
                        (QPalette.Mid, "#3a3f45"), (QPalette.Dark, "#151517"),
                        (QPalette.Shadow, "#000000")):
        pal.setColor(role, QColor(value))
    return pal


def apply_font(app: QApplication, delta: int) -> int:
    """Set the application font to its start-up size plus *delta* points."""
    global _base_point_size, _font_delta
    _font_delta = clamp_font_delta(delta)
    font = QFont(app.font())
    if _base_point_size is None:
        _base_point_size = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
    font.setPointSizeF(max(6.0, _base_point_size + _font_delta))
    app.setFont(font)
    return _font_delta


def apply_app(app: QApplication, name: str, delta: Optional[int] = None) -> str:
    """Switch the whole application to theme *name* (and font *delta*)."""
    name = set_current(name)
    style = QStyleFactory.create("Fusion")
    if style is not None:
        app.setStyle(style)
    if name == DARK:
        app.setPalette(_qt_palette(PALETTES[DARK]))
        app.setStyleSheet(_dark_qss(PALETTES[DARK],
                                    _indicator_icons(PALETTES[DARK].text)))
    else:
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")
    if delta is not None:
        apply_font(app, delta)
    return name
