#!/usr/bin/env python3
"""
Generate the per-point test benches, and collect results into CSVs.

The tools themselves (synth2, actsim) are run by the Makefile, not
from here. `make sweep` is three steps:

  1. sweep.py gen       for every (module, parameter) point, write
                        sim/work/<module>/<tag>/ with
                          dut.act         a NON-template proc `dut` holding one
                                          concrete instance of the module, plus a
                                          test bench `tb` that feeds it from
                                          harness.act sources and drains it into
                                          sinks. The harness sits OUTSIDE `dut`
                                          on purpose: synth2 only decomposes
                                          `dut`, so only the module under test
                                          is back-annotated and charged.
                          top_decomp.act  the same bench around `decomp_dut`
                          run.scr         the actsim command script
                        and list the point dirs in sim/work/points.mk.

  2. make points        synthesize and simulate every listed dir -- see the
                        tool rules in the Makefile.

  3. sweep.py collect   parse each dir into a row of sim/csv/<module>.csv.

Why `dut` is not a template: `synth2 -p "multiplier<8>"` works, but names its
output `decomp_multiplier_38_4` -- a mangled name that depends on internal
counters and is miserable to script against. Wrapping in a plain `dut` gives a
predictable `decomp_dut` every time.
"""

import argparse
import csv
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_metrics  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
SIM = os.path.join(REPO, "sim")
# sim/ holds three sibling folders: csv/ (results), plots/ (charts, written by
# plot.py) and work/ (per-run scratch).
CSV = os.path.join(SIM, "csv")
WORK = os.path.join(SIM, "work")



def clog2(n):
    """Match std::ceil_log2 in ACT: bits needed to index n things."""
    return max(1, (n - 1).bit_length())


# ============================================================================
# Wrapper generation
# ----------------------------------------------------------------------------
# Each builder returns a spec describing the wrapper to emit:
#   imports   extra .act files to import
#   ports     the port list of `dut`
#   body      the module instance(s) inside `dut`
#   chans     channel declarations in the test bench
#   hookup    connections from test-bench channels to the dut instance `d`
#   drivers   harness instances (sources and sinks)
#   watch     dut ports to watch / chcount, i.e. the module's outputs
#   track     (optional) extra dut ports watched only to time the run, for a
#             module that may finish without producing any output
# ============================================================================


def w_multiplier(p, n):
    W = p["width"]
    return dict(
        imports=["multiplier.act"],
        ports=f"chan?(int<{W}>) A, B; chan!(int<{2*W}>) OUT",
        body=f"multiplier<{W}> m(A, B, OUT);",
        chans=[f"chan(int<{W}>) a, b;", f"chan(int<{2*W}>) o;"],
        hookup=["d.A = a;", "d.B = b;", "d.OUT = o;"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa(a);",
            f"rand_src<{W}, 1013904223, {n}> sb(b);",
            f"nsink<{2*W}> k(o);",
        ],
        watch=["d.OUT"],
        ops=n,
    )


def w_adder(p, n):
    W = p["width"]
    return dict(
        imports=["adder.act"],
        ports=f"chan?(int<{W}>) A, B; chan!(int<{W+1}>) OUT",
        body=f"adder<{W}> m(A, B, OUT);",
        chans=[f"chan(int<{W}>) a, b;", f"chan(int<{W+1}>) o;"],
        hookup=["d.A = a;", "d.B = b;", "d.OUT = o;"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa(a);",
            f"rand_src<{W}, 1013904223, {n}> sb(b);",
            f"nsink<{W+1}> k(o);",
        ],
        watch=["d.OUT"],
        ops=n,
    )


# Accumulator guard bits above the 2*width product: room for 2^8 = 256 terms.
MAC_GUARD = 8


def w_mac(p, n):
    W = p["width"]
    OW = 2 * W + MAC_GUARD
    # The sweep is over width only. RST is held at 0, so the accumulation never
    # restarts: the reset selection is still in the circuit and evaluated on
    # every token, it just never takes the rst=1 branch.
    return dict(
        imports=["mac.act"],
        ports=f"chan?(int<{W}>) A, B; chan?(int<1>) RST; chan!(int<{OW}>) OUT",
        body=f"mac<{W}, {OW}> m(A, B, RST, OUT);",
        chans=[f"chan(int<{W}>) a, b;", "chan(int<1>) rs;", f"chan(int<{OW}>) o;"],
        hookup=["d.A = a;", "d.B = b;", "d.RST = rs;", "d.OUT = o;"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa(a);",
            f"rand_src<{W}, 1013904223, {n}> sb(b);",
            f"const_src<1, 0, {n}> sr(rs);",
            f"nsink<{OW}> k(o);",
        ],
        watch=["d.OUT"],
        ops=n,
    )


def w_buffer(p, n):
    W, D = p["width"], p["depth"]
    return dict(
        imports=["buffer.act"],
        ports=f"chan?(int<{W}>) IN; chan!(int<{W}>) OUT",
        body=f"buffer<{W}, {D}> m(IN, OUT);",
        chans=[f"chan(int<{W}>) a, o;"],
        hookup=["d.IN = a;", "d.OUT = o;"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa(a);",
            f"nsink<{W}> k(o);",
        ],
        watch=["d.OUT"],
        ops=n,
    )


def w_copy(p, n):
    W, M = p["width"], p["outDim"]
    return dict(
        imports=["copy.act"],
        ports=f"chan?(int<{W}>) IN; chan!(int<{W}>) OUT[{M}]",
        body=f"copy<{W}, {M}> m(IN, OUT);",
        chans=[f"chan(int<{W}>) a;", f"chan(int<{W}>) o[{M}];"],
        hookup=["d.IN = a;", f"(i : {M} : d.OUT[i] = o[i];)"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa(a);",
            f"nsink<{W}> k[{M}];",
            f"(i : {M} : o[i] = k[i].I;)",
        ],
        watch=[f"d.OUT[{j}]" for j in range(M)],
        ops=n,
    )


def w_split(p, n):
    W, M = p["width"], p["outDim"]
    SW = clog2(M)
    return dict(
        imports=["split.act"],
        ports=f"chan?(int<{W}>) IN; chan?(int<{SW}>) SEL; chan!(int<{W}>) OUT[{M}]",
        body=f"split<{W}, {M}> m(IN, SEL, OUT);",
        chans=[f"chan(int<{W}>) a;", f"chan(int<{SW}>) s;", f"chan(int<{W}>) o[{M}];"],
        hookup=["d.IN = a;", "d.SEL = s;", f"(i : {M} : d.OUT[i] = o[i];)"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa(a);",
            f"rand_mod<{SW}, {M}, 1013904223, {n}> ss(s);",
            f"nsink<{W}> k[{M}];",
            f"(i : {M} : o[i] = k[i].I;)",
        ],
        watch=[f"d.OUT[{j}]" for j in range(M)],
        ops=n,
    )


def w_det_merge(p, n):
    W, M = p["width"], p["inDim"]
    SW = clog2(M)
    # Each input source must supply exactly the tokens SEL asks for. SEL is
    # round-robin (rot_fwd with offset 0), so over n rounds input i is selected
    # ceil((n-i)/M) times; giving every source n tokens is safe (sources that
    # still hold tokens at the end just stay blocked, which does not extend the
    # measured time because nothing downstream waits on them).
    return dict(
        imports=["det_merge.act"],
        ports=f"chan?(int<{W}>) IN[{M}]; chan?(int<{SW}>) SEL; chan!(int<{W}>) OUT",
        body=f"det_merge<{W}, {M}> m(IN, SEL, OUT);",
        chans=[f"chan(int<{W}>) a[{M}];", f"chan(int<{SW}>) s;", f"chan(int<{W}>) o;"],
        hookup=[f"(i : {M} : d.IN[i] = a[i];)", "d.SEL = s;", "d.OUT = o;"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa[{M}];",
            f"(i : {M} : sa[i].O = a[i];)",
            f"rot_fwd<{SW}, 0, {M}, {n}> ss(s);",
            f"nsink<{W}> k(o);",
        ],
        watch=["d.OUT"],
        ops=n,
    )


def w_non_det_merge(p, n):
    W, M = p["width"], p["inDim"]
    # n tokens per input, so the arbiter sees M*n arrivals and genuinely has to
    # resolve contention -- that is the cost we are trying to measure.
    return dict(
        imports=["non_det_merge.act"],
        ports=f"chan?(int<{W}>) IN[{M}]; chan!(int<{W}>) OUT",
        body=f"non_det_merge<{W}, {M}> m(IN, OUT);",
        chans=[f"chan(int<{W}>) a[{M}];", f"chan(int<{W}>) o;"],
        hookup=[f"(i : {M} : d.IN[i] = a[i];)", "d.OUT = o;"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa[{M}];",
            f"(i : {M} : sa[i].O = a[i];)",
            f"nsink<{W}> k(o);",
        ],
        watch=["d.OUT"],
        ops=n * M,
    )


def w_xbar_det(p, n):
    W, N = p["width"], p["ports"]
    SW = clog2(N)
    drivers = [f"rand_src<{W}, 2463534242, {n}> sa[{N}];",
               f"(i : {N} : sa[i].O = a[i];)"]
    # rot_fwd/rot_rev take their offset as a template pint, so one instance per
    # port rather than an array -- the offset differs per instance.
    for i in range(N):
        drivers.append(f"rot_fwd<{SW}, {i}, {N}, {n}> fw{i}(ds[{i}]);")
    for j in range(N):
        drivers.append(f"rot_rev<{SW}, {j}, {N}, {n}> rv{j}(sl[{j}]);")
    drivers += [f"nsink<{W}> k[{N}];", f"(i : {N} : o[i] = k[i].I;)"]
    return dict(
        imports=["crossbar.act"],
        ports=(f"chan?(int<{W}>) IN[{N}]; chan?(int<{SW}>) DST[{N}]; "
               f"chan?(int<{SW}>) SEL[{N}]; chan!(int<{W}>) OUT[{N}]"),
        body=f"xbar_det<{W}, {N}> m(IN, DST, SEL, OUT);",
        chans=[f"chan(int<{W}>) a[{N}];", f"chan(int<{SW}>) ds[{N}];",
               f"chan(int<{SW}>) sl[{N}];", f"chan(int<{W}>) o[{N}];"],
        hookup=[f"(i : {N} : d.IN[i] = a[i];)", f"(i : {N} : d.DST[i] = ds[i];)",
                f"(i : {N} : d.SEL[i] = sl[i];)", f"(i : {N} : d.OUT[i] = o[i];)"],
        drivers=drivers,
        watch=[f"d.OUT[{j}]" for j in range(N)],
        ops=n * N,
    )


def w_xbar_nd(p, n):
    # xbar_nd gives `ports` (square N x N, to line up with xbar_det);
    # xbar_nd_io gives inDim x outDim.
    W = p["width"]
    NI = p.get("inDim", p.get("ports"))
    NO = p.get("outDim", p.get("ports"))
    SW = clog2(NO)
    drivers = [f"rand_src<{W}, 2463534242, {n}> sa[{NI}];",
               f"(i : {NI} : sa[i].O = a[i];)"]
    # Different seed per input so destinations collide the way they would in a
    # real network -- that contention is exactly what the arbiter is paid for.
    for i in range(NI):
        drivers.append(f"rand_mod<{SW}, {NO}, {1013904223 + i*7919}, {n}> dm{i}(ds[{i}]);")
    drivers += [f"nsink<{W}> k[{NO}];", f"(i : {NO} : o[i] = k[i].I;)"]
    return dict(
        imports=["crossbar.act"],
        ports=(f"chan?(int<{W}>) IN[{NI}]; chan?(int<{SW}>) DST[{NI}]; "
               f"chan!(int<{W}>) OUT[{NO}]"),
        body=f"xbar_nd<{W}, {NI}, {NO}> m(IN, DST, OUT);",
        chans=[f"chan(int<{W}>) a[{NI}];", f"chan(int<{SW}>) ds[{NI}];",
               f"chan(int<{W}>) o[{NO}];"],
        hookup=[f"(i : {NI} : d.IN[i] = a[i];)", f"(i : {NI} : d.DST[i] = ds[i];)",
                f"(i : {NO} : d.OUT[i] = o[i];)"],
        drivers=drivers,
        watch=[f"d.OUT[{j}]" for j in range(NO)],
        ops=n * NI,
    )



def w_cond_select(p, n):
    """Selection overhead: `variant` picks the process, `mode` drives CTRL.

    cond_adder is [ c=1 -> OUT!a+b [] else -> skip ]; plain_adder always does
    OUT!a+b but still consumes CTRL, so against cond_adder(always) the only
    difference left is the `[ ]`. On the skip path nothing is sent, so the run
    is timed (and checked) on CTRL, which every iteration receives.
    """
    W, variant, mode = p["width"], p["variant"], p["mode"]
    ctrl = {
        "always": f"const_src<1, 1, {n}> sc(c);",
        "never":  f"const_src<1, 0, {n}> sc(c);",
        "random": f"rand_mod<1, 2, 1013904223, {n}> sc(c);",
    }[mode]
    return dict(
        imports=["cond_select.act"],
        ports=f"chan?(int<{W}>) A, B; chan?(int<1>) CTRL; chan!(int<{W+1}>) OUT",
        body=f"{variant}<{W}> m(A, B, CTRL, OUT);",
        chans=[f"chan(int<{W}>) a, b;", "chan(int<1>) c;", f"chan(int<{W+1}>) o;"],
        hookup=["d.A = a;", "d.B = b;", "d.CTRL = c;", "d.OUT = o;"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa(a);",
            f"rand_src<{W}, 1013904223, {n}> sb(b);",
            ctrl,
            f"nsink<{W+1}> k(o);",
        ],
        watch=["d.OUT"],
        track=["d.CTRL"],
        ops=n,
    )

# ============================================================================
# Sweep definitions
# ============================================================================

def grid(**axes):
    """Cartesian product of named axes, as a list of dicts."""
    keys = list(axes)
    out = [{}]
    for k in keys:
        out = [dict(d, **{k: v}) for d in out for v in axes[k]]
    return out


# Every curve gets 6 points along its x-axis (the knob plot.py puts there);
# series/facet axes stay at <= 3 values, the most plot.py draws per panel.
MODULES = {
    "multiplier":     dict(build=w_multiplier,     points=grid(width=[4, 6, 8, 10, 12, 16])),
    "adder":          dict(build=w_adder,          points=grid(width=[4, 8, 12, 16, 24, 32])),
    "mac":            dict(build=w_mac,            points=grid(width=[4, 6, 8, 10, 12, 16])),
    "buffer":         dict(build=w_buffer,         points=grid(width=[8], depth=[2, 4, 6, 8, 12, 16])),
    "copy":           dict(build=w_copy,           points=grid(width=[8], outDim=[2, 4, 6, 8, 12, 16])),
    "split":          dict(build=w_split,          points=grid(width=[8], outDim=[2, 4, 6, 8, 12, 16])),
    "det_merge":      dict(build=w_det_merge,      points=grid(width=[8], inDim=[2, 4, 6, 8, 12, 16])),
    "non_det_merge":  dict(build=w_non_det_merge,  points=grid(width=[8], inDim=[2, 4, 6, 8, 12, 16])),
    # Selection overhead. `mode` pins cond_adder's CTRL so the a+b path is
    # taken always / never (skip) / half the time; plain_adder is the
    # no-selection baseline.
    "cond_select":    dict(build=w_cond_select, points=[
        dict(width=w, variant=v, mode=m)
        for w in (8, 16)
        for v, m in (("cond_adder", "always"), ("cond_adder", "never"),
                     ("cond_adder", "random"), ("plain_adder", "always"))]),
    # The crossbar is the headline sweep: ports x width x (det vs probe).
    "xbar_det":       dict(build=w_xbar_det,       points=grid(width=[4, 8], ports=[2, 4, 6, 8, 12, 16])),
    "xbar_nd":        dict(build=w_xbar_nd,        points=grid(width=[4, 8], ports=[2, 4, 6, 8, 12, 16])),
    # The same arbitrated crossbar, inputs x outputs swept separately.
    "xbar_nd_io":     dict(build=w_xbar_nd, points=[
        dict(width=w, inDim=i, outDim=o)
        for w in (4, 8)
        for i, o in ((2, 2), (2, 4), (2, 8), (2, 16),
                     (4, 4), (4, 8), (4, 16), (4, 32))]),
}


def pin(module, **fixed):
    """MODULES[module]'s points with some axes held at a single value."""
    return [p for p in MODULES[module]["points"]
            if all(p[k] == v for k, v in fixed.items())]


# A faster subset: the full x-axis of every curve, but only one value of each
# secondary axis, so every plot still gets its 6 points.
QUICK = {m: d["points"] for m, d in MODULES.items()}
QUICK.update(
    cond_select=pin("cond_select", width=8),
    xbar_det=pin("xbar_det", width=4),
    xbar_nd=pin("xbar_nd", width=4),
    xbar_nd_io=pin("xbar_nd_io", width=4),
)


# ============================================================================
# File emission
# ============================================================================

ACT_HEADER = 'import std;\nimport "harness.act";\n'


def emit_wrapper(spec, path):
    """dut.act: the `dut` wrapper plus the pure-CHP test bench `tb`."""
    L = [ACT_HEADER]
    for im in spec["imports"]:
        L.append(f'import "{im}";')
    L.append("")
    L.append(f'defproc dut ({spec["ports"]})')
    L.append("{")
    L.append(f'  {spec["body"]}')
    L.append("}")
    L.append("")
    L.append(_tb("tb", "dut", spec))
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


def emit_top_decomp(spec, path):
    """top_decomp.act: the same bench, but instantiating the DECOMPOSED dut."""
    L = ['import "dut_decomp.act";', ""]
    L.append(_tb("tb_d", "decomp_dut", spec))
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


def _tb(name, dut_type, spec):
    L = [f"defproc {name} ()", "{"]
    for c in spec["chans"]:
        L.append(f"  {c}")
    L.append(f"  {dut_type} d;")
    for h in spec["hookup"]:
        L.append(f"  {h}")
    for drv in spec["drivers"]:
        L.append(f"  {drv}")
    L.append("}")
    return "\n".join(L)


def emit_script(spec, path):
    """actsim command script: watch outputs, run, count tokens, dump energy."""
    chans = spec["watch"] + spec.get("track", [])
    L = []
    for w in chans:
        L.append(f"watch {w}")
    L.append("cycle")
    for w in chans:
        L.append(f"chcount {w}")
    L.append("energy -v energy.txt")
    L.append("exit")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


# ============================================================================
# gen / collect
# ============================================================================

def tag_of(params):
    return "_".join(f"{k}{v}" for k, v in sorted(params.items()))


def point_dir(module, params):
    return os.path.join(WORK, module, tag_of(params))


def selected(args):
    """[(module, [params, ...])] picked by --module / --quick."""
    names = args.module or list(MODULES)
    bad = [m for m in names if m not in MODULES]
    if bad:
        sys.exit(f"unknown module(s): {bad}\nknown: {list(MODULES)}")
    return [(m, QUICK[m] if args.quick else MODULES[m]["points"]) for m in names]


def gen(args):
    """Write a fresh work dir per point, and list them in sim/work/points.mk."""
    dirs = []
    for module, pts in selected(args):
        build = MODULES[module]["build"]
        for params in pts:
            wd = point_dir(module, params)
            # Start clean so no stage result from an older run survives.
            shutil.rmtree(wd, ignore_errors=True)
            os.makedirs(wd)
            spec = build(params, args.inputs)
            emit_wrapper(spec, os.path.join(wd, "dut.act"))
            emit_top_decomp(spec, os.path.join(wd, "top_decomp.act"))
            emit_script(spec, os.path.join(wd, "run.scr"))
            dirs.append(os.path.relpath(wd, REPO))
    with open(os.path.join(WORK, "points.mk"), "w") as f:
        f.write("# Written by `sweep.py gen`: the point dirs `make points` runs.\n")
        f.write("POINTS := \\\n  " + " \\\n  ".join(dirs) + "\n")
    print(f"generated {len(dirs)} point(s) in {os.path.relpath(WORK, REPO)}/")


def read_time(path):
    """Wall seconds from a `/usr/bin/time -f %e` file (last line), else 0."""
    try:
        with open(path) as f:
            return float(f.read().split()[-1])
    except (OSError, ValueError, IndexError):
        return 0.0


def collect_point(module, params, n, tech):
    """One CSV row from the stage outputs the Makefile left in the point dir.

    Each stage touches a `<stage>.done` file only when it succeeded, so a
    missing one is how a failure shows up here.
    """
    wd = point_dir(module, params)
    has = lambda f: os.path.exists(os.path.join(wd, f))  # noqa: E731
    spec = MODULES[module]["build"](params, n)

    row = dict(module=module, **params)
    row["inputs_per_source"] = n
    row["tech"] = tech
    # synth.time is the Genus attempt, synth_abc.time the fallback if it ran.
    row["synth_s"] = round(read_time(os.path.join(wd, "synth.time"))
                           + read_time(os.path.join(wd, "synth_abc.time")), 1)
    if has("synth.backend"):
        with open(os.path.join(wd, "synth.backend")) as f:
            row["synth_backend"] = f.read().strip()
    if not has("synth.done"):
        row["status"] = "SYNTH_FAIL"
        return row
    row["sim_s"] = round(read_time(os.path.join(wd, "sim.time")), 1)

    m = extract_metrics.collect(wd, ops=spec.get("ops"))
    # tokens counts outputs only; `track` channels just prove the run happened.
    counts = m.pop("chcounts", None) or {}
    m["tokens"] = sum(v for c, v in counts.items() if c in spec["watch"])
    ran = m["tokens"] or any(counts.get(c) for c in spec.get("track", []))
    row.update(m)
    row["status"] = "OK" if (has("sim.done") and ran) else "SIM_FAIL"
    return row


def collect(args):
    for module, pts in selected(args):
        print(f"\n=== {module}: {len(pts)} point(s) ===")
        rows = []
        for params in pts:
            row = collect_point(module, params, args.inputs, args.tech)
            rows.append(row)
            d = row.get("delay_per_op_ps")
            e = row.get("energy_per_op_fJ")
            print(f"  {tag_of(params):<40} {row['status']:<10} "
                  f"delay/op={d if d is None else round(d, 1)} ps  "
                  f"E/op={e if e is None else round(e, 2)} fJ")
        print(f"  -> {os.path.relpath(write_csv(module, rows), REPO)}")


# Column order: identity first, then the three things we actually plot.
COLS = ["module", "tech", "status", "width", "depth", "ports",
        "inDim", "outDim", "variant", "mode",
        "inputs_per_source", "ops", "tokens",
        "total_delay_ps", "delay_per_op_ps",
        "dynamic_energy_fJ", "energy_per_op_fJ",
        "leakage_power_W", "leakage_energy_fJ", "total_energy_fJ",
        "actsim_area",
        "synth_backend", "synth_s", "sim_s"]


def write_csv(module, rows):
    os.makedirs(CSV, exist_ok=True)
    path = os.path.join(CSV, f"{module}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--module", action="append",
                        help="module to sweep (repeatable); default: all")
    common.add_argument("-n", "--inputs", type=int, default=30,
                        help="tokens generated per source (default 30)")
    common.add_argument("--quick", action="store_true",
                        help="the QUICK subset instead of the full sweep")

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen", parents=[common],
                   help="write the per-point work dirs + sim/work/points.mk")
    c = sub.add_parser("collect", parents=[common],
                       help="parse the work dirs into sim/csv/<module>.csv")
    c.add_argument("--tech", default="generic",
                   help="technology name recorded in the CSV (the TECH it ran with)")
    args = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    gen(args) if args.cmd == "gen" else collect(args)


if __name__ == "__main__":
    main()
