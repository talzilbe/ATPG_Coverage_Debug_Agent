# ATPG Coverage Debug Agent

A local Python application that helps a hardware/DFT engineer understand **where
ATPG test coverage is lost** and **why**, starting from three artefacts:

1. a **hierarchical gate-level Verilog netlist**,
2. a **Tessent-style ATPG fault list**, and
3. a **constraint file**.

It correlates undetected faults back to netlist objects and produces an
evidence-based root-cause diagnosis for every coverage-loss fault, surfaced
through a **GUI**, a **CLI**, and **Markdown/CSV reports**.

> **Important:** This is a *structural* analyzer, not a logic simulator or a
> full Verilog compiler. Every conclusion is conservative and carries a
> **confidence level** and **evidence**. Verify diagnoses before acting on them.

---

## Features

- Structural parser for the common gate-level Verilog subset (modules,
  instances, cell types, pin/net connectivity, driver/load relationships).
- Flexible Tessent fault-list parser supporting multiple line layouts and the
  fault classes `DS`, `DI`, `TI`, `AU`, `UO`, `UC`.
- Keyword-driven constraint parser (force / constant / disable / block /
  constrain / clock / reset / test-enable, plus Tessent
  `add_input_constraints ... C0|C1|CX`).
- Connectivity model with immediate fan-in/fan-out and bounded cone tracing
  (uses `networkx` when available, with a pure-Python fallback).
- Tiered fault-to-netlist **mapper** with `high` / `medium` / `low` /
  `unresolved` confidence and candidate lists (no hidden ambiguity).
- Conservative **root-cause engine** that separates *observed facts* from
  *inferred conclusions* and attaches evidence to every diagnosis.
- Executive summary, per-fault detail table, and repeated-pattern grouping.
- Output to console, GUI tables, Markdown, and CSV.
- PySide6 GUI with file pickers, a worker thread (non-blocking analysis),
  progress updates, sortable/filterable table, and a details panel.
- `pytest` unit tests and synthetic sample inputs.

---

## Root-cause categories

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

```powershell
python -m atpg_coverage_debug_agent
```

Then:

1. Browse to the **netlist**, **fault list**, and (optionally) **constraints**.
2. Optionally pick an **output directory**.
3. Click **Analyze**. Analysis runs on a worker thread; progress is shown in the
   status bar.
4. Inspect the **Summary**, **Coverage Loss Table**, **Repeated Patterns**, and
   **Logs / Warnings** tabs. Select any table row to see full evidence in the
   details panel.
5. Use **Export Markdown Report** / **Export CSV** to save results.

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

```powershell
python -m atpg_coverage_debug_agent.cli `
  --netlist sample_data/sample_netlist.v `
  --faults sample_data/sample_faults.txt `
  --constraints sample_data/sample_constraints.txt `
  --report-md report.md `
  --report-csv report.csv
```

The CLI prints a summary to stdout, optionally writes Markdown/CSV reports, and
returns a non-zero exit code on fatal errors (`2` for bad inputs, `1` for
unexpected failures).

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
Whitespace-delimited; the parser locates the class token and the path-like
object token on each line. All of these work:

```
AU 1 top/u_alu/U5/Y
top/u_ctrl/U4/Y UO
UC top/u_alu/reg_scan/SE
```

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

See [sample_data/](sample_data) for complete examples.

---

## Example

```powershell
python -m atpg_coverage_debug_agent.cli `
  --netlist sample_data/sample_netlist.v `
  --faults sample_data/sample_faults.txt `
  --constraints sample_data/sample_constraints.txt
```

produces a summary of fault classes, top root causes, and the most-affected
instances, plus per-fault evidence in the exported reports.

---

## Running the tests

```powershell
pip install -r requirements.txt
pytest
```

The tests cover fault parsing, constraint parsing, Verilog parsing,
connectivity, mapping, root-cause classification, and report generation using
the synthetic files in `sample_data/`.

---

## Project structure

```
atpg_coverage_debug_agent/
  __init__.py
  __main__.py          # `python -m atpg_coverage_debug_agent` launches the GUI
  models.py            # typed dataclasses / enums
  app.py               # orchestration shared by CLI and GUI
  cli.py               # command-line interface
  parser/
    verilog_parser.py  # structural Verilog parser
    fault_parser.py    # Tessent fault-list parser
    constraint_parser.py
  analysis/
    connectivity.py    # driver/load graph + fan-in/out + cone tracing
    mapper.py          # fault-object -> netlist-object correlation
    root_cause.py      # conservative root-cause classification
    summarizer.py      # summary, patterns, pipeline orchestration
  reporting/
    markdown_report.py
    csv_report.py
  gui/
    main_window.py     # PySide6 main window
    workers.py         # QThread analysis worker
    details_panel.py   # per-fault evidence panel
tests/                 # pytest suite
sample_data/           # synthetic netlist / faults / constraints
requirements.txt
README.md
```

---

## Limitations (first version)

- **Structural only.** No simulation; conclusions are heuristic, not formal
  proofs. The engine is intentionally conservative and labels unproven items.
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

- Integrate a real Verilog elaboration library for accurate hierarchy.
- Use formal/structural justification (e.g. SAT-based controllability cones).
- Cross-module net tracing through port connections for full-chip cones.
- Configurable, vendor-specific fault/constraint dialect profiles.
- Richer GUI visualisation (schematic/cone views) and saved sessions.
- Test-point recommendation ranking based on coverage impact.
