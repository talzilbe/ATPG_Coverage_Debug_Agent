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

Requires **Python 3.11+**.

```powershell
# from the project root (the folder containing requirements.txt)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`PySide6` is required only for the GUI. `networkx` and `pandas` are optional;
the tool degrades gracefully without them.

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
