// ---------------------------------------------------------------------------
// sample_data/sample_netlist.v
// A small synthetic hierarchical gate-level netlist for demonstration/tests.
// It is intentionally simple but exercises: hierarchy, scan & non-scan flops,
// a tie cell, combinational logic, and a clock/reset/test-enable network.
// ---------------------------------------------------------------------------

module alu_block (clk, rst_n, test_se, a, b, sel, y);
  input  clk, rst_n, test_se;
  input  a, b, sel;
  output y;
  wire   n1, n2, n3, tie0;

  // Combinational logic
  AND2  U1 ( .A(a), .B(b), .Y(n1) );
  INV   U2 ( .A(n1), .Y(n2) );
  MUX2  U3 ( .A(n2), .B(b), .S(sel), .Y(n3) );

  // A scannable flop (scan cell by type) capturing the result.
  SDFF  reg_scan ( .D(n3), .CK(clk), .RN(rst_n), .SE(test_se), .Q(y) );

  // A tie cell driving a constant into nothing useful (structurally tied).
  TIE0  U_tie ( .Y(tie0) );
endmodule


module ctrl_block (clk, rst_n, test_se, din, dout);
  input  clk, rst_n, test_se;
  input  din;
  output dout;
  wire   c1, c2;

  // A non-scan flop in the observe path (blocks propagation for ATPG).
  DFF_nsff  reg_nonscan ( .D(din), .CK(clk), .RN(rst_n), .Q(c1) );
  BUF   U4 ( .A(c1), .Y(c2) );
  AND2  U5 ( .A(c2), .B(din), .Y(dout) );
endmodule


module top (clk, rst_n, test_se, a, b, sel, din, y, dout);
  input  clk, rst_n, test_se;
  input  a, b, sel, din;
  output y, dout;

  alu_block  u_alu  ( .clk(clk), .rst_n(rst_n), .test_se(test_se),
                      .a(a), .b(b), .sel(sel), .y(y) );

  ctrl_block u_ctrl ( .clk(clk), .rst_n(rst_n), .test_se(test_se),
                      .din(din), .dout(dout) );
endmodule
