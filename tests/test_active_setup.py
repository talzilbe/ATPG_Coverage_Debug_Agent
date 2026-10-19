"""Launching from a shell that is already inside the project setup.

The setup must then be reused, not entered a second time; a profile that names
no marker variables keeps the old behaviour whatever the environment holds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from atpg_coverage_debug_agent.launcher import visualizer as vis
from atpg_coverage_debug_agent.launcher.profiles import ToolProfile
from atpg_coverage_debug_agent.launcher.terminals import TerminalSpec
from atpg_coverage_debug_agent.launcher.visualizer import (
    VisualizerInputs, build_psetup_script, describe_chain, detect_active_setup,
    launch_health, missing_inputs, setup_mismatches, write_launch_bundle,
)

REPO = Path(__file__).resolve().parent.parent

PROFILE = {
    "name": "setup_demo",
    "display_name": "Setup Demo",
    "shell": "/bin/tcsh",
    "psetup": {
        "executable": "/bin/echo", "proj": "demoproj/REL1", "cfg": "demo.cth",
        "command_flag": "-x",
        "active_env": ["DEMO_SESSION_ID", "DEMO_ENV_INDICATOR"],
        "setup_cmd_env": "DEMO_SETUP_CMD",
        "ward_env": "DEMO_WORKAREA",
    },
    "tool": {"executable": "/bin/echo"},
    "commands": {
        "load": [{"key": "faults", "command": "read_faults", "label": "Faults"}],
        "open": "open_visualizer",
    },
}

INSIDE = {
    "DEMO_SESSION_ID": "me_1_2",
    "DEMO_SETUP_CMD": ("/x/bin/setup -proj demoproj/REL1 -cfg demo.cth "
                       "-ward /work/me -x /tmp/a.csh"),
    "DEMO_WORKAREA": "/work/me",
}


@pytest.fixture()
def parts(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    faults = tmp_path / "f.gz"
    faults.write_text("x", encoding="utf-8")
    profile = ToolProfile.from_dict(PROFILE)
    inputs = VisualizerInputs(proj="demoproj/REL1", cfg="demo.cth",
                              paths={"faults": str(faults)})
    term = TerminalSpec("xterm", "-e", "-title", executable="/usr/bin/xterm")
    return profile, inputs, term


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_no_marker_means_not_inside(parts):
    profile, _, _ = parts
    assert detect_active_setup(profile, {}) is None
    assert detect_active_setup(profile, {"DEMO_SESSION_ID": "  "}) is None


def test_a_profile_without_markers_never_detects_a_setup(parts):
    profile, _, _ = parts
    profile.psetup.active_env = []
    assert detect_active_setup(profile, INSIDE) is None


def test_the_active_project_config_and_workarea_are_read(parts):
    profile, _, _ = parts
    active = detect_active_setup(profile, INSIDE)
    assert active.markers == ["DEMO_SESSION_ID"]
    assert (active.proj, active.cfg, active.ward) == (
        "demoproj/REL1", "demo.cth", "/work/me")


def test_any_single_marker_is_enough(parts):
    profile, _, _ = parts
    active = detect_active_setup(profile, {"DEMO_ENV_INDICATOR": "1"})
    assert active is not None and active.proj == ""


def test_a_setup_without_a_project_is_not_this_profiles(parts, tmp_path):
    # Cth.ai's own setup sets the same markers but enters no project.
    profile, inputs, term = parts
    env = {"DEMO_SESSION_ID": "me_1_2", "DEMO_WORKAREA": "/tmp/ai",
           "DEMO_SETUP_CMD": "/x/bin/setup -cfg_ov /tmp/c -ward /tmp/ai "
                             "-cmd /tmp/ai/reopen.sh"}
    assert detect_active_setup(profile, env) is None
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term,
                                 environ=env)
    outer = _read(bundle.psetup_script_path)
    assert not bundle.setup_reused
    assert ("unsetenv DEMO_SESSION_ID DEMO_ENV_INDICATOR DEMO_SETUP_CMD "
            "DEMO_WORKAREA\n") in outer
    assert outer.index("unsetenv") < outer.index("'/bin/echo' -proj")


def test_the_outer_script_runs_the_inner_script_without_entering_setup(parts):
    profile, inputs, _ = parts
    active = detect_active_setup(profile, INSIDE)
    script = build_psetup_script(profile, inputs, "/tmp/inner.csh", active)
    assert "/bin/echo" not in script and "-proj" not in script
    assert "\n'/tmp/inner.csh'\n" in script
    assert "set rc = $status" in script and 'set reply = "$<"' in script


def test_reuse_needs_neither_a_setup_executable_nor_a_project(parts):
    profile, inputs, _ = parts
    profile.psetup.executable = ""
    profile.psetup.proj = ""
    inputs.proj = ""
    active = detect_active_setup(profile, INSIDE)
    assert "'/tmp/i.csh'" in build_psetup_script(profile, inputs, "/tmp/i.csh",
                                                 active)
    assert "Project (-proj)" not in missing_inputs(profile, inputs, INSIDE)
    assert "Project (-proj)" in missing_inputs(profile, inputs, {})


def test_outside_the_setup_the_bundle_is_unchanged(parts, tmp_path):
    profile, inputs, term = parts
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term,
                                 environ={})
    assert not bundle.setup_reused
    assert "'/bin/echo' -proj 'demoproj/REL1'" in _read(bundle.psetup_script_path)


def test_inside_the_setup_the_bundle_reuses_it(parts, tmp_path):
    profile, inputs, term = parts
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term,
                                 environ=INSIDE)
    assert bundle.setup_reused and bundle.as_dict()["setup_reused"]
    outer = _read(bundle.psetup_script_path)
    assert "-proj" not in outer
    assert f"'{bundle.tessent_script_path}'" in outer
    assert not any("Already inside" in w for w in bundle.warnings)


def test_heavy_mode_inside_the_setup_submits_directly(parts, tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("DISPLAY", "box.example.com:5")
    monkeypatch.setattr(vis, "local_addresses", lambda: ["10.1.2.3"])
    profile, inputs, term = parts
    inputs.heavy = True
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term,
                                 environ=INSIDE)
    outer = _read(bundle.psetup_script_path)
    assert "-proj" not in outer
    assert f"'{bundle.submit_script_path}'" in outer


@pytest.mark.parametrize("field,value,label", [
    ("proj", "other/REL2", "project other"),
    ("cfg", "other.cth", "config other"),
])
def test_a_different_setup_is_reused_with_a_warning(parts, tmp_path, field,
                                                    value, label):
    profile, inputs, term = parts
    env = dict(INSIDE)
    env["DEMO_SETUP_CMD"] = env["DEMO_SETUP_CMD"].replace(
        "demoproj/REL1" if field == "proj" else "demo.cth", value)
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term,
                                 environ=env)
    assert bundle.setup_reused
    assert any("Already inside" in w and label in w for w in bundle.warnings)


def test_a_different_workarea_is_warned_about(parts, tmp_path):
    profile, inputs, _ = parts
    inputs.ward = str(tmp_path)
    active = detect_active_setup(profile, INSIDE)
    warnings = setup_mismatches(profile, inputs, active)
    assert warnings and "workarea /work/me" in warnings[0]
    inputs.ward = "/work/me/"
    assert setup_mismatches(profile, inputs, active) == []


def test_health_reports_the_reused_setup_and_does_not_need_the_wrapper(parts):
    profile, inputs, _ = parts
    profile.psetup.executable = "/no/such/setup"
    checks = {c.label: c for c in launch_health(profile, inputs, INSIDE)}
    assert checks["Project setup"].ok
    assert "reused" in checks["Project setup"].detail
    assert "Project setup wrapper" not in checks

    outside = {c.label: c for c in launch_health(profile, inputs, {})}
    assert not outside["Project setup wrapper"].ok
    assert "Project setup" not in outside


def test_health_warns_but_does_not_block_on_a_mismatch(parts):
    profile, inputs, _ = parts
    inputs.proj = "other/REL2"
    checks = [c for c in launch_health(profile, inputs, INSIDE)
              if c.label == "Project setup matches the form"]
    assert checks and not checks[0].ok and not checks[0].blocking


def test_the_described_chain_skips_the_setup_line(parts):
    profile, inputs, _ = parts
    inside = describe_chain(profile, inputs, environ=INSIDE)
    assert inside[0].startswith("# already inside project setup")
    assert "echo is not run again" in inside[0]
    assert describe_chain(profile, inputs, environ={})[0].startswith("/bin/echo -proj")


def test_the_new_profile_fields_round_trip(parts):
    profile, _, _ = parts
    again = ToolProfile.from_dict(profile.as_dict())
    assert again.psetup.active_env == ["DEMO_SESSION_ID", "DEMO_ENV_INDICATOR"]
    assert again.psetup.setup_cmd_env == "DEMO_SETUP_CMD"
    assert again.psetup.ward_env == "DEMO_WORKAREA"


def test_the_shipped_cheetah_profile_declares_its_markers():
    path = REPO / "profiles" / "ttlc.json"
    if not path.exists():
        pytest.skip("site profile not present")
    psetup = json.loads(path.read_text(encoding="utf-8"))["psetup"]
    assert "CTH_SESSION_ID" in psetup["active_env"]
    assert psetup["setup_cmd_env"] == "CTH_SETUP_CMD"
    assert psetup["ward_env"] == "WORKAREA"


@pytest.mark.skipif(shutil.which("tcsh") is None, reason="tcsh not installed")
def test_the_reused_outer_script_is_valid_tcsh(parts, tmp_path):
    profile, inputs, term = parts
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term,
                                 environ=INSIDE)
    result = subprocess.run(["tcsh", "-n", bundle.psetup_script_path],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
