"""Tab that opens the vendor viewer on the design the analysis just examined.

The offline analysis is structural and conservative; the viewer owns the
authoritative answer.  This panel turns a finding into a loaded viewer session
in one click, and shows the exact commands it will run so the user can check or
edit them first -- or paste them by hand if the launch is not available.

The launch is *detached*: the vendor shell must outlive this application.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QProcess, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from ..launcher import (
    InspectAction, LaunchInputError, LiveSession, LiveSessionError,
    ProfileError, ToolProfile, VisualizerInputs, build_dofile, find_terminal,
    list_profiles, signal_inspect_actions, write_launch_bundle,
)
from ..launcher.visualizer import (
    derive_paths_from_run_dir, fault_inspect_commands, missing_inputs,
)

logger = logging.getLogger(__name__)

#: How often the vendor log is re-read while a session is open.
LOG_POLL_MS = 1500

#: How often the control channel is re-checked while waiting for it to appear.
SESSION_POLL_MS = 2000

#: Cap on the log text kept in the view, in characters.
MAX_LOG_CHARS = 400_000

#: Shown in the saved-configuration combo when the form matches no saved entry.
_UNSAVED_LABEL = "(current form — not saved)"


class _PathRow(QWidget):
    """A labelled line edit with a Browse button."""

    changed = Signal()

    def __init__(self, directory: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._directory = directory
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(self)
        self.edit.textChanged.connect(lambda _t: self.changed.emit())
        layout.addWidget(self.edit, 1)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        layout.addWidget(browse, 0)

    def _browse(self) -> None:
        if self._directory:
            path = QFileDialog.getExistingDirectory(self, "Select directory")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select file")
        if path:
            self.edit.setText(path)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, value: str) -> None:
        self.edit.setText(value or "")


class VisualizerPanel(QWidget):
    """Collects the launch inputs, previews the commands, and starts the viewer."""

    config_changed = Signal()
    status_message = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._profiles: List[ToolProfile] = []
        self._path_rows: Dict[str, _PathRow] = {}
        self._presets: Dict[str, Dict[str, Any]] = {}
        self._extra_commands: List[str] = []
        self._analysis_faults = ""
        self._dofile_edited = False
        self._last_generated = ""
        self._script_dir = ""
        self._log_path = ""
        self._log_offset = 0
        self._session: Optional[LiveSession] = None
        self._session_ready = False
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(LOG_POLL_MS)
        self._log_timer.timeout.connect(self._poll_log)
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(SESSION_POLL_MS)
        self._session_timer.timeout.connect(self._poll_session)
        self._build()
        self.reload_profiles()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)

        intro = QLabel(
            "Open the vendor viewer on this same design, to confirm a "
            "structural finding in the tool that owns the authoritative "
            "answer. The commands are shown before they run.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        outer.addWidget(self._build_profile_box())
        outer.addWidget(self._build_setup_box())
        outer.addWidget(self._build_inputs_box())
        outer.addWidget(self._build_commands_box(), 1)
        outer.addLayout(self._build_button_row())

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        outer.addWidget(self._build_log_box(), 1)

    def _build_profile_box(self) -> QGroupBox:
        box = QGroupBox("Project profile")
        form = QFormLayout(box)

        row = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        row.addWidget(self.profile_combo, 1)
        reload_btn = QPushButton("Reload")
        reload_btn.setToolTip(
            "Re-read the profile files. Use after editing or adding one.")
        reload_btn.clicked.connect(self.reload_profiles)
        row.addWidget(reload_btn, 0)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("Profile:", holder)

        self.profile_note = QLabel("")
        self.profile_note.setWordWrap(True)
        form.addRow("", self.profile_note)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip(
            "Named launch configurations. Selecting one fills the whole form "
            "below, so a design you open often is two clicks away.")
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        preset_row.addWidget(self.preset_combo, 1)

        self.save_preset_btn = QPushButton("Save as…")
        self.save_preset_btn.setToolTip(
            "Store the current project setup, licence server, workarea and "
            "design paths under a name you choose.")
        self.save_preset_btn.clicked.connect(self.on_save_preset)
        preset_row.addWidget(self.save_preset_btn, 0)

        self.delete_preset_btn = QPushButton("Delete")
        self.delete_preset_btn.setEnabled(False)
        self.delete_preset_btn.clicked.connect(self.on_delete_preset)
        preset_row.addWidget(self.delete_preset_btn, 0)

        preset_holder = QWidget()
        preset_holder.setLayout(preset_row)
        form.addRow("Saved configuration:", preset_holder)
        self._refresh_preset_combo()
        return box

    def _build_setup_box(self) -> QGroupBox:
        box = QGroupBox("Project setup")
        form = QFormLayout(box)

        self.proj_edit = QLineEdit()
        self.proj_edit.setPlaceholderText("project name passed to -proj")
        self.proj_edit.textChanged.connect(self._on_input_changed)
        form.addRow("Project (-proj):", self.proj_edit)

        self.cfg_edit = QLineEdit()
        self.cfg_edit.setPlaceholderText("config file passed to -cfg")
        self.cfg_edit.textChanged.connect(self._on_input_changed)
        form.addRow("Config (-cfg):", self.cfg_edit)

        self.ward_row = _PathRow(directory=True)
        self.ward_row.edit.setPlaceholderText(
            "optional — defaults to the working directory of the setup shell")
        self.ward_row.changed.connect(self._on_input_changed)
        form.addRow("Workarea (-ward):", self.ward_row)

        self.licence_edit = QLineEdit()
        self.licence_edit.setPlaceholderText(
            "port@host[:port@host…] — blank uses the profile's value")
        self.licence_edit.textChanged.connect(self._on_input_changed)
        form.addRow("Licence server:", self.licence_edit)
        return box

    def _build_inputs_box(self) -> QGroupBox:
        box = QGroupBox("Design inputs")
        layout = QVBoxLayout(box)

        mode_row = QHBoxLayout()
        self.mode_individual = QRadioButton("Enter each path")
        self.mode_run_dir = QRadioButton("Derive from an ATPG run directory")
        self.mode_individual.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_individual)
        self.mode_group.addButton(self.mode_run_dir)
        self.mode_individual.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_individual)
        mode_row.addWidget(self.mode_run_dir)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.run_dir_widget = QWidget()
        run_form = QFormLayout(self.run_dir_widget)
        run_form.setContentsMargins(0, 0, 0, 0)
        run_row = QHBoxLayout()
        self.run_dir_row = _PathRow(directory=True)
        run_row.addWidget(self.run_dir_row, 1)
        self.fill_btn = QPushButton("Fill paths")
        self.fill_btn.setToolTip(
            "Search the run directory using the profile's patterns and fill in "
            "the paths below. Each one stays editable.")
        self.fill_btn.clicked.connect(self.on_fill_from_run_dir)
        run_row.addWidget(self.fill_btn, 0)
        run_holder = QWidget()
        run_holder.setLayout(run_row)
        run_form.addRow("Run directory:", run_holder)
        self.run_dir_widget.setVisible(False)
        layout.addWidget(self.run_dir_widget)

        self.paths_widget = QWidget()
        self.paths_form = QFormLayout(self.paths_widget)
        self.paths_form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.paths_widget)

        self.use_analysis_faults = QCheckBox(
            "Use the fault list from the analysis inputs")
        self.use_analysis_faults.setChecked(True)
        self.use_analysis_faults.toggled.connect(self._on_use_analysis_faults)
        layout.addWidget(self.use_analysis_faults)
        return box

    def _build_commands_box(self) -> QGroupBox:
        box = QGroupBox("Commands that will run")
        layout = QVBoxLayout(box)

        note = QLabel(
            "This is the dofile, verbatim. Edit it to change what runs — an "
            "edited dofile is used exactly as written and is not re-checked "
            "against the inputs above.")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.commands_view = QPlainTextEdit()
        self.commands_view.setMinimumHeight(140)
        self.commands_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.commands_view.textChanged.connect(self._on_commands_edited)
        layout.addWidget(self.commands_view, 1)

        row = QHBoxLayout()
        regen = QPushButton("Regenerate")
        regen.setToolTip("Rebuild the dofile from the inputs, discarding edits.")
        regen.clicked.connect(self.refresh_preview)
        row.addWidget(regen)
        copy_btn = QPushButton("Copy commands")
        copy_btn.setToolTip(
            "Copy the whole chain — setup, environment, tool, commands — so it "
            "can be run by hand.")
        copy_btn.clicked.connect(self.on_copy_commands)
        row.addWidget(copy_btn)
        clear_extra = QPushButton("Clear added faults")
        clear_extra.setToolTip(
            "Remove the commands added by 'Inspect in Visualizer'.")
        clear_extra.clicked.connect(self.clear_extra_commands)
        row.addWidget(clear_extra)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.launch_btn = QPushButton("Launch Visualizer")
        self.launch_btn.setToolTip(
            "Open a terminal running the whole chain. The session is detached, "
            "so it keeps running if this window closes.")
        self.launch_btn.clicked.connect(self.on_launch)
        row.addWidget(self.launch_btn)

        self.open_dir_btn = QPushButton("Open script folder")
        self.open_dir_btn.setEnabled(False)
        self.open_dir_btn.clicked.connect(self.on_open_script_folder)
        row.addWidget(self.open_dir_btn)

        self.session_label = QLabel("No live session")
        self.session_label.setToolTip(
            "A live session accepts 'Open signal in Tessent Visualizer' from "
            "the other tabs.")
        row.addWidget(self.session_label)
        row.addStretch(1)
        return row

    def _build_log_box(self) -> QGroupBox:
        box = QGroupBox("Tool log")
        layout = QVBoxLayout(box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText(
            "The vendor tool's log appears here once a session is launched.")
        layout.addWidget(self.log_view)
        return box

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------
    def reload_profiles(self) -> None:
        current = self.current_profile_name()
        self._profiles = list_profiles()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self._profiles:
            label = profile.title
            if profile.is_template:
                label += "  (template — placeholder paths, cannot launch)"
            self.profile_combo.addItem(label, profile.name)
        self.profile_combo.blockSignals(False)
        if current:
            self.select_profile(current)
        elif self._profiles:
            # Never default to the shipped template just because it sorts
            # first; a real profile is what the user came here to launch.
            self.select_profile(self._first_real_profile_name())
        if not self._profiles:
            self.profile_note.setText(
                "No launch profiles were found. Add a JSON profile to the "
                "profiles directory, or set $ATPG_TOOL_PROFILES.")
            self.launch_btn.setEnabled(False)
            return
        self.launch_btn.setEnabled(True)
        self._on_profile_changed()

    def _first_real_profile_name(self) -> str:
        """The first non-template profile, else whatever is first."""
        for profile in self._profiles:
            if not profile.is_template:
                return profile.name
        return self._profiles[0].name if self._profiles else ""

    def _template_message(self, profile: ToolProfile) -> str:
        real = [p.title for p in self._profiles if not p.is_template]
        hint = (f"Pick your site profile instead: {', '.join(real)}." if real
                else "Copy it to <site>.json in the profiles directory and "
                     "fill in the real executables.")
        return (f"'{profile.title}' is the sanitised template shipped with "
                f"the repository; its executables are placeholders. {hint}")

    def current_profile(self) -> Optional[ToolProfile]:
        name = self.current_profile_name()
        for profile in self._profiles:
            if profile.name == name:
                return profile
        return None

    def current_profile_name(self) -> str:
        data = self.profile_combo.currentData()
        return str(data) if data else ""

    def select_profile(self, name: str) -> None:
        index = self.profile_combo.findData(name)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)

    def _on_profile_changed(self, *_args: Any) -> None:
        profile = self.current_profile()
        if profile is None:
            return
        note = profile.description or ""
        if profile.source_path:
            note = (note + "  ") if note else ""
            note += f"Defined in {profile.source_path}"
        if profile.is_template:
            note = self._template_message(profile) + "  " + note
        self.profile_note.setText(note)
        self.launch_btn.setEnabled(not profile.is_template)
        self.launch_btn.setToolTip(
            self._template_message(profile) if profile.is_template else "")
        if not self.proj_edit.text().strip():
            self.proj_edit.setText(profile.psetup.proj)
        if not self.cfg_edit.text().strip():
            self.cfg_edit.setText(profile.psetup.cfg)
        self._rebuild_path_rows(profile)
        self.refresh_preview()
        self._notify_config_changed()

    def _rebuild_path_rows(self, profile: ToolProfile) -> None:
        """Rebuild the path form for this profile, keeping values by key."""
        previous = {key: row.path() for key, row in self._path_rows.items()}
        while self.paths_form.rowCount():
            self.paths_form.removeRow(0)
        self._path_rows = {}
        for entry in profile.commands.load:
            row = _PathRow()
            row.changed.connect(self._on_input_changed)
            row.set_path(previous.get(entry.key, ""))
            label = entry.label or entry.key
            if not entry.required:
                label += " (optional)"
            self.paths_form.addRow(f"{label}:", row)
            self._path_rows[entry.key] = row
        self._apply_analysis_faults()

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    def set_analysis_faults(self, path: str) -> None:
        """Tell the panel which fault list the analysis ran on."""
        self._analysis_faults = (path or "").strip()
        self._apply_analysis_faults()

    def _apply_analysis_faults(self) -> None:
        row = self._path_rows.get("faults")
        if row is None:
            return
        linked = self.use_analysis_faults.isChecked() and bool(self._analysis_faults)
        if linked:
            row.set_path(self._analysis_faults)
        row.setEnabled(not linked)

    def _on_use_analysis_faults(self, checked: bool) -> None:
        self._apply_analysis_faults()
        self.refresh_preview()
        self._notify_config_changed()

    def _on_mode_changed(self, _checked: bool) -> None:
        self.run_dir_widget.setVisible(self.mode_run_dir.isChecked())
        self._notify_config_changed()

    def _on_input_changed(self, *_args: Any) -> None:
        self.refresh_preview()
        self._notify_config_changed()

    def _on_commands_edited(self) -> None:
        self._dofile_edited = self.commands_view.toPlainText() != self._generated_text

    def current_inputs(self) -> VisualizerInputs:
        paths = {key: row.path() for key, row in self._path_rows.items()
                 if row.path()}
        return VisualizerInputs(
            proj=self.proj_edit.text().strip(),
            cfg=self.cfg_edit.text().strip(),
            ward=self.ward_row.path(),
            licence_server=self.licence_edit.text().strip(),
            paths=paths,
            extra_commands=list(self._extra_commands),
        )

    def on_fill_from_run_dir(self) -> None:
        profile = self.current_profile()
        if profile is None:
            return
        found, warnings = derive_paths_from_run_dir(profile, self.run_dir_row.path())
        for key, path in found.items():
            row = self._path_rows.get(key)
            if row is not None and row.isEnabled():
                row.set_path(path)
        filled = ", ".join(sorted(found)) or "nothing"
        message = f"Filled: {filled}."
        if warnings:
            message += "  " + "  ".join(warnings)
        self._set_status(message, bool(warnings))
        self.refresh_preview()

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    @property
    def _generated_text(self) -> str:
        return self._last_generated

    def refresh_preview(self) -> None:
        """Rebuild the dofile preview, discarding any hand edits."""
        profile = self.current_profile()
        if profile is None:
            return
        try:
            text = build_dofile(profile, self.current_inputs())
        except LaunchInputError as exc:
            text = f"# Cannot build the commands yet:\n# {exc}\n"
        self._last_generated = text
        self.commands_view.blockSignals(True)
        self.commands_view.setPlainText(text)
        self.commands_view.blockSignals(False)
        self._dofile_edited = False

    def add_fault_commands(self, fault_path: str, stuck_value: str = "") -> bool:
        """Append the profile's inspection commands for one fault."""
        profile = self.current_profile()
        if profile is None:
            self._set_status("No launch profile is selected.", True)
            return False
        if not profile.commands.fault_inspect:
            self._set_status(
                f"Profile '{profile.title}' defines no fault-inspection commands.",
                True)
            return False
        try:
            commands = fault_inspect_commands(profile, fault_path, stuck_value)
        except LaunchInputError as exc:
            self._set_status(str(exc), True)
            return False
        if not commands:
            self._set_status(
                "That fault has no stuck-at value recorded, and every "
                "inspection command in this profile needs one.", True)
            return False
        added = [c for c in commands if c not in self._extra_commands]
        self._extra_commands.extend(added)
        self.refresh_preview()
        self._set_status(
            f"Added {len(added)} command(s) for {fault_path}. "
            "They run after the viewer opens." if added
            else f"{fault_path} is already in the command list.")
        return True

    def clear_extra_commands(self) -> None:
        self._extra_commands = []
        self.refresh_preview()
        self._set_status("Cleared the added fault commands.")

    def on_copy_commands(self) -> None:
        profile = self.current_profile()
        if profile is None:
            return
        from ..launcher.visualizer import describe_chain
        try:
            steps = describe_chain(profile, self.current_inputs())
        except LaunchInputError as exc:
            self._set_status(f"Cannot build the chain: {exc}", True)
            return
        header = [
            "# Run these in order. The first line starts a new shell;",
            "# everything after 'tessent -shell' is typed at the tool prompt.",
        ]
        QApplication.clipboard().setText("\n".join(header + steps))
        self._set_status("The whole chain was copied to the clipboard.")

    # ------------------------------------------------------------------
    # Launching
    # ------------------------------------------------------------------
    def on_launch(self) -> None:
        profile = self.current_profile()
        if profile is None:
            self._set_status("No launch profile is selected.", True)
            return
        if profile.is_template:
            self._set_status("Cannot launch: " + self._template_message(profile),
                             True)
            return
        inputs = self.current_inputs()
        missing = missing_inputs(profile, inputs)
        if missing:
            self._set_status("Still needed: " + ", ".join(missing), True)
            return
        if find_terminal(profile.terminal_preference) is None:
            self._set_status(
                "No supported terminal emulator was found. Use 'Copy commands' "
                "and run the chain by hand.", True)
            return

        override = self.commands_view.toPlainText() if self._dofile_edited else None
        try:
            bundle = write_launch_bundle(profile, inputs, dofile_text=override)
        except (LaunchInputError, ProfileError, OSError) as exc:
            self._set_status(f"Cannot launch: {exc}", True)
            return

        ok, pid = QProcess.startDetached(bundle.argv[0], bundle.argv[1:])
        if not ok:
            self._set_status(
                f"The terminal ({bundle.terminal}) could not be started. "
                f"The scripts are in {bundle.directory} and can be run by hand.",
                True)
            self._enable_script_folder(bundle.directory)
            return

        self._enable_script_folder(bundle.directory)
        self._start_log_tail(bundle.log_path)
        self._begin_session(bundle)
        message = (f"Launched in {bundle.terminal} (pid {pid}). "
                   f"Loading the design takes a while; the viewer opens when "
                   f"the commands finish.")
        if bundle.warnings:
            message += "  " + "  ".join(bundle.warnings)
        self._set_status(message, bool(bundle.warnings))
        self.status_message.emit("Tessent Visualizer launching…")

    # ------------------------------------------------------------------
    # The live control channel
    # ------------------------------------------------------------------
    def _begin_session(self, bundle) -> None:
        self._session_ready = False
        if not bundle.token or not bundle.port_file:
            self._session = None
            self._set_session_label(
                "No control channel (this profile disables it)")
            return
        self._session = LiveSession(bundle.port_file, bundle.token)
        self._set_session_label("Session starting…")
        self._session_timer.start()

    def _poll_session(self) -> None:
        if self._session is None:
            self._session_timer.stop()
            return
        if self._session.port is None:
            return
        self._session_timer.stop()
        self._session_ready = True
        self._set_session_label(f"Live session on port {self._session.port}")

    def _set_session_label(self, text: str) -> None:
        self.session_label.setText(text)
        self.session_label.setStyleSheet(
            "color: #1a7f37;" if self._session_ready else "color: #777;")

    def has_live_session(self) -> bool:
        return self._session is not None and self._session.port is not None

    def signal_actions(self) -> List[InspectAction]:
        """The profile's 'show this object' actions, for building a menu."""
        profile = self.current_profile()
        if profile is None:
            return []
        try:
            return signal_inspect_actions(profile)
        except Exception as exc:
            logger.warning("profile %s has a bad signal_inspect entry: %s",
                           profile.name, exc)
            return []

    def show_signal(self, obj: str, action: Optional[InspectAction] = None,
                    ) -> bool:
        """Display *obj* in the running viewer.  Returns True on success."""
        actions = self.signal_actions()
        if action is None:
            if not actions:
                self._set_status(
                    "This profile defines no way to show an object in the "
                    "viewer (commands.signal_inspect is empty).", True)
                return False
            action = actions[0]
        if self._session is None:
            self._set_status(
                f"No live session. Launch the viewer first, then '{obj}' can "
                f"be shown in it. The command would be: "
                f"{action.rendered(obj)}", True)
            return False
        try:
            reply = self._session.send(action, obj)
        except LiveSessionError as exc:
            self._set_status(f"Could not show {obj}: {exc}", True)
            return False
        self._set_status(
            f"Showing {obj} in the viewer ({action.label})."
            + (f" Tool said: {reply}" if reply else ""))
        self.status_message.emit(f"Sent {obj} to Tessent Visualizer.")
        return True

    def on_open_script_folder(self) -> None:
        if self._script_dir and os.path.isdir(self._script_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._script_dir))

    def _enable_script_folder(self, directory: str) -> None:
        self._script_dir = directory
        self.open_dir_btn.setEnabled(True)
        self.open_dir_btn.setToolTip(f"Generated scripts are in {directory}")

    # ------------------------------------------------------------------
    # Log tailing
    # ------------------------------------------------------------------
    def _start_log_tail(self, path: str) -> None:
        self._log_path = path
        self._log_offset = 0
        self.log_view.clear()
        self.log_view.appendPlainText(f"[watching {path}]")
        self._log_timer.start()

    def _poll_log(self) -> None:
        if not self._log_path or not os.path.isfile(self._log_path):
            return
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._log_offset)
                chunk = fh.read()
                self._log_offset = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        self.log_view.appendPlainText(chunk.rstrip("\n"))
        document = self.log_view.toPlainText()
        if len(document) > MAX_LOG_CHARS:
            self.log_view.setPlainText(document[-MAX_LOG_CHARS:])

    def stop_log_tail(self) -> None:
        self._log_timer.stop()
        self._session_timer.stop()

    # ------------------------------------------------------------------
    # Status and settings
    # ------------------------------------------------------------------
    def _set_status(self, text: str, warning: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #a05000;" if warning else "")
        if text:
            logger.debug("visualizer panel: %s", text)

    def _notify_config_changed(self, *_args: Any) -> None:
        self.config_changed.emit()

    # ------------------------------------------------------------------
    # Saved configurations
    # ------------------------------------------------------------------
    def preset_names(self) -> List[str]:
        """Every saved configuration name, in display order."""
        return sorted(self._presets, key=str.lower)

    def save_preset(self, name: str) -> bool:
        """Store the current form under *name*, replacing any existing one."""
        name = (name or "").strip()
        if not name:
            return False
        self._presets[name] = self._form_config()
        self._refresh_preset_combo(select=name)
        self._set_status(f"Saved configuration '{name}'.")
        self._notify_config_changed()
        return True

    def load_preset(self, name: str) -> bool:
        """Apply the saved configuration *name* to the form."""
        cfg = self._presets.get((name or "").strip())
        if cfg is None:
            return False
        self._apply_form_config(cfg)
        self._refresh_preset_combo(select=name)
        self._set_status(f"Loaded configuration '{name}'.")
        self._notify_config_changed()
        return True

    def delete_preset(self, name: str) -> bool:
        """Forget the saved configuration *name*."""
        if (name or "").strip() not in self._presets:
            return False
        del self._presets[name.strip()]
        self._refresh_preset_combo()
        self._set_status(f"Deleted configuration '{name}'.")
        self._notify_config_changed()
        return True

    def current_preset_name(self) -> str:
        """The saved configuration currently selected, or ``""``."""
        return str(self.preset_combo.currentData() or "")

    def _refresh_preset_combo(self, select: str = "") -> None:
        keep = select or self.current_preset_name()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem(_UNSAVED_LABEL, "")
        for name in self.preset_names():
            self.preset_combo.addItem(name, name)
        index = self.preset_combo.findData(keep)
        self.preset_combo.setCurrentIndex(index if index >= 0 else 0)
        self.preset_combo.blockSignals(False)
        has_selection = bool(self.current_preset_name())
        self.delete_preset_btn.setEnabled(has_selection)

    def _on_preset_selected(self, *_args: Any) -> None:
        name = self.current_preset_name()
        if name:
            self.load_preset(name)

    def on_save_preset(self) -> None:
        """Prompt for a name and store the current form under it."""
        suggested = self.current_preset_name() or self.proj_edit.text().strip()
        name, accepted = QInputDialog.getText(
            self, "Save configuration",
            "Name for this launch configuration:", text=suggested)
        if not accepted:
            return
        if not self.save_preset(name):
            self._set_status("A configuration needs a name to be saved.",
                             warning=True)

    def on_delete_preset(self) -> None:
        """Delete the selected saved configuration, after confirming."""
        name = self.current_preset_name()
        if not name:
            return
        confirm = QMessageBox.question(
            self, "Delete configuration",
            f"Delete the saved configuration '{name}'?")
        if confirm == QMessageBox.StandardButton.Yes:
            self.delete_preset(name)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _form_config(self) -> Dict[str, Any]:
        """The launch form on its own, without the saved-configuration store."""
        return {
            "profile": self.current_profile_name(),
            "proj": self.proj_edit.text().strip(),
            "cfg": self.cfg_edit.text().strip(),
            "ward": self.ward_row.path(),
            "licence_server": self.licence_edit.text().strip(),
            "paths": {key: row.path() for key, row in self._path_rows.items()},
            "run_dir": self.run_dir_row.path(),
            "mode": "run_dir" if self.mode_run_dir.isChecked() else "individual",
            "use_analysis_faults": self.use_analysis_faults.isChecked(),
        }

    def _apply_form_config(self, cfg: Dict[str, Any]) -> None:
        if not isinstance(cfg, dict):
            return
        name = str(cfg.get("profile", ""))
        if name:
            # A saved selection of the template is a stale default from before
            # templates were told apart; fall back to the real profile.
            chosen = next((p for p in self._profiles if p.name == name), None)
            if chosen is not None and chosen.is_template:
                real = self._first_real_profile_name()
                if real and real != name:
                    name = real
                    self._set_status(
                        f"The saved profile was the template; switched to "
                        f"'{real}'.")
            # Selecting the profile rebuilds the path rows, so it must happen
            # before the saved paths are written into them.
            self.select_profile(name)
        self.proj_edit.setText(str(cfg.get("proj", "")))
        self.cfg_edit.setText(str(cfg.get("cfg", "")))
        self.ward_row.set_path(str(cfg.get("ward", "")))
        self.licence_edit.setText(str(cfg.get("licence_server", "")))
        self.run_dir_row.set_path(str(cfg.get("run_dir", "")))
        if str(cfg.get("mode", "")) == "run_dir":
            self.mode_run_dir.setChecked(True)
        else:
            self.mode_individual.setChecked(True)
        self.use_analysis_faults.setChecked(
            bool(cfg.get("use_analysis_faults", True)))
        for key, value in dict(cfg.get("paths") or {}).items():
            row = self._path_rows.get(str(key))
            if row is not None:
                row.set_path(str(value))
        self.refresh_preview()

    def export_settings(self) -> Dict[str, Any]:
        """Serialise the form and the saved configurations for persistence.

        Both live in the same blob so the existing per-user settings file
        carries them with no extra wiring; the panel itself does no file IO.
        """
        payload = self._form_config()
        payload["presets"] = {name: dict(cfg)
                              for name, cfg in self._presets.items()}
        payload["last_preset"] = self.current_preset_name()
        return payload

    def import_settings(self, cfg: Dict[str, Any]) -> None:
        if not isinstance(cfg, dict):
            return
        self._presets = {
            str(name): dict(value)
            for name, value in dict(cfg.get("presets") or {}).items()
            if isinstance(value, dict)
        }
        self._apply_form_config(cfg)
        # The form was just restored from the last session, so the remembered
        # configuration is only marked as selected, never re-applied over it.
        self._refresh_preset_combo(select=str(cfg.get("last_preset", "")))
