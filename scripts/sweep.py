#!/usr/bin/env python3
"""
Drive the characterization sweep.

For every (module, parameter) point this does four things:

  1. generate a wrapper   `dut.act`  -- a NON-template proc `dut` holding one
     concrete instance of the module, plus a test bench `tb` that feeds it from
     harness.act sources and drains it into sinks. The harness sits OUTSIDE
     `dut` on purpose: synth2 only decomposes `dut`, so only the module under
     test is back-annotated and charged.

  2. synthesize           `synth2 -Tgeneric -F decomp -t -p dut dut.act`
     -> dut_decomp.act (the refined CHP) + decomp_sim.conf (per-action delay
     and energy tables).

  3. simulate             `actsim -ref=1 -cnf=decomp_sim.conf top_decomp.act tb_d`
     with the decomposed instance swapped in, watching the module's outputs so
     the log carries timestamps.

  4. optionally measure area via the dataflow flow (synth2 -F dataflow +
     dflowmap), which is the only path that reports real cell area under
     -Tgeneric.

Results are appended to sim/<module>.csv, one row per parameter point.

Why `dut` is not a template: `synth2 -p "multiplier<8>"` works, but names its
output `decomp_multiplier_38_4` -- a mangled name that depends on internal
counters and is miserable to script against. Wrapping in a plain `dut` gives a
predictable `decomp_dut` every time.
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_metrics  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
SIM = os.path.join(REPO, "sim")
# sim/ holds three sibling folders: csv/ (results), plots/ (charts, written by
# plot.py) and work/ (per-run scratch).
CSV = os.path.join(SIM, "csv")
WORK = os.path.join(SIM, "work")

TECH_DEFAULT = "generic"


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


def w_mac(p, n):
    W, K = p["width"], p["terms"]
    # accw must hold 2*width plus enough guard bits for a K-term accumulation.
    OW = 2 * W + clog2(K)
    # The mac has no internal loop: RST pulses every K tokens, which is what
    # makes an accumulation K terms long.
    return dict(
        imports=["mac.act"],
        ports=f"chan?(int<{W}>) A, B; chan?(int<1>) RST; chan!(int<{OW}>) OUT",
        body=f"mac<{W}, {OW}> m(A, B, RST, OUT);",
        chans=[f"chan(int<{W}>) a, b;", "chan(int<1>) rs;", f"chan(int<{OW}>) o;"],
        hookup=["d.A = a;", "d.B = b;", "d.RST = rs;", "d.OUT = o;"],
        drivers=[
            f"rand_src<{W}, 2463534242, {n}> sa(a);",
            f"rand_src<{W}, 1013904223, {n}> sb(b);",
            f"pulse_src<{K}, {n}> sr(rs);",
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
        composite=True,
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
        composite=True,
    )


def w_xbar_nd(p, n):
    W, NI, NO = p["width"], p["inDim"], p["outDim"]
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
        composite=True,
    )



def w_cond_select(p, n):
    """Selection overhead: `variant` picks the process, `mode` drives CTRL.

    plain_adder / skip_adder have no selection but still consume CTRL, so the
    handshake count is identical and the only difference left is the `[ ]`.
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


MODULES = {
    "multiplier":     dict(build=w_multiplier,     points=grid(width=[4, 6, 8, 12, 16])),
    "adder":          dict(build=w_adder,          points=grid(width=[4, 8, 16, 24, 32])),
    "mac":            dict(build=w_mac,            points=grid(width=[4, 8], terms=[4, 8, 16])),
    "buffer":         dict(build=w_buffer,         points=grid(width=[8], depth=[1, 2, 4, 8, 16])),
    "copy":           dict(build=w_copy,           points=grid(width=[8], outDim=[2, 3, 4, 6, 8])),
    "split":          dict(build=w_split,          points=grid(width=[8], outDim=[2, 3, 4, 6, 8])),
    "det_merge":      dict(build=w_det_merge,      points=grid(width=[8], inDim=[2, 3, 4, 6, 8])),
    "non_det_merge":  dict(build=w_non_det_merge,  points=grid(width=[8], inDim=[2, 3, 4, 6, 8])),
    # The crossbar is the headline sweep: ports x width x (det vs probe).
    # Selection overhead. plain/skip are the no-selection baselines; `mode`
    # pins CTRL so the adder path is always / never / randomly taken.
    "cond_select":    dict(build=w_cond_select, points=[
        dict(width=w, variant=v, mode=m)
        for w in (8, 16)
        for v, m in (("cond_adder", "always"), ("cond_adder", "never"),
                     ("cond_adder", "random"), ("plain_adder", "always"),
                     ("skip_adder", "never"))]),
    "xbar_det":       dict(build=w_xbar_det,       points=grid(width=[4, 8], ports=[2, 4, 8])),
    "xbar_nd":        dict(build=w_xbar_nd,        points=grid(width=[4, 8], inDim=[2, 4, 8], outDim=[2, 4, 8])),
}

# A fast subset, for checking the flow works before committing to the full run.
QUICK = {
    "multiplier":     grid(width=[4, 8]),
    "adder":          grid(width=[4, 8]),
    "mac":            grid(width=[4], terms=[4]),
    "buffer":         grid(width=[8], depth=[1, 4]),
    "copy":           grid(width=[8], outDim=[2, 4]),
    "split":          grid(width=[8], outDim=[2, 4]),
    "det_merge":      grid(width=[8], inDim=[2, 4]),
    "non_det_merge":  grid(width=[8], inDim=[2, 4]),
    "cond_select":    [dict(width=8, variant=v, mode=m)
                       for v, m in (("cond_adder", "always"),
                                    ("cond_adder", "never"),
                                    ("plain_adder", "always"))],
    "xbar_det":       grid(width=[4], ports=[2, 4]),
    "xbar_nd":        grid(width=[4], inDim=[2], outDim=[2, 4]),
}


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
    L = []
    for w in spec["watch"]:
        L.append(f"watch {w}")
    L.append("cycle")
    for w in spec["watch"]:
        L.append(f"chcount {w}")
    L.append("energy -v energy.txt")
    L.append("exit")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


# ============================================================================
# Running
# ============================================================================

def run(cmd, cwd, log, env, timeout):
    """Run a tool, tee its output to `log`, return (ok, seconds)."""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode(errors="replace")
        ok = r.returncode == 0
    except subprocess.TimeoutExpired as e:
        out = (e.output or b"").decode(errors="replace") + f"\n*** TIMEOUT after {timeout}s\n"
        ok = False
    with open(log, "w") as f:
        f.write(" ".join(cmd) + "\n\n" + out)
    return ok, time.time() - t0


def tag_of(params):
    return "_".join(f"{k}{v}" for k, v in sorted(params.items()))


def run_point(module, spec_fn, params, n, tech, timeout, do_area, env_base):
    tag = tag_of(params)
    wd = os.path.join(WORK, module, tag)
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(wd, exist_ok=True)

    spec = spec_fn(params, n)
    emit_wrapper(spec, os.path.join(wd, "dut.act"))
    emit_script(spec, os.path.join(wd, "run.scr"))

    env = dict(env_base)
    # ACT_PATH lets the generated wrapper say `import "multiplier.act"` instead
    # of carrying a brittle relative path.
    env["ACT_PATH"] = SRC + ":" + env.get("ACT_PATH", "")

    row = dict(module=module, **params)
    row["inputs_per_source"] = n
    row["tech"] = tech

    # ---- 1. decompose the DUT ------------------------------------------------
    ok, t = run(["synth2", f"-T{tech}", "-o", "dut_decomp.act", "-F", "decomp",
                 "-t", "-p", "dut", "dut.act"],
                wd, os.path.join(wd, "synth.log"), env, timeout)
    row["synth_s"] = round(t, 1)
    if not ok or not os.path.exists(os.path.join(wd, "dut_decomp.act")):
        row["status"] = "SYNTH_FAIL"
        return row

    # ---- 2. simulate the decomposed DUT -------------------------------------
    emit_top_decomp(spec, os.path.join(wd, "top_decomp.act"))
    with open(os.path.join(wd, "run.scr")) as f:
        scr = f.read()
    t0 = time.time()
    try:
        r = subprocess.run(
            ["actsim", "-ref=1", "-Wlang_subst:off", "-cnf=decomp_sim.conf",
             "top_decomp.act", "tb_d"],
            cwd=wd, env=env, input=scr.encode(), timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode(errors="replace")
        sim_ok = r.returncode == 0
    except subprocess.TimeoutExpired as e:
        out = (e.output or b"").decode(errors="replace") + "\n*** SIM TIMEOUT\n"
        sim_ok = False
    with open(os.path.join(wd, "sim.log"), "w") as f:
        f.write(out)
    row["sim_s"] = round(time.time() - t0, 1)

    # ---- 3. optional area pass via the dataflow flow ------------------------
    area_stat = None
    if do_area:
        awd = os.path.join(wd, "area")
        os.makedirs(awd, exist_ok=True)
        shutil.copy(os.path.join(wd, "dut.act"), awd)
        a_ok, _ = run(["synth2", f"-T{tech}", "-o", "df_dut.act", "-F", "dataflow",
                       "-p", "dut", "dut.act"],
                      awd, os.path.join(awd, "synth_df.log"), env, timeout)
        if a_ok and os.path.exists(os.path.join(awd, "df_dut.act")):
            run(["dflowmap", f"-T{tech}", "-s", "abc", "-ref=1", "-f", "qdi",
                 "-c", "bd", "-M", "8", "-p", "df_dut", "df_dut.act"],
                awd, os.path.join(awd, "dflowmap.log"), env, timeout)
            cand = os.path.join(awd, "df_dut_out", "df_dut.stat")
            if os.path.exists(cand):
                area_stat = cand

    # ---- 4. parse ------------------------------------------------------------
    m = extract_metrics.collect(wd, area_stat, ops=spec.get("ops"))
    m.pop("chcounts", None)
    if spec.get("composite") and "area_um2" in m:
        # See the note on `composite` above: the number is an undercount, so
        # drop it rather than plot something wrong.
        m["area_note"] = f"dropped {m.pop('area_um2')}: arrayed sub-instances undercounted"
    row.update(m)
    row["status"] = "OK" if (sim_ok and m.get("tokens")) else "SIM_FAIL"
    return row


# Column order: identity first, then the three things we actually plot.
COLS = ["module", "tech", "status", "width", "depth", "terms", "ports",
        "inDim", "outDim", "variant", "mode",
        "inputs_per_source", "ops", "tokens",
        "total_delay_ps", "delay_per_op_ps",
        "dynamic_energy_fJ", "energy_per_op_fJ",
        "leakage_power_W", "leakage_energy_fJ", "total_energy_fJ",
        "area_um2", "area_note", "actsim_area", "dflow_leak_power",
        "synth_s", "sim_s"]


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--module", action="append",
                    help="module to sweep (repeatable); default: all")
    ap.add_argument("-n", "--inputs", type=int, default=30,
                    help="tokens generated per source (default 30)")
    ap.add_argument("--tech", default=TECH_DEFAULT)
    ap.add_argument("--timeout", type=int, default=1800,
                    help="per-tool timeout in seconds")
    ap.add_argument("--quick", action="store_true",
                    help="small parameter subset, to smoke-test the flow")
    ap.add_argument("--no-area", action="store_true",
                    help="skip the dataflow+dflowmap area pass")
    args = ap.parse_args()

    names = args.module or list(MODULES)
    bad = [m for m in names if m not in MODULES]
    if bad:
        sys.exit(f"unknown module(s): {bad}\nknown: {list(MODULES)}")
    if args.quick:
        names = [m for m in names if m in QUICK]

    env_base = dict(os.environ)
    os.makedirs(WORK, exist_ok=True)

    for module in names:
        pts = QUICK[module] if args.quick else MODULES[module]["points"]
        build = MODULES[module]["build"]
        rows = []
        print(f"\n=== {module}: {len(pts)} point(s) ===", flush=True)
        for params in pts:
            print(f"  {tag_of(params)} ... ", end="", flush=True)
            row = run_point(module, build, params, args.inputs, args.tech,
                            args.timeout, not args.no_area, env_base)
            rows.append(row)
            d = row.get("delay_per_op_ps")
            e = row.get("energy_per_op_fJ")
            a = row.get("area_um2")
            print(f"{row['status']}  "
                  f"delay/op={d if d is None else round(d,1)} ps  "
                  f"E/op={e if e is None else round(e,2)} fJ  "
                  f"area={a} um2", flush=True)
        print(f"  -> {write_csv(module, rows)}")


if __name__ == "__main__":
    main()
