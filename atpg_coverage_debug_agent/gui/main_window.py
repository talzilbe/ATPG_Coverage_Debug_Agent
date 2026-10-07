"""Main application window for the PySide6 GUI — v2 with Skills system."""
from __future__ import annotations

import csv as csv_mod
import html as html_mod
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

from PySide6.QtCore import QByteArray, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDesktopServices,
    QKeySequence,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QStatusBar, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QToolButton, QVBoxLayout,
    QWidget,
)

from ..app import AnalysisInputs, PartitionInputs, _design_name
from ..analysis import investigate, regression, report_edit
from ..config.settings import AppSettings
from ..models import AnalysisReport, FaultAnalysisResult
from ..reporting.csv_report import write_csv
from ..reporting.html_report import build_html_report
from ..reporting.category_dump import dump_dir_for, write_category_dumps
from ..reporting.markdown_report import write_markdown
from ..reporting.session_report import load_report, save_report
from ..skills.manager import SkillManager
from .details_panel import DetailsPanel
from .skills_panel import SkillsPanel
from .agent_panel import AgentPanel
from .custom_skills_panel import CustomSkillsPanel
from .command_palette import FAULT_PREFIX, CommandPalette, PaletteEntry, rank_entries
from .empty_state import EmptyState
from .find_bar import FindBar
from .getting_started import GettingStarted
from .input_check import CHECKS, ERROR, OK, WARN, InputCheck, classify_file
from .preferences import PreferencesDialog
from . import shortcuts, theme
from .toast import Toast
from .triage_panel import TriagePanel
from .visualizer_panel import VisualizerPanel
from .workers import start_worker, start_multi_worker

logger = logging.getLogger(__name__)

_TABLE_HEADERS = [
    "Fault Object", "Class", "Mapped to", "Mapping confidence", "Instance",
    "Cell", "Fan-in", "Fan-out", "Controllability issue",
    "Observability issue", "Constraint touches it", "Scan boundary",
    "Root Cause",
]

#: Fault-class filter entries: (shown text, class code or "all").
_CLASS_FILTER_ITEMS = [
    ("all classes", "all"),
    ("AU — ATPG untestable", "AU"),
    ("UO — unobserved", "UO"),
    ("UC — uncontrolled", "UC"),
]

_TABLE_HEADER_TIPS = [
    "The fault site exactly as the fault list names it (hover a cell for the "
    "full path).",
    "Fault class from the ATPG tool: AU = ATPG untestable, UO = unobserved, "
    "UC = uncontrolled.",
    "The netlist instance the fault was mapped to.",
    "How sure the mapping is: high / medium / low, or unresolved when the "
    "object was not found in the netlist.",
    "Instance name of the mapped cell.",
    "Library cell type of the mapped instance.",
    "Number of cells driving this site ('?' = unknown because unmapped).",
    "Number of cells this site drives ('?' = unknown because unmapped).",
    "Controllability problem found at the site (yes/no).",
    "Observability problem found at the site (yes/no).",
    "A constraint in the dofile touches this site (yes/no).",
    "Whether the site sits on a scan boundary (yes / no / unknown).",
    "The structural root cause this tool derived for the fault.",
]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _html_escape(text) -> str:
    return html_mod.escape(str(text))


def _demo_inputs() -> Optional[tuple]:
    """(netlist, faults, constraints) of the bundled demo, if it is present."""
    base = os.path.join(_REPO_ROOT, "sample_data")
    paths = tuple(os.path.join(base, n) for n in (
        "demo_netlist.v", "demo_faults.mtfi", "demo_constraints.do"))
    return paths if all(os.path.isfile(p) for p in paths) else None


_WELCOME_HTML = """
<body style="font-family: Segoe UI, sans-serif; padding: 30px; color: #333;">
<h2 style="color:#0b5394;">ATPG Coverage Debug &mdash; getting started</h2>
<ol style="font-size: 14px; line-height: 1.7;">
  <li>Pick a <b>Netlist</b> and a <b>Fault list</b> at the top
      (constraints are optional) &mdash; or simply <b>drag the files onto
      this window</b>; each one lands in the right box.</li>
  <li>Click the blue <b>&#9654; Analyze</b> button (<b>Ctrl+R</b>).</li>
  <li>Read this <b>Summary</b> tab: the box at the top shows the coverage and
      where to start.</li>
  <li>Work the <b>Triage &amp; Fix Plan</b> tab, then drill into single
      faults in the <b>Coverage Loss Table</b>.</li>
  <li>Optionally ask the <b>AI Debug Agent</b> for an explanation.</li>
</ol>
<p style="color:#555;">New here? Click <b>Try the demo data</b> above to load
a small example design. <b>Ctrl+Shift+P</b> finds any command by name,
<b>F1</b> opens the User Guide (with every keyboard shortcut), and
<b>View &rarr; Theme</b> switches between Light and Dark.</p>
</body>
"""


_HELP_HTML = """
<html><head><style>
  body { font-family: 'Segoe UI', sans-serif; color: #1f2933; line-height: 1.5;
         padding: 8px 18px; }
  h1 { color: #0b5394; font-size: 22px; margin: 4px 0 2px; }
  h2 { color: #0b5394; font-size: 17px; margin: 20px 0 4px;
       border-bottom: 2px solid #d0e2f2; padding-bottom: 3px; }
  h3 { color: #1a5276; font-size: 14px; margin: 14px 0 3px; }
  p, li { font-size: 13px; }
  code { background: #eef2f7; padding: 1px 5px; border-radius: 3px;
         font-family: Consolas, monospace; font-size: 12px; }
  b.k { color: #0b5394; }
  .tip { background: #e8f5e9; border-left: 4px solid #4CAF50;
         padding: 6px 12px; margin: 8px 0; }
  .warn { background: #fff8e1; border-left: 4px solid #FFC107;
          padding: 6px 12px; margin: 8px 0; }
  .step { margin: 2px 0; }
  table { border-collapse: collapse; margin: 6px 0; }
  td, th { border: 1px solid #cfd8e3; padding: 4px 9px; font-size: 12px;
           text-align: left; vertical-align: top; }
  th { background: #eef2f7; }
</style></head><body>

<h1>ATPG Coverage-Loss Debug Agent &mdash; Help</h1>
<p>This tool analyses <b>structural</b> (non-simulation) ATPG coverage loss. It
maps untestable / unobservable / uncontrollable faults onto your gate-level
netlist, root-causes them, ranks the categories worth debugging, proposes
concrete fixes with runnable commands, and lets an AI agent explain the
results. Everything below is organised in the order you would normally use
it.</p>

<div class="tip"><b>Quick start:</b> 1) pick a <b>Netlist</b> and a
<b>Fault list</b> (constraints optional) &rarr; 2) click the blue
<b>&#9654; Analyze</b> button &rarr; 3) read the box at the top of the
<b>Summary</b> tab, then work the <b>Triage &amp; Fix Plan</b> tab
&rarr; 4) drill into individual faults in the <b>Coverage Loss Table</b>
&rarr; 5) optionally run the <b>AI Debug Agent</b> for an explanation.
First time? <b>Try the demo data</b> (Summary tab, or <b>Help</b> menu) loads
a small example design so you can explore before using your own files.</div>

<h2>Finding your way around &mdash; and getting things back</h2>
<p><b>Guided tour.</b> After your first analysis a yellow bar above the tabs
walks through Summary &rarr; Categories &rarr; Fix Plan &rarr; Coverage Loss
Table &rarr; AI agent (<b>Next &rsaquo;</b> / <b>&lsaquo; Back</b> /
<b>Skip tour</b>). <b>Help &rarr; Show the Guided Tour</b> replays it. Tabs
with nothing to show yet offer <i>Try the demo data</i>, <i>Load a saved
report</i> and <i>Pick my own files</i>. The window size and every divider
position are remembered between sessions.</p>
<p>The window shows only what a first-time user needs. Nothing has been
removed: everything else is one click away, and this table says where.</p>
<table>
<tr><th>If you are looking for&hellip;</th><th>Do this</th></tr>
<tr><td>The input file rows (they fold away after Analyze)</td>
    <td>Click <b class="k">Change inputs &#9662;</b> at the right of the
    one-line design summary. <b class="k">Hide inputs &#9652;</b> folds
    them again.</td></tr>
<tr><td>The partition queue</td>
    <td>Click <b class="k">+ Analyze several partitions&hellip;</b> under the
    input rows (see section&nbsp;1).</td></tr>
<tr><td>Export Markdown / CSV</td>
    <td>The <b class="k">Export &#9662;</b> button.</td></tr>
<tr><td>Compare Report, Edit Report, Clear</td>
    <td>The <b class="k">More &#9662;</b> button.</td></tr>
<tr><td>The <b>Logs / Warnings</b>, <b>Skills</b> and <b>Custom Skills</b>
    tabs</td>
    <td><b>View &rarr; Open Logs / Warnings</b> (or Skills, Custom Skills)
    opens one; <b>View &rarr; Show Advanced Tabs</b> keeps all three on the tab
    bar (remembered next time). The warnings count on the Summary box also
    opens the Logs tab.</td></tr>
<tr><td>Jumping between the main tabs</td>
    <td><b>View &rarr; Go to Tab</b>, or <code>Ctrl+1</code> Summary,
    <code>Ctrl+2</code> Triage &amp; Fix Plan, <code>Ctrl+3</code> Coverage
    Loss Table, <code>Ctrl+4</code> AI Debug Agent, <code>Ctrl+5</code>
    Tessent Visualizer.</td></tr>
<tr><td>The AI agent's connection settings (they fold away after the first
    successful run)</td>
    <td><b class="k">Edit connection &#9662;</b> on the AI Debug Agent tab.
    <b class="k">Done &mdash; hide these settings</b> folds them again.</td></tr>
<tr><td>The <b>Assembled Prompt</b> and <b>Agent Tool Trace</b> panes</td>
    <td><b class="k">Show prompt &amp; tool trace</b> on the AI Debug Agent
    tab. They also open by themselves when <b>Verify</b> or <b>Build Prompt
    Only</b> writes into them.</td></tr>
<tr><td>Build Prompt Only, Copy / Save Prompt, Copy / Save Response,
    Suggest Fixes</td>
    <td>The <b class="k">&#8943; More</b> button next to <b>Verify</b>.</td></tr>
<tr><td>A panel you popped out into its own window</td>
    <td>Close that window, or click <b class="k">Dock back</b>.</td></tr>
<tr><td>The full report in a browser</td>
    <td><b class="k">Open Report in Browser</b> (Summary tab) or the link in
    the Summary box.</td></tr>
<tr><td>This guide</td><td><b>Help &rarr; User Guide</b>, or <code>F1</code>.</td></tr>
</table>
<p>Hover any column header in the tables, or any button, for a one-line
explanation.</p>

<h2>1. Input files (top of the window)</h2>
<table>
<tr><th>Field</th><th>What to load</th></tr>
<tr><td><b class="k">Netlist (.v / .v.gz)</b></td>
    <td>Gate-level Verilog structural netlist. Used to trace fan-in/fan-out,
    map faults to instances, and find scan boundaries.</td></tr>
<tr><td><b class="k">Fault list (.mtfi / .mtfi.gz / flat)</b></td>
    <td>Tessent MTFI fault list or a flat <code>&lt;class&gt; &lt;value&gt;
    &lt;path&gt;</code> list. Dotted subtypes (e.g. <code>AU.NOFAULTS</code>,
    <code>AU.TC</code>) are preserved.</td></tr>
<tr><td><b class="k">Constraints (optional .do)</b></td>
    <td>Tessent constraint / dofile commands (force, disable, clock, reset,
    tie…). Used to attribute constraint-induced coverage loss.</td></tr>
<tr><td><b class="k">Output dir</b></td>
    <td>Default folder for exported Markdown / CSV / JSON reports.</td></tr>
</table>
<p><b>Checked as you type.</b> A mark beside each box says what was found:
<span style="color:#1a7f37">&#10003;</span> recognised (with the format,
compression and size), <span style="color:#a05000">!</span> readable but
unexpected, <span style="color:#c62828">&#10007;</span> missing or unreadable.
Analyze refuses to start while a box is red, and says why.</p>
<p><b class="k">Recent &#9662;</b> (left of the partition toggle) and
<b>File &rarr; Recent Analyses</b> refill every box from a design you analysed
before.</p>
<p><b>Cancel</b> stops a running analysis at its next checkpoint (between
faults, or between steps) and leaves the previous results on screen. The
status bar shows elapsed time and, during the per-fault step, an estimate of
the time left.</p>
<p>Re-running on the <b>same netlist</b> (for example after editing the
constraints) reuses the fault mappings, constant drivers and site profiles of
the earlier run &mdash; none of them depend on the constraints &mdash; so the
re-run is faster. The status bar says how many mappings were reused.</p>
<p>Once a report is shown, these rows fold into one line naming the design and
the files that were analysed, so the results get the screen. Click
<b class="k">Change inputs &#9662;</b> on that line to show them again; a new
<b>Analyze</b> or <b>Load Report</b> folds them back. <b>More &#9662; &rarr;
Clear results</b> brings the full input area back too.</p>

<h3>Analyzing several partitions at once</h3>
<p>You can queue multiple partitions (each its own netlist + fault list +
optional constraints) and analyze them together. The queue is hidden until you
need it: click <b class="k">+ Analyze several partitions&hellip;</b> under the
input rows to show it (<b class="k">&minus; Hide the partition queue</b>
hides it again; a queued partition stays queued).</p>
<ul>
  <li class="step">Set the files above, click <b class="k">Add Partition</b>
      &mdash; it is queued with a name derived from the netlist. Repeat for
      each partition.</li>
  <li class="step"><b class="k">Remove</b> drops the selected queued partition;
      <b class="k">Clear Queue</b> empties the list.</li>
  <li class="step"><b>Analyze</b> then runs every queued partition (one after
      another). If the queue is empty it just analyzes the single file set
      above, exactly as before.</li>
  <li class="step">After the run, an <b class="k">Active partition</b> dropdown
      appears above the tabs. Switching it re-loads that partition's full
      report into every tab &mdash; Summary, Coverage Loss Table, AI agent and
      Edit Report all act on the selected partition, and each keeps its own
      edits.</li>
</ul>

<h3>Auto-saving the report</h3>
<p>Tick <b class="k">Auto-save report</b> (next to the buttons) to have Analyze
save the JSON report for you. It first asks for a name, pre-filled with a
default derived from the netlist &mdash; e.g. <code>my_design.v.gz</code>
&rarr; <code>my_design_atpg_report</code> &mdash; then writes it to the
<b>Output dir</b> (or the netlist's folder if none is set) once the run
finishes. In a multi-partition run each partition is saved under its own
derived name. The saved file is a full report you can re-open with
<b>Load Report</b>.</p>

<h2>2. Action buttons</h2>
<p>One row under the inputs. The less common actions sit behind the two
<b>&#9662;</b> drop-down buttons so the row stays short.</p>
<table>
<tr><th>Button</th><th>Use</th></tr>
<tr><td><b class="k">&#9654; Analyze</b> (blue)</td><td>Parse the inputs and
    build the report. Runs in the background; watch the progress bar and
    status bar. This is where every session starts.</td></tr>
<tr><td><b class="k">Cancel</b></td><td>Abort a running analysis.</td></tr>
<tr><td><b class="k">Add Partition / Remove / Clear Queue</b></td>
    <td>Build a queue of partitions to analyze together (see section 1).</td></tr>
<tr><td><b class="k">Export &#9662;</b> &rarr; Markdown report / CSV table</td>
    <td>Save the report as Markdown &mdash; including the coverage triage,
    hierarchy hotspots, blocking sources and the ranked fix plan &mdash; or
    the coverage-loss table as CSV. Exporting Markdown also writes a
    <code>&lt;name&gt;_categories</code> folder beside it holding every fault
    of each selected category, and links to it. The same two entries are in
    the <b>File</b> menu.</td></tr>
<tr><td><b class="k">Save Report / Load Report</b></td>
    <td>Save the full analysis (including the AI investigation) to JSON and
    reload it later &mdash; no need to re-run Analyze. The per-category fault
    files are written into a folder beside the JSON, so the saved session is
    self-contained and can be handed to someone else. A reloaded session
    remembers which files it wrote.</td></tr>
<tr><td><b class="k">More &#9662;</b> &rarr; Compare with a previous
    report</td>
    <td>Load a previous (baseline) JSON report and diff it against the current
    one: <b>regressed</b> (new loss), <b>fixed</b>, and <b>changed</b> faults.
    You can then ask the AI agent &ldquo;what changed vs the baseline?&rdquo;</td></tr>
<tr><td><b class="k">More &#9662;</b> &rarr; Edit report (waive faults)</td>
    <td>Waive faults and recompute coverage &mdash; see section 4.</td></tr>
<tr><td><b class="k">Auto-save report</b> (checkbox)</td>
    <td>Auto-save the JSON report to the Output dir after Analyze (see
    section 1).</td></tr>
<tr><td><b class="k">More &#9662;</b> &rarr; Clear results</td><td>Reset the
    views to start fresh; the input rows are shown again.</td></tr>
</table>

<h3>Window size &mdash; the View menu</h3>
<p>The window opens maximized. If you need to change that, use <b>View</b>
rather than the title bar:</p>
<table>
<tr><th>Action</th><th>Shortcut</th></tr>
<tr><td><b class="k">Maximize</b></td><td><code>Ctrl+M</code></td></tr>
<tr><td><b class="k">Full Screen</b> (toggle)</td><td><code>F11</code></td></tr>
<tr><td><b class="k">Restore Down</b></td><td><code>Ctrl+Shift+M</code></td></tr>
</table>
<p>These drive the window through Qt directly, so they work even on remote X
sessions whose window manager ignores the title-bar maximize button. The same
<b>View</b> menu also holds <b>Go to Tab</b> (<code>Ctrl+1</code>&hellip;<code>Ctrl+5</code>)
and the advanced tabs (see <i>Finding your way around</i> above).</p>

<h2>3. Result tabs</h2>
<p>Five tabs are always shown: <b>Summary</b>, <b>Triage &amp; Fix Plan</b>,
<b>Coverage Loss Table</b>, <b>AI Debug Agent</b> and <b>Tessent
Visualizer</b>. <b>Logs / Warnings</b>, <b>Skills</b> and <b>Custom
Skills</b> are advanced tabs, hidden until you open them from the
<b>View</b> menu.</p>
<h3>Summary</h3>
<p><b>The box at the top</b> is the place to start. It shows the test and
fault coverage, how many faults lose coverage, how many of those are
<i>actionable</i> (mapped onto the netlist and not tied off), and any
warnings (click the count to read them). Under <b>Where to start</b> it lists
the biggest coverage-loss categories with the first fix the Fix Plan proposes
for each; click a category to open it on the Triage tab. The <b>Next</b>
links jump to the Triage tab, the fault table, the AI agent or the full
report in a browser.</p>
<p>Below the box is the full HTML report: coverage metric, fault-class /
subtype breakdown, top
root causes, module and instance hotspots, and any analyst note. Click
<b>Open Report in Browser</b> for the full-fidelity version (and a shareable
local link).</p>
<p>Section&nbsp;3 of that report, <b>Evidence Quality &amp; Scan Evidence</b>,
is worth reading before anything else. It splits the coverage loss into what
the analysis can and cannot stand behind:</p>
<ul>
  <li class="step"><b>Mapped</b> &mdash; the fault object was located in the
      netlist, so its fan-in, fan-out and scan status were measured.</li>
  <li class="step"><b>Not mapped</b> &mdash; the object was never located.
      Its connectivity is <i>unknown</i>, and the tables show
      <code>NULL</code> and <i>unknown</i> rather than <code>0</code> and
      <i>no</i>. Nothing may be concluded from these rows; the table beneath
      says whether the cause is a missing cell model, a repeated name, or a
      netlist covering a different block.</li>
  <li class="step"><b>Tied to a constant</b> &mdash; the site's driver was
      traced across the hierarchy to a tie cell. A stuck-at fault on a
      constant pin is undetectable by construction, so these are
      <b>expected and non-actionable</b>: waive them instead of debugging
      them. The tie cells holding the most sites are listed.</li>
  <li class="step"><b>Actionable coverage loss</b> &mdash; what is left, and
      the only number a priority ranking should be built on.</li>
</ul>
<p>The scan-status table there is read from each instantiation's pin list: a
cell counts as scan only when a dedicated scan-data input <b>and</b> a
shift-enable pin were both read. <i>unknown</i> means no instantiation was
read &mdash; it is never reported as non-scan.</p>

<h3>Triage &amp; Fix Plan</h3>
<p>The shortest route from &ldquo;there is coverage loss&rdquo; to &ldquo;here is
what to do about it&rdquo;. Three views, deliberately kept apart because they
carry different weight &mdash; see section&nbsp;5 for how each is derived.</p>
<ul>
  <li class="step"><b>Categories</b> &mdash; every coverage-loss class with its
      plain-language meaning (<i>What it means</i>; hover for the full
      explanation), fault count, share of the design, stuck-at split, and a verdict on
      whether it is <i>worth acting on</i> (<code>true</code> /
      <code>partial</code> / <code>false</code>) with a confidence level.
      The <b>Evidence basis</b> column, and the coloured badges in the detail
      pane and on every Fix Plan evidence line, say where each finding comes
      from, strongest first: <b>measured</b> (Tessent's own reports),
      <b>fault list</b>, <b>structural estimate</b> (netlist tracing &mdash;
      confirm in Tessent before expensive work), <b>naming hint</b>
      (recognised by a name only), <b>past re-runs</b> (recorded fix
      outcomes).
      Select a row to see what the subclass means, the reasoning behind the
      verdict, which structure is blocking the faults, and why they were hard
      to test. <b>Export category faults&hellip;</b> writes one CSV and one
      JSON per category into a folder you choose, each holding every fault in
      that category &mdash; the CSV for spreadsheet or pandas triage, the JSON
      for the AI agent. The Markdown and HTML reports link to the same files,
      and the agent can read a category in-band with
      <code>list_category_faults</code> instead.</li>
  <li class="step"><b>Where the loss is</b> &mdash; hierarchy clustering. A
      dominant prefix tells you <i>where</i> to look; it is never a root
      cause. Sample paths are quoted <b>verbatim</b>, so they can be pasted
      into an ATPG tool unmodified &mdash; double-click one to focus that
      fault in the Coverage Loss Table, or use <b>Copy selected path</b>.</li>
  <li class="step"><b>Fix Plan</b> &mdash; ranked proposals, each with its
      rationale, what to confirm first, the supporting evidence, caveats, and
      a copyable command block. This tool never runs the commands, and it
      never predicts a coverage gain: where an action is marked as needing
      measurement, the re-run is what establishes the benefit.</li>
</ul>

<h3>Coverage Loss Table</h3>
<p>One row per coverage-loss fault with its class, mapped instance, mapping
confidence, fan-in/out sizes, controllability / observability / constraint /
scan-boundary flags, and the diagnosed root cause.</p>
<ul>
  <li class="step"><b>Filter</b> by substring, fault class, or mapping
      confidence; <b>Export Filtered CSV</b> saves just the visible rows.</li>
  <li class="step"><b>Click a row</b> to see full per-fault evidence in the
      Details panel on the right.</li>
  <li class="step"><b>Right-click a row</b> to <i>Ask the AI agent about this
      fault</i>, <i>Inspect in Tessent Visualizer</i> (see
      section&nbsp;7), or <i>Exclude selected fault(s)</i> from the
      report.</li>
</ul>

<h3>Logs / Warnings</h3>
<p>Advanced tab (<b>View &rarr; Open Logs / Warnings</b>, or the warnings count
on the Summary box). Parser and skill warnings (unrecognised lines,
unresolved mappings, etc.). Check here first if a result looks
incomplete.</p>

<h3>Skills</h3>
<p>Advanced tab (<b>View &rarr; Open Skills</b>). Toggle and configure the
deterministic analysis skills (coverage hotspots,
constraint impact, fault-cone summary, scan-boundary, DFT/ATPG debug…). Each
card has an enable checkbox and tunable parameters; changes persist.</p>

<h3>Custom Skills</h3>
<p>Advanced tab (<b>View &rarr; Open Custom Skills</b>). Load your own Python
skills from a directory, or write one in the built-in
editor from the provided template, to add project-specific detectors. Loaded
custom skills appear on the Skills tab and become AI agent tools.</p>

<h3>Tessent Visualizer</h3>
<p>Opens the vendor viewer on this same design in one click, so a structural
conclusion can be confirmed in the tool that owns the authoritative answer.
Fully described in section&nbsp;7.</p>

<h2>4. Edit Report &mdash; waiving faults &amp; recomputing coverage</h2>
<p>Open with <b>More &#9662; &rarr; Edit report (waive faults)</b>. Excluded faults are removed from the totals so
the coverage metric rises, while the report <b>layout stays identical</b>.
Edits are reversible (they apply to a pristine base report) and are saved with
the JSON report. You can waive at four levels:</p>
<ul>
  <li class="step"><b>Whole classes</b> &mdash; all <code>AU</code> /
      <code>UO</code> / <code>UC</code> faults.</li>
  <li class="step"><b>Specific subtypes</b> &mdash; tick e.g.
      <code>AU.NOFAULTS</code> or <code>AU.TC</code> (each shown with its fault
      count).</li>
  <li class="step"><b>Individual faults by path</b> &mdash; type object paths,
      one per line.</li>
  <li class="step"><b>Table selection</b> &mdash; right-click selected rows in
      the Coverage Loss Table &rarr; <i>Exclude selected fault(s)</i>.</li>
</ul>
<p>Add an <b>analyst note</b> to record <i>why</i> the waiver is legitimate; it
appears on the report.</p>
<p>Waiving also updates the <b>Triage &amp; Fix Plan</b> tab: a category you
have written off leaves the fix plan, so the remaining proposals only cover
loss you still intend to chase.</p>

<h2>5. How the triage reaches its conclusions</h2>
<p>Everything below is derived from the same three input files &mdash; no ATPG
tool is run. Each conclusion is labelled with how it was obtained, because the
labels differ in how much they can be trusted.</p>

<h3>Fault subclasses &mdash; the strongest signal</h3>
<p>A Tessent fault list records a <i>dotted</i> class such as
<code>AU.TC</code> or <code>UO.AAB</code>. That suffix is the ATPG tool's own
root-cause label, which makes it far more reliable than anything that can be
inferred from the netlist. When the fault list carries no subtype, only the
coarse class is known and the confidence drops accordingly &mdash; you will
see this reported as <code>reduced</code>.</p>

<h3>Coverage figures</h3>
<p>Every fault class is assigned a <b>coverage role</b>, and the role &mdash;
never the class label &mdash; drives the numbers. <code>DS</code> and
<code>DI.*</code> are <code>DT</code> (detected); <code>PT</code> and
<code>PU</code> are <code>PD</code> (possibly detected);
<code>UU</code>, <code>TI</code>, <code>BL</code> and <code>RE</code> are
<code>UD</code> (undetectable, removed from the test-coverage denominator);
<code>AU.*</code> is <code>AU</code>; <code>UO.*</code> and <code>UC.*</code>
are <code>ND</code> (coverage loss).</p>
<pre>test_coverage      = (DT + posdet_credit*PD) / (FU - UD)
fault_coverage     = (DT + posdet_credit*PD) / FU
atpg_effectiveness = (DT + PU + UD + AU) / FU</pre>
<p><code>posdet_credit</code> defaults to <b>0</b>, which is what the ATPG
tool reports: a possibly-detected fault earns no test- or fault-coverage
credit. ATPG effectiveness is different &mdash; it credits <code>PU</code>
(possibly detected, untestable) and <b>not</b> <code>PT</code>, because a
posdet-untestable fault is as resolved as ATPG can make it. Both rules are
configurable (<code>posdet_credit</code> and
<code>effectiveness_posdet_families</code>) and are printed in the report
header.</p>
<p>Counts are printed next to every percentage so any figure can be
re-derived, and a metric that is undefined for this population is shown as
<code>n/a</code> rather than invented. The report also states whether the
fault list declared itself <b>collapsed</b> or <b>uncollapsed</b>, because the
two are not comparable. After parsing, the analyzer checks that
<code>DT + PD + UD + AU + ND</code> equals the number of records parsed and
aborts if it does not.</p>
<p>A class the configuration does not describe is never merged into a
catch-all. It keeps its verbatim token, is listed by name under <b>Input
Quality</b> with sample records, is excluded from every metric, and past a
configurable threshold it fails the run.</p>

<h3>Which snapshot was analysed &mdash; pre- or post-disposition</h3>
<p>A run commonly performs a <b>fault-disposition</b> step after its last ATPG
phase: it reclassifies a block of faults into a waiver subclass and excludes
that subclass from the relevant coverage column
(<code>set_relevant_coverage -exclude</code>). That is why the tool prints two
columns, <b>total</b> and <b>total relevant</b>, and the report reproduces
both.</p>
<p>The disposition does not only move the totals &mdash; it <b>rewrites the AU
subclass distribution</b>, which is exactly what the category ranking and the
fix plan are built from. So the report names the fault list it parsed and
states whether it is <b>pre-disposition</b>, <b>post-disposition</b> or
<b>undetermined</b>. The verdict comes from the file's contents (is the waiver
subclass present?) with the phase tag in the file name breaking ties; a file
name alone never decides it.</p>
<p>If you point the tool at a per-phase snapshot it says so prominently, warns
that the ranking may not reflect the final design state, and names the
post-disposition file sitting beside it. Point <b>Fault list</b> at the
<i>directory</i> instead and the best candidate is chosen for you. The triage
is built from the relevant population, so a waived block never drives the fix
plan.</p>

<h3>Input quality &mdash; what the files actually gave us</h3>
<p>The report separates <i>&ldquo;no constraint affects this fault&rdquo;</i>
from <i>&ldquo;the constraint file could not be fully parsed&rdquo;</i>. The
constraint dofile is read as Tcl commands, not as lines: continuations,
comments, <code>set</code> variables, <code>dofile</code> includes,
<code>if</code>/<code>else</code> bodies and <code>[get_pins&nbsp;-hier&nbsp;...]</code>
collections are all handled. A directive that cannot be evaluated is recorded
as <b>unresolved</b> with its source line and counted &mdash; while that count
is non-zero, a fault reported with no constraint hit is <b>not</b> proven
unconstrained.</p>

<h3>Configuration &mdash; onboarding a new partition</h3>
<p>Nothing design-specific is written into the tool. The fault-class role map,
scan / scan-out / shift-enable / clock pin names, tie-cell and dangling-net
naming, the possibly-detected credit factor and every input-quality threshold
live in a JSON file named by the <code>ATPG_ANALYSIS_CONFIG</code> environment
variable. Values are merged into the documented defaults, so a partition only
lists what differs. The configuration actually used is recorded in the report,
which is what lets you tell <i>&ldquo;this library has no scan cells&rdquo;</i>
from <i>&ldquo;we were never told what this library calls its scan
pins&rdquo;</i>. See the README for the full key list and defaults.</p>
<p>Scan-ness, sequential-ness and tie-ness are always decided from pin lists
and resolved connectivity, never from instance or cell-type names; the
vocabularies only say what a pin is <i>called</i>. A scan/non-scan boundary is
claimed only when the neighbour is <b>sequential</b> &mdash; every
combinational cell is trivially non-scan, so counting those as boundaries
would fire on almost every fault. A scannable cell whose scan-in or scan-out
is dangling gets its own category,
<code>scan_capable_but_not_chain_connected</code>, because it needs
re-stitching rather than a wrapper.</p>

<h3>Hierarchy clustering &mdash; where, not why</h3>
<p>Faults are grouped by hierarchy prefix. The depth is chosen automatically:
the tool descends while the largest cluster still holds a meaningful share of
the faults, and stops before the grouping fragments into a long tail. A
dominant prefix is a <b>pointer</b>, not a diagnosis.</p>

<h3>Scored verdicts</h3>
<p>Four fixed measurements decide whether a category is worth acting on:</p>
<ul>
  <li class="step"><b>Concentration</b> &mdash; how much of the loss sits in
      the top few clusters. High means there is a focal point.</li>
  <li class="step"><b>Symmetry</b> &mdash; how evenly the top clusters are
      sized. Even sizing usually means a replicated structure rather than one
      broken block.</li>
  <li class="step"><b>Stuck-at asymmetry</b> &mdash; a strong skew towards
      sa0 or sa1 points at a value held fixed upstream.</li>
  <li class="step"><b>Depth</b> &mdash; how deep the loss had to be traced
      before it localised.</li>
</ul>
<p>Categories below a minimum size are <b>not</b> scored at all: a single
fault is trivially 100% concentrated and 100% skewed, and reading signal into
that would manufacture evidence. Such categories are tagged
<code>low_population</code>.</p>

<h3>What is blocking the faults</h3>
<p>For <code>AU.TC</code> and <code>AU.PC</code> the fan-in cone is traced to
find the structure responsible, because the answer changes what you do:</p>
<ul>
  <li class="step"><b>Test data register</b> &mdash; configurable per run, so a
      topoff with the opposite value is the cheap fix.</li>
  <li class="step"><b>Hardwired tie</b> or <b>unscanned flop</b> &mdash;
      cannot be overridden from the ATPG side; needs a design change.</li>
  <li class="step"><b>A few named pins at fixed values</b> &mdash; configured
      loss; waive it, or recover it with a topoff if the owner agrees.</li>
  <li class="step"><b>A broad or masked blocking set</b> &mdash; usually means
      the faults are covered by another partition's patterns rather than
      lost.</li>
</ul>

<h3>Why aborted faults were hard to test</h3>
<p>Aborted faults (<code>UC.AAB</code>, <code>UO.AAB</code>) were not proven
untestable &mdash; the search ran out of budget. The netlist is measured to
estimate which obstacle it hit: <b>low controllability</b>, a <b>hard
observability gap</b>, an <b>observability bottleneck</b>, <b>reconvergent
complexity</b>, or a <b>sequential depth explosion</b>. This distinction
matters: a bottleneck is often fixed by raising the abort limit, while
reconvergent complexity needs a design bypass and more abort budget is wasted
runtime.</p>
<p>A site <i>inside</i> a reconvergent cone sees only one narrow path, which
used to read as a bottleneck. The analysis now also checks the side inputs of
that path: when two or more of its gates take a side input from the same
fan-out stem that feeds the site (up to three levels above it), the site is
reported as <b>reconvergent complexity</b> instead. <code>AU.SEQ</code> sites
are profiled the same way, for their sequential depth.</p>
<p>When an <code>analyze_fault</code> log is supplied (section&nbsp;1), the
tool's own fields &mdash; activation, observation points, observe depth &mdash;
are classified with the same rules. Where a category has samples, the
<b>measured</b> verdict replaces the estimate when the fixes are chosen, and
the note says whether it confirmed or overrode it.</p>

<h3>Memories, unscanned state and black boxes</h3>
<p><code>AU.SEQ</code> faults are traced to the memories, latches and
unscanned flops around the site: next to a memory the plan starts with the
RAM DRC check (A14/A15/A16), otherwise with an observation point that cuts
the chain. <code>AU.BB</code> faults are traced to the undefined macros
around them (a leaf with many pins, or a memory/PLL/PHY-like name &mdash; the
latter is only a naming hint), ranked by fault count, before
<code>report_black_boxes</code> confirms them.</p>

<h3>Learning from re-runs</h3>
<p><b>More &#9662; &rarr; Compare with a previous report&hellip;</b> now also
shows, for every fix the baseline proposed, how its category changed: faults
before and after, how many left the coverage loss and how many moved to
another category. Tick the fixes you actually applied and press <b>Save ticked
outcomes to fix history</b>. The history (in
<code>~/.atpg_debug_agent/fix_history.json</code>) feeds later plans: a fix
that recovered at least half its category goes first, one that repeatedly
recovered nothing goes last, and its evidence shows a <b>past re-runs</b>
line. Only measured before/after counts are stored.</p>

<h3>Honesty guardrails</h3>
<p>Two checks run over everything the tool generates, and over the AI agent's
answers:</p>
<ul>
  <li class="step"><b>Copy-exact paths</b> &mdash; every hierarchy path must
      appear in the fault list, constraint file or netlist, or be a
      component-aligned prefix of one. Shortened paths (with
      &ldquo;&hellip;&rdquo;) and reconstructed ones are flagged, because they
      will not resolve when pasted into a tool.</li>
  <li class="step"><b>No unmeasured claims</b> &mdash; a coverage gain can only
      be established by re-running ATPG. Predicted percentages are flagged
      rather than presented as evidence.</li>
</ul>
<p>Violations appear in <b>Logs / Warnings</b>, and beneath an agent answer as
a <i>Guardrail check</i> note. When an agent answer trips either check, the
model is first asked <b>once</b> to correct it &mdash; annotating a fabricated
path still leaves the fabricated path in front of you, and you may act on it.
Anything still flagged after that correction is shown with the answer. Both the
first answer and every follow-up reply are checked.</p>

<h3>What this analysis cannot do</h3>
<p>It is a structural analyser, not a simulator. Cone tracing cannot reason
about Boolean satisfiability, multi-driver resolution or mode-dependent gating
the way ATPG does, so the blocking sources and site profiles are
<b>estimates</b>. Confirm them in a real tool session before acting on
anything expensive.</p>

<h2>6. AI Debug Agent &mdash; usage guide</h2>
<p>The agent explains coverage loss using an evidence-driven ATPG/DFT prompt.
It reads only the deterministic report, so it cannot invent faults. Run an
Analyze first, then open the <b>AI Debug Agent</b> tab.</p>

<h3>Step 1 &mdash; choose a backend (LLM Backend box)</h3>
<p>Fill this box in once. After the first successful run it folds into a
single <i>Connection: &hellip;</i> line; click <b class="k">Edit connection
&#9662;</b> to change it, or <b class="k">Done &mdash; hide these
settings</b> to fold it yourself. The choice is remembered.</p>
<ul>
  <li class="step"><b>GitHub Copilot CLI (local subprocess)</b> &mdash; the
      default. Uses the bundled <code>copilot</code> CLI; data stays in the
      CLI's authenticated channel. Set the <b>CLI model</b>
      (<code>auto</code> lets Copilot choose). The model list is <b>not</b>
      hard-coded: on every launch the GUI asks the CLI which models your
      account can use and refills the drop-down, so newly released models
      appear on their own. The last answer is cached per user, so the list is
      populated instantly at start and updated a moment later; press
      <b>Refresh</b> next to the box to re-read it on demand. The box stays
      editable, so a model id the CLI has not advertised can still be typed
      in.</li>
  <li class="step"><b>OpenAI-compatible HTTP endpoint</b> &mdash; point at an
      internal endpoint with a Base URL, Model id, and API key (kept in memory
      only, never written to disk).</li>
</ul>

<h3>Step 2 &mdash; authenticate (CLI backend only)</h3>
<p>On the agent's <b>Authentication</b> sub-tab, use <b>either</b>:</p>
<ul>
  <li class="step"><b>Option A &mdash; GitHub token:</b> paste a fine-grained
      PAT with the <i>Copilot Requests</i> permission (or an OAuth token).
      Classic <code>ghp_</code> tokens are not supported. Leave
      <b>Remember this token for my account on this machine</b> ticked and the
      token is re-filled automatically on every later launch &mdash; it is kept
      in the system keyring, or in an owner-only
      <code>~/.atpg_debug_agent/credentials.json</code> under <i>your</i> home
      directory. The store is per user, not per working directory: several
      people can run this GUI on the same host from different directories and
      each sees only their own token. It is never written to
      <code>settings.json</code> and never into a saved report. Use
      <b>Forget saved token</b> to delete it.</li>
  <li class="step"><b>Option B &mdash; device login:</b> click <i>Sign in with
      device code</i>, open the shown URL and enter the code.</li>
</ul>
<div class="warn">On a headless host with no keychain, the browser login can
succeed but fail to <b>save</b> the token &mdash; use Option A there. Use
<b>Check authentication</b> to confirm you are signed in.</div>
<p>The <b>readiness line</b> at the top of the agent tab shows, at a glance,
whether the CLI was found, whether you are signed in, which model is used and
whether a report is loaded. Its button jumps straight to the first thing
that is missing (<i>Find the CLI&hellip;</i>, <i>Check sign-in</i>,
<i>Sign in&hellip;</i>). It also shows whether a <b>Tessent session</b> is
open: it is optional, but with one the agent can <b>measure</b> instead of
estimate (<code>report_statistics</code> / <code>analyze_fault</code> on
sampled faults). When you press Run in Deep investigation without a live
session, the agent offers to open the <b>Tessent Visualizer</b> tab first
(tick <i>Don't ask again</i> to stop the reminder).</p>

<h3>Step 3 &mdash; pick a mode</h3>
<p>One <b class="k">Mode</b> drop-down above the Run button:</p>
<ul>
  <li class="step"><b>Quick summary</b> (formerly <i>Quick diagnosis</i>):
      one LLM call. The enabled skills run
      locally and their findings are folded into a single prompt. One answer,
      no way to fetch more evidence.</li>
  <li class="step"><b>Deep investigation</b> (formerly <i>Investigate</i>;
      recommended, the default): the model
      itself decides which
      investigative tools to call and iterates. For the CLI backend this is
      driven through a local <b>MCP</b> server, switched on automatically by
      this mode. The HTTP backend needs an endpoint that supports
      tool/function calling. The available tools are:
      <table>
      <tr><th>Tool</th><th>Answers</th></tr>
      <tr><td><code>coverage_triage</code></td><td>Which categories are losing
          coverage, and which were selected to debug.</td></tr>
      <tr><td><code>explain_subclass</code></td><td>What a class such as
          <code>AU.TC</code> means, its usual causes and its fixes. Needs no
          analysis loaded.</td></tr>
      <tr><td><code>list_clusters</code></td><td>Where in the hierarchy each
          category concentrates, with verbatim samples.</td></tr>
      <tr><td><code>list_category_faults</code></td><td>Every fault behind one
          category&rsquo;s count, by its dotted id
          (<code>AU.TC</code>, <code>UO.AAB</code>, &hellip;). Reads a whole
          triage bucket without guessing at an instance substring. Matching is
          exact, so <code>AU</code> and <code>AU.TC</code> are different
          categories; page through a large one with <code>offset</code>.
          </td></tr>
      <tr><td><code>list_blocking_sources</code></td><td>Which constant driver
          or constrained signal is blocking the faults.</td></tr>
      <tr><td><code>profile_fault_sites</code></td><td>Why aborted faults were
          structurally hard to test.</td></tr>
      <tr><td><code>recommend_fixes</code></td><td>Ranked, evidence-backed fix
          proposals with commands.</td></tr>
      <tr><td><code>diagnose_unresolved</code></td><td>Why fault objects failed
          to map onto the netlist &mdash; a missing cell model, a repeated
          name the path did not narrow down, or a netlist covering a
          different block. Unmapped faults have <i>unknown</i> connectivity,
          not zero, so nothing may be concluded from them until this is
          resolved.</td></tr>
      <tr><td><code>scan_status</code></td><td>Whether an instance is a scan
          cell, decided <i>only</i> by reading its actual netlist
          instantiation: it returns the verbatim pin list plus the scan-in,
          shift-enable and scan-out pins found. With no netlist loaded, or
          when the object does not map, it answers
          <i>&ldquo;Unresolved &mdash; scan status cannot be determined
          without netlist pin evidence.&rdquo;</i> rather than guessing.
          Fan-in/fan-out counts and the scan-boundary column never decide
          this.</td></tr>
      <tr><td><code>verify_paths</code></td><td>Whether a path is safe to quote
          before putting it in an answer.</td></tr>
      <tr><td><code>report_context</code></td><td>Called first. Returns the
          <b>complete fault census</b> &mdash; every class, grouped by
          coverage role, with the sum check already done &mdash; plus the
          state of the evidence itself: how much of the loss actually mapped
          onto the netlist (and why the rest did not), the scan-status split,
          how much sits on hard constants, the coverage metrics with their
          formulas, the repeated patterns, the parser warnings, and any
          waivers you have applied. The census is never abridged here, so
          once the agent has called it, no fault can be left unaccounted
          for.</td></tr>
      <tr><td><code>report_insufficient_evidence</code></td><td>Lets the agent
          declare that the evidence does <i>not</i> settle a question, and say
          what would. This exists so &ldquo;not determined&rdquo; is a real
          action it can take rather than something it has to argue its way
          into against the pull of sounding helpful &mdash; a confident wrong
          root cause costs far more than an honest gap. It is reserved for
          evidence that does not exist, not for evidence the agent has not
          fetched yet.</td></tr>
      <tr><td><code>report_handoff_gap</code></td><td>Lets the agent report
          that the <i>numbers it was handed contradict each other</i> &mdash;
          a class list that does not sum to its stated total, or two sections
          disagreeing. Without this channel the path of least resistance is
          to close the arithmetic privately and invent a category to hold the
          difference, which reads exactly like a real fault bucket to the
          next person. The correct action is to report the inconsistency and
          stop, and this makes that possible.</td></tr>
      <tr><td><code>visualizer_commands</code></td><td>Returns the exact
          command chain that reopens this design in Tessent Visualizer,
          optionally with the inspection commands for one fault. The agent
          quotes them; it never runs them. Available only once the
          <b class="k">Tessent Visualizer</b> tab has been filled in &mdash;
          otherwise it says so rather than inventing a project name or a
          path.</td></tr>
      <tr><td><code>tessent_session_status</code></td><td>Copilot CLI backend
          only. Tells the agent whether a live Tessent Visualizer session is
          open in this GUI, whether its profile accepts agent commands, and
          which design inputs it was launched with. Needs no approval.</td></tr>
      <tr><td><code>tessent_run</code></td><td>Copilot CLI backend only. The
          agent asks to run a Tcl script in your <i>live</i> Tessent session.
          Nothing runs until you press <b class="k">Approve &amp; Run</b> in
          the box under the chat; you may edit the script first or
          <b class="k">Reject</b> it. The agent gets the return value and the
          transcript the tool printed, and is told if you edited the
          script.</td></tr>
      <tr><td><code>tessent_collect_evidence</code></td><td>Copilot CLI
          backend only. Measures in your <i>live</i> session what the offline
          analysis only estimated: one script &mdash; shown to you for
          approval like any <code>tessent_run</code> &mdash; runs
          <code>report_statistics -detailed_analysis</code> and
          <code>analyze_fault</code> on a few spread-out faults of each
          selected category. The output is parsed and folded into the report
          exactly as if you had supplied <b>Tessent reports</b> as an input:
          the Summary shows Tessent's coverage, Triage shows <i>measured</i>
          badges, and measured verdicts replace estimates in the Fix Plan.
          So you need no report file &mdash; open the Visualizer session and
          let the agent collect it.</td></tr>
      <tr><td><code>list_open_questions</code></td><td>Where the <i>offline</i>
          analysis itself is weakest, as an ordered list of questions each
          naming the tool that would settle it: categories scored with
          reduced confidence, blockers only partly traced, structurally mixed
          categories, truncated cones, an unreconciled census, a
          pre-disposition snapshot, and every place this tool's root cause
          contradicts the ATPG tool's own subclass. The agent is told to call
          it before choosing what to investigate, so its budget goes where
          the analysis is least sure rather than where it is most sure. The
          same list appears in the reports under <b>Open Questions</b>.
          </td></tr>
      <tr><td><code>classification_crosscheck</code></td><td>Compares the ATPG
          tool's fault subclass (<code>AU.TC</code>, <code>AU.PC</code>,
          <code>UO.AAB</code>&hellip;) with the structural root cause this
          tool derived for the same fault, over every mapped fault. Each pair
          gets a verdict: <i>agree</i>, <i>disagree</i> (the two contradict
          &mdash; one is wrong or the structure is not modelled),
          <i>unconfirmed</i> (the ATPG tool names a mechanism this tool could
          not find at the site) or <i>uninformative</i>. Disagreeing and
          unconfirmed pairs come with verbatim sample faults as leads. It
          never decides which side is right.</td></tr>
      <tr><td><code>record_finding</code></td><td>Lets the agent record a
          <b>structured</b> finding &mdash; a correction, a confirmation, a
          new lead or a gap &mdash; against a fault, a category or a report
          section, with the evidence that supports it. Findings are shown in
          the trace pane, saved with the report, and listed in the exported
          reports under <b>Agent review</b>. They never overwrite an offline
          value; they sit beside it, attributed to the agent, so both are
          always visible.</td></tr>
      <tr><td><code>propose_fix</code></td><td>Lets the agent put its fix-plan
          review <b>into the Fix Plan</b> (Triage tab and report section 5)
          rather than only into prose: <i>amend</i> attaches a practical note
          to an offline entry (the offline text stays verbatim), <i>add</i>
          appends the agent's own proposal, and <i>replace</i> puts a better
          fix in an offline entry's slot &mdash; the offline entry is kept,
          demoted to the end and marked <i>superseded</i>, so the
          deterministic plan is always still readable. Proposals are held to
          the same rules as the offline catalogue: no path that is not in
          your inputs, no elided path, no predicted coverage gain, and
          evidence for anything added or replaced. Commands are text for you
          to run; the tool runs nothing.</td></tr>
      <tr><td><code>list_faults</code>, <code>get_fault_detail</code>,
          <code>why_blocked</code>, <code>list_constraints</code>,
          <code>trace_path</code>, <code>suggest_test_points</code></td>
          <td>Per-fault drill-down, constraints and structural path
          tracing. <code>list_faults</code> filters by one
          <code>issue</code> (controllability, observability, constraint or
          scan_boundary); fault rows are compact unless the agent asks for
          <code>detail=full</code>.</td></tr>
      <tr><td><code>skill_findings</code></td><td>The results of the analysis
          skills you enabled on the <b>Skills</b> tab (constraint impact,
          hotspots, cone summary, scan boundary&hellip;), which ran with the
          analysis. Each skill card on that tab says how it reaches the
          agent.</td></tr>
      <tr><td><code>read_guidance</code></td><td>Your guidance documents
          &mdash; Markdown skills on the <b>Custom Skills</b> tab and the
          dft-atpg-debug methodology &mdash; read one section at a time
          instead of pasted whole. A Markdown skill may open with a
          <code>---</code> block holding <code>name:</code> and
          <code>description:</code>; the description is shown in the Skills
          tab. Guidance tells the agent where to look; it is never treated as
          evidence.</td></tr>
      <tr><td><code>regression</code></td><td>After <b>Compare Report</b>:
          <code>mode=summary</code> for the counts, or
          <code>regressed</code> / <code>fixed</code> / <code>changed</code>
          for the faults.</td></tr>
      <tr><td><code>read_spill</code></td><td>When a tool answer was too
          large and got shortened, it says so and names a file holding the
          complete answer; this reads that file page by page, so the agent
          never has to conclude from a shortened list.</td></tr>
      </table>
      <p>Every list the tools return says how many items there are in total
      and, when it is one page of a longer list, how to fetch the next page
      (<code>more</code> / <code>next_offset</code>). A shortened list is
      always marked as such.</p></li>
</ul>

<h3>Step 4 &mdash; run &amp; review</h3>
<p>The row under the Mode drop-down holds <b>Run</b>, <b>Stop</b>,
<b>Verify</b>, a <b class="k">&#8943; More</b> menu and
<b class="k">Show prompt &amp; tool trace</b>. The two expert panes &mdash;
<b>Assembled Prompt</b> and <b>Agent Tool Trace</b> &mdash; are hidden at
first so the answer and the chat get the room; that button shows them, and
they open by themselves when Verify or Build Prompt Only writes into
them.</p>
<table>
<tr><th>Button</th><th>Use</th></tr>
<tr><td><b class="k">Run AI Debug Agent</b> / <b class="k">Run Agentic
    Agent</b></td><td>Generate the A&ndash;F
    diagnosis. Output streams into the Agent Response pane and is then laid
    out for reading (see <i>Reading the answer</i> below); fault ids are
    clickable and focus the row in the table.</td></tr>
<tr><td><b class="k">Stop</b></td><td>Stop the turn in progress (a run or a
    follow-up reply). Whatever streamed so far is kept and marked
    <i>partial</i>; the conversation, its session and its tools survive, so
    you can ask a follow-up or run again. A second Stop sits in the chat row
    so it is reachable from a popped-out chat window.</td></tr>
<tr><td><b class="k">&#8943; More &rarr; Build Prompt Only</b></td><td>Preview exactly what would be
    sent to the LLM, without calling it.</td></tr>
<tr><td><b class="k">Verify</b></td><td>Cross-check the answer against the
    report &mdash; confirms every referenced fault exists and flags invented
    paths. The result appears in the Agent Tool Trace pane.</td></tr>
<tr><td><b class="k">&#8943; More &rarr; Suggest Fixes</b></td><td>Deterministically rank faults by
    impact and propose concrete DFT fixes (observation/control points,
    constraint relaxation, scan insertion). No LLM used. For fixes tied to a
    specific fault <i>category</i>, use the <b>Fix Plan</b> view on the
    Triage tab instead.</td></tr>
<tr><td><b class="k">&#8943; More</b> &rarr; Copy / Save Prompt &amp;
    Response</td><td>Export the prompt or the
    agent's answer.</td></tr>
<tr><td><b class="k">Show prompt &amp; tool trace</b></td><td>Show or hide
    the Assembled Prompt and Agent Tool Trace panes. Remembered next
    time.</td></tr>
</table>
<p>Every answer is also checked automatically against the guardrails described
in section&nbsp;5 &mdash; the first answer and every follow-up alike. If the
model shortens a hierarchy path, quotes one that is not in your inputs, or
predicts a coverage gain, it is asked once to correct the answer; anything
still unsupported afterwards appears as a <b>Guardrail check</b> note beneath
it. Treat anything listed there as unverified.</p>
<p><b>Deep investigation mode is what gives the agent its tools.</b> In <b>Quick
summary</b> the enabled skills
execute once locally, their findings are folded into a single prompt, and the
model gets one pass with no way to ask for anything further. Choose
<b>Deep investigation</b> for a real investigation loop &mdash; and for follow-up
questions that can still call tools (and, with the Copilot CLI, Tessent
commands).</p>
<p><b>What the agent will and will not say.</b> It is told that you are already
looking at the report, so it does not restate fault counts, hotspots, the
per-fault table or the fix plan &mdash; those are computed exactly and
repeating them would only add transcription risk. Its answer is confined to
what the deterministic pass cannot do: <b>A</b> a verdict on which mechanism
dominates the <i>actionable</i> loss, <b>B</b> evidence gaps not already
quantified in &sect;3, <b>C</b> corrections where its reading differs from the
computed root cause, <b>D</b> patterns that span categories, <b>E</b> detailed
narratives for the few findings that matter, and <b>F</b> a review of the fix
plan rather than a second one. A section with nothing to say is left out.</p>
<p><b>Reading the answer.</b> When the answer has finished streaming, the
Agent Response pane lays it out in layers so it can be read in a minute and
audited when needed:</p>
<ul>
  <li>a <b>summary card</b> at the top: the <b>Verdict</b> with a confidence
      badge, at most three <b>Next actions</b>, and a line counting the
      findings, corrections, evidence gaps and plan-review items &mdash; click
      a count to jump to it. Sections with nothing to report are named there
      instead of taking space. Any <b>Guardrail check</b> warning also shows
      in the card;</li>
  <li>below it, <b>one line per section</b>. Click <b>&#9656;</b> to unfold
      it, <b>&#9662;</b> to fold it again. Findings open as one headline each
      (the first is already unfolded), and every finding keeps its
      <b>Evidence</b> &mdash; the instantiation lines, corroboration steps
      and tool results it rests on &mdash; folded underneath;</li>
  <li><b class="k">Expand all</b> / <b class="k">Collapse all</b> at the top
      right, and <b class="k">Show plain text</b> to see the answer exactly
      as the model wrote it (<b class="k">Show formatted view</b> goes
      back).</li>
</ul>
<p>Folding only changes what is on screen: the model is told to keep doing
every check and to put the depth into the Evidence blocks, and Save chat,
Copy/Save Response and Verify always use the complete answer.</p>

<h3>Step 5 &mdash; follow-up chat</h3>
<p>After a run, use <b>Follow-up Chat</b> to ask questions about the diagnosis;
the conversation keeps the full analysis context (e.g. &ldquo;which module
contributes the most loss?&rdquo;, &ldquo;how would a control point on X
help?&rdquo;). The diagnosis itself stays in the Agent Response pane rather
than being repeated in the chat, and follow-up answers are short and direct
&mdash; the answer first, then only the evidence for it &mdash; unless you ask
for the full report. <b>Max tokens</b> and <b>Temperature</b> tune size and
determinism (temperature&nbsp;0 is most repeatable).</p>
<p><b>Keeping the conversation.</b> <b class="k">Save chat&hellip;</b> writes
the whole session as Markdown &mdash; the initial diagnosis and every
follow-up turn, including turns removed from view by <b class="k">Clear
chat</b>, which only clears the display. <b>Right-click</b> a hierarchy path
in a reply to <b class="k">Open</b> it <b class="k">in Tessent
Visualizer</b> (see section&nbsp;7) or copy it.</p>
<p><b>Follow-ups keep the tools.</b> After an agentic run the investigation
tools stay attached for every follow-up question: with the Copilot CLI the
local MCP server (and the parsed netlist handed to it) lives for the whole
conversation, and with the HTTP backend each follow-up runs through the same
tool loop as the first answer. The status line says whether a follow-up has
tools or can only recall the first answer. Every follow-up answer gets the
same guardrail correction as the first.</p>
<p><b>Seeing what the agent looked at.</b> In agentic CLI mode every tool call
the model makes is logged by the MCP server and appears in the <b>Agent Tool
Trace</b> pane as it happens (<i>MCP tool call #n: name(args)</i>), so you can
tell an answer grounded in fetched evidence from one recalled from the
prompt.</p>
<p><b>Popped-out chat.</b> The chat box's <b>Open in window</b> button detaches
it into its own window; the &ldquo;agent is replying&rdquo; indicator is
mirrored into that window so a slow reply is not mistaken for a hang.</p>

<h3>Fault rows in prompt</h3>
<p>This caps how many coverage-loss fault rows are written into the prompt's
per-fault table. Each row costs roughly 26&nbsp;tokens, so a large partition
cannot be sent whole &mdash; 80,000&nbsp;faults would be over 2&nbsp;million
tokens, far past any model's context window. The label next to the spinner
tells you when rows are being left out.</p>
<p>It caps the <i>table only</i>. The summary, the evidence basis and the whole
triage &mdash; categories, hotspots, blocking sources, fix plan &mdash; are
computed over <b>every</b> fault and are always sent in full, so the agent
always reasons about the complete population. In <b>Deep investigation</b> mode it can
also pull any individual fault on demand with the <code>list_faults</code> and
<code>get_fault_detail</code> tools, so the cap hides nothing from it. Raise it
when you want the model to eyeball raw rows for a pattern the clustering
missed; lower it for a small-context endpoint or to cut cost.</p>

<h3>Bigger, easier-to-read panels &mdash; pop-out windows</h3>
<p>Each of the four panels &mdash; <b>Assembled Prompt</b>, <b>Agent Tool
Trace</b>, <b>Agent Response</b> and <b>Follow-up Chat</b> &mdash; has an
<b class="k">&#10530; Open in window</b> button in its top-right corner (the
first two are visible once <b>Show prompt &amp; tool trace</b> is on). Click it
to detach that panel into a large, resizable window that is easier to read and
type in. Live streaming, clickable fault ids, and the chat input all keep
working in the pop-out. Close the window (or click <b>Dock back</b>) to return
the panel to its place.</p>
<p>A pop-out sizes like any ordinary window. Use
<b class="k">&#11036; Maximize</b> (<b>Ctrl+M</b>) to fill the screen keeping the
title bar, or <b class="k">&#9974; Full screen</b> (<b>F11</b>) to use the whole
screen without one; <b>Esc</b> leaves full screen. These buttons drive the
toolkit directly, so they work even where the window manager ignores the
title-bar buttons &mdash; the same reason the main window has a <b>View</b>
menu (see section&nbsp;2).</p>

<h3>Knowing the agent is still working</h3>
<p>A model call can run for minutes with nothing to show while it thinks, which
looks the same as a hang. The status line under the buttons animates and counts
up &mdash; for example <code>/&nbsp;&nbsp;Agent running, calling tools&hellip;
1m&nbsp;24s</code> &mdash; for as long as a run or a chat reply is in flight,
and reports the total time when the answer arrives.</p>

<div class="tip"><b>Recommended flow:</b> Analyze &rarr; read the Summary box
&rarr; work the <b>Triage &amp; Fix Plan</b> tab &rarr; run the agent in
<b>Deep investigation</b> mode
&rarr; <b>Verify</b> the answer &rarr; ask follow-ups &rarr; waive legitimate
faults via <b>More &#9662; &rarr; Edit report</b> &rarr; <b>Save Report</b>.</div>

<h2>7. Tessent Visualizer &mdash; opening the real tool</h2>
<p>Everything this application concludes is <i>structural</i>: it is read off
the netlist, the fault list and the constraint file, without simulation. The
vendor tool owns the authoritative answer. The <b class="k">Tessent
Visualizer</b> tab exists to close that gap in one click &mdash; it opens the
viewer on the <i>same</i> design, so a finding can be confirmed rather than
trusted.</p>

<h3>What it does</h3>
<p>It builds the whole chain for you: enter the project setup, set the licence
environment, start the tool shell, set the context, read the ICL, the flat
model and the fault list, and open the viewer. The chain runs in its own
terminal window and is <b>detached</b>, so the session keeps running if you
close this application &mdash; and the tool prompt stays usable after the
viewer appears, for anything you want to type yourself.</p>

<h3>Filling it in</h3>
<table>
<tr><th>Field</th><th>Meaning</th></tr>
<tr><td><b class="k">Profile</b></td><td>The project. Profiles are JSON files
    in the <code>profiles/</code> directory, so adding a project is adding a
    file &mdash; no new version of this application. <b class="k">Load
    profile&hellip;</b> takes a profile JSON from anywhere on disk, checks it,
    copies it into <code>~/.atpg_debug_agent/profiles</code> so it is found
    again next time, and selects it. <code>$ATPG_TOOL_PROFILES</code> can also
    point at your own directory to add or override one.</td></tr>
<tr><td><b class="k">Project</b>, <b class="k">Config</b></td><td>Passed to the
    setup wrapper. Pre-filled from the profile; override per run.</td></tr>
<tr><td><b class="k">Workarea</b></td><td>Optional. Leave it blank and the
    setup wrapper uses its own working directory.</td></tr>
<tr><td><b class="k">Licence server</b></td><td>Pre-filled for everyone with the
    site licence list shipped in the profile, so nobody has to know or type
    it. Change it only if told to; <b class="k">Use site default</b> puts the
    shared list back. Must be <code>port@host</code>, colon separated.</td></tr>
<tr><td><b class="k">Design inputs</b></td><td>Either type each path, or switch
    to <b>Derive from an ATPG run directory</b>. The run directory starts as
    the one the analysed fault list came from (for
    <code>&lt;run&gt;/faultlist/x.faults.gz</code> that is
    <code>&lt;run&gt;</code>) and the paths are filled at once &mdash; the
    profile's search patterns locate the ICL, the flat model and the fault
    list. If the ICL or flat model is not there, the status line says which is
    missing; type the run directory that holds them and press
    <b class="k">Fill paths</b>. Every derived path stays editable, and an
    ambiguous match is reported rather than chosen silently.</td></tr>
<tr><td><b class="k">Use the fault list from the analysis inputs</b></td>
    <td>On by default, so the viewer loads exactly the faults this report was
    built from.</td></tr>
</table>
<p><b>Ready to launch?</b> Under the form, a checklist re-runs as you type:
setup wrapper found, tool executable visible, terminal emulator, X display,
licence format, project and every required design file.
<span style="color:#c62828">&#10007;</span> items must be fixed before
<b>Launch</b> will start; <span style="color:#a05000">!</span> items are only
warnings (the setup wrapper may still put the tool on the path). Nothing is run
by the check.</p>

<h3>Saving a setup so you only type it once</h3>
<p>Every field above is remembered <b>per user</b> &mdash; the project, config,
workarea, licence server and each design path are written to
<code>~/.atpg_debug_agent/settings.json</code> as you type them, and restored
the next time the application starts. Nothing is shared between users, and the
licence server in particular never has to be retyped.</p>
<p>For designs you open regularly, use <b class="k">Saved configuration</b> at
the top of the tab. <b class="k">Save as&hellip;</b> writes the whole form
&mdash; setup, licence, workarea and every design path &mdash; to a JSON file
<b>you choose the location of</b> (it opens in
<code>~/.atpg_debug_agent/visualizer_configs</code>), and lists it under the
file's name; picking that name from the list later fills the entire form in
one step, so launching is two clicks. <b class="k">Load&hellip;</b> opens a
configuration file saved earlier &mdash; by you, or by a colleague who kept one
beside a run directory &mdash; and applies it. <b class="k">Delete</b> forgets
an entry in the list; the file on disk is left alone. The list itself survives
a restart through the per-user settings file.</p>
<p>The list opens on <i>(current form &mdash; not saved)</i>, which is the
setup you were last using. Selecting a saved configuration overwrites the form
with it, so save your current setup first if you want to keep it.</p>

<h3>Seeing what will run, before it runs</h3>
<p>The <b>Commands that will run</b> box shows the dofile verbatim. You can
edit it &mdash; an edited dofile is used exactly as written, and is
deliberately <i>not</i> re-checked against the fields above. <b class="k">
Regenerate</b> rebuilds it and discards the edits.</p>
<p><b class="k">Copy commands</b> puts the entire chain on the clipboard so you
can run it by hand. This is the fallback whenever the launch itself is
unavailable &mdash; for instance on a host with no terminal emulator.</p>

<h3>Going straight to one fault</h3>
<p>Right-click any row in the <b>Coverage Loss Table</b> and choose
<b class="k">Inspect in Tessent Visualizer</b>. The profile's fault-inspection
commands are appended to the dofile for that fault and its stuck-at value, so
the session opens already looking at it. <b class="k">Clear added faults</b>
removes them again.</p>

<h3>Opening a signal in a session that is already running</h3>
<p>Once the viewer is up you do not have to relaunch it to look at something
else. <b>Right-click</b> a signal and choose <b class="k">Open signal in
Tessent Visualizer</b>; it appears in the running session. This works from five
places:</p>
<ul>
  <li>the <b>Triage &amp; Fix Plan</b> &rarr; <i>Where the loss is</i> tree, on
      a fault sample;</li>
  <li>the <b>Categories</b> table, which offers the tie drivers and constrained
      signals identified as blocking that category;</li>
  <li>the <b>Fix Plan</b>, on a proposal's hotspot path;</li>
  <li>the <b>Coverage Loss Table</b>, on any fault row;</li>
  <li>the <b>AI Debug Agent</b>'s <b>Agent Response</b> and <b>Follow-up
      Chat</b> text, on any hierarchy path the agent mentions. A path the
      analysed design does not contain is <b>greyed out</b> (the agent may have
      shortened or invented it); <b class="k">Copy path</b> stays available.
      Right-click on prose lists every path in that paragraph.</li>
</ul>
<p>On a <i>cluster prefix</i> the entry is deliberately <b>greyed out</b>. A
prefix is a string the triage computed to show where faults concentrate &mdash;
it is not an object in the design, so the tool could not resolve it.</p>
<p>The status line beside <b class="k">Launch Visualizer</b> shows whether a
live session is available. If there is none, the action does not fail silently:
it tells you so and prints the exact command, so you can paste it yourself.
The tool's own reply is shown verbatim &mdash; for instance, asking for a
schematic before the design has been flattened reports exactly that.</p>

<h3>How the live connection works, and what it is allowed to do</h3>
<p>The generated dofile opens a small listener inside the tool session, and
this application talks to it. It is deliberately narrow:</p>
<ul>
  <li>it listens on <code>127.0.0.1</code> only, on a port the operating system
      picks, and hangs up on anything that is not a local connection;</li>
  <li>every request carries a token generated per session and kept in a
      private folder;</li>
  <li><b>no command text is ever sent.</b> The request is a verb, one object and
      option pairs, and the tool side rebuilds the command so that a bracket or
      a <code>$</code> in an object name stays data and can never become code;</li>
  <li>the verb must appear in the profile's <code>control.allowed_commands</code>
      list, which the shipped profile limits to viewing commands. Widening it
      is an edit to the profile, not something this application can decide.</li>
</ul>
<p>A profile can set <code>control.enabled</code> to <code>false</code> to turn
the channel off entirely; the launch then works exactly as before, and the
signal action reports that the profile disables it.</p>

<h3>Letting the AI agent run commands in the session</h3>
<p>With the Copilot CLI backend in <b class="k">Deep investigation</b> mode, the agent
can ask to run a Tcl script in the running session &mdash; to check something
the offline analysis cannot see, or because you asked it to. This needs the
profile to set <code>control.allow_agent_eval</code> to <code>true</code>
(the shipped profile does) and a session launched from this tab.</p>
<ul>
  <li>Every script appears in a purple <b>The agent wants to run this in
      Tessent</b> box under the chat, with the agent's reason. Nothing reaches
      the tool until you press <b class="k">Approve &amp; Run</b>; you can
      edit the script first, or <b class="k">Reject</b> it.</li>
  <li>The agent never holds the session token &mdash; only this GUI does &mdash;
      so it cannot bypass the approval.</li>
  <li>The result and the transcript the tool printed (cut out of the tool log
      between markers) go back to the agent and are shown in the chat.</li>
  <li><b class="k">Stop</b> rejects anything still waiting. A request nobody
      answers within 10 minutes is withdrawn and does not run.</li>
  <li><b class="k">Allow all Tessent commands</b> (in the chat row) skips the
      approval: every script the agent sends runs at once and is still logged
      in the chat. It is off at start-up; untick it to approve one by one
      again.</li>
</ul>

<h3>What it refuses to do</h3>
<ul>
  <li>Every path, project name and licence string is checked before it reaches
      a generated script. Anything that could escape its quoting is rejected
      with a message naming the offending character.</li>
  <li>The launch is refused, with the list of what is still missing, rather
      than started with a half-filled form.</li>
  <li>If <code>$DISPLAY</code> is not set you are told <i>before</i> the
      launch. The viewer is an X client, so otherwise the failure only appears
      minutes later, after the design has finished loading.</li>
</ul>

<h3>Where the evidence goes</h3>
<p>The generated scripts and the tool's log are written to a private scratch
folder; <b class="k">Open script folder</b> opens it. The <b>Tool log</b> pane
follows that log while the session runs, so a failure inside the tool is
readable here without hunting for the terminal window. If the project setup
itself fails, the terminal window stays open holding the error rather than
closing instantly.</p>
<p>Once configured, the launch details travel with the report: they are saved
in the session file, printed in the Markdown and HTML reports under
<i>Reproduce in Tessent Visualizer</i>, and offered to the AI agent through the
<code>visualizer_commands</code> tool.</p>

</body></html>
"""

_EVERYDAY_HELP = """
<h2>Everyday helpers &mdash; appearance, keyboard and shortcuts</h2>
<p>Everything in this section is optional; it exists so the tool is quick to
learn the first time and quick to drive every day after that.</p>

<a name="checklist"></a><h3>Getting Started checklist</h3>
<p>Until you have done each step once, the top of the <b>Summary</b> tab shows a
five-step checklist: <i>pick the inputs &rarr; Analyze &rarr; review Triage
&amp; Fix Plan &rarr; ask the AI agent &rarr; open the design in Tessent
Visualizer</i>. Each step ticks itself when you do it, and its button takes you
straight there. <b>Hide checklist</b> removes it; <b>Help &rarr; Getting Started
Checklist</b> brings it back. Progress is remembered between sessions.</p>

<a name="dragdrop"></a><h3>Drag and drop</h3>
<p>Drop files from a file manager anywhere on the window. Each file is
recognised by its <i>content</i>: a Verilog netlist fills <b>Netlist</b>, a
Tessent fault list fills <b>Fault list</b>, a <code>.do</code> /
<code>.tcl</code> dofile fills <b>Constraints</b>, a folder fills <b>Output
dir</b>, and a report saved with <b>Save Report</b> is loaded directly. A
notice in the corner says where every file went, and names any file it could
not recognise.</p>

<a name="palette"></a><h3>Command palette</h3>
<p><code>Ctrl+Shift+P</code> (or <b>Edit &rarr; Command Palette</b>) opens a
search box over every menu command, every tab and every recent analysis.
Type a few letters of what you want &mdash; <i>exp csv</i>, <i>dark</i>,
<i>triage</i> &mdash; and press <b>Enter</b>. Start with <code>@</code> to
search the fault paths of the current report instead: <i>@u_tdr</i> jumps to
that row of the Coverage Loss Table.</p>

<a name="find"></a><h3>Find</h3>
<p><code>Ctrl+F</code> does the natural thing for the tab you are on: on the
<b>Coverage Loss Table</b> it puts the cursor in the filter box; on the
<b>Summary</b>, the <b>Triage</b> Categories / Fix Plan details and the
<b>AI agent</b>'s answer it opens a find bar above the tabs (<b>Enter</b> next
match, <b>Shift+Enter</b> previous, <b>Esc</b> close).</p>

<a name="undo"></a><h3>Undo a waiver</h3>
<p>Every waiver &mdash; <b>Edit report</b>, or <i>Exclude this fault</i> from
the table's right-click menu &mdash; can be taken back with
<code>Ctrl+Z</code> (<b>Edit &rarr; Undo Last Waiver</b>), or with the
<b>Undo</b> button on the notice that appears. The last 20 are kept until the
next Analyze or Load Report.</p>

<a name="status"></a><h3>Status bar and notices</h3>
<p>When the application starts and a GitHub token is already available
(saved on the Authentication tab, or in <code>COPILOT_GITHUB_TOKEN</code> /
<code>GH_TOKEN</code> / <code>GITHUB_TOKEN</code>), the Copilot sign-in is
checked in the background, so the AI agent is ready to run without pressing
<b>Check authentication</b>. The agent item in the status bar shows the
result; if the check fails, a notice says so and its <b>Fix sign-in</b>
button opens the Authentication tab.</p>
<p>The right end of the status bar always shows the loaded design with its
coverage-loss count and test coverage, whether the AI agent is ready to run,
and whether a Tessent Visualizer session is live. Click any of them to go to
that tab. Short confirmations (files assigned, waiver applied, report loaded)
appear as a notice in the bottom-right corner that fades on its own; only
real errors still open a dialog. When a long analysis finishes while you are
in another window, the task-bar entry flashes.</p>

<a name="appearance"></a><h3>Light and Dark theme, text size</h3>
<p><b>View &rarr; Theme</b> chooses <b>Light</b> or <b>Dark</b> (modelled on
VS&nbsp;Code's Dark+); <code>Ctrl+K, Ctrl+T</code> and the &#9790;/&#9728;
button in the status bar switch between them. Every view follows the theme,
including the Summary report, this guide and the agent's answers.
<b>Exported and browser reports always stay light</b>, so a file you share
looks the same for everyone. <code>Ctrl+=</code> / <code>Ctrl+-</code> make
the text larger or smaller and <code>Ctrl+0</code> resets it. Both choices are
remembered per user.</p>

<a name="preferences"></a><h3>Preferences</h3>
<p><b>Edit &rarr; Preferences&hellip;</b> (<code>Ctrl+,</code>) collects the
per-user choices in one dialog: theme, text size, auto-save of the report,
the advanced tabs, the Getting Started checklist and whether the guided tour
runs again after the next analysis. They are stored in
<code>~/.atpg_debug_agent/settings.json</code>.</p>

<a name="help_buttons"></a><h3>Help for the screen you are on</h3>
<p>The <b>?</b> at the right end of the tab bar opens this guide at the
section for the current tab; the <b>?</b> buttons beside the input files and
the action buttons do the same for those.</p>

<a name="shortcuts"></a><h3>Keyboard shortcuts &mdash; the complete map</h3>
<p>Every shortcut the application defines. The menus show the same keys next
to each command.</p>
<!--SHORTCUT_TABLE-->
"""

#: User Guide section anchors, inserted before these headings.
_HELP_ANCHORS = [
    ("<h2>Finding your way around", "findyourway"),
    ("<h2>1. Input files", "inputs"),
    ("<h2>2. Action buttons", "actions"),
    ("<h3>Summary</h3>", "summary"),
    ("<h3>Triage &amp; Fix Plan</h3>", "triage"),
    ("<h3>Coverage Loss Table</h3>", "table"),
    ("<h3>Logs / Warnings</h3>", "logs"),
    ("<h3>Skills</h3>", "skills"),
    ("<h2>4. Edit Report", "edit"),
    ("<h2>6. AI Debug Agent", "agent"),
    ("<h2>7. Tessent Visualizer", "visualizer"),
]


def _build_help_html(base: str) -> str:
    for heading, name in _HELP_ANCHORS:
        base = base.replace(heading, f'<a name="{name}"></a>{heading}', 1)
    extra = _EVERYDAY_HELP.replace("<!--SHORTCUT_TABLE-->",
                                   shortcuts.help_table_html())
    marker = '<a name="inputs"></a>'
    return base.replace(marker, '<a name="everyday"></a>' + extra + marker, 1)


_HELP_HTML = _build_help_html(_HELP_HTML)

#: Help anchor for each tab title (the "?" in the tab bar).
_TAB_HELP = {
    "Summary": "summary", "Triage & Fix Plan": "triage",
    "Coverage Loss Table": "table", "AI Debug Agent": "agent",
    "Tessent Visualizer": "visualizer", "Logs / Warnings": "logs",
    "Skills": "skills", "Custom Skills": "skills",
}


def _fmt_secs(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


#: (tab title, what to look at there) for the first-run tour.
TOUR_STEPS = [
    ("Summary",
     "<b>1/5 &nbsp;Summary.</b> The blue box shows test coverage and the "
     "number of coverage-loss faults. <i>Where to start</i> lists the biggest "
     "categories and the first fix for each \u2014 click one to jump to it."),
    ("Triage & Fix Plan",
     "<b>2/5 &nbsp;Categories.</b> One row per loss category. <i>What it "
     "means</i> explains the code in plain words; select a row for the "
     "reasoning, blocking sources and how hard the sites are to test."),
    ("Triage & Fix Plan",
     "<b>3/5 &nbsp;Fix Plan.</b> Ranked actions with the exact Tessent "
     "commands (<i>Copy commands</i>). The tool never runs them, and never "
     "predicts a gain \u2014 the re-run measures it."),
    ("Coverage Loss Table",
     "<b>4/5 &nbsp;Every fault.</b> Filter, sort, and select a row for its "
     "evidence on the right. Right-click a row to ask the AI agent about it or "
     "open it in Tessent Visualizer."),
    ("AI Debug Agent",
     "<b>5/5 &nbsp;AI agent.</b> The line at the top says what is still "
     "missing (CLI, sign-in). Pick <i>Deep investigation</i> and press Run "
     "for a written diagnosis you can question further."),
]


class _TourBar(QFrame):
    """A slim, non-modal banner that walks a new user through the tabs."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window
        self._step = -1
        self.setStyleSheet(
            "_TourBar { background: #fff8e1; border: 1px solid #f0c36d;"
            " border-radius: 4px; }")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        row.addWidget(self.label, 1)
        self.back_btn = QPushButton("\u2039 Back")
        self.back_btn.clicked.connect(lambda: self.show_step(self._step - 1))
        row.addWidget(self.back_btn)
        self.next_btn = QPushButton("Next \u203a")
        self.next_btn.clicked.connect(self._next)
        row.addWidget(self.next_btn)
        self.skip_btn = QPushButton("Skip tour")
        self.skip_btn.clicked.connect(self.finish)
        row.addWidget(self.skip_btn)
        self.setVisible(False)

    @property
    def step(self) -> int:
        return self._step

    def start(self) -> None:
        self.show_step(0)

    def show_step(self, index: int) -> None:
        index = max(0, min(index, len(TOUR_STEPS) - 1))
        self._step = index
        tab, text = TOUR_STEPS[index]
        self._window._switch_to_tab(tab)
        panel = self._window.triage_panel
        if tab == "Triage & Fix Plan":
            panel.tabs.setCurrentIndex(2 if "Fix Plan" in text[:40] else 0)
        self.label.setText(text)
        self.back_btn.setEnabled(index > 0)
        last = index == len(TOUR_STEPS) - 1
        self.next_btn.setText("Finish" if last else "Next \u203a")
        self.setVisible(True)

    def _next(self) -> None:
        if self._step >= len(TOUR_STEPS) - 1:
            self.finish()
        else:
            self.show_step(self._step + 1)

    def stop(self) -> None:
        self._step = -1
        self.setVisible(False)

    def finish(self) -> None:
        self.stop()
        self._window._finish_tour()
        self._window.statusBar().showMessage(
            "Tour finished. Help \u2192 Show the Guided Tour replays it; "
            "F1 opens the full user guide.")


class _QuietHTTPRequestHandler(SimpleHTTPRequestHandler):
    """A ``SimpleHTTPRequestHandler`` that logs to the logger, not stderr."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("report-server: " + format, *args)


class _FilePicker(QWidget):
    checked = Signal()

    def __init__(self, label: str, *, directory: bool = False,
                 check=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._directory = directory
        self._check = check
        self.last_check: Optional[InputCheck] = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel(label)
        title.setMinimumWidth(230)
        layout.addWidget(title, 0)
        self.edit = QLineEdit(self)
        layout.addWidget(self.edit, 1)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        layout.addWidget(browse, 0)
        self.status = QLabel("", self)
        self.status.setMinimumWidth(260)
        self.status.setWordWrap(True)
        layout.addWidget(self.status, 0)
        # Checks touch the file system (possibly NFS), so wait for typing to stop.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self.run_check)
        self.edit.textChanged.connect(lambda _t: self._timer.start())

    def _browse(self) -> None:
        if self._directory:
            path = QFileDialog.getExistingDirectory(self, "Select directory")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select file")
        if path:
            self.edit.setText(path)

    def run_check(self) -> Optional[InputCheck]:
        """Re-check the path now and show the verdict beside the box."""
        self._timer.stop()
        if self._check is None:
            return None
        try:
            result = self._check(self.path())
        except Exception as exc:  # noqa: BLE001 - a check must never break the form
            result = InputCheck("warn", f"could not check: {exc}")
        self.last_check = result
        theme.set_label(
            self.status,
            f"<span style='color:{result.colour}; font-weight:bold;'>"
            f"{result.symbol}</span> <span style='color:{result.colour};'>"
            f"{_html_escape(result.message)}</span>")
        self.status.setToolTip(result.message)
        self.checked.emit()
        return result

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, value: str) -> None:
        self.edit.setText(value)
        self.run_check()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ATPG Coverage-Loss Debug Agent  v2")
        self.resize(1400, 900)

        self._report: Optional[AnalysisReport] = None
        self._base_report: Optional[AnalysisReport] = None
        self._results: List[FaultAnalysisResult] = []
        self._report_html: Optional[str] = None
        self._thread: Optional[QThread] = None
        self._worker = None

        # Multi-partition state: a queue of partitions to analyze, and the
        # analyzed partitions (each keeps its own pristine base + current
        # report) plus the index of the one currently shown in every tab.
        self._queued: List[PartitionInputs] = []
        self._partitions: List[dict] = []
        self._active_idx: int = -1
        # Pending JSON path for an auto-saved report (set at Analyze time when
        # the Auto-save checkbox is on; consumed when the run finishes).
        self._pending_save_path: Optional[str] = None

        # Background HTTP server used to publish a shareable report link.
        self._report_server: Optional[ThreadingHTTPServer] = None
        self._report_server_thread: Optional[threading.Thread] = None
        self._report_server_dir: Optional[str] = None

        self._settings = AppSettings.load()
        self._skill_manager = SkillManager()
        if self._settings.skills:
            self._skill_manager.from_config(self._settings.skills)
        # Earlier states of the current report, newest last, for Ctrl+Z.
        self._undo_stack: List[AnalysisReport] = []
        self._settings.font_delta = theme.clamp_font_delta(
            getattr(self._settings, "font_delta", 0))
        theme.apply_app(QApplication.instance(),
                        getattr(self._settings, "theme", theme.LIGHT),
                        self._settings.font_delta)

        self._build_ui()
        self._build_menu()
        self._build_status_chips()
        self._restore_paths()
        self._refresh_recent_menus()
        self.toast = Toast(self)
        self.command_palette = CommandPalette(self, self.palette_entries)
        self.setAcceptDrops(True)
        self._init_getting_started()
        theme.refresh(self)

    def _restore_paths(self) -> None:
        s = self._settings
        if s.last_netlist:
            self.netlist_picker.set_path(s.last_netlist)
        if s.last_faults:
            self.faults_picker.set_path(s.last_faults)
        if s.last_constraints:
            self.constraints_picker.set_path(s.last_constraints)
        if s.last_output_dir:
            self.outdir_picker.set_path(s.last_output_dir)
        self._last_tool_reports = getattr(s, "last_tool_reports", "") or ""
        if s.filter_text:
            self.filter_text.setText(s.filter_text)
        if s.class_filter:
            idx = self.class_filter.findData(s.class_filter)
            if idx >= 0:
                self.class_filter.setCurrentIndex(idx)
        if s.conf_filter:
            idx = self.conf_filter.findText(s.conf_filter)
            if idx >= 0:
                self.conf_filter.setCurrentIndex(idx)
        if s.agent:
            self.agent_panel.import_settings(s.agent)
        if getattr(s, "visualizer", None):
            self.visualizer_panel.import_settings(s.visualizer)
        if s.last_faults:
            self.visualizer_panel.set_analysis_faults(s.last_faults)
        if s.custom_skills_dir:
            self.custom_skills_panel.set_custom_dir(s.custom_skills_dir)
        self.autosave_check.setChecked(bool(getattr(s, "auto_save_report", False)))

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)

        # Inputs: shown in full until a report exists, then folded into one
        # summary line so the results get the screen.
        self.inputs_box = QWidget()
        inputs_layout = QVBoxLayout(self.inputs_box)
        inputs_layout.setContentsMargins(0, 0, 0, 0)
        self.netlist_picker = _FilePicker("Netlist (.v / .v.gz):",
                                          check=CHECKS["netlist"])
        self.faults_picker = _FilePicker(
            "Fault list (.mtfi / .mtfi.gz / flat):", check=CHECKS["faults"])
        self.constraints_picker = _FilePicker(
            "Constraints (optional .do):", check=CHECKS["constraints"])
        self.outdir_picker = _FilePicker("Output dir:", directory=True,
                                         check=CHECKS["outdir"])
        # Tessent's own reports are collected live by the agent, or added
        # afterwards via More -> Add Tessent reports.
        self._last_tool_reports = ""
        for picker in (self.netlist_picker, self.faults_picker,
                       self.constraints_picker, self.outdir_picker):
            inputs_layout.addWidget(picker)

        toggle_row = QHBoxLayout()
        self.recent_btn = QToolButton()
        self.recent_btn.setText("Recent ▾")
        self.recent_btn.setToolTip(
            "Re-fill all the inputs from a design you analysed before.")
        self.recent_btn.setPopupMode(QToolButton.InstantPopup)
        self.recent_menu = QMenu(self.recent_btn)
        self.recent_btn.setMenu(self.recent_menu)
        toggle_row.addWidget(self.recent_btn)
        self.partitions_toggle = QPushButton("+ Analyze several partitions…")
        self.partitions_toggle.setFlat(True)
        self.partitions_toggle.setCheckable(True)
        self.partitions_toggle.setStyleSheet("color: #0b5394; text-align: left;")
        self.partitions_toggle.setToolTip(
            "Show the partition queue, to analyze several netlist / fault-list "
            "sets in one run. Not needed for a single design.")
        self.partitions_toggle.toggled.connect(self._set_partitions_visible)
        toggle_row.addWidget(self.partitions_toggle)
        toggle_row.addStretch(1)
        toggle_row.addWidget(self._help_button(
            "inputs", "What each input box expects, and how they are checked"))
        inputs_layout.addLayout(toggle_row)

        # --- Partition queue (analyze several partitions together) ---
        self.partition_box = QWidget()
        part_bar = QHBoxLayout(self.partition_box)
        part_bar.setContentsMargins(0, 0, 0, 0)
        part_bar.addWidget(QLabel("Partitions:"))
        self.partition_list = QListWidget()
        self.partition_list.setMaximumHeight(78)
        self.partition_list.setToolTip(
            "Partitions queued for analysis. Set the netlist / fault list / "
            "constraints above, click 'Add Partition', then repeat for each "
            "partition. 'Analyze' runs them all; leave empty to analyze the "
            "single file set above.")
        part_bar.addWidget(self.partition_list, 1)
        part_btns = QVBoxLayout()
        self.add_partition_btn = QPushButton("Add Partition")
        self.add_partition_btn.setToolTip(
            "Queue the current netlist / fault list / constraints as a named "
            "partition.")
        self.add_partition_btn.clicked.connect(self.on_add_partition)
        self.remove_partition_btn = QPushButton("Remove")
        self.remove_partition_btn.setToolTip(
            "Remove the selected partition from the queue.")
        self.remove_partition_btn.clicked.connect(self.on_remove_partition)
        self.clear_partitions_btn = QPushButton("Clear Queue")
        self.clear_partitions_btn.clicked.connect(self.on_clear_partitions)
        for b in (self.add_partition_btn, self.remove_partition_btn,
                  self.clear_partitions_btn):
            part_btns.addWidget(b)
        part_btns.addStretch(1)
        part_bar.addLayout(part_btns)
        self.partition_box.setVisible(False)
        inputs_layout.addWidget(self.partition_box)
        outer.addWidget(self.inputs_box)

        # The one-line stand-in for the inputs once a report is shown.
        self.inputs_summary = QWidget()
        summary_row = QHBoxLayout(self.inputs_summary)
        summary_row.setContentsMargins(0, 0, 0, 0)
        self.inputs_summary_label = QLabel("")
        self.inputs_summary_label.setStyleSheet("color: #333;")
        self.inputs_summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        summary_row.addWidget(self.inputs_summary_label, 1)
        self.change_inputs_btn = QPushButton("Change inputs \u25be")
        self.change_inputs_btn.setToolTip(
            "Show the input files again, to pick different ones and re-run "
            "Analyze.")
        self.change_inputs_btn.clicked.connect(self._toggle_inputs)
        summary_row.addWidget(self.change_inputs_btn)
        self.inputs_summary.setVisible(False)
        outer.addWidget(self.inputs_summary)
        self._inputs_expanded = False

        btn_row = QHBoxLayout()
        self.analyze_btn = QPushButton("\u25b6  Analyze")
        self.analyze_btn.setToolTip(
            "Parse the inputs above and build the report. Start here.")
        self.analyze_btn.setStyleSheet(
            "QPushButton { background: #0b5394; color: white; font-weight: bold;"
            " padding: 5px 18px; border-radius: 4px; }"
            "QPushButton:disabled { background: #9fb6cc; }")
        self.analyze_btn.clicked.connect(self.on_analyze)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.cancel_btn.setEnabled(False)
        self.save_report_btn = QPushButton("Save Report")
        self.save_report_btn.setToolTip(
            "Save the full analysis to a JSON file you can reload later "
            "without re-running Analyze. The per-category fault files are "
            "written into a folder beside it, so the whole session can be "
            "handed to someone else.")
        self.save_report_btn.clicked.connect(self.on_save_report)
        self.load_report_btn = QPushButton("Load Report")
        self.load_report_btn.setToolTip(
            "Load a previously saved report and work on it (tables + AI agent) "
            "without re-analyzing.")
        self.load_report_btn.clicked.connect(self.on_load_report)

        self.export_btn = QToolButton()
        self.export_btn.setText("Export \u25be")
        self.export_btn.setToolTip("Write the report as Markdown or CSV.")
        self.export_btn.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(self.export_btn)
        self.md_action = export_menu.addAction("Markdown report…")
        self.md_action.triggered.connect(self.on_export_md)
        self.csv_action = export_menu.addAction("CSV table…")
        self.csv_action.triggered.connect(self.on_export_csv)
        self.export_btn.setMenu(export_menu)

        self.more_btn = QToolButton()
        self.more_btn.setText("More \u25be")
        self.more_btn.setToolTip(
            "Compare with a previous report, waive faults, or clear the "
            "results.")
        self.more_btn.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(self.more_btn)
        self.compare_action = more_menu.addAction("Compare with a previous report…")
        self.compare_action.setToolTip(
            "Load a baseline report (a previous run) and diff it against the "
            "current one — regressed / fixed / changed faults — then ask the AI "
            "agent what changed.")
        self.compare_action.triggered.connect(self.on_compare_report)
        self.edit_action = more_menu.addAction("Edit report (waive faults)…")
        self.edit_action.setToolTip(
            "Waive whole classes (AU/UO/UC), specific subtypes (e.g. "
            "AU.NOFAULTS), or individual faults; coverage recomputes and the "
            "layout is unchanged. Reversible and saved with the report.")
        self.edit_action.triggered.connect(self.on_edit_report)
        self.tool_reports_action = more_menu.addAction(
            "Add Tessent reports to these results…")
        self.tool_reports_action.setToolTip(
            "Read report_statistics / analyze_fault output now and compare it "
            "with the current results; measured verdicts replace estimates.")
        self.tool_reports_action.triggered.connect(self.on_add_tool_reports)
        more_menu.addSeparator()
        self.clear_action = more_menu.addAction("Clear results")
        self.clear_action.triggered.connect(self.on_clear)
        more_menu.setToolTipsVisible(True)
        self.more_btn.setMenu(more_menu)

        for b in (self.analyze_btn, self.cancel_btn, self.load_report_btn,
                  self.save_report_btn, self.export_btn, self.more_btn):
            btn_row.addWidget(b)
        self.autosave_check = QCheckBox("Auto-save report")
        self.autosave_check.setToolTip(
            "After Analyze, automatically save the JSON report into the Output "
            "dir. You'll be asked for a name first (default derived from the "
            "netlist, e.g. my_design.v.gz \u2192 my_design_atpg_report).")
        self.autosave_check.toggled.connect(self._save_settings)
        btn_row.addWidget(self.autosave_check)
        btn_row.addStretch(1)
        btn_row.addWidget(self._help_button(
            "actions", "What each of these buttons does"))
        outer.addLayout(btn_row)
        self._set_export_enabled(False)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        # --- Active-partition selector (shown after a multi-partition run) ---
        sel_row = QHBoxLayout()
        self.partition_selector_label = QLabel("Active partition:")
        self.partition_selector = QComboBox()
        self.partition_selector.setToolTip(
            "Switch the partition shown in every tab below (Summary, table, "
            "AI agent, Edit Report). Each partition keeps its own report and "
            "edits.")
        self.partition_selector.currentIndexChanged.connect(
            self._on_partition_selected)
        sel_row.addWidget(self.partition_selector_label)
        sel_row.addWidget(self.partition_selector, 1)
        sel_row.addStretch(1)
        self.partition_selector_label.setVisible(False)
        self.partition_selector.setVisible(False)
        outer.addLayout(sel_row)

        self.tabs = QTabWidget()
        self.tour_bar = _TourBar(self)
        outer.addWidget(self.tour_bar)
        self.find_bar = FindBar()
        outer.addWidget(self.find_bar)
        tab_help = self._help_button(
            "", "Open the User Guide at the section for this tab (F1 opens "
                "it from the top)", slot=self.show_help_for_current_tab)
        self.tabs.setCornerWidget(tab_help, Qt.TopRightCorner)
        self._summary_tab = self._build_summary_tab()
        self.tabs.addTab(self._summary_tab, "Summary")
        self.triage_panel = TriagePanel()
        self.triage_panel.empty_action_requested.connect(self._on_empty_action)
        self.triage_panel.fault_referenced.connect(self._focus_fault_in_table)
        self.triage_panel.export_categories_requested.connect(
            self.on_export_category_faults)
        self.tabs.addTab(self.triage_panel, "Triage && Fix Plan")
        self._table_tab = self._build_table_tab()
        self.tabs.addTab(self._table_tab, "Coverage Loss Table")
        # The "Repeated Patterns" tab is intentionally not shown; the backing
        # widget is still built so the populate/reset logic keeps working.
        self._build_patterns_tab()
        self._logs_tab = self._build_logs_tab()
        self.tabs.addTab(self._logs_tab, "Logs / Warnings")

        self.skills_panel = SkillsPanel(self._skill_manager)
        self.skills_panel.settings_changed.connect(self._save_settings)
        self.tabs.addTab(self.skills_panel, "Skills")

        self.custom_skills_panel = CustomSkillsPanel(self._skill_manager)
        self.custom_skills_panel.skills_loaded.connect(self._on_custom_skills_loaded)
        self.tabs.addTab(self.custom_skills_panel, "Custom Skills")

        self.agent_panel = AgentPanel()
        self.agent_panel.config_changed.connect(self._save_settings)
        self.agent_panel.fault_referenced.connect(self._focus_fault_in_table)
        self.agent_panel.findings_changed.connect(self._on_agent_findings)
        self.agent_panel.fix_plan_changed.connect(self._on_agent_fix_edits)
        self.agent_panel.tool_evidence_collected.connect(
            self._on_agent_tool_evidence)
        self.agent_panel.signal_inspect_requested.connect(
            self._show_signal_in_visualizer)
        # The agent panel is tall. Placed directly in the tab widget its
        # minimum size propagates to the whole window, which then cannot be
        # shrunk and barely changes when maximised. A scroll area decouples
        # the two, exactly as the Skills tab already does.
        agent_scroll = QScrollArea()
        agent_scroll.setWidgetResizable(True)
        agent_scroll.setFrameShape(QFrame.NoFrame)
        agent_scroll.setWidget(self.agent_panel)
        self._agent_tab = agent_scroll
        self.tabs.addTab(agent_scroll, "AI Debug Agent")

        self.visualizer_panel = VisualizerPanel()
        self.visualizer_panel.config_changed.connect(self._save_settings)
        self.visualizer_panel.status_message.connect(
            lambda msg: self.statusBar().showMessage(msg))
        # Tall form; scrolled for the same reason as the agent panel.
        vis_scroll = QScrollArea()
        vis_scroll.setWidgetResizable(True)
        vis_scroll.setFrameShape(QFrame.NoFrame)
        vis_scroll.setWidget(self.visualizer_panel)
        self.tabs.addTab(vis_scroll, "Tessent Visualizer")
        self.visualizer_panel.set_analysis_faults(self.faults_picker.path())
        self.faults_picker.edit.textChanged.connect(
            self.visualizer_panel.set_analysis_faults)
        self.triage_panel.signal_inspect_requested.connect(
            self._show_signal_in_visualizer)
        self.agent_panel.set_tessent_provider(self.visualizer_panel.agent_target)
        self.agent_panel.open_visualizer_requested.connect(
            lambda: self.tabs.setCurrentWidget(vis_scroll))
        self.visualizer_panel.status_message.connect(
            lambda *_: self.agent_panel.refresh_readiness())
        self.tabs.currentChanged.connect(
            lambda *_: self.agent_panel.refresh_readiness())
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.agent_panel.run_completed.connect(
            lambda: self._gs_mark("agent"))
        self.agent_panel.auto_auth_finished.connect(self._on_auto_auth)
        self.visualizer_panel.launched.connect(
            lambda: self._gs_mark("visualizer"))
        self._advanced_tabs = [self._logs_tab, self.skills_panel,
                               self.custom_skills_panel]
        self._set_advanced_tabs_visible(
            bool(getattr(self._settings, "show_advanced_tabs", False)))

        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready — load files and click Analyze.")

    def _build_summary_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.demo_btn = QPushButton("Try the demo data")
        self.demo_btn.setToolTip(
            "Fill in the bundled example design (netlist, fault list and "
            "constraints) and analyze it, to explore the tool before using "
            "your own files.")
        self.demo_btn.clicked.connect(self.on_try_demo)
        self.demo_btn.setVisible(_demo_inputs() is not None)
        bar.addWidget(self.demo_btn)
        bar.addStretch(1)
        self.open_browser_btn = QPushButton("Open Report in Browser")
        self.open_browser_btn.setToolTip(
            "Render the full-fidelity HTML report in your web browser")
        self.open_browser_btn.clicked.connect(self.on_open_report_in_browser)
        self.open_browser_btn.setEnabled(False)
        bar.addWidget(self.open_browser_btn)
        layout.addLayout(bar)

        self.getting_started = GettingStarted()
        self.getting_started.action_requested.connect(self._on_gs_action)
        self.getting_started.hide_requested.connect(
            lambda: self.set_checklist_visible(False))
        layout.addWidget(self.getting_started)

        # At-a-glance results above the full report.
        self.dashboard = QLabel("")
        self.dashboard.setWordWrap(True)
        self.dashboard.setTextFormat(Qt.RichText)
        self.dashboard.setOpenExternalLinks(False)
        self.dashboard.linkActivated.connect(self._on_dashboard_link)
        self.dashboard.setStyleSheet(
            "QLabel { background: #f4f8fc; border: 1px solid #d0e2f2;"
            " border-radius: 6px; padding: 10px; }")
        self.dashboard.setVisible(False)
        layout.addWidget(self.dashboard)

        self.summary_view = QTextBrowser()
        self.summary_view.setOpenExternalLinks(True)
        layout.addWidget(self.summary_view, 1)
        self._set_summary_html(_WELCOME_HTML)
        return widget

    def _set_summary_html(self, html: str) -> None:
        theme.set_html(self.summary_view, html)

    def _clear_summary(self) -> None:
        self.dashboard.setVisible(False)
        self._set_summary_html(_WELCOME_HTML)


    def _build_table_tab(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 0, 0, 0)
        self.table_empty = EmptyState(
            "No faults to show yet",
            "Every coverage-loss fault is listed here, one row each, with its "
            "mapping, connectivity and the root cause this tool derived.")
        self.table_empty.action_requested.connect(self._on_empty_action)
        outer.addWidget(self.table_empty, 1)
        self.table_content = QWidget()
        outer.addWidget(self.table_content, 1)
        self.table_content.setVisible(False)
        layout = QVBoxLayout(self.table_content)
        filt = QHBoxLayout()
        filt.addWidget(QLabel("Filter:"))
        self.filter_text = QLineEdit()
        self.filter_text.setPlaceholderText("substring on object/instance/root-cause…")
        self.filter_text.textChanged.connect(self._apply_filter)
        filt.addWidget(self.filter_text, 1)
        filt.addWidget(QLabel("Class:"))
        self.class_filter = QComboBox()
        for text, code in _CLASS_FILTER_ITEMS:
            self.class_filter.addItem(text, code)
        self.class_filter.currentTextChanged.connect(self._apply_filter)
        filt.addWidget(self.class_filter)
        filt.addWidget(QLabel("Confidence:"))
        self.conf_filter = QComboBox()
        self.conf_filter.addItems(["all", "high", "medium", "low", "unresolved"])
        self.conf_filter.currentTextChanged.connect(self._apply_filter)
        filt.addWidget(self.conf_filter)
        export_filtered_btn = QPushButton("Export Filtered CSV")
        export_filtered_btn.clicked.connect(self.on_export_filtered_csv)
        filt.addWidget(export_filtered_btn)
        layout.addLayout(filt)

        splitter = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, len(_TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(_TABLE_HEADERS)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        # The end of a hierarchy path is the part that identifies the site.
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setColumnWidth(0, 420)
        for col, tip in enumerate(_TABLE_HEADER_TIPS):
            self.table.horizontalHeaderItem(col).setToolTip(tip)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        splitter.addWidget(self.table)
        self.details = DetailsPanel()
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        return widget

    def _build_patterns_tab(self) -> QWidget:
        self.patterns_table = QTableWidget(0, 4)
        self.patterns_table.setHorizontalHeaderLabels(
            ["Kind", "Key", "Count", "Sample faults"])
        self.patterns_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.patterns_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        return self.patterns_table

    def _build_logs_tab(self) -> QWidget:
        self.logs_view = QPlainTextEdit()
        self.logs_view.setReadOnly(True)
        return self.logs_view

    def _help_button(self, anchor: str, tip: str, slot=None) -> QToolButton:
        """A small "?" that opens the User Guide at *anchor*."""
        btn = QToolButton()
        btn.setText("?")
        btn.setAutoRaise(True)
        btn.setToolTip(tip)
        btn.setProperty("help_anchor", anchor)
        btn.clicked.connect(slot or partial(self.show_help, anchor))
        return btn

    def show_help_for_current_tab(self) -> None:
        title = self.tabs.tabText(self.tabs.currentIndex()).replace("&&", "&")
        self.show_help(_TAB_HELP.get(title, ""))

    def show_help(self, anchor: str = "") -> None:
        """Open the user guide in a scrollable dialog, at *anchor* if given."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Help — User Guide")
        dlg.resize(860, 760)
        v = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        theme.set_html(view, _HELP_HTML)
        view.moveCursor(QTextCursor.Start)
        v.addWidget(view, 1)
        if anchor:
            QTimer.singleShot(0, lambda: view.scrollToAnchor(anchor))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        v.addWidget(buttons)
        dlg.exec()

    def _action(self, text: str, slot, shortcut_id: str = "",
                menu: Optional[QMenu] = None, extra_keys=()) -> QAction:
        """A window-wide action; keys come from :mod:`shortcuts`."""
        act = QAction(text, self)
        if shortcut_id:
            seqs = [QKeySequence(shortcuts.keys(shortcut_id))]
            seqs += [QKeySequence(k) for k in extra_keys]
            act.setShortcuts(seqs)
            act.setShortcutContext(Qt.WindowShortcut)
        act.triggered.connect(slot)
        if menu is not None:
            menu.addAction(act)
        else:
            self.addAction(act)
        return act

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.analyze_action = self._action(
            "Analyze", self.on_analyze, "analyze", file_menu)
        self.cancel_action = self._action(
            "Cancel Analysis", self.on_cancel, "cancel", file_menu)
        self.cancel_action.setEnabled(False)
        file_menu.addSeparator()
        self._action("Load Report…", self.on_load_report, "load_report",
                     file_menu)
        self.save_report_action = self._action(
            "Save Report…", self.on_save_report, "save_report", file_menu)
        self.file_recent_menu = file_menu.addMenu("Recent Analyses")
        file_menu.addSeparator()
        self.md_action.setText("Export Markdown Report…")
        self.md_action.setShortcut(QKeySequence(shortcuts.keys("export_md")))
        file_menu.addAction(self.md_action)
        self.csv_action.setText("Export CSV…")
        self.csv_action.setShortcut(QKeySequence(shortcuts.keys("export_csv")))
        file_menu.addAction(self.csv_action)
        file_menu.addSeparator()
        self._action("Quit", self.close, "quit", file_menu)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = self._action(
            "Undo Last Waiver", self.undo_exclusion, "undo", edit_menu)
        self.undo_action.setEnabled(False)
        edit_menu.addSeparator()
        self._action("Find…", self.on_find, "find", edit_menu)
        self.palette_action = self._action(
            "Command Palette…", self.open_command_palette, "palette",
            edit_menu)
        edit_menu.addSeparator()
        edit_menu.addAction(self.edit_action)
        edit_menu.addAction(self.compare_action)
        edit_menu.addAction(self.tool_reports_action)
        edit_menu.addAction(self.clear_action)
        edit_menu.addSeparator()
        self._action("Preferences…", self.on_preferences, "preferences",
                     edit_menu)

        skills_menu = self.menuBar().addMenu("&Skills")
        enable_all = QAction("Enable All Skills", self)
        enable_all.triggered.connect(self._on_enable_all_skills)
        skills_menu.addAction(enable_all)
        disable_all = QAction("Disable All Skills", self)
        disable_all.triggered.connect(self._on_disable_all_skills)
        skills_menu.addAction(disable_all)
        skills_menu.addSeparator()
        reset_defaults = QAction("Reset Skill Defaults", self)
        reset_defaults.triggered.connect(self._on_reset_skill_defaults)
        skills_menu.addAction(reset_defaults)

        self._build_view_menu()

        help_menu = self.menuBar().addMenu("&Help")
        self._action("User Guide…", self.show_help, "help", help_menu)
        self._action("Keyboard Shortcuts…",
                     partial(self.show_help, "shortcuts"), menu=help_menu)
        self._action("Getting Started Checklist",
                     lambda: self.set_checklist_visible(True), menu=help_menu)
        demo = self._action("Try the Demo Data", self.on_try_demo,
                            menu=help_menu)
        demo.setEnabled(_demo_inputs() is not None)
        self._action("Show the Guided Tour", self.start_tour, menu=help_menu)

    def _build_view_menu(self) -> None:
        """Tabs, theme, text size and window-sizing actions.

        Some remote X sessions have a window manager whose maximise button
        does nothing. The sizing actions drive the window directly through
        Qt, so they work regardless.
        """
        view_menu = self.menuBar().addMenu("&View")

        goto = view_menu.addMenu("Go to Tab")
        for n, title in enumerate(("Summary", "Triage & Fix Plan",
                                   "Coverage Loss Table", "AI Debug Agent",
                                   "Tessent Visualizer"), start=1):
            self._action(title.replace("&", "&&"),
                         partial(self._switch_to_tab, title), f"tab{n}", goto)

        theme_menu = view_menu.addMenu("Theme")
        self.theme_group = QActionGroup(self)
        self.theme_actions = {}
        for label, name in (("Light", theme.LIGHT), ("Dark", theme.DARK)):
            act = QAction(label, self, checkable=True)
            act.setChecked(theme.current() == name)
            act.triggered.connect(partial(self.set_theme, name))
            self.theme_group.addAction(act)
            theme_menu.addAction(act)
            self.theme_actions[name] = act
        self._action("Toggle Light / Dark Theme", self.toggle_theme,
                     "toggle_theme", view_menu)
        self._action("Zoom In (larger text)", lambda: self.change_font(+1),
                     "zoom_in", view_menu, extra_keys=("Ctrl++",))
        self._action("Zoom Out (smaller text)", lambda: self.change_font(-1),
                     "zoom_out", view_menu)
        self._action("Reset Zoom", lambda: self.set_font_delta(0),
                     "zoom_reset", view_menu)
        view_menu.addSeparator()

        self.advanced_tabs_action = QAction(
            "Show Advanced Tabs (Logs, Skills, Custom Skills)", self)
        self.advanced_tabs_action.setCheckable(True)
        self.advanced_tabs_action.setChecked(
            bool(getattr(self._settings, "show_advanced_tabs", False)))
        self.advanced_tabs_action.toggled.connect(self._on_advanced_tabs_toggled)
        view_menu.addAction(self.advanced_tabs_action)
        for title in ("Logs / Warnings", "Skills", "Custom Skills"):
            act = QAction(f"Open {title}", self)
            act.triggered.connect(partial(self._switch_to_tab, title))
            view_menu.addAction(act)
        view_menu.addSeparator()
        self._action("Maximize", self.showMaximized, "maximize", view_menu)
        self.fullscreen_action = self._action(
            "Full Screen", self._toggle_full_screen, "fullscreen", view_menu)
        self.fullscreen_action.setCheckable(True)
        self._action("Restore Down", self._restore_window, "restore",
                     view_menu)

    def _toggle_full_screen(self, checked: bool) -> None:
        """Enter or leave full screen, returning to a maximised window."""
        if checked:
            self.showFullScreen()
        else:
            self.showMaximized()

    def _restore_window(self) -> None:
        """Leave full screen or maximised state and show a normal window."""
        self.fullscreen_action.setChecked(False)
        self.showNormal()

    def _on_enable_all_skills(self) -> None:
        self._skill_manager.enable_all()
        self.skills_panel.settings_pane._refresh_all()
        self._save_settings()
        self.statusBar().showMessage("All skills enabled.")

    def _on_disable_all_skills(self) -> None:
        self._skill_manager.disable_all()
        self.skills_panel.settings_pane._refresh_all()
        self._save_settings()
        self.statusBar().showMessage("All skills disabled.")

    def _on_reset_skill_defaults(self) -> None:
        self._skill_manager.reset_defaults()
        self.skills_panel.settings_pane._refresh_all()
        self._save_settings()
        self.statusBar().showMessage("Skill defaults restored.")

    def _on_custom_skills_loaded(self) -> None:
        """Rebuild the Skills tab cards when custom skills are added."""
        self.skills_panel.settings_pane.rebuild()
        self._settings.custom_skills_dir = self.custom_skills_panel.custom_dir()
        self._save_settings()
        self.statusBar().showMessage("Custom skills loaded — see the Skills tab.")

    def on_analyze(self) -> None:
        if self._queued:
            self._save_settings()
            self._start_multi_analysis(list(self._queued))
            return

        netlist = self.netlist_picker.path()
        faults = self.faults_picker.path()
        constraints = self.constraints_picker.path() or None

        problems = []
        for name, picker in (("Netlist", self.netlist_picker),
                             ("Fault list", self.faults_picker),
                             ("Constraints", self.constraints_picker)):
            result = picker.run_check()
            if result is not None and result.state == ERROR:
                problems.append(f"{name}: {result.message}")
        if problems:
            self._error("Please fix the inputs first (see the red marks next "
                        "to the file boxes):\n\n" + "\n".join(problems))
            self._set_inputs_collapsed(False)
            return
        if not netlist or not os.path.isfile(netlist):
            self._error("Please select a valid netlist file.")
            return
        if not faults or not os.path.isfile(faults):
            self._error("Please select a valid fault-list file.")
            return
        if constraints and not os.path.isfile(constraints):
            self._error("The constraint path is set but the file does not exist.")
            return
        if not constraints:
            self.notify("No constraint file selected — constraint-related "
                        "diagnoses are disabled for this run.", "warn")

        self._pending_save_path = None
        if self.autosave_check.isChecked():
            if not self._prompt_autosave_name(netlist):
                return  # user cancelled the name dialog -> cancel Analyze

        self._settings.remember_inputs({
            "netlist": netlist, "faults": faults,
            "constraints": constraints or "",
            "outdir": self.outdir_picker.path()})
        self._refresh_recent_menus()
        self._save_settings()
        inputs = AnalysisInputs(netlist, faults, constraints)
        self._start_analysis(inputs)

    # ------------------------------------------------------------------
    # Recent inputs
    # ------------------------------------------------------------------
    def _refresh_recent_menus(self) -> None:
        menus = [self.recent_menu]
        if getattr(self, "file_recent_menu", None) is not None:
            menus.append(self.file_recent_menu)
        recent = list(self._settings.recent_inputs or [])
        for menu in menus:
            menu.clear()
            if not recent:
                act = menu.addAction("(no recent analyses yet)")
                act.setEnabled(False)
                continue
            for entry in recent:
                netlist = entry.get("netlist", "")
                faults = entry.get("faults", "")
                label = (f"{_design_name(netlist) or os.path.basename(netlist)}"
                         f"  —  {os.path.basename(faults)}")
                act = menu.addAction(label)
                act.setToolTip("\n".join(
                    p for p in (netlist, faults, entry.get("constraints", ""))
                    if p))
                act.triggered.connect(partial(self.apply_recent, dict(entry)))
            menu.setToolTipsVisible(True)
        self.recent_btn.setEnabled(bool(recent))

    def apply_recent(self, entry: dict) -> None:
        """Fill every input box from a remembered analysis."""
        self.netlist_picker.set_path(entry.get("netlist", ""))
        self.faults_picker.set_path(entry.get("faults", ""))
        self.constraints_picker.set_path(entry.get("constraints", ""))
        if entry.get("outdir"):
            self.outdir_picker.set_path(entry["outdir"])
        self._set_inputs_collapsed(False)
        self.statusBar().showMessage(
            "Inputs filled from a recent analysis — press ▶ Analyze.")

    # ------------------------------------------------------------------
    # Empty states and the guided tour
    # ------------------------------------------------------------------
    def _on_empty_action(self, action: str) -> None:
        if action == "demo":
            self.on_try_demo()
        elif action == "load":
            self.on_load_report()
        else:
            self._set_inputs_collapsed(False)
            self._switch_to_tab("Summary")
            self.netlist_picker.edit.setFocus()
            self.statusBar().showMessage(
                "Pick a netlist and a fault list at the top, then press "
                "▶ Analyze.")

    def _set_has_report(self, has_report: bool) -> None:
        self.table_empty.setVisible(not has_report)
        self.table_content.setVisible(has_report)

    def start_tour(self) -> None:
        """Walk a new user through the result tabs, one step at a time."""
        if self._report is None:
            self.statusBar().showMessage(
                "The guided tour starts after an analysis — try the demo "
                "data first.")
            return
        self.tour_bar.start()

    def _finish_tour(self) -> None:
        self._settings.tour_done = True
        self._save_settings()

    # ------------------------------------------------------------------
    # Window and splitter layout persistence
    # ------------------------------------------------------------------
    def _splitters(self) -> dict:
        """Every splitter in the window, keyed by owner class and order."""
        keyed = {}
        counts: dict = {}
        for splitter in self.findChildren(QSplitter):
            owner = splitter.parent()
            while owner is not None and not isinstance(
                    owner, (TriagePanel, AgentPanel, VisualizerPanel,
                            MainWindow)):
                owner = owner.parent()
            base = type(owner).__name__ if owner is not None else "window"
            n = counts.get(base, 0)
            counts[base] = n + 1
            keyed[f"{base}.{n}"] = splitter
        return keyed

    def save_layout(self) -> None:
        s = self._settings
        s.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        s.splitter_states = {
            key: bytes(sp.saveState().toBase64()).decode()
            for key, sp in self._splitters().items()}

    def restore_layout(self) -> bool:
        """Restore splitters and geometry; True when a geometry was restored."""
        s = self._settings
        for key, splitter in self._splitters().items():
            state = (s.splitter_states or {}).get(key)
            if state:
                splitter.restoreState(QByteArray.fromBase64(state.encode()))
        if s.window_geometry:
            return bool(self.restoreGeometry(
                QByteArray.fromBase64(s.window_geometry.encode())))
        return False

    def _default_report_name(self, netlist: str) -> str:
        """Report base name derived from the netlist (``<design>_atpg_report``)."""
        design = _design_name(netlist) or "atpg"
        return f"{design}_atpg_report"

    def _autosave_dir(self, netlist: str = "") -> str:
        """Directory to auto-save into: the Output dir, else the netlist's dir."""
        outdir = self.outdir_picker.path()
        if outdir and os.path.isdir(outdir):
            return outdir
        return os.path.dirname(netlist) or os.getcwd()

    def _prompt_autosave_name(self, netlist: str) -> bool:
        """Ask for the auto-save report name; store the target path.

        Returns ``False`` if the user cancelled (so Analyze is aborted).
        """
        default = self._default_report_name(netlist)
        name, ok = QInputDialog.getText(
            self, "Auto-save report",
            "Save the analysis report under this name (in the output dir):",
            text=default)
        if not ok:
            return False
        name = (name or "").strip() or default
        if not name.lower().endswith(".json"):
            name += ".json"
        self._pending_save_path = os.path.join(self._autosave_dir(netlist), name)
        return True

    def _auto_save_report(self, report: AnalysisReport, path: str) -> bool:
        """Save *report* to *path*, folding in the current agent investigation."""
        try:
            report.investigation = self.agent_panel.export_investigation()
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            save_report(report, path)
        except OSError as exc:
            self._error(f"Could not auto-save report:\n{exc}")
            return False
        return True

    def _auto_save_partitions(self, results) -> int:
        """Auto-save each partition report to the output dir; return the count."""
        saved = 0
        for name, report in results:
            netlist = ""
            if getattr(report, "sources", None):
                netlist = report.sources.get("netlist", "") or ""
            path = os.path.join(self._autosave_dir(netlist),
                                f"{name}_atpg_report.json")
            try:
                save_report(report, path)
                saved += 1
            except OSError as exc:
                logger.warning("Auto-save failed for partition %s: %s",
                               name, exc)
        return saved

    def _unique_partition_name(self, base: str) -> str:
        """Return *base* made unique against already-queued partition names."""
        existing = {p.name for p in self._queued}
        if base not in existing:
            return base
        i = 2
        while f"{base}_{i}" in existing:
            i += 1
        return f"{base}_{i}"

    def on_add_partition(self) -> None:
        netlist = self.netlist_picker.path()
        faults = self.faults_picker.path()
        constraints = self.constraints_picker.path() or None
        if not netlist or not os.path.isfile(netlist):
            self._error("Select a valid netlist file before adding a partition.")
            return
        if not faults or not os.path.isfile(faults):
            self._error("Select a valid fault-list file before adding a partition.")
            return
        if constraints and not os.path.isfile(constraints):
            self._error("The constraint path is set but the file does not exist.")
            return
        base = _design_name(netlist) or f"partition_{len(self._queued) + 1}"
        name = self._unique_partition_name(base)
        self._queued.append(
            PartitionInputs(name, AnalysisInputs(netlist, faults, constraints)))
        self.partition_list.addItem(
            f"{name}  —  {os.path.basename(netlist)} / {os.path.basename(faults)}")
        self.statusBar().showMessage(
            f"Queued partition '{name}'. Set the next partition's files and "
            f"click 'Add Partition' again, or click 'Analyze' to run all "
            f"{len(self._queued)}.")

    def on_remove_partition(self) -> None:
        row = self.partition_list.currentRow()
        if row < 0:
            return
        self.partition_list.takeItem(row)
        del self._queued[row]

    def on_clear_partitions(self) -> None:
        self._queued = []
        self.partition_list.clear()
        self.statusBar().showMessage("Partition queue cleared.")

    def _start_analysis(self, inputs: AnalysisInputs) -> None:
        self._set_running(True)
        self._set_export_enabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage("Starting analysis…")
        self._reset_progress_clock()

        self._thread, self._worker = start_worker(inputs, self._skill_manager)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _start_multi_analysis(self, partitions) -> None:
        self._set_running(True)
        self._set_export_enabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage(
            f"Analyzing {len(partitions)} partition(s)…")
        self._reset_progress_clock()

        self._thread, self._worker = start_multi_worker(
            partitions, self._skill_manager)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_multi_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def on_cancel(self) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.requestInterruption()
            self.statusBar().showMessage(
                "Cancelling — the analysis stops at its next checkpoint…")
            self.cancel_btn.setEnabled(False)
            self.cancel_action.setEnabled(False)

    def _set_running(self, running: bool) -> None:
        self.analyze_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        if getattr(self, "analyze_action", None) is not None:
            self.analyze_action.setEnabled(not running)
            self.cancel_action.setEnabled(running)

    def _on_cancelled(self) -> None:
        self.progress.setVisible(False)
        self._set_running(False)
        self._pending_save_path = None
        self._set_export_enabled(self._report is not None)
        self.statusBar().showMessage(
            "Analysis cancelled. Nothing was changed; the previous results "
            "(if any) are still shown.")

    def _reset_progress_clock(self) -> None:
        self._run_started = time.monotonic()
        self._phase_key = None
        self._phase_started = self._run_started

    def progress_text(self, done: int, total: int, msg: str,
                      now: Optional[float] = None) -> str:
        """*msg* with elapsed time and, inside a counted phase, time left."""
        now = time.monotonic() if now is None else now
        started = getattr(self, "_run_started", now)
        key = (total, msg.split(" ")[0] if msg else "")
        if key != getattr(self, "_phase_key", None):
            self._phase_key = key
            self._phase_started = now
        text = f"{msg}  —  {_fmt_secs(now - started)} elapsed"
        phase = now - self._phase_started
        if total > 0 and 0 < done < total and phase >= 3.0:
            left = phase * (total - done) / done
            text += f", about {_fmt_secs(left)} left in this step"
        return text

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)
        self.statusBar().showMessage(self.progress_text(done, total, msg))

    def _on_finished(self, report: AnalysisReport) -> None:
        self.progress.setVisible(False)
        self._set_running(False)
        self._reset_partitions()
        self._clear_undo()
        self._base_report = report
        self._apply_report(report)
        self._after_new_report()
        n_skills = len(report.skill_results) if report.skill_results else 0
        msg = (f"Done. {report.summary.coverage_loss_count} coverage-loss "
               f"faults. {n_skills} skill(s) ran.")
        reuse = (getattr(report, "sources", None) or {}).get("analysis_cache")
        if reuse and reuse.get("reused_mappings"):
            msg += (f"  Reused {reuse['reused_mappings']:,} fault mapping(s) "
                    f"from an earlier run on this netlist.")
        if getattr(report, "tool_evidence", None) is not None:
            msg += (f"  Tessent reports read; "
                    f"{report.tool_evidence.disagreements} disagreement(s).")
        if self._pending_save_path:
            if self._auto_save_report(report, self._pending_save_path):
                msg += f"  Saved: {self._pending_save_path}"
            self._pending_save_path = None
        self.statusBar().showMessage(msg)
        self._notify_done(
            f"Analysis finished: {report.summary.coverage_loss_count:,} "
            "coverage-loss faults.", "ok")

    def _on_multi_finished(self, results) -> None:
        self.progress.setVisible(False)
        self._set_running(False)
        self._clear_undo()
        self._partitions = [
            {"name": name, "report": rep, "base_report": rep}
            for name, rep in results
        ]
        self._active_idx = -1
        self._populate_partition_selector()
        if self._partitions:
            self._set_active_partition(0)
            self._after_new_report()
        total_loss = sum(
            rep.summary.coverage_loss_count for _, rep in results)
        msg = (f"Done. {len(results)} partition(s), {total_loss} total "
               f"coverage-loss faults. Use 'Active partition' to switch views.")
        if self.autosave_check.isChecked() and results:
            saved = self._auto_save_partitions(results)
            if saved:
                msg += (f"  Auto-saved {saved} report(s) to "
                        f"{self._autosave_dir()}.")
        self.statusBar().showMessage(msg)
        self._notify_done(f"Analysis finished: {len(results)} partition(s).",
                          "ok")

    def _reset_partitions(self) -> None:
        """Clear analyzed-partition state and hide the selector (single run)."""
        self._partitions = []
        self._active_idx = -1
        self.partition_selector.blockSignals(True)
        self.partition_selector.clear()
        self.partition_selector.blockSignals(False)
        self.partition_selector.setVisible(False)
        self.partition_selector_label.setVisible(False)

    def _populate_partition_selector(self) -> None:
        self.partition_selector.blockSignals(True)
        self.partition_selector.clear()
        for p in self._partitions:
            self.partition_selector.addItem(
                f"{p['name']}  "
                f"({p['report'].summary.coverage_loss_count} loss)")
        self.partition_selector.blockSignals(False)
        show = len(self._partitions) >= 1
        self.partition_selector.setVisible(show)
        self.partition_selector_label.setVisible(show)

    def _set_active_partition(self, idx: int) -> None:
        if not (0 <= idx < len(self._partitions)):
            return
        self._active_idx = idx
        self.partition_selector.blockSignals(True)
        self.partition_selector.setCurrentIndex(idx)
        self.partition_selector.blockSignals(False)
        p = self._partitions[idx]
        self._base_report = p["base_report"]
        self._apply_report(p["report"])

    def _on_partition_selected(self, idx: int) -> None:
        self._clear_undo()
        self._set_active_partition(idx)

    def _apply_report(self, report: AnalysisReport) -> None:
        """Populate all views from *report* (shared by Analyze and Load)."""
        self._report = report
        self._results = report.fault_results
        # Keep the active partition's stored report in sync so edits/waivers
        # survive switching between partitions.
        if 0 <= self._active_idx < len(self._partitions):
            p = self._partitions[self._active_idx]
            p["report"] = report
            self.partition_selector.blockSignals(True)
            self.partition_selector.setItemText(
                self._active_idx,
                f"{p['name']}  ({report.summary.coverage_loss_count} loss)")
            self.partition_selector.blockSignals(False)
        self._set_export_enabled(True)
        self._set_has_report(True)
        self._populate(report)
        if report.skill_results:
            self.skills_panel.show_results(report.skill_results)
        self.agent_panel.set_report(report, self._skill_manager)
        # Restore a saved agent investigation (or clear stale agent output).
        self.agent_panel.import_investigation(getattr(report, "investigation",
                                                      None))
        self._gs_mark("analyze")
        self._update_status_chips()
        if theme.is_dark():
            theme.refresh(self)

    def _on_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self._set_running(False)
        if not self.isActiveWindow():
            QApplication.alert(self)
        self._error(f"Analysis failed:\n{message}")
        self.statusBar().showMessage("Analysis failed.")

    def _on_agent_findings(self, findings: list) -> None:
        """Carry the agent's structured findings into the report.

        The Summary page renders section 9.2 from ``report.investigation``,
        so it is refreshed here; the offline values themselves are untouched.
        """
        report = self._report
        if report is None:
            return
        current = dict(getattr(report, "investigation", None) or {})
        if list(current.get("findings") or []) == list(findings):
            return
        current["findings"] = list(findings)
        report.investigation = current
        self._populate_summary(report)
        if findings:
            self.statusBar().showMessage(
                f"Agent recorded {len(findings)} structured finding(s) — "
                "see Summary section 9.2 and the Agent Tool Trace.")

    def _on_agent_tool_evidence(self, data: dict) -> None:
        """Fold what the agent measured in live Tessent into the report.

        Only the report-side views are refreshed; the agent panel keeps its
        conversation.
        """
        from ..analysis.tool_evidence import (ToolEvidence, apply_tool_evidence,
                                              merge_evidence)
        incoming = ToolEvidence.from_dict(data)
        for report in {id(r): r for r in (self._report, self._base_report)
                       if r is not None}.values():
            current = getattr(report, "tool_evidence", None)
            apply_tool_evidence(report, merge_evidence(
                ToolEvidence.from_dict(current.as_dict()) if current else None,
                ToolEvidence.from_dict(incoming.as_dict())))
        if self._report is None:
            return
        self.triage_panel.set_report(self._report)
        self._populate_summary(self._report)
        self._populate_logs(self._report)
        ev = self._report.tool_evidence
        self.statusBar().showMessage(
            f"Live Tessent measurement folded in: {len(ev.analyses)} "
            f"analyze_fault sample(s); {ev.disagreements} disagreement(s) with "
            "the offline estimate. See Summary and Triage.")

    def _on_agent_fix_edits(self, edits: list) -> None:
        """Overlay the agent's fix-plan edits onto the Triage tab and Summary.

        The offline plan on the report is untouched; the edits live in
        ``report.investigation`` and every renderer applies them on read.
        """
        report = self._report
        if report is None:
            return
        current = dict(getattr(report, "investigation", None) or {})
        if list(current.get("fix_plan_edits") or []) == list(edits):
            return
        current["fix_plan_edits"] = list(edits)
        report.investigation = current
        self.triage_panel.refresh_fix_plan()
        self._populate_summary(report)
        if edits:
            self.statusBar().showMessage(
                f"Agent contributed {len(edits)} fix-plan edit(s) — see "
                "Triage & Fix Plan > Fix Plan and Summary section 5.")

    def _cleanup_thread(self) -> None:
        self._thread = None
        self._worker = None

    def _populate(self, report: AnalysisReport) -> None:
        self._populate_summary(report)
        self.triage_panel.set_report(report)
        self._populate_table(report)
        self._populate_patterns(report)
        self._populate_logs(report)

    def _populate_summary(self, report: AnalysisReport) -> None:
        try:
            html = self._build_report_html(report)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to build HTML report: %s", exc)
            self._set_summary_html(
                f"<body><h2>Report generation failed</h2><pre>{exc}</pre></body>")
            return
        self._report_html = html
        self._set_summary_html(html)
        self.open_browser_btn.setEnabled(True)
        try:
            theme.set_label(self.dashboard, self._dashboard_html(report))
            self.dashboard.setVisible(True)
        except Exception:  # noqa: BLE001 - the full report is still shown
            logger.exception("Could not build the summary dashboard")
            self.dashboard.setVisible(False)
        self._update_inputs_summary(report)

    def _build_report_html(self, report: AnalysisReport,
                           category_dumps=None) -> str:
        """Render the HTML report for *report*.

        Args:
            report: The report to render.
            category_dumps: Per-category fault dumps to link from the triage
                section. Only pass these when the dump files sit beside the
                HTML being written, otherwise the links would be dead.
        """
        # Prefer the current file pickers; fall back to source metadata stored
        # in the report (so a loaded report still shows its cover header).
        sources = getattr(report, "sources", None) or {}
        netlist = self.netlist_picker.path() or sources.get("netlist") or ""
        faults = self.faults_picker.path() or sources.get("faults") or ""
        constraints = (self.constraints_picker.path()
                       or sources.get("constraints") or "")
        design = None
        if netlist:
            base = os.path.basename(netlist)
            for ext in (".v.gz", ".gz", ".v"):
                if base.endswith(ext):
                    base = base[: -len(ext)]
                    break
            design = base or None
        if not design:
            design = sources.get("design")
        edits = getattr(report, "edits", None) or {}
        note_parts = []
        banner = report_edit.edit_banner(edits)
        if banner:
            note_parts.append(f"Edits applied — {banner}.")
        if edits.get("note"):
            note_parts.append(edits["note"])
        analyst_note = "\n".join(note_parts) if note_parts else None
        return build_html_report(
            report,
            design_name=design,
            netlist_path=netlist or None,
            faults_path=faults or None,
            constraints_path=constraints or None,
            analyst_note=analyst_note,
            category_dumps=category_dumps,
        )

    def _populate_table(self, report: AnalysisReport) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for r in report.fault_results:
            row = self.table.rowCount()
            self.table.insertRow(row)
            # Unmapped objects have no measured connectivity: show "?" rather
            # than "0", which would read as "proven to have no fan-in/out".
            fan_in = r.fan_in_count
            fan_out = r.fan_out_count
            values = [
                r.fault.fault_object, r.fault.fault_class.value,
                r.mapping.instance_name or "—", r.mapping.confidence.value,
                r.instance_name or "—", r.cell_type or "—",
                "?" if fan_in is None else str(fan_in),
                "?" if fan_out is None else str(fan_out),
                "yes" if r.controllability_issue else "no",
                "yes" if r.observability_issue else "no",
                "yes" if r.constraint_related else "no",
                r.scan_boundary_state,
                r.root_cause.value,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col == 0:
                    item.setData(Qt.UserRole, row)
                    item.setToolTip(val)
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self._apply_filter()

    def _populate_patterns(self, report: AnalysisReport) -> None:
        self.patterns_table.setRowCount(0)
        for g in report.pattern_groups:
            row = self.patterns_table.rowCount()
            self.patterns_table.insertRow(row)
            for col, val in enumerate([g.kind, g.key, str(g.count),
                                        ", ".join(g.sample_faults)]):
                self.patterns_table.setItem(row, col, QTableWidgetItem(val))

    def _populate_logs(self, report: AnalysisReport) -> None:
        lines = []
        if report.warnings:
            lines.extend(f"- {w}" for w in report.warnings)
        if report.skill_results:
            skill_warnings = []
            for sr in report.skill_results:
                for msg in sr.warnings:
                    skill_warnings.append(f"[{sr.skill_id}] {msg}")
            if skill_warnings:
                lines.append("")
                lines.append("=== Skill Warnings ===")
                lines.extend(skill_warnings)
        self.logs_view.setPlainText("\n".join(lines) if lines else "No warnings.")

    def _apply_filter(self) -> None:
        text = self.filter_text.text().strip().lower()
        cls = self.class_filter.currentData() or "all"
        conf = self.conf_filter.currentText()
        for row in range(self.table.rowCount()):
            visible = True
            row_class = self.table.item(row, 1).text()
            row_conf = self.table.item(row, 3).text()
            if cls != "all" and row_class != cls:
                visible = False
            if conf != "all" and row_conf != conf:
                visible = False
            if visible and text:
                joined = " ".join(
                    self.table.item(row, c).text().lower() for c in (0, 4, 12))
                if text not in joined:
                    visible = False
            self.table.setRowHidden(row, not visible)

    def _on_row_selected(self) -> None:
        items = self.table.selectedItems()
        if not items:
            return
        row = items[0].row()
        idx_item = self.table.item(row, 0)
        idx = idx_item.data(Qt.UserRole)
        if idx is None or idx >= len(self._results):
            return
        self.details.show_result(self._results[idx])

    def _on_table_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        id_item = self.table.item(item.row(), 0)
        if id_item is None:
            return
        fault_object = id_item.text()
        menu = QMenu(self)
        ask_act = menu.addAction("Ask AI agent about this fault")
        inspect_act = menu.addAction("Inspect in Tessent Visualizer")
        show_act = menu.addAction("Open signal in Tessent Visualizer")
        rows = {idx.row() for idx in self.table.selectionModel().selectedRows()}
        rows.add(item.row())
        exclude_act = None
        if self._base_report:
            label = ("Exclude selected fault(s) from report" if len(rows) > 1
                     else "Exclude this fault from report")
            exclude_act = menu.addAction(label)
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == ask_act:
            self._switch_to_tab("AI Debug Agent")
            self.agent_panel.ask_about_fault(fault_object)
        elif chosen == inspect_act:
            self._inspect_fault_in_visualizer(item.row(), fault_object)
        elif chosen == show_act:
            self._show_signal_in_visualizer(fault_object)
        elif exclude_act is not None and chosen == exclude_act:
            if not self.table.selectionModel().isRowSelected(
                    item.row(), self.table.rootIndex()):
                self.table.selectRow(item.row())
            self._exclude_selected_faults()

    def _inspect_fault_in_visualizer(self, row: int, fault_object: str) -> None:
        """Queue this fault's inspection commands on the Visualizer tab."""
        stuck = ""
        idx_item = self.table.item(row, 0)
        idx = idx_item.data(Qt.UserRole) if idx_item is not None else None
        if idx is not None and idx < len(self._results):
            fault = getattr(self._results[idx], "fault", None)
            stuck = str(getattr(fault, "fault_type", "") or "")
        self._switch_to_tab("Tessent Visualizer")
        if self.visualizer_panel.add_fault_commands(fault_object, stuck):
            self.statusBar().showMessage(
                f"Added {fault_object} to the Visualizer command list.")

    def _sync_visualizer_config(self) -> None:
        """Record the viewer form on the report, so it survives a save."""
        config = self.visualizer_panel.export_settings()
        has_paths = any(config.get("paths", {}).values())
        for report in (self._report, self._base_report):
            if report is not None:
                report.visualizer_config = config if has_paths else None

    def _show_signal_in_visualizer(self, obj: str) -> None:
        """Display *obj* in the running viewer, via the live control channel."""
        if not self.visualizer_panel.show_signal(obj):
            # The panel holds the reason; surface it where the user is looking.
            self._switch_to_tab("Tessent Visualizer")
            self.statusBar().showMessage(
                self.visualizer_panel.status_label.text())

    def _switch_to_tab(self, title: str) -> None:
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).replace("&&", "&") == title:
                self.tabs.setTabVisible(i, True)
                self.tabs.setCurrentIndex(i)
                return

    # ------------------------------------------------------------------
    # Layout: inputs, partitions, advanced tabs, dashboard
    # ------------------------------------------------------------------
    def _set_partitions_visible(self, visible: bool) -> None:
        self.partition_box.setVisible(visible)
        self.partitions_toggle.setText(
            "− Hide the partition queue" if visible
            else "+ Analyze several partitions…")

    def _set_inputs_collapsed(self, collapsed: bool) -> None:
        """Fold the input files into one summary line (or show them again)."""
        self._inputs_expanded = not collapsed
        has_report = self._report is not None
        self.inputs_summary.setVisible(has_report)
        self.inputs_box.setVisible(not (collapsed and has_report))
        self.change_inputs_btn.setText(
            "Hide inputs \u25b4" if self._inputs_expanded
            else "Change inputs \u25be")

    def _toggle_inputs(self) -> None:
        self._set_inputs_collapsed(self._inputs_expanded)

    def _update_inputs_summary(self, report: AnalysisReport) -> None:
        sources = getattr(report, "sources", None) or {}
        netlist = sources.get("netlist") or self.netlist_picker.path() or ""
        faults = sources.get("faults") or self.faults_picker.path() or ""
        constraints = (sources.get("constraints")
                       or self.constraints_picker.path() or "")
        design = sources.get("design") or _design_name(netlist) or "design"
        parts = [f"<b>{_html_escape(design)}</b>"]
        if netlist:
            parts.append("netlist: " + _html_escape(os.path.basename(netlist)))
        if faults:
            parts.append("faults: " + _html_escape(os.path.basename(faults)))
        parts.append("constraints: " + (
            _html_escape(os.path.basename(constraints)) if constraints
            else "<i>none</i>"))
        parts.append(f"{report.summary.total_faults:,} faults analysed")
        self.inputs_summary_label.setText("  ·  ".join(parts))
        self.inputs_summary_label.setToolTip(
            "\n".join(p for p in (netlist, faults, constraints) if p))

    def _after_new_report(self) -> None:
        """A fresh analysis or a loaded report: give the results the screen."""
        self._set_inputs_collapsed(True)
        self.demo_btn.setVisible(False)
        self._switch_to_tab("Summary")
        if not self._settings.tour_done:
            self.tour_bar.start()

    def _set_advanced_tabs_visible(self, visible: bool) -> None:
        for widget in self._advanced_tabs:
            idx = self.tabs.indexOf(widget)
            if idx >= 0:
                self.tabs.setTabVisible(idx, visible)
        action = getattr(self, "advanced_tabs_action", None)
        if action is not None and action.isChecked() != visible:
            action.setChecked(visible)

    def _on_advanced_tabs_toggled(self, visible: bool) -> None:
        self._set_advanced_tabs_visible(visible)
        self._save_settings()

    def on_try_demo(self) -> None:
        """Fill in the bundled example design and analyze it."""
        demo = self.load_demo_inputs()
        if demo is None:
            self._error("The demo data is not installed with this copy.")
            return
        self._queued = []
        self.partition_list.clear()
        self.on_analyze()

    def load_demo_inputs(self) -> Optional[tuple]:
        demo = _demo_inputs()
        if demo is None:
            return None
        netlist, faults, constraints = demo
        self.netlist_picker.set_path(netlist)
        self.faults_picker.set_path(faults)
        self.constraints_picker.set_path(constraints)
        self.statusBar().showMessage("Demo inputs filled in.")
        return demo

    def _dashboard_html(self, report: AnalysisReport) -> str:
        """The at-a-glance box at the top of the Summary tab."""
        summary = report.summary
        stats = getattr(report, "statistics", None)
        metrics = stats.metrics() if stats is not None else {}

        def _pct(value) -> str:
            return "n/a" if value is None else f"{value:.2f}%"

        def _card(value: str, label: str) -> str:
            return ("<td style='padding:2px 22px 2px 0;'>"
                    f"<span style='font-size:20px; font-weight:bold; "
                    f"color:#0b5394;'>{value}</span><br>"
                    f"<span style='color:#555;'>{label}</span></td>")

        loss = summary.coverage_loss_count
        from ..analysis.tool_evidence import measured_metrics
        measured = measured_metrics(report)
        if measured.get("test_coverage") is not None:
            coverage_cards = [
                _card(_pct(measured["test_coverage"]),
                      "Test coverage <b>(measured by Tessent)</b>"),
                _card(_pct(metrics.get("test_coverage")),
                      "Test coverage computed from the fault list"),
            ]
        else:
            coverage_cards = [
                _card(_pct(metrics.get("test_coverage")), "Test coverage"),
                _card(_pct(metrics.get("fault_coverage")), "Fault coverage"),
            ]
        cards = coverage_cards + [
            _card(f"{loss:,}", f"Coverage-loss faults (of "
                  f"{summary.total_faults:,})"),
            _card(f"{summary.actionable_loss_count:,}",
                  "Actionable loss (mapped, not tied off)"),
        ]
        warnings = len(report.warnings or [])
        if warnings:
            cards.append(_card(
                f"<a href='tab:logs' style='color:#b35900;'>{warnings}</a>",
                "<a href='tab:logs'>warnings &mdash; view</a>"))
        html = ["<table cellspacing='0'><tr>" + "".join(cards) + "</tr></table>"]

        try:
            from ..analysis.fix_plan_edits import effective_plan
            plan, seen = [], set()
            for rec in effective_plan(report):
                if getattr(rec, "superseded", False) or rec.subclass_id in seen:
                    continue
                seen.add(rec.subclass_id)
                plan.append(rec)
                if len(plan) == 3:
                    break
        except Exception:  # noqa: BLE001 - the dashboard must never break a load
            plan = []
        if plan:
            html.append("<p style='margin:10px 0 2px;'><b>Where to start</b> "
                        "&mdash; the biggest categories and the first fix "
                        "the Fix Plan proposes for each:</p><ol "
                        "style='margin:0;'>")
            for rec in plan:
                sub = _html_escape(rec.subclass_id)
                title = _html_escape(getattr(rec.fix, "title", "") or "")
                html.append(
                    f"<li><a href='cat:{sub}'><b>{sub}</b></a> &mdash; "
                    f"{rec.fault_count:,} faults ({rec.pct:.1f}%): {title} "
                    f"<span style='color:#666;'>(worth acting on: "
                    f"{_html_escape(rec.actionable)})</span></li>")
            html.append("</ol>")
        elif loss == 0:
            html.append("<p style='margin:10px 0 2px;'><b>No coverage-loss "
                        "faults</b> in this fault list.</p>")
        html.append(
            "<p style='margin:10px 0 0;'><b>Next:</b> "
            "<a href='tab:triage'>Open Triage &amp; Fix Plan</a> &nbsp;·&nbsp; "
            "<a href='tab:table'>Browse every fault</a> &nbsp;·&nbsp; "
            "<a href='tab:agent'>Ask the AI agent</a> &nbsp;·&nbsp; "
            "<a href='browser'>Full report in the browser</a>"
            "<br><span style='color:#666;'>The complete report follows "
            "below.</span></p>")
        return "".join(html)

    def _on_dashboard_link(self, href: str) -> None:
        if href == "browser":
            self.on_open_report_in_browser()
            return
        if href.startswith("cat:"):
            self._show_category(href[4:])
            return
        target = {"tab:triage": self.triage_panel, "tab:table": self._table_tab,
                  "tab:agent": self._agent_tab,
                  "tab:logs": self._logs_tab}.get(href)
        if target is not None:
            idx = self.tabs.indexOf(target)
            self.tabs.setTabVisible(idx, True)
            self.tabs.setCurrentIndex(idx)

    def _show_category(self, subclass: str) -> None:
        """Open the Triage tab on *subclass*'s row."""
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.triage_panel))
        self.triage_panel.tabs.setCurrentIndex(0)
        table = self.triage_panel.category_table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.text() == subclass:
                table.selectRow(row)
                table.scrollToItem(item)
                return

    def _focus_fault_in_table(self, fault_object: str) -> None:
        """Select and reveal the table row for *fault_object* (from an agent link)."""
        self._switch_to_tab("Coverage Loss Table")
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == fault_object:
                self.table.clearSelection()
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                self.table.setFocus()
                return
        self.statusBar().showMessage(
            f"'{fault_object}' is not a row in the current table.")

    def _default_path(self, name: str) -> str:
        outdir = self.outdir_picker.path()
        if outdir and os.path.isdir(outdir):
            return os.path.join(outdir, name)
        return name

    def on_open_report_in_browser(self) -> None:
        if not self._report_html:
            return
        outdir = self.outdir_picker.path()
        try:
            if outdir and os.path.isdir(outdir):
                serve_dir = outdir
            else:
                serve_dir = tempfile.mkdtemp(prefix="atpg_report_")
            path = os.path.join(serve_dir, "atpg_coverage_report.html")
            # Write the per-category fault files alongside the HTML first, so
            # the links the report emits actually resolve for the reader.
            html = self._report_html
            if self._report is not None:
                try:
                    dumps = write_category_dumps(self._report, path)
                    if dumps:
                        html = self._build_report_html(self._report, dumps)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Category dumps not written: %s", exc)
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
        except OSError as exc:
            self._error(f"Could not write the HTML report:\n{exc}")
            return

        # Publish the report over HTTP so the link can be shared with others.
        try:
            port = self._ensure_report_server(serve_dir)
        except OSError as exc:
            logger.warning("Could not start report server: %s", exc)
            port = None

        if port is not None:
            host = socket.getfqdn()
            share_url = f"http://{host}:{port}/atpg_coverage_report.html"
        else:
            # Fall back to a local-only file URL if the server failed to start.
            share_url = QUrl.fromLocalFile(path).toString()

        firefox = shutil.which("firefox")
        opened = False
        if firefox:
            try:
                subprocess.Popen(
                    [firefox, share_url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                opened = True
            except OSError:
                pass  # fall back to the default browser below

        if not opened:
            opened = QDesktopServices.openUrl(QUrl(share_url))

        if not opened:
            self._error(
                "Could not open Firefox or any web browser automatically. The "
                f"report is available at:\n{share_url}")
            return

        self.statusBar().showMessage(f"Report opened in browser: {share_url}")
        if port is not None:
            self._show_share_dialog(share_url)
        else:
            self.notify(
                "The report was opened locally, but a shareable network link "
                f"could not be created. Local file: {path}", "warn",
                msec=10000)

    def _ensure_report_server(self, directory: str) -> int:
        """Start (or reuse) a background HTTP server serving ``directory``.

        Returns the TCP port the server is listening on. If a server is
        already running for a different directory it is restarted so the
        shared link always points at the freshest report.
        """
        directory = os.path.abspath(directory)
        if self._report_server is not None:
            if self._report_server_dir == directory:
                return self._report_server.server_address[1]
            self._shutdown_report_server()

        handler = partial(_QuietHTTPRequestHandler, directory=directory)
        # Bind to all interfaces on an ephemeral port so peers can reach it.
        server = ThreadingHTTPServer(("0.0.0.0", 0), handler)
        thread = threading.Thread(
            target=server.serve_forever, name="atpg-report-server", daemon=True)
        thread.start()

        self._report_server = server
        self._report_server_thread = thread
        self._report_server_dir = directory
        return server.server_address[1]

    def _shutdown_report_server(self) -> None:
        if self._report_server is not None:
            try:
                self._report_server.shutdown()
                self._report_server.server_close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        self._report_server = None
        self._report_server_thread = None
        self._report_server_dir = None

    def _show_share_dialog(self, url: str) -> None:
        """Show the shareable report link with a one-click copy button."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Share Report Link")
        layout = QVBoxLayout(dialog)

        label = QLabel(
            "The report is now published on this machine. Share the link "
            "below so other users on the network can view it:")
        label.setWordWrap(True)
        layout.addWidget(label)

        row = QHBoxLayout()
        link_edit = QLineEdit(url)
        link_edit.setReadOnly(True)
        link_edit.selectAll()
        row.addWidget(link_edit, 1)

        copy_btn = QPushButton("Copy Link")

        def _copy() -> None:
            QApplication.clipboard().setText(url)
            copy_btn.setText("Copied!")
            self.statusBar().showMessage(f"Report link copied: {url}")

        copy_btn.clicked.connect(_copy)
        row.addWidget(copy_btn)
        layout.addLayout(row)

        note = QLabel(
            "<i>The link stays active while this application is running.</i>")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.resize(520, dialog.sizeHint().height())
        dialog.exec()

    def on_export_md(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Markdown report", self._default_path("atpg_report.md"), "Markdown (*.md)")
        if not path:
            return
        try:
            write_markdown(self._report, path)
        except OSError as exc:
            self._error(f"Could not write Markdown report:\n{exc}")
            return
        self.statusBar().showMessage(f"Markdown report saved: {path}")

    def on_export_category_faults(self) -> None:
        """Write one CSV and one JSON per selected category into a folder.

        Operates on the active partition only, like the other exports.
        """
        if not self._report:
            return
        if not (getattr(self._report, "selected_categories", None) or []):
            self._error(
                "This report has no coverage-loss categories selected for "
                "investigation, so there is nothing to export.")
            return
        outdir = self.outdir_picker.path()
        start = outdir if outdir and os.path.isdir(outdir) else ""
        directory = QFileDialog.getExistingDirectory(
            self, "Choose a folder for the category fault files", start)
        if not directory:
            return
        base = os.path.join(directory, "atpg_report")
        try:
            dumps = write_category_dumps(self._report, base)
        except OSError as exc:
            self._error(f"Could not write the category fault files:\n{exc}")
            return
        files = sum(1 for d in dumps for n in (d.csv_name, d.json_name) if n)
        self.statusBar().showMessage(
            f"{len(dumps)} category file set(s), {files} files, written to "
            f"{dump_dir_for(base)}")

    def on_export_csv(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV report", self._default_path("atpg_report.csv"), "CSV (*.csv)")
        if not path:
            return
        try:
            write_csv(self._report, path)
        except OSError as exc:
            self._error(f"Could not write CSV report:\n{exc}")
            return
        self.statusBar().showMessage(f"CSV report saved: {path}")

    def on_export_filtered_csv(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save filtered CSV", self._default_path("atpg_filtered.csv"), "CSV (*.csv)")
        if not path:
            return
        visible_results = []
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                idx_item = self.table.item(row, 0)
                idx = idx_item.data(Qt.UserRole)
                if idx is not None and idx < len(self._results):
                    visible_results.append(self._results[idx])
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv_mod.writer(f)
                writer.writerow(_TABLE_HEADERS)
                for r in visible_results:
                    writer.writerow([
                        r.fault.fault_object, r.fault.fault_class.value,
                        r.mapping.instance_name or "", r.mapping.confidence.value,
                        r.instance_name or "", r.cell_type or "",
                        len(r.fan_in), len(r.fan_out),
                        r.controllability_issue, r.observability_issue,
                        r.constraint_related, r.scan_boundary_involved,
                        r.root_cause.value,
                    ])
        except OSError as exc:
            self._error(f"Could not write filtered CSV:\n{exc}")
            return
        self.statusBar().showMessage(
            f"Filtered CSV saved: {path} ({len(visible_results)} rows)")

    def on_clear(self) -> None:
        self._report = None
        self._base_report = None
        self._results = []
        self._report_html = None
        self._reset_partitions()
        self.table.setRowCount(0)
        self.patterns_table.setRowCount(0)
        self.triage_panel.clear()
        self._clear_summary()
        self.open_browser_btn.setEnabled(False)
        self.logs_view.clear()
        self.details.clear_details()
        self.skills_panel.clear_results()
        self.agent_panel.clear()
        self._set_export_enabled(False)
        self._set_has_report(False)
        self.tour_bar.stop()
        self._set_inputs_collapsed(False)
        self._clear_undo()
        self._update_status_chips()
        self.statusBar().showMessage("Cleared.")

    def _save_settings(self) -> None:
        s = self._settings
        s.last_netlist = self.netlist_picker.path()
        s.last_faults = self.faults_picker.path()
        s.last_constraints = self.constraints_picker.path()
        s.last_output_dir = self.outdir_picker.path()
        s.last_tool_reports = self._last_tool_reports
        s.filter_text = self.filter_text.text()
        s.class_filter = self.class_filter.currentData() or "all"
        s.conf_filter = self.conf_filter.currentText()
        s.update_skills(self._skill_manager.to_config())
        s.agent = self.agent_panel.export_settings()
        s.visualizer = self.visualizer_panel.export_settings()
        self._sync_visualizer_config()
        s.custom_skills_dir = self.custom_skills_panel.custom_dir()
        s.auto_save_report = self.autosave_check.isChecked()
        s.show_advanced_tabs = bool(
            getattr(self, "advanced_tabs_action", None)
            and self.advanced_tabs_action.isChecked())
        s.save()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._chip_timer.stop()
        self.save_layout()
        self._save_settings()
        self._shutdown_report_server()
        # The agent panel owns background threads (CLI model fetch, agent run,
        # chat turn). Qt aborts the process if one is still running when it is
        # destroyed, so drain them before the window goes away.
        self.agent_panel.shutdown()
        super().closeEvent(event)

    def _set_export_enabled(self, enabled: bool) -> None:
        self.md_action.setEnabled(enabled)
        self.csv_action.setEnabled(enabled)
        self.export_btn.setEnabled(enabled)
        self.save_report_btn.setEnabled(enabled)
        self.compare_action.setEnabled(enabled)
        self.edit_action.setEnabled(enabled)
        self.tool_reports_action.setEnabled(enabled)

    def on_edit_report(self) -> None:
        if not self._base_report:
            return
        current_edits = getattr(self._report, "edits", None) or {}
        ex_classes = set(current_edits.get("excluded_classes", []))
        ex_subtypes = {s.upper() for s in current_edits.get("excluded_subtypes", [])}
        ex_ids = list(current_edits.get("excluded_ids", []))
        note = current_edits.get("note", "")

        # Loss subtypes present in the *base* report, with their fault counts.
        subtype_counts = self._loss_subtype_counts(self._base_report)

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Report")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "Waive coverage-loss faults and/or record an analyst note. Excluded "
            "faults are removed from the totals so the coverage metric recomputes; "
            "the report layout is unchanged. Edits are reversible and saved with "
            "the report."))

        # --- Whole coarse classes -------------------------------------------
        class_box = QGroupBox("Exclude whole fault classes")
        class_layout = QVBoxLayout(class_box)
        class_checks = {}
        for cls in ("AU", "UO", "UC"):
            cb = QCheckBox(f"Exclude all {cls} faults")
            cb.setChecked(cls in ex_classes)
            class_layout.addWidget(cb)
            class_checks[cls] = cb
        v.addWidget(class_box)

        # --- Specific subtypes (e.g. AU.NOFAULTS) ---------------------------
        subtype_checks = {}
        if subtype_counts:
            sub_box = QGroupBox("Exclude specific fault subtypes")
            sub_outer = QVBoxLayout(sub_box)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            inner = QWidget()
            inner_layout = QVBoxLayout(inner)
            for token, count in subtype_counts:
                cb = QCheckBox(f"{token}  ({count} fault{'s' if count != 1 else ''})")
                cb.setChecked(token.upper() in ex_subtypes)
                inner_layout.addWidget(cb)
                subtype_checks[token] = cb
            inner_layout.addStretch(1)
            scroll.setWidget(inner)
            scroll.setMinimumHeight(120)
            sub_outer.addWidget(scroll)
            v.addWidget(sub_box, 1)

        # --- Specific faults by id / path -----------------------------------
        v.addWidget(QLabel("Exclude specific faults by object path (one per line):"))
        ids_edit = QPlainTextEdit()
        ids_edit.setPlainText("\n".join(ex_ids))
        ids_edit.setPlaceholderText("/top/u_seq/u_opt_900/o")
        ids_edit.setMaximumHeight(80)
        v.addWidget(ids_edit)

        v.addWidget(QLabel("Analyst note / annotation:"))
        note_edit = QPlainTextEdit()
        note_edit.setPlainText(note)
        note_edit.setPlaceholderText(
            "e.g. AU.NOFAULTS reviewed and waived as legitimately untestable "
            "(black-box RAM boundary).")
        note_edit.setMaximumHeight(80)
        v.addWidget(note_edit, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)
        dlg.resize(560, 560)
        if dlg.exec() != QDialog.Accepted:
            return

        excluded = [cls for cls, cb in class_checks.items() if cb.isChecked()]
        excluded_subtypes = [tok for tok, cb in subtype_checks.items()
                             if cb.isChecked()]
        excluded_ids = [ln.strip() for ln in ids_edit.toPlainText().splitlines()
                        if ln.strip()]
        new_note = note_edit.toPlainText().strip()
        edited = report_edit.apply_exclusions(
            self._base_report, excluded_classes=excluded,
            excluded_subtypes=excluded_subtypes, excluded_ids=excluded_ids,
            note=new_note)
        self._push_undo()
        self._apply_report(edited)
        banner = report_edit.edit_banner(edited.edits)
        self.statusBar().showMessage(
            "Report edited" + (f": {banner}" if banner else " (note updated).")
            + f"  {edited.summary.coverage_loss_count} coverage-loss faults remain.")
        self._offer_undo("Report edited.")

    @staticmethod
    def _loss_subtype_counts(report: AnalysisReport) -> List[tuple]:
        """Return ``[(subtype_token, count), ...]`` for coverage-loss subtypes.

        Only dotted subtypes whose coarse class is a coverage-loss class
        (``AU`` / ``UO`` / ``UC``) are offered for waiving; they are sorted by
        descending count so the biggest contributors surface first.
        """
        from collections import Counter
        counts: Counter = Counter()
        for r in report.fault_results:
            cls = r.fault.fault_class.value
            if cls not in ("AU", "UO", "UC"):
                continue
            token = r.fault.raw_class_token or cls
            if "." in token:
                counts[token] += 1
        return counts.most_common()

    def _exclude_selected_faults(self) -> None:
        """Waive the faults selected in the Coverage Loss Table."""
        if not self._base_report:
            return
        objects = set()
        for item in self.table.selectedItems():
            if item.column() == 0:
                objects.add(item.text())
        if not objects:
            return
        current_edits = getattr(self._report, "edits", None) or {}
        ex_ids = set(current_edits.get("excluded_ids", [])) | objects
        edited = report_edit.apply_exclusions(
            self._base_report,
            excluded_classes=current_edits.get("excluded_classes", []),
            excluded_subtypes=current_edits.get("excluded_subtypes", []),
            excluded_ids=sorted(ex_ids),
            note=current_edits.get("note", ""))
        self._push_undo()
        self._apply_report(edited)
        self.statusBar().showMessage(
            f"Excluded {len(objects)} fault(s).  "
            f"{edited.summary.coverage_loss_count} coverage-loss faults remain.")
        self._offer_undo(f"Excluded {len(objects)} fault(s).")

    # ------------------------------------------------------------------
    # Undo for waivers
    # ------------------------------------------------------------------
    MAX_UNDO = 20

    def _push_undo(self) -> None:
        if self._report is None:
            return
        self._undo_stack.append(self._report)
        del self._undo_stack[:-self.MAX_UNDO]
        self.undo_action.setEnabled(True)

    def _clear_undo(self) -> None:
        self._undo_stack = []
        self.undo_action.setEnabled(False)

    def _offer_undo(self, text: str) -> None:
        self.notify(text, "ok", action="Undo", callback=self.undo_exclusion,
                    status=False)

    def undo_exclusion(self) -> bool:
        """Put back the report as it was before the last waiver."""
        if not self._undo_stack:
            self.notify("Nothing to undo.")
            return False
        previous = self._undo_stack.pop()
        self._apply_report(previous)
        self.undo_action.setEnabled(bool(self._undo_stack))
        self.notify(f"Waiver undone — {previous.summary.coverage_loss_count:,} "
                    "coverage-loss faults.", "ok")
        return True

    def on_compare_report(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load baseline report to compare (JSON)",
            self._default_path("atpg_report.json"),
            "ATPG report (*.json);;All files (*)")
        if not path:
            return
        try:
            baseline = load_report(path)
        except (OSError, ValueError) as exc:
            self._error(f"Could not load baseline report:\n{exc}")
            return
        label = os.path.basename(path)
        compare = investigate.serialize_report_for_compare(
            baseline.fault_results, baseline.summary, baseline.constraints,
            label=label)
        current = [investigate.serialize_fault_result(fr)
                   for fr in self._report.fault_results]
        summ = regression.summary(
            compare["faults"], current, compare.get("summary"),
            {"class_counts": dict(self._report.summary.class_counts)},
            label=label)
        c = summ["counts"]
        self.agent_panel.set_compare(compare)
        from ..analysis.fix_history import evaluate_fix_outcomes
        outcomes = evaluate_fix_outcomes(baseline, self._report)
        self._show_compare_dialog(label, c, outcomes)
        self._switch_to_tab("AI Debug Agent")
        self.statusBar().showMessage(
            f"Compared against {label}: +{c['regressed']} regressed, "
            f"-{c['fixed']} fixed, {c['changed']} changed.")

    def _show_compare_dialog(self, label: str, counts: dict,
                             outcomes: list) -> None:
        """Regression counts plus, per baseline fix, what the re-run measured."""
        from ..analysis import fix_history
        dlg = QDialog(self)
        dlg.setWindowTitle("Regression vs baseline")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"<b>Baseline:</b> {_html_escape(label)}<br>"
            f"Coverage-loss: {counts['baseline_loss']:,} \u2192 "
            f"{counts['current_loss']:,} (net {counts['net_delta']:+,})<br>"
            f"Regressed (new loss): {counts['regressed']:,} \u00b7 "
            f"Fixed: {counts['fixed']:,} \u00b7 Changed: {counts['changed']:,}"
            "<br><i>The AI agent can now answer \u201cwhat changed vs the "
            "baseline?\u201d with its regression tools.</i>"))
        v.addWidget(QLabel(
            "<b>Did the fixes work?</b> For each fix the baseline proposed, "
            "how its category changed. Tick the fixes you actually applied "
            "and save: future plans rank fixes by these measured outcomes."))
        table = QTableWidget(len(outcomes), 7)
        table.setHorizontalHeaderLabels(
            ["Applied?", "Category", "Fix", "Before", "After",
             "Left the loss", "Moved to another category"])
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for row, o in enumerate(outcomes):
            check = QTableWidgetItem("")
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Unchecked)
            table.setItem(row, 0, check)
            for col, value in enumerate(
                    [o.subclass, o.title, f"{o.before:,}", f"{o.after:,}",
                     f"{o.recovered:,} ({o.recovered_share:.0%})",
                     f"{o.moved:,}"], start=1):
                table.setItem(row, col, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        v.addWidget(table, 1)
        self._compare_outcomes = (outcomes, table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        save_btn = buttons.addButton("Save ticked outcomes to fix history",
                                     QDialogButtonBox.ActionRole)
        save_btn.setEnabled(bool(outcomes))
        note = QLabel("")
        v.addWidget(note)

        def _save() -> None:
            n = self.save_fix_outcomes()
            note.setText(f"Saved {n} outcome(s) to "
                         f"{fix_history.history_path()}." if n else
                         "Tick at least one fix you applied.")
        save_btn.clicked.connect(_save)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)
        dlg.resize(900, 460)
        dlg.exec()

    def save_fix_outcomes(self) -> int:
        """Record the ticked rows of the last comparison in the fix history."""
        from ..analysis import fix_history
        outcomes, table = getattr(self, "_compare_outcomes", ([], None))
        if table is None:
            return 0
        chosen = [o for row, o in enumerate(outcomes)
                  if table.item(row, 0).checkState() == Qt.Checked]
        design = (getattr(self._report, "sources", None) or {}).get("design", "")
        return fix_history.record_outcomes(chosen, design=design or "")

    def on_add_tool_reports(self) -> None:
        """Fold Tessent's own reports into the current results."""
        if not self._report:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Tessent report_statistics / analyze_fault output",
            self._last_tool_reports,
            "Tool reports (*.log *.txt *.rpt *.out *.gz);;All files (*)")
        if not path:
            return
        self.apply_tool_reports(path)

    def apply_tool_reports(self, path: str) -> None:
        from ..analysis.tool_evidence import (apply_tool_evidence,
                                              load_tool_evidence)
        evidence = load_tool_evidence(path)
        if not evidence.statistics and not evidence.analyses:
            self._error("No report_statistics table or analyze_fault entry "
                        "was recognised in:\n" + path)
            return
        self._last_tool_reports = path
        apply_tool_evidence(self._report, evidence)
        self._apply_report(self._report)
        self.statusBar().showMessage(
            f"Tessent reports added: {len(evidence.statistics)} statistics "
            f"table(s), {len(evidence.analyses)} analyze_fault sample(s); "
            f"{evidence.disagreements} disagreement(s) with this analysis.")

    def on_save_report(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save report (JSON)",
            self._default_path("atpg_report.json"), "ATPG report (*.json)")
        if not path:
            return
        # Capture the current agent investigation (diagnosis, chat, trace).
        self._report.investigation = self.agent_panel.export_investigation()
        try:
            save_report(self._report, path)
        except OSError as exc:
            self._error(f"Could not save report:\n{exc}")
            return
        self.statusBar().showMessage(f"Report saved: {path}")

    def on_load_report(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load report (JSON)",
            self._default_path("atpg_report.json"),
            "ATPG report (*.json);;All files (*)")
        if not path:
            return
        self.load_report_path(path)

    def load_report_path(self, path: str) -> bool:
        try:
            report = load_report(path)
        except (OSError, ValueError) as exc:
            self._error(f"Could not load report:\n{exc}")
            return False
        self._reset_partitions()
        self._clear_undo()
        self._base_report = report
        self._apply_report(report)
        self._after_new_report()
        self.statusBar().showMessage(
            f"Report loaded: {path} — "
            f"{report.summary.coverage_loss_count} coverage-loss faults. "
            "Work on it without re-analyzing.")
        return True

    # ------------------------------------------------------------------
    # Start-up sign-in check
    # ------------------------------------------------------------------
    def auto_check_sign_in(self) -> bool:
        """Confirm the saved GitHub token works, so the agent is ready."""
        started = self.agent_panel.auto_check_auth()
        if started:
            self.statusBar().showMessage(
                "Checking Copilot sign-in with the saved GitHub token…")
            self._update_status_chips()
        return started

    def _open_auth_tab(self) -> None:
        self._switch_to_tab("AI Debug Agent")
        self.agent_panel.tabs.setCurrentIndex(self.agent_panel.tabs.count() - 1)

    def _on_auto_auth(self, ok: bool, message: str) -> None:
        self._update_status_chips()
        if ok:
            self.notify(message, "ok")
        else:
            self.notify(message, "error", action="Fix sign-in",
                        callback=self._open_auth_tab, msec=15000)

    # ------------------------------------------------------------------
    # Notices
    # ------------------------------------------------------------------
    def notify(self, text: str, kind: str = "info", action: Optional[str] = None,
               callback=None, msec: int = 5000, status: bool = True) -> None:
        """A corner notice that fades by itself (errors still use a dialog)."""
        if status:
            self.statusBar().showMessage(text)
        self.toast.show_message(text, kind, action, callback, msec)

    def _notify_done(self, text: str, kind: str = "ok") -> None:
        """A long run ended: notice, and flash the task bar when unfocused."""
        self.notify(text, kind, status=False)
        if not self.isActiveWindow():
            QApplication.alert(self)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        toast = getattr(self, "toast", None)
        if toast is not None and toast.isVisible():
            toast.reposition()

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------
    def _build_status_chips(self) -> None:
        bar = self.statusBar()

        def _chip(tip: str, slot) -> QToolButton:
            btn = QToolButton()
            btn.setAutoRaise(True)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            bar.addPermanentWidget(btn)
            return btn

        self.report_chip = _chip("The loaded report \u2014 click for the "
                                 "Summary", lambda: self._switch_to_tab("Summary"))
        self.agent_chip = _chip("", lambda: self._switch_to_tab("AI Debug Agent"))
        self.vis_chip = _chip("", lambda: self._switch_to_tab("Tessent Visualizer"))
        self.theme_btn = _chip("Switch between the Light and Dark theme "
                               "(Ctrl+K, Ctrl+T)", self.toggle_theme)
        self._chip_timer = QTimer(self)
        self._chip_timer.setInterval(3000)
        self._chip_timer.timeout.connect(self._update_status_chips)
        self._chip_timer.start()
        self.visualizer_panel.status_message.connect(
            lambda *_: self._update_status_chips())
        self.agent_panel.config_changed.connect(self._update_status_chips)
        self._update_status_chips()

    def _update_status_chips(self) -> None:
        if getattr(self, "report_chip", None) is None:
            return
        report = self._report
        if report is None:
            self.report_chip.setText("No report loaded")
        else:
            sources = getattr(report, "sources", None) or {}
            design = (sources.get("design")
                      or _design_name(sources.get("netlist") or "") or "report")
            if len(design) > 22:
                design = design[:21] + "\u2026"
            text = f"{design} \u00b7 {report.summary.coverage_loss_count:,} loss"
            stats = getattr(report, "statistics", None)
            tc = stats.metrics().get("test_coverage") if stats is not None else None
            if tc is not None:
                text += f" \u00b7 TC {tc:.2f}%"
            self.report_chip.setText(text)
        try:
            ready, text, tip = self.agent_panel.readiness_summary()
        except Exception:  # noqa: BLE001 - a status hint must never break the UI
            ready, text, tip = False, "Agent", ""
        self.agent_chip.setText(("\u2713 " if ready else "\u25cb ") + text)
        self.agent_chip.setToolTip(tip + "\n\nClick to open the AI Debug Agent tab.")
        live = self.visualizer_panel.has_live_session()
        self.vis_chip.setText("\u25cf Visualizer: live" if live
                              else "\u25cb Visualizer: not open")
        self.vis_chip.setToolTip(
            "A Tessent Visualizer session is running; right-click a signal to "
            "show it there." if live else
            "No Tessent Visualizer session \u2014 click to launch one.")
        theme.set_css(self.vis_chip,
                      f"color: {theme.color('ok')};" if live else "")
        theme.set_css(self.agent_chip,
                      f"color: {theme.color('ok')};" if ready else "")
        self.theme_btn.setText("\u2600 Light" if theme.is_dark()
                               else "\u263e Dark")

    # ------------------------------------------------------------------
    # Theme and text size
    # ------------------------------------------------------------------
    def set_theme(self, name: str, *_args) -> None:
        name = theme.apply_app(QApplication.instance(), name)
        self._settings.theme = name
        for key, act in getattr(self, "theme_actions", {}).items():
            act.setChecked(key == name)
        self._refresh_theme_views()
        self._settings.save()
        self.statusBar().showMessage(
            f"{name.title()} theme. View \u2192 Theme or Ctrl+K, Ctrl+T "
            "switches back.")

    def toggle_theme(self, *_args) -> None:
        self.set_theme(theme.LIGHT if theme.is_dark() else theme.DARK)

    def _refresh_theme_views(self) -> None:
        theme.refresh(self)
        if self._report is not None:
            self.triage_panel.set_report(self._report)
            self._on_row_selected()
        self.agent_panel.refresh_theme()
        self.visualizer_panel.refresh_health()
        self.getting_started.restyle()
        self.toast.hide()
        self._update_status_chips()

    def set_font_delta(self, delta: int) -> int:
        delta = theme.apply_font(QApplication.instance(), delta)
        self._settings.font_delta = delta
        self._settings.save()
        self.statusBar().showMessage(
            "Text size: default" if delta == 0 else
            f"Text size: {delta:+d} pt (Ctrl+0 resets)")
        return delta

    def change_font(self, step: int) -> int:
        return self.set_font_delta(theme.font_delta() + step)

    # ------------------------------------------------------------------
    # Find, command palette, preferences
    # ------------------------------------------------------------------
    def on_find(self) -> None:
        current = self.tabs.currentWidget()
        if current is self._table_tab and self._report is not None:
            self.filter_text.setFocus()
            self.filter_text.selectAll()
            return
        target, where = None, ""
        if current is self._summary_tab:
            target, where = self.summary_view, "Summary"
        elif current is self.triage_panel:
            inner = self.triage_panel.tabs.currentIndex()
            if inner == 0:
                target, where = self.triage_panel.category_detail, "Category details"
            elif inner == 2:
                target, where = self.triage_panel.fix_detail, "Fix details"
        elif current is self._agent_tab:
            target, where = self.agent_panel.response_view, "Agent response"
        if target is None:
            self.notify("Find works on the Summary, the Triage Categories and "
                        "Fix Plan details, the Coverage Loss Table and the AI "
                        "agent's answer.")
            return
        self.find_bar.open_for(target, where)

    def open_command_palette(self) -> None:
        self.command_palette.open()

    def _menu_entries(self, menu: QMenu, path: str) -> List[PaletteEntry]:
        entries = []
        for act in menu.actions():
            if act.isSeparator() or not act.isVisible():
                continue
            text = act.text().replace("&&", "\0").replace("&", "").replace("\0", "&")
            if act.menu() is not None:
                entries.extend(self._menu_entries(act.menu(), f"{path} \u203a {text}"))
                continue
            if not act.isEnabled() or act is getattr(self, "palette_action", None):
                continue
            keys = act.shortcut().toString(QKeySequence.NativeText)
            entries.append(PaletteEntry(f"{path} \u203a {text}", act.trigger, keys))
        return entries

    def palette_entries(self, query: str) -> List[PaletteEntry]:
        """What the command palette lists for *query*."""
        if query.startswith(FAULT_PREFIX):
            needle = query[len(FAULT_PREFIX):].strip()
            if not self._results:
                return [PaletteEntry("No report is loaded \u2014 run Analyze "
                                     "first", lambda: None)]
            low = needle.lower()
            faults = []
            for r in self._results:
                fo = r.fault.fault_object
                if not low or low in fo.lower():
                    faults.append(PaletteEntry(
                        fo, partial(self._focus_fault_in_table, fo),
                        r.fault.fault_class.value))
                    if len(faults) >= 500:
                        break
            return rank_entries(needle, faults)
        commands: List[PaletteEntry] = []
        for act in self.menuBar().actions():
            if act.menu() is not None:
                name = act.text().replace("&", "")
                commands.extend(self._menu_entries(act.menu(), name))
        commands.append(PaletteEntry(
            f"Search faults \u2014 type {FAULT_PREFIX} and part of a path",
            lambda: QTimer.singleShot(0, self._reopen_palette_for_faults)))
        return rank_entries(query, commands)

    def _reopen_palette_for_faults(self) -> None:
        self.command_palette.open()
        self.command_palette.edit.setText(FAULT_PREFIX)

    def preference_values(self) -> dict:
        s = self._settings
        return {
            "theme": theme.current(),
            "font_delta": theme.font_delta(),
            "auto_save_report": self.autosave_check.isChecked(),
            "show_advanced_tabs": self.advanced_tabs_action.isChecked(),
            "show_checklist": not s.getting_started_hidden,
            "tour_done": bool(s.tour_done),
        }

    def apply_preferences(self, values: dict) -> None:
        if values.get("theme", theme.current()) != theme.current():
            self.set_theme(values["theme"])
        if "font_delta" in values and \
                theme.clamp_font_delta(values["font_delta"]) != theme.font_delta():
            self.set_font_delta(values["font_delta"])
        self.autosave_check.setChecked(bool(values.get("auto_save_report")))
        self.advanced_tabs_action.setChecked(
            bool(values.get("show_advanced_tabs")))
        self.set_checklist_visible(bool(values.get("show_checklist", True)))
        self._settings.tour_done = bool(values.get("tour_done",
                                                   self._settings.tour_done))
        self._save_settings()

    def on_preferences(self) -> None:
        dlg = PreferencesDialog(self.preference_values(), self)
        if dlg.exec() == QDialog.Accepted:
            self.apply_preferences(dlg.values())
            self.notify("Preferences saved.", "ok", status=False)

    # ------------------------------------------------------------------
    # Getting Started checklist
    # ------------------------------------------------------------------
    def _init_getting_started(self) -> None:
        s = self._settings
        self.getting_started.set_done(s.getting_started_done or [])
        self.getting_started.setVisible(not s.getting_started_hidden)
        for picker in (self.netlist_picker, self.faults_picker):
            picker.checked.connect(self._check_inputs_step)
        self._check_inputs_step()

    def _check_inputs_step(self) -> None:
        good = (OK, WARN)
        if all(p.last_check is not None and p.last_check.state in good
               for p in (self.netlist_picker, self.faults_picker)):
            self._gs_mark("inputs")

    def _gs_mark(self, step: str) -> None:
        gs = getattr(self, "getting_started", None)
        if gs is None or not gs.mark(step):
            return
        self._settings.getting_started_done = gs.done_steps()
        self._settings.save()
        if gs.all_done() and gs.isVisible():
            self.notify("Getting Started complete \u2014 you have used every "
                        "part of the tool.", "ok", status=False)

    def set_checklist_visible(self, visible: bool) -> None:
        self._settings.getting_started_hidden = not visible
        self.getting_started.setVisible(visible)
        self._settings.save()
        if visible:
            self._switch_to_tab("Summary")
        else:
            self.notify("Checklist hidden. Help \u2192 Getting Started "
                        "Checklist brings it back.", status=False)

    def _on_gs_action(self, step: str) -> None:
        if step == "inputs":
            self._on_empty_action("inputs")
        elif step == "analyze":
            self.on_analyze()
        elif step == "triage":
            self._switch_to_tab("Triage & Fix Plan")
        elif step == "agent":
            self._switch_to_tab("AI Debug Agent")
        elif step == "visualizer":
            self._switch_to_tab("Tessent Visualizer")

    def _on_tab_changed(self, _index: int) -> None:
        if self._report is not None and \
                self.tabs.currentWidget() is self.triage_panel:
            self._gs_mark("triage")
        if not self.find_bar.isHidden():
            self.find_bar.hide()

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        mime = event.mimeData()
        if mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.handle_dropped_paths(paths)

    def handle_dropped_paths(self, paths: List[str]) -> dict:
        """Put each dropped file in the box it belongs to; load a report."""
        pickers = {"netlist": self.netlist_picker, "faults": self.faults_picker,
                   "constraints": self.constraints_picker,
                   "outdir": self.outdir_picker}
        names = {"netlist": "Netlist", "faults": "Fault list",
                 "constraints": "Constraints", "outdir": "Output dir"}
        assigned, unknown, report_path = {}, [], None
        for path in paths:
            kind = classify_file(path)
            if kind == "report":
                report_path = report_path or path
            elif kind in pickers:
                pickers[kind].set_path(path)
                assigned[kind] = path
            else:
                unknown.append(path)
        parts = [f"{names[k]} \u2190 {os.path.basename(p)}"
                 for k, p in assigned.items()]
        if unknown:
            parts.append("not recognised: " + ", ".join(
                os.path.basename(p) for p in unknown))
        if assigned:
            self._set_inputs_collapsed(False)
        if report_path and self.load_report_path(report_path):
            parts.insert(0, f"Report loaded \u2190 {os.path.basename(report_path)}")
        if parts:
            ready = "netlist" in assigned and "faults" in assigned
            self.notify("; ".join(parts) + (".  Press \u25b6 Analyze (Ctrl+R)."
                                            if ready else "."),
                        "warn" if unknown else "ok", msec=8000)
        result = dict(assigned)
        if report_path:
            result["report"] = report_path
        if unknown:
            result["unknown"] = unknown
        return result

    def _error(self, message: str) -> None:
        QMessageBox.critical(self, "Error", message)


def run() -> int:
    app = QApplication.instance() or QApplication([])
    # The whole UI is English text; never mirror it because of an RTL locale.
    app.setLayoutDirection(Qt.LeftToRight)
    window = MainWindow()
    # Reopen where the user left it; first launch starts maximised because on
    # remote displays a broken title-bar maximise button leaves it small.
    if window.restore_layout():
        window.show()
    else:
        window.showMaximized()
    QTimer.singleShot(800, window.auto_check_sign_in)
    return app.exec()
