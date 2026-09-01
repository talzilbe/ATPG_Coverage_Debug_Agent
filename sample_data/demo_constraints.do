# ---------------------------------------------------------------------------
# sample_data/demo_constraints.do
#
# Constraints for demo_netlist.v. pi_hold is fixed to 1, which is what makes the
# io_blk cone appear as AU.PC coverage loss; pi_cfg is masked, so it shows up as
# the diffuse / cross-partition case rather than a deliberately fixed pin.
# ---------------------------------------------------------------------------

add_input_constraints pi_hold C1
add_input_constraints pi_cfg CX

# Clock and reset definitions, used to recognise the test infrastructure.
clock clk
reset rst_n
test_en scan_en
