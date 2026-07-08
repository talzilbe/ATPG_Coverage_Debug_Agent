"""AI Debug Agent tab — runs the strict ATPG/DFT system prompt via an LLM."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
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
    QVBoxLayout,
    QWidget,
)

from ..agent.debug_agent import AgentConfig, DebugAgent, SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class _AgentWorker(QObject):
    """Runs the LLM call off the GUI thread."""

    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, agent: DebugAgent, report) -> None:
        super().__init__()
        self._agent = agent
        self._report = report

    def run(self) -> None:
        try:
            text = self._agent.run(self._report)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI debug agent failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(text)


class AgentPanel(QWidget):
    """Full 'AI Debug Agent' tab: LLM config + run + prompt/response views."""

    #: Emitted when the LLM connection settings change (for persistence).
    config_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._report = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_AgentWorker] = None
        self._build()

    # -- UI ------------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        # --- LLM connection settings ---
        cfg_box = QGroupBox("LLM Connection (OpenAI-compatible)")
        form = QFormLayout(cfg_box)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://your-llm-host/v1")
        self.base_url_edit.textChanged.connect(self.config_changed.emit)
        form.addRow("Base URL:", self.base_url_edit)

        self.model_edit = QLineEdit("gpt-4")
        self.model_edit.textChanged.connect(self.config_changed.emit)
        form.addRow("Model:", self.model_edit)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText(
            "Bearer token — kept in memory only, never written to disk")
        form.addRow("API Key:", self.api_key_edit)

        knobs = QHBoxLayout()
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setValue(0.0)
        self.temp_spin.valueChanged.connect(self.config_changed.emit)
        knobs.addWidget(QLabel("Temperature:"))
        knobs.addWidget(self.temp_spin)

        self.maxtok_spin = QSpinBox()
        self.maxtok_spin.setRange(256, 32000)
        self.maxtok_spin.setValue(4000)
        self.maxtok_spin.valueChanged.connect(self.config_changed.emit)
        knobs.addWidget(QLabel("Max tokens:"))
        knobs.addWidget(self.maxtok_spin)

        self.maxfaults_spin = QSpinBox()
        self.maxfaults_spin.setRange(10, 5000)
        self.maxfaults_spin.setValue(200)
        self.maxfaults_spin.valueChanged.connect(self.config_changed.emit)
        knobs.addWidget(QLabel("Max faults in prompt:"))
        knobs.addWidget(self.maxfaults_spin)
        knobs.addStretch(1)
        form.addRow("", self._wrap(knobs))

        layout.addWidget(cfg_box)

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
        for b in (self.run_btn, self.build_btn, self.copy_prompt_btn,
                  self.save_prompt_btn, self.copy_resp_btn, self.save_resp_btn):
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

        resp_box = QGroupBox("Agent Response")
        resp_layout = QVBoxLayout(resp_box)
        self.response_view = QPlainTextEdit()
        self.response_view.setReadOnly(True)
        self.response_view.setPlaceholderText(
            "The agent's evidence-driven A–F diagnosis appears here.")
        resp_layout.addWidget(self.response_view)
        splitter.addWidget(resp_box)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self._update_button_state()

    @staticmethod
    def _wrap(inner_layout) -> QWidget:
        w = QWidget()
        w.setLayout(inner_layout)
        return w

    # -- state ---------------------------------------------------------------

    def set_report(self, report) -> None:
        """Provide the latest analysis report to the panel."""
        self._report = report
        self._update_button_state()
        if report is not None:
            self.status_label.setText(
                f"Report ready: {report.summary.coverage_loss_count} "
                "coverage-loss faults available for the agent.")

    def clear(self) -> None:
        self._report = None
        self.prompt_view.clear()
        self.response_view.clear()
        self.status_label.setText("Run an analysis first, then run the AI agent.")
        self._update_button_state()

    def _update_button_state(self) -> None:
        has_report = self._report is not None
        for b in (self.run_btn, self.build_btn):
            b.setEnabled(has_report)

    def current_config(self) -> AgentConfig:
        return AgentConfig(
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.text().strip() or "gpt-4",
            api_key=self.api_key_edit.text(),
            temperature=float(self.temp_spin.value()),
            max_tokens=int(self.maxtok_spin.value()),
            max_faults=int(self.maxfaults_spin.value()),
        )

    # -- settings persistence (no secrets) -----------------------------------

    def export_settings(self) -> dict:
        """Return persistable settings (API key intentionally excluded)."""
        return {
            "base_url": self.base_url_edit.text().strip(),
            "model": self.model_edit.text().strip(),
            "temperature": float(self.temp_spin.value()),
            "max_tokens": int(self.maxtok_spin.value()),
            "max_faults": int(self.maxfaults_spin.value()),
        }

    def import_settings(self, cfg: dict) -> None:
        if not cfg:
            return
        self.base_url_edit.setText(cfg.get("base_url", ""))
        self.model_edit.setText(cfg.get("model", "gpt-4") or "gpt-4")
        self.temp_spin.setValue(float(cfg.get("temperature", 0.0)))
        self.maxtok_spin.setValue(int(cfg.get("max_tokens", 4000)))
        self.maxfaults_spin.setValue(int(cfg.get("max_faults", 200)))

    # -- actions -------------------------------------------------------------

    def on_build_prompt(self) -> None:
        if self._report is None:
            return
        agent = DebugAgent(self.current_config())
        prompt = agent.build_prompt(self._report)
        full = (
            "===== SYSTEM PROMPT =====\n"
            + SYSTEM_PROMPT
            + "\n\n===== USER PAYLOAD =====\n"
            + prompt
        )
        self.prompt_view.setPlainText(full)
        self.status_label.setText("Prompt built. Copy it into your LLM, or click "
                                  "'Run AI Debug Agent' if an endpoint is configured.")

    def on_run(self) -> None:
        if self._report is None:
            return
        config = self.current_config()
        if not config.configured:
            self.status_label.setText(
                "No LLM endpoint configured — showing prompt instead. "
                "Set a Base URL + Model to run live.")
            self.on_build_prompt()
            return

        self.on_build_prompt()  # show the prompt that is being sent
        self.run_btn.setEnabled(False)
        self.status_label.setText("Calling LLM… this may take a while.")

        agent = DebugAgent(config)
        self._thread = QThread()
        self._worker = _AgentWorker(agent, self._report)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def _on_finished(self, text: str) -> None:
        self.response_view.setPlainText(text)
        self.status_label.setText("Agent response received.")
        self.run_btn.setEnabled(True)

    def _on_failed(self, message: str) -> None:
        self.response_view.setPlainText(f"[ERROR] {message}")
        self.status_label.setText("Agent call failed — see response panel.")
        self.run_btn.setEnabled(True)

    def _cleanup(self) -> None:
        self._thread = None
        self._worker = None

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
