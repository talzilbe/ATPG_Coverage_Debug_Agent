# Synthetic partition A constraints.
#
# Exercises the dofile grammar: variables, continuations, collections,
# an unevaluated conditional and a directive the tool does not know.

set BLOCK top/blk_pc

add_input_constraints pin_hold C1
add_cell_constraints TX [get_pins -hier ${BLOCK}/u_pc* ]
add_clocks 0 clk -pulse_always
add_primary_inputs a
add_primary_outputs y
add_output_masks so
set_atpg_limits -abort_limit 500
add_cell_constraints T0 \
    top/blk_uc/u_uc0 \
    top/blk_uc/u_uc1

if { $MODE == "internal" } {
    add_input_constraints se C0
}

# Not in the directive vocabulary: must be reported by name, never dropped.
site_specific_wrapper_command top/blk_a/u_and0
