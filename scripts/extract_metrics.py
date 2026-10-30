#!/usr/bin/env python3
"""
Parse one simulation run directory into a metrics dict.

Everything this reads is produced by the flow in sweep.py:

  energy.txt     written by actsim's `energy -v energy.txt`. One line:
                     Total: <dynamic_fJ>  (<leakage_W> W); area: <area>
                 With a real technology (tsmc65 + genus) these are real
                 numbers. With -Tgeneric they come back as 0 -- see
                 README.md -- because the generic config ships no per-cell
                 energy tables and the only available logic-synthesis
                 backend (built-in abc) does not report power.

  sim.log        actsim stdout. Watched channels print "[ <ps> ] <proc> ..."
                 lines, so the largest timestamp is the time the simulation
                 finished. `chcount` lines give the exact number of completed
                 actions per output channel, which is what we divide by to get
                 a per-token figure.

  df_dut.stat    written by dflowmap in the optional area pass. "totalArea:"
                 IS real under -Tgeneric (abc maps onto osu018 and reports
                 cell area), so it is the one metric that scales today.

Anything missing comes back as None rather than 0, so a failed sub-step is
visible in the CSV instead of silently looking like a free circuit.
"""

import argparse
import json
import os
import re

# "Total: 1234  (5.6e-07 W); area: 789"
RE_ENERGY = re.compile(
    r"Total:\s+([\d.eE+-]+)\s+\(([\d.eE+-]+)\s+W\);\s+area:\s+([\d.eE+-]+)"
)
# actsim watch line: "[             3224] <d.m>  d.m.OUT : send value: ..."
RE_TIME = re.compile(r"^\[\s*(\d+)\s*\]")
# "Channel d.OUT: completed actions 100"
RE_CHCOUNT = re.compile(r"Channel\s+(\S+):\s+completed actions\s+(\d+)")
RE_AREA = re.compile(r"totalArea:\s*([\d.]+)")
RE_LEAK = re.compile(r"totalLeakPow\w*:\s*([\d.]+)")


def parse_energy(path):
    """Dynamic energy [fJ], leakage power [W] and area as actsim reports them."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        for line in f:
            m = RE_ENERGY.search(line)
            if m:
                return {
                    "dynamic_energy_fJ": float(m.group(1)),
                    "leakage_power_W": float(m.group(2)),
                    "actsim_area": float(m.group(3)),
                }
    return {}


def parse_sim_log(path):
    """Final simulation time [ps] and completed-action counts per channel."""
    if not os.path.exists(path):
        return {}, {}
    t_end = None
    counts = {}
    with open(path, errors="replace") as f:
        for line in f:
            m = RE_TIME.match(line)
            if m:
                t = int(m.group(1))
                if t_end is None or t > t_end:
                    t_end = t
            m = RE_CHCOUNT.search(line)
            if m:
                counts[m.group(1)] = int(m.group(2))
    out = {}
    if t_end is not None:
        out["total_delay_ps"] = t_end
    return out, counts


def parse_area(path):
    """dflowmap's area/leakage summary, if the optional area pass ran.

    An area of exactly 0 means dflowmap emitted a stat file but mapped nothing
    (it happens on probe-based modules), so report it as missing rather than as
    a zero-area circuit.
    """
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as f:
        text = f.read()
    m = RE_AREA.search(text)
    if m and float(m.group(1)) > 0:
        out["area_um2"] = float(m.group(1))
    m = RE_LEAK.search(text)
    if m:
        out["dflow_leak_power"] = float(m.group(1))
    return out


def collect(run_dir, area_stat=None, ops=None):
    """Gather every metric for one run directory into a flat dict.

    `ops` is how many times the module's main loop body ran -- the correct
    denominator for a per-operation figure. It is NOT the same as `tokens`
    (completed output actions): a copy with 4 outputs emits 4 tokens per input,
    so normalising by tokens would make a wider, more expensive fork look
    4x cheaper. tokens is still recorded, as a sanity check that the run did
    the work we expected.
    """
    m = {}
    m.update(parse_energy(os.path.join(run_dir, "energy.txt")))

    timing, counts = parse_sim_log(os.path.join(run_dir, "sim.log"))
    m.update(timing)

    tokens = sum(counts.values()) if counts else 0
    m["tokens"] = tokens
    m["chcounts"] = counts
    if ops:
        m["ops"] = ops

    delay = m.get("total_delay_ps")
    if delay is not None and ops:
        # A THROUGHPUT figure (total time / operations), not a single-token
        # latency: the sources run flat out, so the DUT is kept busy and this is
        # the cycle time you would see in a pipeline.
        m["delay_per_op_ps"] = delay / ops

    dyn = m.get("dynamic_energy_fJ")
    if dyn is not None and ops:
        m["energy_per_op_fJ"] = dyn / ops

    # Leakage energy = leakage power x wall-clock time of the run.
    leak_W = m.get("leakage_power_W")
    if leak_W is not None and delay is not None:
        # E[fJ] = P[W] * t[ps] * 1e3
        leak_fJ = leak_W * delay * 1e3
        m["leakage_energy_fJ"] = leak_fJ
        if dyn is not None:
            m["total_energy_fJ"] = dyn + leak_fJ

    if area_stat:
        m.update(parse_area(area_stat))
    else:
        m.update(parse_area(os.path.join(run_dir, "area", "df_dut_out", "df_dut.stat")))

    return m


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", help="directory holding energy.txt and sim.log")
    p.add_argument("--area-stat", default=None, help="path to df_dut.stat")
    args = p.parse_args()
    print(json.dumps(collect(args.run_dir, args.area_stat), indent=2))
