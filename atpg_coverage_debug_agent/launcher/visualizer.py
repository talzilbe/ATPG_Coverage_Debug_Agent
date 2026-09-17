"""Build the command chain that opens the vendor viewer on the analysed design.

Nothing in this module runs anything.  It produces three artefacts and an argv:

``visualizer.dofile``
    the tool commands -- context, the design-loading commands, open the viewer.
``tessent_launch.csh``
    sets the licence environment, then execs the tool with that dofile.
``psetup_launch.csh``
    enters the project setup and runs the inner script inside it, then holds the
    window open so a setup failure is readable instead of a window that blinks
    out of existence.

Every value that reaches a generated file is validated first.  Paths are
brace-quoted for Tcl and single-quoted for the shell; the argv handed to the
terminal is a list, never a command string, so no shell parses it.
"""

from __future__ import annotations

import datetime
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .live_session import (
    PORT_FILE, TOKEN_FILE, InspectAction, build_listener_tcl, new_token,
)
from .profiles import FAULT_PLACEHOLDER, STUCK_PLACEHOLDER, ToolProfile
from .terminals import TerminalSpec, display_available, find_terminal

#: Characters that would break out of a Tcl brace-quoted word.
_TCL_BREAKERS = "{}\\"

#: Characters that would break out of a shell single-quoted word, plus the
#: control characters that have no business in a path.
_SHELL_BREAKERS = "'\"\n\r\t\0"

#: Shell metacharacters.  Quoting already neutralises these, but a real file
#: path never contains them, so refusing them keeps a typo from ever reaching a
#: shell that quotes differently than we expect.
_PATH_UNSAFE = ";|&<>$`()!*?"

#: A project or config token: conservative, and wide enough for "proj/branch".
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/@:+-]+$")

#: ``port@host`` pairs separated by colons.
_LICENCE_RE = re.compile(
    r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+@[A-Za-z0-9._-]+)*$")

#: A shell environment variable name.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A stuck-at value, as it appears in a fault list.
_STUCK_RE = re.compile(r"^[01]$")

DOFILE_NAME = "visualizer.dofile"
TESSENT_SCRIPT_NAME = "tessent_launch.csh"
PSETUP_SCRIPT_NAME = "psetup_launch.csh"
LOG_NAME = "tessent.log"


class LaunchInputError(Exception):
    """An input cannot be placed in a generated script safely or sensibly."""


def _reject_chars(value: str, chars: str, label: str) -> None:
    bad = sorted({c for c in value if c in chars or ord(c) < 32})
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        raise LaunchInputError(
            f"{label} contains character(s) that cannot be quoted safely: {shown}")


def validate_fs_path(value: str, label: str, *, require_exists: bool = True,
                     must_be_dir: bool = False) -> str:
    """Validate a filesystem path destined for a script, and return it."""
    value = (value or "").strip()
    if not value:
        raise LaunchInputError(f"{label} is required")
    _reject_chars(value, _TCL_BREAKERS + _SHELL_BREAKERS + _PATH_UNSAFE, label)
    if require_exists:
        if must_be_dir and not os.path.isdir(value):
            raise LaunchInputError(f"{label} is not a directory: {value}")
        if not must_be_dir and not os.path.isfile(value):
            raise LaunchInputError(f"{label} does not exist: {value}")
    return value


def validate_design_path(value: str, label: str) -> str:
    """Validate a hierarchical design name, e.g. a fault location.

    Bus subscripts are legitimate here, and are literal inside Tcl braces, so
    brackets are allowed where they would be rejected in a filesystem path.
    """
    value = (value or "").strip()
    if not value:
        raise LaunchInputError(f"{label} is required")
    _reject_chars(value, _TCL_BREAKERS + "\n\r\0", label)
    return value


def validate_token(value: str, label: str, *, required: bool = True) -> str:
    """Validate a project/config style token."""
    value = (value or "").strip()
    if not value:
        if required:
            raise LaunchInputError(f"{label} is required")
        return ""
    if not _TOKEN_RE.match(value):
        raise LaunchInputError(
            f"{label} may only contain letters, digits and ._/@:+- -- got: {value}")
    return value


def validate_licence(value: str, label: str = "Licence server",
                     *, required: bool = False) -> str:
    """Validate a ``port@host[:port@host...]`` licence server list."""
    value = (value or "").strip()
    if not value:
        if required:
            raise LaunchInputError(f"{label} is required")
        return ""
    if not _LICENCE_RE.match(value):
        raise LaunchInputError(
            f"{label} must be port@host, colon separated -- got: {value}")
    return value


def validate_env(env: Dict[str, str]) -> Dict[str, str]:
    """Validate environment names and values destined for ``setenv`` lines."""
    clean: Dict[str, str] = {}
    for name, value in env.items():
        name = str(name).strip()
        if not _ENV_NAME_RE.match(name):
            raise LaunchInputError(f"'{name}' is not a valid environment variable name")
        value = str(value)
        _reject_chars(value, _SHELL_BREAKERS, f"value of ${name}")
        clean[name] = value
    return clean


def quote_tcl(value: str) -> str:
    """Brace-quote *value* for Tcl, so nothing inside it is substituted."""
    _reject_chars(value, _TCL_BREAKERS, "path")
    return "{" + value + "}"


def quote_shell(value: str) -> str:
    """Single-quote *value* for the shell."""
    _reject_chars(value, _SHELL_BREAKERS, "value")
    return "'" + value + "'"


@dataclass
class VisualizerInputs:
    """The user-supplied half of a launch: what to load, and where."""

    proj: str = ""
    cfg: str = ""
    ward: str = ""
    licence_server: str = ""
    #: Design paths keyed by the profile's load-command keys.
    paths: Dict[str, str] = field(default_factory=dict)
    extra_commands: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "proj": self.proj,
            "cfg": self.cfg,
            "ward": self.ward,
            "licence_server": self.licence_server,
            "paths": dict(self.paths),
            "extra_commands": list(self.extra_commands),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisualizerInputs":
        paths = {str(k): str(v) for k, v in dict(data.get("paths") or {}).items()}
        extra = list(data.get("extra_commands") or [])
        return cls(
            proj=str(data.get("proj", "")),
            cfg=str(data.get("cfg", "")),
            ward=str(data.get("ward", "")),
            licence_server=str(data.get("licence_server", "")),
            paths=paths,
            extra_commands=[str(c) for c in extra],
        )


@dataclass
class LaunchBundle:
    """The generated artefacts and the argv that runs them."""

    directory: str
    dofile_path: str
    tessent_script_path: str
    psetup_script_path: str
    log_path: str
    argv: List[str]
    terminal: str
    commands: List[str]
    warnings: List[str] = field(default_factory=list)
    #: Control-channel handles; empty when the profile disables the channel.
    port_file: str = ""
    token: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "directory": self.directory,
            "dofile_path": self.dofile_path,
            "tessent_script_path": self.tessent_script_path,
            "psetup_script_path": self.psetup_script_path,
            "log_path": self.log_path,
            "argv": list(self.argv),
            "terminal": self.terminal,
            "commands": list(self.commands),
            "warnings": list(self.warnings),
            "port_file": self.port_file,
            "token": self.token,
        }


def _effective_env(profile: ToolProfile, inputs: VisualizerInputs) -> Dict[str, str]:
    env = dict(profile.environment)
    licence = validate_licence(inputs.licence_server)
    if licence:
        # The profile names which variable carries the licence; if it names
        # none, fall back to the single variable it does define.
        target = next((k for k in env if "LICENSE" in k.upper() or "LICENCE" in k.upper()),
                      "")
        if target:
            env[target] = licence
    return validate_env(env)


def build_commands(profile: ToolProfile, inputs: VisualizerInputs,
                   extra_commands: Sequence[str] = ()) -> List[str]:
    """Return the ordered tool commands for this launch."""
    commands: List[str] = []
    if profile.commands.context:
        commands.append(profile.commands.context)
    for entry in profile.commands.load:
        raw = (inputs.paths.get(entry.key) or "").strip()
        if not raw:
            if entry.required:
                raise LaunchInputError(
                    f"{entry.label or entry.key} is required by profile "
                    f"'{profile.title}' (command '{entry.command}')")
            continue
        path = validate_fs_path(raw, entry.label or entry.key)
        parts = [entry.command, quote_tcl(path)]
        parts.extend(entry.switches)
        commands.append(" ".join(parts))
    if profile.commands.open:
        commands.append(profile.commands.open)
    for command in list(inputs.extra_commands) + list(extra_commands):
        command = command.strip()
        if command:
            _reject_chars(command, "\n\r\0", "extra command")
            commands.append(command)
    return commands


def build_dofile(profile: ToolProfile, inputs: VisualizerInputs,
                 extra_commands: Sequence[str] = (),
                 listener_tcl: str = "") -> str:
    """Render the tool dofile.

    *listener_tcl* goes first so the control channel exists before the design
    starts loading, which can take minutes.
    """
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    lines = [
        "# Tessent dofile generated by the ATPG Coverage Debug Agent.",
        f"# profile: {profile.name}",
        f"# generated: {stamp}",
        "# Every path is brace-quoted, so Tcl performs no substitution on it.",
        "",
    ]
    if listener_tcl:
        lines.append(listener_tcl)
        lines.append("")
    lines.extend(build_commands(profile, inputs, extra_commands))
    lines.append("")
    return "\n".join(lines)


def build_tessent_script(profile: ToolProfile, inputs: VisualizerInputs,
                         dofile_path: str, log_path: str) -> str:
    """Render the inner script: set the environment, then exec the tool."""
    if not profile.tool.executable:
        raise LaunchInputError(
            f"profile '{profile.title}' does not name a tool executable")
    exe = validate_fs_path(profile.tool.executable, "Tool executable")
    dofile = validate_fs_path(dofile_path, "Dofile", require_exists=False)
    log = validate_fs_path(log_path, "Log file", require_exists=False)
    env = _effective_env(profile, inputs)

    lines = [
        f"#!{profile.shell} -f",
        "# Generated by the ATPG Coverage Debug Agent. Runs inside project setup.",
        "",
    ]
    for name in sorted(env):
        lines.append(f"setenv {name} {quote_shell(env[name])}")
    if env:
        lines.append("")
    argv = [quote_shell(exe), profile.tool.shell_flag]
    argv.extend(profile.tool.extra_args)
    argv.extend([profile.tool.dofile_flag, quote_shell(dofile)])
    if profile.tool.logfile_flag:
        argv.extend([profile.tool.logfile_flag, quote_shell(log)])
    if profile.tool.replace_flag:
        argv.append(profile.tool.replace_flag)
    lines.append("exec " + " ".join(argv))
    lines.append("")
    return "\n".join(lines)


def build_psetup_script(profile: ToolProfile, inputs: VisualizerInputs,
                        tessent_script_path: str) -> str:
    """Render the outer script: enter project setup, run the inner script.

    The window is held open afterwards on purpose.  Without it a project-setup
    failure closes the terminal instantly and the user has nothing to read.
    """
    psetup = profile.psetup
    if not psetup.executable:
        raise LaunchInputError(
            f"profile '{profile.title}' does not name a project-setup executable")
    exe = validate_fs_path(psetup.executable, "Project setup executable")
    inner = validate_fs_path(tessent_script_path, "Inner script",
                             require_exists=False)
    proj = validate_token(inputs.proj or psetup.proj, "Project (-proj)")
    cfg = validate_token(inputs.cfg or psetup.cfg, "Config (-cfg)", required=False)
    ward = inputs.ward.strip()
    if ward:
        ward = validate_fs_path(ward, "Workarea (-ward)", require_exists=True,
                                must_be_dir=True)

    parts = [quote_shell(exe), psetup.proj_flag, quote_shell(proj)]
    if cfg:
        parts.extend([psetup.cfg_flag, quote_shell(cfg)])
    if ward:
        parts.extend([psetup.ward_flag, quote_shell(ward)])
    parts.extend(psetup.extra_args)
    parts.extend([psetup.command_flag, quote_shell(inner)])

    return "\n".join([
        f"#!{profile.shell} -f",
        "# Generated by the ATPG Coverage Debug Agent.",
        "",
        " ".join(parts),
        "set rc = $status",
        "if ( $rc != 0 ) then",
        '    echo ""',
        '    echo "Project setup exited with status $rc."',
        "endif",
        'echo ""',
        'echo "Session ended. Press Enter to close this window."',
        'set reply = "$<"',
        "",
    ])


def build_launch_argv(profile: ToolProfile, psetup_script_path: str,
                      terminal: Optional[TerminalSpec] = None,
                      title: str = "") -> Tuple[List[str], TerminalSpec]:
    """Wrap the outer script in a terminal emulator; return (argv, terminal)."""
    term = terminal or find_terminal(profile.terminal_preference)
    if term is None:
        raise LaunchInputError(
            "no supported terminal emulator was found on this host "
            "(looked for: xterm, gnome-terminal, xfce4-terminal, konsole). "
            "Use 'Copy commands' and run the chain by hand.")
    argv = term.wrap([psetup_script_path], title or f"Tessent — {profile.title}")
    return argv, term


def _write(path: str, text: str, mode: int) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)


def write_launch_bundle(profile: ToolProfile, inputs: VisualizerInputs,
                        dest_dir: Optional[str] = None,
                        terminal: Optional[TerminalSpec] = None,
                        extra_commands: Sequence[str] = (),
                        dofile_text: Optional[str] = None) -> LaunchBundle:
    """Validate the inputs, write the three artefacts, and return the bundle.

    *dofile_text* replaces the generated dofile verbatim.  It is the escape
    hatch for a user who edited the commands in the preview: their text is used
    as typed, exactly as if they had typed it at the tool's own prompt.
    """
    if profile.is_template:
        raise LaunchInputError(
            f"profile '{profile.title}' is the sanitised template shipped with "
            "the repository (its executables are /path/to/... placeholders). "
            "Select a site profile, or copy the template to <site>.json in the "
            "profiles directory and fill in the real paths.")
    if dest_dir is None:
        from ..session import session_dir  # local import keeps this module standalone
        dest_dir = session_dir(design="visualizer")
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)

    dofile_path = os.path.join(dest_dir, DOFILE_NAME)
    tessent_path = os.path.join(dest_dir, TESSENT_SCRIPT_NAME)
    psetup_path = os.path.join(dest_dir, PSETUP_SCRIPT_NAME)
    log_path = os.path.join(dest_dir, LOG_NAME)
    port_path = os.path.join(dest_dir, PORT_FILE)
    token_path = os.path.join(dest_dir, TOKEN_FILE)

    listener = ""
    token = ""
    if profile.control.enabled and profile.control.allowed_commands:
        token = new_token()
        listener = build_listener_tcl(
            port_path, token, profile.control.allowed_commands,
            profile.control.allowed_options)

    warnings: List[str] = []
    if dofile_text is None:
        commands = build_commands(profile, inputs, extra_commands)
        dofile = build_dofile(profile, inputs, extra_commands, listener)
    else:
        dofile = dofile_text
        commands = [line.strip() for line in dofile_text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        warnings.append(
            "The commands were edited by hand and are used exactly as written; "
            "they were not checked against the inputs above.")

    tessent_script = build_tessent_script(profile, inputs, dofile_path, log_path)
    psetup_script = build_psetup_script(profile, inputs, tessent_path)
    argv, term = build_launch_argv(profile, psetup_path, terminal)

    # A stale port file would point the GUI at a dead or unrelated session.
    for path in (port_path, token_path):
        try:
            os.remove(path)
        except OSError:
            pass

    _write(dofile_path, dofile, stat.S_IRUSR | stat.S_IWUSR)
    _write(tessent_path, tessent_script, stat.S_IRWXU)
    _write(psetup_path, psetup_script, stat.S_IRWXU)
    if token:
        _write(token_path, token + "\n", stat.S_IRUSR | stat.S_IWUSR)

    if not display_available():
        warnings.append(
            "$DISPLAY is not set. The viewer is an X client and will fail to "
            "open, most likely only after the design has finished loading.")
    return LaunchBundle(
        directory=dest_dir,
        dofile_path=dofile_path,
        tessent_script_path=tessent_path,
        psetup_script_path=psetup_path,
        log_path=log_path,
        argv=argv,
        terminal=term.name,
        commands=commands,
        warnings=warnings,
        port_file=port_path if token else "",
        token=token,
    )


def signal_inspect_actions(profile: ToolProfile) -> List[InspectAction]:
    """The profile's structured 'show this object' actions."""
    return [InspectAction.from_dict(d) for d in profile.commands.signal_inspect]


def derive_paths_from_run_dir(profile: ToolProfile, run_dir: str,
                              ) -> Tuple[Dict[str, str], List[str]]:
    """Auto-fill the design paths from an ATPG run directory.

    Returns the paths that were found and a warning for every load command that
    matched nothing or matched several files.  An ambiguous match is reported
    and the first candidate offered -- never silently chosen.
    """
    import glob

    warnings: List[str] = []
    found: Dict[str, str] = {}
    run_dir = (run_dir or "").strip()
    if not run_dir:
        return found, ["No run directory given."]
    if not os.path.isdir(run_dir):
        return found, [f"Run directory does not exist: {run_dir}"]

    for entry in profile.commands.load:
        if not entry.run_dir_glob:
            warnings.append(
                f"{entry.label or entry.key}: this profile defines no run-directory "
                f"pattern, so it must be set by hand.")
            continue
        pattern = os.path.join(run_dir, entry.run_dir_glob)
        matches = sorted(glob.glob(pattern))
        matches = [m for m in matches if os.path.isfile(m)]
        if not matches:
            warnings.append(
                f"{entry.label or entry.key}: nothing matched {pattern}")
            continue
        found[entry.key] = matches[0]
        if len(matches) > 1:
            warnings.append(
                f"{entry.label or entry.key}: {len(matches)} files matched "
                f"{pattern}; using the first one -- check it is the right one.")
    return found, warnings


def fault_inspect_commands(profile: ToolProfile, fault_path: str,
                           stuck_value: str = "") -> List[str]:
    """Render the profile's fault-inspection commands for one fault."""
    path = validate_design_path(fault_path, "Fault location")
    stuck = (stuck_value or "").strip()
    if stuck and not _STUCK_RE.match(stuck):
        raise LaunchInputError(f"Stuck-at value must be 0 or 1 -- got: {stuck}")
    rendered: List[str] = []
    for template in profile.commands.fault_inspect:
        if not stuck and "{" + STUCK_PLACEHOLDER + "}" in template:
            continue
        rendered.append(
            template.replace("{" + FAULT_PLACEHOLDER + "}", quote_tcl(path))
                    .replace("{" + STUCK_PLACEHOLDER + "}", stuck))
    return rendered


def describe_chain(profile: ToolProfile, inputs: VisualizerInputs,
                   extra_commands: Sequence[str] = ()) -> List[str]:
    """A human-readable account of the whole chain, for reports and the agent.

    Used where the commands are shown rather than run, so the user can paste
    them by hand if the launch is unavailable.
    """
    psetup = profile.psetup
    proj = (inputs.proj or psetup.proj).strip()
    cfg = (inputs.cfg or psetup.cfg).strip()
    steps: List[str] = []

    setup = f"{psetup.executable} {psetup.proj_flag} {proj}"
    if cfg:
        setup += f" {psetup.cfg_flag} {cfg}"
    if inputs.ward.strip():
        setup += f" {psetup.ward_flag} {inputs.ward.strip()}"
    steps.append(setup)

    env = dict(profile.environment)
    licence = inputs.licence_server.strip()
    if licence:
        target = next((k for k in env if "LICENSE" in k.upper() or "LICENCE" in k.upper()),
                      "")
        if target:
            env[target] = licence
    for name in sorted(env):
        steps.append(f"setenv {name} {env[name]}")

    steps.append(f"{profile.tool.executable} {profile.tool.shell_flag}")
    steps.extend(build_commands(profile, inputs, extra_commands))
    return steps


def missing_inputs(profile: ToolProfile, inputs: VisualizerInputs) -> List[str]:
    """Labels of everything still required before a launch can be attempted."""
    missing: List[str] = []
    if not (inputs.proj or profile.psetup.proj).strip():
        missing.append("Project (-proj)")
    for entry in profile.commands.load:
        if entry.required and not (inputs.paths.get(entry.key) or "").strip():
            missing.append(entry.label or entry.key)
    return missing


def iter_load_entries(profile: ToolProfile) -> Iterable[Tuple[str, str, bool]]:
    """(key, label, required) for each load command, for building a form."""
    for entry in profile.commands.load:
        yield entry.key, entry.label or entry.key, entry.required
