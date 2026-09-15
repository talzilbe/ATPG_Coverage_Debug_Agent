"""Tests for the external-tool launcher.

The launch chain reaches a shell, so the interesting cases here are the ones
where an input is hostile or merely wrong: everything that lands in a generated
script has to be validated before it gets there.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from atpg_coverage_debug_agent.launcher import profiles as profiles_mod
from atpg_coverage_debug_agent.launcher import terminals as terminals_mod
from atpg_coverage_debug_agent.launcher import visualizer as vis
from atpg_coverage_debug_agent.launcher.profiles import (
    ProfileError, ToolProfile, get_profile, list_profiles, load_profiles,
    profile_search_path,
)
from atpg_coverage_debug_agent.launcher.terminals import (
    TerminalSpec, find_terminal,
)
from atpg_coverage_debug_agent.launcher.visualizer import (
    LaunchInputError, VisualizerInputs, build_commands, build_dofile,
    build_launch_argv, build_psetup_script, build_tessent_script,
    derive_paths_from_run_dir, describe_chain, fault_inspect_commands,
    missing_inputs, write_launch_bundle,
)

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
PROFILE_DATA = {
    "name": "demo",
    "display_name": "Demo Project",
    "shell": "/bin/tcsh",
    "psetup": {
        "executable": "/bin/echo",
        "proj": "demoproj/REL1",
        "cfg": "demo.cth",
        "command_flag": "-x",
    },
    "environment": {"DEMO_LICENSE_SERVER": "1717@licsrv.example.com"},
    "tool": {"executable": "/bin/echo"},
    "commands": {
        "context": "set_context pattern -scan",
        "load": [
            {"key": "icl", "command": "read_icl", "label": "ICL file",
             "run_dir_glob": "icl/*.icl"},
            {"key": "flat_model", "command": "read_flat_model",
             "label": "Flat model", "switches": ["-en", "on"],
             "run_dir_glob": "model/*.flat.gz"},
            {"key": "faults", "command": "read_faults", "label": "Fault list",
             "switches": ["-retain"], "run_dir_glob": "faultlist/*.faults.gz"},
            {"key": "notes", "command": "read_notes", "label": "Notes",
             "required": False},
        ],
        "open": "open_visualizer",
        "fault_inspect": ["analyze_fault {fault} -stuck_at {stuck}"],
    },
    "terminal_preference": ["xterm"],
}


@pytest.fixture()
def profile_dir(tmp_path):
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "demo.json").write_text(json.dumps(PROFILE_DATA), encoding="utf-8")
    return directory


@pytest.fixture()
def profile(profile_dir):
    return get_profile("demo", search_path=[profile_dir])


@pytest.fixture()
def design_paths(tmp_path):
    paths = {}
    for key in ("icl", "flat_model", "faults"):
        path = tmp_path / f"{key}.dat"
        path.write_text("x", encoding="utf-8")
        paths[key] = str(path)
    return paths


@pytest.fixture()
def inputs(design_paths):
    return VisualizerInputs(proj="demoproj/REL1", cfg="demo.cth",
                            paths=dict(design_paths))


@pytest.fixture()
def fake_terminal():
    return TerminalSpec("xterm", exec_flag="-e", title_flag="-title",
                        executable="/usr/bin/xterm")


# ---------------------------------------------------------------------------
# Profiles are data
# ---------------------------------------------------------------------------
def test_a_profile_is_loaded_from_a_json_file(profile):
    assert profile.name == "demo"
    assert profile.title == "Demo Project"
    assert profile.load_keys == ["icl", "flat_model", "faults", "notes"]
    assert profile.load_command("faults").switches == ["-retain"]
    assert profile.load_command("nope") is None


def test_the_environment_variable_adds_a_search_directory(profile_dir, monkeypatch):
    monkeypatch.setenv(profiles_mod.PROFILE_PATH_ENV, str(profile_dir))
    assert profile_dir in profile_search_path()
    assert "demo" in load_profiles()


def test_an_earlier_directory_shadows_a_later_one(tmp_path, profile_dir):
    override_dir = tmp_path / "mine"
    override_dir.mkdir()
    data = dict(PROFILE_DATA, display_name="Mine")
    (override_dir / "demo.json").write_text(json.dumps(data), encoding="utf-8")
    found = load_profiles([override_dir, profile_dir])
    assert found["demo"].title == "Mine"


def test_one_broken_profile_does_not_hide_the_others(profile_dir):
    (profile_dir / "broken.json").write_text("{not json", encoding="utf-8")
    (profile_dir / "nameless.json").write_text("{}", encoding="utf-8")
    found = load_profiles([profile_dir])
    assert "demo" in found


def test_an_unknown_profile_names_the_ones_that_exist(profile_dir):
    with pytest.raises(ProfileError) as excinfo:
        get_profile("nope", search_path=[profile_dir])
    assert "demo" in str(excinfo.value)


def test_a_profile_round_trips_through_its_dict(profile):
    again = ToolProfile.from_dict(profile.as_dict())
    assert again.as_dict() == profile.as_dict()


def test_a_load_command_must_name_a_command():
    with pytest.raises(ProfileError):
        profiles_mod.LoadCommand.from_dict({"key": "x"})


def test_the_shipped_profiles_directory_parses():
    """Every profile we ship must actually load."""
    shipped = list_profiles([REPO / "profiles"])
    assert shipped, "no launch profiles are shipped"
    for entry in shipped:
        assert entry.psetup.executable
        assert entry.tool.executable
        assert entry.commands.open


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
def test_the_commands_are_built_in_profile_order(profile, inputs):
    commands = build_commands(profile, inputs)
    assert commands[0] == "set_context pattern -scan"
    assert commands[-1] == "open_visualizer"
    assert [c.split()[0] for c in commands[1:-1]] == [
        "read_icl", "read_flat_model", "read_faults"]


def test_switches_follow_the_path_not_precede_it(profile, inputs, design_paths):
    commands = build_commands(profile, inputs)
    flat = next(c for c in commands if c.startswith("read_flat_model"))
    assert flat == "read_flat_model {%s} -en on" % design_paths["flat_model"]
    faults = next(c for c in commands if c.startswith("read_faults"))
    assert faults.endswith(" -retain")


def test_every_path_is_brace_quoted(profile, inputs, design_paths):
    dofile = build_dofile(profile, inputs)
    for path in design_paths.values():
        assert "{" + path + "}" in dofile


def test_an_optional_path_is_skipped_when_empty(profile, inputs):
    assert not any(c.startswith("read_notes") for c in build_commands(profile, inputs))


def test_an_optional_path_is_used_when_given(profile, inputs, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("x", encoding="utf-8")
    inputs.paths["notes"] = str(notes)
    assert any(c.startswith("read_notes") for c in build_commands(profile, inputs))


def test_a_missing_required_path_is_refused_by_name(profile, inputs):
    inputs.paths.pop("faults")
    with pytest.raises(LaunchInputError) as excinfo:
        build_commands(profile, inputs)
    assert "Fault list" in str(excinfo.value)


def test_missing_inputs_lists_what_is_still_needed(profile, design_paths):
    empty = VisualizerInputs(paths={})
    missing = missing_inputs(profile, empty)
    assert "ICL file" in missing and "Fault list" in missing
    assert "Notes" not in missing


def test_extra_commands_are_appended_after_the_open(profile, inputs):
    commands = build_commands(profile, inputs, ["report_faults -summary"])
    assert commands[-1] == "report_faults -summary"
    assert commands[-2] == "open_visualizer"


# ---------------------------------------------------------------------------
# Refusing hostile and malformed input
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hostile", [
    "/tmp/x; rm -rf /",
    "/tmp/$(whoami)",
    "/tmp/`whoami`",
    "/tmp/a|b",
    "/tmp/a&b",
    "/tmp/a>b",
    "/tmp/a\nb",
    "/tmp/w{x}",
    "/tmp/back\\slash",
    "/tmp/quo'te",
])
def test_a_path_that_could_escape_its_quoting_is_refused(profile, inputs, hostile):
    inputs.paths["icl"] = hostile
    with pytest.raises(LaunchInputError):
        build_commands(profile, inputs)


@pytest.mark.parametrize("hostile", ["a b", "p;ls", "$X", "a`b`", "p&q", ""])
def test_a_bad_project_token_is_refused(profile, inputs, hostile):
    inputs.proj = hostile
    profile.psetup.proj = ""
    with pytest.raises(LaunchInputError):
        build_psetup_script(profile, inputs, "/tmp/inner.csh")


@pytest.mark.parametrize("licence", [
    "1717@host.example.com",
    "1717@a.b:1717@c.d:2020@e.f",
])
def test_a_well_formed_licence_is_accepted(licence):
    assert vis.validate_licence(licence) == licence


@pytest.mark.parametrize("licence", [
    "1717", "host.example.com", "1717@host;ls", "1717@a b", "1717@a:'x'",
])
def test_a_malformed_licence_is_refused(licence):
    with pytest.raises(LaunchInputError):
        vis.validate_licence(licence)


def test_an_empty_licence_is_allowed_because_the_profile_supplies_one():
    assert vis.validate_licence("") == ""


def test_a_licence_from_the_user_overrides_the_profile(profile, inputs):
    inputs.licence_server = "2020@other.example.com"
    script = build_tessent_script(profile, inputs, "/tmp/d.do", "/tmp/t.log")
    assert "2020@other.example.com" in script
    assert "1717@licsrv.example.com" not in script


def test_a_bad_environment_variable_name_is_refused(profile, inputs):
    profile.environment["not a name"] = "x"
    with pytest.raises(LaunchInputError):
        build_tessent_script(profile, inputs, "/tmp/d.do", "/tmp/t.log")


def test_an_environment_value_cannot_close_its_quote(profile, inputs):
    profile.environment["DEMO_VAR"] = "a'; rm -rf /; echo '"
    with pytest.raises(LaunchInputError):
        build_tessent_script(profile, inputs, "/tmp/d.do", "/tmp/t.log")


def test_an_extra_command_cannot_smuggle_a_newline(profile, inputs):
    with pytest.raises(LaunchInputError):
        build_commands(profile, inputs, ["report_faults\nexit -force"])


def test_a_ward_must_be_an_existing_directory(profile, inputs, tmp_path):
    inputs.ward = str(tmp_path / "nope")
    with pytest.raises(LaunchInputError):
        build_psetup_script(profile, inputs, "/tmp/inner.csh")


# ---------------------------------------------------------------------------
# Generated scripts
# ---------------------------------------------------------------------------
def test_the_inner_script_sets_the_environment_then_execs_the_tool(profile, inputs):
    script = build_tessent_script(profile, inputs, "/tmp/d.do", "/tmp/t.log")
    lines = [l for l in script.splitlines() if l.strip()]
    assert lines[0] == "#!/bin/tcsh -f"
    setenv_at = next(i for i, l in enumerate(lines) if l.startswith("setenv "))
    exec_at = next(i for i, l in enumerate(lines) if l.startswith("exec "))
    assert setenv_at < exec_at
    assert "-dofile '/tmp/d.do'" in script
    assert "-logfile '/tmp/t.log'" in script
    assert "-replace" in script


def test_the_outer_script_runs_setup_with_the_stay_in_shell_flag(profile, inputs):
    script = build_psetup_script(profile, inputs, "/tmp/inner.csh")
    assert " -x '/tmp/inner.csh'" in script
    assert "-proj 'demoproj/REL1'" in script
    assert "-cfg 'demo.cth'" in script


def test_the_outer_script_holds_the_window_open_after_a_failure(profile, inputs):
    """A setup failure must be readable, not a window that blinks out."""
    script = build_psetup_script(profile, inputs, "/tmp/inner.csh")
    assert "set rc = $status" in script
    assert "Press Enter to close" in script
    assert 'set reply = "$<"' in script


def test_the_ward_is_only_passed_when_asked_for(profile, inputs, tmp_path):
    assert " -ward " not in build_psetup_script(profile, inputs, "/tmp/i.csh")
    inputs.ward = str(tmp_path)
    assert f" -ward '{tmp_path}'" in build_psetup_script(profile, inputs, "/tmp/i.csh")


def test_a_profile_without_a_tool_executable_is_refused(profile, inputs):
    profile.tool.executable = ""
    with pytest.raises(LaunchInputError):
        build_tessent_script(profile, inputs, "/tmp/d.do", "/tmp/t.log")


def test_a_profile_without_a_setup_executable_is_refused(profile, inputs):
    profile.psetup.executable = ""
    with pytest.raises(LaunchInputError):
        build_psetup_script(profile, inputs, "/tmp/i.csh")


# ---------------------------------------------------------------------------
# Terminals
# ---------------------------------------------------------------------------
def test_each_terminal_style_wraps_an_argv_correctly():
    xterm = TerminalSpec("xterm", "-e", "-title", executable="/usr/bin/xterm")
    assert xterm.wrap(["/tmp/s.csh"], "T") == [
        "/usr/bin/xterm", "-title", "T", "-e", "/tmp/s.csh"]

    gnome = TerminalSpec("gnome-terminal", "--", "--title", title_joined=True,
                         executable="/usr/bin/gnome-terminal")
    assert gnome.wrap(["/tmp/s.csh"], "T") == [
        "/usr/bin/gnome-terminal", "--title=T", "--", "/tmp/s.csh"]


def test_an_unresolved_terminal_refuses_to_wrap():
    with pytest.raises(ValueError):
        TerminalSpec("xterm", "-e").wrap(["/tmp/s.csh"])


def test_a_preferred_terminal_that_is_absent_is_skipped(monkeypatch):
    monkeypatch.setattr(
        terminals_mod.shutil, "which",
        lambda name: "/usr/bin/xterm" if name == "xterm" else None)
    assert find_terminal(["konsole", "xterm"]).name == "xterm"


def test_no_terminal_at_all_yields_none(monkeypatch):
    monkeypatch.setattr(terminals_mod.shutil, "which", lambda name: None)
    assert find_terminal(["xterm"]) is None


def test_launching_without_a_terminal_says_to_copy_the_commands(profile, monkeypatch):
    monkeypatch.setattr(terminals_mod.shutil, "which", lambda name: None)
    with pytest.raises(LaunchInputError) as excinfo:
        build_launch_argv(profile, "/tmp/s.csh")
    assert "Copy commands" in str(excinfo.value)


def test_the_display_check_reads_the_environment(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not terminals_mod.display_available()
    monkeypatch.setenv("DISPLAY", ":0")
    assert terminals_mod.display_available()


# ---------------------------------------------------------------------------
# The bundle on disk
# ---------------------------------------------------------------------------
def test_the_bundle_writes_three_artefacts(profile, inputs, tmp_path, fake_terminal):
    dest = tmp_path / "bundle"
    bundle = write_launch_bundle(profile, inputs, str(dest), fake_terminal)
    for path in (bundle.dofile_path, bundle.tessent_script_path,
                 bundle.psetup_script_path):
        assert os.path.isfile(path)
    assert bundle.argv[-1] == bundle.psetup_script_path
    assert bundle.terminal == "xterm"
    assert bundle.commands[-1] == "open_visualizer"


def test_the_generated_scripts_are_executable_and_private(profile, inputs,
                                                         tmp_path, fake_terminal):
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), fake_terminal)
    for path in (bundle.psetup_script_path, bundle.tessent_script_path):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(bundle.dofile_path).st_mode) == 0o600


def test_the_bundle_warns_when_there_is_no_display(profile, inputs, tmp_path,
                                                   fake_terminal, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), fake_terminal)
    assert any("DISPLAY" in w for w in bundle.warnings)


def test_the_bundle_round_trips_through_its_dict(profile, inputs, tmp_path,
                                                 fake_terminal):
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), fake_terminal)
    assert bundle.as_dict()["argv"] == bundle.argv


def test_the_inputs_round_trip_through_their_dict(inputs):
    again = VisualizerInputs.from_dict(inputs.as_dict())
    assert again.as_dict() == inputs.as_dict()


# ---------------------------------------------------------------------------
# Run-directory derivation
# ---------------------------------------------------------------------------
def _make_run_dir(root: Path) -> Path:
    (root / "icl").mkdir(parents=True)
    (root / "icl" / "design.icl").write_text("x", encoding="utf-8")
    (root / "model").mkdir()
    (root / "model" / "design.flat.gz").write_text("x", encoding="utf-8")
    (root / "faultlist").mkdir()
    (root / "faultlist" / "design.faults.gz").write_text("x", encoding="utf-8")
    return root


def test_the_run_directory_fills_in_the_paths(profile, tmp_path):
    run_dir = _make_run_dir(tmp_path / "run")
    found, warnings = derive_paths_from_run_dir(profile, str(run_dir))
    assert set(found) == {"icl", "flat_model", "faults"}
    assert found["faults"].endswith("design.faults.gz")
    assert any("Notes" in w for w in warnings)


def test_an_ambiguous_match_is_reported_not_chosen_silently(profile, tmp_path):
    run_dir = _make_run_dir(tmp_path / "run")
    (run_dir / "faultlist" / "other.faults.gz").write_text("x", encoding="utf-8")
    found, warnings = derive_paths_from_run_dir(profile, str(run_dir))
    assert "faults" in found
    assert any("2 files matched" in w for w in warnings)


def test_a_missing_run_directory_is_reported(profile, tmp_path):
    found, warnings = derive_paths_from_run_dir(profile, str(tmp_path / "nope"))
    assert found == {}
    assert warnings and "does not exist" in warnings[0]


def test_an_empty_run_directory_is_reported(profile):
    found, warnings = derive_paths_from_run_dir(profile, "")
    assert found == {} and warnings


# ---------------------------------------------------------------------------
# Fault inspection
# ---------------------------------------------------------------------------
def test_a_fault_command_is_rendered_with_its_stuck_value(profile):
    assert fault_inspect_commands(profile, "/top/u_a/Q", "1") == [
        "analyze_fault {/top/u_a/Q} -stuck_at 1"]


def test_a_bus_subscript_survives_because_braces_make_it_literal(profile):
    rendered = fault_inspect_commands(profile, "/top/u_a/Q[3]", "0")
    assert rendered == ["analyze_fault {/top/u_a/Q[3]} -stuck_at 0"]


def test_a_command_needing_a_stuck_value_is_skipped_without_one(profile):
    assert fault_inspect_commands(profile, "/top/u_a/Q", "") == []


def test_a_bad_stuck_value_is_refused(profile):
    with pytest.raises(LaunchInputError):
        fault_inspect_commands(profile, "/top/u_a/Q", "2; exit")


@pytest.mark.parametrize("hostile", ["/top/{x}", "/top/a\\b", "/top/a\nb", ""])
def test_a_hostile_fault_path_is_refused(profile, hostile):
    with pytest.raises(LaunchInputError):
        fault_inspect_commands(profile, hostile, "0")


# ---------------------------------------------------------------------------
# The written account of the chain
# ---------------------------------------------------------------------------
def test_the_chain_description_covers_every_stage(profile, inputs):
    steps = describe_chain(profile, inputs)
    joined = "\n".join(steps)
    assert "-proj demoproj/REL1" in joined
    assert "setenv DEMO_LICENSE_SERVER" in joined
    assert "-shell" in joined
    assert "set_context pattern -scan" in joined
    assert steps[-1] == "open_visualizer"


def test_the_chain_description_is_not_quoted_because_it_is_for_reading(profile, inputs):
    """It is pasted by a human; the quoting belongs in the generated files."""
    steps = describe_chain(profile, inputs)
    assert not any(step.startswith("'") for step in steps)
