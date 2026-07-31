#!/usr/bin/env python3
"""Design-doc figures for the Posse agent framework (Case 400).

Emits three figures as vector PDF (+ PNG preview):
  fig_architecture  — the system architecture (SVG)
  fig_lifecycle     — life of a case: continuity + interrupt + close (SVG)
  fig_costmodel     — the token-cost scheduling model (matplotlib)

SVG per the repo convention (agent-authored SVG, not TikZ). Palette lifted from
the Posse / Oreo-sheriff brand mark (reports/task390/make_mark.py)."""
import math, os
HERE = os.path.dirname(os.path.abspath(__file__))

# ---- Posse palette -------------------------------------------------------
NAVY   = "#20293b"   # primary structural
NAVY2  = "#38538a"   # mid navy accent
INK    = "#2b2b2b"
GOLD   = "#e8bb44"
GOLD_D = "#b3841f"   # deep gold line
CREAM  = "#f7f1e3"   # parchment background
CARD   = "#ffffff"
TAN    = "#b8a488"
BROWN  = "#6f5c44"
SLATE  = "#5b6b86"
# four-problem accents (muted, on-brand)
P1 = "#2e8b8b"   # continuity  (teal)
P2 = "#c98a2b"   # many threads (amber)
P3 = "#5566b5"   # autonomy    (indigo)
P4 = "#b5533e"   # token cost  (terracotta)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def box(x, y, w, h, fill=CARD, stroke=NAVY, sw=2, rx=10, dash=None, opacity=1.0):
    d = f" stroke-dasharray='{dash}'" if dash else ""
    return (f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='{rx}' fill='{fill}' "
            f"fill-opacity='{opacity}' stroke='{stroke}' stroke-width='{sw}'{d}/>")


def txt(x, y, s, size=15, fill=INK, weight="normal", anchor="middle", family="Helvetica, Arial, sans-serif", italic=False, ls=None):
    st = " font-style='italic'" if italic else ""
    lsp = f" letter-spacing='{ls}'" if ls else ""
    return (f"<text x='{x}' y='{y}' font-family='{family}' font-size='{size}' "
            f"fill='{fill}' font-weight='{weight}' text-anchor='{anchor}'{st}{lsp}>{esc(s)}</text>")


def line(x1, y1, x2, y2, stroke=NAVY, sw=2, dash=None, cap="round"):
    d = f" stroke-dasharray='{dash}'" if dash else ""
    return f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='{stroke}' stroke-width='{sw}' stroke-linecap='{cap}'{d}/>"


def arrow(x1, y1, x2, y2, stroke=NAVY, sw=2.4, dash=None, mid=False):
    return line(x1, y1, x2, y2, stroke, sw, dash) + head(x1, y1, x2, y2, stroke)


def head(x1, y1, x2, y2, stroke=NAVY, sz=9):
    a = math.atan2(y2 - y1, x2 - x1)
    p1 = (x2 - sz * math.cos(a - 0.42), y2 - sz * math.sin(a - 0.42))
    p2 = (x2 - sz * math.cos(a + 0.42), y2 - sz * math.sin(a + 0.42))
    return f"<path d='M {x2:.1f},{y2:.1f} L {p1[0]:.1f},{p1[1]:.1f} L {p2[0]:.1f},{p2[1]:.1f} Z' fill='{stroke}'/>"


def star(cx, cy, n, ro, ri, fill=GOLD, stroke=GOLD_D, sw=2, rot=-90):
    p = []
    for i in range(n * 2):
        r = ro if i % 2 == 0 else ri
        a = math.radians(rot + i * 180 / n)
        p.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d = "M " + " ".join(f"{x:.1f},{y:.1f}" for x, y in p) + " Z"
    return f"<path d='{d}' fill='{fill}' stroke='{stroke}' stroke-width='{sw}' stroke-linejoin='round'/>"


def badge(cx, cy, r):
    """A compact Oreo-sheriff star badge (brand touch)."""
    s = [star(cx, cy, 6, r, r * 0.66, GOLD, GOLD_D, 2)]
    s.append(f"<circle cx='{cx}' cy='{cy}' r='{r*0.52:.1f}' fill='{NAVY}' stroke='{GOLD_D}' stroke-width='1.5'/>")
    s.append(f"<ellipse cx='{cx}' cy='{cy+r*0.06:.1f}' rx='{r*0.36:.1f}' ry='{r*0.34:.1f}' fill='{TAN}' stroke='{BROWN}' stroke-width='1'/>")
    # tiny hat brim
    s.append(f"<path d='M {cx-r*0.34:.1f},{cy-r*0.10:.1f} Q {cx},{cy-r*0.02:.1f} {cx+r*0.34:.1f},{cy-r*0.10:.1f} Q {cx},{cy-r*0.20:.1f} {cx-r*0.34:.1f},{cy-r*0.10:.1f} Z' fill='#d99f5c' stroke='#a9713a' stroke-width='0.8'/>")
    # eyes
    for sgn in (-1, 1):
        s.append(f"<circle cx='{cx+sgn*r*0.13:.1f}' cy='{cy+r*0.05:.1f}' r='{r*0.055:.1f}' fill='{INK}'/>")
    return "".join(s)


def svg_wrap(w, h, body):
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w} {h}' width='{w}' height='{h}'>"
            f"<rect x='0' y='0' width='{w}' height='{h}' fill='{CREAM}'/>{body}</svg>")


# =========================================================================
# FIGURE 1 — Architecture
# =========================================================================
def fig_architecture():
    W, H = 1240, 830
    b = []
    # ---- title ----
    b.append(badge(52, 46, 30))
    b.append(txt(92, 40, "POSSE", 26, NAVY, "bold", "start"))
    b.append(txt(92, 62, "autonomous multi-agent operations", 13, SLATE, "normal", "start", italic=True))
    b.append(line(30, 82, W - 30, 82, GOLD_D, 2))

    Cx = 640  # center column axis
    # ---- Human + email (top) ----
    hb_y = 100
    b.append(box(Cx - 250, hb_y, 500, 58, "#eef3fb", NAVY2, 2, 12))
    # person glyph
    b.append(f"<circle cx='{Cx-210}' cy='{hb_y+21}' r='9' fill='{NAVY2}'/>")
    b.append(f"<path d='M {Cx-224},{hb_y+44} Q {Cx-210},{hb_y+28} {Cx-196},{hb_y+44} Z' fill='{NAVY2}'/>")
    b.append(txt(Cx - 20, hb_y + 24, "Human director  (Steven / Feng)", 16, NAVY, "bold"))
    # envelope
    b.append(box(Cx + 158, hb_y + 11, 80, 32, GOLD, GOLD_D, 1.5, 5))
    b.append(f"<path d='M {Cx+158},{hb_y+11} L {Cx+198},{hb_y+31} L {Cx+238},{hb_y+11}' fill='none' stroke='{GOLD_D}' stroke-width='1.5'/>")
    b.append(txt(Cx - 46, hb_y + 46, "asynchronous direction over weeks, by email", 12, SLATE, "normal", "middle", italic=True))

    # ---- Inbox router ----
    rt_y = 196
    b.append(box(Cx - 250, rt_y, 500, 78, CARD, NAVY, 2.4, 12))
    b.append(f"<rect x='{Cx-250}' y='{rt_y}' width='500' height='24' rx='12' fill='{NAVY}'/>")
    b.append(f"<rect x='{Cx-250}' y='{rt_y+12}' width='500' height='12' fill='{NAVY}'/>")
    b.append(txt(Cx, rt_y + 17, "INBOX ROUTER  ·  always-on, zero-LLM", 14, "#fff", "bold"))
    b.append(txt(Cx - 235, rt_y + 44, "reply-thread", 12.5, P1, "bold", "start"))
    b.append(txt(Cx - 235, rt_y + 60, "→ that deputy's mailbox", 11.5, INK, "normal", "start"))
    b.append(txt(Cx - 70, rt_y + 44, "[precinct] tag", 12.5, P2, "bold", "start"))
    b.append(txt(Cx - 70, rt_y + 60, "→ spawn a new deputy", 11.5, INK, "normal", "start"))
    b.append(txt(Cx + 96, rt_y + 44, "unaddressed", 12.5, SLATE, "bold", "start"))
    b.append(txt(Cx + 96, rt_y + 60, "→ receptionist", 11.5, INK, "normal", "start"))
    # arrows human<->router
    b.append(arrow(Cx, hb_y + 58, Cx, rt_y, NAVY))
    b.append(arrow(Cx + 40, rt_y, Cx + 40, hb_y + 58, GOLD_D))

    # ---- Precincts (3 x 2) ----
    pc_top = 314
    b.append(txt(Cx, pc_top - 6, "PRECINCTS  —  durable, bounded, auditable memory per domain", 15, NAVY, "bold"))
    precincts = [("eval", "opus"), ("omp", "opus"), ("query", "opus"),
                 ("paper", "fable"), ("infra", "opus"), ("receptionist", "fixed")]
    pw, ph, gx, gy = 150, 92, 14, 16
    grid_w = 3 * pw + 2 * gx
    x0 = Cx - grid_w / 2
    y0 = pc_top + 8
    dep_on = {"infra": "#400", "omp": None}
    for i, (name, model) in enumerate(precincts):
        col, row = i % 3, i // 3
        x = x0 + col * (pw + gx)
        y = y0 + row * (ph + gy)
        b.append(box(x, y, pw, ph, CARD, NAVY, 2, 9))
        b.append(f"<rect x='{x}' y='{y}' width='{pw}' height='22' rx='9' fill='{NAVY}'/>")
        b.append(f"<rect x='{x}' y='{y+11}' width='{pw}' height='11' fill='{NAVY}'/>")
        b.append(txt(x + 10, y + 16, name, 13.5, "#fff", "bold", "start"))
        b.append(txt(x + pw - 8, y + 16, model, 10.5, GOLD, "bold", "end"))
        # records stack
        b.append(txt(x + 10, y + 40, "ledger  (compacted digest)", 10.5, INK, "normal", "start"))
        b.append(txt(x + 10, y + 55, "case log  (append-only)", 10.5, INK, "normal", "start"))
        b.append(txt(x + 10, y + 70, "case files  (per thread)", 10.5, INK, "normal", "start"))
        b.append(f"<rect x='{x+8}' y='{y+30}' width='4' height='46' rx='2' fill='{GOLD}'/>")
    # a deputy chip attached to the infra precinct
    dx = x0 + 1 * (pw + gx) + pw / 2
    dy = y0 + 1 * (ph + gy) + ph
    b.append(box(dx - 74, dy + 12, 148, 30, "#fbeede", P2, 1.8, 8))
    b.append(txt(dx, dy + 31, "deputy · ephemeral · 1 case", 10.8, BROWN, "bold"))
    b.append(arrow(dx, dy + 12, dx, dy, P2, 1.8))
    # arrow router -> precincts
    b.append(arrow(Cx, rt_y + 78, Cx, pc_top + 2, NAVY))
    b.append(txt(Cx + 150, rt_y + 96, "a case = one numbered thread", 11, SLATE, "normal", "middle", italic=True))

    # ---- Sheriff (left overseer column) ----
    sh_x, sh_y, sh_w, sh_h = 30, 314, 176, 300
    b.append(box(sh_x, sh_y, sh_w, sh_h, "#eef2f8", NAVY, 2.4, 12))
    b.append(badge(sh_x + 34, sh_y + 30, 22))
    b.append(txt(sh_x + 60, sh_y + 26, "SHERIFF", 16, NAVY, "bold", "start"))
    b.append(txt(sh_x + 60, sh_y + 43, "system manager", 11, SLATE, "normal", "start", italic=True))
    b.append(box(sh_x + 12, sh_y + 58, sh_w - 24, 74, CARD, P3, 1.6, 8))
    b.append(txt(sh_x + 20, sh_y + 77, "Deputy supervision", 12, P3, "bold", "start"))
    b.append(txt(sh_x + 20, sh_y + 94, "liveness · crash /", 10.5, INK, "normal", "start"))
    b.append(txt(sh_x + 20, sh_y + 108, "usage-limit relaunch", 10.5, INK, "normal", "start"))
    b.append(txt(sh_x + 20, sh_y + 124, "(watchdog loop)", 10, SLATE, "italic", "start", italic=True))
    b.append(box(sh_x + 12, sh_y + 140, sh_w - 24, 92, CARD, P4, 1.6, 8))
    b.append(txt(sh_x + 20, sh_y + 159, "Records health", 12, P4, "bold", "start"))
    b.append(txt(sh_x + 20, sh_y + 176, "ledger compaction", 10.5, INK, "normal", "start"))
    b.append(txt(sh_x + 20, sh_y + 190, "request queue", 10.5, INK, "normal", "start"))
    b.append(txt(sh_x + 20, sh_y + 204, "precinct lifecycle", 10.5, INK, "normal", "start"))
    b.append(txt(sh_x + 20, sh_y + 224, "(paid only here)", 10, SLATE, "italic", "start", italic=True))
    b.append(box(sh_x + 12, sh_y + 240, sh_w - 24, 46, "#fff7e0", GOLD_D, 1.4, 8))
    b.append(txt(sh_x + sh_w / 2, sh_y + 258, "monitoring is", 10.5, BROWN, "bold"))
    b.append(txt(sh_x + sh_w / 2, sh_y + 273, "ZERO-API", 12.5, GOLD_D, "bold"))
    # oversee arrows into center
    b.append(arrow(sh_x + sh_w, sh_y + 120, x0 - 6, y0 + 40, P3, 1.8, "5,4"))
    b.append(arrow(sh_x + sh_w, sh_y + 200, x0 - 6, y0 + ph + gy + 40, P4, 1.8, "5,4"))

    # ---- Dashboard (right observer column) ----
    db_x, db_y, db_w, db_h = W - 206, 314, 176, 300
    b.append(box(db_x, db_y, db_w, db_h, "#eef2f8", NAVY, 2.4, 12))
    b.append(txt(db_x + db_w / 2, db_y + 28, "DASHBOARD", 16, NAVY, "bold"))
    b.append(txt(db_x + db_w / 2, db_y + 46, "read-only observability", 10.5, SLATE, "italic", "middle", italic=True))
    for j, lab in enumerate(["case lineage", "active deputies", "GPU / jobs", "sheriff health", "precinct records"]):
        yy = db_y + 66 + j * 40
        b.append(box(db_x + 12, yy, db_w - 24, 30, CARD, SLATE, 1.4, 7))
        b.append(txt(db_x + db_w / 2, yy + 20, lab, 11.5, NAVY, "normal"))
    b.append(arrow(x0 + grid_w + 6, y0 + 60, db_x, db_y + 120, SLATE, 1.6, "5,4"))

    # ---- Daemon substrate (base) ----
    ds_y = 636
    b.append(box(Cx - 250, ds_y, 500, 96, CARD, NAVY, 2.4, 12))
    b.append(f"<rect x='{Cx-250}' y='{ds_y}' width='500' height='24' rx='12' fill='{P3}'/>")
    b.append(f"<rect x='{Cx-250}' y='{ds_y+12}' width='500' height='12' fill='{P3}'/>")
    b.append(txt(Cx, ds_y + 17, "ALWAYS-ON DAEMON SUBSTRATE  ·  no LLM calls", 13.5, "#fff", "bold"))
    daemons = ["inbox", "watchdog", "jobmgr", "gpu_manager", "sheriff"]
    dw = 88
    dx0 = Cx - (5 * dw + 4 * 8) / 2
    for k, dn in enumerate(daemons):
        x = dx0 + k * (dw + 8)
        b.append(box(x, ds_y + 34, dw, 30, "#eef3fb", P3, 1.5, 7))
        b.append(txt(x + dw / 2, ds_y + 54, dn, 11.5, NAVY, "bold"))
    b.append(txt(Cx, ds_y + 82, "tmux-persistent · survive disconnects · relaunch + event-wake + serialize the one GPU", 11, SLATE, "normal", "middle", italic=True))
    b.append(arrow(Cx, y0 + 2 * ph + gy + 6, Cx, ds_y, NAVY, 1.8, "4,4"))

    # ---- Bottom legend: the four problems ----
    lg_y = 752
    b.append(box(30, lg_y, W - 60, 62, "#fbf6ea", GOLD_D, 1.6, 10))
    b.append(txt(48, lg_y + 22, "The four problems, and where they are solved:", 13, NAVY, "bold", "start"))
    items = [
        (P1, "P1 continuity of a thread", "case file + resume/reinject + mailbox + board"),
        (P2, "P2 many threads at once", "precincts + case numbers + deputies + JTF"),
        (P3, "P3 continuous autonomy", "relaunch + event-wake + routing + GPU"),
        (P4, "P4 token efficiency", "cost-aware waits + zero-API control + compaction"),
    ]
    colw = (W - 60) / 4
    for i, (c, a, d) in enumerate(items):
        x = 48 + i * colw
        b.append(f"<rect x='{x}' y='{lg_y+32}' width='12' height='12' rx='3' fill='{c}'/>")
        b.append(txt(x + 18, lg_y + 42, a, 11, INK, "bold", "start"))
        b.append(txt(x + 18, lg_y + 56, d, 9.5, SLATE, "normal", "start"))

    return svg_wrap(W, H, "".join(b))


# =========================================================================
# FIGURE 2 — Life of a case (continuity + interrupt + close)
# =========================================================================
def fig_lifecycle():
    W, H = 1240, 500
    b = []
    b.append(badge(48, 40, 24))
    b.append(txt(84, 46, "Life of a case", 20, NAVY, "bold", "start"))
    b.append(line(30, 66, W - 30, 66, GOLD_D, 1.6))

    lane_y = 214            # deputy lane centre
    lh = 66
    b.append(box(150, lane_y - lh / 2, W - 320, lh, "#eef3fb", NAVY2, 2, 12))
    b.append(txt(160, lane_y - lh / 2 - 8, "one deputy — a single linear LLM context (the thread)", 12.5, NAVY, "bold", "start", italic=True))

    # inbound: email -> router -> spawn/resume
    b.append(box(30, lane_y - 26, 96, 52, GOLD, GOLD_D, 1.6, 8))
    b.append(txt(78, lane_y - 4, "email", 13, NAVY, "bold"))
    b.append(txt(78, lane_y + 14, "arrives", 11, NAVY))
    b.append(arrow(126, lane_y, 150, lane_y, NAVY, 2.2))
    b.append(txt(138, lane_y - 40, "route +", 9.5, SLATE, "normal", "middle"))
    b.append(txt(138, lane_y - 30, "spawn/resume", 9.5, SLATE, "normal", "middle"))

    # milestones along the lane
    stops = [
        (210, "START", "plan email"),
        (330, "work", "small chunks"),
        (450, "detach", ">2 min compute"),
        (585, "submit +", "sleep >50 min"),
        (712, "milestone", "email"),
    ]
    for x, t1, t2 in stops:
        b.append(f"<circle cx='{x}' cy='{lane_y}' r='7' fill='{NAVY}' stroke='#fff' stroke-width='2'/>")
        b.append(txt(x, lane_y - 16, t1, 11.5, NAVY, "bold"))
        b.append(txt(x, lane_y + 24, t2, 10, SLATE))
    b.append(line(210, lane_y, 712, lane_y, NAVY, 2, None))

    # INTERRUPT block
    ix = 820
    b.append(box(ix - 66, lane_y - 58, 132, 116, "#fdece7", P4, 2.2, 10))
    b.append(txt(ix, lane_y - 40, "INTERRUPT", 12.5, P4, "bold"))
    b.append(txt(ix, lane_y - 24, "new user email", 10, INK))
    b.append(txt(ix, lane_y - 6, "surgical kill +", 10.5, INK, "bold"))
    b.append(txt(ix, lane_y + 8, "relaunch + inject", 10.5, INK, "bold"))
    b.append(txt(ix, lane_y + 26, "jobs survive;", 9.8, SLATE))
    b.append(txt(ix, lane_y + 40, "reply-first", 9.8, SLATE, "italic", "middle", italic=True))
    b.append(arrow(748, lane_y, ix - 66, lane_y, NAVY, 2))
    b.append(arrow(ix + 66, lane_y, 930, lane_y, NAVY, 2))

    # CLOSE block
    cx = 986
    b.append(f"<circle cx='{cx}' cy='{lane_y}' r='9' fill='{P1}' stroke='#fff' stroke-width='2'/>")
    b.append(txt(cx, lane_y - 18, "FINAL", 12, P1, "bold"))
    b.append(box(cx + 26, lane_y - 44, 168, 88, CARD, P1, 1.8, 9))
    b.append(txt(cx + 110, lane_y - 26, "CLOSE the case", 12, P1, "bold"))
    b.append(txt(cx + 34, lane_y - 8, "+1 ledger paragraph", 10.3, INK, "normal", "start"))
    b.append(txt(cx + 34, lane_y + 8, "+1 case-log line", 10.3, INK, "normal", "start"))
    b.append(txt(cx + 34, lane_y + 24, "touch done-sentinel", 10.3, INK, "normal", "start"))
    b.append(arrow(cx + 9, lane_y, cx + 26, lane_y, P1, 2))

    # continuity band (dashed, above lane) spanning the interrupt
    cont_y = lane_y - lh / 2 - 40
    b.append(line(210, cont_y, cx, cont_y, P1, 1.8, "6,5"))
    b.append(f"<circle cx='210' cy='{cont_y}' r='4' fill='{P1}'/>")
    b.append(f"<circle cx='{cx}' cy='{cont_y}' r='4' fill='{P1}'/>")
    b.append(txt((210 + cx) / 2, cont_y - 8, "continuity carried across the interrupt  —  case file + session resume (or injected brief)", 11.5, P1, "bold", "middle"))
    b.append(arrow(ix, lane_y - 58, ix, cont_y + 6, P1, 1.4, "4,3"))

    # human email band (top) — inbound arrows
    for x in (210, 712):
        b.append(arrow(x, 92, x, lane_y - 16 - 12, GOLD_D, 1.4, "3,3"))
    b.append(txt(461, 86, "deputy emails the human: plan · milestones · FINAL   (and drains its mailbox at every step)", 12, SLATE, "normal", "middle", italic=True))

    # Sheriff lane (bottom) — autonomy
    sy = lane_y + lh / 2 + 58
    b.append(box(150, sy - 24, W - 320, 48, "#eef2f8", P3, 1.8, 10))
    b.append(badge(178, sy, 16))
    b.append(txt(200, sy - 2, "Sheriff + daemons (always-on, zero-API):", 12, P3, "bold", "start"))
    b.append(txt(200, sy + 14, "relaunch on crash / usage-limit · wake the deputy when a >50-min job finishes · compact the ledger when it grows", 10.8, INK, "normal", "start"))
    for x in (330, 585, cx):
        b.append(arrow(x, sy - 24, x, lane_y + lh / 2 + 2, P3, 1.3, "4,3"))

    return svg_wrap(W, H, "".join(b))


# =========================================================================
# FIGURE 3 — Cost model (matplotlib)
# =========================================================================
def fig_costmodel():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.edgecolor": "#333", "axes.linewidth": 0.9})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.4, 3.9))

    # -- Panel (a): per-wake cost vs poll gap (cache TTL step) --
    WARM, COLD = 1.0, 12.5
    # explicit segments so the riser sits exactly on the 300 s TTL line
    ax1.plot([0, 300], [WARM, WARM], color=NAVY, lw=2.2)
    ax1.plot([300, 300], [WARM, COLD], color=NAVY, lw=2.2)
    ax1.plot([300, 950], [COLD, COLD], color=NAVY, lw=2.2)
    ax1.axvline(300, color=P4, ls="--", lw=1.6)
    ax1.axvspan(0, 300, color="#e6f2ea", alpha=0.7)
    ax1.axvspan(300, 950, color="#fbe9e3", alpha=0.7)
    ax1.text(150, 6.6, "warm cache READ\n(poll ≤ 240 s)", ha="center", va="center",
             fontsize=10.5, color="#1c6b40", fontweight="bold")
    ax1.text(640, 8.5, "cold context REWRITE\n≈ 12.5× a read", ha="center", va="center",
             fontsize=10.5, color=P4, fontweight="bold")
    ax1.text(305, 11.6, "prompt-cache TTL = 300 s", ha="left", va="top", fontsize=9.5, color=P4)
    ax1.set_xlabel("gap between two polls of one context  (s)")
    ax1.set_ylabel("cost per wake  (× a warm read)")
    ax1.set_ylim(0, 13.5)
    ax1.set_xlim(0, 950)
    ax1.set_title("(a)  why polls must stay under the cache TTL", fontsize=11.5, fontweight="bold")

    # -- Panel (b): cumulative cost vs job duration, poll vs event-wake --
    dur = list(range(0, 121, 2))                     # minutes
    reads_per_hr = 15.0                              # warm reads/hr of context while polling
    poll_cost = [WARM * reads_per_hr * (d / 60.0) for d in dur]   # grows with duration
    wake_cost = [COLD for _ in dur]                  # one cold rewrite, flat
    ax2.plot(dur, poll_cost, color=NAVY, lw=2.3, label="keep polling (warm reads)")
    ax2.plot(dur, wake_cost, color=P4, lw=2.3, ls="-", label="submit + sleep → 1 event-wake")
    be = COLD / reads_per_hr * 60.0                  # break-even minutes = 12.5/15 h
    ax2.axvline(be, color=SLATE, ls=":", lw=1.5)
    ax2.plot([be], [COLD], "o", color=GOLD_D, ms=7, zorder=5)
    ax2.text(be + 3, 4.5, f"break-even\n≈ {be:.0f} min", fontsize=10, color=SLATE, fontweight="bold")
    ax2.text(52, 22, "poll  ≤ 50 min", fontsize=10, color=NAVY, rotation=32)
    ax2.text(74, 15.2, "event-wake  > 50 min", fontsize=10, color=P4)
    ax2.set_xlabel("time the deputy waits on a job  (min)")
    ax2.set_ylabel("cumulative cost  (× a warm read)")
    ax2.set_xlim(0, 120)
    ax2.set_ylim(0, 32)
    ax2.set_title("(b)  poll vs. event-wake: the 50-min rule", fontsize=11.5, fontweight="bold")
    ax2.legend(loc="upper left", fontsize=9.5, frameon=True)

    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig_costmodel.pdf"))
    fig.savefig(os.path.join(HERE, "fig_costmodel.png"), dpi=150)
    plt.close(fig)
    return "fig_costmodel"


# =========================================================================
def render_svg(name, svg):
    import cairosvg
    with open(os.path.join(HERE, name + ".svg"), "w") as f:
        f.write(svg)
    cairosvg.svg2pdf(bytestring=svg.encode(), write_to=os.path.join(HERE, name + ".pdf"))
    cairosvg.svg2png(bytestring=svg.encode(), write_to=os.path.join(HERE, name + ".png"), output_width=1500)
    return name


if __name__ == "__main__":
    ok = []
    for name, fn in [("fig_architecture", fig_architecture), ("fig_lifecycle", fig_lifecycle)]:
        try:
            render_svg(name, fn())
            ok.append(name)
        except Exception as e:
            print("ERR", name, repr(e))
    try:
        ok.append(fig_costmodel())
    except Exception as e:
        print("ERR fig_costmodel", repr(e))
    print("built:", ", ".join(ok))
