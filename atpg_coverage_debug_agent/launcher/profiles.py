"""Project launch profiles, loaded from JSON data files.

A profile describes how to reach a vendor tool for one project: the project
setup wrapper, the environment it needs, the tool binary, and the ordered
commands that load a design.  Adding a project is dropping a JSON file into the
profile directory -- no code change, and no project identifier ever enters this
package.

Search order for profiles:

1. every directory listed in ``$ATPG_TOOL_PROFILES`` (colon separated),
2. the repository's ``profiles/`` directory.

A profile found earlier wins, so a user directory can shadow a shipped one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Environment variable holding extra profile directories.
PROFILE_PATH_ENV = "ATPG_TOOL_PROFILES"

#: Placeholder a fault-inspection command may use for the fault path.
FAULT_PLACEHOLDER = "fault"

#: Placeholder a fault-inspection command may use for the stuck-at value.
STUCK_PLACEHOLDER = "stuck"


class ProfileError(Exception):
    """A profile file is missing, malformed, or names an unknown field."""


@dataclass
class LoadCommand:
    """One design-loading command, e.g. ``read_faults <path> -retain``."""

    key: str
    command: str
    label: str = ""
    switches: List[str] = field(default_factory=list)
    required: bool = True
    #: Glob, relative to an ATPG run directory, used to auto-fill this path.
    run_dir_glob: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LoadCommand":
        missing = [k for k in ("key", "command") if not data.get(k)]
        if missing:
            raise ProfileError(
                f"load command is missing required field(s): {', '.join(missing)}")
        return cls(
            key=str(data["key"]),
            command=str(data["command"]),
            label=str(data.get("label", "")) or str(data["key"]),
            switches=[str(s) for s in data.get("switches", [])],
            required=bool(data.get("required", True)),
            run_dir_glob=str(data.get("run_dir_glob", "")),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "command": self.command,
            "label": self.label,
            "switches": list(self.switches),
            "required": self.required,
            "run_dir_glob": self.run_dir_glob,
        }


@dataclass
class PsetupSpec:
    """The project-setup wrapper that establishes the tool environment."""

    executable: str = ""
    proj: str = ""
    cfg: str = ""
    proj_flag: str = "-proj"
    cfg_flag: str = "-cfg"
    ward_flag: str = "-ward"
    #: Flag that runs a command inside the setup shell.  The probed wrapper
    #: offers ``-x`` (stay in the shell afterwards) and ``-cmd`` (exit).
    command_flag: str = "-x"
    extra_args: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PsetupSpec":
        return cls(
            executable=str(data.get("executable", "")),
            proj=str(data.get("proj", "")),
            cfg=str(data.get("cfg", "")),
            proj_flag=str(data.get("proj_flag", "-proj")),
            cfg_flag=str(data.get("cfg_flag", "-cfg")),
            ward_flag=str(data.get("ward_flag", "-ward")),
            command_flag=str(data.get("command_flag", "-x")),
            extra_args=[str(a) for a in data.get("extra_args", [])],
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "executable": self.executable,
            "proj": self.proj,
            "cfg": self.cfg,
            "proj_flag": self.proj_flag,
            "cfg_flag": self.cfg_flag,
            "ward_flag": self.ward_flag,
            "command_flag": self.command_flag,
            "extra_args": list(self.extra_args),
        }


@dataclass
class ToolSpec:
    """The vendor tool binary and the flags used to drive it from a file."""

    executable: str = ""
    shell_flag: str = "-shell"
    dofile_flag: str = "-dofile"
    logfile_flag: str = "-logfile"
    replace_flag: str = "-replace"
    extra_args: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolSpec":
        return cls(
            executable=str(data.get("executable", "")),
            shell_flag=str(data.get("shell_flag", "-shell")),
            dofile_flag=str(data.get("dofile_flag", "-dofile")),
            logfile_flag=str(data.get("logfile_flag", "-logfile")),
            replace_flag=str(data.get("replace_flag", "-replace")),
            extra_args=[str(a) for a in data.get("extra_args", [])],
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "executable": self.executable,
            "shell_flag": self.shell_flag,
            "dofile_flag": self.dofile_flag,
            "logfile_flag": self.logfile_flag,
            "replace_flag": self.replace_flag,
            "extra_args": list(self.extra_args),
        }


@dataclass
class ControlSpec:
    """The live command channel opened inside the tool session.

    ``allowed_commands`` and ``allowed_options`` are enforced *inside* the
    tool, so widening them is a deliberate act recorded in the profile rather
    than something the application can decide at runtime.
    """

    enabled: bool = True
    allowed_commands: List[str] = field(default_factory=list)
    allowed_options: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ControlSpec":
        return cls(
            enabled=bool(data.get("enabled", True)),
            allowed_commands=[str(c) for c in data.get("allowed_commands", [])],
            allowed_options=[str(o) for o in data.get("allowed_options", [])],
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "allowed_commands": list(self.allowed_commands),
            "allowed_options": list(self.allowed_options),
        }


@dataclass
class CommandSpec:
    """The tool commands: set the context, load the design, open the viewer."""

    context: str = ""
    load: List[LoadCommand] = field(default_factory=list)
    open: str = ""
    fault_inspect: List[str] = field(default_factory=list)
    #: Structured actions used by "open signal in the viewer"; each is a verb
    #: plus options, never a command string (see launcher.live_session).
    signal_inspect: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommandSpec":
        return cls(
            context=str(data.get("context", "")),
            load=[LoadCommand.from_dict(d) for d in data.get("load", [])],
            open=str(data.get("open", "")),
            fault_inspect=[str(c) for c in data.get("fault_inspect", [])],
            signal_inspect=[dict(d) for d in data.get("signal_inspect", [])],
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "context": self.context,
            "load": [c.as_dict() for c in self.load],
            "open": self.open,
            "fault_inspect": list(self.fault_inspect),
            "signal_inspect": [dict(d) for d in self.signal_inspect],
        }


@dataclass
class ToolProfile:
    """Everything needed to open the vendor viewer for one project."""

    name: str
    display_name: str = ""
    description: str = ""
    shell: str = "/bin/tcsh"
    psetup: PsetupSpec = field(default_factory=PsetupSpec)
    tool: ToolSpec = field(default_factory=ToolSpec)
    commands: CommandSpec = field(default_factory=CommandSpec)
    control: ControlSpec = field(default_factory=ControlSpec)
    environment: Dict[str, str] = field(default_factory=dict)
    terminal_preference: List[str] = field(default_factory=list)
    #: Absolute path of the file this profile was read from, for display.
    source_path: str = ""

    @property
    def title(self) -> str:
        return self.display_name or self.name

    def load_command(self, key: str) -> Optional[LoadCommand]:
        for entry in self.commands.load:
            if entry.key == key:
                return entry
        return None

    @property
    def load_keys(self) -> List[str]:
        return [entry.key for entry in self.commands.load]

    @classmethod
    def from_dict(cls, data: Dict[str, Any],
                  source_path: str = "") -> "ToolProfile":
        name = str(data.get("name", "")).strip()
        if not name:
            raise ProfileError(f"profile in '{source_path or '<dict>'}' has no name")
        env = {}
        for key, value in (data.get("environment") or {}).items():
            env[str(key)] = str(value)
        return cls(
            name=name,
            display_name=str(data.get("display_name", "")),
            description=str(data.get("description", "")),
            shell=str(data.get("shell", "/bin/tcsh")),
            psetup=PsetupSpec.from_dict(data.get("psetup") or {}),
            tool=ToolSpec.from_dict(data.get("tool") or {}),
            commands=CommandSpec.from_dict(data.get("commands") or {}),
            control=ControlSpec.from_dict(data.get("control") or {}),
            environment=env,
            terminal_preference=[str(t) for t in data.get("terminal_preference", [])],
            source_path=source_path or str(data.get("source_path", "")),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "shell": self.shell,
            "psetup": self.psetup.as_dict(),
            "tool": self.tool.as_dict(),
            "commands": self.commands.as_dict(),
            "control": self.control.as_dict(),
            "environment": dict(self.environment),
            "terminal_preference": list(self.terminal_preference),
            "source_path": self.source_path,
        }

    def copy(self) -> "ToolProfile":
        """Return an independent copy, so a caller may edit it safely."""
        return ToolProfile.from_dict(self.as_dict(), self.source_path)


def _default_profile_dir() -> Path:
    """The ``profiles/`` directory shipped beside the package."""
    return Path(__file__).resolve().parents[2] / "profiles"


def profile_search_path() -> List[Path]:
    """Directories searched for profile JSON files, highest priority first."""
    dirs: List[Path] = []
    raw = os.environ.get(PROFILE_PATH_ENV, "")
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            dirs.append(Path(part).expanduser())
    dirs.append(_default_profile_dir())
    seen: set = set()
    unique: List[Path] = []
    for path in dirs:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def load_profiles(search_path: Optional[Sequence[Path]] = None,
                  ) -> Dict[str, ToolProfile]:
    """Read every profile on the search path.

    A file that cannot be parsed is logged and skipped rather than aborting the
    whole load: one broken profile must not make the others unreachable.
    """
    dirs = list(search_path) if search_path is not None else profile_search_path()
    profiles: Dict[str, ToolProfile] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profile = ToolProfile.from_dict(data, str(path))
            except (OSError, ValueError, ProfileError) as exc:
                logger.warning("Ignoring launch profile '%s': %s", path, exc)
                continue
            profiles.setdefault(profile.name, profile)
    return profiles


def list_profiles(search_path: Optional[Sequence[Path]] = None,
                  ) -> List[ToolProfile]:
    """Every readable profile, sorted by display title."""
    return sorted(load_profiles(search_path).values(), key=lambda p: p.title.lower())


def get_profile(name: str,
                search_path: Optional[Sequence[Path]] = None) -> ToolProfile:
    """Return the profile called *name*, or raise :class:`ProfileError`."""
    profiles = load_profiles(search_path)
    if name in profiles:
        return profiles[name]
    lowered = {key.lower(): key for key in profiles}
    if name.lower() in lowered:
        return profiles[lowered[name.lower()]]
    available = ", ".join(sorted(profiles)) or "none found"
    raise ProfileError(
        f"no launch profile named '{name}'. Available: {available}. "
        f"Searched: {', '.join(str(d) for d in (search_path or profile_search_path()))}")


def with_overrides(profile: ToolProfile, **overrides: Any) -> ToolProfile:
    """Return a copy of *profile* with top-level fields replaced."""
    return replace(profile.copy(), **overrides)
