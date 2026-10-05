"""The agent's answer is laid out in layers without losing any of it."""

import os

import pytest

from atpg_coverage_debug_agent.agent import answer_format as af
from atpg_coverage_debug_agent.agent.debug_agent import (
    AGENTIC_SYSTEM_PROMPT,
    FOLLOW_UP_INSTRUCTION,
    SYSTEM_PROMPT,
    follow_up_message,
)

ANSWER = """\
## A. Verdict
Confidence: medium
The actionable loss is dominated by a tie cell feeding top/u_alu/reg_a.

## Actions
1. Apply S5 #1 (see E1).
2. Re-run ATPG to measure.

## E. Detailed Debug Notes
### E1: Tie-high upstream blocks the SE pin
The SE pin is driven by a constant 4 levels up.
#### Evidence
- Observed: `u_tie TIEHIX1 (.Y(n1));`
- Derived: n1 reaches reg_a/SE.
### E2: Second finding headline
Body two.

## C. Corrections To The Computed Analysis
| site | computed | mine | evidence |
|---|---|---|---|
| top/u_alu/reg_a/SE | scan_boundary | tied_constant | E1 |

## B. Evidence Gaps That Change The Answer
- Missing SDC -> blocks clock claims -> provide the .sdc

## D. Cross-Cutting Patterns
No cross-cutting patterns found.

## F. Fix Plan Review
- S5 #3 rejected: reconvergent cone, abort budget is wasted.
"""


def test_every_section_is_recognised():
    ans = af.parse_answer(ANSWER)
    assert ans.structured
    assert ans.confidence == "medium"
    assert set(ans.sections) == {"verdict", "actions", "notes", "corrections",
                                 "gaps", "patterns", "review"}
    assert "Confidence" not in ans.sections["verdict"].body


def test_findings_split_into_headline_body_and_evidence():
    notes = af.parse_answer(ANSWER).sections["notes"]
    assert [i.key for i in notes.items] == ["E1", "E2"]
    e1 = notes.items[0]
    assert e1.headline == "Tie-high upstream blocks the SE pin"
    assert "constant 4 levels up" in e1.body
    assert "TIEHIX1" in e1.evidence and "TIEHIX1" not in e1.body


def test_counts_and_empty_sections():
    ans = af.parse_answer(ANSWER)
    assert ans.sections["corrections"].count == 1
    assert ans.sections["gaps"].count == 1
    assert ans.sections["patterns"].empty
    assert not ans.sections["review"].empty


def test_no_line_of_the_answer_is_lost():
    ans = af.parse_answer(ANSWER)
    html = af.render_answer_html(ans, af.all_keys(ans))
    for needle in ("tie cell feeding", "Apply S5 #1", "constant 4 levels up",
                   "TIEHIX1", "Body two", "scan_boundary", "Missing SDC",
                   "abort budget is wasted"):
        assert needle in html, needle


def test_the_default_view_folds_the_detail_away():
    ans = af.parse_answer(ANSWER)
    html = af.render_answer_html(ans, af.default_expanded(ans))
    assert "tie cell feeding" in html           # verdict always visible
    assert "Re-run ATPG" in html                # actions always visible
    assert "constant 4 levels up" in html       # first finding unfolded
    assert "TIEHIX1" not in html                # its evidence folded
    assert "Body two" not in html               # later findings folded
    assert "Second finding headline" in html    # ... but their headline shows
    assert "Missing SDC" not in html            # gaps folded
    assert "nothing to report" in html and "cross-cutting" in html


def test_text_before_the_verdict_is_kept():
    ans = af.parse_answer("I called report_context first.\n\n## A. Verdict\n"
                          "Tie.")
    assert ans.sections["preface"].body.startswith("I called")
    html = af.render_answer_html(ans, af.all_keys(ans))
    assert "I called report_context" in html


@pytest.mark.parametrize("line,key", [
    ("**A. Verdict:** the loss is a tie.", "verdict"),
    ("A. Verdict: the loss is a tie.", "verdict"),
    ("### C. Corrections", "corrections"),
    ("**Fix Plan Review**", "review"),
])
def test_heading_variants(line, key):
    assert af._match_heading(line)[0] == key


@pytest.mark.parametrize("line", [
    "**Findings** from report_context: two categories",
    "The verdict is that the tie dominates.",
    "Actions were taken by the tool.",
])
def test_prose_is_not_a_heading(line):
    assert af._match_heading(line)[0] is None


def test_unstructured_text_is_rendered_whole():
    ans = af.parse_answer("Just a note about top/x.")
    assert not ans.structured
    assert "Just a note" in af.markdown_to_html(ans.text)


def test_guardrail_notice_reaches_the_card():
    text = (ANSWER + "\n---\n**Guardrail check on the answer above:**\n\n"
            "- unmeasured claim: 'will recover 12%'")
    ans = af.parse_answer(text)
    assert ans.guardrail == ["unmeasured claim: 'will recover 12%'"]
    assert "Guardrail" not in ans.sections["review"].body
    html = af.render_answer_html(ans, set())
    assert "Guardrail check" in html and "12%" in html


def test_markdown_renders_tables_lists_code_and_links():
    html = af.markdown_to_html(
        "**bold** and `top/a/b`\n\n- one\n  - nested\n\n| a | b |\n|---|---|\n"
        "| 1 | 2 |", linkify=lambda s: s.replace("top/a/b", "<a>top/a/b</a>"))
    assert "<b>bold</b>" in html
    assert "<a>top/a/b</a>" in html
    assert html.count("<ul>") == 2
    assert "<table" in html and "<td>2</td>" in html
    assert "**" not in html and "|---|" not in html


# -- prompt contract ---------------------------------------------------------
def test_the_prompt_asks_for_the_layered_format():
    for needle in ("## A. Verdict", "## Actions", "### E1: <headline>",
                   "#### Evidence", "OMIT a section that would be empty",
                   "never what you\n  check"):
        assert needle in SYSTEM_PROMPT, needle
    assert "Run the self-check silently" in SYSTEM_PROMPT
    assert "section 7" in AGENTIC_SYSTEM_PROMPT


def test_follow_ups_are_told_to_answer_directly():
    sent = follow_up_message("why?")
    assert sent.startswith(FOLLOW_UP_INSTRUCTION) and sent.endswith("why?")
    assert follow_up_message(sent) == sent
    assert "self-check" in FOLLOW_UP_INSTRUCTION


# -- the panel ---------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def panel(qapp):
    pytest.importorskip("PySide6")
    from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel
    p = AgentPanel()
    yield p
    p.shutdown()


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_panel_folds_and_unfolds(panel):
    from PySide6.QtCore import QUrl

    panel._set_response(ANSWER)
    shown = panel.response_view.toPlainText()
    assert "tie cell feeding" in shown and "TIEHIX1" not in shown
    panel._on_anchor_clicked(QUrl("toggle:ev:E1"))
    assert "TIEHIX1" in panel.response_view.toPlainText()
    panel._on_anchor_clicked(QUrl("toggle:__none__"))
    assert "constant 4 levels up" not in panel.response_view.toPlainText()
    panel._on_anchor_clicked(QUrl("toggle:__all__"))
    assert "Body two" in panel.response_view.toPlainText()
    panel._on_anchor_clicked(QUrl("goto:gaps"))
    assert "Missing SDC" in panel.response_view.toPlainText()


def test_plain_text_view_shows_the_answer_verbatim(panel):
    from PySide6.QtCore import QUrl

    panel._set_response(ANSWER)
    panel._on_anchor_clicked(QUrl("view:raw"))
    assert "## E. Detailed Debug Notes" in panel.response_view.toPlainText()
    panel._on_anchor_clicked(QUrl("view:formatted"))
    assert "## E." not in panel.response_view.toPlainText()
    assert panel._last_response == ANSWER


def test_the_diagnosis_is_not_repeated_in_the_chat(panel):
    panel._on_finished(ANSWER)
    assert panel._chat_turns == []
    chat = panel.chat_view.toPlainText()
    assert "Agent Response pane" in chat and "tie cell feeding" not in chat
    panel._append_chat("You", "why?")
    panel._rebuild_chat_view()
    assert "Agent Response pane" in panel.chat_view.toPlainText()


def test_a_structured_chat_reply_keeps_its_layout(panel):
    panel._append_chat("Agent", "It is a tie.\n\n#### Evidence\n- `u_tie`")
    html = panel.chat_view.toHtml()
    text = panel.chat_view.toPlainText()
    assert "#### " not in text and "Evidence" in text and "u_tie" in text
    assert "<li" in html
