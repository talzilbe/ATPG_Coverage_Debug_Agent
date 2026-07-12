"""AI Debug Agent tab — runs the strict ATPG/DFT system prompt via an LLM."""

from __future__ import annotations

import html
import logging
import os
import re
import uuid
from typing import Optional
from urllib.parse import quote, unquote

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QThread, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..agent.debug_agent import (
    AgentConfig,
    AGENTIC_SYSTEM_PROMPT,
    DebugAgent,
    SYSTEM_PROMPT,
    build_user_payload,
    is_cli_auth_error,
)
from ..analysis import investigate
from ..skills.base import AnalysisContext

logger = logging.getLogger(__name__)

# Default location of the bundled GitHub Copilot CLI (repo tools/ dir) and its
# relocated config/state home (kept off the quota-limited user home).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_DEFAULT_CLI_PATH = os.path.join(_REPO_ROOT, "tools", "copilot-cli", "copilot")
_DEFAULT_CLI_HOME = os.environ.get(
    "COPILOT_HOME", "/nfs/site/disks/talzilbe_wa01/copilot-home")

# Suggested model ids for the GitHub Copilot CLI --model flag. This is only a
# convenience list — the combo is editable, so any id the account supports can
# be typed in. The authoritative list comes from the CLI's /model command once
# authenticated.
_CLI_MODEL_CHOICES = [
    "auto",
    "claude-opus-4.8",
    "claude-sonnet-4.8",
    "claude-opus-4.5",
    "claude-opus-4.1",
    "claude-sonnet-4.5",
    "claude-sonnet-4",
    "gpt-5",
    "gpt-5-mini",
    "gpt-4.1",
    "o3",
    "o4-mini",
]


class _AgentWorker(QObject):
    """Runs the LLM call off the GUI thread."""

    finished = Signal(str)
    failed = Signal(str)
    token = Signal(str)

    def __init__(self, agent: DebugAgent, report, session_id=None) -> None:
        super().__init__()
        self._agent = agent
        self._report = report
        self._session_id = session_id

    def run(self) -> None:
        try:
            text = self._agent.run(self._report, session_id=self._session_id,
                                   on_chunk=lambda c: self.token.emit(c))
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI debug agent failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(text)


class _AgenticWorker(QObject):
    """Runs the tool-using agentic loop off the GUI thread."""

    finished = Signal(str)
    failed = Signal(str)
    trace = Signal(str)
    token = Signal(str)

    def __init__(self, agent: DebugAgent, report, skill_manager, ctx,
                 session_id=None) -> None:
        super().__init__()
        self._agent = agent
        self._report = report
        self._skill_manager = skill_manager
        self._ctx = ctx
        self._session_id = session_id

    def run(self) -> None:
        try:
            text = self._agent.run_agentic(
                self._report,
                self._skill_manager,
                self._ctx,
                on_event=lambda msg: self.trace.emit(msg),
                session_id=self._session_id,
                on_chunk=lambda c: self.token.emit(c),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agentic AI debug agent failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(text)


class _ChatWorker(QObject):
    """Runs a single follow-up chat turn off the GUI thread."""

    finished = Signal(str)
    failed = Signal(str)
    token = Signal(str)

    def __init__(self, agent: DebugAgent, message, session_id, history) -> None:
        super().__init__()
        self._agent = agent
        self._message = message
        self._session_id = session_id
        self._history = history

    def run(self) -> None:
        try:
            text = self._agent.chat(
                self._message, session_id=self._session_id,
                history=self._history,
                on_chunk=lambda c: self.token.emit(c))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Follow-up chat failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(text)


class AgentPanel(QWidget):
    """Full 'AI Debug Agent' tab: LLM config + run + prompt/response views."""

    #: Emitted when the LLM connection settings change (for persistence).
    config_changed = Signal()

    #: Emitted with a fault-object id when the user clicks a fault link in the
    #: agent output — the main window focuses that row in the results table.
    fault_referenced = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._report = None
        self._skill_manager = None
        self._thread: Optional[QThread] = None
        self._worker = None
        self._auth_proc = None
        self._auth_buf: list = []
        self._session_id: Optional[str] = None
        self._chat_messages: list = []
        self._chat_backend: str = "cli"
        self._chat_thread: Optional[QThread] = None
        self._chat_worker = None
        self._last_response: str = ""
        self._chat_turns: list = []
        self._compare = None
        self._stream_buf: str = ""
        self._chat_stream_buf: str = ""
        self._build()

    # -- UI ------------------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        agent_tab = QWidget()
        layout = QVBoxLayout(agent_tab)

        # --- LLM connection settings ---
        cfg_box = QGroupBox("LLM Backend")
        form = QFormLayout(cfg_box)

        self.backend_combo = QComboBox()
        self.backend_combo.addItem("GitHub Copilot CLI (local subprocess)", "cli")
        self.backend_combo.addItem("OpenAI-compatible HTTP endpoint", "http")
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        form.addRow("Backend:", self.backend_combo)

        # -- Copilot CLI fields --
        self.cli_path_edit = QLineEdit(
            _DEFAULT_CLI_PATH if os.path.isfile(_DEFAULT_CLI_PATH) else "")
        self.cli_path_edit.setPlaceholderText(
            "Path to the 'copilot' executable")
        self.cli_path_edit.textChanged.connect(self._notify_config_changed)
        cli_path_row = QHBoxLayout()
        cli_path_row.addWidget(self.cli_path_edit, 1)
        self.cli_browse_btn = QPushButton("Browse…")
        self.cli_browse_btn.clicked.connect(self._on_browse_cli)
        cli_path_row.addWidget(self.cli_browse_btn)
        self.cli_path_row_widget = self._wrap(cli_path_row)
        form.addRow("Copilot CLI:", self.cli_path_row_widget)

        self.cli_home_edit = QLineEdit(_DEFAULT_CLI_HOME)
        self.cli_home_edit.setPlaceholderText(
            "COPILOT_HOME — config/state dir (kept off your quota-limited home)")
        self.cli_home_edit.textChanged.connect(self._notify_config_changed)
        form.addRow("CLI home:", self.cli_home_edit)

        self.cli_model_combo = QComboBox()
        self.cli_model_combo.setEditable(True)
        self.cli_model_combo.addItems(_CLI_MODEL_CHOICES)
        self.cli_model_combo.setCurrentText("auto")
        self.cli_model_combo.lineEdit().setPlaceholderText(
            "Model id for the CLI ('auto' lets Copilot choose)")
        self.cli_model_combo.setToolTip(
            "Pick a model for the GitHub Copilot CLI, or type any model id your "
            "account supports. 'auto' lets Copilot choose automatically.")
        self.cli_model_combo.currentTextChanged.connect(
            self._notify_config_changed)
        form.addRow("CLI model:", self.cli_model_combo)

        self.cli_mcp_check = QCheckBox(
            "Let the agent drive investigation tools (MCP)")
        self.cli_mcp_check.setChecked(True)
        self.cli_mcp_check.setToolTip(
            "In agentic mode, expose the investigative tools (list_faults, "
            "get_fault_detail, why_blocked, list_constraints, trace_path) to "
            "the Copilot CLI via a local MCP server so the model calls them "
            "itself and iterates. When off, the enabled skills are run locally "
            "and their findings are folded into a single prompt.")
        self.cli_mcp_check.toggled.connect(self._notify_config_changed)
        form.addRow("Agentic tools:", self.cli_mcp_check)

        # -- HTTP endpoint fields --
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText(
            "Internal Intel OpenAI-compatible endpoint, e.g. https://<internal-host>/v1")
        self.base_url_edit.textChanged.connect(self._notify_config_changed)
        self.base_url_row = form.rowCount()
        form.addRow("Base URL:", self.base_url_edit)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText(
            "Model id served by your internal endpoint")
        self.model_edit.textChanged.connect(self._notify_config_changed)
        form.addRow("Model:", self.model_edit)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText(
            "Bearer token — kept in memory only, never written to disk")
        form.addRow("API Key:", self.api_key_edit)

        self._form = form

        knobs = QHBoxLayout()
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setValue(0.0)
        self.temp_spin.valueChanged.connect(self._notify_config_changed)
        knobs.addWidget(QLabel("Temperature:"))
        knobs.addWidget(self.temp_spin)

        self.maxtok_spin = QSpinBox()
        self.maxtok_spin.setRange(256, 32000)
        self.maxtok_spin.setValue(4000)
        self.maxtok_spin.valueChanged.connect(self._notify_config_changed)
        knobs.addWidget(QLabel("Max tokens:"))
        knobs.addWidget(self.maxtok_spin)

        self.maxfaults_spin = QSpinBox()
        self.maxfaults_spin.setRange(10, 5000)
        self.maxfaults_spin.setValue(200)
        self.maxfaults_spin.valueChanged.connect(self._notify_config_changed)
        knobs.addWidget(QLabel("Max faults in prompt:"))
        knobs.addWidget(self.maxfaults_spin)
        knobs.addStretch(1)
        form.addRow("", self._wrap(knobs))

        layout.addWidget(cfg_box)

        # --- Mode selector ---
        mode_row = QHBoxLayout()
        self.agentic_check = QCheckBox("Agentic mode (let the agent call skills as tools)")
        self.agentic_check.setToolTip(
            "When on, the LLM decides which analysis skills to invoke, sees "
            "their findings, and iterates before writing its diagnosis.\n"
            "Requires an LLM endpoint that supports tool/function calling.")
        self.agentic_check.toggled.connect(self._on_mode_toggled)
        mode_row.addWidget(self.agentic_check)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        # --- Action buttons ---
        btns = QHBoxLayout()
        self.run_btn = QPushButton("Run AI Debug Agent")
        self.run_btn.clicked.connect(self.on_run)
        self.build_btn = QPushButton("Build Prompt Only")
        self.build_btn.clicked.connect(self.on_build_prompt)
        self.copy_prompt_btn = QPushButton("Copy Prompt")
        self.copy_prompt_btn.clicked.connect(self.on_copy_prompt)
        self.save_prompt_btn = QPushButton("Save Prompt…")
        self.save_prompt_btn.clicked.connect(self.on_save_prompt)
        self.copy_resp_btn = QPushButton("Copy Response")
        self.copy_resp_btn.clicked.connect(self.on_copy_response)
        self.save_resp_btn = QPushButton("Save Response…")
        self.save_resp_btn.clicked.connect(self.on_save_response)
        self.verify_btn = QPushButton("Verify")
        self.verify_btn.setToolTip(
            "Cross-check the agent's answer against the deterministic report: "
            "confirm every fault it references exists and show its true "
            "class / root-cause, and flag any invented fault paths.")
        self.verify_btn.clicked.connect(self.on_verify)
        self.suggest_btn = QPushButton("Suggest Fixes")
        self.suggest_btn.setToolTip(
            "Deterministically rank coverage-loss faults by impact and propose "
            "concrete DFT fixes (observation/control points, constraint "
            "relaxation, scan insertion) — no LLM used.")
        self.suggest_btn.clicked.connect(self.on_suggest_fixes)
        for b in (self.run_btn, self.build_btn, self.copy_prompt_btn,
                  self.save_prompt_btn, self.copy_resp_btn, self.save_resp_btn,
                  self.verify_btn, self.suggest_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.status_label = QLabel("Run an analysis first, then run the AI agent.")
        self.status_label.setStyleSheet("color: #555;")
        layout.addWidget(self.status_label)

        # --- Prompt / response split ---
        splitter = QSplitter(Qt.Horizontal)

        prompt_box = QGroupBox("Assembled Prompt (system + structural evidence)")
        prompt_layout = QVBoxLayout(prompt_box)
        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setPlaceholderText(
            "Click 'Build Prompt Only' to preview what will be sent to the LLM.")
        prompt_layout.addWidget(self.prompt_view)
        splitter.addWidget(prompt_box)

        trace_box = QGroupBox("Agent Tool Trace / Verification")
        trace_layout = QVBoxLayout(trace_box)
        self.trace_view = QPlainTextEdit()
        self.trace_view.setReadOnly(True)
        self.trace_view.setPlaceholderText(
            "In agentic mode, each skill/tool the agent calls is logged here "
            "as it runs. 'Verify' results also appear here.")
        trace_layout.addWidget(self.trace_view)
        splitter.addWidget(trace_box)

        resp_box = QGroupBox("Agent Response (click a fault to focus it in the table)")
        resp_layout = QVBoxLayout(resp_box)
        self.response_view = QTextBrowser()
        self.response_view.setOpenLinks(False)
        self.response_view.setOpenExternalLinks(False)
        self.response_view.anchorClicked.connect(self._on_anchor_clicked)
        self.response_view.setPlaceholderText(
            "The agent's evidence-driven A–F diagnosis appears here. Fault ids "
            "are clickable.")
        resp_layout.addWidget(self.response_view)
        splitter.addWidget(resp_box)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 3)
        layout.addWidget(splitter, 1)

        # --- Follow-up chat ---
        chat_box = QGroupBox("Follow-up Chat with the Agent")
        chat_layout = QVBoxLayout(chat_box)
        self.chat_view = QTextBrowser()
        self.chat_view.setOpenLinks(False)
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.anchorClicked.connect(self._on_anchor_clicked)
        self.chat_view.setPlaceholderText(
            "After you run the agent, ask follow-up questions about its "
            "diagnosis here — the conversation keeps the full analysis context.")
        chat_layout.addWidget(self.chat_view, 1)

        chat_row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText(
            "Run the agent first, then ask a follow-up question…")
        self.chat_input.returnPressed.connect(self.on_send_chat)
        self.chat_send_btn = QPushButton("Send")
        self.chat_send_btn.clicked.connect(self.on_send_chat)
        self.chat_clear_btn = QPushButton("Clear chat")
        self.chat_clear_btn.clicked.connect(self.on_clear_chat)
        chat_row.addWidget(self.chat_input, 1)
        chat_row.addWidget(self.chat_send_btn)
        chat_row.addWidget(self.chat_clear_btn)
        chat_layout.addLayout(chat_row)
        layout.addWidget(chat_box, 1)

        self._set_chat_enabled(False)
        self._update_button_state()
        self._on_backend_changed()

        self.tabs.addTab(agent_tab, "Debug Agent")
        self.tabs.addTab(self._build_auth_tab(), "Authentication")

    def _build_auth_tab(self) -> QWidget:
        """Authentication tab for the GitHub Copilot CLI backend."""
        tab = QWidget()
        v = QVBoxLayout(tab)

        info = QLabel(
            "The GitHub Copilot CLI backend needs GitHub authentication. Use "
            "<b>either</b> option below. Credentials are never written to this "
            "app's settings — a pasted token is kept in memory only; the device "
            "login stores its token in the CLI home shown on the Debug Agent tab.")
        info.setWordWrap(True)
        v.addWidget(info)

        # -- Option A: token --
        tok_box = QGroupBox("Option A — GitHub token (headless)")
        tok_form = QFormLayout(tok_box)
        self.auth_token_edit = QLineEdit()
        self.auth_token_edit.setEchoMode(QLineEdit.Password)
        self.auth_token_edit.setPlaceholderText(
            "Fine-grained PAT with 'Copilot Requests' permission, or an OAuth token")
        self.auth_token_edit.textChanged.connect(self._notify_config_changed)
        tok_form.addRow("GitHub token:", self.auth_token_edit)
        tok_note = QLabel(
            "Injected as COPILOT_GITHUB_TOKEN for CLI runs. Classic ghp_ tokens "
            "are not supported.")
        tok_note.setWordWrap(True)
        tok_note.setStyleSheet("color: #666;")
        tok_form.addRow("", tok_note)
        v.addWidget(tok_box)

        # -- Option B: device login --
        dev_box = QGroupBox("Option B — Sign in with a browser (device code)")
        dev_layout = QVBoxLayout(dev_box)
        dev_desc = QLabel(
            "Runs 'copilot login'. A one-time code and URL will appear below — "
            "open the URL in a browser and enter the code to authorize.")
        dev_desc.setWordWrap(True)
        dev_layout.addWidget(dev_desc)
        dev_warn = QLabel(
            "Note: on a headless host with no system keychain, the browser login "
            "can succeed but fail to <b>save</b> the token (it can't answer the "
            "plaintext-storage prompt). If that happens, use Option A instead.")
        dev_warn.setWordWrap(True)
        dev_warn.setStyleSheet("color: #8a5a00;")
        dev_layout.addWidget(dev_warn)
        v.addWidget(dev_box)

        # -- Buttons --
        btns = QHBoxLayout()
        self.auth_check_btn = QPushButton("Check authentication")
        self.auth_check_btn.clicked.connect(self.on_check_auth)
        self.auth_login_btn = QPushButton("Sign in with device code…")
        self.auth_login_btn.clicked.connect(self.on_device_login)
        self.auth_cancel_btn = QPushButton("Cancel sign-in")
        self.auth_cancel_btn.clicked.connect(self.on_cancel_login)
        self.auth_cancel_btn.setEnabled(False)
        for b in (self.auth_check_btn, self.auth_login_btn, self.auth_cancel_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)

        self.auth_status_label = QLabel("")
        self.auth_status_label.setWordWrap(True)
        self.auth_status_label.setStyleSheet("color: #555;")
        v.addWidget(self.auth_status_label)

        self.auth_log = QPlainTextEdit()
        self.auth_log.setReadOnly(True)
        self.auth_log.setPlaceholderText(
            "Authentication output (device code, URL, results) appears here.")
        v.addWidget(self.auth_log, 1)
        return tab

    @staticmethod
    def _wrap(inner_layout) -> QWidget:
        w = QWidget()
        w.setLayout(inner_layout)
        return w

    def _notify_config_changed(self, *args) -> None:
        """Zero-arg-safe relay for widget signals to ``config_changed``."""
        self.config_changed.emit()

    def _current_backend(self) -> str:
        return self.backend_combo.currentData() or "http"

    def _on_backend_changed(self, *args) -> None:
        """Show only the fields relevant to the selected backend."""
        is_cli = self._current_backend() == "cli"
        for w in (self.cli_path_row_widget, self.cli_home_edit,
                  self.cli_model_combo, self.cli_mcp_check):
            self._form.setRowVisible(w, is_cli)
        for w in (self.base_url_edit, self.model_edit, self.api_key_edit):
            self._form.setRowVisible(w, not is_cli)
        # Temperature / max-tokens are only honored by the HTTP backend; the
        # Copilot CLI manages its own sampling and output limits.
        na_tip = ("Not used by the GitHub Copilot CLI backend — the CLI manages "
                  "this itself. Applies to the HTTP endpoint backend only.")
        for w in (self.temp_spin, self.maxtok_spin):
            w.setEnabled(not is_cli)
            w.setToolTip(na_tip if is_cli else "")
        self.config_changed.emit()

    def _on_browse_cli(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select the copilot executable")
        if path:
            self.cli_path_edit.setText(path)

    # -- state ---------------------------------------------------------------

    def set_report(self, report, skill_manager=None) -> None:
        """Provide the latest analysis report (and skill manager) to the panel."""
        self._report = report
        if skill_manager is not None:
            self._skill_manager = skill_manager
        self._update_button_state()
        if report is not None:
            self.status_label.setText(
                f"Report ready: {report.summary.coverage_loss_count} "
                "coverage-loss faults available for the agent.")

    def set_compare(self, compare) -> None:
        """Attach (or clear) a baseline report payload for regression tools."""
        self._compare = compare
        if compare:
            label = compare.get("label") or "baseline"
            n = len(compare.get("faults", []) or [])
            self.status_label.setText(
                f"Regression mode: baseline '{label}' ({n} faults) loaded — "
                "ask the agent what changed, or run agentic mode.")

    def clear(self) -> None:
        self._report = None
        self.prompt_view.clear()
        self.trace_view.clear()
        self.response_view.clear()
        self.chat_view.clear()
        self._last_response = ""
        self._chat_turns = []
        self._compare = None
        self._session_id = None
        self._chat_messages = []
        self._set_chat_enabled(False)
        self.status_label.setText("Run an analysis first, then run the AI agent.")
        self._update_button_state()

    # -- investigation persistence (save/load with the report) ---------------

    def export_investigation(self) -> Optional[dict]:
        """Return the current diagnosis + chat transcript + trace, or None."""
        diagnosis = getattr(self, "_last_response", "") or ""
        chat = [{"role": r, "text": t} for r, t in self._chat_turns]
        trace = self.trace_view.toPlainText()
        if not diagnosis and not chat and not trace.strip():
            return None
        return {"diagnosis": diagnosis, "chat": chat, "trace": trace}

    def import_investigation(self, data: Optional[dict]) -> None:
        """Restore a saved investigation (view-only), or clear if *data* is None."""
        self.response_view.clear()
        self.chat_view.clear()
        self.trace_view.clear()
        self._chat_turns = []
        self._last_response = ""
        self._set_chat_enabled(False)
        if not data:
            return
        diagnosis = data.get("diagnosis", "") or ""
        if diagnosis:
            self._set_response(diagnosis)
        for turn in data.get("chat", []):
            self._append_chat(turn.get("role", "Agent"), turn.get("text", ""))
        trace = data.get("trace", "")
        if trace:
            self.trace_view.setPlainText(trace)
        if diagnosis or data.get("chat"):
            self.status_label.setText(
                "Loaded saved investigation (view-only transcript). Press Run "
                "to continue the conversation on this report.")

    def _on_mode_toggled(self, checked: bool) -> None:
        self.run_btn.setText(
            "Run Agentic Agent" if checked else "Run AI Debug Agent")
        if checked:
            n = (len(self._skill_manager.enabled_skills())
                 if self._skill_manager else 0)
            self.status_label.setText(
                f"Agentic mode ON — the agent uses {n} enabled skill(s). With "
                "the HTTP backend the model calls them as tools; with the "
                "Copilot CLI backend they run locally and their findings are "
                "handed to the CLI.")
        else:
            self.status_label.setText("Agentic mode OFF — single-shot diagnosis.")

    def _update_button_state(self) -> None:
        has_report = self._report is not None
        for b in (self.run_btn, self.build_btn):
            b.setEnabled(has_report)

    def current_config(self) -> AgentConfig:
        return AgentConfig(
            backend=self._current_backend(),
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.text().strip() or "gpt-4",
            api_key=self.api_key_edit.text(),
            temperature=float(self.temp_spin.value()),
            max_tokens=int(self.maxtok_spin.value()),
            max_faults=int(self.maxfaults_spin.value()),
            cli_path=self.cli_path_edit.text().strip(),
            cli_home=self.cli_home_edit.text().strip(),
            cli_model=self.cli_model_combo.currentText().strip(),
            cli_token=self.auth_token_edit.text(),
            cli_use_mcp=self.cli_mcp_check.isChecked(),
        )

    # -- settings persistence (no secrets) -----------------------------------

    def export_settings(self) -> dict:
        """Return persistable settings (API key intentionally excluded)."""
        return {
            "backend": self._current_backend(),
            "base_url": self.base_url_edit.text().strip(),
            "model": self.model_edit.text().strip(),
            "cli_path": self.cli_path_edit.text().strip(),
            "cli_home": self.cli_home_edit.text().strip(),
            "cli_model": self.cli_model_combo.currentText().strip(),
            "cli_use_mcp": self.cli_mcp_check.isChecked(),
            "temperature": float(self.temp_spin.value()),
            "max_tokens": int(self.maxtok_spin.value()),
            "max_faults": int(self.maxfaults_spin.value()),
        }

    def import_settings(self, cfg: dict) -> None:
        if not cfg:
            return
        backend = cfg.get("backend", "cli")
        idx = self.backend_combo.findData(backend)
        if idx >= 0:
            self.backend_combo.setCurrentIndex(idx)
        if cfg.get("cli_path"):
            self.cli_path_edit.setText(cfg["cli_path"])
        if cfg.get("cli_home"):
            self.cli_home_edit.setText(cfg["cli_home"])
        self.cli_model_combo.setCurrentText(cfg.get("cli_model", "") or "auto")
        self.cli_mcp_check.setChecked(bool(cfg.get("cli_use_mcp", True)))
        self.base_url_edit.setText(cfg.get("base_url", ""))
        self.model_edit.setText(cfg.get("model", "") or "")
        self.temp_spin.setValue(float(cfg.get("temperature", 0.0)))
        self.maxtok_spin.setValue(int(cfg.get("max_tokens", 4000)))
        self.maxfaults_spin.setValue(int(cfg.get("max_faults", 200)))
        self._on_backend_changed()

    # -- actions -------------------------------------------------------------

    def on_build_prompt(self) -> None:
        if self._report is None:
            return
        config = self.current_config()
        agentic = self.agentic_check.isChecked()
        system = AGENTIC_SYSTEM_PROMPT if agentic else SYSTEM_PROMPT
        payload = build_user_payload(self._report, config.max_faults,
                                     agentic=agentic)
        full = (
            "===== SYSTEM PROMPT =====\n"
            + system
            + "\n\n===== USER PAYLOAD =====\n"
            + payload
        )
        self.prompt_view.setPlainText(full)
        note = ("Agentic prompt built (skills exposed as tools). "
                if agentic else "Prompt built. ")
        self.status_label.setText(
            note + "Copy it into your LLM, or click Run if an endpoint is "
            "configured.")

    def _build_context(self) -> Optional[AnalysisContext]:
        """Assemble an AnalysisContext from the current report for skill tools."""
        r = self._report
        if r is None:
            return None
        return AnalysisContext(
            netlist=getattr(r, "netlist", None),
            faults=getattr(r, "faults", None),
            constraints=getattr(r, "constraints", None),
            fault_results=r.fault_results,
            pattern_groups=r.pattern_groups,
            summary=r.summary,
            adjacency=getattr(r, "adjacency", None),
            compare=getattr(self, "_compare", None),
        )

    def on_run(self) -> None:
        if self._report is None:
            return
        config = self.current_config()
        if not config.configured:
            if config.backend == "cli":
                msg = ("Copilot CLI path is not set or not found — showing prompt "
                       "instead. Set a valid Copilot CLI path to run live.")
            else:
                msg = ("No LLM endpoint configured — showing prompt instead. "
                       "Set a Base URL + Model to run live.")
            self.status_label.setText(msg)
            self.on_build_prompt()
            return

        self.on_build_prompt()  # show the prompt that is being sent
        if self.agentic_check.isChecked():
            self._run_agentic(config)
        else:
            self._run_single_shot(config)

    def _run_single_shot(self, config: AgentConfig) -> None:
        self.run_btn.setEnabled(False)
        self.status_label.setText("Calling LLM… streaming response below.")
        self._begin_session(config, agentic=False)
        self.response_view.clear()
        self._stream_buf = ""

        agent = DebugAgent(config)
        self._thread = QThread()
        self._worker = _AgentWorker(agent, self._report,
                                    session_id=self._session_id)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.token.connect(self._on_response_token)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def _run_agentic(self, config: AgentConfig) -> None:
        if self._skill_manager is None:
            self.status_label.setText(
                "Agentic mode needs the skill manager — run an analysis first.")
            return
        ctx = self._build_context()
        if ctx is None:
            self.status_label.setText("No analysis context available.")
            return

        n = len(self._skill_manager.enabled_skills())
        self.trace_view.clear()
        self._append_trace(f"Starting agentic run with {n} enabled skill(s)…")
        self.run_btn.setEnabled(False)
        self.status_label.setText("Agentic agent running — calling tools…")
        self._begin_session(config, agentic=True)
        self.response_view.clear()
        self._stream_buf = ""

        agent = DebugAgent(config)
        self._thread = QThread()
        self._worker = _AgenticWorker(agent, self._report,
                                      self._skill_manager, ctx,
                                      session_id=self._session_id)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.trace.connect(self._append_trace)
        self._worker.token.connect(self._on_response_token)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def _on_response_token(self, chunk: str) -> None:
        self._stream_buf += chunk
        self.response_view.moveCursor(QTextCursor.End)
        self.response_view.insertPlainText(chunk)
        self.response_view.moveCursor(QTextCursor.End)

    def _append_trace(self, line: str) -> None:
        self.trace_view.appendPlainText(line)

    def _on_finished(self, text: str) -> None:
        final = text or getattr(self, "_stream_buf", "")
        self._set_response(final)
        self.status_label.setText(
            "Agent response received — click a fault to focus it, or Verify.")
        self.run_btn.setEnabled(True)
        # Seed the follow-up conversation with this diagnosis.
        self._chat_view_reset()
        self._append_chat("Agent", final)
        if self._chat_backend != "cli":
            # HTTP backend: keep a running messages list for follow-ups.
            self._chat_messages.append({"role": "assistant", "content": text})
        self._set_chat_enabled(True)
        self.chat_input.setFocus()

    def _on_failed(self, message: str) -> None:
        self._set_response(f"[ERROR] {message}")
        self.run_btn.setEnabled(True)
        if is_cli_auth_error(message):
            self.status_label.setText(
                "Copilot CLI is not authenticated — opening the Authentication "
                "tab so you can sign in.")
            self.auth_status_label.setText(
                "The last run failed because the Copilot CLI is not "
                "authenticated. Paste a GitHub token (Option A) or use device "
                "sign-in (Option B), then re-run.")
            self.auth_log.appendPlainText("[auth needed] " + message.strip())
            self.tabs.setCurrentIndex(self.tabs.count() - 1)
        else:
            self.status_label.setText("Agent call failed — see response panel.")

    def _cleanup(self) -> None:
        self._thread = None
        self._worker = None

    # -- follow-up chat ------------------------------------------------------

    def _begin_session(self, config: AgentConfig, agentic: bool) -> None:
        """Start a fresh conversation context for a new agent run."""
        self._session_id = str(uuid.uuid4())
        self._chat_backend = config.backend
        self.chat_view.clear()
        self._chat_turns = []
        self._set_chat_enabled(False)
        if config.backend == "cli":
            self._chat_messages = []
        else:
            system = AGENTIC_SYSTEM_PROMPT if agentic else SYSTEM_PROMPT
            payload = build_user_payload(self._report, config.max_faults,
                                         agentic=agentic)
            self._chat_messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": payload},
            ]

    def _set_chat_enabled(self, enabled: bool) -> None:
        self.chat_input.setEnabled(enabled)
        self.chat_send_btn.setEnabled(enabled)
        self.chat_input.setPlaceholderText(
            "Ask a follow-up question about the diagnosis…" if enabled
            else "Run the agent first, then ask a follow-up question…")

    def _chat_view_reset(self) -> None:
        self.chat_view.clear()

    def _render_turn(self, role: str, text: str) -> None:
        """Render one chat turn into the view (no bookkeeping)."""
        if role == "Agent":
            body = self._to_html(text)
        else:
            body = html.escape((text or "").strip()).replace("\n", "<br>")
        colour = {"You": "#0a7", "Agent": "#036", "Error": "#c0392b"}.get(
            role, "#555")
        block = (f'<p style="margin:6px 0;"><b style="color:{colour};">'
                 f'{html.escape(role)}:</b><br>{body}</p>')
        self._append_html(self.chat_view, block)

    def _append_chat(self, role: str, text: str) -> None:
        self._chat_turns.append((role, (text or "").strip()))
        self._render_turn(role, text)

    def _rebuild_chat_view(self) -> None:
        self.chat_view.clear()
        for role, text in self._chat_turns:
            self._render_turn(role, text)

    # -- grounded evidence (clickable faults) --------------------------------

    def _known_fault_objects(self) -> list:
        r = self._report
        if r is None:
            return []
        out = []
        for fr in getattr(r, "fault_results", []) or []:
            fo = getattr(getattr(fr, "fault", None), "fault_object", "")
            if fo:
                out.append(fo)
        return out

    def _to_html(self, text: str) -> str:
        """HTML-escape *text* and turn known fault ids into clickable links."""
        esc = html.escape(text or "")
        faults = self._known_fault_objects()
        if faults:
            esc_map = {html.escape(fo): fo for fo in faults}
            keys = sorted(esc_map, key=len, reverse=True)
            pattern = re.compile("|".join(re.escape(k) for k in keys))

            def _repl(m: "re.Match") -> str:
                e = m.group(0)
                fo = esc_map.get(e, e)
                href = "fault:" + quote(fo, safe="")
                return (f'<a href="{href}" style="color:#0055aa;">{e}</a>')

            esc = pattern.sub(_repl, esc)
        return esc.replace("\n", "<br>")

    def _append_html(self, browser: QTextBrowser, html_str: str) -> None:
        browser.moveCursor(QTextCursor.End)
        browser.insertHtml(html_str)
        browser.moveCursor(QTextCursor.End)

    def _set_response(self, text: str) -> None:
        self._last_response = text
        self.response_view.setHtml(
            f'<div style="font-family:monospace;white-space:pre-wrap;">'
            f'{self._to_html(text)}</div>')

    def _on_anchor_clicked(self, url) -> None:
        s = url.toString()
        if s.startswith("fault:"):
            self.fault_referenced.emit(unquote(s[len("fault:"):]))

    def ask_about_fault(self, fault_object: str) -> None:
        """Pre-fill a follow-up question about *fault_object* (from the table)."""
        self.tabs.setCurrentIndex(0)
        question = (f"Explain why coverage is lost for fault "
                    f"'{fault_object}'. Use why_blocked and get_fault_detail "
                    "and cite the structural evidence.")
        self.chat_input.setText(question)
        if self.chat_input.isEnabled():
            self.chat_input.setFocus()
            self.status_label.setText(
                "Question ready — press Enter/Send to ask about this fault.")
        else:
            self.status_label.setText(
                "Run the agent once to start a chat; this question is ready to "
                "send afterwards.")

    def on_suggest_fixes(self) -> None:
        """Run the deterministic test-point suggester and show ranked fixes."""
        if self._report is None:
            self.status_label.setText("Run or load an analysis first.")
            return
        ctx = self._build_context()
        if ctx is None:
            self.status_label.setText("No analysis context available.")
            return
        data = investigate.run_tool(
            "suggest_test_points", {},
            fault_results=ctx.fault_results, constraints=ctx.constraints,
            netlist=ctx.netlist, adjacency=getattr(ctx, "adjacency", None))
        suggestions = data.get("suggestions", [])
        lines = [f"# Suggested Test Points / DFT Fixes "
                 f"({data.get('total', 0)} total, showing {len(suggestions)})",
                 ""]
        for i, s in enumerate(suggestions, 1):
            lines.append(
                f"{i}. [{s['kind']}] {s['fault_object']} "
                f"(score {s['score']}, fan_in={s['fan_in']}, "
                f"fan_out={s['fan_out']})")
            lines.append(f"   Action: {s['suggested_action']}")
            lines.append(f"   Why:    {s['rationale']}")
            lines.append("")
        if not suggestions:
            lines.append("No coverage-loss faults to suggest fixes for.")
        self._set_response("\n".join(lines))
        self.status_label.setText(
            f"{data.get('total', 0)} deterministic test-point suggestion(s) — "
            "no LLM used. Fault ids are clickable.")

    def on_verify(self) -> None:
        """Cross-check the agent answer against the deterministic report."""
        text = getattr(self, "_last_response", "") or ""
        if not text.strip():
            self.status_label.setText("Nothing to verify — run the agent first.")
            return
        lookup = {}
        for fr in (getattr(self._report, "fault_results", []) or []):
            lookup[fr.fault.fault_object] = fr

        # Tokens that look like hierarchical fault paths (contain a '/').
        tokens = set(re.findall(r"[A-Za-z_][\w.$:\[\]-]*(?:/[\w.$:\[\]-]+)+",
                                text))
        grounded = sorted(t for t in tokens if t in lookup)
        ungrounded = sorted(t for t in tokens if t not in lookup)

        lines = ["=== VERIFICATION (deterministic ground-truth) ===",
                 f"Fault-path references in answer: {len(tokens)}",
                 f"  ✓ exact matches in report: {len(grounded)}",
                 f"  ⚠ NOT in report (possible hallucination): "
                 f"{len(ungrounded)}", ""]
        for t in grounded:
            fr = lookup[t]
            lines.append(
                f"  ✓ {t}: class={fr.fault.fault_class.value} "
                f"root_cause={fr.root_cause.value} "
                f"ctrl={'Y' if fr.controllability_issue else 'N'} "
                f"obsv={'Y' if fr.observability_issue else 'N'}")
        for t in ungrounded:
            lines.append(f"  ⚠ {t}: not found in the report's faults")
        self.trace_view.appendPlainText("\n".join(lines))
        if ungrounded:
            self.status_label.setText(
                f"Verify: {len(grounded)} grounded, {len(ungrounded)} NOT in "
                "report — review flagged references in the trace pane.")
        else:
            self.status_label.setText(
                f"Verify: all {len(grounded)} referenced fault(s) exist in the "
                "report.")

    def on_clear_chat(self) -> None:
        self.chat_view.clear()
        self.status_label.setText("Chat transcript cleared (session kept).")

    def on_send_chat(self) -> None:
        msg = self.chat_input.text().strip()
        if not msg:
            return
        if self._chat_thread is not None:
            self.status_label.setText("Agent is still replying — please wait.")
            return
        if self._chat_backend == "cli" and not self._session_id:
            self.status_label.setText(
                "Run the agent first to start a conversation.")
            return

        config = self.current_config()
        if not config.configured:
            self.status_label.setText(
                "Backend is not configured — cannot chat.")
            return

        self.chat_input.clear()
        self._append_chat("You", msg)

        history = None
        if self._chat_backend != "cli":
            self._chat_messages.append({"role": "user", "content": msg})
            history = list(self._chat_messages)

        self._set_chat_enabled(False)
        self.status_label.setText("Agent is replying…")
        self._chat_stream_buf = ""
        # Live streaming cue appended below the You turn; replaced on finish.
        self._append_html(
            self.chat_view,
            '<p style="margin:6px 0;"><b style="color:#036;">Agent:</b><br></p>')

        agent = DebugAgent(config)
        self._chat_thread = QThread()
        self._chat_worker = _ChatWorker(agent, msg, self._session_id, history)
        self._chat_worker.moveToThread(self._chat_thread)
        self._chat_thread.started.connect(self._chat_worker.run)
        self._chat_worker.token.connect(self._on_chat_token)
        self._chat_worker.finished.connect(self._on_chat_finished)
        self._chat_worker.failed.connect(self._on_chat_failed)
        self._chat_worker.finished.connect(self._chat_thread.quit)
        self._chat_worker.failed.connect(self._chat_thread.quit)
        self._chat_thread.finished.connect(self._chat_cleanup)
        self._chat_thread.start()

    def _on_chat_token(self, chunk: str) -> None:
        self._chat_stream_buf += chunk
        self.chat_view.moveCursor(QTextCursor.End)
        self.chat_view.insertPlainText(chunk)
        self.chat_view.moveCursor(QTextCursor.End)

    def _on_chat_finished(self, text: str) -> None:
        final = text or getattr(self, "_chat_stream_buf", "")
        self._chat_turns.append(("Agent", (final or "").strip()))
        if self._chat_backend != "cli":
            self._chat_messages.append({"role": "assistant", "content": final})
        # Rebuild so the streamed plain text becomes linkified transcript.
        self._rebuild_chat_view()
        self._set_chat_enabled(True)
        self.status_label.setText(
            "Reply received — continue the conversation or re-run.")
        self.chat_input.setFocus()

    def _on_chat_failed(self, message: str) -> None:
        self._chat_turns.append(("Error", (message or "").strip()))
        self._rebuild_chat_view()
        self._set_chat_enabled(True)
        if is_cli_auth_error(message):
            self.status_label.setText(
                "Copilot CLI is not authenticated — see the Authentication tab.")
            self.tabs.setCurrentIndex(self.tabs.count() - 1)
        else:
            self.status_label.setText("Chat failed — see the chat log.")

    def _chat_cleanup(self) -> None:
        self._chat_thread = None
        self._chat_worker = None

    # -- authentication (Copilot CLI) ----------------------------------------

    def _cli_exe_or_warn(self) -> Optional[str]:
        """Return a valid Copilot CLI path, or warn on the auth tab and return None."""
        exe = self.cli_path_edit.text().strip()
        if not exe or not os.path.isfile(exe):
            self.auth_status_label.setText(
                "Set a valid Copilot CLI path on the Debug Agent tab first.")
            return None
        return exe

    def on_check_auth(self) -> None:
        exe = self._cli_exe_or_warn()
        if exe is None:
            return
        self._start_auth_proc(
            [exe, "-p", "ping", "-s", "--no-color", "--allow-all-tools",
             "--no-remote", "--log-level", "error"],
            action="check")

    def on_device_login(self) -> None:
        exe = self._cli_exe_or_warn()
        if exe is None:
            return
        self._start_auth_proc([exe, "login"], action="login")

    def on_cancel_login(self) -> None:
        if self._auth_proc is not None:
            self.auth_log.appendPlainText("[cancelled by user]")
            self._auth_proc.kill()

    def _start_auth_proc(self, args: list, action: str) -> None:
        if self._auth_proc is not None:
            self.auth_status_label.setText(
                "An authentication process is already running.")
            return
        cfg = self.current_config()
        env = QProcessEnvironment.systemEnvironment()
        if cfg.cli_home.strip():
            env.insert("COPILOT_HOME", cfg.cli_home.strip())
        # For the token check, validate the pasted token; never inject it into
        # the interactive device-login flow.
        if action != "login" and cfg.cli_token.strip():
            env.insert("COPILOT_GITHUB_TOKEN", cfg.cli_token.strip())

        self._auth_buf = []
        proc = QProcess(self)
        proc.setProcessEnvironment(env)
        proc.setProgram(args[0])
        proc.setArguments(args[1:])
        proc.readyReadStandardOutput.connect(self._on_auth_stdout)
        proc.readyReadStandardError.connect(self._on_auth_stderr)
        proc.errorOccurred.connect(
            lambda err: self.auth_log.appendPlainText(f"[process error] {err}"))
        proc.finished.connect(
            lambda code, status: self._on_auth_finished(code, action))
        self._auth_proc = proc

        self.auth_log.appendPlainText(
            "$ copilot " + " ".join(args[1:]))
        self.auth_status_label.setText(
            "Signing in — follow the one-time code and URL shown below."
            if action == "login" else "Checking authentication…")
        self.auth_login_btn.setEnabled(False)
        self.auth_check_btn.setEnabled(False)
        self.auth_cancel_btn.setEnabled(True)
        proc.start()

    def _on_auth_stdout(self) -> None:
        if self._auth_proc is None:
            return
        data = bytes(self._auth_proc.readAllStandardOutput()).decode(
            "utf-8", "replace").rstrip()
        if data:
            self._auth_buf.append(data)
            self.auth_log.appendPlainText(data)

    def _on_auth_stderr(self) -> None:
        if self._auth_proc is None:
            return
        data = bytes(self._auth_proc.readAllStandardError()).decode(
            "utf-8", "replace").rstrip()
        if data:
            self._auth_buf.append(data)
            self.auth_log.appendPlainText(data)

    def _on_auth_finished(self, code: int, action: str) -> None:
        ok = code == 0
        log_text = "\n".join(getattr(self, "_auth_buf", [])).lower()
        not_saved = ("not saved" in log_text or "keychain" in log_text
                     or "plaintext storage" in log_text)
        if action == "login" and not_saved:
            # Browser auth worked but the token could not be persisted because
            # this host has no system keychain and the GUI login is not a TTY,
            # so the plaintext-storage prompt could not be answered.
            msg = ("Login succeeded but the token could NOT be saved (no system "
                   "keychain on this host, and GUI login can't answer the "
                   "plaintext prompt). Fix: use Option A — paste a fine-grained "
                   "PAT above (no keychain needed). See the log for details.")
            self.auth_status_label.setText(msg)
            self.auth_log.appendPlainText(
                "[hint] Recommended: create a fine-grained GitHub PAT with the "
                "'Copilot Requests' permission and paste it into Option A. "
                "Alternatively run 'copilot login' in a real terminal and "
                "accept plaintext storage when prompted.")
        elif action == "login":
            msg = ("Signed in successfully — CLI runs should now work. "
                   "Go back to the Debug Agent tab and re-run."
                   if ok else
                   f"Sign-in failed (exit {code}). See the log above.")
            self.auth_status_label.setText(msg)
        else:
            msg = ("Authentication OK — the Copilot CLI is ready to use."
                   if ok else
                   f"Not authenticated (exit {code}). Paste a token (Option A) "
                   "or use device sign-in (Option B).")
            self.auth_status_label.setText(msg)
        self.auth_log.appendPlainText(f"[done] exit={code}")
        self._auth_proc = None
        self.auth_login_btn.setEnabled(True)
        self.auth_check_btn.setEnabled(True)
        self.auth_cancel_btn.setEnabled(False)

    def on_copy_prompt(self) -> None:
        QApplication.clipboard().setText(self.prompt_view.toPlainText())
        self.status_label.setText("Prompt copied to clipboard.")

    def on_copy_response(self) -> None:
        QApplication.clipboard().setText(self.response_view.toPlainText())
        self.status_label.setText("Response copied to clipboard.")

    def on_save_prompt(self) -> None:
        self._save(self.prompt_view.toPlainText(), "atpg_agent_prompt.txt")

    def on_save_response(self) -> None:
        self._save(self.response_view.toPlainText(), "atpg_agent_response.md")

    def _save(self, content: str, default_name: str) -> None:
        if not content.strip():
            self.status_label.setText("Nothing to save.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save", default_name)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            self.status_label.setText(f"Save failed: {exc}")
            return
        self.status_label.setText(f"Saved: {path}")
