#!/usr/bin/env python3
"""Analytical evaluation of Posse's cost-aware wait scheduling (Case 400).

Two panels, both derived purely from the cost model (schematic, not measured):
  (a) per-wait cost vs. wait duration for three policies; Posse is the lower envelope.
  (b) aggregate spend of a whole fleet over a representative job-duration mix, as a
      relative-cost bar (cost-unaware coarse polling vs. sub-TTL polling vs. Posse).
Emits fig_eval.pdf/.png. Provider constants are stated; the *shape* is what matters."""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
NAVY, GOLD_D, SLATE, P4 = "#20293b", "#b3841f", "#5b6b86", "#b5533e"

M = 12.5            # cold-rewrite / warm-read multiplier
TTL_MIN = 5.0       # prompt-cache TTL (minutes)
COARSE_MIN = 10.0   # cost-unaware cadence (> TTL -> every poll cold)
SUBTTL_MIN = 4.0    # disciplined cadence (< TTL -> every poll warm)
BREAKEVEN = 50.0    # m / (60/SUBTTL_MIN) = 12.5 / 15 h -> ~50 min


def cost_coarse(t):   # cold rewrite every COARSE_MIN
    return M * (t / COARSE_MIN)


def cost_subttl(t):   # warm read every SUBTTL_MIN
    return t / SUBTTL_MIN


def cost_posse(t):    # sub-TTL poll if short, else ONE event-wake (a single rewrite)
    return cost_subttl(t) if t <= BREAKEVEN else M


def main():
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.edgecolor": "#333", "axes.linewidth": 0.9})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.4, 3.9))

    # -- (a) per-wait cost vs duration --
    ts = [i for i in range(0, 181, 2)]
    ax1.plot(ts, [cost_coarse(t) for t in ts], color=P4, lw=2.4,
             label="cost-unaware (poll every 10 min)")
    ax1.plot(ts, [cost_subttl(t) for t in ts], color=SLATE, lw=2.2, ls="--",
             label="sub-TTL polling (every 4 min)")
    ax1.plot(ts, [cost_posse(t) for t in ts], color=NAVY, lw=2.8,
             label="Posse (poll ≤ 50 min, else event-wake)")
    ax1.axvline(BREAKEVEN, color=GOLD_D, ls=":", lw=1.4)
    ax1.text(BREAKEVEN + 3, 4, "50-min\nbreak-even", fontsize=9, color=GOLD_D, fontweight="bold")
    ax1.set_xlabel("time a deputy waits on a job  (min)")
    ax1.set_ylabel("cost per wait  (× a warm read)")
    ax1.set_xlim(0, 180)
    ax1.set_ylim(0, 60)
    ax1.set_title("(a)  per-wait cost: Posse is the lower envelope", fontsize=11.3, fontweight="bold")
    ax1.legend(loc="upper left", fontsize=8.8, frameon=True)

    # -- (b) aggregate fleet spend over a representative job-duration mix --
    # buckets: (count, representative duration min) -- many short waits + a heavy tail
    mix = [(120, 8), (80, 20), (50, 45), (30, 90), (18, 180), (10, 300)]
    tot_coarse = sum(n * cost_coarse(t) for n, t in mix)
    tot_subttl = sum(n * cost_subttl(t) for n, t in mix)
    tot_posse = sum(n * cost_posse(t) for n, t in mix)
    base = tot_posse
    vals = [tot_coarse / base, tot_subttl / base, tot_posse / base]
    labels = ["cost-unaware", "sub-TTL poll", "Posse"]
    colors = [P4, SLATE, NAVY]
    bars = ax2.bar(labels, vals, color=colors, width=0.62)
    for b, v in zip(bars, vals):
        ax2.text(b.get_x() + b.get_width() / 2, v + max(vals) * 0.015,
                 f"{v:.1f}×", ha="center", va="bottom", fontsize=11, fontweight="bold",
                 color=b.get_facecolor())
    ax2.set_ylabel("fleet spend  (× Posse)")
    ax2.set_ylim(0, max(vals) * 1.18)
    ax2.set_title("(b)  aggregate spend over a 308-wait fleet mix", fontsize=11.3, fontweight="bold")
    ax2.text(0.5, -0.30,
             "mix: 120@8m  80@20m  50@45m  30@90m  18@180m  10@300m",
             transform=ax2.transAxes, ha="center", fontsize=8.3, color=SLATE)

    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig_eval.pdf"))
    fig.savefig(os.path.join(HERE, "fig_eval.png"), dpi=150)
    plt.close(fig)
    print(f"coarse/posse = {tot_coarse/base:.2f}x ; subttl/posse = {tot_subttl/base:.2f}x")


if __name__ == "__main__":
    main()
