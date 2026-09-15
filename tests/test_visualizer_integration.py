"""Integration tests for the viewer launch across the surfaces that carry it.

The launch details have to survive a save/load cycle, reach the agent, appear
in the reports, and not trip the honesty guardrails.  Each of those is a
separate seam, so each gets a test.
"""

import json
import os

import pytest

from atpg_coverage_debug_agent.analysis import guardrails, investigate
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.reporting.html_report import build_html_report
from atpg_coverage_debug_agent.reporting.markdown_report import render_markdown
from atpg_coverage_debug_agent.reporting.session_report import (
    load_report, save_report,
)

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "sample_data")

PROFILE_DATA = {
    "name": "demo",
    "display_name": "Demo Project",
    "psetup": {"executable": "/bin/echo", "proj": "demoproj/REL1",
               "cfg": "demo.cth"},
    "environment": {"DEMO_LICENSE_SERVER": "1717@licsrv.example.com"},
    "tool": {"executable": "/bin/echo"},
    "commands": {
        "context": "set_context pattern -scan",
        "load": [
            {"key": "icl", "command": "read_icl", "label": "ICL file"},
            {"key": "faults", "command": "read_faults", "label": "Fault list",
             "switches": ["-retain"]},
        ],
        "open": "open_visualizer",
        "fault_inspect": ["analyze_fault {fault} -stuck_at {stuck}"],
    },
}


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "demo.json").write_text(json.dumps(PROFILE_DATA), encoding="utf-8")
    monkeypatch.setenv("ATPG_TOOL_PROFILES", str(directory))
    return directory


@pytest.fixture()
def report(tmp_path, profile_env):
    icl = tmp_path / "design.icl"
    icl.write_text("x", encoding="utf-8")
    faults = os.path.join(SAMPLE, "demo_faults.mtfi")
    built = run_analysis(os.path.join(SAMPLE, "demo_netlist.v"), faults,
                         os.path.join(SAMPLE, "demo_constraints.do"))
    built.visualizer_config = {
        "profile": "demo",
        "proj": "demoproj/REL1",
        "cfg": "demo.cth",
        "ward": "",
        "licence_server": "",
        "paths": {"icl": str(icl), "faults": faults},
    }
    return built


# ---------------------------------------------------------------------------
# The agent's view
# ---------------------------------------------------------------------------
def test_the_context_carries_the_launch_chain(report):
    context = investigate.serialize_context(report)
    viewer = context["visualizer"]
    assert viewer["profile"] == "demo"
    assert viewer["steps"][-1] == "open_visualizer"
    assert any("read_faults" in step for step in viewer["steps"])


def test_the_visualizer_section_is_reachable_through_report_context(report):
    context = investigate.serialize_context(report)
    out = investigate.report_context(context, section="visualizer")
    assert out["visualizer"]["profile"] == "demo"


def test_the_tool_returns_the_chain(report):
    context = investigate.serialize_context(report)
    out = investigate.run_tool(
        "visualizer_commands", {}, fault_results=report.fault_results,
        constraints=report.constraints, netlist=report.netlist, context=context)
    assert out["steps"]
    assert "does not run them" in out["note"]


def test_the_tool_adds_commands_for_one_fault(report):
    context = investigate.serialize_context(report)
    out = investigate.run_tool(
        "visualizer_commands", {"fault": "/top/u_a/Q", "stuck": "1"},
        fault_results=report.fault_results, constraints=report.constraints,
        netlist=report.netlist, context=context)
    assert out["fault_commands"] == ["analyze_fault {/top/u_a/Q} -stuck_at 1"]


def test_the_tool_says_so_when_no_session_is_configured(report):
    report.visualizer_config = None
    context = investigate.serialize_context(report)
    out = investigate.run_tool(
        "visualizer_commands", {}, fault_results=report.fault_results,
        constraints=report.constraints, netlist=report.netlist, context=context)
    assert "error" in out
    assert "cannot supply" in out["error"]


def test_a_profile_that_no_longer_exists_is_admitted_not_guessed(report, monkeypatch):
    report.visualizer_config["profile"] = "vanished"
    payload = investigate.serialize_visualizer(report.visualizer_config)
    assert payload["steps"] == []
    assert "could not be rendered" in payload["unavailable"]


def test_the_tool_is_declared_in_the_specs():
    assert "visualizer_commands" in investigate.TOOL_SPECS
    spec = investigate.TOOL_SPECS["visualizer_commands"]
    assert set(spec["params"]) == {"fault", "stuck"}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_the_launch_details_survive_a_save_and_load(report, tmp_path):
    path = tmp_path / "session.json"
    save_report(report, str(path))
    again = load_report(str(path))
    assert again.visualizer_config == report.visualizer_config


def test_a_session_without_a_viewer_loads_with_none(report, tmp_path):
    report.visualizer_config = None
    path = tmp_path / "session.json"
    save_report(report, str(path))
    assert load_report(str(path)).visualizer_config is None


def test_an_older_session_file_without_the_key_still_loads(report, tmp_path):
    path = tmp_path / "session.json"
    save_report(report, str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("visualizer_config")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_report(str(path)).visualizer_config is None


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def test_the_markdown_report_shows_how_to_reproduce(report):
    text = render_markdown(report)
    assert "## Reproduce in Tessent Visualizer" in text
    assert "open_visualizer" in text


def test_the_markdown_report_stays_silent_without_a_session(report):
    report.visualizer_config = None
    assert "Reproduce in Tessent Visualizer" not in render_markdown(report)


def test_the_html_report_shows_how_to_reproduce(report):
    html = build_html_report(report)
    assert "Reproduce in Tessent Visualizer" in html
    assert "open_visualizer" in html


def test_the_html_block_adds_no_numbered_section(report):
    """Section numbering is checked for gaps; this must stay a sub-heading."""
    with_viewer = build_html_report(report)
    report.visualizer_config = None
    without = build_html_report(report)
    assert with_viewer.count("<h2>") == without.count("<h2>")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
def test_a_viewer_path_may_be_quoted_without_being_called_invented(report):
    """The user supplied these paths, so they are legitimate sources."""
    registry = guardrails.PathRegistry.from_report(report)
    icl = report.visualizer_config["paths"]["icl"]
    assert not guardrails.scan_paths(f"Load {icl} in the viewer.", registry)


def test_the_generated_reproduce_block_passes_the_guardrails(report):
    """The tool's own output must not violate its own honesty rules."""
    registry = guardrails.PathRegistry.from_report(report)
    text = render_markdown(report)
    start = text.index("## Reproduce in Tessent Visualizer")
    block = text[start:]
    assert not guardrails.scan_paths(block, registry)
    assert not guardrails.scan_claims(block)
