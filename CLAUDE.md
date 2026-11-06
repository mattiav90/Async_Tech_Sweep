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

## Technology: the repo stays on tsmc65

`TECH ?= tsmc65` in the Makefile is the committed default and must stay that
way. Testing locally with `TECH=generic` is fine -- pass it on the command
line, `make sweep TECH=generic`, and never edit the default to generic and
leave it there. Same for the CSVs: a `tech=generic` row is a smoke-test
artifact, not a result, so delete it rather than committing it next to the
tsmc65 numbers.

## Local runs produce MEANINGLESS numbers -- they only set up the flow

Locally there is no Genus, so `make sweep TECH=generic` falls back to
`synth2` with abc. That run WILL print delays and energies. **Those numbers are
meaningless. Do not look at them, do not reason about them, and never report
them.** abc leaves the energy tables in `decomp_sim.conf` at zero and does not
back-annotate delay, so `E/op` comes out 0.0 and every point on the curve comes
back with the same delay no matter what the sweep knob does.

The only thing a local run proves is that the FLOW works: points generate,
synth2 is actually invoked, actsim runs and counts the right number of tokens,
and the CSV gets written. That is the entire purpose of running it. Check
`status == OK` and the token count, then stop -- a flat curve is the expected
outcome, not a finding.

Real delay and energy come only from the tsmc65 + Genus run on the synthesis
machine, which is run by hand, not here.

## What makes a good sweep here

Always ask what the curve is supposed to *isolate*. A sweep is worth running
when the difference between two curves has a name: the arbiter
(`det_merge` vs `non_det_merge`), the selection itself (`cond_select`'s
always/never/random), the run-time control path (`det_merge` vs `config_merge`).
Prefer an axis where one module is the controlled baseline of the other and
everything else is held constant.
