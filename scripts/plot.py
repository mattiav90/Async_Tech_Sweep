#!/usr/bin/env python3
"""
Plot the sweep results.

Reads every sim/csv/<module>.csv written by sweep.py and renders:

  sim/plots/<module>.png        one panel per metric that actually has data
  sim/plots/_compare_*.png      cross-module comparisons (the interesting ones)

A metric panel is DROPPED, with a note on the figure, when the column is empty
or all-zero. Under -Tgeneric the energy columns are all zero (see README), so
you normally get delay and area only. That is deliberate: a flat line at zero
is worse than an honest "no data".

Colours are the first three categorical slots of the data-viz reference palette,
which are the documented all-pairs-safe subset -- correct here because these are
small multiples, where every series pair can end up side by side. Series are
therefore capped at 3; anything wider is faceted instead of cycling hues.
"""

import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(REPO, "sim")
CSV = os.path.join(SIM, "csv")
PLOTS = os.path.join(SIM, "plots")

# Categorical slots 1-3 (blue, orange, aqua) + ink/surface roles.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e5e4df"
SURFACE = "#fcfcfb"

# (column, axis label, unit)
METRICS = [
    ("delay_per_op_ps", "Delay per operation", "ps"),
    ("energy_per_op_fJ", "Energy per operation", "fJ"),
    ("area_um2", "Area", "um$^2$"),
]

# x = the knob on the x-axis; series = up to 3 lines; facet = one figure each.
PLOT_SPEC = {
    "multiplier":    dict(x="width"),
    "adder":         dict(x="width"),
    "mac":           dict(x="width"),
    "buffer":        dict(x="depth"),
    "copy":          dict(x="outDim"),
    "split":         dict(x="outDim"),
    "det_merge":     dict(x="inDim"),
    "non_det_merge": dict(x="inDim"),
    "xbar_det":      dict(x="ports", series="width"),
    "xbar_nd":       dict(x="ports", series="width"),
}

XLABEL = {
    "width": "data width (bits)",
    "depth": "chain depth (stages)",
    "ports": "ports (N x N)",
    "inDim": "input ports",
    "outDim": "output ports",
}


def style(ax):
    """Recessive axes and grid; the data is the only loud thing."""
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)


def int_ticks(ax, values):
    """Port counts and bit widths are integers -- never label them 2.25."""
    vals = sorted({int(v) for v in values if pd.notna(v)})
    ax.set_xticks(vals)
    ax.set_xticklabels([str(v) for v in vals])


# A panel whose largest value reaches 1000 in one of these units is drawn in
# the next unit up, so energies read as 2.5 pJ rather than 2,505 fJ.
UNIT_UP = {"fJ": "pJ"}


def panel_unit(frames, col, unit):
    """(divisor, unit) for one panel, given every frame drawn on it."""
    vals = pd.concat([pd.to_numeric(d[col], errors="coerce") for d in frames])
    if unit in UNIT_UP and vals.abs().max() >= 1000:
        return 1000.0, UNIT_UP[unit]
    return 1.0, unit


def fmt(v):
    """Value label: 3-4 significant digits, thousands separated."""
    a = abs(v)
    if a == 0:
        return "0"
    if a >= 100:
        return f"{v:,.0f}"
    if a >= 10:
        return f"{v:.1f}"
    if a >= 1:
        return f"{v:.2f}"
    return f"{v:.3g}"


def label_points(ax, curves):
    """Write each point's value beside its marker, in ink (never series) color.

    `curves` is [(xs, ys), ...], one per series drawn on `ax`. At each x the
    highest curve is labelled above its marker and the lowest below, so close
    or crossing curves label on opposite sides; a middle one (3 series) goes to
    the right.
    """
    at = {}
    for xs, ys in curves:
        for x, y in zip(xs, ys):
            if pd.notna(x) and pd.notna(y):
                at.setdefault(x, []).append(y)
    for x, ys in at.items():
        ys = sorted(ys, reverse=True)
        for rank, y in enumerate(ys):
            if rank == 0:
                off, ha, va = (0, 7), "center", "bottom"
            elif rank == len(ys) - 1:
                off, ha, va = (0, -8), "center", "top"
            else:
                off, ha, va = (8, 0), "left", "center"
            ax.annotate(fmt(y), (x, y), xytext=off, textcoords="offset points",
                        ha=ha, va=va, fontsize=7, color=INK2, zorder=4)
    # Headroom so the outermost labels stay inside the axes.
    ax.margins(y=0.14)


def has_data(df, col):
    if col not in df.columns:
        return False
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    return len(s) > 0 and (s != 0).any()


def draw_panel(ax, df, xcol, metric, label, unit, series_col=None):
    k, unit = panel_unit([df], metric, unit)
    curves = []
    if series_col and df[series_col].nunique() > 1:
        vals = sorted(df[series_col].dropna().unique())
        for i, v in enumerate(vals[:3]):
            sub = df[df[series_col] == v].sort_values(xcol)
            ys = pd.to_numeric(sub[metric], errors="coerce") / k
            ax.plot(sub[xcol], ys,
                    marker="o", markersize=5, linewidth=2,
                    color=SERIES[i], label=f"{series_col}={int(v)}", zorder=3)
            curves.append((sub[xcol], ys))
    else:
        sub = df.sort_values(xcol)
        ys = pd.to_numeric(sub[metric], errors="coerce") / k
        ax.plot(sub[xcol], ys,
                marker="o", markersize=5, linewidth=2,
                color=SERIES[0], zorder=3)
        curves.append((sub[xcol], ys))
    label_points(ax, curves)
    int_ticks(ax, df[xcol])
    ax.set_xlabel(XLABEL.get(xcol, xcol), color=INK2, fontsize=9)
    ax.set_title(f"{label}  [{unit}]", color=INK, fontsize=10, loc="left", pad=8)
    style(ax)


def plot_module(module, df, spec):
    xcol = spec["x"]
    series_col = spec.get("series")
    facet = spec.get("facet")
    df = df[df["status"] == "OK"].copy()
    if df.empty:
        print(f"  {module}: no OK rows, skipped")
        return

    facets = [None] if not facet or df[facet].nunique() <= 1 \
        else sorted(df[facet].dropna().unique())

    for fv in facets:
        d = df if fv is None else df[df[facet] == fv]
        live = [(c, l, u) for c, l, u in METRICS if has_data(d, c)]
        missing = [l for c, l, u in METRICS if not has_data(d, c)]
        if not live:
            print(f"  {module}: no metric has data, skipped")
            continue

        fig, axes = plt.subplots(1, len(live), figsize=(4.6 * len(live), 3.9))
        axes = [axes] if len(live) == 1 else list(axes)
        fig.patch.set_facecolor(SURFACE)
        for ax, (c, l, u) in zip(axes, live):
            draw_panel(ax, d, xcol, c, l, u, series_col)

        name = module if fv is None else f"{module} ({facet}={int(fv)})"
        title = f"{name} - scaling in {d['tech'].iloc[0]} technology"
        fig.suptitle(title, color=INK, fontsize=12, x=0.01, ha="left", y=0.99)
        if missing:
            fig.text(0.01, 0.015, "no data: " + ", ".join(missing),
                     color=INK2, fontsize=8)
        # A legend is mandatory whenever there are >= 2 series.
        h, lab = axes[0].get_legend_handles_labels()
        if len(lab) >= 2:
            fig.legend(h, lab, loc="upper right", frameon=False,
                       fontsize=9, labelcolor=INK2, ncol=len(lab))
        fig.tight_layout(rect=[0, 0.04, 1, 0.93])
        out = os.path.join(PLOTS, f"{module}.png" if fv is None
                           else f"{module}_{facet}{int(fv)}.png")
        fig.savefig(out, dpi=150, facecolor=SURFACE)
        plt.close(fig)
        print(f"  {out}")


def plot_compare(name, title, frames, xcol, xlabel):
    """One panel per metric; one line per named data set."""
    frames = [(lbl, d) for lbl, d in frames if d is not None and not d.empty]
    if len(frames) < 2:
        return
    live = [(c, l, u) for c, l, u in METRICS
            if any(has_data(d, c) for _, d in frames)]
    if not live:
        return
    fig, axes = plt.subplots(1, len(live), figsize=(4.6 * len(live), 3.9))
    axes = [axes] if len(live) == 1 else list(axes)
    fig.patch.set_facecolor(SURFACE)
    for ax, (c, l, u) in zip(axes, live):
        k, u = panel_unit([d for _, d in frames[:3]], c, u)
        curves = []
        for i, (lbl, d) in enumerate(frames[:3]):
            d = d.sort_values(xcol)
            ys = pd.to_numeric(d[c], errors="coerce") / k
            ax.plot(d[xcol], ys,
                    marker="o", markersize=5, linewidth=2,
                    color=SERIES[i], label=lbl, zorder=3)
            curves.append((d[xcol], ys))
        label_points(ax, curves)
        int_ticks(ax, pd.concat([d[xcol] for _, d in frames]))
        ax.set_xlabel(xlabel, color=INK2, fontsize=9)
        ax.set_title(f"{l}  [{u}]", color=INK, fontsize=10, loc="left", pad=8)
        style(ax)
    fig.suptitle(title, color=INK, fontsize=12, x=0.01, ha="left", y=0.99)
    h, lab = axes[0].get_legend_handles_labels()
    fig.legend(h, lab, loc="upper right", frameon=False, fontsize=9,
               labelcolor=INK2, ncol=len(lab))
    fig.tight_layout(rect=[0, 0.02, 1, 0.93])
    out = os.path.join(PLOTS, f"_compare_{name}.png")
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"  {out}")


def plot_cond_select(df):
    """Selection overhead. The variants are named categories, not a numeric
    sweep, so this is a bar chart -- one group per width, one bar per case."""
    live = [(c, l, u) for c, l, u in METRICS if has_data(df, c)]
    if not live:
        return
    df = df.copy()
    df["case"] = df["variant"] + "\n(" + df["mode"] + ")"
    # Fixed order so the baselines bracket the thing being measured.
    order = ["plain_adder\n(always)", "cond_adder\n(always)",
             "cond_adder\n(random)", "cond_adder\n(never)"]
    cases = [c for c in order if c in set(df["case"])]
    widths = sorted(df["width"].dropna().unique())

    fig, axes = plt.subplots(1, len(live), figsize=(5.2 * len(live), 4.2))
    axes = [axes] if len(live) == 1 else list(axes)
    fig.patch.set_facecolor(SURFACE)
    for ax, (c, l, u) in zip(axes, live):
        k, u = panel_unit([df], c, u)
        nb = len(widths)
        bw = 0.8 / nb
        for i, w in enumerate(widths[:3]):
            d = df[df["width"] == w].set_index("case")
            vals = [pd.to_numeric(d[c], errors="coerce").get(case, float("nan")) / k
                    for case in cases]
            xs = [j + (i - (nb - 1) / 2) * bw for j in range(len(cases))]
            # 2px surface gap between adjacent bars (bw*0.92).
            bars = ax.bar(xs, vals, width=bw * 0.92, color=SERIES[i],
                          label=f"width={int(w)}", zorder=3)
            ax.bar_label(bars, labels=["" if pd.isna(v) else fmt(v) for v in vals],
                         padding=2, fontsize=7, color=INK2)
        ax.margins(y=0.1)
        ax.set_xticks(range(len(cases)))
        ax.set_xticklabels(cases, fontsize=8, color=INK2)
        ax.set_title(f"{l}  [{u}]", color=INK, fontsize=10, loc="left", pad=8)
        style(ax)
    fig.suptitle("Cost of a CHP selection: [ c=1 -> OUT!a+b [] else -> skip ]",
                 color=INK, fontsize=12, x=0.01, ha="left", y=0.99)
    fig.text(0.01, 0.015,
             "plain_adder (OUT!a+b, no selection) also consumes CTRL, so vs "
             "cond_adder(always) the only difference is the [ ] itself. "
             "always / random / never = a+b path taken 100% / 50% / 0%.",
             color=INK2, fontsize=8)
    h, lab = axes[0].get_legend_handles_labels()
    if len(lab) >= 2:
        fig.legend(h, lab, loc="upper right", frameon=False, fontsize=9,
                   labelcolor=INK2, ncol=len(lab))
    fig.tight_layout(rect=[0, 0.05, 1, 0.93])
    out = os.path.join(PLOTS, "cond_select.png")
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"  {out}")


def load(module):
    p = os.path.join(CSV, f"{module}.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p)
    return df[df["status"] == "OK"].copy()


def main():
    os.makedirs(PLOTS, exist_ok=True)
    any_csv = False
    for module, spec in PLOT_SPEC.items():
        df = load(module)
        if df is None:
            continue
        any_csv = True
        plot_module(module, df, spec)

    cs = load("cond_select")
    if cs is not None and not cs.empty:
        any_csv = True
        plot_cond_select(cs)

    if not any_csv:
        sys.exit("no sim/csv/*.csv found -- run `make sweep` first")

    # --- the two comparisons worth making -----------------------------------
    dm, ndm = load("det_merge"), load("non_det_merge")
    plot_compare(
        "merge_arbitration",
        "What arbitration costs: controlled merge vs probed merge",
        [("det_merge (SEL-driven)", dm), ("non_det_merge (probes)", ndm)],
        "inDim", XLABEL["inDim"])

    # Both crossbars are square (ports = N x N), so they line up port-for-port.
    xd, xn = load("xbar_det"), load("xbar_nd")
    if xd is not None and xn is not None and not xn.empty:
        w = sorted(set(xd["width"]) & set(xn["width"]))
        if w:
            plot_compare(
                "crossbar_det_vs_probe",
                f"Crossbar scaling, deterministic vs probed (width={int(w[0])})",
                [("xbar_det", xd[xd["width"] == w[0]]),
                 ("xbar_nd", xn[xn["width"] == w[0]])],
                "ports", XLABEL["ports"])

if __name__ == "__main__":
    main()
