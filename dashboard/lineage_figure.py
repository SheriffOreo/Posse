#!/usr/bin/env python3
"""
Standalone task-lineage figure  (DEV TOOL — needs matplotlib; NOT used by the
pure-stdlib server at runtime).

Encoding rationale
------------------
The first attempt drew a single global "forest" of all 169 task nodes. But 130 of
them are singletons with no reconstructed parent, so ~77% of the canvas was
disconnected dots and the 13 real trees drowned in the noise — a hairball.

This version instead draws ONLY the 13 real trees, each as an indented outline
(a file-browser tree): parent→child is a plain elbow connector, so there are
*zero* edge crossings by construction. Cards are packed into balanced columns
(largest tree first). Node colour encodes how the parent link was reconstructed
(the confidence legend). The 130 singletons are summarised in one caption line,
not plotted.

Usage:  INFRA_STATE_ROOT=/home/steven/Projects/time-series-omp python lineage_figure.py [out.png]
"""
import os
import sys
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Patch, Rectangle

import lineage  # noqa: E402  (local module)

# ---- palette (light background, print/email friendly) ----------------------
BG = "#ffffff"
CARD = "#f4f6fa"
CARD_EDGE = "#d0d7e2"
INK = "#1b2130"
MUTED = "#6b7688"
CONNECT = "#aeb7c7"
BASIS_COLOR = {
    "explicit-chain": "#1a7f37",   # highest confidence  (explicit arrow in spec)
    "parent-field":   "#1a7f37",   # highest confidence  (parent_task: field)
    "reply-subject":  "#c98a00",   # medium   (Task 323: originating 'Re: Task N' subject)
    "followup-phrase": "#c98a00",  # medium              (follow-up phrasing)
    "subject-thread": "#8a94a6",   # lowest              (subject heuristic)
    "root":           "#31415e",   # a tree's root task
}

# card layout constants (data units)
COL_W = 10.0          # horizontal span of one column
INDENT = 0.50         # x shift per tree depth
INDENT_CAP = 3        # ...but stop indenting past this depth (deep chains stay flat)
ROW_H = 1.0           # vertical span of one node row
HEAD_H = 1.35         # header height inside a card
PAD = 0.55            # inner padding
GAP = 0.9             # vertical gap between cards in a column
TREE_X0 = 0.62        # x where the depth-0 node marker sits
LABEL_DX = 0.22       # gap from node marker (confidence bar) to label text
MAXCHARS = 60         # title truncation (wider cards ⇒ more room)


def _preorder(node, depth=0, out=None):
    out = [] if out is None else out
    out.append((node, depth))
    for c in node["children"]:
        _preorder(c, depth + 1, out)
    return out


def _card_height(tree):
    return HEAD_H + tree["size"] * ROW_H + 2 * PAD


def _pack_columns(trees, ncols):
    """Masonry: drop each (already size-sorted) tree into the shortest column."""
    cols = [[] for _ in range(ncols)]
    heights = [0.0] * ncols
    for t in trees:
        c = heights.index(min(heights))
        cols[c].append(t)
        heights[c] += _card_height(t) + GAP
    return cols, max(heights) if heights else 0.0


def render(out_path):
    forest = lineage.build_forest()
    trees = forest["trees"]
    ncols = 3 if len(trees) >= 6 else 2
    cols, col_h = _pack_columns(trees, ncols)

    fig_w = 5.9 * ncols
    fig_h = max(7.0, col_h * 0.30 + 2.4)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=170)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, ncols * COL_W)
    ax.set_ylim(0, col_h + 2.0)
    ax.invert_yaxis()
    ax.axis("off")

    for ci, col in enumerate(cols):
        x0 = ci * COL_W
        y = 0.4
        for tree in col:
            h = _card_height(tree)
            # card background
            ax.add_patch(FancyBboxPatch(
                (x0 + 0.15, y), COL_W - 0.55, h,
                boxstyle="round,pad=0.02,rounding_size=0.25",
                linewidth=1.0, edgecolor=CARD_EDGE, facecolor=CARD, zorder=1))
            # header
            ax.text(x0 + 0.5, y + 0.62, f"root #{tree['task_id']}",
                    fontsize=11, fontweight="bold", color=INK, zorder=3, va="center")
            ax.text(x0 + COL_W - 0.85, y + 0.62,
                    f"{tree['size']} tasks · depth {tree['depth']}",
                    fontsize=9, color=MUTED, ha="right", va="center", zorder=3)
            ax.plot([x0 + 0.5, x0 + COL_W - 0.85], [y + HEAD_H - 0.12]*2,
                    color=CARD_EDGE, linewidth=0.8, zorder=2)

            rows = _preorder(tree)
            node_xy = {}
            base_y = y + HEAD_H + PAD
            for i, (node, depth) in enumerate(rows):
                cy = base_y + i * ROW_H + ROW_H * 0.5
                # indent is CAPPED so a deep chain (#225) doesn't march off-card;
                # past the cap the elbow becomes a straight vertical run == "chain".
                eff = min(depth, INDENT_CAP)
                nx = x0 + TREE_X0 + eff * INDENT
                node_xy[node["task_id"]] = (nx, cy)
                # faint neutral elbow connector = structure only
                if depth > 0 and node["parent"] in node_xy:
                    px, py = node_xy[node["parent"]]
                    ax.plot([px, px], [py, cy], color=CONNECT, linewidth=1.0, zorder=2)
                    ax.plot([px, nx], [cy, cy], color=CONNECT, linewidth=1.0, zorder=2)
                # per-entry confidence bar (the colour = how the link was inferred)
                basis = node["basis"] or "root"
                color = BASIS_COLOR.get(basis, MUTED)
                ax.add_patch(Rectangle((nx - 0.05, cy - 0.34), 0.12, 0.68,
                                       facecolor=color, edgecolor="none", zorder=4))
                title = (node["title"] or "").strip()
                title = textwrap.shorten(title, width=MAXCHARS, placeholder="…")
                ax.text(nx + LABEL_DX, cy, f"#{node['task_id']}  {title}",
                        fontsize=9.0, color=INK, va="center", zorder=4)
            y += h + GAP

    # ---- title + caption -------------------------------------------------
    n_tasks = forest["n_tasks"]
    n_tree = forest["n_in_trees"]
    n_sing = forest["n_singletons"]
    fig.suptitle("Task lineage — reconstructed follow-up trees",
                 fontsize=15, fontweight="bold", color=INK, y=0.985)
    cap = (f"{forest['n_trees']} trees link {n_tree} of {n_tasks} tasks.  "
           f"The other {n_sing} tasks are standalone (no reconstructed parent) "
           f"and are omitted.  Links are best-effort (In-Reply-To is not stored).")
    fig.text(0.5, 0.010, cap, ha="center", fontsize=8.6, color=MUTED)

    # ---- confidence legend (swatch bars, matching the per-entry bars) -----
    legend_items = [
        (BASIS_COLOR["explicit-chain"], "explicit link — high confidence"),
        (BASIS_COLOR["followup-phrase"], "follow-up phrasing — medium"),
        (BASIS_COLOR["subject-thread"], "same subject — low (heuristic)"),
        (BASIS_COLOR["root"], "root task (no parent)"),
    ]
    def _swatches():
        return [Patch(facecolor=c, edgecolor="none", label=lbl)
                for c, lbl in legend_items]
    leg = fig.legend(handles=_swatches(), loc="upper center", ncol=len(legend_items),
                     bbox_to_anchor=(0.5, 0.955), frameon=False, fontsize=9,
                     handletextpad=0.5, columnspacing=1.8)
    for t in leg.get_texts():
        t.set_color(INK)
    # repeat the swatches in the footer so a reader at the bottom of a tall column
    # doesn't have to trek back up to decode a colour (reviewer note).
    from matplotlib.legend import Legend
    leg2 = Legend(fig, _swatches(), [lbl for _, lbl in legend_items],
                  loc="lower center", ncol=len(legend_items), frameon=False,
                  bbox_to_anchor=(0.5, 0.038), fontsize=7.6, handletextpad=0.5,
                  columnspacing=1.6)
    for t in leg2.get_texts():
        t.set_color(MUTED)
    fig.add_artist(leg2)

    fig.subplots_adjust(left=0.02, right=0.98, top=0.90, bottom=0.07)
    fig.savefig(out_path, facecolor=BG, bbox_inches="tight")
    print(f"wrote {out_path}  ({forest['n_trees']} trees, "
          f"{n_tree} linked, {n_sing} singletons)")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "lineage_forest.png"
    render(out)
