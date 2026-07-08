"""Custom Skills tab — load user-authored skills from a directory or editor."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from ..skills.manager import SkillManager

logger = logging.getLogger(__name__)


_SKILL_TEMPLATE = '''\
"""Custom ATPG debug skill."""

from atpg_coverage_debug_agent.skills.base import (
    AnalysisContext, SkillBase, SkillResult,
)
from atpg_coverage_debug_agent.skills.registry import register


@register
class MyCustomSkill(SkillBase):
    skill_id = "my_custom_skill"
    display_name = "My Custom Skill"
    description = "Describe what this skill detects."
    default_enabled = True

    def parameters_schema(self):
        return {
            "min_faults": {
                "type": "int",
                "default": 5,
                "description": "Minimum faults to flag",
            },
        }

    def run(self, ctx: AnalysisContext) -> SkillResult:
        result = SkillResult(skill_id=self.skill_id)
        min_faults = self.get_param("min_faults")

        # ctx.fault_results is a list of FaultAnalysisResult objects.
        loss = [r for r in ctx.fault_results if r.fault.is_coverage_loss]
        result.add_info(f"Inspected {len(loss)} coverage-loss faults.")

        if len(loss) >= min_faults:
            result.add_finding(
                title="Example finding",
                description=f"Found {len(loss)} coverage-loss faults.",
                evidence=[f"threshold = {min_faults}"],
                affected_objects=[r.fault.fault_object for r in loss[:5]],
                confidence="medium",
                recommendation="Review these faults manually.",
            )

        result.summary = f"{len(result.findings)} finding(s)."
        return result
'''


_MD_TEMPLATE = '''\
# My Markdown Skill

One-line description of what this guidance skill is for.

## When to use

Describe the coverage-debug situations where this guidance applies
(e.g. non-scan boundaries, clock-gate observability, black-box SRAM edges).

## Guidance

- Step 1: what to check first.
- Step 2: structural evidence to collect.
- Step 3: how to confirm the root cause.

This content is surfaced as a finding in the Skills tab and is also included
in the AI Debug Agent prompt.
'''


class CustomSkillsPanel(QWidget):
    """Tab letting users add their own skills via a directory or inline editor."""

    #: Emitted after new custom skills are successfully loaded.
    skills_loaded = Signal()

    def __init__(self, manager: SkillManager,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._build()

    # -- UI ------------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Add your own skills. A skill is either a <b>Python</b> file "
            "(<code>.py</code>) defining a <code>SkillBase</code> subclass "
            "decorated with <code>@register</code>, or a <b>Markdown</b> file "
            "(<code>.md</code>) containing guidance. Loaded skills appear in the "
            "<b>Skills</b> tab.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- Directory loader ---
        dir_box = QGroupBox("Load skills from a directory")
        dir_layout = QHBoxLayout(dir_box)
        dir_layout.addWidget(QLabel("Custom skills dir:"))
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText(
            "Folder containing custom *.py and *.md skill files")
        dir_layout.addWidget(self.dir_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_dir)
        dir_layout.addWidget(browse_btn)
        load_dir_btn = QPushButton("Load Skills from Directory")
        load_dir_btn.clicked.connect(self.on_load_dir)
        dir_layout.addWidget(load_dir_btn)
        layout.addWidget(dir_box)

        # --- Single-file loader ---
        file_box = QGroupBox("Add a single skill file")
        file_layout = QHBoxLayout(file_box)
        file_layout.addWidget(QLabel(
            "Pick one <code>.py</code> or <code>.md</code> skill file:"))
        file_layout.addStretch(1)
        choose_btn = QPushButton("Choose Skill File…")
        choose_btn.clicked.connect(self.on_choose_file)
        file_layout.addWidget(choose_btn)
        layout.addWidget(file_box)

        # --- Editor + loaded list split ---
        splitter = QSplitter(Qt.Horizontal)

        editor_box = QGroupBox("New skill (inline editor)")
        editor_layout = QVBoxLayout(editor_box)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Type:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Python (.py)", "Markdown (.md)"])
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        name_row.addWidget(self.type_combo)
        name_row.addWidget(QLabel("File name:"))
        self.fname_edit = QLineEdit("my_custom_skill.py")
        name_row.addWidget(self.fname_edit, 1)
        editor_layout.addLayout(name_row)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(_SKILL_TEMPLATE)
        self.editor.setStyleSheet("font-family: monospace;")
        editor_layout.addWidget(self.editor, 1)

        editor_btns = QHBoxLayout()
        reset_tpl_btn = QPushButton("Reset Template")
        reset_tpl_btn.clicked.connect(self._reset_template)
        save_load_btn = QPushButton("Save && Load Skill")
        save_load_btn.clicked.connect(self.on_save_and_load)
        editor_btns.addWidget(reset_tpl_btn)
        editor_btns.addStretch(1)
        editor_btns.addWidget(save_load_btn)
        editor_layout.addLayout(editor_btns)
        splitter.addWidget(editor_box)

        list_box = QGroupBox("Loaded custom skills")
        list_layout = QVBoxLayout(list_box)
        self.loaded_list = QListWidget()
        list_layout.addWidget(self.loaded_list)
        splitter.addWidget(list_box)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #555;")
        layout.addWidget(self.status_label)

    # -- settings persistence ------------------------------------------------

    def custom_dir(self) -> str:
        return self.dir_edit.text().strip()

    def set_custom_dir(self, value: str) -> None:
        self.dir_edit.setText(value or "")

    # -- actions -------------------------------------------------------------

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select custom skills directory")
        if path:
            self.dir_edit.setText(path)

    def _is_markdown(self) -> bool:
        return self.type_combo.currentIndex() == 1

    def _on_type_changed(self) -> None:
        """Swap the template and adjust the default filename extension."""
        stem = os.path.splitext(self.fname_edit.text().strip())[0] or "my_custom_skill"
        if self._is_markdown():
            self.editor.setPlainText(_MD_TEMPLATE)
            self.fname_edit.setText(f"{stem}.md")
        else:
            self.editor.setPlainText(_SKILL_TEMPLATE)
            self.fname_edit.setText(f"{stem}.py")

    def _reset_template(self) -> None:
        self.editor.setPlainText(
            _MD_TEMPLATE if self._is_markdown() else _SKILL_TEMPLATE)

    def on_load_dir(self) -> None:
        directory = self.custom_dir()
        if not directory or not os.path.isdir(directory):
            self._error("Select a valid directory first.")
            return
        self._load_from(directory)

    def on_choose_file(self) -> None:
        """Pick a single .py/.md skill file, add it, and load it."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a skill file", self.custom_dir() or "",
            "Skill files (*.py *.md);;All files (*)")
        if not path:
            return
        if not (path.endswith(".py") or path.endswith(".md")):
            self._error("Please choose a .py or .md skill file.")
            return

        directory = self.custom_dir()
        if not directory:
            directory = os.path.expanduser("~/.atpg_debug_agent/custom_skills")
            self.dir_edit.setText(directory)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            self._error(f"Could not create directory: {exc}")
            return

        dest = os.path.join(directory, os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(dest):
            try:
                shutil.copy(path, dest)
            except OSError as exc:
                self._error(f"Could not copy file: {exc}")
                return
        self.status_label.setText(
            f"Added {os.path.basename(path)}. Loading…")
        self._load_from(directory)

    def on_save_and_load(self) -> None:
        directory = self.custom_dir()
        if not directory:
            # Default to a per-user directory if none chosen.
            directory = os.path.expanduser("~/.atpg_debug_agent/custom_skills")
            self.dir_edit.setText(directory)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            self._error(f"Could not create directory: {exc}")
            return

        fname = self.fname_edit.text().strip()
        if not (fname.endswith(".py") or fname.endswith(".md")) or fname.startswith("_"):
            self._error("File name must end with .py or .md and not start with '_'.")
            return

        target = os.path.join(directory, fname)
        try:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(self.editor.toPlainText())
        except OSError as exc:
            self._error(f"Could not write file: {exc}")
            return

        self.status_label.setText(f"Saved {target}. Loading…")
        self._load_from(directory)

    def _load_from(self, directory: str) -> None:
        try:
            new_ids = self._manager.load_custom_skills(directory)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Custom skill load failed")
            self._error(f"Load failed: {exc}")
            return

        if not new_ids:
            self.status_label.setText(
                "No new skills loaded (already loaded, or no valid skills found). "
                "Check that each file defines a @register SkillBase subclass.")
        else:
            self.status_label.setText(
                f"Loaded {len(new_ids)} skill(s): {', '.join(new_ids)}")
            self._refresh_loaded_list(new_ids)
            self.skills_loaded.emit()

    def _refresh_loaded_list(self, new_ids) -> None:
        existing = {self.loaded_list.item(i).text().split("  ")[0]
                    for i in range(self.loaded_list.count())}
        for skill_id in new_ids:
            if skill_id in existing:
                continue
            skill = self._manager.get(skill_id)
            label = skill_id
            if skill is not None:
                label = f"{skill_id}  —  {skill.display_name}"
            self.loaded_list.addItem(label)

    def _error(self, message: str) -> None:
        self.status_label.setText(message)
        QMessageBox.warning(self, "Custom Skills", message)
