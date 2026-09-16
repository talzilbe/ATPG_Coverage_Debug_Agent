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
  bottleneck, reconvergent complexity or sequential depth explosion. A fan-out
  cone too large to walk within the search bound is reported as such rather
  than being given a verdict its partial measurements cannot support.
- **Fix catalogue** &mdash; ranked, evidence-backed proposals with
  preconditions, caveats and copyable Tessent commands. The tool never runs
  them.
- **Honesty guardrails** &mdash; every emitted hierarchy path must trace back
  to an input file, and no coverage gain is ever predicted without a measured
  re-run.
- **Per-category fault dumps** &mdash; each category selected for
  investigation gets its own CSV and JSON holding *every* fault in it, so a
  bucket can be debugged, loaded into pandas, or read by the AI agent without
  re-deriving which faults belong to it.

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
- One-click **Tessent Visualizer launch**: opens the vendor viewer on the same
  design, so a structural conclusion can be confirmed in the tool that owns
  the authoritative answer.

### Opening the design in Tessent Visualizer

Everything this tool concludes is structural. The **Tessent Visualizer** tab
builds the whole chain &mdash; project setup, licence environment, tool shell,
`read_icl` / `read_flat_model` / `read_faults`, `open_visualizer` &mdash; and
runs it in its own terminal. The session is detached, so it outlives the
application and the tool prompt stays usable after the viewer appears.

Projects are **data**: one JSON file per project in `profiles/`, so adding a
project needs no code change. Point `$ATPG_TOOL_PROFILES` at your own directory
to add or shadow one. See [profiles/README.md](profiles/README.md) for the
schema.

The exact dofile is shown before it runs and can be edited; **Copy commands**
puts the whole chain on the clipboard for hosts where the launch is not
available. Right-click any row in the Coverage Loss Table &rarr; *Inspect in
Tessent Visualizer* to open the session already looking at that fault.

### Opening a signal in a session that is already running

The generated dofile also opens a small control channel inside the tool, so a
signal can be sent to a session that is **already open** &mdash; no relaunch.
Right-click a signal and choose *Open signal in Tessent Visualizer*, from the
*Where the loss is* tree, the blocking signals of a category, a fix proposal's
hotspot, or the Coverage Loss Table.

On a *cluster prefix* the entry is greyed out on purpose: a prefix is a string
the triage computed to show where faults concentrate, not an object the tool
can resolve.

The channel is deliberately narrow. It listens on loopback only, on an
OS-assigned port; every request carries a per-session token; **no command text
is transmitted** &mdash; a verb, one object and option pairs are sent
separately and the command is rebuilt inside the tool, so a bracket or `$` in
an object name stays data; and the verb must appear in the profile's
`control.allowed_commands`, which the shipped profile limits to viewing
commands. Set `control.enabled` to `false` to switch it off entirely.

From the CLI, `--list-vis-profiles` lists the profiles and
`--emit-visualizer-script DIR` writes the launch scripts and prints the chain
without running anything.
- `pytest` suite (250+ tests) and synthetic sample inputs.

---

## Root-cause categories

Assigned by the structural engine when the fault list carries no subtype, and
used to corroborate it when one is present:

- `constraint_induced_controllability_loss`
- `constraint_induced_observability_loss`
- `scan_to_non_scan_boundary` — only ever from a **sequential** neighbour
- `non_scan_blocks_propagation` — likewise
- `scan_capable_but_not_chain_connected` — the cell has a scan-data input and
  a shift-enable but its scan-in/scan-out is dangling; re-stitch it, do not
  add a wrapper
- `tied_or_constant_hardware`
- `clock_reset_or_test_enable_blocking`
- `structural_masking_or_reconvergence`
- `unresolved_connectivity`
- `other_structural_cause`

---

## Coverage metrics

The analyzer assigns every fault class a **coverage role**, and the role — never
the class label — drives the numbers:

| Role | Meaning | Default classes | Effect |
| --- | --- | --- | --- |
| `DT` | detected | `DS`, `DI.*` | numerator, full credit |
| `PD` | possibly detected | `PT`, `PU` | numerator, `posdet_credit` (default `0`) |
| `UD` | undetectable | `UU`, `TI`, `BL`, `RE` | removed from the test-coverage denominator |
| `AU` | ATPG untestable | `AU.*` | stays in the denominator |
| `ND` | not detected | `UO.*`, `UC.*` | coverage loss |

```
test_coverage      = (DT + posdet_credit*PD) / (FU - UD)
fault_coverage     = (DT + posdet_credit*PD) / FU
atpg_effectiveness = (DT + PU + UD + AU) / FU
```

`posdet_credit` defaults to **0**, matching the ATPG tool: a possibly-detected
fault earns no test- or fault-coverage credit. ATPG effectiveness follows a
separate rule — it credits `PU` (possibly detected, *untestable*) and **not**
`PT`, because a posdet-untestable fault is as resolved as ATPG can make it.
Both are configurable (`posdet_credit`, `effectiveness_posdet_families`) and
both are printed in the report header.

### Two populations: total and total relevant

A run that performs a **fault disposition** reclassifies a block of faults into
a waiver subclass and excludes it from the relevant coverage column
(`set_relevant_coverage -exclude <subclass>`). The report reproduces both
columns. ATPG effectiveness is reported over the *total* population in both,
since the waived faults were resolved by ATPG — re-basing it would understate
how much of the design ATPG settled. This is configurable via
`effectiveness_basis`.

The disposition step also **rewrites the ATPG-untestable subclass
distribution**, which is what the category ranking and fix plan are built from.
The report therefore names the fault list it parsed and states whether it is
`pre-disposition`, `post-disposition` or `undetermined`. The verdict comes from
the file's *contents* — is the waiver subclass present? — with the phase tag in
the file name only breaking ties. Point `--faults` at a **directory** and the
best candidate is selected by pattern; name a file explicitly and it is always
honoured, with any better-looking sibling reported rather than silently
substituted.

Every percentage is printed next to the counts it came from, and a metric that
is undefined for the population (no faults, or every fault undetectable) is
reported as `n/a` rather than as a fabricated number. After parsing, the
analyzer asserts `DT + PD + UD + AU + ND == records parsed`; a census that does
not reconcile aborts instead of shipping plausible-looking wrong figures.

A class the role map does not describe is **never** merged into a catch-all: it
keeps its verbatim token, is warned about by name with retained sample records,
is excluded from every metric, and past a configurable threshold it fails the
run outright.

---

## Configuration — onboarding a new partition

Everything design-specific lives in one JSON file, so a different design,
hierarchy depth, cell library, Tessent version or fault model is a
configuration change and never a code change. Point `ATPG_ANALYSIS_CONFIG` at
it, or pass `config=` to `run_analysis`.

| Key | Default | What it controls |
| --- | --- | --- |
| `class_roles` | `DS,DI→DT`, `PT,PU→PD`, `UU,TI,BL,RE→UD`, `AU→AU`, `UO,UC→ND` | Fault class → coverage role |
| `posdet_credit` | `0.5` | Credit given to a `PD` fault |
| `unknown_class_threshold_pct` | `0.1` | Unrecognised-class share that fails the run |
| `unknown_class_fatal` | `true` | Whether that threshold raises |
| `unresolved_constraint_threshold_pct` | `10.0` | Unknown-directive share before escalation |
| `unresolved_constraint_fatal` | `false` | Whether that threshold raises |
| `unmapped_object_threshold_pct` | `100.0` | Unmapped-fault-object share before escalation |
| `unmapped_object_fatal` | `false` | Whether that threshold raises |
| `sample_limit` | `20` | Verbatim samples kept per unrecognised token |
| `scan_in_pins` | `si, sd, ti, sin, scan_in, scanin, sdi, test_si, tie` | Scan-data input pin names |
| `scan_out_pins` | `so, to, sout, scan_out, scanout, sdo, test_so, q_so` | Scan-data output pin names |
| `shift_enable_pins` | `se, ssb, sen, scan_en, scan_enable, shift_en, test_se, sh, smc` | Shift-enable pin names |
| `clock_pins` | `clk, ck, clock, cp, gclk, clkin, clkb, ckn, clk_n` | Clock pin names (what makes a cell sequential) |
| `unconnected_net_patterns` | `SYNOPSYS_UNCONNECTED*, *_UNCONNECTED*, *UNCONNECTED*, *_OPEN*, *_DANGLING*` | Dangling-net naming (globs) |
| `tie_high_patterns` | `tiehi, tihi, tieh, thi\b, tie1, logic1, const1` | Tie-high cell naming (regex) |
| `tie_low_patterns` | `tielo, tilo, tiel, tlo\b, tie0, logic0, const0` | Tie-low cell naming (regex) |
| `constraint_directives` | Tessent `add_*` / `set_*` set | Dofile directive → constraint kind |
| `constraint_value_codes` | `C0/C1/CX`, `T0/T1/TX`, `0/1/X` | Constraint value token → logical value |
| `replace_defaults` | `false` | Replace the defaults instead of merging into them |

Maps and lists are **merged** into the defaults, so a partition only names what
differs:

```json
{
  "class_roles": { "NC": "UD" },
  "scan_in_pins": ["chain_data_in"],
  "shift_enable_pins": ["shiftctl"],
  "clock_pins": ["clkin"],
  "unconnected_net_patterns": ["nc_house_style_*"],
  "tie_high_patterns": ["mylib_tiehi"]
}
```

Scan-ness, sequential-ness and tie-ness are decided from **pin lists and
connectivity**, never from instance or cell-type names; the vocabularies above
only say what a pin is called. The active configuration is recorded in every
report, so a reader can tell "this library has no scan cells" apart from "we
were never told what this library calls its scan pins".

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
   conversation keeps the full analysis context **and the investigation
   tools**: after an agentic run the local MCP server (with the parsed
   netlist handed to it) stays alive for every follow-up, and each tool call
   the model makes is shown live in the **Agent Tool Trace** pane. The chat
   box can be popped out into its own window; the "agent is replying"
   indicator travels with it.

> With the Copilot CLI backend, **Agentic tools** must stay ticked for a real
> investigation loop. Unticked, the enabled skills run once locally and the
> model gets a single pass with no way to request further evidence — the run
> log says so explicitly.

### The offline ↔ agent loop

The offline analysis and the agent review each other rather than the agent
merely restating the report:

- **The analysis says where it is weakest.** Every place it recorded reduced
  confidence, a blocker only partly traced, a structurally mixed category, a
  truncated cone, an unreconciled census or a pre-disposition snapshot becomes
  an **Open Question**, ordered by priority and naming the tool that would
  settle it (`list_open_questions`; also in the reports). The agent is told to
  spend its budget there first.
- **The two classifications are cross-checked.** For every mapped fault the
  ATPG tool's own subclass (`AU.TC`, `AU.PC`, `UO.AAB` …) is compared with the
  structural root cause derived here. Pairs are judged *agree*, *disagree*,
  *unconfirmed* (the tool names a mechanism the structural walk did not find)
  or *uninformative*; contradictions come with verbatim sample faults as leads
  (`classification_crosscheck`). It never decides which side is right.
- **What the agent establishes comes back structured.** `record_finding` lets
  it record a correction, confirmation, new lead or gap against a fault,
  category or report section, with the evidence it cites. Findings are saved
  with the session and listed in the reports under **Agent review** beside the
  offline value — nothing offline is ever overwritten.
- **The tools see the real design.** The parsed netlist is handed to the
  out-of-process tool server (as a pickle, reused from a cache keyed on the
  netlist file), so `scan_status`, `trace_path` and `verify_paths` answer from
  the netlist itself rather than from recorded evidence.

### How the agent is kept honest

- **Every answer is checked** — the first one and every follow-up — against the
  same guardrails the offline analysis applies to itself: hierarchy paths must
  trace back to an input file, and no coverage gain may be predicted without a
  measured re-run.
- **A flagged answer is corrected, not just annotated.** The model gets one
  corrective round-trip; anything still unsupported afterwards is listed
  beneath the answer. Annotating a fabricated path leaves the fabricated path
  in front of you, and you may act on it.
- **"Not determined" is an action it can take.** The `report_insufficient_evidence`
  tool lets the agent declare that the evidence does not settle a question and
  say what would settle it, rather than reaching for a plausible answer.
- **It can check its own footing.** `report_context` reports how much of the
  coverage loss actually mapped onto the netlist, how much sits on hard
  constants, and whether you have waived faults — so a percentage computed
  over mostly unmapped faults is not read at face value.

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

### Per-category fault files

Writing a Markdown report also writes a sidecar folder beside it holding the
faults behind each selected category, and the report links to them:

```
report.md
report_categories/
  index.json      # manifest: every category, its files and its counts
  AU.TC.csv       # all 27 AU.TC faults, same columns as --report-csv
  AU.TC.json      # the same faults with full evidence + the category's
                  # verdict, clusters, blocking sources and site profile
  UO.AAB.csv
  UO.AAB.json
  ...
```

The CSV always holds every fault in the category. The JSON is capped at 5000
faults &mdash; one constant driver can hold far more &mdash; and says so,
pointing at its CSV for the remainder. The same folder is produced by
**Export category faults&hellip;** on the GUI's *Triage &amp; Fix Plan* tab,
and by **Save Report**, so a saved session is self-contained. The AI agent can
read a category in-band with the `list_category_faults` tool instead of
opening files.

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
    category_dump.py   # one file per category, holding all of its faults
    html_report.py     # print-style document, also shown in the GUI
    session_report.py  # save / load a full analysis as JSON
  skills/              # deterministic and on-demand analysis skills
  launcher/
    profiles.py        # launch profiles, loaded from profiles/*.json
    visualizer.py      # builds the viewer launch chain (runs nothing itself)
    terminals.py       # terminal-emulator discovery
  agent/
    debug_agent.py     # LLM backends (Copilot CLI / OpenAI-compatible)
  gui/
    main_window.py     # PySide6 main window
    triage_panel.py    # Triage & Fix Plan tab
    agent_panel.py     # AI Debug Agent tab
    visualizer_panel.py # Tessent Visualizer tab
    workers.py         # QThread analysis worker
    details_panel.py   # per-fault evidence panel
tests/                 # pytest suite
profiles/              # one JSON launch profile per project (data, not code)
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
- **Reconvergence is only visible from above the fan-out.** It is counted where
  paths re-merge, so a site *inside* a reconvergent cone sees one narrow path
  and reads as an observability bottleneck instead. The two call for opposite
  fixes &mdash; more abort budget helps a bottleneck and is wasted on
  reconvergence &mdash; so every bottleneck verdict states this explicitly.
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
- **The viewer launch needs a terminal emulator and an X display.** It is
  built for a Linux workstation with `xterm`, `gnome-terminal` or
  `xfce4-terminal`; elsewhere, use *Copy commands* and run the chain by hand.

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
