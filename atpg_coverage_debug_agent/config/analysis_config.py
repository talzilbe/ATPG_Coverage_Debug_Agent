"""Design-independent analysis configuration.

Every convention the analyzer needs but cannot derive from its inputs lives
here: the fault-class role map, scan/clock pin vocabularies, tie-cell and
unconnected-net naming patterns, credit factors and input-quality thresholds.

The rule this module enforces is that **bringing up a new partition is a
configuration change, never a code change**. A different design, hierarchy
depth, cell library, Tessent version or fault model must be absorbed by
overriding values here.

Loading order, later winning:

1. The built-in defaults documented below.
2. A JSON file named by the ``ATPG_ANALYSIS_CONFIG`` environment variable.
3. A JSON file passed explicitly to :meth:`AnalysisConfig.load`.
4. Individual environment overrides for the tie patterns
   (``ATPG_TIE_HIGH_PATTERNS`` / ``ATPG_TIE_LOW_PATTERNS``), kept for
   backwards compatibility.

JSON keys mirror the dataclass field names. Unknown keys are reported rather
than ignored, because a silently-dropped override looks exactly like a
configuration that did not work.

Example ``atpg_analysis.json``::

    {
      "class_roles": {"NC": "UD"},
      "posdet_credit": 0.5,
      "scan_in_pins": ["si", "sd", "ti", "my_lib_scan_in"],
      "tie_high_patterns": ["mylib_tiehi"],
      "unconnected_net_patterns": ["*_DANGLING*"]
    }

``class_roles`` and the pin vocabularies are *merged* into the defaults so a
partition only has to name what is different; list-valued patterns are
*extended*. Set ``replace_defaults`` to ``true`` to start from an empty map
instead.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import logging
import os
import re
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Environment variable naming a JSON configuration file.
CONFIG_ENV_VAR = "ATPG_ANALYSIS_CONFIG"


class CoverageRole(str, Enum):
    """What a fault class contributes to the coverage metrics.

    The *role*, never the class label, drives every number the tool reports.
    That indirection is what lets an unseen class be onboarded from config.
    """

    #: Detected. Counts in full towards the numerator.
    DT = "DT"
    #: Possibly detected. Counts with a partial credit factor.
    PD = "PD"
    #: Undetectable. Removed from the test-coverage denominator.
    UD = "UD"
    #: ATPG untestable. Stays in the denominator; a legitimate debug target.
    AU = "AU"
    #: Not detected. Coverage loss.
    ND = "ND"
    #: The class was not in the map. Never used for arithmetic.
    UNKNOWN = "UNKNOWN"


#: Default Tessent stuck-at class -> coverage role.
#:
#: ``TI`` sits with ``UU``/``BL``/``RE`` in ``UD`` deliberately. Treating
#: ``TI`` as the *entire* undetectable population is the specific defect that
#: made a real partition's reported coverage wrong by a couple of points.
DEFAULT_CLASS_ROLES: Dict[str, str] = {
    "DS": CoverageRole.DT.value,   # detected by simulation
    "DI": CoverageRole.DT.value,   # detected by implication
    "PT": CoverageRole.PD.value,   # possibly detected, testable
    "PU": CoverageRole.PD.value,   # possibly detected, untestable
    "UU": CoverageRole.UD.value,   # undetectable unused
    "TI": CoverageRole.UD.value,   # undetectable tied
    "BL": CoverageRole.UD.value,   # undetectable blocked
    "RE": CoverageRole.UD.value,   # undetectable redundant
    "AU": CoverageRole.AU.value,   # ATPG untestable
    "UO": CoverageRole.ND.value,   # not detected, unobserved
    "UC": CoverageRole.ND.value,   # not detected, uncontrolled
}

#: Roles whose faults are debuggable coverage loss.
LOSS_ROLES = (CoverageRole.AU, CoverageRole.ND)

# ---------------------------------------------------------------------------
# Pin vocabularies
# ---------------------------------------------------------------------------
# A cell is scannable when its instantiation carries BOTH a dedicated
# scan-data input and a shift-enable pin. Matching is on the literal pin name
# read from the netlist, never on the instance or cell-type name.
DEFAULT_SCAN_IN_PINS = ["si", "sd", "ti", "sin", "scan_in", "scanin", "sdi",
                        "test_si", "tie"]
DEFAULT_SCAN_OUT_PINS = ["so", "to", "sout", "scan_out", "scanout", "sdo",
                         "test_so", "q_so"]
DEFAULT_SHIFT_ENABLE_PINS = ["se", "ssb", "sen", "scan_en", "scan_enable",
                             "shift_en", "test_se", "sh", "smc"]
#: A clock pin is what makes a cell sequential. Only sequential neighbours can
#: form a scan/non-scan boundary; every combinational cell is trivially
#: "non-scan", which is why naming one as a boundary is near-vacuous.
DEFAULT_CLOCK_PINS = ["clk", "ck", "clock", "cp", "gclk", "clkin", "clkb",
                      "ckn", "clk_n"]

#: Nets a scan-stitch left dangling. Glob patterns, case-insensitive.
DEFAULT_UNCONNECTED_NET_PATTERNS = ["SYNOPSYS_UNCONNECTED*", "*_UNCONNECTED*",
                                    "*UNCONNECTED*", "*_OPEN*", "*_DANGLING*"]

#: Tie-cell naming. Structure decides whether a cell is a constant source (no
#: input pins); naming is only needed to tell tie-high from tie-low, since the
#: two are structurally identical.
DEFAULT_TIE_HIGH_PATTERNS = ["tiehi", "tihi", "tieh", r"thi\b", "tie1",
                             "logic1", "const1"]
DEFAULT_TIE_LOW_PATTERNS = ["tielo", "tilo", "tiel", r"tlo\b", "tie0",
                            "logic0", "const0"]

# ---------------------------------------------------------------------------
# Constraint dofile vocabulary
# ---------------------------------------------------------------------------
#: Tessent dofile directive -> canonical constraint kind. Extendable from
#: config so a site-specific wrapper command is one JSON entry, not a patch.
DEFAULT_CONSTRAINT_DIRECTIVES: Dict[str, str] = {
    "add_cell_constraints": "constrain",
    "add_input_constraints": "constrain",
    "add_atpg_constraints": "constrain",
    "set_atpg_constraint": "constrain",
    "add_output_masks": "mask",
    "add_primary_inputs": "primary_input",
    "add_primary_outputs": "primary_output",
    "add_clocks": "clock",
    "add_write_controls": "clock",
    "add_read_controls": "clock",
    "set_atpg_limits": "limit",
    "add_nofaults": "nofault",
    "delete_nofaults": "nofault",
    "set_abort_limit": "limit",
    # Free-form dialects kept from the original keyword parser.
    "force": "force",
    "constant": "constant",
    "tie": "constant",
    "tied": "constant",
    "disable": "disable",
    "disabled": "disable",
    "block": "block",
    "blocked": "block",
    "constrain": "constrain",
    "constraint": "constrain",
    "constraints": "constrain",
    "clock": "clock",
    "reset": "reset",
    "test_en": "test_enable",
    "test_enable": "test_enable",
    "scan_en": "test_enable",
    "scan_enable": "test_enable",
}

#: Tessent constraint value codes -> logical value.
DEFAULT_CONSTRAINT_VALUE_CODES: Dict[str, str] = {
    "c0": "0", "c1": "1", "cx": "X",
    "t0": "0", "t1": "1", "tx": "X",
    "0": "0", "1": "1", "x": "X",
}

# ---------------------------------------------------------------------------
# Coverage metric rules
# ---------------------------------------------------------------------------
#: Possibly-detected families credited in ``atpg_effectiveness``, at full
#: weight. Tessent credits ``PU`` and not ``PT``: a possibly-detected-untestable
#: fault has been resolved as far as ATPG can resolve it, whereas a
#: possibly-detected-testable one has not. This is deliberately independent of
#: :attr:`AnalysisConfig.posdet_credit`, which governs test/fault coverage --
#: one shared knob would double-count a PD fault once the credit is non-zero.
DEFAULT_EFFECTIVENESS_POSDET_FAMILIES = ["PU"]

#: Which population ``atpg_effectiveness`` is computed over when a relevant
#: (waiver-excluded) population also exists.
#:
#: ``"total"`` reproduces the tool: it prints ONE effectiveness figure, derived
#: from the full population, in both the total and the relevant column. The
#: waived block was resolved by ATPG, so removing it from the denominator would
#: understate how much of the design ATPG actually settled. ``"population"``
#: re-bases the figure on whichever population is being reported.
DEFAULT_EFFECTIVENESS_BASIS = "total"

# ---------------------------------------------------------------------------
# Fault-disposition vocabulary
# ---------------------------------------------------------------------------
# A Tessent flow commonly runs a fault-disposition step after the last ATPG
# phase: it reclassifies a block of faults into a waiver subclass and excludes
# that subclass from the "total relevant" coverage column. A per-phase snapshot
# therefore describes a DIFFERENT population from the tool's final report, and
# the AU subclass distribution a fix plan is ranked from is rewritten by it.
#
# Everything below is a naming hint used to RANK candidate files and to explain
# a verdict. The pre/post determination itself is made from file CONTENTS --
# whether the waiver subclass is present -- never from a name alone.

#: Subclass tokens that name a disposition waiver bucket. Case-insensitive
#: globs matched against the dotted subclass (e.g. ``AU.DISPOSITION``).
DEFAULT_WAIVER_SUBCLASS_PATTERNS = ["*.DISPOSITION", "*.DISPOSED", "*.DISP",
                                    "*DISPOSITION*", "*.WAIVED", "*.WAIVER"]

#: Files that look like a complete fault list.
DEFAULT_FAULT_LIST_FILE_PATTERNS = ["*.mtfi", "*.mtfi.*", "*faults*",
                                    "*.flt", "*.flt.*", "*.fault", "*.fault.*"]

#: Files that hold only the waived block or another partial slice, never the
#: whole population. Excluded from candidacy so a 40 KB waiver list is never
#: mistaken for the fault list of the design.
DEFAULT_PARTIAL_FAULT_FILE_PATTERNS = ["*disp_faults*", "*_disp_*",
                                       "*.disposition.*", "*_orig.*",
                                       "*.detected.*", "*.del.*", "*.dt.*"]

#: Names suggesting the final, post-disposition list. ``fd`` is the fault
#: disposition tag; the rest are the usual final/full spellings.
DEFAULT_DISPOSITION_FILE_PATTERNS = ["*.fd", "*.fd.*", "*fault*.fd*",
                                     "*final*", "*post_disp*", "*postdisp*"]

#: Names carrying a per-phase tag. The capture group is the phase number, used
#: to prefer the LAST phase when several snapshots are present.
DEFAULT_PHASE_FILE_PATTERNS = [r"(?:^|[._-])(?:ph|phase|pass)[._-]?(\d+)"]


def _lower_list(values: Any, default: List[str]) -> List[str]:
    if not values:
        return list(default)
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _matches_any(value: str, patterns: List[str]) -> bool:
    """Case-insensitive glob match of *value* (basename) against *patterns*."""
    text = os.path.basename(str(value or "").strip())
    if not text:
        return False
    lowered = text.lower()
    return any(fnmatch.fnmatch(lowered, str(p).strip().lower())
               for p in patterns if str(p).strip())


@dataclass
class AnalysisConfig:
    """Every design-specific convention, in one overridable place.

    Attributes:
        class_roles: Fault-class token -> :class:`CoverageRole` value. Merged
            into :data:`DEFAULT_CLASS_ROLES` unless ``replace_defaults``.
        posdet_credit: Credit given to a possibly-detected (``PD``) fault in
            ``test_coverage`` and ``fault_coverage``. Defaults to ``0.0``,
            which is what Tessent reports; it is printed in every report
            header so a figure can be re-derived.
        effectiveness_posdet_families: Possibly-detected families credited at
            full weight in ``atpg_effectiveness`` only. Separate from
            ``posdet_credit`` so the two cannot double-count.
        effectiveness_basis: ``"total"`` (the tool's behaviour) or
            ``"population"``. See :data:`DEFAULT_EFFECTIVENESS_BASIS`.
        waiver_subclass_patterns: Globs naming a fault-disposition waiver
            subclass, used to discover it from the data.
        fault_list_file_patterns: Globs naming a complete fault-list file.
        partial_fault_file_patterns: Globs naming a partial fault file, which
            is excluded from fault-list candidacy.
        disposition_file_patterns: Globs suggesting a post-disposition list.
        phase_file_patterns: Regexes whose first group is a phase number.
        unknown_class_threshold_pct: Share of records that may carry an
            unrecognised fault class before parsing fails outright.
        unknown_class_fatal: Whether breaching that threshold raises.
        unresolved_constraint_threshold_pct: Same idea for constraint
            directives. Not fatal by default: a dofile legitimately contains
            site-specific commands, and refusing to analyse the design because
            of one is worse than reporting the gap prominently.
        unresolved_constraint_fatal: Whether breaching that threshold raises.
        unmapped_object_threshold_pct: Same idea for fault objects that do not
            map onto a netlist instance. Defaults to 100 (report only), since
            a partition analysed without its full cell library legitimately
            leaves objects unmapped and the per-cause diagnosis already
            reports them.
        unmapped_object_fatal: Whether breaching that threshold raises.
        sample_limit: Verbatim sample records retained per unrecognised token.
        scan_in_pins / scan_out_pins / shift_enable_pins / clock_pins: Pin-name
            vocabularies. Extended from config, never replaced, unless
            ``replace_defaults``.
        unconnected_net_patterns: Glob patterns naming a dangling net.
        tie_high_patterns / tie_low_patterns: Regex alternatives for tie cells.
        constraint_directives: Dofile directive -> canonical constraint kind.
        constraint_value_codes: Constraint value token -> logical value.
        replace_defaults: When True the JSON values replace rather than extend
            the built-in defaults.
        source: Where this configuration came from, for the report header.
    """

    class_roles: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_CLASS_ROLES))
    posdet_credit: float = 0.0
    effectiveness_posdet_families: List[str] = field(
        default_factory=lambda: list(DEFAULT_EFFECTIVENESS_POSDET_FAMILIES))
    effectiveness_basis: str = DEFAULT_EFFECTIVENESS_BASIS

    waiver_subclass_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_WAIVER_SUBCLASS_PATTERNS))
    fault_list_file_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_FAULT_LIST_FILE_PATTERNS))
    partial_fault_file_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_PARTIAL_FAULT_FILE_PATTERNS))
    disposition_file_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_DISPOSITION_FILE_PATTERNS))
    phase_file_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_PHASE_FILE_PATTERNS))

    unknown_class_threshold_pct: float = 0.1
    unknown_class_fatal: bool = True
    unresolved_constraint_threshold_pct: float = 10.0
    unresolved_constraint_fatal: bool = False
    unmapped_object_threshold_pct: float = 100.0
    unmapped_object_fatal: bool = False
    sample_limit: int = 20

    scan_in_pins: List[str] = field(
        default_factory=lambda: list(DEFAULT_SCAN_IN_PINS))
    scan_out_pins: List[str] = field(
        default_factory=lambda: list(DEFAULT_SCAN_OUT_PINS))
    shift_enable_pins: List[str] = field(
        default_factory=lambda: list(DEFAULT_SHIFT_ENABLE_PINS))
    clock_pins: List[str] = field(
        default_factory=lambda: list(DEFAULT_CLOCK_PINS))

    unconnected_net_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_UNCONNECTED_NET_PATTERNS))
    tie_high_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_TIE_HIGH_PATTERNS))
    tie_low_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_TIE_LOW_PATTERNS))

    constraint_directives: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_CONSTRAINT_DIRECTIVES))
    constraint_value_codes: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_CONSTRAINT_VALUE_CODES))

    replace_defaults: bool = False
    source: str = "built-in defaults"

    # -- fault-class helpers ---------------------------------------------
    def role_of(self, class_token: str) -> CoverageRole:
        """Coverage role of a (possibly dotted) class token.

        The family is the prefix before the first ``.``, so an unseen subclass
        such as ``AU.XYZ`` or ``UO.NEW`` resolves to its family rather than
        collapsing into ``UNKNOWN``. Only a wholly unknown *family* is unknown.
        """
        family = self.family_of(class_token)
        if not family:
            return CoverageRole.UNKNOWN
        role = self.class_roles.get(family)
        if role is None:
            return CoverageRole.UNKNOWN
        try:
            return CoverageRole(role)
        except ValueError:
            logger.warning(
                "Configured role %r for fault class %r is not a known "
                "coverage role; treating the class as unknown.", role, family)
            return CoverageRole.UNKNOWN

    @staticmethod
    def family_of(class_token: str) -> str:
        """Family prefix of a class token, upper-cased (``AU.TC`` -> ``AU``)."""
        return (class_token or "").strip().upper().split(".", 1)[0]

    def is_known_class(self, class_token: str) -> bool:
        """True when the class family appears in the role map."""
        return self.family_of(class_token) in self.class_roles

    def is_loss_class(self, class_token: str) -> bool:
        """True when the class represents debuggable coverage loss."""
        return self.role_of(class_token) in LOSS_ROLES

    def known_families(self) -> List[str]:
        """Every class family the role map knows, sorted."""
        return sorted(self.class_roles)

    # -- coverage metric helpers ------------------------------------------
    def credits_effectiveness(self, class_token: str) -> bool:
        """True when *class_token* is credited in ``atpg_effectiveness``.

        Only consulted for ``PD`` classes; every other class already carries
        its own role weight.
        """
        family = self.family_of(class_token)
        return any(family == str(f).strip().upper()
                   for f in self.effectiveness_posdet_families)

    # -- fault-disposition helpers ----------------------------------------
    def is_waiver_subclass(self, subclass_id: str) -> bool:
        """True when *subclass_id* matches a configured waiver-subclass glob."""
        return _matches_any(subclass_id, self.waiver_subclass_patterns)

    def looks_like_fault_list(self, filename: str) -> bool:
        """True when *filename* looks like a complete fault list."""
        return (_matches_any(filename, self.fault_list_file_patterns)
                and not self.looks_partial(filename))

    def looks_partial(self, filename: str) -> bool:
        """True when *filename* names only a slice of the fault population."""
        return _matches_any(filename, self.partial_fault_file_patterns)

    def looks_post_disposition(self, filename: str) -> bool:
        """True when *filename* is spelled like a final/post-disposition list."""
        return _matches_any(filename, self.disposition_file_patterns)

    def phase_of(self, filename: str) -> Optional[int]:
        """Phase number encoded in *filename*, or ``None`` when untagged."""
        name = os.path.basename(filename or "")
        for pattern in self.phase_file_patterns:
            try:
                match = re.search(pattern, name, re.IGNORECASE)
            except re.error:
                logger.warning("Ignoring unusable phase pattern %r", pattern)
                continue
            if match and match.groups():
                try:
                    return int(match.group(1))
                except (TypeError, ValueError):
                    continue
        return None

    # -- pin helpers ------------------------------------------------------
    def scan_pin_role(self, pin_name: str) -> Optional[str]:
        """``'scan_in'`` / ``'scan_out'`` / ``'shift_enable'`` or ``None``."""
        name = (pin_name or "").strip().lstrip(".").lower()
        if name in self.scan_in_pins:
            return "scan_in"
        if name in self.scan_out_pins:
            return "scan_out"
        if name in self.shift_enable_pins:
            return "shift_enable"
        return None

    def is_clock_pin(self, pin_name: str) -> bool:
        """True when *pin_name* is a clock pin under the configured vocabulary."""
        return (pin_name or "").strip().lstrip(".").lower() in self.clock_pins

    def documented_patterns(self) -> Dict[str, Any]:
        """Every configurable convention and its active value.

        Emitted into the report so a reader can audit which vocabulary the run
        used, which is the difference between "this library has no scan cells"
        and "this library names its scan pins something we were not told about".
        """
        return {
            "source": self.source,
            "class_roles": dict(self.class_roles),
            "posdet_credit": self.posdet_credit,
            "effectiveness_posdet_families":
                list(self.effectiveness_posdet_families),
            "effectiveness_basis": self.effectiveness_basis,
            "waiver_subclass_patterns": list(self.waiver_subclass_patterns),
            "fault_list_file_patterns": list(self.fault_list_file_patterns),
            "partial_fault_file_patterns":
                list(self.partial_fault_file_patterns),
            "disposition_file_patterns": list(self.disposition_file_patterns),
            "phase_file_patterns": list(self.phase_file_patterns),
            "unknown_class_threshold_pct": self.unknown_class_threshold_pct,
            "unresolved_constraint_threshold_pct":
                self.unresolved_constraint_threshold_pct,
            "unmapped_object_threshold_pct":
                self.unmapped_object_threshold_pct,
            "sample_limit": self.sample_limit,
            "scan_in_pins": list(self.scan_in_pins),
            "scan_out_pins": list(self.scan_out_pins),
            "shift_enable_pins": list(self.shift_enable_pins),
            "clock_pins": list(self.clock_pins),
            "unconnected_net_patterns": list(self.unconnected_net_patterns),
            "tie_high_patterns": list(self.tie_high_patterns),
            "tie_low_patterns": list(self.tie_low_patterns),
            "constraint_directives": sorted(self.constraint_directives),
        }

    # -- construction -----------------------------------------------------
    @classmethod
    def from_dict(cls, data: Dict[str, Any],
                  source: str = "dict") -> "AnalysisConfig":
        """Build a config from a JSON-shaped mapping, merging into defaults."""
        cfg = cls()
        data = dict(data or {})
        replace = bool(data.get("replace_defaults", False))
        cfg.replace_defaults = replace
        cfg.source = source

        known = {f.name for f in fields(cls)}
        unknown_keys = sorted(set(data) - known)
        if unknown_keys:
            logger.warning(
                "Ignoring unknown analysis-config key(s): %s. Known keys: %s",
                ", ".join(unknown_keys), ", ".join(sorted(known)))

        for name in ("class_roles", "constraint_directives",
                     "constraint_value_codes"):
            if name in data and isinstance(data[name], dict):
                incoming = {str(k).strip().upper() if name == "class_roles"
                            else str(k).strip().lower(): str(v)
                            for k, v in data[name].items()}
                if name == "class_roles":
                    incoming = {k: str(v).strip().upper()
                                for k, v in incoming.items()}
                base = {} if replace else dict(getattr(cfg, name))
                base.update(incoming)
                setattr(cfg, name, base)

        for name in ("scan_in_pins", "scan_out_pins", "shift_enable_pins",
                     "clock_pins"):
            if name in data:
                incoming = _lower_list(data[name], [])
                base = [] if replace else list(getattr(cfg, name))
                for item in incoming:
                    if item not in base:
                        base.append(item)
                setattr(cfg, name, base)

        for name in ("unconnected_net_patterns", "tie_high_patterns",
                     "tie_low_patterns", "effectiveness_posdet_families",
                     "waiver_subclass_patterns", "fault_list_file_patterns",
                     "partial_fault_file_patterns",
                     "disposition_file_patterns", "phase_file_patterns"):
            if name in data:
                incoming = [str(v) for v in data[name] if str(v).strip()]
                base = [] if replace else list(getattr(cfg, name))
                for item in incoming:
                    if item not in base:
                        base.append(item)
                setattr(cfg, name, base)

        for name, caster in (
            ("posdet_credit", float),
            ("effectiveness_basis", str),
            ("unknown_class_threshold_pct", float),
            ("unresolved_constraint_threshold_pct", float),
            ("unmapped_object_threshold_pct", float),
            ("sample_limit", int),
            ("unknown_class_fatal", bool),
            ("unresolved_constraint_fatal", bool),
            ("unmapped_object_fatal", bool),
        ):
            if name in data:
                try:
                    setattr(cfg, name, caster(data[name]))
                except (TypeError, ValueError):
                    logger.warning(
                        "Analysis-config key %r has an unusable value %r; "
                        "keeping the default %r.",
                        name, data[name], getattr(cfg, name))

        cfg._apply_env_pattern_overrides()
        return cfg

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AnalysisConfig":
        """Load configuration from *path*, ``ATPG_ANALYSIS_CONFIG`` or defaults.

        A missing or malformed file yields the documented defaults with a
        warning; the analyzer never refuses to run because of configuration.
        """
        chosen = path or os.environ.get(CONFIG_ENV_VAR, "").strip() or None
        if not chosen:
            cfg = cls()
            cfg._apply_env_pattern_overrides()
            return cfg
        try:
            with open(chosen, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001 - config must never abort a run
            logger.warning(
                "Could not read analysis config %s (%s); using defaults.",
                chosen, exc)
            cfg = cls()
            cfg._apply_env_pattern_overrides()
            return cfg
        logger.info("Analysis configuration loaded from %s", chosen)
        return cls.from_dict(data, source=chosen)

    def _apply_env_pattern_overrides(self) -> None:
        """Fold the legacy tie-pattern environment variables into the lists."""
        for env_var, attr in (("ATPG_TIE_HIGH_PATTERNS", "tie_high_patterns"),
                              ("ATPG_TIE_LOW_PATTERNS", "tie_low_patterns")):
            extra = os.environ.get(env_var, "").strip()
            if not extra:
                continue
            values = list(getattr(self, attr))
            for part in extra.split("|"):
                part = part.strip()
                if part and part not in values:
                    values.append(part)
            setattr(self, attr, values)

    def copy(self) -> "AnalysisConfig":
        """A deep copy, so a caller can tweak a config without side effects."""
        return copy.deepcopy(self)


_ACTIVE: Optional[AnalysisConfig] = None


def get_config() -> AnalysisConfig:
    """Return the process-wide active configuration, loading it on first use."""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = AnalysisConfig.load()
    return _ACTIVE


def set_config(config: Optional[AnalysisConfig]) -> AnalysisConfig:
    """Install *config* as the active configuration and return it.

    Passing ``None`` restores the defaults. Tests use this to exercise a
    different cell library or class map without touching the environment.
    """
    global _ACTIVE
    _ACTIVE = config if config is not None else AnalysisConfig.load()
    return _ACTIVE


def resolve(config: Optional[AnalysisConfig]) -> AnalysisConfig:
    """Return *config* when given, else the active configuration."""
    return config if config is not None else get_config()
