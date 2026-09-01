// ---------------------------------------------------------------------------
// sample_data/demo_netlist.v
//
// A synthetic gate-level netlist built to exercise every part of the coverage
// triage. Each block below reproduces one situation the analysis distinguishes,
// so the shipped demo shows what the tool actually does rather than only its
// fallback behaviour.
//
//   fifo_blk    a test data register and a hardwired tie holding cones constant
//   crypto_blk  a fan-out that re-merges repeatedly (reconvergent complexity)
//   io_blk      a cone fed by a constrained primary input
//   mem_blk     a long chain of unscanned flops (sequential depth)
//   dead_blk    a cone with no scan cell downstream (observability gap)
//   good_blk    a healthy cone with several independent capture points
//
// Pair it with demo_faults.mtfi and demo_constraints.do.
// ---------------------------------------------------------------------------

// --- Constant-driven cones -------------------------------------------------
// uf_tdr_out_inter_reg is a test data register: its value is set per ATPG run,
// so faults behind it are recoverable with a topoff. uf_tie_lo is hardwired and
// is not.
module fifo_blk (clk, pi_cfg, din, dout0, dout1);
  input  clk, pi_cfg, din;
  output dout0, dout1;
  wire   n_tdr, n_tie;
  wire   n_f0, n_f1, n_f2, n_f3, n_f4, n_f5;
  wire   n_g0, n_g1, n_g2;

  DFF   uf_tdr_out_inter_reg ( .D(pi_cfg), .CK(clk), .Q(n_tdr) );
  AND2  uf_and_0 ( .A(n_tdr), .B(din),  .Y(n_f0) );
  AND2  uf_and_1 ( .A(n_tdr), .B(n_f0), .Y(n_f1) );
  AND2  uf_and_2 ( .A(n_tdr), .B(n_f1), .Y(n_f2) );
  BUF   uf_and_3 ( .A(n_f2),  .Y(n_f3) );
  BUF   uf_and_4 ( .A(n_f3),  .Y(n_f4) );
  BUF   uf_and_5 ( .A(n_f4),  .Y(n_f5) );
  SDFF  uf_scan_0 ( .D(n_f5), .CK(clk), .Q(dout0) );

  TIE0  uf_tie_lo ( .Y(n_tie) );
  AND2  uf_gate_0 ( .A(n_tie), .B(din),  .Y(n_g0) );
  AND2  uf_gate_1 ( .A(n_tie), .B(n_g0), .Y(n_g1) );
  BUF   uf_gate_2 ( .A(n_g1),  .Y(n_g2) );
  SDFF  uf_scan_1 ( .D(n_g2), .CK(clk), .Q(dout1) );
endmodule


// --- Reconvergent fan-out --------------------------------------------------
// uc_head fans out to two branches that re-merge across six gates. ATPG can
// reach an observation point, but only by satisfying many conditions at once.
module crypto_blk (clk, cin, cout);
  input  clk, cin;
  output cout;
  wire   uc_n_head, uc_n_a, uc_n_b;
  wire   uc_n_m0, uc_n_m1, uc_n_m2, uc_n_m3, uc_n_m4, uc_n_m5;
  wire   uc_n_j0, uc_n_j1, uc_n_j2, uc_n_j3, uc_n_j4;

  BUF   uc_head ( .A(cin), .Y(uc_n_head) );
  BUF   uc_a    ( .A(uc_n_head), .Y(uc_n_a) );
  BUF   uc_b    ( .A(uc_n_head), .Y(uc_n_b) );

  AND2  uc_m0 ( .A(uc_n_a), .B(uc_n_b), .Y(uc_n_m0) );
  AND2  uc_m1 ( .A(uc_n_a), .B(uc_n_b), .Y(uc_n_m1) );
  AND2  uc_m2 ( .A(uc_n_a), .B(uc_n_b), .Y(uc_n_m2) );
  AND2  uc_m3 ( .A(uc_n_a), .B(uc_n_b), .Y(uc_n_m3) );
  AND2  uc_m4 ( .A(uc_n_a), .B(uc_n_b), .Y(uc_n_m4) );
  AND2  uc_m5 ( .A(uc_n_a), .B(uc_n_b), .Y(uc_n_m5) );

  // Every branch merges back together, so no net is left dangling and the
  // whole cone shares one capture point.
  AND2  uc_j0 ( .A(uc_n_m0), .B(uc_n_m1), .Y(uc_n_j0) );
  AND2  uc_j1 ( .A(uc_n_m2), .B(uc_n_m3), .Y(uc_n_j1) );
  AND2  uc_j2 ( .A(uc_n_m4), .B(uc_n_m5), .Y(uc_n_j2) );
  AND2  uc_j3 ( .A(uc_n_j0), .B(uc_n_j1), .Y(uc_n_j3) );
  AND2  uc_j4 ( .A(uc_n_j3), .B(uc_n_j2), .Y(uc_n_j4) );
  SDFF  uc_scan ( .D(uc_n_j4), .CK(clk), .Q(cout) );
endmodule


// --- Constrained primary input ---------------------------------------------
// pi_hold is fixed by demo_constraints.do, so this whole cone is held.
module io_blk (clk, pi_hold, iin, iout);
  input  clk, pi_hold, iin;
  output iout;
  wire   ui_n0, ui_n1, ui_n2, ui_n3;

  AND2  ui_buf_0 ( .A(pi_hold), .B(iin),   .Y(ui_n0) );
  AND2  ui_buf_1 ( .A(pi_hold), .B(ui_n0), .Y(ui_n1) );
  BUF   ui_buf_2 ( .A(ui_n1), .Y(ui_n2) );
  BUF   ui_buf_3 ( .A(ui_n2), .Y(ui_n3) );
  SDFF  ui_scan  ( .D(ui_n3), .CK(clk), .Q(iout) );
endmodule


// --- Long unscanned chain --------------------------------------------------
// Nine non-scan flops sit between the fault sites and the only capture point.
module mem_blk (clk, min, mout);
  input  clk, min;
  output mout;
  wire   um_n0, um_n1, um_n2, um_n3, um_n4;
  wire   um_n5, um_n6, um_n7, um_n8, um_n9;

  BUF      um_head ( .A(min), .Y(um_n0) );
  DFF_nsff um_d0 ( .D(um_n0), .CK(clk), .Q(um_n1) );
  DFF_nsff um_d1 ( .D(um_n1), .CK(clk), .Q(um_n2) );
  DFF_nsff um_d2 ( .D(um_n2), .CK(clk), .Q(um_n3) );
  DFF_nsff um_d3 ( .D(um_n3), .CK(clk), .Q(um_n4) );
  DFF_nsff um_d4 ( .D(um_n4), .CK(clk), .Q(um_n5) );
  DFF_nsff um_d5 ( .D(um_n5), .CK(clk), .Q(um_n6) );
  DFF_nsff um_d6 ( .D(um_n6), .CK(clk), .Q(um_n7) );
  DFF_nsff um_d7 ( .D(um_n7), .CK(clk), .Q(um_n8) );
  DFF_nsff um_d8 ( .D(um_n8), .CK(clk), .Q(um_n9) );
  SDFF     um_scan ( .D(um_n9), .CK(clk), .Q(mout) );
endmodule


// --- No capture point ------------------------------------------------------
// This cone terminates at a primary output with no scan cell anywhere in it.
module dead_blk (din, dout);
  input  din;
  output dout;
  wire   ud_n0, ud_n1, ud_n2;

  BUF  ud_dead_0 ( .A(din),   .Y(ud_n0) );
  BUF  ud_dead_1 ( .A(ud_n0), .Y(ud_n1) );
  BUF  ud_dead_2 ( .A(ud_n1), .Y(ud_n2) );
  BUF  ud_dead_3 ( .A(ud_n2), .Y(dout)  );
endmodule


// --- Healthy logic ---------------------------------------------------------
// Several independent capture points: nothing structural blocks these.
module good_blk (clk, gin, gout0, gout1, gout2);
  input  clk, gin;
  output gout0, gout1, gout2;
  wire   ug_n0;

  BUF   ug_head  ( .A(gin), .Y(ug_n0) );
  SDFF  ug_scan_0 ( .D(ug_n0), .CK(clk), .Q(gout0) );
  SDFF  ug_scan_1 ( .D(ug_n0), .CK(clk), .Q(gout1) );
  SDFF  ug_scan_2 ( .D(ug_n0), .CK(clk), .Q(gout2) );
endmodule


module top (clk, pi_cfg, pi_hold, din, cin, iin, min, gin,
            dout0, dout1, cout, iout, mout, ddout, gout0, gout1, gout2);
  input  clk, pi_cfg, pi_hold, din, cin, iin, min, gin;
  output dout0, dout1, cout, iout, mout, ddout, gout0, gout1, gout2;

  fifo_blk   u_fifo   ( .clk(clk), .pi_cfg(pi_cfg), .din(din),
                        .dout0(dout0), .dout1(dout1) );
  crypto_blk u_crypto ( .clk(clk), .cin(cin), .cout(cout) );
  io_blk     u_io     ( .clk(clk), .pi_hold(pi_hold), .iin(iin), .iout(iout) );
  mem_blk    u_mem    ( .clk(clk), .min(min), .mout(mout) );
  dead_blk   u_dead   ( .din(din), .dout(ddout) );
  good_blk   u_good   ( .clk(clk), .gin(gin), .gout0(gout0),
                        .gout1(gout1), .gout2(gout2) );
endmodule
