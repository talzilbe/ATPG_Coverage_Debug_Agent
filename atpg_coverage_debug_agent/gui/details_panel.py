"""Detail panel that explains a single selected fault result."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QTextBrowser, QWidget, QVBoxLayout

from ..models import FaultAnalysisResult


class DetailsPanel(QWidget):
    """Read-only rich-text panel showing evidence for one fault."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._browser = QTextBrowser(self)
        self._browser.setOpenExternalLinks(False)
        layout.addWidget(self._browser)
        self.clear_details()

    def clear_details(self) -> None:
        """Reset to the placeholder message."""
        self._browser.setHtml(
            "<i>Select a row in the Coverage Loss table to see details.</i>"
        )

    def show_result(self, result: FaultAnalysisResult) -> None:
        """Render *result* as HTML in the panel."""
        r = result
        html = [f"<h2>{r.fault.fault_object}</h2>"]
        html.append(f"<p><b>Fault class:</b> {r.fault.fault_class.value} "
                    f"&nbsp; <b>Raw line {r.fault.line_number}:</b> "
                    f"<code>{_esc(r.fault.raw_text)}</code></p>")
        html.append("<table cellpadding='4'>")
        html.append(_row("Mapped instance", r.mapping.instance_name or "—"))
        html.append(_row("Cell type", r.cell_type or "—"))
        html.append(_row("Mapping confidence", r.mapping.confidence.value))
        html.append(_row("Root cause", f"<b>{r.root_cause.value}</b>"))
        html.append(_row("Controllability issue",
                         "yes" if r.controllability_issue else "no"))
        html.append(_row("Observability issue",
                         "yes" if r.observability_issue else "no"))
        html.append(_row("Constraint related",
                         "yes" if r.constraint_related else "no"))
        html.append(_row("Scan boundary involved",
                         "yes" if r.scan_boundary_involved else "no"))
        html.append(_row("Immediate fan-in", _esc(", ".join(r.fan_in) or "—")))
        html.append(_row("Immediate fan-out", _esc(", ".join(r.fan_out) or "—")))
        html.append("</table>")

        html.append("<h3>Observed facts</h3>")
        html.append(_ul(r.observed_facts))
        html.append("<h3>Inferred conclusions</h3>")
        html.append(_ul(r.inferred_conclusions))
        html.append("<h3>Evidence</h3>")
        html.append(_ul(r.evidence))
        if r.mapping.candidates:
            html.append("<h3>Candidate mappings</h3>")
            html.append(_ul(r.mapping.candidates))
        html.append("<h3>Recommended next step</h3>")
        html.append(f"<p>{_esc(r.recommended_step)}</p>")
        self._browser.setHtml("".join(html))


def _row(label: str, value: str) -> str:
    return f"<tr><td><b>{label}</b></td><td>{value}</td></tr>"


def _ul(items) -> str:
    if not items:
        return "<p><i>none</i></p>"
    return "<ul>" + "".join(f"<li>{_esc(str(i))}</li>" for i in items) + "</ul>"


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
