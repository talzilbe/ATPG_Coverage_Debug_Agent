"""Launching external DFT tools from the analysis session.

The analysis itself is offline and structural.  This package builds the command
chain that opens the vendor tool on the *same* design, so a finding can be
checked in the tool that owns the authoritative answer.

Nothing here executes a command: the builders return text and argument lists,
and the caller decides whether to run them.  Project-specific values live in
JSON profiles under ``profiles/`` and never in this code.
"""

from .live_session import (
    InspectAction,
    LiveSession,
    LiveSessionError,
    build_listener_tcl,
    new_token,
    read_port,
)
from .profiles import (
    ProfileError,
    ToolProfile,
    get_profile,
    list_profiles,
    load_profiles,
    profile_search_path,
)
from .terminals import (
    KNOWN_TERMINALS,
    TerminalSpec,
    display_available,
    find_terminal,
)
from .visualizer import (
    LaunchBundle,
    LaunchInputError,
    VisualizerInputs,
    build_dofile,
    build_launch_argv,
    build_psetup_script,
    build_tessent_script,
    derive_paths_from_run_dir,
    describe_chain,
    signal_inspect_actions,
    write_launch_bundle,
)

__all__ = [
    "KNOWN_TERMINALS",
    "InspectAction",
    "LaunchBundle",
    "LaunchInputError",
    "LiveSession",
    "LiveSessionError",
    "ProfileError",
    "TerminalSpec",
    "ToolProfile",
    "VisualizerInputs",
    "build_dofile",
    "build_launch_argv",
    "build_listener_tcl",
    "build_psetup_script",
    "build_tessent_script",
    "derive_paths_from_run_dir",
    "describe_chain",
    "display_available",
    "find_terminal",
    "get_profile",
    "list_profiles",
    "load_profiles",
    "new_token",
    "profile_search_path",
    "read_port",
    "signal_inspect_actions",
    "write_launch_bundle",
]
