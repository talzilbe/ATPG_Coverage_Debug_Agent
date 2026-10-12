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
  any peer that is not loopback.  The one exception is heavy mode, where the
  tool runs on a Netbatch host: it then binds every interface but still hangs
  up on any peer that is not one of the launching GUI machine's addresses;
* every request carries a per-session token, generated here and known only
  through the 0700 session directory;
* **no command string is ever transmitted.** The request is structured --
  a verb, one object, and option pairs -- and the Tcl side rebuilds the command
  with ``list``, which quotes each word so the re-parse cannot substitute
  anything. A bracket or ``$`` in an object name is data, never code;
* the verb must appear in the profile's allow-list, and option names and values
  are checked against patterns on both sides.

Agent scripts
-------------
A profile may additionally set ``control.allow_agent_eval``. That enables one
more request, :data:`EVAL_VERB`, which runs a free-form script. It is meant
only for the GUI's approval flow: the token never leaves the GUI, so a script
reaches the tool only after a person approved it there. The script and the
reply travel base64-encoded, and the tool brackets the script's transcript
with ``ATPG_BEGIN <id>`` / ``ATPG_END <id>`` markers so the output that was
printed rather than returned can be cut out of the log file.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import socket
import time
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

#: The request that runs an approved agent script. Not a valid Tcl command
#: name for the allow-list, so it can never be confused with one.
EVAL_VERB = "@atpg_eval"

#: Request ids travel on the wire and appear in the transcript markers.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Largest script accepted from the agent.
MAX_SCRIPT_CHARS = 20000

#: Seconds an approved script may run. Reports on a large flat model are slow.
DEFAULT_SCRIPT_TIMEOUT = 300.0

#: Seconds to wait for the end marker to reach the log after the reply.
TRANSCRIPT_WAIT = 3.0

#: Largest transcript the listener sends back inline, in characters.
MAX_REMOTE_TRANSCRIPT = 2_000_000


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


#: A peer address the remote listener may accept (IPv4 or IPv6 text form).
_PEER_RE = re.compile(r"^[0-9A-Fa-f.:]+$")

#: A host name as written into the port file by a remote listener.
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

LOOPBACK = "127.0.0.1"


def local_addresses() -> List[str]:
    """Non-loopback addresses of this machine, as a remote peer would see them."""
    found: List[str] = []
    names = {socket.gethostname(), socket.getfqdn()}
    for name in names:
        try:
            infos = socket.getaddrinfo(name, None)
        except OSError:
            continue
        for info in infos:
            addr = str(info[4][0])
            if addr.startswith("127.") or addr == "::1" or addr in found:
                continue
            found.append(addr)
    return found


def _peer_forms(peers: Sequence[str]) -> List[str]:
    forms: List[str] = []
    for peer in peers:
        peer = peer.strip()
        if not _PEER_RE.match(peer):
            raise LiveSessionError(f"not a valid peer address: {peer!r}")
        for form in (peer, "::ffff:" + peer if ":" not in peer else ""):
            if form and form not in forms:
                forms.append(form)
    return forms


def build_listener_tcl(port_file: str, token: str,
                       allowed_commands: Sequence[str],
                       allowed_options: Sequence[str],
                       allow_eval: bool = False,
                       allowed_peers: Sequence[str] = (),
                       log_file: str = "") -> str:
    """Return the Tcl that serves the control channel inside the tool.

    Placed at the top of the dofile so the channel exists before the design
    starts loading; a request that arrives during the load simply does not get
    serviced until the tool is back at its prompt.

    *allow_eval* enables :data:`EVAL_VERB` (approved agent scripts).

    *allowed_peers* switches to remote mode, for a tool running on another
    host: the listener binds every interface, accepts only these addresses,
    and writes ``<hostname> <port>`` to the port file.

    *log_file* is the tool's own log. The listener cuts an agent script's
    transcript out of it and returns it in the reply, because the GUI host
    may not see a remote writer's log for many seconds (NFS caching).
    """
    for verb in allowed_commands:
        if not _VERB_RE.match(verb):
            raise LiveSessionError(f"not a valid command name: {verb!r}")
    for name in allowed_options:
        if not _OPTION_NAME_RE.match(name):
            raise LiveSessionError(f"not a valid option name: {name!r}")
    remote = bool(allowed_peers)
    peers = _peer_forms(allowed_peers) if remote else [LOOPBACK]
    if remote:
        accept_check = "if {[lsearch -exact $::atpg_peers $addr] < 0} {"
        bind = "socket -server ::atpg_accept 0"
        port_line = 'puts $::atpg_fh "[info hostname] $::atpg_port"'
        where = "[info hostname]:$::atpg_port (accepting only: $::atpg_peers)"
    else:
        accept_check = 'if {![string equal $addr "127.0.0.1"]} {'
        bind = "socket -server ::atpg_accept -myaddr 127.0.0.1 0"
        port_line = "puts $::atpg_fh $::atpg_port"
        where = "127.0.0.1:$::atpg_port"

    template = r'''
# --- control channel (ATPG Coverage Debug Agent) ----------------------------
# Loopback only, token checked, and the command is rebuilt with [list] so that
# nothing in an object name can be substituted as code.
set ::atpg_token @@TOKEN@@
set ::atpg_allowed_cmds @@CMDS@@
set ::atpg_allowed_opts @@OPTS@@
set ::atpg_allow_eval @@EVAL@@
set ::atpg_peers @@PEERS@@
set ::atpg_log @@LOGFILE@@

proc ::atpg_reply {chan status text} {
    catch {puts $chan "$status $text" ; flush $chan}
}

# The text logged between a script's markers, read on the tool's own host.
# Returns {complete text}; complete is 0 when the end marker never appeared.
proc ::atpg_slice {id start} {
    if {$::atpg_log eq ""} { return [list 0 ""] }
    set begin "ATPG_BEGIN $id"
    set end "ATPG_END $id"
    for {set i 0} {$i < 30} {incr i} {
        if {![catch {open $::atpg_log r} fh]} {
            catch {seek $fh $start}
            set data [read $fh]
            catch {close $fh}
            set b [string first $begin $data]
            set e [string first $end $data]
            if {$b >= 0 && $e > $b} {
                set from [expr {[string first "\n" $data $b] + 1}]
                set to [expr {[string last "\n" $data $e] - 1}]
                set body ""
                if {$to >= $from} { set body [string range $data $from $to] }
                if {[string length $body] > @@MAXTRANSCRIPT@@} {
                    set body [string range $body 0 @@MAXTRANSCRIPT@@]
                }
                return [list 1 $body]
            }
        }
        after 100
    }
    return [list 0 ""]
}

proc ::atpg_b64 {text} {
    set enc [binary encode base64 [encoding convertto utf-8 $text]]
    if {$enc eq ""} { return "-" }
    return $enc
}

# An approved agent script: decoded, run at global level, transcript bracketed.
proc ::atpg_eval {chan id payload} {
    if {!$::atpg_allow_eval} {
        ::atpg_reply $chan ERR "agent scripts are disabled in this profile"
        return
    }
    if {![regexp {^[A-Za-z0-9_-]+$} $id]} {
        ::atpg_reply $chan ERR "malformed request id"
        return
    }
    if {[catch {encoding convertfrom utf-8 [binary decode base64 $payload]} script]} {
        ::atpg_reply $chan ERR "malformed script"
        return
    }
    set start 0
    catch {set start [file size $::atpg_log]}
    puts "ATPG_BEGIN $id"
    catch {flush stdout}
    set rc [catch {uplevel #0 $script} result]
    puts "ATPG_END $id rc=$rc"
    catch {flush stdout}
    set enc [::atpg_b64 $result]
    lassign [::atpg_slice $id $start] complete transcript
    set tail "T$complete [::atpg_b64 $transcript]"
    if {$rc == 0 || $rc == 2} {
        ::atpg_reply $chan OK64 "$enc $tail"
    } else {
        ::atpg_reply $chan ERR64 "$enc $tail"
    }
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
    if {[string equal $verb "@@EVALVERB@@"]} {
        ::atpg_eval $chan [lindex $parts 2] [lindex $parts 3]
        return
    }
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
    @@ACCEPTCHECK@@
        catch {close $chan}
        return
    }
    fconfigure $chan -buffering line -blocking 0 -translation lf
    fileevent $chan readable [list ::atpg_serve $chan]
}

if {[catch {
    set ::atpg_sock [@@BIND@@]
    set ::atpg_port [lindex [fconfigure $::atpg_sock -sockname] 2]
    set ::atpg_fh [open @@PORTFILE@@ w]
    @@PORTLINE@@
    close $::atpg_fh
    puts "ATPG control channel listening on @@WHERE@@"
} ::atpg_err]} {
    puts "ATPG control channel unavailable: $::atpg_err"
}
# --- end control channel ----------------------------------------------------
'''
    return (template
            .replace("@@TOKEN@@", _tcl_literal(token))
            .replace("@@CMDS@@", _tcl_literal(" ".join(allowed_commands)))
            .replace("@@OPTS@@", _tcl_literal(" ".join(allowed_options)))
            .replace("@@EVAL@@", "1" if allow_eval else "0")
            .replace("@@EVALVERB@@", EVAL_VERB)
            .replace("@@PEERS@@", _tcl_literal(" ".join(peers)))
            .replace("@@ACCEPTCHECK@@", accept_check)
            .replace("@@BIND@@", bind)
            .replace("@@PORTLINE@@", port_line)
            .replace("@@WHERE@@", where)
            .replace("@@MAXTRANSCRIPT@@", str(MAX_REMOTE_TRANSCRIPT))
            .replace("@@LOGFILE@@", _tcl_literal(log_file))
            .replace("@@PORTFILE@@", _tcl_literal(port_file)))


@dataclass
class ScriptResult:
    """What one approved script produced in the tool."""

    ok: bool
    result: str
    transcript: str = ""
    transcript_complete: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {"ok": self.ok, "result": self.result,
                "transcript": self.transcript,
                "transcript_complete": self.transcript_complete}


def validate_script(script: str) -> str:
    """Check an agent script before it goes on the wire."""
    script = (script or "").strip()
    if not script:
        raise LiveSessionError("no script given")
    if "\0" in script:
        raise LiveSessionError("script contains a NUL character")
    if len(script) > MAX_SCRIPT_CHARS:
        raise LiveSessionError(
            f"script is {len(script)} characters; the limit is "
            f"{MAX_SCRIPT_CHARS}")
    return script


def _b64_field(value: str) -> str:
    return "" if value in ("", "-") else base64.b64decode(value).decode(
        "utf-8", "replace")


def log_size(log_path: str) -> int:
    """Current size of the tool's log, so a later read starts after it."""
    try:
        return os.path.getsize(log_path)
    except OSError:
        return 0


def slice_transcript(log_path: str, request_id: str, offset: int = 0,
                     wait: float = TRANSCRIPT_WAIT) -> Tuple[str, bool]:
    """Text the tool logged between the markers of *request_id*.

    Returns ``(text, complete)``; *complete* is False when the end marker had
    not reached the log within *wait* seconds (what was there is returned).
    """
    begin = f"ATPG_BEGIN {request_id}"
    end = f"ATPG_END {request_id}"
    deadline = time.monotonic() + max(0.0, wait)
    text = ""
    while True:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                text = fh.read()
        except OSError:
            text = ""
        if end in text or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if begin in ln), None)
    if start is None:
        return "", False
    body: List[str] = []
    for ln in lines[start + 1:]:
        if end in ln:
            return "\n".join(body).strip("\n"), True
        body.append(ln)
    return "\n".join(body).strip("\n"), False


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


def read_endpoint(port_file: str) -> Optional[Tuple[str, int]]:
    """(host, port) the listener wrote, or None if it has not written one yet.

    A bare port means a loopback listener; ``host port`` a remote one.
    """
    try:
        with open(port_file, "r", encoding="utf-8") as handle:
            words = handle.read().split()
    except OSError:
        return None
    if len(words) == 1:
        host, text = LOOPBACK, words[0]
    elif len(words) == 2 and _HOST_RE.match(words[0]):
        host, text = words
    else:
        return None
    try:
        port = int(text)
    except ValueError:
        return None
    return (host, port) if 0 < port < 65536 else None


def read_port(port_file: str) -> Optional[int]:
    """The port the listener wrote, or None if it has not written one yet."""
    endpoint = read_endpoint(port_file)
    return endpoint[1] if endpoint else None


class LiveSession:
    """Client for the control channel of one running tool session."""

    def __init__(self, port_file: str, token: str,
                 host: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.port_file = port_file
        self.token = token
        self._host = host
        self.timeout = timeout

    @property
    def endpoint(self) -> Optional[Tuple[str, int]]:
        found = read_endpoint(self.port_file)
        if found is None:
            return None
        return (self._host or found[0], found[1])

    @property
    def host(self) -> str:
        found = self.endpoint
        return found[0] if found else (self._host or LOOPBACK)

    @property
    def port(self) -> Optional[int]:
        found = self.endpoint
        return found[1] if found else None

    def is_listening(self) -> bool:
        """True when the port is open.  Says nothing about the tool being idle."""
        endpoint = self.endpoint
        if endpoint is None:
            return False
        try:
            with socket.create_connection(endpoint, timeout=1.0):
                return True
        except OSError:
            return False

    def send(self, action: InspectAction, obj: str) -> str:
        """Run *action* on *obj* in the live session; return the tool's reply."""
        endpoint = self.endpoint
        if endpoint is None:
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
            with socket.create_connection(endpoint,
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
                f"the session could not be reached on port {endpoint[1]}: "
                f"{exc}") from exc

        status, _, text = reply.partition(" ")
        if status == "OK":
            return text.strip()
        raise LiveSessionError(text.strip() or "the tool refused the request")

    def run_script(self, script: str, request_id: str, log_path: str = "",
                   timeout: float = DEFAULT_SCRIPT_TIMEOUT) -> ScriptResult:
        """Run an approved script; return its result and logged transcript.

        A script that raised in the tool is a normal result (``ok`` False),
        not an exception; only a refused or unreachable channel raises.
        """
        endpoint = self.endpoint
        if endpoint is None:
            raise LiveSessionError(
                "no control channel for this session. It may still be "
                "starting, or it was launched without one.")
        script = validate_script(script)
        if not _REQUEST_ID_RE.match(request_id or ""):
            raise LiveSessionError(f"not a valid request id: {request_id!r}")
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        payload = (SEP.join([self.token, EVAL_VERB, request_id, encoded])
                   + "\n").encode("utf-8")
        offset = log_size(log_path) if log_path else 0

        try:
            with socket.create_connection(endpoint,
                                          timeout=self.timeout) as conn:
                conn.settimeout(timeout)
                conn.sendall(payload)
                reply = self._read_line(conn)
        except socket.timeout as exc:
            raise LiveSessionError(
                f"the session did not finish the script within {timeout:.0f} "
                "s. It may still be running; check the terminal.") from exc
        except OSError as exc:
            raise LiveSessionError(
                f"the session could not be reached on port {endpoint[1]}: "
                f"{exc}") from exc

        status, _, text = reply.partition(" ")
        if status not in ("OK64", "ERR64"):
            raise LiveSessionError(text.strip() or reply
                                   or "the tool refused the request")
        fields = text.split()
        try:
            result = _b64_field(fields[0] if fields else "")
            remote = None
            if len(fields) >= 3 and fields[1] in ("T0", "T1"):
                remote = (_b64_field(fields[2]), fields[1] == "T1")
        except ValueError as exc:
            raise LiveSessionError(f"unreadable reply from the tool: {exc}") from exc
        if remote is not None and remote[1]:
            transcript, complete = remote
        elif log_path:
            transcript, complete = slice_transcript(log_path, request_id, offset)
        else:
            transcript, complete = "", False
        return ScriptResult(ok=status == "OK64", result=result,
                            transcript=transcript,
                            transcript_complete=complete)

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
