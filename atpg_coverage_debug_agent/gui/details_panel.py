"""Detail panel that explains a single selected fault result."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QTextBrowser, QWidget, QVBoxLayout

from ..models import FaultAnalysisResult

#: Human labels for the tri-state scan verdict. "unknown" is never softened
#: into "non-scan" -- that substitution is what produced a wrong published
#: analysis once already.
_SCAN_LABEL = {
    "scan": "scan (scan-data input + shift-enable pins read)",
    "non_scan": "non-scan (no scan-data input, no shift-enable)",
    "unknown": "unknown \u2014 no instantiation read, or unrecognised pin names",
}


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
        html.append(_row("Scan boundary involved", r.scan_boundary_state))
        html.append(_row("Scan status (from pin list)",
                         _SCAN_LABEL.get(r.scan_cell_state,
                                         r.scan_cell_state)))
        if r.tie_driver:
            tie = r.tie_driver
            value = tie.get("value")
            html.append(_row(
                "Constant driver",
                f"<b>{_esc(tie.get('instance', ''))}</b> "
                f"(<code>{_esc(tie.get('cell_type', ''))}</code>)"
                + (f" holding constant {_esc(value)}" if value else "")
                + f", {tie.get('levels', 0)} hierarchy level(s) away "
                  f"on net <code>{_esc(tie.get('net', ''))}</code>"))
        if r.connectivity_known:
            html.append(_row("Immediate fan-in",
                             _esc(", ".join(r.fan_in) or "—")))
            html.append(_row("Immediate fan-out",
                             _esc(", ".join(r.fan_out) or "—")))
        else:
            unknown = ("<i>unknown — this object never mapped onto the "
                       "netlist, so no connectivity was measured</i>")
            html.append(_row("Immediate fan-in", unknown))
            html.append(_row("Immediate fan-out", unknown))
        html.append("</table>")

        html.append("<h3>Observed facts</h3>")
        html.append(_ul(r.observed_facts))
        if r.scan_evidence:
            html.append("<h3>Instantiation read from the netlist</h3>")
            html.append(f"<pre>{_esc(r.scan_evidence)}</pre>")
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
