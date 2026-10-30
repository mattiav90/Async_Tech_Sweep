# Async_Tech_Sweep

Characterize simple asynchronous (QDI / handshake) building blocks written in
ACT: synthesize each one for a target technology, simulate it back-annotated,
and sweep its template parameters to get a scaling curve.

```bash
make quick        # small subset of every module -- check the flow works
make sweep        # the full sweep  -> sim/csv/*.csv
make plot         # render           -> sim/plots/*.png
make help         # targets and variables
```

---

## How a single measurement works

For one parameter point (say `multiplier`, width 8) the flow is:

1. **Generate a wrapper.** `scripts/sweep.py` writes `sim/work/multiplier/width8/dut.act`:
   a non-template proc `dut` holding one concrete instance of the module, plus a
   test bench `tb` that feeds it from `src/harness.act` sources and drains it
   into sinks.

2. **Synthesize only the DUT.**
   `synth2 -Tgeneric -o dut_decomp.act -F decomp -t -p dut dut.act`
   produces the decomposed CHP and `decomp_sim.conf`, which carries the
   per-action delay and energy tables.

3. **Simulate back-annotated.**
   `actsim -ref=1 -cnf=decomp_sim.conf top_decomp.act tb_d`
   where `top_decomp.act` is the same bench with `decomp_dut` swapped in.

4. **Parse.** `scripts/extract_metrics.py` reads `energy.txt` and the sim log
   into one CSV row.

Two design choices worth knowing:

- **The harness lives outside `dut`.** `synth2 -p dut` only decomposes `dut`, so
  the sources and sinks stay plain un-annotated CHP (~20-60 ps per action) and
  are never charged to the module. Only the module under test is measured.
- **`dut` is not a template.** `synth2 -p "multiplier<8>"` works, but names its
  output `decomp_multiplier_38_4` -- a mangled name that depends on internal
  counters. Wrapping it gives a predictable `decomp_dut` every time.

### Where the test benches are

They are **generated**, one per parameter point, into
`sim/work/<module>/<tag>/dut.act`. The generator for each module is the
`w_<module>()` function in `scripts/sweep.py`; edit that to change a bench.
Nothing under `sim/work/` is hand-maintained -- `make clean` deletes it all.

---

## What the numbers mean

| Column | Meaning |
|---|---|
| `ops` | times the module's main loop body ran -- the denominator |
| `total_delay_ps` | last timestamp in the simulation |
| `delay_per_op_ps` | `total_delay / ops`. A **throughput** figure (cycle time), not single-token latency: the sources run flat out, so the DUT is kept busy. |
| `dynamic_energy_fJ` | actsim's `Total:` switching energy |
| `energy_per_op_fJ` | `dynamic_energy / ops` |
| `area_um2` | `totalArea` from the optional `dflowmap` pass |

`ops` is deliberately **not** `tokens` (completed output actions). A copy with 4
outputs emits 4 tokens per input; normalising by tokens would make a wider, more
expensive fork look 4x cheaper.

---

## Three honest caveats about `-Tgeneric`

`generic` is the only usable technology on this machine: `sky130` ships no
`synth.conf`, and `tsmc65` is not installed.

**1. Energy is always 0.** `decomp_sim.conf` comes back with
`int_table energies 0 0 0 ...` and `real leakage 0.0`, and `energy -v` prints
`Total: 0 (0 W); area: 0`. The generic config ships no per-cell energy tables,
and the only logic-synthesis backend available (the built-in `abc` -- `genus`
and `yosys` are not installed) does not report power back. SPACE's real energy
numbers all came from `tsmc65` + `genus`.

*The plumbing is correct and untouched* -- `make sweep TECH=tsmc65` on a machine
with the PDK fills these columns in. `scripts/plot.py` drops the energy panel
rather than drawing a flat line at zero.

**2. Delay is real, but from dummy constants.** Delays come from `abc` mapping
expressions onto `osu018_stdcells.lib`, so they scale correctly with width and
logic depth. But the *handshake* delays around them come from
`conf/generic/synth.conf`, which labels its own values "these are dummy numbers"
(`send_delay 40.0`, `recv_delay 40.0`, ...). So **relative** comparisons between
modules are meaningful; absolute picoseconds are not silicon.

**3. Area is suppressed for composite modules.** Area comes from a *second*,
different flow (`synth2 -F dataflow` + `dflowmap`) -- so it describes the
dataflow implementation of the module, not the decomposed one that delay was
measured on. Worse, that flow drops array dimensions: a 4-deep `buffer` and a
1-deep one both report 194 um^2, and a 4x4 crossbar reports one split and one
merge. Those modules are marked `composite=True` in `sweep.py` and their area is
dropped with a reason in the `area_note` column, rather than published wrong.

Area is therefore trustworthy only for **leaf** modules (one CHP process in the
DUT): multiplier, adder, mac, copy, split, det_merge, cond_select.
Probe-based modules (`non_det_merge`, `xbar_nd`) get no area at all -- the
dataflow flow cannot synthesize probes, which is why SPACE replaces its
non-deterministic merges with handwritten dataflow before that step.

---

## The modules

All are in `src/`, all templated on at least `width`.

| Module | Sweep knobs | What it is for |
|---|---|---|
| `multiplier` | width | datapath cost vs width |
| `adder` | width | the cheap baseline |
| `mac` | width, terms | multiply-accumulate; `RST` on a channel starts a new accumulation, so there is **no loop in the hardware** -- the harness decides how long an accumulation is |
| `buffer` | width, depth | slack chain: shows that depth buys latency, not throughput |
| `copy` | width, outDim | fork / broadcast |
| `split` | width, outDim | demux, the input stage of the crossbar |
| `det_merge` | width, inDim | controlled merge -- a `SEL` channel picks, no arbiter |
| `non_det_merge` | width, inDim | **probed** arbitrated merge; same CHP as SPACE's `Non_Det_Merge_Single` |
| `cond_select` | width, variant, mode | what a CHP `[ g -> .. [] else -> .. ]` costs |
| `xbar_det` | width, ports | crossbar, split + `det_merge`, no arbitration |
| `xbar_nd` | width, inDim, outDim | crossbar, split + **probed** `non_det_merge` |

### The crossbar's two types

Both are `split` on the input side and a merge on the output side; the merge is
the variable.

- **`xbar_nd`** uses the probed `non_det_merge`, so destinations can collide and
  the arbiter resolves them. Free to be rectangular.
- **`xbar_det`** uses `det_merge`, driven by a rotation schedule from the test
  bench (round *r*: input *i* -> output *(i+r) mod N*, output *j* takes input
  *(j-r) mod N*). That is a permutation, so nothing ever contends and the
  crossbar cannot deadlock without an arbiter. The schedule only exists for a
  **square** crossbar, which is why `xbar_det` takes a single `N`.

### `cond_select` -- the cost of `[ ]`

Three processes with identical handshake counts (all three receive `CTRL`), so
the only difference is the selection:

- `plain_adder` -- always adds, no selection
- `skip_adder` -- never adds, no selection
- `cond_adder` -- `[ c=1 -> OUT!a+b [] else -> OUT!a ]`

and `mode` pins `CTRL` to `always` / `never` / `random`. So:

- `cond_adder(always)` vs `plain_adder` = **the overhead of the selection**
- `cond_adder(always)` vs `cond_adder(never)` = the cost of the adder path
- `cond_adder(random)` = what a data-dependent branch really costs

---

## Layout

```
Makefile              sweep / plot / clean
src/*.act             the modules, all templated
src/harness.act       test-bench-only sources and sinks (never synthesized)
scripts/sweep.py      generates benches, runs synth2 + actsim, writes CSVs
scripts/extract_metrics.py   parses one run directory into metrics
scripts/plot.py       sim/csv/*.csv -> sim/plots/*.png
sim/csv/              results, one CSV per module, one row per parameter point
sim/plots/            charts
sim/work/             per-run scratch (generated; make clean removes it)
```
