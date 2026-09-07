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
import json
import logging
import os
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


def _lower_list(values: Any, default: List[str]) -> List[str]:
    if not values:
        return list(default)
    return [str(v).strip().lower() for v in values if str(v).strip()]


@dataclass
class AnalysisConfig:
    """Every design-specific convention, in one overridable place.

    Attributes:
        class_roles: Fault-class token -> :class:`CoverageRole` value. Merged
            into :data:`DEFAULT_CLASS_ROLES` unless ``replace_defaults``.
        posdet_credit: Credit given to a possibly-detected (``PD``) fault when
            computing coverage. Tessent's default is ``0.5``; it is printed in
            every report header so a figure can be re-derived.
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
    posdet_credit: float = 0.5

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
                     "tie_low_patterns"):
            if name in data:
                incoming = [str(v) for v in data[name] if str(v).strip()]
                base = [] if replace else list(getattr(cfg, name))
                for item in incoming:
                    if item not in base:
                        base.append(item)
                setattr(cfg, name, base)

        for name, caster in (
            ("posdet_credit", float),
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
