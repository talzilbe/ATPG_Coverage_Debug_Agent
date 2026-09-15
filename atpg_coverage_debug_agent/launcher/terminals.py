"""Terminal-emulator discovery.

The vendor shell has to stay interactive after the viewer opens, so the launch
runs inside a terminal window rather than as a managed child process.  Each
emulator spells "now run this argv" differently; this module owns that table.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import List, Optional, Sequence

#: Emulators we know how to drive, in default preference order.
#: ``exec_flag`` is the last flag before the command; everything after it is
#: taken as a plain argv, so no shell quoting is involved.
KNOWN_TERMINALS: List["TerminalSpec"] = []


@dataclass(frozen=True)
class TerminalSpec:
    """How to ask one terminal emulator to run an argv in a new window."""

    name: str
    exec_flag: str
    title_flag: str = ""
    #: True when the title is passed as ``--title=X`` rather than two tokens.
    title_joined: bool = False
    executable: str = ""

    def resolve(self) -> Optional["TerminalSpec"]:
        """Return a copy with :attr:`executable` filled in, or None if absent."""
        found = shutil.which(self.name)
        if not found:
            return None
        return TerminalSpec(
            name=self.name,
            exec_flag=self.exec_flag,
            title_flag=self.title_flag,
            title_joined=self.title_joined,
            executable=found,
        )

    def wrap(self, argv: Sequence[str], title: str = "") -> List[str]:
        """Wrap *argv* so it runs in a new window of this emulator."""
        if not self.executable:
            raise ValueError(
                f"terminal '{self.name}' has not been resolved to an executable")
        out: List[str] = [self.executable]
        if title and self.title_flag:
            if self.title_joined:
                out.append(f"{self.title_flag}={title}")
            else:
                out.extend([self.title_flag, title])
        out.append(self.exec_flag)
        out.extend(argv)
        return out


KNOWN_TERMINALS.extend([
    TerminalSpec("xterm", exec_flag="-e", title_flag="-title"),
    TerminalSpec("gnome-terminal", exec_flag="--", title_flag="--title",
                 title_joined=True),
    TerminalSpec("xfce4-terminal", exec_flag="-x", title_flag="--title",
                 title_joined=True),
    TerminalSpec("konsole", exec_flag="-e", title_flag="--title"),
    TerminalSpec("mate-terminal", exec_flag="-x", title_flag="--title",
                 title_joined=True),
    TerminalSpec("x-terminal-emulator", exec_flag="-e", title_flag="-title"),
])


def available_terminals() -> List[TerminalSpec]:
    """Every known emulator actually installed, in preference order."""
    found = []
    for spec in KNOWN_TERMINALS:
        resolved = spec.resolve()
        if resolved is not None:
            found.append(resolved)
    return found


def find_terminal(preference: Optional[Sequence[str]] = None,
                  ) -> Optional[TerminalSpec]:
    """Pick a terminal emulator, honouring *preference* before the defaults.

    A preferred name that is not installed is skipped rather than failing, so a
    profile written for one host still launches on another.
    """
    by_name = {spec.name: spec for spec in KNOWN_TERMINALS}
    order: List[TerminalSpec] = []
    for name in preference or ():
        spec = by_name.get(name)
        if spec is not None:
            order.append(spec)
    order.extend(spec for spec in KNOWN_TERMINALS if spec not in order)
    for spec in order:
        resolved = spec.resolve()
        if resolved is not None:
            return resolved
    return None


def display_available() -> bool:
    """True when an X display looks reachable.

    The viewer is an X client, so a missing display fails only after the design
    has loaded -- minutes in.  Checking up front turns that into an immediate,
    understandable refusal.
    """
    return bool(os.environ.get("DISPLAY", "").strip()
                or os.environ.get("WAYLAND_DISPLAY", "").strip())
