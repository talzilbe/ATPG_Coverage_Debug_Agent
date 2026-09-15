"""A command channel into the running vendor shell.

The launched session is detached, so the GUI holds no process handle on it.
Instead the generated dofile opens a small Tcl listener, and this module talks
to it.  That was measured against the real tool: the shell services the Tcl
event loop while sitting at its interactive prompt, so the user keeps a usable
terminal *and* the GUI can drive the viewer.

Security
--------
This is a channel that makes a design tool execute things, so it is built
closed rather than open:

* the listener binds ``127.0.0.1`` only, on an ephemeral port, and hangs up on
  any peer that is not loopback;
* every request carries a per-session token, generated here and known only
  through the 0700 session directory;
* **no command string is ever transmitted.** The request is structured --
  a verb, one object, and option pairs -- and the Tcl side rebuilds the command
  with ``list``, which quotes each word so the re-parse cannot substitute
  anything. A bracket or ``$`` in an object name is data, never code;
* the verb must appear in the profile's allow-list, and option names and values
  are checked against patterns on both sides.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: File the listener writes its port into, inside the bundle directory.
PORT_FILE = "control.port"

#: File holding the session token, written beside the scripts at 0600.
TOKEN_FILE = "control.token"

#: Field separator on the wire.  Rejected in every value, so it cannot be faked.
SEP = "\t"

#: Default seconds to wait for a reply.  The shell is single threaded: while it
#: is reading a flat model it services no events, so a slow reply means "busy",
#: not "broken".
DEFAULT_TIMEOUT = 5.0

#: Option values we are willing to pass through (colours, tab names, keywords).
_OPTION_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")

#: Option names, e.g. ``-highlight``.
_OPTION_NAME_RE = re.compile(r"^-[A-Za-z][A-Za-z0-9_]*$")

#: A vendor command name.
_VERB_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class LiveSessionError(Exception):
    """The live session could not be reached, or refused the request."""


def new_token() -> str:
    """A fresh per-session token."""
    return secrets.token_hex(16)


def _tcl_literal(value: str) -> str:
    """Embed *value* in generated Tcl as a brace-quoted literal."""
    if any(c in value for c in "{}\\"):
        raise LiveSessionError(
            f"value cannot be embedded in the listener safely: {value!r}")
    return "{" + value + "}"


def build_listener_tcl(port_file: str, token: str,
                       allowed_commands: Sequence[str],
                       allowed_options: Sequence[str]) -> str:
    """Return the Tcl that serves the control channel inside the tool.

    Placed at the top of the dofile so the channel exists before the design
    starts loading; a request that arrives during the load simply does not get
    serviced until the tool is back at its prompt.
    """
    for verb in allowed_commands:
        if not _VERB_RE.match(verb):
            raise LiveSessionError(f"not a valid command name: {verb!r}")
    for name in allowed_options:
        if not _OPTION_NAME_RE.match(name):
            raise LiveSessionError(f"not a valid option name: {name!r}")

    template = r'''
# --- control channel (ATPG Coverage Debug Agent) ----------------------------
# Loopback only, token checked, and the command is rebuilt with [list] so that
# nothing in an object name can be substituted as code.
set ::atpg_token @@TOKEN@@
set ::atpg_allowed_cmds @@CMDS@@
set ::atpg_allowed_opts @@OPTS@@

proc ::atpg_reply {chan status text} {
    catch {puts $chan "$status $text" ; flush $chan}
}

proc ::atpg_serve {chan} {
    if {[catch {gets $chan line} n] || $n < 0} {
        if {[eof $chan]} { catch {close $chan} }
        return
    }
    set parts [split $line \t]
    if {[llength $parts] < 3} {
        ::atpg_reply $chan ERR "malformed request"
        return
    }
    if {![string equal [lindex $parts 0] $::atpg_token]} {
        ::atpg_reply $chan ERR "bad token"
        catch {close $chan}
        return
    }
    set verb [lindex $parts 1]
    set object [lindex $parts 2]
    if {[lsearch -exact $::atpg_allowed_cmds $verb] < 0} {
        ::atpg_reply $chan ERR "command not permitted: $verb"
        return
    }
    set argv [list $verb $object]
    foreach pair [lrange $parts 3 end] {
        if {[string length $pair] == 0} { continue }
        set eq [string first "=" $pair]
        if {$eq < 1} {
            ::atpg_reply $chan ERR "malformed option: $pair"
            return
        }
        set name [string range $pair 0 [expr {$eq - 1}]]
        set value [string range $pair [expr {$eq + 1}] end]
        if {[lsearch -exact $::atpg_allowed_opts $name] < 0} {
            ::atpg_reply $chan ERR "option not permitted: $name"
            return
        }
        lappend argv $name $value
    }
    set rc [catch {uplevel #0 $argv} result]
    if {$rc == 0} {
        ::atpg_reply $chan OK $result
    } else {
        ::atpg_reply $chan ERR $result
    }
}

proc ::atpg_accept {chan addr port} {
    if {![string equal $addr "127.0.0.1"]} {
        catch {close $chan}
        return
    }
    fconfigure $chan -buffering line -blocking 0 -translation lf
    fileevent $chan readable [list ::atpg_serve $chan]
}

if {[catch {
    set ::atpg_sock [socket -server ::atpg_accept -myaddr 127.0.0.1 0]
    set ::atpg_port [lindex [fconfigure $::atpg_sock -sockname] 2]
    set ::atpg_fh [open @@PORTFILE@@ w]
    puts $::atpg_fh $::atpg_port
    close $::atpg_fh
    puts "ATPG control channel listening on 127.0.0.1:$::atpg_port"
} ::atpg_err]} {
    puts "ATPG control channel unavailable: $::atpg_err"
}
# --- end control channel ----------------------------------------------------
'''
    return (template
            .replace("@@TOKEN@@", _tcl_literal(token))
            .replace("@@CMDS@@", _tcl_literal(" ".join(allowed_commands)))
            .replace("@@OPTS@@", _tcl_literal(" ".join(allowed_options)))
            .replace("@@PORTFILE@@", _tcl_literal(port_file)))


@dataclass
class InspectAction:
    """One structured request: a verb, the object, and option pairs."""

    verb: str
    options: Dict[str, str] = field(default_factory=dict)
    label: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "InspectAction":
        verb = str(data.get("verb", "")).strip()
        if not _VERB_RE.match(verb):
            raise LiveSessionError(
                f"inspect action has no usable 'verb': {data!r}")
        options = {str(k): str(v) for k, v in (data.get("options") or {}).items()}
        return cls(verb=verb, options=options,
                   label=str(data.get("label", "")) or verb)

    def as_dict(self) -> Dict[str, object]:
        return {"verb": self.verb, "options": dict(self.options),
                "label": self.label}

    def rendered(self, obj: str) -> str:
        """The equivalent command, for display and for the copy fallback."""
        parts = [self.verb, "{" + obj + "}"]
        for name, value in self.options.items():
            parts.extend([name, value])
        return " ".join(parts)


def validate_object(obj: str) -> str:
    """Check a design object name before it goes on the wire."""
    obj = (obj or "").strip()
    if not obj:
        raise LiveSessionError("no object given")
    if any(c in obj for c in "\t\r\n\0"):
        raise LiveSessionError("object name contains a control character")
    if any(c in obj for c in "{}\\"):
        raise LiveSessionError(
            f"object name cannot be quoted safely: {obj}")
    return obj


def validate_option(name: str, value: str) -> Tuple[str, str]:
    """Check one option pair before it goes on the wire."""
    if not _OPTION_NAME_RE.match(name):
        raise LiveSessionError(f"not a valid option name: {name!r}")
    if not _OPTION_VALUE_RE.match(value):
        raise LiveSessionError(
            f"option {name} has a value that is not permitted: {value!r}")
    return name, value


def read_port(port_file: str) -> Optional[int]:
    """The port the listener wrote, or None if it has not written one yet."""
    try:
        with open(port_file, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    try:
        port = int(text)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


class LiveSession:
    """Client for the control channel of one running tool session."""

    def __init__(self, port_file: str, token: str,
                 host: str = "127.0.0.1",
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.port_file = port_file
        self.token = token
        self.host = host
        self.timeout = timeout

    @property
    def port(self) -> Optional[int]:
        return read_port(self.port_file)

    def is_listening(self) -> bool:
        """True when the port is open.  Says nothing about the tool being idle."""
        port = self.port
        if port is None:
            return False
        try:
            with socket.create_connection((self.host, port), timeout=1.0):
                return True
        except OSError:
            return False

    def send(self, action: InspectAction, obj: str) -> str:
        """Run *action* on *obj* in the live session; return the tool's reply."""
        port = self.port
        if port is None:
            raise LiveSessionError(
                "no control channel for this session. It may still be "
                "starting, or it was launched without one.")
        obj = validate_object(obj)
        fields = [self.token, action.verb, obj]
        for name, value in action.options.items():
            name, value = validate_option(name, value)
            fields.append(f"{name}={value}")
        payload = (SEP.join(fields) + "\n").encode("utf-8")

        try:
            with socket.create_connection((self.host, port),
                                          timeout=self.timeout) as conn:
                conn.settimeout(self.timeout)
                conn.sendall(payload)
                reply = self._read_line(conn)
        except socket.timeout as exc:
            raise LiveSessionError(
                "the session did not answer in time. It is single threaded, "
                "so it is most likely busy loading the design -- try again "
                "once the viewer has opened.") from exc
        except OSError as exc:
            raise LiveSessionError(
                f"the session could not be reached on port {port}: {exc}") from exc

        status, _, text = reply.partition(" ")
        if status == "OK":
            return text.strip()
        raise LiveSessionError(text.strip() or "the tool refused the request")

    @staticmethod
    def _read_line(conn: socket.socket) -> str:
        chunks: List[bytes] = []
        while True:
            data = conn.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
        return b"".join(chunks).decode("utf-8", "replace").strip()
