"""Directive-level parser for Tessent ATPG constraint dofiles.

A dofile is Tcl. Matching it line-by-line against a handful of keywords is
what produced the defect this module replaces: on one real partition every
constraint line failed to classify, the report showed **zero** contributing
constraints, and a parser limitation was presented as a fact about the design.

The parser therefore works on *commands*, not lines:

* logical commands are assembled across backslash continuations and braces
* ``#`` and ``//`` comments are stripped outside quotes
* ``set NAME VALUE`` is recorded and ``$NAME`` / ``${NAME}`` substituted
* ``dofile`` / ``source`` includes are followed relative to the including file
* ``if`` / ``else`` bodies are parsed, and every directive inside one is
  flagged ``conditional`` because the condition was not evaluated
* ``[get_pins ...]`` / ``[get_cells ...]`` / ``[get_ports ...]`` collections
  are expanded against the netlist when one is supplied, honouring ``-hier``
  and glob wildcards

Recognised directives come from :data:`..config.analysis_config
.DEFAULT_CONSTRAINT_DIRECTIVES` and are extendable per partition, so a
site-specific wrapper command is a configuration entry rather than a patch.

Nothing is ever dropped. A command the grammar cannot evaluate becomes a
record with ``resolved=False`` carrying its source line, and an unrecognised
directive name is warned about per distinct name and counted against a
configurable threshold, exactly like an unrecognised fault class.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..config.analysis_config import AnalysisConfig, resolve
from ..diagnostics import UnrecognisedReport, UnrecognisedTracker
from ..models import ConstraintRecord
from .fault_parser import normalize_object

logger = logging.getLogger(__name__)

#: Tcl control-flow and structural words that are not constraint directives.
_CONTROL_WORDS = frozenset({
    "set", "unset", "dofile", "source", "if", "elseif", "else", "while",
    "for", "foreach", "proc", "return", "puts", "catch", "expr", "incr",
    "namespace", "package", "global", "variable", "break", "continue",
})

#: Collection commands whose result is a set of design objects.
_COLLECTION_COMMANDS = frozenset({
    "get_pins", "get_cells", "get_ports", "get_nets", "get_instances",
})

#: Recursion guard for ``dofile`` includes.
MAX_INCLUDE_DEPTH = 8

#: Upper bound on objects one collection expression may expand to. A
#: ``-hier *`` pattern legitimately matches an entire partition, and turning
#: that into a million records would help nobody.
MAX_COLLECTION_EXPANSION = 5000

_VAR_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")
_SIGNAL_TOKEN = re.compile(r"[A-Za-z0-9_$\/\.\[\]*?%-]+")
_NUMERIC = re.compile(r"[-+]?\d+(?:\.\d+)?")
_ASSIGNMENT = re.compile(
    r"^(?P<sig>[\w$\/\.\[\]]+)\s*=\s*(?P<val>[01xX])\s*$")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class ConstraintParseResult:
    """Everything one constraint-file parse produced.

    Attributes:
        records: Every directive, resolved or not, in source order.
        warnings: Human-readable warnings, one per distinct unknown directive.
        unrecognised: Per-directive accounting of commands the tool does not
            know.
        variables: ``set`` variables captured while parsing.
        files: Every file read, including followed ``dofile`` includes.
        expanded_count: Objects produced by collection expansion.
    """

    records: List[ConstraintRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    unrecognised: Optional[UnrecognisedReport] = None
    variables: Dict[str, str] = field(default_factory=dict)
    files: List[str] = field(default_factory=list)
    expanded_count: int = 0

    @property
    def unresolved(self) -> List[ConstraintRecord]:
        """Directives that were recognised but could not be evaluated."""
        return [r for r in self.records if not r.resolved]

    @property
    def unresolved_count(self) -> int:
        """How many directives could not be evaluated."""
        return len(self.unresolved)

    def summary(self) -> Dict[str, Any]:
        """Counts a report needs to state what the constraint file gave us."""
        return {
            "files": list(self.files),
            "directives": len(self.records),
            "resolved": len(self.records) - self.unresolved_count,
            "unresolved": self.unresolved_count,
            "expanded_objects": self.expanded_count,
            "unrecognised": (self.unrecognised.as_dict()
                             if self.unrecognised else None),
        }

    def as_tuple(self) -> Tuple[List[ConstraintRecord], List[str]]:
        """Backwards-compatible ``(records, warnings)`` view."""
        return self.records, self.warnings


# ---------------------------------------------------------------------------
# Lexing
# ---------------------------------------------------------------------------
@dataclass
class _Command:
    """One logical Tcl command with its source position."""

    text: str
    line_number: int
    source_file: str


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` / ``//`` comment that is outside quotes."""
    out: List[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            break
        if ch == "/" and line.startswith("//", i):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _iter_commands(text: str, source_file: str) -> List[_Command]:
    """Split *text* into logical commands.

    Joins backslash continuations, keeps a command open while braces or
    brackets are unbalanced, and splits on ``;`` at depth zero. A dofile
    routinely wraps a long ``[get_pins ...]`` list across several lines, and a
    line-oriented reader sees each fragment as an unparsable command.
    """
    commands: List[_Command] = []
    buffer: List[str] = []
    start_line = 0
    depth = 0
    quote: Optional[str] = None

    def flush() -> None:
        nonlocal buffer, start_line
        joined = " ".join(part.strip() for part in buffer if part.strip())
        if joined.strip():
            commands.append(_Command(joined.strip(), start_line, source_file))
        buffer = []
        start_line = 0

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw
        if quote is None and depth == 0:
            line = _strip_comment(line)
        continued = line.rstrip().endswith("\\")
        if continued:
            line = line.rstrip()[:-1]
        if not buffer:
            start_line = lineno
        buffer.append(line)

        for ch in line:
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth = max(0, depth - 1)

        if continued or depth > 0 or quote:
            continue
        flush()

    flush()
    return commands


def _tokenize(command: str) -> List[str]:
    """Split a command into words, keeping ``[...]`` and ``{...}`` groups whole."""
    tokens: List[str] = []
    current: List[str] = []
    depth = 0
    quote: Optional[str] = None
    for ch in command:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
            continue
        if ch in "[{":
            depth += 1
            current.append(ch)
            continue
        if ch in "]}":
            depth = max(0, depth - 1)
            current.append(ch)
            continue
        if ch.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


# ---------------------------------------------------------------------------
# Netlist-backed collection expansion
# ---------------------------------------------------------------------------
class _ObjectIndex:
    """Design object names a collection expression can resolve against.

    Built from the parsed netlist when one is available. With no netlist the
    index is empty and every collection stays unresolved — which is reported,
    not quietly treated as "matches nothing".
    """

    def __init__(self, netlist: Any = None) -> None:
        self.available = netlist is not None
        self.instances: List[str] = []
        self.nets: List[str] = []
        self.ports: List[str] = []
        if netlist is None:
            return
        try:
            for module_name, inst in netlist.all_instances():
                self.instances.append(inst.name)
                for pin in inst.pins:
                    if pin.net:
                        self.nets.append(pin.net)
            for module in netlist.modules.values():
                for port in module.ports:
                    self.ports.append(port.name)
        except Exception as exc:  # noqa: BLE001 - never fail a parse on this
            logger.warning("Could not index the netlist for constraint "
                           "expansion: %s", exc)
            self.available = False
        self.instances = sorted(set(self.instances))
        self.nets = sorted(set(self.nets))
        self.ports = sorted(set(self.ports))

    def match(self, command: str, patterns: Sequence[str],
              limit: int) -> List[str]:
        """Objects matching *patterns* for collection *command*.

        A pattern may be written hierarchically (``top/core/*tdr*``) while the
        index holds the names as the netlist declares them, so the leaf
        component is tried as well as the whole path. This is a naming
        convention, not a design assumption, and works at any hierarchy depth.
        """
        if command in ("get_cells", "get_instances"):
            pool = self.instances
        elif command == "get_ports":
            pool = self.ports
        else:
            # A pin pattern is routinely written as an instance path
            # (``blk/u_hold*``) rather than as a net name, so both pools are
            # searched. Restricting to nets alone silently matched nothing.
            pool = self.nets + self.instances
        out: List[str] = []
        for pattern in patterns:
            forms = [pattern]
            leaf = pattern.replace(".", "/").rstrip("/").split("/")[-1]
            if leaf and leaf != pattern:
                forms.append(leaf)
            for name in pool:
                if any(fnmatch.fnmatch(name, form) or name == form
                       for form in forms):
                    out.append(name)
                    if len(out) >= limit:
                        return out
        # Preserve order while de-duplicating.
        seen: Set[str] = set()
        unique = []
        for name in out:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class _DofileParser:
    """Parses one constraint dofile, following includes."""

    def __init__(self, config: AnalysisConfig, netlist: Any = None) -> None:
        self.config = config
        self.index = _ObjectIndex(netlist)
        self.result = ConstraintParseResult()
        self.tracker = UnrecognisedTracker(
            domain="constraint directive",
            threshold_pct=config.unresolved_constraint_threshold_pct,
            sample_limit=config.sample_limit,
            fatal=config.unresolved_constraint_fatal,
        )
        self._vars: Dict[str, str] = {}

    # -- entry points -----------------------------------------------------
    def parse_text(self, text: str, source_file: str = "",
                   depth: int = 0, conditional: bool = False,
                   line_override: Optional[int] = None) -> None:
        for command in _iter_commands(text, source_file):
            if line_override is not None:
                # A conditional body is re-parsed as its own snippet, so its
                # internal line numbers restart at 1. Report the line of the
                # enclosing block instead: a line number that does not exist
                # in the file is worse than none.
                command = _Command(command.text, line_override, source_file)
            try:
                self._handle(command, depth, conditional)
            except Exception as exc:  # noqa: BLE001 - one bad command only
                self.result.warnings.append(
                    f"{self._where(command)}: failed to parse "
                    f"({exc}); recorded as unresolved.")
                self._emit(command, kind="unknown", resolved=False,
                           notes=f"parser error: {exc}",
                           conditional=conditional)

    def finish(self) -> ConstraintParseResult:
        self.result.unrecognised = self.tracker.report()
        self.result.warnings.extend(self.result.unrecognised.warnings())
        self.result.variables = dict(self._vars)
        unresolved = self.result.unresolved_count
        if unresolved:
            self.result.warnings.append(
                f"WARNING: {unresolved} of {len(self.result.records)} "
                f"constraint directive(s) were recognised but could not be "
                f"evaluated. Faults are NOT proven unconstrained: the "
                f"constraint file was only partly understood."
            )
        if self.tracker.fatal and self.result.unrecognised.exceeded:
            self.tracker.enforce()
        return self.result

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _where(command: _Command) -> str:
        location = f"line {command.line_number}"
        if command.source_file:
            location = f"{os.path.basename(command.source_file)}:{location}"
        return location

    def _emit(self, command: _Command, *, kind: str, signal: Optional[str] = None,
              value: Optional[str] = None, notes: str = "",
              directive: str = "", options: Optional[Dict[str, str]] = None,
              targets: Optional[List[str]] = None, resolved: bool = True,
              conditional: bool = False) -> ConstraintRecord:
        record = ConstraintRecord(
            raw_text=command.text,
            line_number=command.line_number,
            kind=kind,
            signal=signal,
            normalized_signal=normalize_object(signal) if signal else None,
            value=value,
            notes=notes,
            directive=directive,
            options=dict(options or {}),
            targets=list(targets or ([signal] if signal else [])),
            resolved=resolved,
            source_file=command.source_file,
            conditional=conditional,
        )
        self.result.records.append(record)
        return record

    def _substitute(self, token: str) -> Tuple[str, bool]:
        """Expand ``$var`` references. Returns ``(text, fully_resolved)``."""
        missing = False

        def repl(match: "re.Match") -> str:
            nonlocal missing
            name = match.group(1) or match.group(2)
            if name in self._vars:
                return self._vars[name]
            missing = True
            return match.group(0)

        return _VAR_REF.sub(repl, token), not missing

    # -- command dispatch -------------------------------------------------
    def _handle(self, command: _Command, depth: int, conditional: bool) -> None:
        text = command.text.strip()
        if not text or text in ("{", "}"):
            return

        assign = _ASSIGNMENT.match(text)
        if assign:
            self.tracker.seen()
            self._emit(command, kind="constant", signal=assign.group("sig"),
                       value=assign.group("val").upper(),
                       directive="=", notes="parsed from assignment syntax",
                       conditional=conditional)
            return

        tokens = _tokenize(text)
        if not tokens:
            return
        word = tokens[0].lower().lstrip("\\")

        if word in _CONTROL_WORDS:
            self._handle_control(word, tokens, command, depth, conditional)
            return

        self.tracker.seen()
        kind = self.config.constraint_directives.get(word)
        if kind is None:
            self.tracker.add(tokens[0], sample=text,
                             line_number=command.line_number)
            self._emit(command, kind="unknown", directive=tokens[0],
                       resolved=False,
                       notes="directive not in the configured vocabulary; "
                             "add it to 'constraint_directives' to classify it",
                       conditional=conditional)
            return

        self._handle_directive(word, kind, tokens, command, conditional)

    def _handle_control(self, word: str, tokens: List[str], command: _Command,
                        depth: int, conditional: bool) -> None:
        if word == "set" and len(tokens) >= 3:
            value, _ok = self._substitute(tokens[2].strip("{}\"'"))
            self._vars[tokens[1].lstrip("$")] = value
            return
        if word in ("dofile", "source") and len(tokens) >= 2:
            self._handle_include(tokens[1], command, depth, conditional)
            return
        if word in ("if", "elseif", "while", "for", "foreach"):
            self._handle_block(word, tokens, command, depth)
            return
        # Remaining control words carry no constraint information.
        return

    def _handle_include(self, target: str, command: _Command, depth: int,
                        conditional: bool) -> None:
        path, ok = self._substitute(target.strip("{}\"'"))
        if not ok:
            self.tracker.seen()
            self._emit(command, kind="include", directive="dofile",
                       resolved=False, signal=path, conditional=conditional,
                       notes="include path depends on an unset variable")
            return
        if depth >= MAX_INCLUDE_DEPTH:
            self.tracker.seen()
            self._emit(command, kind="include", directive="dofile",
                       resolved=False, signal=path, conditional=conditional,
                       notes=f"include nesting exceeded {MAX_INCLUDE_DEPTH} "
                             f"levels; not followed")
            return
        base = os.path.dirname(command.source_file) if command.source_file else ""
        candidate = path if os.path.isabs(path) else os.path.join(base, path)
        if not os.path.isfile(candidate):
            self.tracker.seen()
            self._emit(command, kind="include", directive="dofile",
                       resolved=False, signal=path, conditional=conditional,
                       notes=f"included file not found: {candidate}")
            return
        if candidate in self.result.files:
            return  # cycle guard
        self.result.files.append(candidate)
        with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
            self.parse_text(fh.read(), candidate, depth + 1, conditional)

    def _handle_block(self, word: str, tokens: List[str], command: _Command,
                      depth: int) -> None:
        """Record an unevaluated control block and parse its bodies.

        The condition is not evaluated — doing so would mean implementing Tcl
        — so the block itself is recorded as unresolved and every directive
        inside it is flagged ``conditional``. Both halves matter: the reader
        learns that a branch exists, and the directives inside are still
        visible instead of being dropped.
        """
        self.tracker.seen()
        condition = tokens[1] if len(tokens) > 1 else ""
        self._emit(command, kind="conditional", directive=word,
                   resolved=False, signal=None,
                   notes=f"'{word}' condition {condition} was not evaluated; "
                         f"directives in its branches are marked conditional")
        for token in tokens[1:]:
            body = token.strip()
            if body.startswith("{") and body.endswith("}") and len(body) > 2:
                inner = body[1:-1]
                if any(w in inner for w in ("add_", "set_", "delete_")):
                    self.parse_text(inner, command.source_file, depth + 1,
                                    conditional=True,
                                    line_override=command.line_number)

    # -- constraint directives -------------------------------------------
    def _handle_directive(self, word: str, kind: str, tokens: List[str],
                          command: _Command, conditional: bool) -> None:
        options: Dict[str, str] = {}
        values: List[str] = []
        targets: List[str] = []
        collections: List[str] = []
        unresolved_reason = ""
        last_flag = ""

        i = 1
        while i < len(tokens):
            token = tokens[i]
            expanded, ok = self._substitute(token)
            if not ok:
                unresolved_reason = (
                    f"unset variable in argument '{token}'")
            token = expanded

            if token.startswith("-"):
                last_flag = token.lower()
                options.setdefault(last_flag, "")
                i += 1
                continue

            if token.startswith("[") or token.startswith("{["):
                collections.append(token)
                last_flag = ""
                i += 1
                continue

            bare = token.strip("{}\"'")
            low = bare.lower()
            if low in self.config.constraint_value_codes:
                values.append(self.config.constraint_value_codes[low])
            elif last_flag and _NUMERIC.fullmatch(bare):
                # A bare number straight after a flag is that flag's argument
                # (``-abort_limit 500``), not a design object. Without this a
                # limit setting is indexed as a constrained signal named
                # "500" and matches nothing for the rest of the run.
                options[last_flag] = bare
            elif bare:
                targets.append(bare)
            last_flag = ""
            i += 1

        for expression in collections:
            resolved_names, reason = self._expand_collection(expression)
            if reason:
                unresolved_reason = unresolved_reason or reason
            targets.extend(resolved_names)

        value = values[0] if values else None

        if not targets:
            self.tracker.seen()
            self._emit(command, kind=kind, directive=tokens[0],
                       options=options, value=value,
                       resolved=not unresolved_reason,
                       conditional=conditional,
                       notes=unresolved_reason or
                             "directive names no design object")
            return

        for target in targets:
            self.tracker.seen()
            self._emit(command, kind=kind, signal=target, value=value,
                       directive=tokens[0], options=options,
                       targets=[target],
                       resolved=not unresolved_reason,
                       conditional=conditional,
                       notes=unresolved_reason)

    def _expand_collection(self, expression: str) -> Tuple[List[str], str]:
        """Expand ``[get_pins -hier a* b*]`` into concrete object names.

        Returns ``(names, unresolved_reason)``. When no netlist was supplied,
        or the command is not a collection command, the patterns themselves
        are returned together with a reason, so the directive is recorded as
        unresolved rather than silently matching nothing.
        """
        inner = expression.strip()
        while inner.startswith("{") and inner.endswith("}"):
            inner = inner[1:-1].strip()
        inner = inner.strip("[]").strip()
        parts = _tokenize(inner)
        if not parts:
            return [], "empty collection expression"
        cmd = parts[0].lower()
        patterns = [p.strip("{}\"'") for p in parts[1:]
                    if not p.startswith("-")]
        if cmd not in _COLLECTION_COMMANDS:
            return patterns, (f"'{cmd}' is not a known collection command; "
                              f"its result could not be resolved")
        if not self.index.available:
            return patterns, ("collection could not be resolved: no netlist "
                              "was available to expand it against")
        remaining = max(0, MAX_COLLECTION_EXPANSION - self.result.expanded_count)
        if remaining == 0:
            return patterns, (f"collection expansion capped at "
                              f"{MAX_COLLECTION_EXPANSION} objects")
        names = self.index.match(cmd, patterns, remaining)
        self.result.expanded_count += len(names)
        if not names:
            return patterns, (f"'{cmd}' matched no object in the netlist; the "
                              f"pattern is recorded verbatim")
        return names, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_constraints_ex(text: str, config: Optional[AnalysisConfig] = None,
                         netlist: Any = None,
                         source_file: str = "") -> ConstraintParseResult:
    """Parse constraint *text* with the full dofile grammar.

    Args:
        text: Dofile contents.
        config: Analysis configuration; the active one is used when omitted.
        netlist: Parsed netlist used to expand ``[get_*]`` collections. Without
            it collections stay unresolved and are reported as such.
        source_file: Path the text came from, used to resolve ``dofile``
            includes relative to it.

    Returns:
        A :class:`ConstraintParseResult`.
    """
    parser = _DofileParser(resolve(config), netlist)
    if source_file:
        parser.result.files.append(source_file)
    parser.parse_text(text, source_file)
    result = parser.finish()
    logger.info(
        "Parsed %d constraint directive(s) from %d file(s); %d unresolved, "
        "%d unrecognised directive name(s), %d object(s) expanded.",
        len(result.records), max(1, len(result.files)),
        result.unresolved_count,
        len(result.unrecognised.tokens) if result.unrecognised else 0,
        result.expanded_count,
    )
    return result


def parse_constraints(text: str, config: Optional[AnalysisConfig] = None,
                      netlist: Any = None
                      ) -> Tuple[List[ConstraintRecord], List[str]]:
    """Parse constraint *text*.

    Returns:
        ``(records, warnings)``. Use :func:`parse_constraints_ex` when the
        resolved/unresolved accounting is needed.
    """
    return parse_constraints_ex(text, config=config, netlist=netlist).as_tuple()


def parse_constraints_file_ex(path: str,
                              config: Optional[AnalysisConfig] = None,
                              netlist: Any = None) -> ConstraintParseResult:
    """Read *path* and parse it as a constraint dofile, following includes."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_constraints_ex(handle.read(), config=config,
                                    netlist=netlist, source_file=path)


def parse_constraints_file(path: str, config: Optional[AnalysisConfig] = None,
                           netlist: Any = None
                           ) -> Tuple[List[ConstraintRecord], List[str]]:
    """Read *path* and parse it as constraints."""
    return parse_constraints_file_ex(path, config=config,
                                     netlist=netlist).as_tuple()
