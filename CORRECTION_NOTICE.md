# CORRECTION NOTICE — ATPG / DFT coverage-loss analysis, par_base_punit

**Status:** corrections to previously published findings. Nothing below silently
overwrites the earlier report; each item states what was published, what is
wrong with it, and what replaces it.

**Trigger:** the published answer for
`/punit/punit_inst/ptpcbclk/pcusapmbootfsm/start_fuse_sense_boot_fsm_ctrl/SRoutXnnnH_reg`
was contradicted by the netlist itself.

---

## C1 — RETRACTION: "SRoutXnnnH_reg is outside the scan chain"

**Published (WRONG):** the instance is non-scan, and non-scan logic cannot
sample it. An entire propagation/observability narrative was built on top of
that statement.

**Retracted.** The instance **is a scan cell.**

**Observed** — `par_base_punit.v.gz`, line 5259389, inside module
`par_base_punit_pcu_srmsff_with_xover_1_0_0_1`:

```verilog
g1mfuz043ac2n02x5 SRoutXnnnH_reg ( .si ( ropt_net_1667503 ) ,
.rb ( Reset_BAR ) , .d ( p11198 ) , .den ( SetCondXnnnH ) ,
.ssb ( HFSNET_1 ) , .clk ( clock_gate_logic_0 ) , .o ( SRoutXnnnH ) ,
.so ( test_so1976 ) ) ;
```

| Pin | Role | Net |
|---|---|---|
| `.si` | scan-data in | `ropt_net_1667503` |
| `.ssb` | shift enable | `HFSNET_1` |
| `.so` | scan out | `test_so1976` (module output port) |

A dedicated scan-data input **and** a shift-enable pin are both present, and the
scan output reaches a module output port. The cell is scan.

**How the wrong answer was reached:** it was not read from the netlist at all.
It was inferred from a fault-table row showing `mapped_instance='-'`,
`confidence='unresolved'`, `fanin=0`, `fanout=0`, `scan='N'`. Those zeros meant
*the extractor failed to map the object*, not *the object has no connections*.
The "N" meant *no evidence found*, not *confirmed non-scan*. Design intuition
("boot/reset FSMs are usually non-scan") supplied the rest.

---

## C2 — RETRACTION: mis-attributed fan-in and cell type

**Published (WRONG):** `SRoutXnnnH_reg` has fan-in 4 and cell type
`g1mfuz043ac3n02x5`, and its loss is explained by
`clock_reset_or_test_enable_blocking`.

**Retracted.** Those values belong to a **different instance**:

```
/punit/punit_inst/ptpcbclk/punit_ucie_reset_fsm/reset_bus_ignore_errors_reset_entry_reg/d
```

The `clock_reset_or_test_enable_blocking` narrative holds **only** for that
instance. It has no bearing on `SRoutXnnnH_reg`, whose cell type is
`g1mfuz043ac2n02x5` (note `ac2`, not `ac3`).

**Cause:** the leaf name `SRoutXnnnH_reg` occurs 110 times across replicated
modules. Rows from different instances sharing that leaf name were merged.

---

## C3 — RECLASSIFICATION: three boot-FSM sites are tied constants

**Published (WRONG):** `unresolved_connectivity`.

**Reclassified to:** `Tied / constant hardware condition` (`tied_constant`).

Sites:

- `/punit/punit_inst/ptpcbclk/pcusapmbootfsm/start_fuse_sense_boot_fsm_ctrl/SRoutXnnnH_reg/d`
- `/punit/punit_inst/ptpcbclk/pcusapmbootfsm/ratio_monitor_enable_boot_fsm_ctrl/SRoutXnnnH_reg/d`
- `/punit/punit_inst/ptpcbclk/pcusapmbootfsm/ese_deassert_pgcb_reset_boot_fsm_ctrl/SRoutXnnnH_reg/d`

**Evidence — four-level driver trace.** The `.d` pin is bound to `p11198`, which
is a feedthrough port, not a driven net, at every level until the last:

| Level | Module | Port / net | Bound to |
|---|---|---|---|
| 0 | `par_base_punit_pcu_srmsff_with_xover_1_0_0_1` | `.d ( p11198 )` | input port `p11198` |
| 1 | `pcusapmbootfsm` | feedthrough | — |
| 2 | `ptpcbclk` | feedthrough | — |
| 3 | parent of `ptpcbclk` | net `p25523` (line 5396702) | — |
| 4 | — | line 5832610 | `g1mtihi00ac3n02x5 optlc1874990 ( .o ( p25523 ) ) ;` |

`g1mtihi` is a **tie-high** cell: output pin only, no inputs. The `.d` input is
therefore a hard constant 1.

**Corroboration:**

- `optlc1874990` appears in the fault list itself as `UO`, fan-in 0, fan-out 1,
  cell `g1mtihi00ac3n02x5`.
- Net `p25523` has exactly two references in the whole netlist — the tie and
  this one load — confirming fan-out 1.

**Consequence:** a stuck-at fault on a pin held at a hard constant is
undetectable because no differing value can ever be established there. This is
**not scan-related and not an observability problem**. It is expected and
non-actionable; it should be waived, not debugged.

---

## C4 — PENDING MEASUREMENT: root-cause histogram after reclassification

**Published (now known to be contaminated):** `other_structural_cause` holds
42,731 of the 83,450 coverage-loss faults, and is visibly dominated by `optlc*`
instances of types `g1mtihi00*` and `g1mtilo00*`.

The `tied_constant` classifier that reclassifies these has landed (see
"What changed in the tool" below). **The new histogram is not reported here
because it has not been measured.** Publishing an estimated split would repeat
exactly the failure this notice exists to correct.

To produce it, re-run against the real inputs:

```bash
python -m atpg_coverage_debug_agent.cli \
    --netlist  <path>/par_base_punit.v.gz \
    --faults   <path>/<faultlist>.mtfi.gz \
    --constraints <path>/<constraints>.do \
    --report-md  corrected_report.md \
    --report-csv corrected_report.csv
```

Then report:

1. `tied_constant` count and its share of the previous 42,731.
2. The residual `other_structural_cause` count — the real debug target list.
3. `unresolved_connectivity`, now split by cause via `diagnose_unresolved`.

`tied_constant` faults must be flagged **expected / non-actionable** in that
report so no debug effort is spent on them.

---

## C5 — RETRACTION: "the loss is observability-dominant"

**Published (WRONG basis):** coverage loss is observability-dominant.

**Retracted as unsupported.** That conclusion was computed from buckets now
known to be contaminated by two artefacts:

1. **Tie-cell faults** counted as structural/observability loss. They are
   undetectable by construction and belong in their own non-actionable bucket.
2. **Unmapped objects** whose `fanin=0 / fanout=0` were read as "no observation
   path". They carry no connectivity information at all; 15,409 faults tagged
   `unresolved_connectivity` sit in this class.

Together these are large enough to move the ranking, so the priority list must
be recomputed, not adjusted. Until C4 is measured, the honest statement is:

> Unresolved — the dominant coverage-loss mechanism cannot be determined until
> tie-driven faults are separated out and the unmapped objects are either
> mapped or excluded from the accounting.

**Priority order to publish after the re-run:**

1. Residual `other_structural_cause` after `tied_constant` is removed.
2. `unresolved_connectivity`, split by cause (`absent_leaf` / `ambiguous` /
   `outside_scope`) — every root cause on these sites is unprovable until
   fixed, so this gates the accuracy of everything else.
3. Constraint-induced controllability / observability, on mapped sites only.
4. `tied_constant` — reported for completeness, flagged non-actionable.

---

## Open item — 15,409 `unresolved_connectivity` faults

Clustered on `ctech_lib_glbdrvuclk_dcszo/soft_high_out`, `ctmi_*`, and
`ctech_lib_mux_2to1_dcszo`. The tool now attributes each unmapped fault to one
of three causes (`diagnose_unresolved`), which distinguishes the three
possibilities the earlier report could not:

- **`absent_leaf`** — the parent module is present but the leaf is not: the
  cell model is missing (black box / library model not read in).
- **`ambiguous`** — the name repeats and the path did not narrow it to one
  instance.
- **`outside_scope`** — no segment of the path exists in the loaded netlist.

Run `diagnose_unresolved` on the real inputs to settle which applies to each
cluster. Until then, no root cause on those sites is provable.

---

## What changed in the tool

| Defect | Fix |
|---|---|
| Unmapped objects emitted `fanin=0 / fanout=0 / scan=N` | They now emit NULL / NULL / `unknown` in every renderer (CSV, Markdown, HTML, GUI, LLM payload, tool JSON). Zero and "no" are reserved for measured facts. |
| Scan status asserted from naming and table fields | `scan_status` decides only from a literally-read instantiation, and answers "Unresolved - scan status cannot be determined without netlist pin evidence." otherwise. |
| Leaf names repeating across replicated modules resolved to the wrong instance, or not at all | The mapper resolves the parent instance to its module type and finds the leaf inside that module body only. |
| Driver tracing stopped at the first feedthrough port | Driver resolution now crosses hierarchy in both directions until it reaches a gate with input pins. |
| Tie-driven faults landed in `other_structural_cause` | `tied_constant` takes precedence over every scan-boundary and observability rule. |
| `unresolved_connectivity` reported without a reason | Attributed to `absent_leaf` / `ambiguous` / `outside_scope`, and surfaced in the report warnings. |

Acceptance coverage lives in `tests/test_scan_status.py`.
