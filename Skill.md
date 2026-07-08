
---
name: dft-atpg-debug
description: 'DFT/ATPG structural coverage debug. Analyzes gate-level Verilog netlists, Tessent ATPG fault lists (.mtfi.gz), and ATPG constraint files (.do) to identify coverage loss root causes. Use for: ATPG debug, stuck-at coverage loss, AU/UO/UC fault analysis, non-scan boundary detection, constraint-induced masking, scan chain observability, clock gate testability, black-box SRAM boundary, IDI repeater coverage, UCLK slice faults, ctmTdsLR analysis, DFT coverage report.'
argument-hint: 'Path to directory containing .v.gz netlist, .mtfi.gz fault list, and .do constraint file'
---

# DFT / ATPG Structural Coverage Debug

## When to Use
- Stuck-at ATPG coverage is below target and you need to find root causes
- Fault list contains AU, UO, or UC entries you need to explain
- You have a compressed gate-level netlist + Tessent fault list + constraint dofile
- You need to identify non-scan boundaries, constraint-induced masking, or clock testability issues

## Inputs Required
| File | Pattern | Contents |
|------|---------|----------|
| Verilog netlist | `*.v.gz` | Hierarchical gate-level netlist (Fusion Compiler output) |
| Fault list | `*.mtfi.gz` | Tessent ATPG fault list, flat hierarchy, `.mtfi` format |
| Constraint file | `*.do` / `*.user_constraints.do` | ATPG dofile with `add_clocks`, `add_input_constraints`, `add_black_boxes`, etc. |

## Fault Class Reference
| Class | Meaning | Coverage Impact |
|-------|---------|----------------|
| DS | Detected (stuck-at) | Positive |
| DI.SCAN / DI.CLK / DI.SEN | Detected (scan/clock/SEN) | Positive |
| TI | Tied by hardware | Excluded |
| UU | Uncovered (typically AU.BB excluded) | Excluded if `set_relevant_coverage -exclude AU.BB` |
| **AU** | Undetected — coverage loss | **Negative** |
| **AU.SEQ** | Undetected sequential — coverage loss | **Negative** |
| **AU.TC** | Undetected timing conflict — coverage loss | **Negative** |
| **AU.PC** | Undetected partial capture — coverage loss | **Negative** |
| **AU.BB** | Undetected black-box boundary | Excluded by convention |
| **UO.AAB** | Unobserved after activation blocked | **Negative** |
| **UC** | Uncontrolled — coverage loss | **Negative** |

## Procedure

### Step 1 — Inventory the Input Files
```bash
ls -lh <input_dir>/
```
Verify all three file types are present and record their sizes.

### Step 2 — Parse the Constraint File
Read the full `.do` file (it is small, ~1–5 KB). Extract:
- `add_clocks` — which clocks are defined and at what value (0 = constant, else functional)
- `add_input_constraints` — forced input values during test
- `add_black_boxes` — modules treated as opaque (uncontrollable outputs, unobservable inputs)
- `set_relevant_coverage -exclude` — fault classes removed from denominator
- `set_test_setup_icall` / PDL commands — test mode setup

### Step 3 — Fault Statistics Summary
```bash
zcat <fault_list>.mtfi.gz | grep -E ",[ ]*(AU|UO|UC|DS|DI|TI|UU)" \
  | awk -F',' 'NF>=3{print $2}' | sed 's/[ ]//g' \
  | sort | uniq -c | sort -rn
```
Compute:
- **Detected** = DS + DI.* 
- **Coverage-loss** = AU + AU.SEQ + AU.TC + AU.PC + UO.* + UC
- **Coverage** = Detected / (Detected + Coverage-loss) × 100%

### Step 4 — Module-Level Hotspot Analysis
```bash
# Top-level module breakdown
zcat <fault_list>.mtfi.gz | grep -E ",[ ]*(AU|UO|UC)" \
  | awk -F'"' '{n=split($2,a,"/"); print a[2]}' \
  | sort | uniq -c | sort -rn | head -20

# Instance-level hotspots
zcat <fault_list>.mtfi.gz | grep -E ",[ ]*(AU|UO|UC)" \
  | awk -F'"' '{print $2}' | sed 's|/[^/]*$||' \
  | sort | uniq -c | sort -rn | head -20
```

### Step 5 — Extract All Unique Coverage-Loss Paths
```bash
zcat <fault_list>.mtfi.gz | grep -E ",[ ]*(AU|UO|UC)" \
  | awk -F'"' '{print $2}' | sort -u
```
Group by: module name, instance type, pin name pattern.

### Step 6 — Netlist Connectivity Analysis
For each hotspot instance, look up its cell type and pin connections:
```bash
# Find instance declaration
zcat <netlist>.v.gz | grep -A 5 "<instance_name>"

# Find cell type for all instances of a pattern
zcat <netlist>.v.gz | grep "<pattern>" | head -10
```

For each AU/UO/UC cell, determine:
- **Cell type** (from the library prefix, e.g. `g1minrf00aa1d48x5` = non-scan isolation repeater)
- **Fan-in nets** (input pin → driving net → driving instance)
- **Fan-out nets** (output pin → driven net → load instance)
- **Scan connectivity** — does it have SI/SO pins? If not → non-scan cell

### Step 7 — Root-Cause Classification
Apply these rules in order:

| Condition | Root Cause |
|-----------|-----------|
| Cell has no SI/SO, outputs don't reach scan FF | Non-scan boundary |
| Fault class AU.SEQ + upstream clock in `add_clocks 0` | Ungated/constant clock constraint |
| `clken` has AU.TC (SA1) + AU.PC (SA0) | Clock gate enable controllability |
| Driving nets come from `add_black_boxes` module | SRAM/BB boundary |
| Driving signal is power-domain control (PwrDn, isolation) | Uncontrolled power/isolation signal |
| `UO.AAB` on ICG `en`/`te`, `en` fed from AU net | Observability blocked by upstream AU |
| Clock tree node (HFSBUF/ZCTSBUF) with no `add_clocks` | Missing clock domain definition |
| Downstream of another AU gate | Cascaded undetectability — fix upstream first |

### Step 8 — Constraint Cross-Reference
For each root cause found, check whether a constraint explains or could fix it:
- `add_clocks 0 <clk>` → all downstream logic is AU.SEQ — **expected if intentional**
- Missing `add_input_constraints` on power-domain control → fix: add `add_input_constraints -c0`
- Missing `add_clocks` for a CTS segment → fix: add clock definition
- `add_black_boxes` → fix: provide model or `add_output_constraints` on BB outputs

### Step 9 — Produce the Output Report

Generate output in four sections (see [Output Format](#output-format) below).

---

## Output Format

### A. Executive Summary Table
```
| Metric              | Value |
| Total faults        | N     |
| DS                  | N (%) |
| DI.*                | N (%) |
| TI                  | N     |
| UU (BB excluded)    | N     |
| AU                  | N     |
| AU.SEQ              | N     |
| AU.TC               | N     |
| AU.PC               | N     |
| UO.*                | N     |
| UC                  | N     |
| Total coverage-loss | N     |
| Estimated coverage  | X.XX% |
```
Follow with: top 5 hotspot modules by fault count, top constraint impacts.

### B. Coverage-Loss Table
One row per **unique instance/pin group**. Columns:

| Column | Content |
|--------|---------|
| Hierarchical path | `/module/instance/pin` |
| Fault class | AU / AU.SEQ / AU.TC / AU.PC / UO.AAB / UC |
| Cell type | Library cell name |
| Fan-in | Driving net(s) |
| Fan-out | Driven net(s) / load |
| Ctrl issue? | Y/N |
| Obs issue? | Y/N |
| Constraint-related? | Y/N — cite specific constraint |
| Non-scan boundary? | Y/N |
| Root cause | One of the 8 categories from Step 7 |
| Evidence | Quote netlist connectivity or constraint |
| Recommended action | Specific fix command or investigation step |

### C. Detailed Debug Narratives
For each root-cause cluster (not every individual fault), write:
1. **Structural signal path** — ASCII chain from driver to fault site to load
2. **Why ATPG cannot solve this** — exact mechanism
3. **Debug direction** — most actionable next step

### D. Final Diagnosis
| Item | Content |
|------|---------|
| Primary cause | Non-scan / Constraint / BB-boundary / Mix |
| Justified faults | Faults that are expected/intentional (cite dofile comment) |
| Fix priority 1 | Highest-impact action with estimated fault reduction |
| Fix priority 2 | Second action |
| Fix priority 3 | Third action |

---

## Quality Checks Before Finishing
- [ ] Every AU/UO/UC fault group has a root cause assigned
- [ ] No root cause is listed without netlist or constraint evidence
- [ ] Cascaded AU (output of AU cell is also AU) are noted but not double-counted as separate root causes
- [ ] Intentional/justified faults (e.g., `add_clocks 0` with design comment) are clearly labeled
- [ ] AU.BB count is 0 when `set_relevant_coverage -exclude AU.BB` is active — verify this
- [ ] Fix recommendations cite specific Tessent commands or netlist changes

---

## Common Cell Type Patterns (Intel/CBBB Library)
| Cell prefix | Type | Scan? | Notes |
|-------------|------|-------|-------|
| `g1minrf00a*` | Isolation repeater (AGR) | No | Non-scan; 48-bit repeater across power/clock domains |
| `g1mfsg*` / `g1mfun*` | Scan FF | Yes | Standard scan flip-flop |
| `g1mcirbc*` | ICG (Integrated Clock Gate) | No (gate itself) | `en` / `te` / `clk` / `clkout` |
| `g1mbfn*` / `g1mbfm*` | Buffer | No | HFSBUF, ZBUF, ROPT buf |
| `g1minv*` | Inverter | No | HFSINV, ZINV |
| `g1morn*` / `g1mand*` / `g1mnor*` | Combinational gate | No | OR, AND, NOR, ctmTdsLR |
| `g1mcbf*` / `g1mcbf*d*` | CTS buffer | No | Clock tree node |
| `cbb_base__clksrc*` | UCLK htree buffer | No | Clock source cell |
| `g1mmbn*` / `g1mmkn*` | Scan mux / hold mux | No (mux only) | wrp_hold_mux |
| `ip76d9hcr*` | SRAM macro | **Black-box** | Use `add_black_boxes` |

## Known ATPG Fault Subtype Meaning
| AU.SEQ | Sequential: fault activatable but capture clock missing |
| AU.TC | Timing conflict: fault activation causes clocking hazard |
| AU.PC | Partial capture: only one polarity can be tested |
| UO.AAB | After-Activation-Blocked: en toggled but blocked downstream |
