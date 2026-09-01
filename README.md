# ATPG Coverage Debug Agent

A local Python application that helps a hardware/DFT engineer understand **where
ATPG test coverage is lost** and **why**, starting from three artefacts:

1. a **hierarchical gate-level Verilog netlist**,
2. a **Tessent-style ATPG fault list**, and
3. a **constraint file**.

It correlates undetected faults back to netlist objects, ranks the categories
worth debugging, names what is blocking them, and proposes concrete fixes with
runnable commands &mdash; surfaced through a **GUI**, a **CLI**,
**Markdown/CSV/HTML reports**, and an **AI agent** that investigates through the
same deterministic tools.

> **Important:** This is a *structural* analyzer, not a logic simulator or a
> full Verilog compiler. Every conclusion is conservative and carries a
> **confidence level** and **evidence**. It never predicts a coverage gain:
> only re-running ATPG can establish that. Verify diagnoses before acting on
> them.

---

## Features

**Triage and fixes**

- **Fault-subclass taxonomy** &mdash; dotted Tessent classes (`AU.PC`, `AU.TC`,
  `AU.SEQ`, `AU.BB`, `AU.UDN`, `AU.CC`, `UC/UO.AAB`, `UC/UO.EAB`) are the ATPG
  tool's own root-cause labels, so they drive the analysis instead of
  structural guesswork.
- **Derived coverage statistics** &mdash; per-class and per-subclass counts,
  percentages and stuck-at split, computed from the fault list.
- **Hierarchy clustering** with automatic depth selection, to show *where* the
  loss concentrates (a pointer, never a root cause).
- **Scored verdicts** &mdash; concentration, symmetry, stuck-at asymmetry and
  depth produce a reproducible `true` / `partial` / `false` actionability call
  with an explicit confidence level.
- **Blocking-source attribution** &mdash; traces fan-in cones to name the tie
  cell, test data register, unscanned flop or constrained pin responsible.
- **Structural site profiling** &mdash; estimates why aborted faults were hard
  to test: low controllability, hard observability gap, observability
  bottleneck, reconvergent complexity or sequential depth explosion.
- **Fix catalogue** &mdash; ranked, evidence-backed proposals with
  preconditions, caveats and copyable Tessent commands. The tool never runs
  them.
- **Honesty guardrails** &mdash; every emitted hierarchy path must trace back
  to an input file, and no coverage gain is ever predicted without a measured
  re-run.

**Core analysis**

- Structural parser for the common gate-level Verilog subset (modules,
  instances, cell types, pin/net connectivity, driver/load relationships).
- Flexible Tessent fault-list parser: the MTFI structured format and several
  flat layouts, preserving dotted subclasses and stuck-at values.
- Keyword-driven constraint parser (force / constant / disable / block /
  constrain / clock / reset / test-enable, plus Tessent
  `add_input_constraints ... C0|C1|CX`).
- Connectivity model with immediate fan-in/fan-out and bounded cone tracing
  (uses `networkx` when available, with a pure-Python fallback).
- Tiered fault-to-netlist **mapper** with `high` / `medium` / `low` /
  `unresolved` confidence and candidate lists (no hidden ambiguity).
- Conservative **root-cause engine** that separates *observed facts* from
  *inferred conclusions* and attaches evidence to every diagnosis.

**Interfaces**

- PySide6 GUI: file pickers, non-blocking analysis, a **Triage &amp; Fix Plan**
  tab, sortable/filterable fault table, per-fault details, multi-partition
  queueing, report waivers, and save/load/compare of sessions.
- CLI with console triage and fix plan, plus Markdown / CSV / HTML export.
- An **AI Debug Agent** that investigates through deterministic tools, over a
  local MCP server or an OpenAI-compatible endpoint.
- `pytest` suite (250+ tests) and synthetic sample inputs.

---

## Root-cause categories

Assigned by the structural engine when the fault list carries no subtype, and
used to corroborate it when one is present:

- `constraint_induced_controllability_loss`
- `constraint_induced_observability_loss`
- `scan_to_non_scan_boundary`
- `non_scan_blocks_propagation`
- `tied_or_constant_hardware`
- `clock_reset_or_test_enable_blocking`
- `structural_masking_or_reconvergence`
- `unresolved_connectivity`
- `other_structural_cause`

---

## What the triage produces

Running the shipped demo dataset:

```
Coverage triage (derived from the fault list):
  detected     : 34 (28.81%)
  coverage loss: 84 (71.19%)

  Category      Faults        %   sa0/sa1   Imbalance
  UO.AAB           32   27.12%   16/16   0.00
  AU.TC            27   22.88%    0/27   1.00
  AU.PC            12   10.17%    6/6    0.00
  AU.SEQ           12   10.17%    6/6    0.00
  AU.BB             1    0.85%    1/0    1.00

  What is blocking them (structural estimate, not the tool's own attribution):
    AU.TC        configurable_register (27/27 traced)
          18  uf_tdr_out_inter_reg [test_data_register]
           9  uf_tie_lo [tie_cell tied 0]
    AU.PC        user_configured (12/12 traced)
          12  pi_hold = 1 [constrain]
```

Each conclusion carries its evidence source: `fault_list`, `constraint_file`,
`netlist`, `structural_inference` or `clustering_hint`. The first three are
direct readings of an input file; the last two are this tool's own reasoning
and are labelled as such.

---

## Installation

### Prerequisites

- **Python 3.11+** (developed and tested on CPython 3.11).
- **pip** and the standard-library **`venv`** module (to create a virtual
  environment).
- **PySide6** (installed from `requirements.txt`) — required for the GUI.
- Optional: **networkx** (faster connectivity graph) and **pandas**
  (CSV/table convenience). Both degrade gracefully if absent.
- Optional, **only** for the AI Debug Agent's *GitHub Copilot CLI* backend:
  **Node.js 18+ / npm** (or a prebuilt `copilot` binary). See
  [Installing the GitHub Copilot CLI](#installing-the-github-copilot-cli-for-the-ai-debug-agent).

### Set up the Python environment

From the project root (the folder containing `requirements.txt`):

**Linux / macOS (bash/zsh):**
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Linux (tcsh / csh):**
```tcsh
python3.11 -m venv .venv
source .venv/bin/activate.csh
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` installs:

| Package | Version | Purpose |
| --- | --- | --- |
| `PySide6` | `>=6.5` | GUI framework (**required for the GUI**) |
| `networkx` | `>=3.0` | Optional — faster connectivity graph |
| `pandas` | `>=2.0` | Optional — CSV/table generation (stdlib fallback exists) |
| `pytest` | `>=7.0` | Running the test suite |

The CLI and the analysis engine work without `networkx`/`pandas`; only the GUI
strictly needs `PySide6`.

---

## Running the GUI

```bash
python -m atpg_coverage_debug_agent
```

Then:

1. Browse to the **netlist**, **fault list**, and (optionally) **constraints**.
   To see what the tool does, load the `demo_*` files from `sample_data/`.
2. Optionally pick an **output directory**.
3. Click **Analyze**. Analysis runs on a worker thread; progress is shown in the
   status bar.
4. Work the **Triage &amp; Fix Plan** tab &mdash; *Categories* for what is
   losing coverage and whether it is worth acting on, *Where the loss is* for
   the hierarchy hotspots, and *Fix Plan* for ranked proposals with copyable
   commands.
5. Drill into individual faults in the **Coverage Loss Table**; select a row to
   see full evidence in the details panel.
6. Use **Export Markdown Report** / **Export CSV**, or **Open Report in
   Browser** for the full HTML document.

The in-app **Help** (`?` in the menu) documents every tab and explains how each
conclusion is reached.

---

## Using the AI Debug Agent

The GUI includes an **AI Debug Agent** tab that turns the structural report into
a natural-language, evidence-driven diagnosis and lets you **chat** about it.
Two backends are supported:

- **GitHub Copilot CLI** (default) — runs a local `copilot` subprocess; no
  endpoint URL to configure.
- **OpenAI-compatible HTTP endpoint** — e.g. an internal LLM gateway
  (`base URL` + `model` + optional API key).

### Installing the GitHub Copilot CLI (for the AI Debug Agent)

The Copilot CLI is a large binary and is **not** committed to the repo
(git-ignored). You only need it if you want the default **GitHub Copilot CLI**
backend — the OpenAI-compatible **HTTP** backend needs no CLI at all.

**Prerequisite:** Node.js 18+ and npm (for the npm install method).

Install it with **one** of the following:

```bash
# 1) npm (recommended) — puts a `copilot` command on your PATH
npm install -g @github/copilot
copilot --version                 # verify the install

# 2) Prebuilt binary — download from the releases page, then mark it executable
#    https://github.com/github/copilot-cli/releases
chmod +x /path/to/copilot
/path/to/copilot --version
```

Keep the CLI's config/state **off** a quota-limited home directory (recommended
on shared NFS hosts, where `$HOME` is small):

```bash
export COPILOT_HOME=/path/with/space/copilot-home     # bash / zsh
```
```tcsh
setenv COPILOT_HOME /path/with/space/copilot-home     # tcsh / csh
```

Then point the GUI at it: **AI Debug Agent → Backend = *GitHub Copilot CLI*** →
set the **Copilot CLI** field to your `copilot` executable via **Browse…**
(or leave it if `copilot` is already on your PATH), and authenticate once on the
**Authentication** tab (see [Using the AI Debug Agent](#using-the-ai-debug-agent)).

> **Authentication needs a Copilot-enabled GitHub account.** Use the
> **Authentication** tab's device-code sign-in, or paste a **fine-grained PAT**
> with the *Copilot Requests* permission. Classic `ghp_` tokens are **not**
> supported.

### Steps

1. Run an analysis first (see **Running the GUI**) so a report exists.
2. Open the **AI Debug Agent** tab → set **Backend** to *GitHub Copilot CLI*.
3. Set the **Copilot CLI** path to your `copilot` executable (use **Browse…**).
   Optionally pick a **CLI model** (`auto` is fine; the list is editable).
4. Open the **Authentication** tab and sign in once (only for the CLI backend):
   - **Sign in with device code** — open the shown URL, enter the code. On a
     headless host with no system keychain, instead run `copilot login` in a
     terminal and **accept plaintext storage**; **or**
   - paste a **fine-grained PAT** (with the *Copilot Requests* permission) into
     **Option A**. Classic `ghp_` tokens are not supported.
   - Click **Check authentication** to confirm.
5. Tick **Agentic mode** and click **Run AI Debug Agent**. The analysis skills
   run and the agent produces its A–F diagnosis. (Untick it for a single-shot
   run, or use **Build Prompt Only** to copy the prompt into your own chat.)
6. Use the **Follow-up Chat** box to ask questions about the diagnosis — the
   conversation keeps the full analysis context.

> Data leaves your machine only when you explicitly configure a backend. With
> the Copilot CLI, prompts go through GitHub Copilot's authenticated service;
> for an internal-only setup, use the HTTP backend pointed at your own gateway.

---

## Running the CLI

```bash
python -m atpg_coverage_debug_agent.cli \
  --netlist sample_data/demo_netlist.v \
  --faults sample_data/demo_faults.mtfi \
  --constraints sample_data/demo_constraints.do \
  --report-md report.md \
  --report-csv report.csv
```

The CLI prints the fault-class summary, the coverage triage (categories,
hierarchy hotspots, blocking sources and structural signatures) and the ranked
fix plan, optionally writes Markdown/CSV reports, and returns a non-zero exit
code on fatal errors (`2` for bad inputs, `1` for unexpected failures).

| Option | Effect |
| --- | --- |
| `--fix-limit N` | How many fix proposals to print (default 5). |
| `--explain SUBCLASS` | Explain a class such as `AU.TC` &mdash; what it means, its usual causes, the evidence that would confirm it and the fixes that apply &mdash; then exit. Needs no input files. |

```bash
python -m atpg_coverage_debug_agent.cli --explain AU.TC
```

---

## Expected input formats

### Verilog netlist
Structural gate-level Verilog. Supported constructs:

```verilog
module top (a, b, y);
  input a, b;
  output y;
  wire n1;
  AND2 U1 ( .A(a), .B(b), .Y(n1) );
  INV  U2 ( .A(n1), .Y(y) );
endmodule
```

### Fault list

Two shapes are supported. The **Tessent MTFI** structured format is detected
automatically and is the one to prefer, because it carries the dotted subclass
and the stuck-at value that the triage depends on:

```
FaultInformation {
 FaultType (Stuck) {
  FaultList {
   Format : Identifier, Class, Location;
   Instance ("") {
    1,  AU.TC,   "/top/u_fifo/uf_and_0/A";
    0,  UO.AAB,  "/top/u_crypto/uc_m0/Y";
    0,  DS,      "/top/u_good/ug_head/A";
```

A **flat** whitespace-delimited list also works; the parser locates the class
token and the path-like object token on each line. Dotted subclasses are
accepted here too:

```
AU.TC 1 top/u_alu/U5/Y
top/u_ctrl/U4/Y UO
UC top/u_alu/reg_scan/SE
```

> Without dotted subclasses the tool still works, but it falls back to
> structural inference alone and reports `reduced` confidence. Use an MTFI
> list where you can.

### Constraint file
Intent detected by keyword:

```
force sel 0
constrain test_se C0
clock clk
reset rst_n
block din
scan_en = 0
```

See [sample_data/](sample_data) for complete examples. Two sets are shipped:

| Set | Use |
| --- | --- |
| `demo_netlist.v` + `demo_faults.mtfi` + `demo_constraints.do` | **Start here.** Dotted subclasses and a design built to exercise every part of the triage: a test data register and a hardwired tie holding cones constant, a constrained input, a reconvergent cone, a long unscanned chain, and a cone with no capture point. |
| `sample_netlist.v` + `sample_faults.txt` + `sample_constraints.txt` | A minimal set using bare fault classes, kept for the parser tests. |

---

## Example

```bash
python -m atpg_coverage_debug_agent.cli \
  --netlist sample_data/demo_netlist.v \
  --faults sample_data/demo_faults.mtfi \
  --constraints sample_data/demo_constraints.do
```

produces the fault-class summary, the coverage triage with named blocking
sources, and the ranked fix plan shown earlier, plus per-fault evidence in the
exported reports.

---

## Running the tests

```powershell
pip install -r requirements.txt
pytest
```

The tests cover fault parsing, constraint parsing, Verilog parsing,
connectivity, mapping, root-cause classification, the coverage triage
(statistics, clustering, scoring, attribution, structural profiling and fix
ranking), the honesty guardrails, report generation and the GUI panels, using
the synthetic files in `sample_data/`.

Two of them are worth knowing about:

- a **self-audit** that runs a full analysis and asserts the tool's own output
  quotes no fabricated path and predicts no unmeasured coverage gain;
- a **help-drift** check that fails if an investigative tool is added without
  being documented in the in-app Help.

---

## Project structure

```
atpg_coverage_debug_agent/
  __init__.py
  __main__.py          # `python -m atpg_coverage_debug_agent` launches the GUI
  models.py            # typed dataclasses / enums
  app.py               # orchestration shared by CLI and GUI
  cli.py               # command-line interface
  mcp_server.py        # stdio MCP server exposing the investigative tools
  knowledge/
    subclasses.py      # fault-subclass taxonomy (meanings, causes, evidence)
    fixes.py           # catalogue of candidate fixes and their commands
  parser/
    verilog_parser.py  # structural Verilog parser
    fault_parser.py    # Tessent fault-list parser (MTFI + flat)
    constraint_parser.py
  analysis/
    connectivity.py    # driver/load graph + fan-in/out + cone tracing
    mapper.py          # fault-object -> netlist-object correlation
    root_cause.py      # conservative root-cause classification
    statistics.py      # derived coverage breakdown + category selection
    cluster.py         # hierarchy clustering with auto-depth
    scoring.py         # score factors, patterns, actionability verdicts
    attribution.py     # traces what is blocking AU.TC / AU.PC faults
    reachability.py    # structural profiling of aborted fault sites
    recommend.py       # ranked fix proposals with evidence
    guardrails.py      # copy-exact paths + no unmeasured claims
    investigate.py     # deterministic query core shared by skills and MCP
    report_edit.py     # waivers, with the triage recomputed
    regression.py      # baseline comparison
    summarizer.py      # summary, patterns, pipeline orchestration
  reporting/
    markdown_report.py
    csv_report.py
    html_report.py     # print-style document, also shown in the GUI
    session_report.py  # save / load a full analysis as JSON
  skills/              # deterministic and on-demand analysis skills
  agent/
    debug_agent.py     # LLM backends (Copilot CLI / OpenAI-compatible)
  gui/
    main_window.py     # PySide6 main window
    triage_panel.py    # Triage & Fix Plan tab
    agent_panel.py     # AI Debug Agent tab
    workers.py         # QThread analysis worker
    details_panel.py   # per-fault evidence panel
tests/                 # pytest suite
sample_data/           # demo and minimal netlist / faults / constraints
requirements.txt
README.md
```

---

## Limitations (first version)

- **Structural only.** No simulation; conclusions are heuristic, not formal
  proofs. The engine is intentionally conservative and labels unproven items.
- **Blocking sources and site profiles are estimates.** Cone tracing cannot
  reason about Boolean satisfiability, multi-driver resolution or
  mode-dependent gating the way ATPG does. Confirm them in a real tool session
  before acting on anything expensive.
- **Coverage percentages are fault-list ratios**, not the ATPG tool's
  test-coverage figure, which also accounts for fault collapsing and
  untestable-fault credit.
- **No measured coverage gain.** The benefit of a proposed fix can only be
  established by re-running ATPG; the tool proposes hypotheses to test and
  never predicts a number.
- **Verilog subset.** Behavioural RTL, generate loops, parameter elaboration,
  macros and complex bus expressions are not elaborated.
- **Flat-name ambiguity.** Mapping flattened fault names back to hierarchy can
  be ambiguous; such cases are returned as `unresolved` with candidates rather
  than guessed.
- **Scan detection heuristics.** Scan vs non-scan is detected from cell-type and
  signal naming conventions unless scan cells are explicitly identifiable.
- **Constraint mapping** depends on signal names lining up with netlist nets.

---

## Future improvements

- Ingest `report_statistics -detailed_analysis` and `analyze_fault` logs when
  they are available, upgrading the estimated attribution and site profiles to
  the tool's own authoritative findings.
- Connect to a live Tessent shell, and validate a proposed fix by submitting
  baseline and cut-point runs and diffing the measured coverage.
- Compare two revisions: coverage swing, class deltas and cluster movers.
- Integrate a real Verilog elaboration library for accurate hierarchy.
- Use formal/structural justification (e.g. SAT-based controllability cones).
- Cross-module net tracing through port connections for full-chip cones.
- Configurable, vendor-specific fault/constraint dialect profiles.
- Richer GUI visualisation (schematic/cone views).
