// Synthetic partition A -- a golden-file fixture for the analyzer.
//
// Deliberately generic: block, cell and pin names follow no vendor's
// convention, so nothing in the tool may key off them. Cells are named for the
// STRUCTURE they exercise, not for any real design.

module top (input a, input clk, input se, input si, output y, output so);
  blk_a       u_blk_a   (.a(a), .y(y));
  blk_scan    u_blk_scan(.d(a), .clk(clk), .se(se), .si(si), .so(so));
  blk_tc      u_blk_tc  (.clk(clk), .y());
  blk_pc      u_blk_pc  (.a(a), .y());
  blk_aab     u_blk_aab (.a(a), .clk(clk), .y());
  blk_uc      u_blk_uc  (.a(a), .y());
endmodule

module blk_a (input a, output y);
  gcell_and2 u_and0 (.a(a), .b(a), .y(n0));
  gcell_and2 u_and1 (.a(n0), .b(a), .y(n1));
  gcell_buf  u_buf0 (.a(n1), .y(y));
endmodule

// Two scannable flops. u_ff0 is stitched; u_ff1 has a dangling scan-out and is
// therefore scan-capable but NOT chain-connected -- a generic scan-stitch
// artefact, not a scan/non-scan boundary.
module blk_scan (input d, input clk, input se, input si, output so);
  gcell_seq_scan u_ff0 (.d(d), .clk(clk), .se(se), .si(si), .so(mid), .q(q0));
  gcell_seq_scan u_ff1 (.d(q0), .clk(clk), .se(se), .si(mid),
                        .so(SYNOPSYS_UNCONNECTED_3), .q(so));
endmodule

// A constant reaches the data pins through a feedthrough port.
module blk_tc (input clk, output y);
  gcell_tiehi  u_const (.y(k));
  tc_inner     u_inner (.din(k), .clk(clk), .y(y));
endmodule

module tc_inner (input din, input clk, output y);
  gcell_seq_scan u_tc0 (.d(din), .clk(clk), .se(1'b0), .si(1'b0), .so(), .q(t0));
  gcell_seq_scan u_tc1 (.d(t0),  .clk(clk), .se(1'b0), .si(1'b0), .so(), .q(t1));
  gcell_seq_scan u_tc2 (.d(t1),  .clk(clk), .se(1'b0), .si(1'b0), .so(), .q(t2));
  gcell_seq_scan u_tc3 (.d(t2),  .clk(clk), .se(1'b0), .si(1'b0), .so(), .q(t3));
  gcell_seq_scan u_tc4 (.d(t3),  .clk(clk), .se(1'b0), .si(1'b0), .so(), .q(y));
endmodule

// Sites reached by a constrained pin.
module blk_pc (input a, output y);
  gcell_and2 u_pc0 (.a(a), .b(pin_hold), .y(p0));
  gcell_and2 u_pc1 (.a(p0), .b(pin_hold), .y(p1));
  gcell_and2 u_pc2 (.a(p1), .b(pin_hold), .y(y));
endmodule

// A scan flop whose only neighbours are combinational. Nothing here is a
// scan/non-scan boundary, however "non-scan" a plain AND gate looks.
module blk_aab (input a, input clk, output y);
  gcell_and2     u_ab0 (.a(a),  .b(a),  .y(m0));
  gcell_buf      u_ab1 (.a(m0), .y(m1));
  gcell_and2     u_ab2 (.a(m1), .b(a),  .y(m2));
  gcell_seq_scan u_ab3 (.d(m2), .clk(clk), .se(1'b0), .si(1'b0), .so(), .q(y));
endmodule

module blk_uc (input a, output y);
  gcell_and2 u_uc0 (.a(a),  .b(a), .y(c0));
  gcell_and2 u_uc1 (.a(c0), .b(a), .y(y));
endmodule
