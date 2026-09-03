"""Tests for the per-category fault dumps and the ``list_category_faults`` tool.

Two things must never drift apart: the CSV row count for a category and the
count the triage table prints for it, and the fault set the dump files hold
versus the one the agent tool returns. Both are asserted here, over the live
report and over a rehydrated (out-of-process) evidence payload.
"""

from __future__ import annotations

import csv
import json
import os

import pytest

from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.analysis.guardrails import audit_report
from atpg_coverage_debug_agent.analysis.report_edit import apply_exclusions
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.reporting import category_dump as cd
from atpg_coverage_debug_agent.reporting.csv_report import COLUMNS
from atpg_coverage_debug_agent.reporting.html_report import build_html_report
from atpg_coverage_debug_agent.reporting.markdown_report import (
    render_markdown,
    write_markdown,
)
from atpg_coverage_debug_agent.reporting.session_report import (
    load_report,
    save_report,
)

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLE = os.path.join(_HERE, "sample_data")


@pytest.fixture(scope="module")
def demo_report():
    return run_analysis(
        os.path.join(_SAMPLE, "demo_netlist.v"),
        os.path.join(_SAMPLE, "demo_faults.mtfi"),
        os.path.join(_SAMPLE, "demo_constraints.do"),
    )


@pytest.fixture
def dumped(demo_report, tmp_path):
    base = str(tmp_path / "atpg_report.md")
    dumps = cd.write_category_dumps(demo_report, base)
    return demo_report, base, cd.dump_dir_for(base), dumps


# ---------------------------------------------------------------------------
# File layout
# ---------------------------------------------------------------------------
def test_every_selected_category_gets_a_csv_and_a_json(dumped):
    report, _base, directory, dumps = dumped
    assert dumps, "the demo must select at least one category"
    assert len(dumps) == len(report.selected_categories)
    for dump in dumps:
        assert os.path.isfile(os.path.join(directory, dump.csv_name))
        assert os.path.isfile(os.path.join(directory, dump.json_name))


def test_the_sidecar_directory_sits_beside_the_report():
    assert cd.dump_dir_for("/tmp/atpg_report.md") == "/tmp/atpg_report_categories"
    assert cd.dump_dir_for("/tmp/run.html") == "/tmp/run_categories"


def test_csv_row_count_matches_the_triage_count(dumped):
    _report, _base, directory, dumps = dumped
    for dump in dumps:
        with open(os.path.join(directory, dump.csv_name),
                  newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == dump.count, dump.subclass_id


def test_csv_header_is_the_shared_column_set(dumped):
    """A category CSV must be loadable exactly like the full per-fault CSV."""
    _report, _base, directory, dumps = dumped
    with open(os.path.join(directory, dumps[0].csv_name),
              newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert header == COLUMNS


def test_index_lists_every_category(dumped):
    _report, _base, directory, dumps = dumped
    with open(os.path.join(directory, "index.json"), encoding="utf-8") as handle:
        index = json.load(handle)
    assert index["format"] == cd.INDEX_FORMAT
    assert index["columns"] == COLUMNS
    listed = {c["subclass"] for c in index["categories"]}
    assert listed == {d.subclass_id for d in dumps}


def test_category_json_carries_the_faults_and_its_context(dumped):
    report, _base, directory, dumps = dumped
    dump = dumps[0]
    with open(os.path.join(directory, dump.json_name), encoding="utf-8") as fh:
        payload = json.load(fh)

    assert payload["format"] == cd.CATEGORY_FORMAT
    assert payload["subclass"] == dump.subclass_id
    expected = cd.faults_for_category(report, dump.subclass_id)
    assert len(payload["faults"]) == len(expected)
    assert ([f["fault_object"] for f in payload["faults"]]
            == [r.fault.fault_object for r in expected])
    # Every fault carries the category id it was filed under.
    assert {f["dotted_class"] for f in payload["faults"]} == {dump.subclass_id}
    # The category's own triage context travels with it.
    assert payload["counts"]["in_category"] == dump.count
    assert payload["verdict"] is not None
    assert payload["clusters"] is not None


def test_fault_objects_are_copied_verbatim(dumped):
    """A rewritten path will not resolve when pasted into an ATPG tool."""
    report, _base, directory, dumps = dumped
    originals = {r.fault.fault_object for r in report.fault_results}
    with open(os.path.join(directory, dumps[0].csv_name),
              newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            assert row["fault_object"] in originals


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
def test_a_report_without_triage_writes_nothing(demo_report, tmp_path):
    class _Bare:
        selected_categories = None
        statistics = None
        fault_results = []

    base = str(tmp_path / "bare.md")
    assert cd.write_category_dumps(_Bare(), base) == []
    assert not os.path.exists(cd.dump_dir_for(base))


def test_slug_never_escapes_the_dump_directory():
    assert cd.safe_slug("AU.TC") == "AU.TC"
    assert "/" not in cd.safe_slug("../../etc/passwd")
    assert cd.safe_slug("../../etc/passwd") == "etc_passwd"
    assert cd.safe_slug("") == "unclassified"


def test_selecting_only_one_format(demo_report, tmp_path):
    base = str(tmp_path / "csv_only.md")
    dumps = cd.write_category_dumps(demo_report, base, formats=("csv",))
    assert all(d.json_name is None and d.csv_name for d in dumps)
    assert all(d.json_href is None for d in dumps)


def test_json_is_capped_but_the_csv_stays_complete(demo_report, tmp_path,
                                                   monkeypatch):
    monkeypatch.setattr(cd, "MAX_JSON_FAULTS", 3)
    base = str(tmp_path / "capped.md")
    directory = cd.dump_dir_for(base)
    dumps = cd.write_category_dumps(demo_report, base)

    big = [d for d in dumps if d.count > 3]
    assert big, "the demo needs a category with more than 3 faults"
    dump = big[0]
    assert dump.truncated

    with open(os.path.join(directory, dump.json_name), encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["truncated"] is True
    assert payload["complete_in"] == dump.csv_name
    assert len(payload["faults"]) == 3

    with open(os.path.join(directory, dump.csv_name),
              newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == dump.count


# ---------------------------------------------------------------------------
# Report wiring
# ---------------------------------------------------------------------------
def test_markdown_links_the_files_only_when_they_exist(dumped):
    report, _base, _directory, dumps = dumped
    without = render_markdown(report)
    assert ".csv)" not in without

    with_links = render_markdown(report, dumps)
    for dump in dumps:
        assert f"]({dump.csv_href})" in with_links
        assert f"]({dump.json_href})" in with_links


def test_write_markdown_emits_the_dumps_beside_the_report(demo_report, tmp_path):
    path = str(tmp_path / "report.md")
    write_markdown(demo_report, path)
    directory = cd.dump_dir_for(path)
    assert os.path.isdir(directory)
    content = open(path, encoding="utf-8").read()
    # The link must be relative, so the report stays portable.
    assert "report_categories/" in content
    assert str(tmp_path) not in content.split("## Coverage Triage", 1)[1]


def test_write_markdown_can_skip_the_dumps(demo_report, tmp_path):
    path = str(tmp_path / "solo.md")
    write_markdown(demo_report, path, dump_categories=False)
    assert os.path.isfile(path)
    assert not os.path.exists(cd.dump_dir_for(path))


def test_html_links_the_files_and_keeps_its_section_numbering(dumped):
    report, _base, _directory, dumps = dumped
    html = build_html_report(report, category_dumps=dumps)
    for dump in dumps:
        assert f'href="{dump.csv_href}"' in html
        assert f'href="{dump.json_href}"' in html
    # No new numbered section was introduced.
    for n in range(1, 10):
        assert f"<h2>{n}." in html
    assert "<h2>10." not in html


def test_html_without_dumps_emits_no_links(demo_report):
    assert "_categories/" not in build_html_report(demo_report)


def test_dumped_report_stays_within_the_honesty_guardrails(dumped):
    report, _base, _directory, _dumps = dumped
    assert audit_report(report) == []


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------
def test_saving_a_session_writes_the_category_files_beside_it(demo_report,
                                                              tmp_path):
    path = str(tmp_path / "session.json")
    save_report(demo_report, path)

    directory = cd.dump_dir_for(path)
    assert os.path.isdir(directory)
    assert os.path.isfile(os.path.join(directory, "index.json"))
    for cat in demo_report.selected_categories:
        slug = cd.safe_slug(cat.subclass_id)
        assert os.path.isfile(os.path.join(directory, f"{slug}.csv"))
        assert os.path.isfile(os.path.join(directory, f"{slug}.json"))


def test_saving_a_session_can_skip_the_category_files(demo_report, tmp_path):
    path = str(tmp_path / "solo_session.json")
    save_report(demo_report, path, dump_categories=False)
    assert os.path.isfile(path)
    assert not os.path.exists(cd.dump_dir_for(path))


def test_a_loaded_session_remembers_its_category_files(demo_report, tmp_path):
    path = str(tmp_path / "session.json")
    save_report(demo_report, path)
    reloaded = load_report(path)

    assert reloaded.category_dumps
    saved = {d.subclass_id: d for d in demo_report.category_dumps}
    for dump in reloaded.category_dumps:
        original = saved[dump.subclass_id]
        assert dump.count == original.count
        assert dump.csv_name == original.csv_name
        assert dump.json_href == original.json_href
        # Recorded relative, so the folder stays movable.
        assert not os.path.isabs(dump.csv_href)


def test_a_reloaded_session_can_regenerate_its_dumps(demo_report, tmp_path):
    """The netlist is gone after a reload; the dumps must still be writable."""
    path = str(tmp_path / "session.json")
    save_report(demo_report, path)
    reloaded = load_report(path)
    assert reloaded.netlist is None

    again = str(tmp_path / "elsewhere" / "copy.json")
    os.makedirs(os.path.dirname(again))
    save_report(reloaded, again)

    before = {d.subclass_id: d.count for d in demo_report.category_dumps}
    after = {d.subclass_id: d.count for d in reloaded.category_dumps}
    assert after == before


def test_an_old_session_without_the_manifest_still_loads(demo_report, tmp_path):
    path = str(tmp_path / "old.json")
    save_report(demo_report, path, dump_categories=False)
    data = json.load(open(path, encoding="utf-8"))
    data.pop("category_dumps", None)
    json.dump(data, open(path, "w", encoding="utf-8"))

    reloaded = load_report(path)
    assert reloaded.category_dumps is None
    assert reloaded.selected_categories


def test_waiving_faults_drops_the_stale_manifest(demo_report, tmp_path):
    """Edited faults invalidate the files, so the edited report claims none."""
    save_report(demo_report, str(tmp_path / "session.json"))
    assert demo_report.category_dumps

    edited = apply_exclusions(demo_report, excluded_subtypes=["AU.TC"])
    assert edited.category_dumps is None


# ---------------------------------------------------------------------------
# The agent tool
# ---------------------------------------------------------------------------
def _run(fault_results, **args):
    return investigate.run_tool(
        "list_category_faults", args, fault_results=fault_results,
        constraints=[], netlist=None)


def test_tool_is_registered():
    assert "list_category_faults" in investigate.TOOL_SPECS
    params = investigate.TOOL_SPECS["list_category_faults"]["params"]
    assert {"subclass", "limit", "offset", "full"} <= set(params)


def test_tool_returns_the_same_faults_as_the_dump(dumped):
    report, _base, _directory, dumps = dumped
    dump = dumps[0]
    result = _run(report.fault_results, subclass=dump.subclass_id, limit=1000)
    expected = cd.faults_for_category(report, dump.subclass_id)
    assert result["total_matched"] == len(expected)
    assert ([f["fault_object"] for f in result["faults"]]
            == [r.fault.fault_object for r in expected])


def test_tool_matches_exactly_not_by_prefix(demo_report):
    """'AU' and 'AU.TC' are different categories, not a substring relation."""
    tc = _run(demo_report.fault_results, subclass="AU.TC", limit=1000)
    assert tc["total_matched"] > 0
    bare = _run(demo_report.fault_results, subclass="AU", limit=1000)
    assert bare["total_matched"] == 0
    assert "AU.TC" in bare["available_subclasses"]


def test_tool_is_case_insensitive(demo_report):
    lower = _run(demo_report.fault_results, subclass="au.tc", limit=1000)
    upper = _run(demo_report.fault_results, subclass="AU.TC", limit=1000)
    assert lower["total_matched"] == upper["total_matched"] > 0


def test_tool_pages_through_a_category(demo_report):
    everything = _run(demo_report.fault_results, subclass="AU.TC", limit=1000)
    total = everything["total_matched"]
    assert total > 4

    first = _run(demo_report.fault_results, subclass="AU.TC", limit=2)
    assert first["returned"] == 2
    assert first["more"] is True
    assert first["next_offset"] == 2

    second = _run(demo_report.fault_results, subclass="AU.TC", limit=2,
                  offset=first["next_offset"])
    objects = ([f["fault_object"] for f in first["faults"]]
               + [f["fault_object"] for f in second["faults"]])
    assert objects == [f["fault_object"]
                       for f in everything["faults"][:4]]


def test_tool_requires_a_subclass(demo_report):
    assert "error" in _run(demo_report.fault_results, subclass="")


def test_tool_survives_the_out_of_process_round_trip(demo_report):
    """The MCP server sees a serialised payload, not the live records.

    Without the dotted class in the payload the tool silently matches nothing,
    which is why this is asserted rather than assumed.
    """
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints, demo_report.netlist)
    rehydrated, _constraints, _adjacency = investigate.rehydrate(evidence)

    live = _run(demo_report.fault_results, subclass="AU.TC", limit=1000)
    remote = _run(rehydrated, subclass="AU.TC", limit=1000)
    assert remote["total_matched"] == live["total_matched"] > 0
    assert ([f["fault_object"] for f in remote["faults"]]
            == [f["fault_object"] for f in live["faults"]])


def test_older_payload_without_the_dotted_class_still_groups(demo_report):
    """An evidence file written before this field existed must still work."""
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints, demo_report.netlist)
    for row in evidence["faults"]:
        row.pop("dotted_class", None)
    rehydrated, _c, _a = investigate.rehydrate(evidence)

    live = _run(demo_report.fault_results, subclass="AU.TC", limit=1000)
    recovered = _run(rehydrated, subclass="AU.TC", limit=1000)
    assert recovered["total_matched"] == live["total_matched"] > 0
