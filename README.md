# Async_Tech_Sweep

Characterize simple asynchronous (QDI / handshake) building blocks written in
ACT: synthesize each one for a target technology, simulate it back-annotated,
and sweep its template parameters to get a scaling curve.

```bash
make venv         # one-time: .venv with matplotlib + pandas (other targets do this on demand)
make quick        # every curve (6 points), secondary axes pinned -- faster check
make sweep        # the full sweep  -> sim/csv/*.csv  (8 points at once; JOBS=1 to serialize)
make plot         # render           -> sim/plots/*.png
make help         # targets and variables
```

---

## How a single measurement works

`make sweep` is three steps. Python only writes inputs and reads outputs;
every tool command lives in the Makefile (the *Tool commands* block).

1. **Generate wrappers** -- `sweep.py gen`. For each parameter point (say
   `multiplier`, width 8) it writes `sim/work/multiplier/width8/` with
   `dut.act` (a non-template proc `dut` holding one concrete instance of the
   module, plus a test bench `tb` that feeds it from `src/harness.act` sources
   and drains it into sinks), `top_decomp.act` (the same bench with
   `decomp_dut` swapped in) and `run.scr` (the actsim script). The point dirs
   are listed in `sim/work/points.mk`.

2. **Run the tools** -- `make points`, i.e. per point dir:
   - `synth.done`: `synth2 -T$(TECH) -o dut_decomp.act -F decomp -t -E genus -p dut dut.act`
     (retried with abc if Genus fails) produces the decomposed CHP and
     `decomp_sim.conf`, the per-action delay and energy tables.
   - `sim.done`: `actsim -ref=1 -cnf=decomp_sim.conf top_decomp.act tb_d < run.scr`

   Each stage logs to `<stage>.log`, its wall time to `<stage>.time`, and
   touches its `.done` file only on success. Points are independent, so
   `make sweep` runs 8 at once (`JOBS=`), and a single point can be rerun by hand:
   `make sim/work/multiplier/width8/sim.done`.

3. **Collect** -- `sweep.py collect` runs `scripts/extract_metrics.py` on each
   dir (`energy.txt`, `sim.log`) and writes one CSV row per
   point; a missing `.done` becomes `SYNTH_FAIL` / `SIM_FAIL`.

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

`ops` is deliberately **not** `tokens` (completed output actions). A copy with 4
outputs emits 4 tokens per input; normalising by tokens would make a wider, more
expensive fork look 4x cheaper.

---

## Two caveats about what synth2 charges

**1. Energy covers only the mapped expressions.** `decomp_sim.conf` assigns
each action of the decomposed CHP a delay and an energy, and the energies come
from the expressions Genus maps (`a+b`, a `SEL` compare, a guard `c = 1`).
Handshakes, probes and plain data moves have no energy entry, so a module made
only of those -- `buffer`, `copy`, `non_det_merge` -- reports 0, and
`cond_adder`'s selection costs exactly one guard comparison. If Genus fails and
the abc fallback runs, the energy table is all 0; the `synth_backend` column
says which one ran.

**2. Handshake delays are the technology's defaults.** Expression delays come
from the mapped logic, but channel `send`/`recv` and selection delays are the
constants in `$ACT_HOME/conf/$(TECH)/synth.conf` (`send_delay`, `recv_delay`,
`sel_delays`, ...). So a probed merge's arbitration is timed by those defaults,
not by a modelled arbiter. Relative comparisons between modules are meaningful;
absolute picoseconds are only as good as that table.

---

## The modules

All are in `src/`, all templated on at least `width`.

| Module | Sweep knobs | What it is for |
|---|---|---|
| `multiplier` | width | datapath cost vs width |
| `adder` | width | the cheap baseline |
| `mac` | width | multiply-accumulate, accumulator `2*width + 8` bits; `RST` is held at 0, so the reset selection is in the circuit but never taken |
| `buffer` | width, depth | slack chain: shows that depth buys latency, not throughput |
| `copy` | width, outDim | fork / broadcast |
| `split` | width, outDim | demux, the input stage of the crossbar |
| `det_merge` | width, inDim | controlled merge -- a `SEL` channel picks, no arbiter |
| `non_det_merge` | width, inDim | **probed** arbitrated merge; same CHP as SPACE's `Non_Det_Merge_Single` |
| `cond_select` | width, variant, mode | what a CHP `[ c=1 -> OUT!a+b [] else -> skip ]` costs |
| `xbar_det` | width, ports | crossbar, split + `det_merge`, no arbitration |
| `xbar_nd` | width, ports | crossbar, split + **probed** `non_det_merge` |
| `xbar_nd_io` | width, inDim, outDim | the same crossbar, inputs x outputs swept separately (2x2 .. 2x16, 4x4 .. 4x32) |

### The crossbar's two types

Both are `split` on the input side and a merge on the output side; the merge is
the variable.

- **`xbar_nd`** uses the probed `non_det_merge`, so destinations can collide and
  the arbiter resolves them. The template can be rectangular, but it is swept
  square (`ports` = N x N) so it lines up port-for-port with `xbar_det`. Its
  energy is the splits' only: the probed merges have no expression for Genus to
  map, so synth2 charges them 0 (see `non_det_merge`).
- **`xbar_det`** uses `det_merge`, driven by a rotation schedule from the test
  bench (round *r*: input *i* -> output *(i+r) mod N*, output *j* takes input
  *(j-r) mod N*). That is a permutation, so nothing ever contends and the
  crossbar cannot deadlock without an arbiter. The schedule only exists for a
  **square** crossbar, which is why `xbar_det` takes a single `N`.

### `cond_select` -- the cost of `[ ]`

- `cond_adder` -- `[ c=1 -> OUT!a+b [] else -> skip ]`
- `plain_adder` -- `OUT!a+b`, no selection; it still receives `CTRL`, so its
  handshakes match `cond_adder(always)` exactly

`mode` pins `cond_adder`'s `CTRL`:

- `always` -- every token takes the `a+b` path; vs `plain_adder` this is
  **the overhead of the selection itself**
- `never` -- every token takes the `skip` path (no output at all)
- `random` -- 50/50, what a data-dependent branch really costs

On the `skip` path nothing is sent, so the run is timed on `CTRL` (received
every iteration) as well as `OUT`, and `tokens` counts only the outputs.

---

## Layout

```
Makefile              sweep / plot / clean, and every tool command
src/*.act             the modules, all templated
src/harness.act       test-bench-only sources and sinks (never synthesized)
scripts/sweep.py      sweep points; generates benches, collects CSVs
scripts/extract_metrics.py   parses one run directory into metrics
scripts/plot.py       sim/csv/*.csv -> sim/plots/*.png
sim/csv/              results, one CSV per module, one row per parameter point
sim/plots/            charts
sim/work/             per-run scratch (generated; make clean removes it)
```
