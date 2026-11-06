# Async_Tech_Sweep

This repo is a **characterization** of CHP circuits: every module exists to be
measured, not just to compile. A module with no sweep behind it is dead code
here.

## A new module is never just a .act file

When a new process is added to `src/`, the work is NOT done until all four of
these exist. Do all of them in the same pass, without being asked:

1. **`src/<mod>.act`** -- the process itself, with the header comment style the
   other files use, ending in a `// Sweep knobs:` line that names the real
   parameters.
2. **A sweep axis that actually produces a curve.** Pick the knob that the
   module's cost should scale with (port count, width, depth, fan-out) and give
   it **6 points**, matching the grids already in `MODULES`. One point is not a
   characterization. Secondary axes (series / facets) stay at <= 3 values,
   because that is all `plot.py` draws per panel.
3. **A builder + `MODULES` entry in `scripts/sweep.py`**, plus `QUICK` if a
   secondary axis needs pinning. The builder emits the `dut` wrapper, the
   harness sources/sinks, `watch`, and a correct `ops` count -- `ops` is the
   denominator of every metric, so getting it wrong silently scales the whole
   curve.
4. **A `PLOT_SPEC` entry in `scripts/plot.py`**, and a `plot_compare` call if
   the new module is the variant of something that already exists. The
   comparison is usually the actual result; the standalone curve is just
   context.

If the module needs a new kind of stimulus, it goes in `src/harness.act`, never
inside the `dut` wrapper -- the harness must stay out of the energy numbers.

## What makes a good sweep here

Always ask what the curve is supposed to *isolate*. A sweep is worth running
when the difference between two curves has a name: the arbiter
(`det_merge` vs `non_det_merge`), the selection itself (`cond_select`'s
always/never/random), the run-time control path (`det_merge` vs `cfg_merge`).
Prefer an axis where one module is the controlled baseline of the other and
everything else is held constant.
