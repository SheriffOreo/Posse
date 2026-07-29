"""HTML for the dashboard. Login is server-rendered (escaped); the status/history
pages are thin shells filled by fetch() from the JSON APIs (all dynamic values go
in via textContent on the client, so they are XSS-safe)."""
import html

_CSS = """
/* ---- theme palette (CSS custom properties) ------------------------------- #
   Night (default) lives in :root and holds the ORIGINAL hex values verbatim, so
   the dark theme is byte-identical to before. Day is a .theme-day override on
   <html> (toggled by the nav button, persisted in localStorage). Status/GPU/
   badge accent hues (greens/reds/purples) stay literal — they read on both bgs. */
:root{
  --bg:#0f1420; --fg:#e6e9ef; --link:#6cb6ff;
  --header-bg:#161c2b;
  --border:#263049; --border-soft:#1e273b; --cal-border:#24304a; --code-border:#22304a;
  --card:#141b29; --surface:#131a28; --surface2:#0d1320;
  --row-hover:#161d2c; --has-bg:#16243a; --hover-border:#3d68a8;
  --chip-bg:#20293d; --chip2-bg:#1b2740; --ctx-bg:#1b2233;
  --muted:#8093b0; --muted2:#7f8ba3; --th:#8fa3c7; --h2:#9db2d6;
  --tlink:#cdd7ea; --code-fg:#b9c7e0; --empty:#7c8698; --dot:#5a6b8c; --tree-line:#33415f;
  --accent-border:#2f5488; --btn-bg:#20406b; --btn-fg:#dbe7ff; --btn-hover:#295084;
  --amber:#e3b341; --onday:#f0d48a;
  --heat1:#17283f; --heat2:#1e3a5c; --heat3:#28527e; --heat4:#3466a0;
}
.theme-day{
  --bg:#eef1f6; --fg:#1b2330; --link:#1560c4;
  --header-bg:#ffffff;
  --border:#d5dbe6; --border-soft:#e3e8f0; --cal-border:#ccd4e1; --code-border:#d5dbe6;
  --card:#ffffff; --surface:#f4f7fb; --surface2:#e9edf4;
  --row-hover:#eaf0f9; --has-bg:#dbe8f7; --hover-border:#7ba3d8;
  --chip-bg:#e7ecf4; --chip2-bg:#e2e8f2; --ctx-bg:#edeff5;
  --muted:#5f6b80; --muted2:#6b7789; --th:#57647f; --h2:#3a4d6e;
  --tlink:#2b3648; --code-fg:#39465f; --empty:#66707f; --dot:#9aa7bd; --tree-line:#c2cbd9;
  --accent-border:#b7cbe9; --btn-bg:#e6eefb; --btn-fg:#1a4d8f; --btn-hover:#d6e3f8;
  --amber:#9a6f12; --onday:#8a6d0f;
  --heat1:#e0ecfb; --heat2:#c3daf5; --heat3:#9dc2ec; --heat4:#6ea3df;
}
/* day-mode chips/badges (dark-fill status chips -> light tints, darker text) */
.theme-day .b-running{background:#d8f1df;color:#1a7a33;} .theme-day .b-done{background:#e4e9f2;color:#3f5170;}
.theme-day .b-failed{background:#fbdedb;color:#b3261e;} .theme-day .b-parked{background:#f6ecd0;color:#7d620d;}
.theme-day .b-pending{background:#dde9fb;color:#1560c4;} .theme-day .b-gpu{background:#eaddfb;color:#7b3fb8;}
.theme-day .banner{background:#fce8e6;border-color:#f0b8b3;color:#b3261e;}
* { box-sizing: border-box; }
body { margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg); color:var(--fg); }
a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
header { display:flex; align-items:center; gap:16px; padding:10px 18px;
         background:var(--header-bg); border-bottom:1px solid var(--border); position:sticky; top:0; z-index:5; }
header .brand { font-weight:700; letter-spacing:.3px; display:inline-flex; align-items:center; gap:7px; }
header nav a { margin-right:14px; font-weight:600; }
header nav a.cur { color:var(--fg); border-bottom:2px solid var(--link); padding-bottom:2px; }
header .spacer { flex:1; }
.pill { display:inline-flex; align-items:center; gap:6px; padding:2px 9px; border-radius:20px;
        font-size:12px; font-weight:600; background:var(--chip-bg); }
.dot { width:8px; height:8px; border-radius:50%; background:var(--dot); display:inline-block; }
.dot.ok { background:#3fb950; } .dot.bad { background:#f85149; }
main { padding:18px; max-width:1200px; margin:0 auto; }
h2 { font-size:15px; text-transform:uppercase; letter-spacing:.5px; color:var(--h2);
     margin:22px 0 8px; border-bottom:1px solid var(--border); padding-bottom:4px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:6px 9px; border-bottom:1px solid var(--border-soft); vertical-align:top; }
th { color:var(--th); font-weight:600; font-size:11px; text-transform:uppercase; }
tr:hover td { background:var(--row-hover); }
/* Task 346: wide tables (Status workers/jobmgr) scroll instead of crushing at narrow
   width. The min-width floor is what forces the scroll — a bare width:100% table would
   just re-wrap inside the wrapper. Desktop (container >= 760px) is byte-identical. */
.tablewrap { overflow-x:auto; }
.tablewrap > table { min-width:760px; }
.badge { padding:1px 7px; border-radius:5px; font-size:11px; font-weight:700; }
.b-running{background:#12351d;color:#5ce07e;} .b-done{background:#1b2740;color:#9db2d6;}
.b-failed{background:#3a1518;color:#ff7b72;} .b-parked{background:#33280f;color:#e3b341;}
.b-pending{background:#1a2b3d;color:#6cb6ff;} .b-gpu{background:#2a1a3d;color:#c58bff;}
.card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:12px 14px; margin-bottom:10px; }
.card .card{ background:var(--surface); }  /* nested cards get a distinct surface for depth (Task 344 critic P1.4) */
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:10px; }
.muted { color:var(--muted); } .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.banner { background:#3a1518; border:1px solid #7d2b30; color:#ffb4b0; padding:10px 14px;
          border-radius:8px; margin-bottom:14px; font-weight:600; }
.flag { color:var(--amber); font-weight:700; }
button, .btn { background:var(--btn-bg); color:var(--btn-fg); border:1px solid var(--accent-border); padding:5px 11px;
        border-radius:6px; cursor:pointer; font-size:13px; }
button:hover { background:var(--btn-hover); }
.cal { display:grid; grid-template-columns:repeat(7,1fr); gap:5px; max-width:520px; margin:0 auto; }
.cal .h { text-align:center; color:var(--muted); font-size:11px; font-weight:700; }
.cal .day { min-height:46px; border:1px solid var(--cal-border); border-radius:6px; padding:4px; cursor:pointer;
            background:var(--surface); position:relative; }
.cal .day:hover { border-color:var(--hover-border); }
.cal .day.empty { background:transparent; border:none; cursor:default; }
.cal .day.has { background:var(--has-bg); }
/* Task 346: count-graded heatmap (log-scaled, 4 buckets). Defined AFTER .day.has so
   these win on equal specificity; the --heatN vars swap with the theme automatically. */
.cal .day.h1 { background:var(--heat1); } .cal .day.h2 { background:var(--heat2); }
.cal .day.h3 { background:var(--heat3); } .cal .day.h4 { background:var(--heat4); }
.cal .day.sel { outline:2px solid var(--link); }
.cal .day .n { font-size:11px; color:var(--h2); }
.cal .day .c { position:absolute; right:4px; bottom:3px; font-size:11px; font-weight:700; color:var(--link); }
/* heatmap legend under the calendar (shares the --heatN vars, so it themes too) */
.calkey { display:flex; align-items:center; justify-content:center; gap:5px; margin:8px 0 2px; color:var(--muted); font-size:12px; }
.calkey i { width:14px; height:14px; border-radius:3px; border:1px solid var(--cal-border); display:inline-block; }
.calkey i.k1 { background:var(--heat1); } .calkey i.k2 { background:var(--heat2); }
.calkey i.k3 { background:var(--heat3); } .calkey i.k4 { background:var(--heat4); }
.two { display:grid; grid-template-columns:540px 1fr; gap:18px; align-items:start; }
/* Task 336: the detail/conversation panel follows the scroll (History + Lineage
   reuse .two + #detail). Sticky is scoped to the two-column context, so Status's
   full-width #detail is unaffected; align-self:start keeps the grid child from
   stretching (which would defeat sticky).
   Task 338: bound the sticky panel to the viewport (max-height) and give it its
   own overflow-y, so a conversation taller than the screen scrolls INSIDE the
   pinned panel instead of running off-screen (page-scroll would otherwise only
   move the left day-list, leaving the bottom of a long thread unreachable).
   overflow on the sticky element itself is safe — only overflow on an ANCESTOR
   would break position:sticky, and no ancestor of .two sets it. */
.two > #detail { position:sticky; top:64px; align-self:start;
                 max-height:calc(100vh - 80px); overflow-y:auto;
                 scrollbar-width:thin; scrollbar-color:var(--border) transparent; }
@media(max-width:1000px){ .two{grid-template-columns:1fr;} .two > #detail{ position:static; max-height:none; overflow:visible; } }
/* Task 338: thin themed scrollbar for the internally-scrolling detail panel. Only
   renders when a tall conversation overflows the cap; short panels show none, so
   they stay byte-identical. Scoped to .two > #detail (Status's panel is unaffected). */
.two > #detail::-webkit-scrollbar { width:10px; }
.two > #detail::-webkit-scrollbar-track { background:transparent; }
.two > #detail::-webkit-scrollbar-thumb { background:var(--border); border-radius:5px; }
.thread .msg { border-left:3px solid var(--accent-border); padding:5px 10px; margin:7px 0; background:var(--surface); border-radius:0 6px 6px 0; }
.thread .msg.in { border-left-color:#3fb950; background:var(--has-bg); }  /* faint tint separates inbound from outbound (Task 346 P3) */
.lin { list-style:none; padding-left:0; } .lin li { padding:3px 0; }
.lin .arrow { color:var(--muted); } .lin .cur { font-weight:700; color:var(--amber); }
.small { font-size:12px; } .nowrap{white-space:nowrap;}
code.cmd{ display:block; white-space:pre-wrap; word-break:break-all; background:var(--surface2); padding:6px 8px;
          border-radius:5px; border:1px solid var(--code-border); color:var(--code-fg); }
/* ---- lineage forest ---- */
.legend { display:flex; flex-wrap:wrap; gap:16px; align-items:center; margin:4px 0 14px; font-size:12px; color:var(--h2); }
.legend .k { display:inline-flex; align-items:center; gap:7px; }
.cbar { width:4px; height:15px; border-radius:1px; flex:none; display:inline-block; }
.cbar.bh{background:#3fb950;} .cbar.bm{background:var(--amber);} .cbar.bl{background:#8a94a6;} .cbar.broot{background:var(--link);}
details.tree, details.singles { margin-bottom:6px; }
details.tree > summary, details.singles > summary { cursor:pointer; list-style:none; padding:5px 2px; outline:none; }
details.tree > summary::-webkit-details-marker, details.singles > summary::-webkit-details-marker { display:none; }
details.tree > summary::before, details.singles > summary::before { content:'▾'; color:var(--muted); margin-right:6px; }
details.tree:not([open]) > summary::before, details.singles:not([open]) > summary::before { content:'▸'; }
ul.tree-ul { list-style:none; margin:0; padding-left:15px; border-left:1px solid var(--tree-line); }
ul.tree-ul.root { border-left:none; padding-left:0; }
.tnode { display:flex; align-items:center; gap:8px; padding:2px 0; }
a.tlink { color:var(--tlink); } a.tlink:hover { color:var(--link); }
.bchip { font-size:11px; padding:1px 6px; border-radius:9px; background:var(--chip-bg); color:var(--th); white-space:nowrap; }
.wchip { font-size:11px; color:var(--muted2); white-space:nowrap; font-family:ui-monospace,monospace; }
.singlewrap { display:flex; flex-wrap:wrap; gap:5px; margin-top:8px; }
a.schip { font-size:12px; padding:1px 7px; border-radius:5px; background:var(--chip2-bg); color:var(--h2); font-family:ui-monospace,monospace; }
a.schip:hover { background:var(--btn-bg); color:var(--btn-fg); text-decoration:none; }
.empty-state { text-align:center; color:var(--empty); padding:34px 16px; font-style:italic; }
/* ---- day-lineage mini-trees (History) ---- */
.daytrees .mtree { padding:6px 0; }
.daytrees .mtree + .mtree { border-top:1px solid var(--border-soft); }
.tnode.onday > a.tlink { color:var(--onday); font-weight:700; }
.tnode.offday { opacity:.6; }
.bchip.ctx { background:var(--ctx-bg); color:var(--muted2); font-style:italic; }
/* ---- full conversation body + inline attachments (Task detail) ---- */
.msgbody { white-space:pre-wrap; word-break:break-word; max-height:360px; overflow:auto;
           background:var(--surface2); border:1px solid var(--code-border); border-radius:5px;
           padding:7px 9px; margin-top:5px; font-size:13px; }
.atts { margin-top:8px; display:flex; flex-direction:column; gap:7px; }
img.attimg { max-width:100%; height:auto; border:1px solid var(--border); border-radius:6px; background:var(--surface2); }
a.attfile { display:inline-block; }
/* ---- deliverables section (Task detail) ---- */
.deliv { margin-top:12px; padding-top:8px; border-top:1px solid var(--border); }
.deliv .job { margin:5px 0; display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.deliv a { color:var(--link); }
/* Task 346: login / register brand lockup (mark + wordmark, centered above the form) */
.brandmark { display:flex; flex-direction:column; align-items:center; gap:6px; margin-bottom:14px; }
.brandmark span { font-weight:700; letter-spacing:.3px; font-size:16px; color:var(--fg); }
/* ---- cowork assignment form + agent-search modal (Task 353) ---- */
.cowork-form { max-width:680px; display:flex; flex-direction:column; gap:16px; }
.cowork-form .fld { display:flex; flex-direction:column; gap:6px; }
.cowork-form .fld.chk { flex-direction:row; align-items:center; gap:9px; cursor:pointer; }
.cowork-form .lbl { font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:var(--th); font-weight:600; }
.cowork-form textarea, .cowork-form input[type=text], .modal-card input[type=text] {
  width:100%; padding:9px; border-radius:6px; border:1px solid var(--accent-border);
  background:var(--surface2); color:var(--fg); font:inherit; }
.cowork-form textarea { resize:vertical; min-height:96px; }
.cowork-form .pickrow { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.cowork-form .chips { display:flex; flex-wrap:wrap; gap:6px; }
.cw-chip, .cw-lead { display:inline-flex; align-items:center; gap:7px; font-family:ui-monospace,monospace;
  border-radius:12px; padding:2px 9px; }
.cw-chip { font-size:12px; background:var(--chip-bg); color:var(--h2); }
.cw-lead { font-size:13px; background:var(--has-bg); color:var(--fg); border:1px solid var(--accent-border); }
.cw-chip .x, .cw-lead .x { cursor:pointer; color:var(--muted); font-weight:700; }
.cw-chip .x:hover, .cw-lead .x:hover { color:var(--link); }
.cowork-form .actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
/* modal overlay + card (agent search). Scrim reads on both themes. */
.modal { position:fixed; inset:0; background:rgba(6,10,18,.62); display:flex;
  align-items:flex-start; justify-content:center; z-index:20; padding:56px 16px; }
.modal-card { background:var(--card); border:1px solid var(--border); border-radius:10px;
  width:min(560px,100%); max-height:78vh; display:flex; flex-direction:column; padding:14px;
  box-shadow:0 12px 44px rgba(0,0,0,.45); }
.modal-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
.pickresults { overflow-y:auto; margin-top:10px; display:flex; flex-direction:column; gap:4px;
  scrollbar-width:thin; scrollbar-color:var(--border) transparent; }
.pickrow-item { padding:7px 9px; border-radius:6px; cursor:pointer; border:1px solid transparent; }
.pickrow-item:hover { background:var(--row-hover); border-color:var(--hover-border); }
.pickrow-item .an { font-weight:700; font-family:ui-monospace,monospace; color:var(--tlink); }
.pickrow-item .at { color:var(--muted); font-size:12px; }
.pickrow-item .tk { display:inline-block; font-size:11px; color:var(--th); background:var(--chip-bg);
  border-radius:8px; padding:0 6px; margin:3px 4px 0 0; font-family:ui-monospace,monospace; }
"""

def _mark(px):
    """The Rookery mark (rook tower + beacon) as a self-contained, flat-fill inline SVG.
    No <defs>/gradients/ids, so it never collides when it appears more than once on a
    page, and it reads on any background (nav header + login card, both themes). CSP-safe
    (inline SVG, not an <img src> — the server won't serve asset files)."""
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512' width='{px}' height='{px}' "
        "role='img' aria-label='Rookery' style='vertical-align:middle;flex:none'>"
        "<circle cx='256' cy='150' r='30' fill='#f0d48a'/>"
        "<circle cx='256' cy='150' r='30' fill='none' stroke='#e3b341' stroke-width='8'/>"
        "<circle cx='249' cy='142' r='9' fill='#fff8e6'/>"
        "<path d='M150 372 h212 a12 12 0 0 1 12 12 v28 a12 12 0 0 1 -12 12 h-212 "
        "a12 12 0 0 1 -12 -12 v-28 a12 12 0 0 1 12 -12 z' fill='#3d6ea5'/>"
        "<rect x='186' y='238' width='140' height='146' fill='#5aa0e0'/>"
        "<path d='M162 238 v-52 h32 v-28 h20 v28 h28 v-28 h20 v28 h28 v-28 h20 v28 h32 v52 z' fill='#5aa0e0'/>"
        "<path d='M226 384 v-46 a30 30 0 0 1 60 0 v46 z' fill='#16243a'/>"
        "</svg>"
    )


def _nav(active=""):
    """Header nav. `active` in {status,history,lineage,cowork} marks the current link
    with .cur so you can see which page you're on (Task 346; +cowork Task 353)."""
    def cur(name):
        return " class='cur'" if name == active else ""
    return (
        "<header>"
        f"<span class='brand'>{_mark(22)}Rookery</span>"
        f"<nav><a href='/'{cur('status')}>Status</a>"
        f"<a href='/history'{cur('history')}>History</a>"
        f"<a href='/lineage'{cur('lineage')}>Lineage</a>"
        f"<a href='/cowork'{cur('cowork')}>Cowork</a></nav>"
        "<span id='daemons' class='small muted'></span>"
        "<span class='spacer'></span>"
        "<button id='themebtn' class='small' title='Toggle day / night mode' onclick='toggleTheme()' "
        "style='margin-right:12px;padding:2px 9px;line-height:1.3'>&#9790;</button>"
        "<a href='/logout' class='small'>Logout</a>"
        "</header>"
    )

# Applied in <head> BEFORE the stylesheet so the saved theme is on <html> before
# first paint (no flash-of-dark). Night is the default: with nothing stored, no
# class is added and :root (the original dark palette) applies unchanged.
_THEME_PREPAINT = (
    "try{if(localStorage.getItem('infra-theme')==='day')"
    "document.documentElement.classList.add('theme-day');}catch(e){}"
)
# Defined once (end of <body>, present on every _shell page): flip the class,
# persist it, and swap the nav glyph (moon = night active, sun = day active).
_THEME_JS = (
    "function _setThemeGlyph(){var b=document.getElementById('themebtn');"
    "if(b)b.textContent=document.documentElement.classList.contains('theme-day')?'\\u2600':'\\u263e';}"
    "function toggleTheme(){var d=document.documentElement.classList.toggle('theme-day');"
    "try{localStorage.setItem('infra-theme',d?'day':'night');}catch(e){}_setThemeGlyph();}"
    "_setThemeGlyph();"
)


def _shell(title, body, script="", active=""):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        f"<script>{_THEME_PREPAINT}</script>"
        f"<style>{_CSS}</style></head>"
        f"<body>{_nav(active)}<main>{body}</main>"
        f"<script>{_THEME_JS}</script>"
        f"<script>{script}</script></body></html>"
    )


def login_page(error=""):
    err = f"<div class='banner'>{html.escape(error)}</div>" if error else ""
    body = (
        "<div style='max-width:340px;margin:12vh auto;' class='card'>"
        f"<div class='brandmark'>{_mark(48)}<span>Rookery</span></div>"
        "<h2 style='border:none;margin-top:0'>Sign in</h2>"
        f"{err}"
        "<form method='post' action='/login'>"
        "<input type='password' name='password' placeholder='Password' autofocus "
        "style='width:100%;padding:9px;border-radius:6px;border:1px solid var(--accent-border);"
        "background:var(--surface2);color:var(--fg);margin-bottom:10px'>"
        "<button type='submit' style='width:100%'>Enter</button>"
        "</form></div>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Sign in</title><script>{_THEME_PREPAINT}</script>"
        f"<style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def register_page(token=None, error="", closed=False):
    """One-time registration form (bootstraps the first password). Server-rendered
    and escaped; standalone shell (no nav) like the login page."""
    err = f"<div class='banner'>{html.escape(error)}</div>" if error else ""
    if closed:
        inner = (
            err +
            "<p class='muted small'>If you have a valid one-time link, open it again. "
            "Otherwise ask the operator to generate a fresh registration link.</p>"
            "<p><a href='/login'>Go to sign in</a></p>"
        )
    else:
        safe_tok = html.escape(token or "")
        field = ("width:100%;padding:9px;border-radius:6px;border:1px solid var(--accent-border);"
                 "background:var(--surface2);color:var(--fg);margin-bottom:10px")
        inner = (
            err +
            "<p class='muted small'>Choose a password for this dashboard. This is a "
            "one-time link &mdash; it works once, then expires.</p>"
            "<form method='post' action='/register'>"
            f"<input type='hidden' name='token' value='{safe_tok}'>"
            f"<input type='password' name='password' placeholder='New password (min 8)' "
            f"autofocus style='{field}'>"
            f"<input type='password' name='password2' placeholder='Confirm password' "
            f"style='{field}'>"
            "<button type='submit' style='width:100%'>Set password &amp; continue</button>"
            "</form>"
        )
    body = (
        "<div style='max-width:360px;margin:11vh auto;' class='card'>"
        f"<div class='brandmark'>{_mark(48)}<span>Rookery</span></div>"
        "<h2 style='border:none;margin-top:0'>Register dashboard</h2>"
        f"{inner}</div>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Register</title><script>{_THEME_PREPAINT}</script>"
        f"<style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def status_page():
    body = (
        "<div id='limit'></div>"
        "<h2>Active Workers <span id='wc' class='muted small'></span></h2>"
        "<div id='workers' class='tablewrap'>loading&hellip;</div>"  # Task 346: 7-col table scrolls at narrow width
        "<div id='detail'></div>"  # Task 327: task detail when a worker's task# is clicked
        "<h2>GPU</h2><div id='gpu'>loading&hellip;</div>"
        "<h2>Job Manager &mdash; Active Jobs</h2><div id='jobs' class='tablewrap'>loading&hellip;</div>"
        "<p class='muted small' id='updated'></p>"
    )
    return _shell("Infra Status", body, _STATUS_JS, active="status")


def history_page():
    body = (
        "<h2>Job History</h2>"
        "<div class='two'>"
        "<div><div class='card'><div style='display:flex;align-items:center;gap:10px;margin-bottom:8px'>"
        "<button id='prev'>&#8592;</button><b id='mlabel'></b><button id='next'>&#8594;</button></div>"
        "<div id='cal' class='cal'></div>"
        "<div class='calkey'>fewer<i class='k1'></i><i class='k2'></i>"
        "<i class='k3'></i><i class='k4'></i>more</div>"
        "<p class='muted small'>Click a day to list what was launched.</p></div>"
        "<div id='dayview'></div></div>"
        "<div id='detail'><div class='card muted'>Select a job or task to see details.</div></div>"
        "</div>"
    )
    return _shell("Job History", body, _HISTORY_JS, active="history")


# --------------------------------------------------------------------------- #
# client JS  (plain strings; no server interpolation)
# --------------------------------------------------------------------------- #
_COMMON_JS = r"""
function el(t,c,txt){var e=document.createElement(t); if(c)e.className=c;
  if(txt!==undefined)e.textContent=txt; return e;}
function badge(s){var m={running:'b-running',done:'b-done',failed:'b-failed',
  pending:'b-pending',sleeping:'b-parked',wakes:'b-parked'};
  var b=el('span','badge '+(m[s]||'b-done'), s||'?'); return b;}
function fmtTs(t){ if(!t) return ''; var d=new Date(t*1000); return d.toLocaleString(); }
async function getJSON(u){var r=await fetch(u); if(r.status===401){location='/login';return null;}
  if(!r.ok) throw new Error(u+' '+r.status); return r.json();}
function daemonsBar(ds){var s=document.getElementById('daemons'); if(!s)return; s.innerHTML='';
  ds.forEach(function(d){var p=el('span','pill'); var dot=el('span','dot '+(d.alive?'ok':'bad'));
    p.appendChild(dot); p.appendChild(el('span',null,d.name)); s.appendChild(p);
    s.appendChild(document.createTextNode(' '));});}
function basename(p){ if(!p) return ''; var s=String(p); var i=s.lastIndexOf('/'); return i>=0?s.slice(i+1):s; }
// Lineage tree rendering — shared by the Lineage forest AND the History day view.
// A node with on_day===false is off-day context (muted + a "context" chip); nodes
// with no on_day key (the Lineage page) render normally with no highlight.
var DOT={ 'explicit-chain':'bh','parent-field':'bh','reply-subject':'bm',
  'followup-phrase':'bm','subject-thread':'bl' };
var BLABEL={ 'explicit-chain':'explicit chain','parent-field':'parent_task field',
  'reply-subject':'reply subject','followup-phrase':'follow-up phrasing','subject-thread':'same subject' };
function treeNodeLi(n){
  var li=el('li');
  var row=el('div','tnode'+(n.on_day===true?' onday':(n.on_day===false?' offday':'')));
  row.appendChild(el('span','cbar '+(DOT[n.basis]||'broot')));
  var a=el('a','tlink','#'+n.task_id+'  '+(n.title||''));
  a.href='javascript:void(0)'; a.title=n.title||'';
  a.onclick=(function(id){return function(){showTask(id);};})(n.task_id);
  row.appendChild(a);
  if(n.basis) row.appendChild(el('span','bchip',BLABEL[n.basis]||n.basis));
  if(n.agent) row.appendChild(el('span','wchip','· '+n.agent));  // Task 327: owning worker
  if(n.on_day===false) row.appendChild(el('span','bchip ctx','context'));
  li.appendChild(row);
  if(n.children && n.children.length){
    var ul=el('ul','tree-ul');
    n.children.forEach(function(c){ ul.appendChild(treeNodeLi(c)); });
    li.appendChild(ul);
  }
  return li;
}
// Task detail panel (thread + reconstructed lineage). Shared by History + Lineage;
// harmless on pages without a #detail container (guarded).
async function showTask(id){
  var D=document.getElementById('detail'); if(!D) return;
  D.innerHTML='<div class="card muted">loading&hellip;</div>';
  var t=await getJSON('/api/task?id='+id); if(!t){D.innerHTML='';return;}
  var c=el('div','card'); c.appendChild(el('h2',null,'Task #'+id+(t.conversation.agent?(' · '+t.conversation.agent):'')));
  if(t.conversation.subject) c.appendChild(el('div',null,t.conversation.subject));
  if(t.lineage){ var lin=el('div','card'); lin.appendChild(el('div','muted small','LINEAGE'));
    var ul=el('ul','lin');
    function lrow(n,dir){ var li=el('li'); if(dir){var a0=el('span','arrow',dir+' '); li.appendChild(a0);}
      var s=el('a','',' #'+n.task_id+' '+n.title); s.href='javascript:void(0)';
      s.onclick=(function(x){return function(){showTask(x);};})(n.task_id); li.appendChild(s);
      if(n.basis) li.appendChild(el('span','muted small',' ['+n.basis+']')); return li; }
    t.lineage.ancestors.forEach(function(a){ ul.appendChild(lrow(a,'↑')); });
    var self=el('li','cur'); self.textContent='● #'+t.lineage.self.task_id+' '+t.lineage.self.title; ul.appendChild(self);
    t.lineage.children.forEach(function(k){ ul.appendChild(lrow(k,'↓')); });
    lin.appendChild(ul);
    if(!t.lineage.ancestors.length && !t.lineage.children.length)
      lin.appendChild(el('div','muted small','No linked parent/children reconstructed.'));
    c.appendChild(lin);}
  var conv=el('div','thread'); conv.appendChild(el('div','muted small','CONVERSATION'));
  if(!t.conversation.messages.length) conv.appendChild(el('div','muted','No emails matched this thread.'));
  t.conversation.messages.forEach(function(m){var d=el('div','msg '+(m.dir==='in'?'in':'out'));
    var hd=el('div','small muted', (m.dir==='in'?('⇦ '+(m.from||'user')):('⇨ '+(m.agent||'agent')+' → '+(m.to||'')))+'  ·  '+fmtTs(m.ts));
    d.appendChild(hd); if(m.subject) d.appendChild(el('div',null,m.subject));
    if(m.body) d.appendChild(el('div','msgbody',m.body));              // full, untruncated
    else if(m.dir==='out') d.appendChild(el('div','small muted','(outbound body not stored — email predates Task 323 body logging)'));
    if(m.attachments && m.attachments.length){                        // inline images / file links
      var at=el('div','atts');
      m.attachments.forEach(function(a){
        var url='/download?path='+encodeURIComponent(a.path);
        if(a.is_image){ var im=document.createElement('img'); im.className='attimg';
          im.src=url; im.alt=a.name; im.loading='lazy'; at.appendChild(im); }
        else { var la=el('a','attfile small','📎 '+a.name); la.href=url; la.target='_blank'; at.appendChild(la); }
      });
      d.appendChild(at);
    }
    conv.appendChild(d);});
  c.appendChild(conv);
  // deliverables (best-effort): job artifacts owned by this task's agent + reports/
  var dd=t.deliverables||{jobs:[],files:[]};
  var dl=el('div','deliv'); dl.appendChild(el('div','muted small','DELIVERABLES (best-effort)'));
  if(!(dd.jobs&&dd.jobs.length) && !(dd.files&&dd.files.length))
    dl.appendChild(el('div','muted small','(none found)'));
  (dd.jobs||[]).forEach(function(j){
    var jb=el('div','job'); jb.appendChild(el('span','mono small','job '+j.job_id));
    jb.appendChild(badge(j.status));
    (j.paths||[]).forEach(function(p){ var a=el('a','small',p.label+': '+basename(p.path));
      a.href='/download?path='+encodeURIComponent(p.path); a.target='_blank'; jb.appendChild(a); });
    dl.appendChild(jb);
  });
  (dd.files||[]).forEach(function(f){ var a=el('a','small',(f.emailed?'📧 emailed: ':'📄 ')+f.name);
    a.href='/download?path='+encodeURIComponent(f.path); a.target='_blank';
    a.title=f.emailed?'attachment this task’s agent emailed (authoritative)':'reports/ match';
    a.style.display='block'; dl.appendChild(a); });
  c.appendChild(dl);
  c.appendChild(el('div','muted small',t.conversation.note||''));
  D.innerHTML=''; D.appendChild(c);
}
"""

_STATUS_JS = _COMMON_JS + r"""
async function tick(){
  var s; try{ s=await getJSON('/api/status'); }catch(e){ return; }
  if(!s) return;
  daemonsBar(s.daemons);
  // limit banner
  var L=document.getElementById('limit'); L.innerHTML='';
  if(s.limit){ var b=el('div','banner');
    b.textContent='USAGE LIMIT HIT ('+(s.limit.kind||'?')+') — reset at '+
      (s.limit.reset_str||fmtTs(s.limit.reset_epoch))+'  · source: '+(s.limit.source_worker||'?');
    L.appendChild(b);}
  // workers
  document.getElementById('wc').textContent='('+s.workers.length+' active)';
  var wt=el('table'); wt.innerHTML='<tr><th>worker</th><th>state</th><th>task#</th><th>description</th>'+
    '<th>requester</th><th class=nowrap>mail</th><th>rl</th></tr>';
  s.workers.forEach(function(w){var tr=el('tr');
    tr.appendChild(td(w.name,'mono'));
    var st=el('td'); var cls=w.state==='running'?'b-running':(w.state==='failed'?'b-failed':
      (String(w.state).indexOf('wait')>=0?'b-parked':'b-done'));
    st.appendChild(el('span','badge '+cls, w.state_label||w.state)); tr.appendChild(st);
    // Task 327: task# — clickable like the tree links, opens the detail panel below
    var tk=el('td');
    if(w.task){ var a=el('a','tlink','#'+w.task); a.href='javascript:void(0)';
      a.onclick=(function(id){return function(){showTask(id);};})(w.task); tk.appendChild(a); }
    tr.appendChild(tk);
    // description = the task's title (fall back to the registry desc when unresolved)
    tr.appendChild(td(w.task_title||w.desc||'',''));
    tr.appendChild(td(w.requester||'','small muted'));
    tr.appendChild(td(w.mailbox_pending?'●':'','flag'));
    tr.appendChild(td(String(w.relaunched||0),'small muted'));
    wt.appendChild(tr);});
  var W=document.getElementById('workers'); W.innerHTML=''; W.appendChild(wt);
  // gpu
  var G=document.getElementById('gpu'); G.innerHTML='';
  var gc=el('div','card');
  gc.appendChild(el('span','pill')).appendChild(el('span','dot '+(s.gpu.manager_alive?'ok':'bad')));
  gc.appendChild(el('span',null,' gpu_manager '+(s.gpu.manager_alive?'alive':'DOWN')+' · '));
  gc.appendChild(el('span',null, s.gpu.running.length+' running, '+s.gpu.pending.length+' pending'));
  G.appendChild(gc);
  if(s.gpu.running.length||s.gpu.pending.length){
    var gt=el('table'); gt.innerHTML='<tr><th>state</th><th>id</th><th>command</th></tr>';
    s.gpu.running.concat(s.gpu.pending).forEach(function(j){var tr=el('tr');
      var st=el('td'); st.appendChild(badge(j.status)); tr.appendChild(st);
      tr.appendChild(td(j.job_id,'mono small')); tr.appendChild(td(j.command||'','mono small'));
      gt.appendChild(tr);});
    G.appendChild(gt);}
  // jobmgr
  var J=document.getElementById('jobs'); J.innerHTML='';
  if(!s.jobs.length){ J.appendChild(el('div','card muted','No active jobmgr jobs.')); }
  else{ var jt=el('table'); jt.innerHTML='<tr><th>bucket</th><th>owner</th><th>type</th>'+
      '<th>command</th><th class=nowrap>est</th></tr>';
    s.jobs.forEach(function(j){var tr=el('tr');
      var st=el('td'); st.appendChild(badge(j.bucket)); tr.appendChild(st);
      tr.appendChild(td(j.owner_agent,'')); tr.appendChild(td(j.type,'small'));
      tr.appendChild(td(j.command||'','mono small'));
      tr.appendChild(td(j.est_seconds?(Math.round(j.est_seconds/60)+'m'):'','small muted'));
      jt.appendChild(tr);});
    J.appendChild(jt);}
  document.getElementById('updated').textContent='updated '+new Date().toLocaleTimeString();
}
function td(t,c){var e=el('td',c); e.textContent=(t===null||t===undefined)?'':t; return e;}
tick(); setInterval(tick, 8000);
"""

_HISTORY_JS = _COMMON_JS + r"""
var counts={}, cur=new Date(); cur.setDate(1); var selDay=null;
getJSON('/api/status').then(function(s){ if(s) daemonsBar(s.daemons); });
function td(t,c){var e=document.createElement('td'); if(c)e.className=c;
  e.textContent=(t===null||t===undefined)?'':t; return e;}
async function loadCounts(){ counts=await getJSON('/api/history/days')||{}; draw(); }
function ym(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');}
function draw(){
  document.getElementById('mlabel').textContent=cur.toLocaleString('default',{month:'long',year:'numeric'});
  var cal=document.getElementById('cal'); cal.innerHTML='';
  ['S','M','T','W','T','F','S'].forEach(function(h){cal.appendChild(el('div','h',h));});
  var first=new Date(cur.getFullYear(),cur.getMonth(),1);
  var start=first.getDay(), dim=new Date(cur.getFullYear(),cur.getMonth()+1,0).getDate();
  // Task 346: max launch count over the visible month -> log-scaled heatmap buckets.
  var maxCount=0;
  for(var mc=1; mc<=dim; mc++){ var mk=cur.getFullYear()+'-'+String(cur.getMonth()+1).padStart(2,'0')+'-'+String(mc).padStart(2,'0');
    if((counts[mk]||0)>maxCount) maxCount=counts[mk]||0; }
  for(var i=0;i<start;i++) cal.appendChild(el('div','day empty'));
  for(var d=1; d<=dim; d++){
    var key=cur.getFullYear()+'-'+String(cur.getMonth()+1).padStart(2,'0')+'-'+String(d).padStart(2,'0');
    var n=counts[key]||0; var cell=el('div','day'+(n?' has':'')+(key===selDay?' sel':''));
    if(n){ var t=Math.log(n+1)/Math.log(maxCount+1);           // maxCount>=1 here, so log(maxCount+1)>0
           cell.classList.add('h'+(t>=0.75?4:t>=0.5?3:t>=0.25?2:1)); }
    cell.appendChild(el('div','n',String(d)));
    if(n) cell.appendChild(el('div','c',String(n)));
    (function(k){cell.onclick=function(){selDay=k; draw(); loadDay(k);};})(key);
    cal.appendChild(cell);
  }
}
function dayLegend(){
  var L=el('div','legend');
  [['bh','explicit link'],['bm','follow-up'],['bl','same subject'],['broot','root']].forEach(function(k){
    var s=el('span','k'); s.appendChild(el('span','cbar '+k[0]));
    s.appendChild(document.createTextNode(k[1])); L.appendChild(s);});
  var s2=el('span','k'); s2.appendChild(el('span','bchip ctx','context'));
  s2.appendChild(document.createTextNode(' other-day parent/child')); L.appendChild(s2);
  return L;
}
async function loadDay(day){
  var dv=document.getElementById('dayview'); dv.innerHTML='<div class="card muted">loading…</div>';
  var data=await getJSON('/api/history/day?date='+day); if(!data) return;
  var lin=await getJSON('/api/history/day_lineage?date='+day)||{trees:[],n_day_tasks:0};
  dv.innerHTML=''; var c=el('div','card');
  c.appendChild(el('h2',null,'Launched on '+day));
  // task lineage — mini-trees grouped by root (not a flat list)
  if(lin.trees && lin.trees.length){
    c.appendChild(el('div','muted small','TASK LINEAGE — '+lin.n_day_tasks+
      ' task'+(lin.n_day_tasks===1?'':'s')+' this day, shown in context · click a task for detail'));
    c.appendChild(dayLegend());
    var host=el('div','daytrees');
    lin.trees.forEach(function(t){
      var mt=el('div','mtree'); var ul=el('ul','tree-ul root');
      ul.appendChild(treeNodeLi(t)); mt.appendChild(ul); host.appendChild(mt);});
    c.appendChild(host);
  } else {
    c.appendChild(el('div','muted small','No tasks launched this day.'));
  }
  // jobs
  if(data.jobs.length){ c.appendChild(el('div','muted small','JOBS ('+data.jobs.length+')'));
    var jt=el('table'); jt.innerHTML='<tr><th>status</th><th>owner</th><th>type</th><th>command</th></tr>';
    data.jobs.forEach(function(j){var tr=el('tr'); tr.style.cursor='pointer';
      var st=el('td'); st.appendChild(badge(j.status)); if(j.timed_out) st.appendChild(el('span','badge b-failed',' TO'));
      tr.appendChild(st);
      tr.appendChild(td(j.owner_agent,'')); tr.appendChild(td(j.type,'small'));
      tr.appendChild(td((j.command||'').slice(0,80),'mono small'));
      tr.onclick=function(){showJob(j.job_id);}; jt.appendChild(tr);});
    c.appendChild(jt);}
  dv.appendChild(c);
}
async function showJob(id){
  var D=document.getElementById('detail'); D.innerHTML='<div class="card muted">loading…</div>';
  var j=await getJSON('/api/job?id='+encodeURIComponent(id)); if(!j){D.innerHTML='';return;}
  var c=el('div','card'); c.appendChild(el('h2',null,'Job '+j.job_id));
  var g=el('div','grid');
  [['owner',j.owner_agent],['type',j.type],['status',j.status],
   ['rc',j.rc],['timed_out',String(j.timed_out)],
   ['launched',fmtTs(j.launch_ts)],['duration', j.duration_s?(Math.round(j.duration_s)+'s'):'']].forEach(function(kv){
    var b=el('div','card small'); b.appendChild(el('div','muted',kv[0])); b.appendChild(el('div',null,String(kv[1]===undefined||kv[1]===null?'':kv[1]))); g.appendChild(b);});
  c.appendChild(g);
  c.appendChild(el('div','muted small','command')); var cc=el('code','cmd'); cc.textContent=j.command||''; c.appendChild(cc);
  // deliverables
  var dl=el('div'); dl.appendChild(el('div','muted small','deliverables'));
  var any=false;
  [['output',j.output_path],['stdout',j.out_log],['stderr',j.err_log]].forEach(function(p){
    if(p[1]){ any=true; var a=el('a',null,p[0]+': '+p[1]); a.href='/download?path='+encodeURIComponent(p[1]);
      a.style.display='block'; a.className='small'; dl.appendChild(a);} });
  if(!any) dl.appendChild(el('div','muted small','(none recorded)'));
  c.appendChild(dl); D.innerHTML=''; D.appendChild(c);
}
// showTask() is defined in _COMMON_JS (shared with the Lineage page).
document.getElementById('prev').onclick=function(){cur.setMonth(cur.getMonth()-1);draw();};
document.getElementById('next').onclick=function(){cur.setMonth(cur.getMonth()+1);draw();};
loadCounts();
"""


# --------------------------------------------------------------------------- #
# lineage forest page
# --------------------------------------------------------------------------- #
def lineage_page():
    body = (
        "<h2>Task Lineage</h2>"
        "<div id='summary' class='muted small'>loading&hellip;</div>"
        "<div class='legend'>"
        "<span class='k'><span class='cbar bh'></span>explicit link — high confidence</span>"
        "<span class='k'><span class='cbar bm'></span>follow-up phrasing — medium</span>"
        "<span class='k'><span class='cbar bl'></span>same subject — low (heuristic)</span>"
        "<span class='k'><span class='cbar broot'></span>root task (no parent)</span>"
        "</div>"
        "<div class='two'>"
        "<div><div id='forest'>loading&hellip;</div><div id='singletons'></div></div>"
        "<div id='detail'><div class='card'><div class='empty-state'>Select a task to "
        "see its email thread and reconstructed lineage.</div></div></div>"
        "</div>"
    )
    return _shell("Task Lineage", body, _LINEAGE_JS, active="lineage")


_LINEAGE_JS = _COMMON_JS + r"""
getJSON('/api/status').then(function(s){ if(s) daemonsBar(s.daemons); });
// tree rendering (treeNodeLi, DOT, BLABEL) is shared from _COMMON_JS.
async function loadForest(){
  var f; try{ f=await getJSON('/api/forest'); }catch(e){ return; } if(!f) return;
  document.getElementById('summary').textContent =
    f.n_trees+' trees link '+f.n_in_trees+' of '+f.n_tasks+' tasks · '+
    f.n_singletons+' standalone (no follow-ups)';
  var host=document.getElementById('forest'); host.innerHTML='';
  f.trees.forEach(function(t){
    var d=el('details','tree card'); d.open=true;
    var s=document.createElement('summary');
    s.appendChild(el('b',null,'root #'+t.task_id));
    s.appendChild(el('span','muted small','  '+(t.title||'')+'  ·  '+t.size+' tasks, depth '+t.depth));
    d.appendChild(s);
    var ul=el('ul','tree-ul root'); ul.appendChild(treeNodeLi(t)); d.appendChild(ul);
    host.appendChild(d);
  });
  var sd=el('details','singles card');
  var ss=document.createElement('summary');
  ss.appendChild(el('b',null, f.singletons.length+' standalone tasks'));
  ss.appendChild(el('span','muted small','  no reconstructed follow-ups — click to expand'));
  sd.appendChild(ss);
  var wrap=el('div','singlewrap');
  f.singletons.forEach(function(n){
    var a=el('a','schip','#'+n.task_id); a.title=n.title||''; a.href='javascript:void(0)';
    a.onclick=(function(id){return function(){showTask(id);};})(n.task_id);
    wrap.appendChild(a);
  });
  sd.appendChild(wrap);
  var S=document.getElementById('singletons'); S.innerHTML=''; S.appendChild(sd);
}
loadForest();
"""


# --------------------------------------------------------------------------- #
# cowork assignment page (Task 353)
# --------------------------------------------------------------------------- #
def cowork_page():
    """Assignment form: pick a LEAD + collaborators (via an agent-search popup that
    finds agents by name OR by a task they did), optionally add a critic, describe the
    task, submit. The form is a thin shell; all dynamic values go in via textContent
    (XSS-safe). Submitting POSTs JSON to /api/cowork (wired in B2)."""
    body = (
        "<h2>New Cowork</h2>"
        "<div class='muted small' style='margin-bottom:14px;max-width:680px'>"
        "A cowork is a group of previously-active agents working together on one task. "
        "One agent <b>leads</b> (owns the deliverables, plans, delegates, reviews and "
        "iterates); <b>collaborators</b> carry the knowledge of their prior tasks; an "
        "optional <b>critic</b> audits the outputs independently. All communication is by "
        "email and every agent is watchdog-monitored, exactly like regular work.</div>"
        "<div class='card cowork-form'>"
        "<div class='fld'><span class='lbl'>Lead agent</span>"
        "<div class='pickrow'><span id='leadslot' class='muted small'>none selected</span>"
        "<button type='button' id='lead-search-btn' class='small'>Search&hellip;</button></div></div>"
        "<div class='fld'><span class='lbl'>Collaborating agents</span>"
        "<div id='collabchips' class='chips'></div>"
        "<div><button type='button' id='collab-add-btn' class='small'>Add collaborator&hellip;</button></div></div>"
        "<label class='fld chk'><input type='checkbox' id='critic'>"
        "<span>Add an independent critic agent</span></label>"
        "<div class='fld'><span class='lbl'>Task description</span>"
        "<textarea id='desc' rows='6' placeholder='What should this cowork accomplish? "
        "Be specific about the deliverables.'></textarea></div>"
        "<div class='actions'><button type='button' id='submitcw'>Create cowork</button>"
        "<span id='cwstatus' class='small muted'></span></div>"
        "</div>"
        # agent-search modal (hidden until opened)
        "<div id='pickmodal' class='modal' style='display:none'>"
        "<div class='modal-card'>"
        "<div class='modal-head'><b id='picktitle'>Find an agent</b>"
        "<button type='button' id='pickclose' class='small'>&#10005;</button></div>"
        "<input id='pickq' type='text' autocomplete='off' "
        "placeholder='Search by agent name or a task it did&hellip;'>"
        "<div id='pickresults' class='pickresults'></div>"
        "</div></div>"
    )
    return _shell("New Cowork", body, _COWORK_JS, active="cowork")


_COWORK_JS = _COMMON_JS + r"""
getJSON('/api/status').then(function(s){ if(s) daemonsBar(s.daemons); });
var CW={ lead:null, collabs:[], mode:null, timer:null };
function renderLead(){
  var slot=document.getElementById('leadslot'); slot.innerHTML='';
  if(!CW.lead){ slot.className='muted small'; slot.textContent='none selected'; return; }
  slot.className='';
  var c=el('span','cw-lead'); c.appendChild(el('b',null,CW.lead));
  var x=el('span','x','✕'); x.title='clear lead';
  x.onclick=function(){ CW.lead=null; renderLead(); };
  c.appendChild(x); slot.appendChild(c);
}
function renderCollabs(){
  var host=document.getElementById('collabchips'); host.innerHTML='';
  if(!CW.collabs.length){ host.appendChild(el('span','muted small','none added')); return; }
  CW.collabs.forEach(function(name){
    var c=el('span','cw-chip'); c.appendChild(el('b',null,name));
    var x=el('span','x','✕'); x.title='remove';
    x.onclick=function(){ CW.collabs=CW.collabs.filter(function(n){return n!==name;}); renderCollabs(); };
    c.appendChild(x); host.appendChild(c);
  });
}
function openPick(mode){
  CW.mode=mode;
  document.getElementById('picktitle').textContent =
    mode==='lead' ? 'Select the lead agent' : 'Add a collaborating agent';
  document.getElementById('pickmodal').style.display='flex';
  var q=document.getElementById('pickq'); q.value=''; q.focus();
  runSearch('');
}
function closePick(){ document.getElementById('pickmodal').style.display='none'; CW.mode=null; }
function pick(name){
  if(CW.mode==='lead'){ CW.lead=name; CW.collabs=CW.collabs.filter(function(n){return n!==name;}); renderLead(); }
  else { if(name!==CW.lead && CW.collabs.indexOf(name)<0) CW.collabs.push(name); renderCollabs(); }
  closePick();
}
async function runSearch(q){
  var box=document.getElementById('pickresults'); box.innerHTML='';
  var rows; try{ rows=await getJSON('/api/agents?q='+encodeURIComponent(q)); }catch(e){ return; }
  if(!rows || !rows.length){ box.appendChild(el('div','muted small','No agents match.')); return; }
  rows.forEach(function(r){
    var it=el('div','pickrow-item');
    var head=el('div'); head.appendChild(el('span','an',r.agent));
    if(r.n_tasks) head.appendChild(el('span','at','  ·  '+r.n_tasks+' task'+(r.n_tasks===1?'':'s')));
    it.appendChild(head);
    if(r.blurb) it.appendChild(el('div','at',r.blurb));
    if(r.tasks && r.tasks.length){ var tk=el('div');
      r.tasks.slice(0,6).forEach(function(t){ tk.appendChild(el('span','tk','#'+t.id)); });
      it.appendChild(tk); }
    it.onclick=(function(n){ return function(){ pick(n); }; })(r.agent);
    box.appendChild(it);
  });
}
async function submitCowork(){
  var st=document.getElementById('cwstatus');
  var desc=document.getElementById('desc').value.trim();
  if(!CW.lead){ st.className='small flag'; st.textContent='Pick a lead agent.'; return; }
  if(!CW.collabs.length){ st.className='small flag'; st.textContent='Add at least one collaborator.'; return; }
  if(!desc){ st.className='small flag'; st.textContent='Add a task description.'; return; }
  st.className='small muted'; st.textContent='Submitting…';
  var payload={ lead:CW.lead, collaborators:CW.collabs,
    critic:document.getElementById('critic').checked, description:desc };
  try{
    var r=await fetch('/api/cowork',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if(r.status===401){ location='/login'; return; }
    var d={}; try{ d=await r.json(); }catch(e){}
    if(r.ok && d.ok){
      st.className='small'; st.textContent='Cowork '+(d.cowork_id||'')+' created — '+
        (d.note||'the inbox monitor will spawn the agents.');
      CW.lead=null; CW.collabs=[]; document.getElementById('desc').value='';
      document.getElementById('critic').checked=false; renderLead(); renderCollabs();
    } else if(r.status===404){
      st.className='small muted';
      st.textContent='Form valid. Backend POST /api/cowork is not wired yet (pending sign-off).';
    } else {
      st.className='small flag'; st.textContent='Error: '+(d.error||('HTTP '+r.status));
    }
  }catch(e){ st.className='small flag'; st.textContent='Network error.'; }
}
document.getElementById('lead-search-btn').onclick=function(){ openPick('lead'); };
document.getElementById('collab-add-btn').onclick=function(){ openPick('collab'); };
document.getElementById('pickclose').onclick=closePick;
document.getElementById('submitcw').onclick=submitCowork;
document.getElementById('pickmodal').onclick=function(e){ if(e.target===this) closePick(); };
document.getElementById('pickq').addEventListener('input', function(){
  clearTimeout(CW.timer); var v=this.value; CW.timer=setTimeout(function(){ runSearch(v); }, 180); });
document.addEventListener('keydown', function(e){ if(e.key==='Escape') closePick(); });
renderLead(); renderCollabs();
"""
