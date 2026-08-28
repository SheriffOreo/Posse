"""HTML for the dashboard. Login is server-rendered (escaped); the status/history
pages are thin shells filled by fetch() from the JSON APIs (all dynamic values go
in via textContent on the client, so they are XSS-safe)."""
import base64
import html
import json
import os
import urllib.parse

import auth
import config
import models
import state

# Self-hosted Rye (Case 403): the dashboard CSP blocks external stylesheets/fonts
# (style-src 'self'), so the Google-Fonts @import silently failed in the browser and
# the wood-type headers fell back to serif. Embed the woff2 as a data: URL @font-face
# (CSP allows font-src data:) so Rye loads in every browser, offline, no external dep.
try:
    _RYE_B64 = base64.b64encode(
        open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rye-latin.woff2"), "rb").read()
    ).decode()
    _RYE_FACE = ("@font-face{font-family:'Rye';font-style:normal;font-weight:400;font-display:swap;"
                 "src:url(data:font/woff2;base64," + _RYE_B64 + ") format('woff2');}\n")
except Exception:
    _RYE_FACE = ""  # graceful: headers fall back to the serif stack if the file is missing

_CSS = """
/* ---- Posse theme palette (CSS custom properties) -------------------------- #
   NIGHT (default, :root) = "Modern Sheriff" — dark navy + gold, wood-type (Rye)
   section headers, badge-pill status. DAY (.theme-day) = "Wanted Poster" — light
   kraft parchment + brown ink. Toggled by the nav day/night button (persisted in
   localStorage). Status/GPU accent hues (green/red/purple) stay literal so status
   remains semantically colour-coded on BOTH grounds. (Case 398: A + C on the toggle.) */
:root{
  --bg:#101623; --fg:#e6ebf4; --link:#6cb6ff;
  --header-bg:#1a2233;
  --border:#2b3648; --border-soft:#222c3d; --cal-border:#24304a; --code-border:#22304a;
  --card:#1b2333; --surface:#151d2b; --surface2:#0e1420;
  --row-hover:#212b3d; --has-bg:#16243a; --hover-border:#3d68a8;
  --chip-bg:#212b3d; --chip2-bg:#1b2740; --ctx-bg:#1b2233;
  --muted:#8093b0; --muted2:#7f8ba3; --th:#8fa3c7; --h2:#e8bb44;
  --tlink:#cdd7ea; --code-fg:#b9c7e0; --empty:#7c8698; --dot:#5a6b8c; --tree-line:#33415f;
  --accent-border:#2f5488; --btn-bg:#20406b; --btn-fg:#dbe7ff; --btn-hover:#295084;
  --amber:#e8bb44; --onday:#f6d873; --gold:#e8bb44;
  --heat1:#17283f; --heat2:#1e3a5c; --heat3:#28527e; --heat4:#3466a0;
}
.theme-day{
  --bg:#e3d4b0; --fg:#3a2a17; --link:#8a5a1e;
  --header-bg:#dccba3;
  --border:#b39b6e; --border-soft:#cbb890; --cal-border:#c2ad7e; --code-border:#c2ad7e;
  --card:#f0e6cd; --surface:#f4ecd6; --surface2:#eaddbf;
  --row-hover:#ece0bf; --has-bg:#e8dcbb; --hover-border:#a9713a;
  --chip-bg:#e6d8b6; --chip2-bg:#eadfbf; --ctx-bg:#efe4c8;
  --muted:#6f5836; --muted2:#7a6640; --th:#5b452a; --h2:#3a2a17;
  --tlink:#4a3a24; --code-fg:#4a3a24; --empty:#8a7550; --dot:#a99a78; --tree-line:#c2ad7e;
  --accent-border:#b39b6e; --btn-bg:#20293b; --btn-fg:#f4e7cd; --btn-hover:#2c3a52;
  --amber:#9a6f12; --onday:#8a6d0f; --gold:#b3841f;
  --heat1:#efe3c6; --heat2:#ddc99a; --heat3:#cbb073; --heat4:#b3924a;
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
/* Case 421: a lineage node is a full-height accent bar + a wrapping title block +
   a meta-chip row. The OLD single non-wrapping flex row collapsed a long title to
   its one-word min-content width (the History "broken for long follow-ups" bug). */
.tnode { display:flex; align-items:stretch; gap:9px; padding:4px 6px; border-radius:6px; }
.tnode:hover { background:var(--row-hover); }
.tnode > .cbar { align-self:stretch; height:auto; width:4px; border-radius:2px; }
.tmain { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; gap:3px; }
.tmain > a.tlink { overflow-wrap:anywhere; line-height:1.4; }
.tmeta { display:flex; flex-wrap:wrap; align-items:center; gap:4px 8px; }
a.tlink { color:var(--tlink); } a.tlink:hover { color:var(--link); }
.bchip { font-size:11px; padding:1px 6px; border-radius:9px; background:var(--chip-bg); color:var(--th); white-space:nowrap; }
.wchip { font-size:11px; color:var(--muted2); white-space:nowrap; font-family:ui-monospace,monospace; }
.singlewrap { display:flex; flex-wrap:wrap; gap:5px; margin-top:8px; }
a.schip { font-size:12px; padding:1px 7px; border-radius:5px; background:var(--chip2-bg); color:var(--h2); font-family:ui-monospace,monospace; }
a.schip:hover { background:var(--btn-bg); color:var(--btn-fg); text-decoration:none; }
.empty-state { text-align:center; color:var(--empty); padding:34px 16px; font-style:italic; }
/* ---- day-lineage mini-trees (History) ---- */
.daytrees { margin-top:4px; }
.daytrees .mtree { padding:8px 0; }
.daytrees .mtree + .mtree { border-top:1px solid var(--border-soft); }
.tnode.onday a.tlink { color:var(--onday); font-weight:700; }
.tnode.offday { opacity:.6; }
.bchip.ctx { background:var(--ctx-bg); color:var(--muted2); font-style:italic; }
/* Case 447: precinct chip on a node + per-precinct group section in the day view */
.pchip { font-size:11px; padding:1px 7px; border-radius:9px; background:var(--chip-bg);
  color:var(--link); border:1px solid var(--border-soft); white-space:nowrap; font-weight:600; }
.pgroup { margin:10px 0 4px; }
.pgroup + .pgroup { border-top:1px solid var(--border); margin-top:16px; padding-top:6px; }
.pghead { display:flex; align-items:baseline; gap:10px; margin:2px 0 2px; }
.pgname { font-weight:800; font-size:14px; color:var(--link); text-transform:capitalize;
  letter-spacing:.02em; }
.pgcount { font-size:12px; color:var(--muted2); }
/* Case 421: History layout — compact calendar band on top, then a WIDE day-view
   (lineage chains) beside the sticky detail panel (the calendar used to share the
   narrow 540px column with the chains, crushing long follow-ups). Scoped to
   `.two.hist` so the Lineage page's `.two` is untouched. */
.histcal { max-width:600px; margin:0 auto 14px; }
.histcal .calnav { display:flex; align-items:center; justify-content:center; gap:14px; margin-bottom:8px; }
.histcal .calnav b { min-width:150px; text-align:center; }
.two.hist { grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr); }
@media(max-width:1000px){ .two.hist { grid-template-columns:1fr; } }
/* ---- full conversation body + inline attachments (Task detail) ---- */
.msgbody { white-space:pre-wrap; word-break:break-word; max-height:360px; overflow:auto;
           background:var(--surface2); border:1px solid var(--code-border); border-radius:5px;
           padding:7px 9px; margin-top:5px; font-size:13px; }
.atts { margin-top:8px; display:flex; flex-direction:column; gap:7px; }
img.attimg { max-width:100%; height:auto; border:1px solid var(--border); border-radius:6px; background:var(--surface2); }
a.attfile { display:inline-block; }
/* ---- initial / final message tags (Case 427) ---- */
.kindtag { display:inline-block; font-size:10px; font-weight:700; letter-spacing:.04em;
  padding:1px 6px; border-radius:9px; margin-right:7px; vertical-align:1px; text-transform:uppercase; }
.kindtag.initial { background:var(--accent-border); color:var(--fg); }
.kindtag.final { background:var(--good, #2f855a); color:#fff; }
/* ---- deliverables section (Task detail) ---- */
.deliv { margin-top:12px; padding-top:8px; border-top:1px solid var(--border); }
.deliv .job { margin:5px 0; display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.deliv a { color:var(--link); }
/* ---- Case-log per-row view actions: conversation / deliverables / case file (Case 440) ---- */
.viewacts { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.viewacts a { font-size:12px; line-height:1.4; padding:3px 8px; border-radius:6px;
  border:1px solid var(--accent-border); background:var(--surface2); color:var(--link);
  white-space:nowrap; cursor:pointer; text-decoration:none; }
.viewacts a:hover { border-color:var(--link); }
/* ---- Case-log detail MODAL: floats the popup on top of everything (Case 440, Feng uid=469) ---- */
.pcmodal[hidden] { display:none; }
.pcmodal { position:fixed; inset:0; z-index:9999; display:flex; align-items:flex-start;
  justify-content:center; padding:4vh 16px; }
.pcmodal-backdrop { position:absolute; inset:0; background:rgba(2,6,15,.66); }
.pcmodal-dialog { position:relative; z-index:1; width:min(920px,96vw); max-height:90vh;
  overflow:auto; border-radius:12px; box-shadow:0 20px 60px rgba(0,0,0,.55); }
.pcmodal-bar { position:sticky; top:0; z-index:6; display:flex; justify-content:flex-end;
  padding:8px 8px 0; pointer-events:none; }         /* transparent bar; only the button is clickable */
.pcmodal-bar .pcmodal-x { pointer-events:auto; }
.pcmodal-dialog .card { margin:0; }                 /* the popup card fills the dialog */
.pcmodal-dialog .card > button:first-child { display:none; }  /* card's own ✕ replaced by the sticky bar */
body.pcmodal-open { overflow:hidden; }              /* lock background scroll while open */
/* ---- create-case attachments: drop zone + previews (Case 421) ---- */
.dropzone { border:1.5px dashed var(--accent-border); border-radius:8px; padding:16px 14px;
  text-align:center; color:var(--muted); cursor:pointer; background:var(--surface2); margin-top:5px;
  transition:border-color .12s, background .12s, color .12s; }
.dropzone:hover, .dropzone.drag { border-color:var(--hover-border); background:var(--row-hover); color:var(--fg); }
.dropzone.drag { border-style:solid; }
.dropzone .dz-browse { color:var(--link); text-decoration:underline; }
.attprev { display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; }
.attprev:empty { display:none; }
.att-item { position:relative; width:104px; padding:6px; border:1px solid var(--border);
  border-radius:8px; background:var(--card); }
.att-item img { width:92px; height:68px; object-fit:cover; border-radius:5px; display:block; background:var(--surface2); }
.att-item .fileicon { width:92px; height:68px; border-radius:5px; display:flex; align-items:center;
  justify-content:center; font-weight:700; font-size:15px; letter-spacing:.5px; color:var(--th);
  background:var(--surface2); border:1px solid var(--border-soft); }
.att-item .att-name { font-size:10px; margin-top:4px; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; color:var(--fg); }
.att-item .att-size { font-size:10px; color:var(--muted); }
.att-item .att-x { position:absolute; top:-8px; right:-8px; width:22px; height:22px; padding:0;
  border-radius:50%; border:1px solid var(--border); background:var(--card); color:var(--fg);
  font-size:14px; line-height:1; cursor:pointer; display:flex; align-items:center; justify-content:center; }
.att-item .att-x:hover { background:var(--btn-bg); color:var(--btn-fg); border-color:var(--hover-border); }
/* Task 346: login / register brand lockup (mark + wordmark, centered above the form) */
.brandmark { display:flex; flex-direction:column; align-items:center; gap:6px; margin-bottom:14px; }
.brandmark span { font-weight:700; letter-spacing:.3px; font-size:16px; color:var(--fg); }
/* ---- JTF (Joint Task Force) assignment form + agent/precinct-search modal (Task 353; Case 384e) ---- */
.jtf-form { max-width:680px; display:flex; flex-direction:column; gap:16px; }
.jtf-form .fld { display:flex; flex-direction:column; gap:6px; }
.jtf-form .fld.chk { flex-direction:row; align-items:center; gap:9px; cursor:pointer; }
.jtf-form .lbl { font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:var(--th); font-weight:600; }
.jtf-form textarea, .jtf-form input[type=text], .modal-card input[type=text] {
  width:100%; padding:9px; border-radius:6px; border:1px solid var(--accent-border);
  background:var(--surface2); color:var(--fg); font:inherit; }
.jtf-form textarea { resize:vertical; min-height:96px; }
.jtf-form .pickrow { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.jtf-form .chips { display:flex; flex-wrap:wrap; gap:6px; }
.jtf-chip, .jtf-lead { display:inline-flex; align-items:center; gap:7px; font-family:ui-monospace,monospace;
  border-radius:12px; padding:2px 9px; }
.jtf-chip { font-size:12px; background:var(--chip-bg); color:var(--h2); }
.jtf-lead { font-size:13px; background:var(--has-bg); color:var(--fg); border:1px solid var(--accent-border); }
.jtf-chip .x, .jtf-lead .x { cursor:pointer; color:var(--muted); font-weight:700; }
.jtf-chip .x:hover, .jtf-lead .x:hover { color:var(--link); }
/* the "precinct" vs "specific deputy" kind badge on a filled slot */
.jtf-chip .k, .jtf-lead .k { font-size:9px; text-transform:uppercase; letter-spacing:.4px;
  padding:1px 5px; border-radius:7px; background:var(--chip-bg); color:var(--th); font-weight:700; }
.jtf-lead .k { background:var(--surface2); }
/* the precinct-options band at the top of the picker modal */
.pickband { margin-bottom:6px; }
.picklbl { font-size:10px; text-transform:uppercase; letter-spacing:.5px; color:var(--th);
  font-weight:600; margin:2px 0 5px; display:block; }
.pchip { display:inline-block; font-family:ui-monospace,monospace; font-size:12px; cursor:pointer;
  border:1px solid var(--accent-border); background:var(--surface2); color:var(--fg);
  border-radius:10px; padding:2px 9px; margin:0 5px 5px 0; }
.pchip:hover { border-color:var(--hover-border); background:var(--row-hover); }
.jtf-form .actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
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
/* ---- Posse western component styling (Case 398; themes via the vars above) ---- */
.brand .wm, .brandmark .wm{ font-family:'Rye',Georgia,serif; color:var(--gold); letter-spacing:.5px; line-height:1; }
.brand .wm{ font-size:28px; } .brandmark .wm{ font-size:34px; }
h2{ font-family:'Rye',Georgia,serif; text-transform:none; color:var(--gold); font-size:19px;
    letter-spacing:.4px; font-weight:400; }
h2::before{ content:'★  '; color:var(--gold); font-size:.78em; }
.badge{ border-radius:20px; padding:2px 10px; }
/* status-hue TEXT labels (mode / daemon up-down): darken for day-mode parchment contrast */
.c-ok{ color:#3fb950; } .c-warn{ color:#e3b341; } .c-bad{ color:#f85149; }
.theme-day .c-ok{ color:#2a7d42; } .theme-day .c-warn{ color:#8a6410; } .theme-day .c-bad{ color:#b3261e; }
/* sheriff block (precincts page): logo + "Sheriff" title, two BIGGER boxes */
.smgr-head{ display:flex; align-items:center; gap:15px; margin-bottom:16px; }
.smgr-head h2{ font-size:31px; border:none; margin:0; padding:0; line-height:1.05; }
.smgr-head h2::before{ content:none; }
.smgr-grid{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }
@media(max-width:820px){ .smgr-grid{ grid-template-columns:1fr; } }
.smgr-grid > .card{ padding:18px 20px; margin-bottom:0; }
.smgr-grid h3{ font-family:'Rye',Georgia,serif; font-weight:400; font-size:18px; color:var(--gold); }
"""
_CSS = _RYE_FACE + _CSS   # prepend the self-hosted @font-face (Case 403)

# Posse mark — Oreo the sheriff (gold star badge + tabby cat in a cowboy hat) as a
# FLAT-fill inline SVG: no <defs>/gradients/ids, so it never collides when it appears
# more than once on a page and reads on any background (nav + login + sheriff block,
# both themes). CSP-safe (inline, not an <img src>). Source: reports/task390/make_mark.py.
_OREO_INNER = (
    "<path d='M 256.0,38.0 331.0,132.1 450.0,150.0 406.0,262.0 450.0,374.0 331.0,391.9 256.0,486.0 181.0,391.9 62.0,374.0 106.0,262.0 62.0,150.0 181.0,132.1 Z' fill='#e8bb44' stroke='#8a6314' stroke-width='6' stroke-linejoin='round'/>"
    "<circle cx='256.0' cy='38.0' r='11' fill='#e8bb44' stroke='#8a6314' stroke-width='6'/><circle cx='450.0' cy='150.0' r='11' fill='#e8bb44' stroke='#8a6314' stroke-width='6'/><circle cx='450.0' cy='374.0' r='11' fill='#e8bb44' stroke='#8a6314' stroke-width='6'/><circle cx='256.0' cy='486.0' r='11' fill='#e8bb44' stroke='#8a6314' stroke-width='6'/><circle cx='62.0' cy='374.0' r='11' fill='#e8bb44' stroke='#8a6314' stroke-width='6'/><circle cx='62.0' cy='150.0' r='11' fill='#e8bb44' stroke='#8a6314' stroke-width='6'/>"
    "<circle cx='256' cy='262' r='122' fill='#20293b' stroke='#b3841f' stroke-width='5'/>"
    "<path d='M 204,246 L 172,180 L 160,240 Z' fill='#b8a488' stroke='#6f5c44' stroke-width='4' stroke-linejoin='round'/><path d='M 194,234 L 179,200 L 172,230 Z' fill='#e78ea1'/><path d='M 308,246 L 340,180 L 352,240 Z' fill='#b8a488' stroke='#6f5c44' stroke-width='4' stroke-linejoin='round'/><path d='M 318,234 L 333,200 L 340,230 Z' fill='#e78ea1'/>"
    "<ellipse cx='256' cy='282' rx='92' ry='86' fill='#b8a488' stroke='#6f5c44' stroke-width='4'/>"
    "<path d='M 232,230 L 240,196 L 256,222 L 272,196 L 280,230' fill='none' stroke='#6f5c44' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/>"
    "<path d='M 148,234 Q 256,256 364,234 Q 256,218 148,234 Z' fill='#d99f5c' stroke='#a9713a' stroke-width='3' stroke-linejoin='round'/><path d='M 208,228 L 218,166 Q 256,150 294,166 L 304,228 Z' fill='#d99f5c' stroke='#a9713a' stroke-width='3' stroke-linejoin='round'/><path d='M 207,222 Q 256,232 305,222 L 305,210 Q 256,220 207,210 Z' fill='#38538a'/>"
    "<path d='M 256.0,207.0 258.4,212.8 264.6,213.2 259.8,217.2 261.3,223.3 256.0,220.0 250.7,223.3 252.2,217.2 247.4,213.2 253.6,212.8 Z' fill='#e8bb44'/>"
    "<ellipse cx='222' cy='274' rx='23' ry='25' fill='#f2a91e' stroke='#cf8410' stroke-width='3'/><ellipse cx='222' cy='276' rx='9' ry='15' fill='#2b2118'/><circle cx='216' cy='265' r='5.5' fill='#ffffff'/><ellipse cx='290' cy='274' rx='23' ry='25' fill='#f2a91e' stroke='#cf8410' stroke-width='3'/><ellipse cx='290' cy='276' rx='9' ry='15' fill='#2b2118'/><circle cx='296' cy='265' r='5.5' fill='#ffffff'/>"
    "<path d='M 244,300 L 268,300 L 256,312 Z' fill='#e78ea1' stroke='#c96b80' stroke-width='2'/>"
    "<path d='M 256,312 L 256,321 M 256,321 q -10,8 -18,1 M 256,321 q 10,8 18,1' fill='none' stroke='#6f5c44' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/>"
    "<path d='M 196,348 Q 256,358 316,348 L 299,378 Q 256,392 213,378 Z' fill='#38538a' stroke='#26386a' stroke-width='3' stroke-linejoin='round'/>"
)

def _mark(px):
    """The Posse mark (Oreo the sheriff) — see _OREO_INNER."""
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512' width='{px}' height='{px}' "
        "role='img' aria-label='Posse' style='vertical-align:middle;flex:none'>"
        f"{_OREO_INNER}</svg>"
    )


def _nav(active=""):
    """Header nav. `active` in {status,history,precincts,judges,lineage,jtf} marks the
    current link with .cur so you can see which page you're on (Task 346;
    Cowork->JTF Case 384e; Judges Case 551)."""
    def cur(name):
        return " class='cur'" if name == active else ""
    acct = auth.account().get("email")
    who = (f"<span class='small muted' title='Signed-in account' "
           f"style='margin-right:12px'>{html.escape(acct)}</span>") if acct else ""
    return (
        "<header>"
        f"<span class='brand'>{_mark(44)}<span class='wm'>Posse</span></span>"
        f"<nav><a href='/'{cur('status')}>Status</a>"
        f"<a href='/history'{cur('history')}>History</a>"
        f"<a href='/precincts'{cur('precincts')}>Precincts</a>"
        f"<a href='/judges'{cur('judges')}>Judge</a>"
        f"<a href='/lineage'{cur('lineage')}>Lineage</a>"
        f"<a href='/jtf'{cur('jtf')}>JTF</a></nav>"
        "<span id='daemons' class='small muted'></span>"
        "<span class='spacer'></span>"
        f"{who}"
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
    acct = auth.account().get("email")
    who = (f"<p class='muted small' style='margin-top:-4px'>Account: "
           f"{html.escape(acct)}</p>") if acct else ""
    body = (
        "<div style='max-width:340px;margin:12vh auto;' class='card'>"
        f"<div class='brandmark'>{_mark(88)}<span class='wm'>Posse</span></div>"
        "<h2 style='border:none;margin-top:0'>Sign in</h2>"
        f"{who}{err}"
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


def register_page(token=None, error="", closed=False, email=None, name=None):
    """One-time registration form (bootstraps the account password). Server-rendered
    and escaped; standalone shell (no nav) like the login page. When the operator
    bound an identity to the link, the account email is shown read-only (it is the
    username) and the name is pre-filled but editable."""
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
        # The account username (email) is fixed by the link the operator sent; show
        # it read-only so the person registering sees which account they are setting
        # up. Older links carry no email — the field is simply omitted then.
        email_row = ""
        if email:
            email_row = (
                "<label class='muted small' style='display:block;margin-bottom:4px'>Account (username)</label>"
                f"<input type='text' value='{html.escape(email)}' readonly "
                f"style='{field};opacity:.75;cursor:not-allowed'>"
            )
        name_row = (
            f"<input type='text' name='name' placeholder='Your name (optional)' "
            f"value='{html.escape(name or '')}' style='{field}'>"
        )
        inner = (
            err + email_row +
            "<p class='muted small'>Choose a password for this dashboard. This is a "
            "one-time link &mdash; it works once, then expires.</p>"
            "<form method='post' action='/register'>"
            f"<input type='hidden' name='token' value='{safe_tok}'>"
            f"{name_row}"
            f"<input type='password' name='password' placeholder='New password (min 8)' "
            f"autofocus style='{field}'>"
            f"<input type='password' name='password2' placeholder='Confirm password' "
            f"style='{field}'>"
            "<button type='submit' style='width:100%'>Set password &amp; continue</button>"
            "</form>"
        )
    body = (
        "<div style='max-width:360px;margin:11vh auto;' class='card'>"
        f"<div class='brandmark'>{_mark(88)}<span class='wm'>Posse</span></div>"
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
        "<h2>Active Deputies <span id='wc' class='muted small'></span></h2>"
        # Task 384c / Phase A1: frame this roster as the Sheriff's deputy-supervision
        # loop (the watchdog). The unified "Sheriff = system manager" panel (deputies +
        # precincts) lives on the Precincts page; this is the per-deputy detail of it.
        "<p class='muted small' style='margin:-4px 0 8px'>The Sheriff "
        "(<a href='/precincts'>system manager</a>) supervises these deputies via the "
        "watchdog &mdash; crash / usage-limit recovery + relaunch. Zero-API.</p>"
        "<div id='workers' class='tablewrap'>loading&hellip;</div>"  # Task 377: leads with deputy/case#/precinct; scrolls at narrow width
        "<div id='detail'></div>"  # Task 327: task detail when a worker's case# is clicked
        # Task 377 #3 / Feng uid=380: on-demand per-GPU DEVICE stats behind a Refresh
        # button, placed ABOVE the GPU tasks section. The ONLY trigger for
        # /api/gpu_stats — never part of the 8s auto-poll.
        "<h2>GPU Devices "
        "<button id='gpurefresh' class='small' style='margin-left:8px'>&#8635; Refresh</button> "
        "<span id='gpuspin' class='muted small' style='display:none'>&#9696; querying nvidia-smi&hellip;</span> "
        "<span id='gpustatus' class='muted small'></span></h2>"
        "<div id='gpustats' class='tablewrap'>"
        "<div class='card muted small'>Click <b>Refresh</b> to query <span class='mono'>nvidia-smi</span> "
        "on the dashboard host (per-GPU utilization, memory, temperature). On demand only &mdash; not auto-polled.</div>"
        "</div>"
        "<h2>GPU Tasks</h2><div id='gpu'>loading&hellip;</div>"
        "<h2>Job Manager &mdash; Active Jobs</h2><div id='jobs' class='tablewrap'>loading&hellip;</div>"
        "<p class='muted small' id='updated'></p>"
    )
    return _shell("Infra Status", body, _STATUS_JS, active="status")


def history_page():
    # Case 421: compact calendar band on top, then a WIDE day-view (lineage chains)
    # beside the sticky detail panel. Previously the calendar shared the narrow left
    # column with the chains, which crushed long follow-up chains.
    body = (
        "<h2>Job History</h2>"
        "<div class='card histcal'>"
        "<div class='calnav'>"
        "<button id='prev'>&#8592;</button><b id='mlabel'></b><button id='next'>&#8594;</button></div>"
        "<div id='cal' class='cal'></div>"
        "<div class='calkey'>fewer<i class='k1'></i><i class='k2'></i>"
        "<i class='k3'></i><i class='k4'></i>more</div>"
        "<p class='muted small' style='text-align:center;margin:6px 0 0'>"
        "Click a day to list what was launched.</p></div>"
        "<div class='two hist'>"
        "<div id='dayview'><div class='card muted'>Pick a day above to see its cases.</div></div>"
        "<div id='detail'><div class='card muted'>Select a job or case to see details.</div></div>"
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
  // Case 421: full-height accent bar + a wrapping title block + a meta-chip row, so
  // a long case title wraps naturally and fills the width instead of collapsing to
  // one word (the old flat flex row squeezed it against the nowrap chips).
  var li=el('li');
  var row=el('div','tnode'+(n.on_day===true?' onday':(n.on_day===false?' offday':'')));
  row.appendChild(el('span','cbar '+(DOT[n.basis]||'broot')));
  var main=el('div','tmain');
  // Case 471: the DESCRIPTION (n.desc) is a one-sentence summary of what the case
  // REQUESTED (not the case-log "what was done" summary); fall back to the title.
  var desc = n.desc || n.title || '';
  var a=el('a','tlink','#'+n.task_id+'  '+desc);
  a.href='javascript:void(0)'; a.title=desc;
  a.onclick=(function(id){return function(){showTask(id);};})(n.task_id);
  main.appendChild(a);
  var meta=el('div','tmeta');
  // Case 447: precinct chip — suppressed (showprec===false) under a precinct group
  // header that already names it; shown for a cross-precinct child or on the Lineage page.
  if(n.precinct && n.showprec!==false) meta.appendChild(el('span','pchip',n.precinct));
  if(n.basis) meta.appendChild(el('span','bchip',BLABEL[n.basis]||n.basis));
  if(n.agent) meta.appendChild(el('span','wchip','· '+n.agent));  // Task 327: owning worker
  if(n.on_day===false) meta.appendChild(el('span','bchip ctx','context'));
  if(meta.childNodes.length) main.appendChild(meta);
  row.appendChild(main);
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
async function showTask(id, focus){
  // Case 440: an optional focus ('conversation' | 'deliverables') renders JUST that
  // section — used by the precinct Case-log buttons. No focus = the full view
  // (lineage + conversation + deliverables), so History/Lineage are unchanged.
  var D=document.getElementById('detail'); if(!D) return;
  D.innerHTML='<div class="card muted">loading&hellip;</div>';
  var t=await getJSON('/api/task?id='+id); if(!t){D.innerHTML='';return;}
  var c=el('div','card');
  // Task 391 #2: a close (X) box on the detail pop-up so it can be dismissed.
  var xb=el('button','small','✕ close'); xb.title='close'; xb.style.cssText='float:right;margin-left:8px';
  xb.onclick=function(){ D.innerHTML=''; if(window.pcClose) pcClose(); }; c.appendChild(xb);
  c.appendChild(el('h2',null,'Case #'+id+(t.conversation.agent?(' · '+t.conversation.agent):'')
    +(focus==='conversation'?' · conversation':(focus==='deliverables'?' · deliverables':''))));
  if(t.conversation.subject) c.appendChild(el('div',null,t.conversation.subject));
  if(!focus && t.lineage){ var lin=el('div','card'); lin.appendChild(el('div','muted small','LINEAGE'));
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
  if(!focus || focus==='conversation'){
  var conv=el('div','thread'); conv.appendChild(el('div','muted small','CONVERSATION'));
  if(!t.conversation.messages.length) conv.appendChild(el('div','muted','No emails matched this thread.'));
  t.conversation.messages.forEach(function(m){var d=el('div','msg '+(m.dir==='in'?'in':'out'));
    var hd=el('div','small muted', (m.dir==='in'?('⇦ '+(m.from||'user')):('⇨ '+(m.agent||'agent')+' → '+(m.to||'')))+'  ·  '+fmtTs(m.ts));
    d.appendChild(hd);
    // Case 427: badge the initial request + the deputy's FINAL so the three parts
    // the user asked for (your request / the exchange / the FINAL) read at a glance.
    if(m.kind==='initial'||m.kind==='final'){
      var kt=el('span','kindtag '+m.kind, m.kind==='initial'?'initial request':'final'); d.appendChild(kt); }
    if(m.subject) d.appendChild(el('div',null,m.subject));
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
  }
  // deliverables (best-effort): job artifacts owned by this task's agent + reports/
  if(!focus || focus==='deliverables'){
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
  (dd.files||[]).forEach(function(f){
    if(f.downloadable===false){
      // Case 512: emailed but outside the dashboard download roots — show it
      // (honest: a PDF WAS emailed) as a non-clickable row with the path, rather
      // than dropping it and rendering "(none found)".
      var s=el('div','small'); s.style.display='block';
      s.appendChild(el('span',null,'📧 emailed: '+f.name+' '));
      var n=el('span','muted small','· not downloadable here');
      n.title='This file was emailed to you but lives outside the dashboard download roots:\n'+f.path;
      s.appendChild(n); dl.appendChild(s); return;
    }
    var a=el('a','small',(f.emailed?'📧 emailed: ':'📄 ')+f.name);
    a.href='/download?path='+encodeURIComponent(f.path); a.target='_blank';
    a.title=f.emailed?'attachment this task’s agent emailed (authoritative)':'reports/ match';
    a.style.display='block'; dl.appendChild(a); });
  c.appendChild(dl);
  }
  if(!focus) c.appendChild(el('div','muted small',t.conversation.note||''));
  D.innerHTML=''; D.appendChild(c);
}
// Task 391 (uid=392): an alphanumeric sub-case (391a, 384e) has no numeric
// conversation to open in showTask(), so its case# links here — fetch the case
// FILE text and render it in the SAME #detail popup (a plain <a href=/download>
// just made the browser download the spec). fetch() ignores Content-Disposition,
// so we get the text inline; a 📎 link is still offered for the raw download.
async function showCaseFile(cnum, path){
  var D=document.getElementById('detail'); if(!D) return;
  D.innerHTML='<div class="card muted">loading&hellip;</div>';
  var txt;
  try{ var r=await fetch('/download?path='+encodeURIComponent(path));
    if(r.status===401){location='/login';return;}
    if(!r.ok){ D.innerHTML='<div class="card muted">could not load case #'+cnum+' ('+r.status+')</div>'; return; }
    txt=await r.text();
  }catch(e){ D.innerHTML='<div class="card muted">could not load case #'+cnum+'</div>'; return; }
  var c=el('div','card');
  var xb=el('button','small','✕ close'); xb.title='close'; xb.style.cssText='float:right;margin-left:8px';
  xb.onclick=function(){ D.innerHTML=''; if(window.pcClose) pcClose(); }; c.appendChild(xb);
  c.appendChild(el('h2',null,'Case #'+cnum));
  c.appendChild(el('div','muted small','sub-case spec · '+basename(path)));
  var pre=el('pre','mono small'); pre.textContent=txt;
  pre.style.cssText='white-space:pre-wrap;overflow:auto;max-height:60vh;margin-top:8px';
  c.appendChild(pre);
  var dl=el('a','small','📎 download raw'); dl.href='/download?path='+encodeURIComponent(path);
  dl.target='_blank'; dl.style.display='block'; c.appendChild(dl);
  D.innerHTML=''; D.appendChild(c);
}
"""

_STATUS_JS = _COMMON_JS + r"""
async function tick(){
  var s; try{ s=await getJSON('/api/status'); }catch(e){ return; }
  if(!s) return;
  daemonsBar(s.daemons);
  // limit banner — Case 582. One banner per LIVE limit, each naming WHAT SERVICE
  // and WHICH limit (Feng's ask). Reads s.limits (both vendors' account walls +
  // per-model caps); s.limit was claude's account marker only, so a ChatGPT wall
  // rendered nothing at all. A reset we GUESSED is labelled as a guess rather than
  // printed as the vendor's word — printing one as fact is what put "Reset: ~Fri
  // 18:32" in Feng's inbox for a wall that lifted at 17:47.
  var L=document.getElementById('limit'); L.innerHTML='';
  var lims=s.limits||(s.limit?[s.limit]:[]);
  lims.forEach(function(x){
    var b=el('div','banner');
    var scope=x.scope_label||((x.service_label||x.service||'?')+' account');
    var kind=x.kind_label||x.kind||'usage';
    // the vendor's fragment is already a phrase ("resets 4:50pm", "try again at
    // 5:47 PM"), so show it verbatim + the absolute clock; prefixing it printed
    // "resets resets Tue 6pm".
    var when=x.reset_known===false
      ? 'reset time not stated by the vendor — retrying ~'+fmtTs(x.reset_epoch)
      : (x.reset_str ? x.reset_str+' (~'+fmtTs(x.reset_epoch)+')'
                     : 'resets ~'+fmtTs(x.reset_epoch));
    b.textContent='USAGE LIMIT — '+scope+' · '+kind+' limit · '+when+
      ' · source: '+(x.source_worker||'?');
    L.appendChild(b);
  });
  // workers — Task 377 #3: lead with deputy / case# / precinct, then state/etc.
  document.getElementById('wc').textContent='('+s.workers.length+' active)';
  var wt=el('table'); wt.innerHTML='<tr><th>deputy</th><th>case#</th><th>precinct</th>'+
    '<th>state</th><th>model</th><th>description</th><th>requester</th><th class=nowrap>mail</th><th>rl</th></tr>';
  s.workers.forEach(function(w){var tr=el('tr');
    tr.appendChild(td(w.name,'mono'));                       // deputy = worker name
    // case# — Task 391 #2: a NUMERIC case opens its detail popup; an alphanumeric
    // sub-case (e.g. 384e, no numeric conversation) opens the SAME popup showing its
    // case-FILE text (uid=392: a download link just downloaded the spec); else plain text.
    var tk=el('td'); var cnum=w.case||(w.task?String(w.task):'');
    if(w.task){ var a=el('a','tlink','#'+cnum); a.href='javascript:void(0)';
      a.onclick=(function(id){return function(){showTask(id);};})(w.task); tk.appendChild(a); }
    else if(cnum && w.case_file){ var a2=el('a','tlink','#'+cnum); a2.href='javascript:void(0)';
      a2.onclick=(function(cn,pth){return function(){showCaseFile(cn,pth);};})(cnum,w.case_file); tk.appendChild(a2); }
    else { tk.textContent=cnum?('#'+cnum):''; }
    tr.appendChild(tk);
    tr.appendChild(td(w.precinct||'','small'));              // precinct
    var st=el('td'); var cls=w.state==='running'?'b-running':(w.state==='failed'?'b-failed':
      (String(w.state).indexOf('wait')>=0?'b-parked':'b-done'));
    st.appendChild(el('span','badge '+cls, w.state_label||w.state)); tr.appendChild(st);
    // Case 561: the model this case is running RIGHT NOW. A case may change model
    // mid-flight (the deputy switches itself between the work and report lanes),
    // so this comes from the watchdog roster the switch handler rewrites, never
    // from the launch script. The service is appended only when it is NOT the
    // default vendor, so an all-claude board stays uncluttered; a case that has
    // switched carries a counter whose tooltip names the last switch.
    var mt=el('td'); mt.className='small';
    var mlab=(w.model_label||w.model||'');
    if(w.service && w.service!=='claude') mlab+=' \u00b7 '+w.service;
    mt.textContent=mlab;
    if(w.switches>0){
      var sw=el('span','badge b-parked','\u21c4'+w.switches);
      sw.title=(w.last_switch||('switched '+w.switches+' time(s)'));
      sw.style.marginLeft='6px'; mt.appendChild(sw);
    }
    tr.appendChild(mt);
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
// Task 377 #3: on-demand GPU device stats. Fired ONLY by the Refresh button click
// (never on the 8s poll). Shows a spinner while nvidia-smi runs and a timestamp
// after; degrades gracefully when there is no GPU / nvidia-smi is missing.
async function loadGpuStats(){
  var host=document.getElementById('gpustats'), stat=document.getElementById('gpustatus');
  var spin=document.getElementById('gpuspin'), btn=document.getElementById('gpurefresh');
  if(spin) spin.style.display='inline'; if(stat) stat.textContent=''; if(btn) btn.disabled=true;
  var g=null;
  try{ g=await getJSON('/api/gpu_stats'); }
  catch(e){ if(stat) stat.textContent='error querying nvidia-smi'; }
  if(spin) spin.style.display='none'; if(btn) btn.disabled=false;
  if(!g) return;
  host.innerHTML='';
  if(!g.available){
    host.appendChild(el('div','card muted small', g.error||'No GPU stats available.'));
  } else if(!g.gpus.length){
    host.appendChild(el('div','card muted small','nvidia-smi returned no GPUs.'));
  } else {
    var t=el('table'); t.innerHTML='<tr><th>#</th><th>name</th><th>util</th>'+
      '<th>memory (used / total)</th><th>mem%</th><th>temp</th></tr>';
    g.gpus.forEach(function(gp){var tr=el('tr');
      tr.appendChild(td(gp.index,'mono small'));
      tr.appendChild(td(gp.name||'','small'));
      tr.appendChild(td(gp.util==null?'—':(gp.util+'%'),'small'));
      tr.appendChild(td((gp.mem_used==null?'—':gp.mem_used)+' / '+(gp.mem_total==null?'—':gp.mem_total)+' MB','small'));
      tr.appendChild(td(gp.mem_pct==null?'—':(gp.mem_pct+'%'),'small'));
      tr.appendChild(td(gp.temp==null?'—':(gp.temp+'°C'),'small'));
      t.appendChild(tr);});
    host.appendChild(t);
  }
  if(stat) stat.textContent='updated '+new Date(((g.ts||(Date.now()/1000)))*1000).toLocaleTimeString();
}
(function(){var b=document.getElementById('gpurefresh'); if(b) b.onclick=loadGpuStats;})();
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
  // Case 447: lineage is now direct-reply-only, so the legend is just
  // "direct reply" (a parent_task follow-up) vs a standalone case.
  var L=el('div','legend');
  [['bh','direct reply (parent_task)'],['broot','standalone case']].forEach(function(k){
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
  // Case 447: the day's cases GROUPED BY PRECINCT (a tree's group is its root's
  // precinct), each case described by its one-sentence summary. Direct-reply
  // follow-ups (parent_task) still nest; everything else is standalone.
  if(lin.trees && lin.trees.length){
    c.appendChild(el('div','muted small','CASES — '+lin.n_day_tasks+
      ' case'+(lin.n_day_tasks===1?'':'s')+' this day, grouped by precinct · click a case for detail'));
    c.appendChild(dayLegend());
    function markShowprec(n,grp){ n.showprec = !!(n.precinct && n.precinct!==grp);
      (n.children||[]).forEach(function(k){ markShowprec(k,grp); }); }
    function countOnDay(n){ var k=(n.on_day===true?1:0);
      (n.children||[]).forEach(function(k2){ k+=countOnDay(k2); }); return k; }
    var groups={};
    lin.trees.forEach(function(t){ var grp=t.precinct||'unassigned';
      markShowprec(t,grp);
      if(!groups[grp]) groups[grp]={trees:[],n:0};
      groups[grp].trees.push(t); groups[grp].n+=countOnDay(t); });
    // most-active precinct first, then alphabetical
    var names=Object.keys(groups).sort(function(a,b){
      return groups[b].n-groups[a].n || (a<b?-1:a>b?1:0); });
    names.forEach(function(pname){ var g=groups[pname];
      var sec=el('div','pgroup');
      var hd=el('div','pghead');
      hd.appendChild(el('span','pgname',pname));
      hd.appendChild(el('span','pgcount',g.n+' case'+(g.n===1?'':'s')));
      sec.appendChild(hd);
      var host=el('div','daytrees');
      g.trees.forEach(function(t){ var mt=el('div','mtree'); var ul=el('ul','tree-ul root');
        ul.appendChild(treeNodeLi(t)); mt.appendChild(ul); host.appendChild(mt); });
      sec.appendChild(host); c.appendChild(sec); });
  } else {
    c.appendChild(el('div','muted small','No cases launched this day.'));
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
        "<h2>Case Lineage</h2>"
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
    return _shell("Case Lineage", body, _LINEAGE_JS, active="lineage")


_LINEAGE_JS = _COMMON_JS + r"""
getJSON('/api/status').then(function(s){ if(s) daemonsBar(s.daemons); });
// tree rendering (treeNodeLi, DOT, BLABEL) is shared from _COMMON_JS.
async function loadForest(){
  var f; try{ f=await getJSON('/api/forest'); }catch(e){ return; } if(!f) return;
  document.getElementById('summary').textContent =
    f.n_trees+' trees link '+f.n_in_trees+' of '+f.n_tasks+' cases · '+
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
  ss.appendChild(el('b',null, f.singletons.length+' standalone cases'));
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
# JTF (Joint Task Force) assignment page (Task 353; Cowork->JTF Case 384e)
# --------------------------------------------------------------------------- #
def jtf_page():
    """Assignment form: pick a LEAD + collaborators, where each slot is EITHER a
    precinct (spawns a NEW deputy there) OR a specific deputy (which MUST take the
    role) — chosen in a popup that lists precincts and searches agents by name/task.
    Optionally add a critic, describe the task, submit. The form is a thin shell; all
    dynamic values go in via textContent (XSS-safe). Submitting POSTs JSON to
    /api/jtf, which drops a record the inbox-side scratch_jtf.py bridge materializes."""
    body = (
        "<h2>New JTF</h2>"
        "<div class='muted small' style='margin-bottom:14px;max-width:680px'>"
        "A <b>Joint Task Force</b> is a group of deputies working together on one task. "
        "One <b>leads</b> (owns the deliverables, plans, delegates, reviews and iterates); "
        "<b>collaborators</b> contribute; an optional <b>critic</b> audits the outputs "
        "independently. Each slot below is either a <b>precinct</b> (spawns a fresh deputy "
        "there, precinct default model) or a <b>specific deputy</b> (which <b>must</b> take "
        "the role — if it is mid-task it stops, hands that case to a new deputy, notifies the "
        "sheriff, then takes this). All communication is by email; every deputy is "
        "watchdog-monitored, exactly like regular work.</div>"
        "<div class='card jtf-form'>"
        "<div class='fld'><span class='lbl'>Lead (precinct or specific deputy)</span>"
        "<div class='pickrow'><span id='leadslot' class='muted small'>none selected</span>"
        "<button type='button' id='lead-search-btn' class='small'>Choose&hellip;</button></div></div>"
        "<div class='fld'><span class='lbl'>Collaborators (precinct or specific deputy)</span>"
        "<div id='collabchips' class='chips'></div>"
        "<div><button type='button' id='collab-add-btn' class='small'>Add collaborator&hellip;</button></div></div>"
        # Case 551: the JTF critic is now a NAMED judge from the registry rather
        # than a bare checkbox, so a JTF and a per-case critic mean the same thing.
        "<div class='fld'><span class='lbl'>Judge (optional)</span>"
        "<select id='critic'><option value=''>none</option>"
        + "".join(f"<option value='{html.escape(c['id'])}'>"
                    f"{html.escape(c.get('display_name') or c['id'])}</option>"
                    for c in state.critics())
        + "</select>"
        "<span class='small muted'>The lead iterates with this judge until it signs off "
        "the deliverables.</span></div>"
        "<div class='fld'><span class='lbl'>Task description</span>"
        "<textarea id='desc' rows='6' placeholder='What should this JTF accomplish? "
        "Be specific about the deliverables.'></textarea></div>"
        "<div class='actions'><button type='button' id='submitjtf'>Create JTF</button>"
        "<span id='jtfstatus' class='small muted'></span></div>"
        "</div>"
        # precinct/agent picker modal (hidden until opened)
        "<div id='pickmodal' class='modal' style='display:none'>"
        "<div class='modal-card'>"
        "<div class='modal-head'><b id='picktitle'>Choose a slot</b>"
        "<button type='button' id='pickclose' class='small'>&#10005;</button></div>"
        "<input id='pickq' type='text' autocomplete='off' "
        "placeholder='Filter precincts, or search an agent by name / a task it did&hellip;'>"
        "<div id='pickresults' class='pickresults'></div>"
        "</div></div>"
    )
    return _shell("New JTF", body, _JTF_JS, active="jtf")


# --------------------------------------------------------------------------- #
# Case 551: JUDGES — the critic roster.                                        #
# --------------------------------------------------------------------------- #
def judges_page():
    """The Judges tab: the roster of critics, the fixed charter every one of them
    shares, each one's custom (persona) prompt, the sheriff-approval trail for
    adding one, and the review rounds they have actually ruled on.

    Read-only over the registry by design — the registry is sheriff-owned, so the
    'propose a judge' form files a request onto the sheriff queue rather than
    writing anything itself."""
    e = html.escape
    cs = state.critics(include_retired=True)
    charter = state.critic_charter()
    reqs = state.critic_requests(limit=12)
    revs = state.critic_reviews(limit=15)

    intro = (
        "<h2>Judge</h2>"
        "<div class='muted small' style='margin-bottom:14px;max-width:760px'>"
        "A <b>judge</b> is an independent reviewing agent a deputy must satisfy "
        "before it may close a case. It is spawned as an anonymous worker, sees the work "
        "for the first time, reports only to the deputy, and returns one of three verdicts: "
        "<b>SIGN-OFF</b>, <b>REVISE</b> or <b>REJECT</b>. The deputy fixes and resubmits "
        "until it signs off.<br><br>"
        "Every judge runs the same fixed <b>charter</b> (independence, grounding, the verdict "
        "vocabulary, the output contract) plus its own <b>custom prompt</b> — the taste that "
        "makes it that judge. Pick one per case in the precinct <i>Create new case</i> form, or "
        "by email with a <code>judge: &lt;id&gt;</code> line. The default is <b>no judge</b>."
        "</div>")

    # ---- roster ----------------------------------------------------------
    if cs:
        rows = []
        for c in cs:
            live = c["status"] == "active"
            badge = ("<span class='badge b-done'>active</span>" if live
                     else "<span class='badge'>retired</span>")
            dflt = (" <span class='pill' title='Used when a case asks for a critic "
                    "without naming one'>default</span>" if c["id"] == "anonymous" else "")
            prov = e(c["added_by"] or "—")
            if c["request_id"]:
                prov += f" <span class='small muted'>(request {e(c['request_id'][:18])})</span>"
            rows.append(
                "<tr>"
                f"<td class='mono'>{e(c['id'])}{dflt}</td>"
                f"<td>{e(c['display_name'])}<div class='small muted'>{e(c['description'])}</div></td>"
                f"<td>{e(c['model'] or 'deputy default')}</td>"
                f"<td>{badge}</td>"
                f"<td class='small muted'>{prov}</td>"
                f"<td><button type='button' class='small jview' data-id='{e(c['id'])}'>"
                f"prompt ({c['prompt_chars']:,}&nbsp;ch)</button></td>"
                "</tr>")
        roster = ("<div class='card'><b>Roster</b>"
                  "<div class='tablewrap'><table class='table'><tr>"
                  "<th>id</th><th>judge</th><th>model</th><th>status</th>"
                  "<th>added by</th><th>custom prompt</th></tr>"
                  + "".join(rows) + "</table></div></div>")
    else:
        roster = ("<div class='card banner'>No judges are registered yet. The default "
                  "<code>anonymous</code> judge is seeded through the sheriff-approval path — "
                  "until then, cases created with a judge fall back to no review.</div>")

    # ---- the fixed charter ----------------------------------------------
    charter_card = (
        "<details class='card'><summary style='cursor:pointer;font-weight:600'>"
        f"The Critic Charter &mdash; fixed system prompt shared by every judge "
        f"<span class='small muted'>({len(charter):,} chars)</span></summary>"
        "<div class='small muted' style='margin:8px 0'>Prepended unchanged to every critic. "
        "Changing it is a sheriff operation (<code>critic_update --critic charter</code>), not "
        "an edit anyone can make here.</div>"
        f"<pre class='mono small' style='white-space:pre-wrap;max-height:460px;overflow:auto'>"
        f"{e(charter) or '(not seeded yet)'}</pre></details>")

    # ---- propose a judge (files a RECEPTIONIST CASE) ----------------------
    # Case 569: this form used to ask the user to type the judge's system prompt,
    # plus an id AND a display name, plus a one-line description, plus the reason
    # the sheriff should approve it — five fields of prompt engineering and
    # paperwork to answer one question, "what should this judge care about".
    # It now asks that question and nothing else. The submission becomes a case in
    # the receptionist precinct; the deputy there reads the charter and the live
    # personas, writes the prompt, writes the roster description and the sheriff
    # reason, and files the critic_add. The sheriff still decides.
    fc = ("padding:8px;border-radius:6px;border:1px solid var(--accent-border);"
          "background:var(--surface2);color:var(--fg);font:inherit")
    ta = fc + ";width:100%;resize:vertical"
    propose = (
        "<details class='card'><summary style='cursor:pointer;font-weight:600'>"
        "&#43; Propose a new judge</summary>"
        "<div class='small muted' style='margin:8px 0'>Describe the judge you want; you do "
        "<b>not</b> write its prompt. Submitting opens a case in the <b>receptionist</b> "
        "precinct: a deputy reads the charter and the judges already on the roster, writes "
        "this judge's prompt from your description, and files the <code>critic_add</code> "
        "for the <b>sheriff</b> to approve or deny. Nothing joins the roster until the "
        "sheriff approves it. You can start the same case by email &mdash; see the note "
        "under the request list below.</div>"
        "<div style='display:flex;flex-direction:column;gap:12px;max-width:760px'>"
        "<div style='display:flex;gap:12px;flex-wrap:wrap'>"
        "<label class='small' style='flex:2;min-width:220px'>Name<br>"
        f"<input type='text' id='jname' placeholder='e.g. The Reproducibility Judge' "
        f"style='{fc};width:100%'></label>"
        "<label class='small' style='flex:1;min-width:150px'>Model "
        "<span class='muted'>(optional)</span><br>"
        f"<select id='jmodel' style='{fc};width:100%'><option value=''>deputy default</option>"
        + "".join(f"<option value='{m}'>{models.label(m)}</option>" for m in models.ALIASES)
        + "</select></label></div>"
        "<label class='small'>Description &mdash; what should this judge care about?<br>"
        "<span class='muted'>Plain words. What it should look for, what should be worth "
        "blocking a case over, what kind of work it is for, whose taste it stands in for. "
        "The more concrete the better &mdash; &ldquo;every number in the report must be "
        "traceable to a command I can re-run&rdquo; gives a far better judge than "
        "&ldquo;be rigorous&rdquo;.</span><br>"
        f"<textarea id='jdesc' rows='9' placeholder='What should this judge refuse to sign "
        f"off on?' style='{ta}'></textarea></label>"
        "<div style='display:flex;gap:12px;align-items:center'>"
        "<button type='button' id='jsubmit'>Submit</button>"
        "<span id='jstatus' class='small muted'></span></div>"
        "</div></details>")

    # ---- the approval trail ---------------------------------------------
    if reqs:
        rr = []
        for r in reqs:
            cls = {"done": "b-done", "denied": "b-failed", "pending": "b-running"}.get(r["state"], "")
            verdict = {"done": "approved", "denied": "denied", "pending": "awaiting sheriff"}[r["state"]]
            who = r["deputy"] or r["requester"] or "—"
            rr.append("<tr>"
                      f"<td class='mono small'>{e(r['op'])}</td>"
                      f"<td class='mono'>{e(r['critic'])}</td>"
                      f"<td><span class='badge {cls}'>{verdict}</span></td>"
                      f"<td class='small muted'>{e(who)}</td>"
                      f"<td class='small'>{e((r['decision_reason'] or r['reason'])[:180])}</td>"
                      "</tr>")
        trail = ("<div class='card'><b>Sheriff approval trail</b>"
                 "<div class='small muted' style='margin:6px 0'>Every add / change / retire, and "
                 "how the sheriff ruled.</div>"
                 "<div class='tablewrap'><table class='table'><tr><th>op</th><th>judge</th>"
                 "<th>outcome</th><th>requested by</th><th>reason</th></tr>"
                 + "".join(rr) + "</table></div></div>")
    else:
        trail = ("<div class='card'><b>Sheriff approval trail</b>"
                 "<div class='small muted' style='margin-top:6px'>No judge requests yet.</div></div>")

    # ---- judges actually being used --------------------------------------
    if revs:
        vr = []
        for r in revs:
            cls = ("b-done" if r["latest"] == "SIGN-OFF"
                   else "b-failed" if r["latest"] == "REJECT" else "b-running")
            last = r["rounds"][-1]
            n = len(r["rounds"])
            # Case 555: the arc matters on a multi-round review, so show every
            # round's verdict inline and let the button open all of them in full.
            arc = " → ".join(
                ("<span class='pill' title='round %d'>%s</span>"
                 % (x["round"], e(x["verdict"] or "…"))) for x in r["rounds"])
            vr.append("<tr>"
                      f"<td class='mono'>{e(str(r['case']))}</td>"
                      f"<td class='mono'>{e(r['critic'] or '—')}</td>"
                      f"<td class='small'>{n}<div class='small muted'>{arc}</div></td>"
                      f"<td style='white-space:nowrap'>"
                      f"<span class='badge {cls}'>{e(r['latest'] or 'no verdict')}</span></td>"
                      f"<td class='small'>{e((last['one_line'] or '')[:160])}</td>"
                      f"<td><button type='button' class='small rview' "
                      f"data-case='{e(str(r['case']))}'>read ruling"
                      + (f" ({n} rounds)" if n > 1 else "") + "</button></td>"
                      "</tr>")
        used = ("<div class='card'><b>Recent rulings</b>"
                "<div class='small muted' style='margin:6px 0'>Each round is archived under "
                "<code>scratch_full_logs/critic_reviews/case_&lt;n&gt;/round_&lt;r&gt;/</code>, so a "
                "sign-off is an artifact rather than a claim in an email. "
                "<b>read ruling</b> opens the judge's full written verdict &mdash; every round of it.</div>"
                "<div class='tablewrap'><table class='table'><tr><th>case</th><th>judge</th>"
                "<th>rounds</th><th>latest</th><th>one-line read</th><th>full ruling</th></tr>"
                + "".join(vr) + "</table></div></div>")
    else:
        used = ("<div class='card'><b>Recent rulings</b><div class='small muted' "
                "style='margin-top:6px'>No cases have gone before a judge yet.</div></div>")

    howto = (
        "<div class='card'><b>By email</b>"
        "<div class='small muted' style='margin-top:6px'>"
        "Add a judge: email the receptionist (no precinct tag) describing the judge you want "
        "&mdash; same as the form above. The receptionist writes the prompt and files the "
        "<code>critic_add</code>; the sheriff decides. If you would rather write the prompt "
        "yourself, attach it and say so.<br>"
        "Use a judge on a case: put <code>judge: &lt;id&gt;</code> on its own line in the body "
        "of a precinct-tagged email (or <code>[judge:&lt;id&gt;]</code> in the subject), exactly "
        "like the existing <code>precinct:</code> and <code>model:</code> tags. "
        "Omit it for no judge.</div></div>")

    # per-judge prompt modal
    modal = ("<div id='jmodal' class='pcmodal' hidden><div class='pcmodal-backdrop'></div>"
             "<div class='pcmodal-dialog'><div class='pcmodal-bar'>"
             "<b id='jmtitle'>prompt</b>"
             "<button type='button' id='jmclose' class='small'>&#10005;</button></div>"
             "<pre id='jmbody' class='mono small' style='white-space:pre-wrap;padding:14px'></pre>"
             "</div></div>")

    # Case 555: the full-ruling modal — every round of one case's review.
    # (wider than the shared 920px dialog — a ruling is prose plus file:line
    # citations, and re-wrapping those to 920px hurts more than it helps)
    rmodal = ("<div id='rmodal' class='pcmodal' hidden><div class='pcmodal-backdrop'></div>"
              "<div class='pcmodal-dialog' style='width:min(1120px,96vw)'>"
              "<div class='pcmodal-bar'>"
              "<b id='rmtitle'>ruling</b>"
              "<button type='button' id='rmclose' class='small'>&#10005;</button></div>"
              "<div id='rmbody' style='padding:14px'></div>"
              "</div></div>")

    return _shell("Judge",
                  intro + roster + charter_card + propose + trail + used + howto + modal + rmodal,
                  _JUDGES_JS, active="judges")


_JUDGES_JS = _COMMON_JS + r"""
(function(){
  var modal=document.getElementById('jmodal');
  function close(){ modal.hidden=true; }
  document.getElementById('jmclose').addEventListener('click', close);
  modal.querySelector('.pcmodal-backdrop').addEventListener('click', close);
  document.addEventListener('keydown', function(ev){ if(ev.key==='Escape') close(); });
  Array.prototype.forEach.call(document.querySelectorAll('.jview'), function(b){
    b.addEventListener('click', async function(){
      var id=b.getAttribute('data-id');
      document.getElementById('jmtitle').textContent='custom prompt — '+id;
      document.getElementById('jmbody').textContent='loading…';
      modal.hidden=false;
      try{
        var r=await fetch('/api/judge?id='+encodeURIComponent(id));
        if(r.status===401){ location='/login'; return; }
        var d=await r.json();
        document.getElementById('jmbody').textContent=d.prompt||'(no prompt on disk)';
      }catch(e){ document.getElementById('jmbody').textContent='failed to load'; }
    });
  });

  // ---- Case 555: the FULL ruling modal — every round, in full ------------
  var rmodal=document.getElementById('rmodal');
  var rbody=document.getElementById('rmbody');
  function rclose(){ rmodal.hidden=true; document.body.classList.remove('pcmodal-open'); }
  function ropen(){ rmodal.hidden=false; document.body.classList.add('pcmodal-open');
    var dg=rmodal.querySelector('.pcmodal-dialog'); if(dg) dg.scrollTop=0; }
  document.getElementById('rmclose').addEventListener('click', rclose);
  rmodal.querySelector('.pcmodal-backdrop').addEventListener('click', rclose);
  document.addEventListener('keydown', function(ev){ if(ev.key==='Escape') rclose(); });

  function vcls(v){ return v==='SIGN-OFF' ? 'b-done'
                        : v==='REJECT'   ? 'b-failed' : 'b-running'; }
  var PRE='white-space:pre-wrap;overflow-x:auto;max-height:none;margin:8px 0 0;'
         +'padding:10px;border-radius:6px;background:var(--surface2)';

  // a titled list block; skipped entirely when the judge returned nothing for it
  function listBlock(title, items, render){
    if(!items || !items.length) return null;
    var d=el('div',null); d.style.marginTop='10px';
    d.appendChild(el('div','small',title+' ('+items.length+')'));
    var ul=el('ul'); ul.style.margin='4px 0 0'; ul.style.paddingLeft='20px';
    items.forEach(function(it){ var li=el('li','small'); li.style.marginBottom='4px';
      render(li,it); ul.appendChild(li); });
    d.appendChild(ul); return d;
  }

  function roundCard(cs, r){
    var c=el('div','card'); c.style.margin='0 0 14px';
    var head=el('div'); head.style.cssText='display:flex;gap:8px;align-items:center;flex-wrap:wrap';
    head.appendChild(el('b',null,'Round '+r.round));
    head.appendChild(el('span','badge '+vcls(r.verdict), r.pending ? 'in flight'
                                                        : (r.verdict||'no verdict')));
    var meta=[];
    if(r.critic) meta.push('judge '+r.critic);
    if(r.model)  meta.push(r.model);
    if(r.ts)     meta.push(fmtTs(r.ts));
    if(meta.length) head.appendChild(el('span','small muted', '· '+meta.join(' · ')));
    c.appendChild(head);

    if(r.pending){
      c.appendChild(el('div','small muted',
        'This round has been assigned but the judge has not written its verdict yet.'));
      return c;
    }
    if(r.coerced){
      var w=el('div','small flag'); w.style.marginTop='6px';
      w.textContent='The judge wrote SIGN-OFF but listed must-fixes, so the harness '
                   +'downgraded it to REVISE (fail-closed).';
      c.appendChild(w);
    }
    if(r.error){ var er=el('div','small flag'); er.style.marginTop='6px';
      er.textContent='harness note: '+r.error; c.appendChild(er); }
    if(r.one_line){ var ol=el('div',null,r.one_line);
      ol.style.cssText='margin:8px 0 0;font-style:italic'; c.appendChild(ol); }

    var b;
    b=listBlock('MUST FIX — blockers', r.must_fix, function(li,f){
      if(f.location){ var loc=el('span','mono small',f.location); li.appendChild(loc);
                      li.appendChild(document.createElement('br')); }
      li.appendChild(document.createTextNode(f.problem||''));
      if(f.fix){ li.appendChild(document.createElement('br'));
                 var fx=el('span','muted','fix: '+f.fix); li.appendChild(fx); }
    }); if(b) c.appendChild(b);
    function plain(li,s){ li.textContent=s; }
    b=listBlock('Should fix', r.should_fix, plain);        if(b) c.appendChild(b);
    b=listBlock('Keep', r.keep, plain);                    if(b) c.appendChild(b);
    b=listBlock('Could not verify', r.unverified, plain);  if(b) c.appendChild(b);
    b=listBlock('Artifacts reviewed', r.artifacts, function(li,p){
      li.appendChild(el('span','mono small',p)); });       if(b) c.appendChild(b);
    b=listBlock('Artifacts MISSING at review time', r.missing_artifacts, function(li,p){
      li.appendChild(el('span','mono small flag',p)); });  if(b) c.appendChild(b);

    // the whole written ruling — open by default; this is the point of the button
    var det=el('details'); det.open=true; det.style.marginTop='12px';
    var sm=el('summary','small'); sm.style.cursor='pointer';
    sm.textContent='Full written ruling — verdict.md ('
                  +(r.verdict_md?r.verdict_md.length.toLocaleString():0)+' chars)';
    det.appendChild(sm);
    var pre=el('pre','mono small', r.verdict_md || '(the judge wrote no verdict.md)');
    pre.style.cssText=PRE; det.appendChild(pre); c.appendChild(det);

    // what the judge was given — lazily fetched, it is 20-30 KB per round
    if(r.prompt_chars){
      var pd=el('details'); pd.style.marginTop='8px';
      var ps=el('summary','small muted'); ps.style.cursor='pointer';
      ps.textContent='What this judge was given — charter + persona + assignment ('
                    +r.prompt_chars.toLocaleString()+' chars)';
      pd.appendChild(ps);
      var ppre=el('pre','mono small','loading…'); ppre.style.cssText=PRE; pd.appendChild(ppre);
      var loaded=false;
      pd.addEventListener('toggle', async function(){
        if(!pd.open || loaded) return; loaded=true;
        try{
          var d=await getJSON('/api/judge_review?part=prompt&case='+encodeURIComponent(cs)
                              +'&round='+encodeURIComponent(r.round));
          ppre.textContent=(d&&d.prompt)||'(prompt.md not on disk)';
        }catch(e){ loaded=false; ppre.textContent='failed to load'; }
      });
      c.appendChild(pd);
    }
    return c;
  }

  Array.prototype.forEach.call(document.querySelectorAll('.rview'), function(b){
    b.addEventListener('click', async function(){
      var cs=b.getAttribute('data-case');
      document.getElementById('rmtitle').textContent='Ruling — case '+cs;
      rbody.innerHTML=''; rbody.appendChild(el('div','muted small','loading…'));
      ropen();
      var d;
      try{ d=await getJSON('/api/judge_review?case='+encodeURIComponent(cs)); }
      catch(e){ rbody.innerHTML=''; rbody.appendChild(el('div','flag','failed to load')); return; }
      if(!d) return;                                  // 401 -> getJSON redirected
      rbody.innerHTML='';
      var rs=d.rounds||[];
      if(!rs.length){ rbody.appendChild(el('div','muted','No rounds archived for this case.')); return; }
      document.getElementById('rmtitle').textContent =
        'Ruling — case '+cs+' · '+rs.length+' round'+(rs.length>1?'s':'')
        +(d.critic?' · judge '+d.critic:'');
      // a one-line arc so a multi-round review reads at a glance before the detail
      if(rs.length>1){
        var arc=el('div','small muted'); arc.style.margin='0 0 12px';
        arc.textContent='Arc: '+rs.map(function(r){
          return 'r'+r.round+' '+(r.verdict||'…'); }).join('  →  ');
        rbody.appendChild(arc);
      }
      rs.forEach(function(r){ rbody.appendChild(roundCard(cs, r)); });
    });
  });

  var btn=document.getElementById('jsubmit');
  if(!btn) return;
  var stat=document.getElementById('jstatus');
  function val(id){ var el=document.getElementById(id); return el?(el.value||'').trim():''; }
  btn.addEventListener('click', async function(){
    // Case 569: two fields — a name and a description of what the judge should
    // care about. The id is derived server-side from the name; the prompt, the
    // roster description and the sheriff reason are written by the deputy this
    // submission spawns, not typed here.
    var name=val('jname'), desc=val('jdesc');
    if(name.length<2){
      stat.className='small flag'; stat.textContent='Give the judge a name.'; return; }
    if(desc.length<40){
      stat.className='small flag';
      stat.textContent='Describe what this judge should care about — a sentence or two at '
                      +'minimum, or the deputy has nothing to write a prompt from.'; return; }
    stat.className='small muted'; stat.textContent='Opening the case…'; btn.disabled=true;
    try{
      var r=await fetch('/judges/propose',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:name, description:desc, model:val('jmodel')})});
      if(r.status===401){ location='/login'; return; }
      var d={}; try{ d=await r.json(); }catch(e){}
      if(r.ok && d.ok){
        stat.className='small';
        stat.textContent='Case '+(d.case||'')+' opened for judge \''+(d.id||'')+'\' — a '
                         +'receptionist deputy is writing its prompt and will file it with '
                         +'the sheriff. You will get an email.';
      } else {
        stat.className='small flag'; stat.textContent='Error: '+((d&&d.error)||('HTTP '+r.status));
      }
    }catch(e){ stat.className='small flag'; stat.textContent='Network error.'; }
    finally{ btn.disabled=false; }
  });
})();
"""


_JTF_JS = _COMMON_JS + r"""
getJSON('/api/status').then(function(s){ if(s) daemonsBar(s.daemons); });
var JT={ lead:null, collabs:[], mode:null, timer:null, precincts:[] };
// preload the precinct options (a slot can be a precinct -> a fresh deputy there)
getJSON('/api/precincts').then(function(d){
  JT.precincts = (d && d.precincts ? d.precincts : []).map(function(p){ return p.name; });
}).catch(function(){});
function sameSlot(a,b){ return !!a && !!b && a.kind===b.kind && a.name===b.name; }
function slotChip(slot, cls, onClear){
  var c=el('span',cls);
  c.appendChild(el('span','k', slot.kind==='precinct'?'precinct':'deputy'));
  c.appendChild(el('b',null,slot.name));
  var x=el('span','x','✕'); x.title='remove'; x.onclick=onClear;
  c.appendChild(x); return c;
}
function renderLead(){
  var host=document.getElementById('leadslot'); host.innerHTML='';
  if(!JT.lead){ host.className='muted small'; host.textContent='none selected'; return; }
  host.className='';
  host.appendChild(slotChip(JT.lead,'jtf-lead',function(){ JT.lead=null; renderLead(); }));
}
function renderCollabs(){
  var host=document.getElementById('collabchips'); host.innerHTML='';
  if(!JT.collabs.length){ host.appendChild(el('span','muted small','none added')); return; }
  JT.collabs.forEach(function(slot,i){
    host.appendChild(slotChip(slot,'jtf-chip',function(){
      JT.collabs.splice(i,1); renderCollabs(); }));
  });
}
function openPick(mode){
  JT.mode=mode;
  document.getElementById('picktitle').textContent =
    mode==='lead' ? 'Choose the lead' : 'Add a collaborator';
  document.getElementById('pickmodal').style.display='flex';
  var q=document.getElementById('pickq'); q.value=''; q.focus();
  runSearch('');
}
function closePick(){ document.getElementById('pickmodal').style.display='none'; JT.mode=null; }
function pick(slot){
  if(JT.mode==='lead'){
    JT.lead=slot;
    JT.collabs=JT.collabs.filter(function(s){ return !sameSlot(s,slot); });
    renderLead(); renderCollabs();
  } else {
    var dupLead = JT.lead && sameSlot(JT.lead,slot);
    var dupDep = slot.kind==='deputy' && JT.collabs.some(function(s){ return sameSlot(s,slot); });
    if(!dupLead && !dupDep) JT.collabs.push(slot);
    renderCollabs();
  }
  closePick();
}
async function runSearch(q){
  var box=document.getElementById('pickresults'); box.innerHTML='';
  var ql=(q||'').trim().toLowerCase();
  // 1) precinct options -> a fresh deputy spawned in that precinct
  var band=el('div','pickband');
  band.appendChild(el('div','picklbl','Assign to a precinct — spawns a new deputy'));
  var any=false;
  JT.precincts.forEach(function(p){
    if(ql && p.toLowerCase().indexOf(ql)<0) return;
    any=true;
    var ch=el('span','pchip','◆ '+p);
    ch.onclick=(function(name){ return function(){ pick({kind:'precinct',name:name}); }; })(p);
    band.appendChild(ch);
  });
  if(!any) band.appendChild(el('span','muted small','no precinct matches'));
  box.appendChild(band);
  // 2) specific deputies -> that exact deputy must take the role
  box.appendChild(el('div','picklbl','Or a specific deputy (must-take)'));
  var rows; try{ rows=await getJSON('/api/agents?q='+encodeURIComponent(q)); }catch(e){ rows=null; }
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
    it.onclick=(function(n){ return function(){ pick({kind:'deputy',name:n}); }; })(r.agent);
    box.appendChild(it);
  });
}
async function submitJTF(){
  var st=document.getElementById('jtfstatus');
  var desc=document.getElementById('desc').value.trim();
  if(!JT.lead){ st.className='small flag'; st.textContent='Choose a lead (a precinct or a specific deputy).'; return; }
  if(!JT.collabs.length){ st.className='small flag'; st.textContent='Add at least one collaborator.'; return; }
  if(!desc){ st.className='small flag'; st.textContent='Add a task description.'; return; }
  st.className='small muted'; st.textContent='Submitting…';
  var payload={ lead:JT.lead, collaborators:JT.collabs,
    critic:document.getElementById('critic').value||'', description:desc };
  try{
    var r=await fetch('/api/jtf',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if(r.status===401){ location='/login'; return; }
    var d={}; try{ d=await r.json(); }catch(e){}
    if(r.ok && d.ok){
      st.className='small'; st.textContent='JTF submitted — '+
        (d.note||'the inbox handler will materialize it and email an ACK.');
      JT.lead=null; JT.collabs=[]; document.getElementById('desc').value='';
      document.getElementById('critic').value=''; renderLead(); renderCollabs();
    } else {
      st.className='small flag'; st.textContent='Error: '+(d.error||('HTTP '+r.status));
    }
  }catch(e){ st.className='small flag'; st.textContent='Network error.'; }
}
document.getElementById('lead-search-btn').onclick=function(){ openPick('lead'); };
document.getElementById('collab-add-btn').onclick=function(){ openPick('collab'); };
document.getElementById('pickclose').onclick=closePick;
document.getElementById('submitjtf').onclick=submitJTF;
document.getElementById('pickmodal').onclick=function(e){ if(e.target===this) closePick(); };
document.getElementById('pickq').addEventListener('input', function(){
  clearTimeout(JT.timer); var v=this.value; JT.timer=setTimeout(function(){ runSearch(v); }, 180); });
document.addEventListener('keydown', function(e){ if(e.key==='Escape') closePick(); });
renderLead(); renderCollabs();
"""


# --------------------------------------------------------------------------- #
# precincts (Task 372 — Sheriff & Deputies). Server-rendered (no JS needed).
# --------------------------------------------------------------------------- #
def precincts_page():
    e = html.escape
    # Task 384c / Phase A1: present the watchdog + sheriff as ONE "Sheriff = system
    # manager". system_manager() only COMPOSES the existing read-only readers
    # (sheriff_status / daemon_status / workers / limit_state) -- it measures nothing
    # new and changes no behaviour; both daemons keep running exactly as today.
    sm = state.system_manager()
    sh = sm["records"]              # == state.sheriff_status(); the records-health loop
    prs = state.precincts()
    up = sm["sheriff_up"]           # records-health loop (the sheriff daemon)
    wd_up = sm["watchdog_up"]       # deputy-supervision loop (the watchdog daemon)
    dep = sm["deputies"]
    lim = sm["limit"]

    def _dot(ok):
        return "#3fb950" if ok else "#f85149"

    # ---------- one manager, two always-on loops (header summary) ----------
    if up and wd_up:
        both = "both loops up"
    elif wd_up and not up:
        both = "records loop DOWN"
    elif up and not wd_up:
        both = "deputy loop DOWN"
    else:
        both = "BOTH loops DOWN"

    # ---------- (A) deputy supervision -- the watchdog ----------------------
    def _state_badges(by_state):
        if not by_state:
            return "<span class='muted small'>no active deputies</span>"
        out = []
        for st, n in sorted(by_state.items(), key=lambda kv: (-kv[1], kv[0])):
            cls = ("b-running" if st == "running" else "b-failed" if st == "failed"
                   else "b-parked" if "wait" in str(st) else "b-done")
            label = state._STATE_LABEL.get(st, st)
            out.append(f"<span class='badge {cls}'>{e(str(label))}: {n}</span>")
        return " ".join(out)

    limit_line = ""
    if lim:
        reset = lim.get("reset_str") or lim.get("reset_epoch") or "?"
        limit_line = (
            "<p class='small' style='margin:5px 0;color:#f85149'><b>USAGE LIMIT active</b> "
            f"({e(str(lim.get('kind', '?')))}) &mdash; reset {e(str(reset))}. The watchdog "
            "holds relaunches until the reset (shared Claude Max limit).</p>")
    ndep = dep["active"]
    deputy_card = (
        "<div class='card'>"
        "<h3 style='margin:0 0 4px'>Deputy supervision "
        f"<span class='small c-{'ok' if wd_up else 'bad'}'>&#9679; watchdog "
        f"{'up' if wd_up else 'DOWN'}</span></h3>"
        "<p class='small muted' style='margin:2px 0'>The Sheriff's liveness loop "
        "(<span class='mono'>scratch_watchdog.py</span>): it detects a crashed or "
        "usage-limited deputy and relaunches it &mdash; at once on a crash, after the "
        "reset on a limit. Zero-API and battle-tested; behaviour unchanged.</p>"
        f"<p class='small' style='margin:5px 0'><b>{ndep}</b> active "
        f"deput{'y' if ndep == 1 else 'ies'} &middot; <b>{dep['relaunches']}</b> total "
        f"relaunches &middot; <b>{dep['mailbox_pending']}</b> with mail pending</p>"
        f"<p class='small' style='margin:5px 0'>{_state_badges(dep['by_state'])}</p>"
        f"{limit_line}"
        "<p class='small muted' style='margin:6px 0 0'>Full roster (per-deputy state, "
        "relaunch count, mail) on the <a href='/status'>Status</a> page.</p>"
        "</div>"
    )

    # ---------- (B) precinct records-health -- the sheriff daemon -----------
    if sh.get("A") and sh.get("B"):
        ab = (f"A = {sh['A']:,} tokens (soft limit) &rarr; "
              f"B = {sh['B']:,} tokens (post-compaction target)")
    else:
        ab = "A / B thresholds unknown (no sheriff startup line logged yet)"
    llm_on = bool(sh.get("llm"))
    cur_model = sh.get("model", "fable")
    field_css = ("padding:5px 9px;border-radius:6px;border:1px solid var(--accent-border);"
                 "background:var(--surface2);color:var(--fg);font:inherit")
    # Case 561 (Feng uid=676): the precinct default may be ANY registered model, so
    # a precinct can genuinely have "a default service and model". Grouped by service
    # and labelled with it, since the alias alone no longer tells you the vendor.
    model_opts = "".join(
        "".join(f"<option value='{m}'{' selected' if m == cur_model else ''}>"
                f"{models.label(m)} &middot; {models.service_label(svc)}</option>"
                for m in models.aliases_for(svc))
        for svc in models.SERVICE_IDS)
    # Task 384b / Phase C: the ONE global sheriff model (system-wide), authed write.
    # Case 509: show the SPECIFIC model (label + exact id), not the bare alias.
    model_form = (
        "<div class='small' style='margin:8px 0 2px'>"
        f"<b>Global sheriff model:</b> <span class='mono'>{e(models.label_with_id(cur_model))}</span> "
        "<span class='muted'>&mdash; one model system-wide for ledger compaction AND "
        "change-request decisions.</span></div>"
        "<form method='post' action='/sheriff/model' "
        "style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:2px 0'>"
        f"<select name='model' style='{field_css}'>{model_opts}</select>"
        "<button type='submit'>Set sheriff model</button></form>"
    )
    compaction_txt = (
        "Ledger <b>compaction is a Claude API call</b> on the global "
        f"<span class='mono'>{e(models.label(cur_model))}</span> model &mdash; it is given every precinct's "
        "(truncated) ledger for big-picture context and rewrites the crossing one; a "
        "mechanical rewrite is the automatic FALLBACK if that call fails or hits a usage limit."
        if llm_on else
        "Compaction is currently forced MECHANICAL (deterministic, cost-free; "
        "<code>TSOMP_SHERIFF_LLM=0</code>).")
    acts = "".join(f"<li class='small muted'>{e(a)}</li>" for a in sh.get("actions", [])) \
        or "<li class='small muted'>no ledger changes yet</li>"
    records_card = (
        "<div class='card'>"
        "<h3 style='margin:0 0 4px'>Precinct records-health "
        f"<span class='small c-{'ok' if up else 'bad'}'>&#9679; sheriff daemon "
        f"{'up' if up else 'DOWN'}</span></h3>"
        "<p class='small muted' style='margin:2px 0'>The Sheriff's records loop "
        "(<span class='mono'>scratch_sheriff.py</span>): each pass length-checks every "
        "mutable precinct's ledger and scans the approved-edit + deputy-request queues. "
        f"{compaction_txt}</p>"
        f"<p class='small muted' style='margin:5px 0'>Thresholds: {ab}. When a ledger "
        "crosses A it is compacted down to B and Steven is emailed. Never touches the "
        "fixed receptionist ledger; takes no email and makes no routing decision.</p>"
        f"{model_form}"
        "<p class='small muted' style='margin:7px 0 2px'>Recent ledger actions:</p>"
        f"<ul style='margin:0'>{acts}</ul></div>"
    )

    banner = (
        "<div class='card'>"
        "<div class='smgr-head'>"
        f"{_mark(92)}"
        "<div><h2>Sheriff</h2>"
        "<span class='small muted'>system manager &middot; two always-on loops "
        f"&middot; {both} &middot; all monitoring zero-API</span></div>"
        "</div>"
        f"<div class='smgr-grid'>{deputy_card}{records_card}</div>"
        "</div>"
    )
    intro = (
        "<div class='card'><p class='small' style='margin:0'>Work is organized into "
        "<b>precincts</b>. Route an email by putting <code>precinct: &lt;name&gt;</code> "
        "in the body or <code>[&lt;name&gt;]</code> in the subject (parsed first); a "
        "<b>reply</b> falls back to its parent case's precinct; an untagged, unowned "
        "contact goes to the <b>receptionist</b>, which answers or opens a case.</p></div>"
    )
    rows = ""
    for p in prs:
        mcls = "c-warn" if p["mode"] == "fixed" else "c-ok"
        rows += (
            "<tr>"
            f"<td><a href='/precinct?name={urllib.parse.quote(p['name'])}'><b>{e(p['name'])}</b></a></td>"
            f"<td><span class='small {mcls}'>{e(p['mode'])}</span></td>"
            # Case 557: name the backend service too when it is not claude.
            f"<td class='small mono' title='{e(models.model_id(p.get('model', 'opus')))}'>"
            f"{e(models.label_with_service(p.get('model', 'opus')))}</td>"
            f"<td class='small'>{e(p['description'])}</td>"
            f"<td class='small'>{p['ledger_chars']:,} ch (~{p['ledger_tokens']:,} tok)</td>"
            f"<td class='small'>{p['log_cases']:,}</td>"
            f"<td class='small'>{p['case_files']:,}</td>"
            f"<td class='small'>{p['deputies']:,}</td></tr>"
        )
    table = (
        "<h2>Precinct directory</h2>"
        "<div class='tablewrap'><table>"
        "<tr><th>Precinct</th><th>Ledger</th><th>Model</th><th>Description</th><th>Ledger size</th>"
        "<th>Closed cases</th><th>Case files</th><th>Deputies</th></tr>"
        f"{rows}</table></div>"
        "<p class='muted small'>Model = default model a spawned deputy runs on (set it on "
        "the precinct page; an email 'model:' tag overrides per-request). Ledger = "
        "big-picture digest (sheriff-compacted). Closed cases = the append-only case-log "
        "index. Case files = per-task specs. Deputies = task-number agents mapped to this "
        "precinct. Click a precinct for its ledger, case log, and deputies.</p>"
    )
    return _shell("Precincts", banner + intro + table, active="precincts")


# Case 557: the create-case Service picker chooses a MODE, and the Model list has
# to follow it — a mode may only offer models it can actually run. This map is
# GENERATED from models.aliases_for_mode / models.MODES, so the browser-side filter
# below cannot drift from the Python registry (add a mode there, it appears here).
_CC_MODES_JS = (
    "var CC_MODES=" + json.dumps({
        m: {"label": models.mode_label(m),
            "blurb": models.mode_spec(m)["blurb"],
            "deputy": models.deputy_service(m),
            "writer": models.writer_service(m),
            "aliases": list(models.aliases_for_mode(m))}
        for m in models.MODE_IDS}) + ";\n"
    "var CC_DEFAULT_MODE=" + json.dumps(models.DEFAULT_MODE) + ";\n"
    # Case 557 (uid=671): per-SERVICE alias lists + display labels, so the change
    # handler can REBUILD each model <select> from scratch. The previous approach
    # (optgroup.hidden / .disabled) does NOT work in a macOS native select popup —
    # WebKit ignores `hidden` on <optgroup>, so ChatGPT models stayed visible in
    # Claude mode, which is exactly the bug Steven reported.
    "var CC_SVC_ALIASES=" + json.dumps(
        {s: list(models.aliases_for(s)) for s in models.SERVICE_IDS}) + ";\n"
    "var CC_MODEL_LABELS=" + json.dumps(
        {a: models.label(a) for a in models.ALL_ALIASES}) + ";\n"
    "var CC_SVC_LABELS=" + json.dumps(
        {s: models.service_label(s) for s in models.SERVICE_IDS}) + ";\n"
    # Case 561: the WORK TYPES a case splits into. Generated from
    # models.WORK_TYPES so the form's wording is the same wording the deputy's
    # prompt and the switch CLI use — the definition of "human report writing"
    # only exists in one place.
    + "var CC_WORK_TYPES=" + json.dumps(
        {w: {"label": models.WORK_TYPES[w]["label"],
             "blurb": models.WORK_TYPES[w]["blurb"]}
         for w in models.WORK_TYPE_IDS}) + ";\n"
    + "var CC_SERVICE_IDS=" + json.dumps(list(models.SERVICE_IDS)) + ";\n"
)

_PRECINCT_DETAIL_JS = _COMMON_JS + _CC_MODES_JS + r"""
// Case 440 (Feng uid=469): the precinct Case-log rows open the shared
// showTask()/showCaseFile() popup (from _COMMON_JS, now prepended) inside a centered
// MODAL overlay (#pcmodal) that floats on top of everything — not the old inline box
// that dropped to the bottom of the page. pcShow reveals the dimmed backdrop + dialog
// and locks background scroll; pcClose hides it and clears the content. The card's own
// ✕ button also calls pcClose (guarded so History/Lineage, which have no #pcmodal, are
// unaffected). Backdrop-click and Esc dismiss too.
function pcShow(){ var m=document.getElementById('pcmodal'); if(m){ m.hidden=false;
    document.body.classList.add('pcmodal-open');
    var dg=m.querySelector('.pcmodal-dialog'); if(dg) dg.scrollTop=0; } }
function pcClose(){ var m=document.getElementById('pcmodal'); if(m){ m.hidden=true;
    document.body.classList.remove('pcmodal-open'); }
  var D=document.getElementById('detail'); if(D) D.innerHTML=''; }
function pcOpen(id, focus){ pcShow(); showTask(id, focus); }
function pcCaseFile(cnum, path){ pcShow(); showCaseFile(cnum, path); }
(function(){ var m=document.getElementById('pcmodal'); if(!m) return;   // bind dismissers once
  var bd=m.querySelector('.pcmodal-backdrop'); if(bd) bd.addEventListener('click', pcClose);
  document.addEventListener('keydown', function(ev){ if(ev.key==='Escape' && !m.hidden) pcClose(); }); })();

// Task 377 #4 / Case 421: submit the per-precinct "Create new case" form via multipart
// to the authed POST /precinct/create_case. Case 421 adds paste + drag-and-drop of
// files with live thumbnail/file previews. Files are collected in JS (pasted, dropped,
// or browsed) and appended to a FormData we build by hand on submit — so pasted
// screenshots, which can't live in a <input type=file>, are included. The create-case
// block runs in its own IIFE and ships a local el() helper (harmless shadow).
(function(){
  var form=document.getElementById('ccform'); if(!form) return;
  var btn=document.getElementById('ccsubmit'), stat=document.getElementById('ccstatus');
  var drop=document.getElementById('ccdrop'), input=document.getElementById('ccfiles');
  var prev=document.getElementById('ccprev'), noteEl=document.getElementById('ccnote');
  var CAP=10;                       // mirrors config.WEB_CASE_MAX_FILES
  var atts=[];                      // {file, url} — url = object URL for image previews

  function el(t,c,txt){var e=document.createElement(t); if(c)e.className=c;
    if(txt!==undefined)e.textContent=txt; return e;}
  function note(t){ if(noteEl) noteEl.textContent=t||''; }
  function isImg(f){ return /^image\//.test(f.type||''); }
  function fmtSize(n){ if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB';
    return (n/1048576).toFixed(1)+' MB'; }
  function sameFile(a,b){ return a.name===b.name && a.size===b.size &&
    a.lastModified===b.lastModified && a.type===b.type; }
  // A pasted screenshot arrives as a File named "image.png"; rename so repeat pastes
  // don't collide and the ACK email lists a meaningful name.
  function renamePasted(f){
    if(!(isImg(f) && (!f.name || /^image\.\w+$/i.test(f.name)))) return f;
    var ext=((f.type.split('/')[1])||'png').replace('jpeg','jpg').replace('svg+xml','svg');
    var nm='pasted-'+new Date().toISOString().replace(/[:.]/g,'-').slice(0,19)+
      '-'+Math.random().toString(36).slice(2,6)+'.'+ext;
    try{ return new File([f], nm, {type:f.type, lastModified:f.lastModified}); }
    catch(e){ return f; }          // older browsers: keep the original File
  }
  function addFiles(list){
    var added=0;
    for(var i=0;i<list.length;i++){ var f=list[i]; if(!f) continue;
      if(atts.length>=CAP) break;
      if(atts.some(function(a){return sameFile(a.file,f);})) continue;   // de-dupe
      atts.push({file:f, url:isImg(f)?URL.createObjectURL(f):null}); added++;
    }
    if(added) render();
  }
  function removeAt(i){ if(atts[i]&&atts[i].url) URL.revokeObjectURL(atts[i].url);
    atts.splice(i,1); render(); }
  function clearAtts(){ atts.forEach(function(a){ if(a.url) URL.revokeObjectURL(a.url); }); atts=[]; render(); }
  function render(){
    prev.innerHTML='';
    atts.forEach(function(a,i){
      var it=el('div','att-item');
      if(a.url){ var im=document.createElement('img'); im.src=a.url; im.alt=a.file.name; it.appendChild(im); }
      else { it.appendChild(el('div','fileicon',
        (a.file.name.split('.').pop()||'?').slice(0,4).toUpperCase())); }
      var nm=el('div','att-name',a.file.name); nm.title=a.file.name; it.appendChild(nm);
      it.appendChild(el('div','att-size',fmtSize(a.file.size)));
      var x=el('button','att-x','×'); x.type='button'; x.title='remove';
      x.onclick=(function(idx){return function(ev){ev.preventDefault();removeAt(idx);};})(i);
      it.appendChild(x); prev.appendChild(it);
    });
    note(atts.length>=CAP ? ('Maximum '+CAP+' attachments reached.') : '');
  }

  // browse (hidden input) — click or keyboard-activate the drop zone
  drop.onclick=function(){ input.click(); };
  drop.onkeydown=function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); input.click(); } };
  input.onchange=function(){ addFiles(input.files); input.value=''; };
  // drag & drop
  ['dragenter','dragover'].forEach(function(ev){ drop.addEventListener(ev,function(e){
    e.preventDefault(); e.stopPropagation(); drop.classList.add('drag'); }); });
  ['dragleave','drop'].forEach(function(ev){ drop.addEventListener(ev,function(e){
    e.preventDefault(); e.stopPropagation();
    if(ev==='dragleave' && drop.contains(e.relatedTarget)) return;   // ignore inner leaves
    drop.classList.remove('drag'); }); });
  drop.addEventListener('drop',function(e){
    if(e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files); });
  // paste anywhere in the form — capture image/file items; plain-text paste falls through
  form.addEventListener('paste',function(e){
    var dt=e.clipboardData; if(!dt) return;
    var got=[];
    if(dt.files && dt.files.length){ for(var i=0;i<dt.files.length;i++) got.push(dt.files[i]); }
    else if(dt.items){ for(var j=0;j<dt.items.length;j++){ var it=dt.items[j];
      if(it.kind==='file'){ var f=it.getAsFile(); if(f) got.push(f); } } }
    if(!got.length) return;                     // no files -> let the text paste happen
    e.preventDefault();
    addFiles(got.map(renamePasted));
  });

  // Case 557: the Service field picks a MODE (who works / who writes the report),
  // so the Model list must follow it — offering a chatgpt model in claude-only mode
  // would queue a case that cannot run. CC_MODES is generated from
  // models.aliases_for_mode in Python, so this filter cannot drift from the registry.
  // claude -> hide+disable the ChatGPT group; chatgpt -> hide+disable the Claude
  // group; claude+chatgpt -> BOTH, relabelled by ROLE (there a ChatGPT pick is the
  // report writer's model, not the deputy's). A now-invalid model falls back to the
  // empty "precinct default" option rather than silently posting a model the mode
  // cannot run.
  // Case 557 (uid=671). Two fixes over the first attempt:
  //  (a) REBUILD the option list instead of hiding optgroups. A macOS native select
  //      popup ignores `hidden` on <optgroup> (WebKit), so ChatGPT models stayed
  //      visible in Claude mode. Options that don't apply are now simply not in
  //      the DOM, which no browser can get wrong.
  //  (b) Claude + ChatGPT is TWO agents, so it needs TWO model pickers: the claude
  //      DEPUTY that does the work and the chatgpt WRITER that writes the report.
  //      The writer select's wrapper is display:none outside hybrid mode.
  var svcSel=form.service, modelSel=form.model, wmSel=form.writer_model,
      hint=document.getElementById('svc-hint'),
      mCap=document.getElementById('model-cap'),
      wWrap=document.getElementById('wmodel-wrap');
  // Capture the server-rendered first option BEFORE any rebuild — it carries the
  // precinct-specific default label ("precinct default (Opus 5)"), which the JS
  // has no other way to know.
  var MODEL_HEAD=(modelSel&&modelSel.options.length)?modelSel.options[0].textContent:'precinct default',
      WRITER_HEAD=(wmSel&&wmSel.options.length)?wmSel.options[0].textContent:'default';
  function fillModels(sel, aliases, headText){       // rebuild, preserving the pick
    if(!sel) return;
    var keep=sel.value;
    sel.innerHTML='';
    var o0=document.createElement('option'); o0.value=''; o0.textContent=headText;
    sel.appendChild(o0);
    for(var i=0;i<aliases.length;i++){
      var a=aliases[i], o=document.createElement('option');
      o.value=a; o.textContent=CC_MODEL_LABELS[a]||a; sel.appendChild(o);
    }
    sel.value=(aliases.indexOf(keep)>=0)?keep:'';     // invalid pick -> the default
  }
  // Case 561: the form now expresses a WORK SPLIT, not a mode. Each work type
  // (work / human report writing) picks its own service+model, and the deputy
  // switches ITSELF between them mid-case — one case, one context, several
  // models. The `service` field still posts a SERVICE id, which is also a valid
  // single-agent MODE id, so the server, the email tag and the bridge all keep
  // working unchanged.
  //
  // The report lane defaults to "same as work", which is what makes the split
  // free: leave it alone and the case behaves exactly as it did before.
  var rsvcSel=form.report_service, rmodelSel=form.report_model,
      rWrap=document.getElementById('rmodel-wrap'),
      jsvcSel=form.judge_service, jmodelSel=form.judge_model,
      jWrap=document.getElementById('judge-models'),
      criticSel=form.critic;
  var RMODEL_HEAD=(rmodelSel&&rmodelSel.options.length)?rmodelSel.options[0].textContent:'default',
      JMODEL_HEAD=(jmodelSel&&jmodelSel.options.length)?jmodelSel.options[0].textContent:'default';

  function syncMode(){
    if(!svcSel||!modelSel) return;
    var work=svcSel.value||'claude';
    if(hint) hint.textContent=CC_WORK_TYPES.work.blurb;
    // The WORK model list is exactly the models the work service can run. Rebuilt
    // rather than hidden — WebKit ignores `hidden` on <optgroup> (Case 557 uid=671).
    fillModels(modelSel, CC_SVC_ALIASES[work]||[], MODEL_HEAD);
    if(mCap) mCap.innerHTML = "Work model <span class='muted'>(does the work)</span>";
    // the Case 557 two-agent writer select is gone from this form; keep the field
    // present-but-empty so the POST shape is unchanged.
    if(wWrap) wWrap.style.display='none';
    if(wmSel){ wmSel.innerHTML=''; wmSel.value=''; }

    // REPORT lane: '' means "same as work" -> no model select at all, and the
    // case is not a split.
    var rep=rsvcSel?rsvcSel.value:'';
    if(rWrap) rWrap.style.display = rep ? '' : 'none';
    if(rmodelSel){
      if(rep) fillModels(rmodelSel, CC_SVC_ALIASES[rep]||[], RMODEL_HEAD);
      else { rmodelSel.innerHTML=''; rmodelSel.value=''; }
    }
  }
  function syncJudge(){
    // The judge's own service/model only matter once a judge is actually chosen.
    var on = !!(criticSel && criticSel.value);
    // 'flex' explicitly, not '': the wrapper is a <div> whose default display
    // is block, which would drop the inline flex layout on the two selects.
    if(jWrap) jWrap.style.display = on ? 'flex' : 'none';
    if(!on){ if(jsvcSel) jsvcSel.value=''; if(jmodelSel){ jmodelSel.innerHTML=''; jmodelSel.value=''; } return; }
    var js=jsvcSel?jsvcSel.value:'';
    if(jmodelSel){
      if(js) fillModels(jmodelSel, CC_SVC_ALIASES[js]||[], JMODEL_HEAD);
      else { jmodelSel.innerHTML=''; jmodelSel.value=''; }
    }
  }
  if(svcSel) svcSel.addEventListener('change', syncMode);
  if(rsvcSel) rsvcSel.addEventListener('change', syncMode);
  if(criticSel) criticSel.addEventListener('change', syncJudge);
  if(jsvcSel) jsvcSel.addEventListener('change', syncJudge);
  syncMode(); syncJudge();                     // initial state

  form.addEventListener('submit', async function(ev){
    ev.preventDefault();
    var desc=(form.description.value||'').trim();
    if(!desc){ stat.className='small flag'; stat.textContent='A task description is required.'; return; }
    var parent=(form.parent.value||'').trim();
    if(parent && !/^[0-9]+$/.test(parent)){ stat.className='small flag';
      stat.textContent='Follow-up must be a task/case number (or leave it empty).'; return; }
    stat.className='small muted'; stat.textContent='Submitting…'; btn.disabled=true;
    try{
      var fd=new FormData();
      fd.append('precinct', form.precinct.value);
      fd.append('service', (form.service&&form.service.value)||'');
      fd.append('model', form.model.value||'');
      fd.append('writer_model', (form.writer_model&&form.writer_model.value)||'');
      // Case 561: the report lane and the judge's own service/model. Empty =
      // "same as work" / "default", which is the no-split, pre-561 behaviour.
      fd.append('report_service', (form.report_service&&form.report_service.value)||'');
      fd.append('report_model', (form.report_model&&form.report_model.value)||'');
      fd.append('judge_service', (form.judge_service&&form.judge_service.value)||'');
      fd.append('judge_model', (form.judge_model&&form.judge_model.value)||'');
      fd.append('parent', parent);
      fd.append('description', desc);
      fd.append('critic', (form.critic&&form.critic.value)||'');
      atts.forEach(function(a){ fd.append('files', a.file, a.file.name); });
      var r=await fetch('/precinct/create_case',{method:'POST',body:fd});
      if(r.status===401){ location='/login'; return; }
      var d={}; try{ d=await r.json(); }catch(e){}
      if(r.ok && d.ok){
        stat.className='small';
        stat.textContent='Case '+(d.case||'')+' queued'+(d.files?(' with '+d.files+' file(s)'):'')+(d.critic?(' — judge: '+d.critic):'')+' — '+(d.note||'');
        form.reset(); clearAtts(); syncMode(); syncJudge();   // reset restores the defaults -> re-filter every model list
      } else {
        stat.className='small flag'; stat.textContent='Error: '+((d&&d.error)||('HTTP '+r.status));
      }
    }catch(err){ stat.className='small flag'; stat.textContent='Network error.'; }
    finally{ btn.disabled=false; }
  });
})();
"""


def precinct_detail_page(name):
    e = html.escape
    d = state.precinct_detail(name)
    if not d:
        body = ("<div class='banner'>No such precinct.</div>"
                "<p><a href='/precincts'>&#8592; All precincts</a></p>")
        return _shell("Precinct", body, active="precincts")
    bcol = "#d29922" if d["mode"] == "fixed" else "#3fb950"
    badge = "FIXED (read-only)" if d["mode"] == "fixed" else "mutable"
    head = (
        "<p class='small'><a href='/precincts'>&#8592; All precincts</a></p>"
        f"<h2 style='margin-bottom:2px'>{e(d['name'])} "
        f"<span class='small' style='color:{bcol}'>&#9679; {badge}</span></h2>"
        f"<p class='muted small'>{e(d['description'])}</p>"
    )
    # Task 376: per-precinct default model + the (authed) UI write form to change it.
    cur_model = d.get("model", "opus")
    field_css = ("padding:5px 9px;border-radius:6px;border:1px solid var(--accent-border);"
                 "background:var(--surface2);color:var(--fg);font:inherit")
    opts = "".join(
        f"<option value='{m}'{' selected' if m == cur_model else ''}>{models.label(m)}</option>"
        for m in models.ALIASES)
    model_card = (
        "<div class='card'>"
        "<div class='small muted' style='margin-bottom:6px'>Default model for spawned "
        f"deputies &mdash; current <b class='mono'>{e(models.label_with_id(cur_model))}</b> "
        "<span class='muted'>(an email <code>model:&lt;name&gt;</code> tag overrides it "
        "per request)</span></div>"
        "<form method='post' action='/precinct/model' "
        "style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>"
        f"<input type='hidden' name='name' value='{e(d['name'])}'>"
        f"<select name='model' style='{field_css}'>{opts}</select>"
        "<button type='submit'>Set default model</button></form></div>"
    )
    ledger = (
        f"<h2>Ledger <span class='muted small'>{d['ledger_chars']:,} chars "
        f"(~{d['ledger_tokens']:,} tokens)</span></h2>"
        "<div class='card'><pre style='white-space:pre-wrap;max-height:340px;"
        f"overflow:auto;margin:0'>{e(d['ledger'] or '(empty)')}</pre></div>"
    )
    deps = d["deputies"]
    if deps:
        # Task 377 #1: DEPUTY (who did the work) leads; Handler (triage lineage) is
        # the lesser column so the two are never conflated again.
        drows = "".join(
            f"<tr><td class='small'>{e(str(x['task']))}</td>"
            f"<td class='small'>{e(str(x.get('deputy') or ''))}</td>"
            f"<td class='small muted'>{e(str(x.get('handler') or ''))}</td>"
            f"<td class='small'>{e((x.get('session') or '')[:8])}</td>"
            f"<td class='small muted'>{e(x.get('basis') or '')}</td></tr>" for x in deps)
        deputies = (f"<h2>Deputies <span class='muted small'>{len(deps)} task numbers</span></h2>"
                    "<div class='tablewrap'><table>"
                    "<tr><th>Case</th><th>Deputy</th><th>Handler</th><th>Session</th><th>Basis</th></tr>"
                    f"{drows}</table></div>")
    else:
        deputies = "<h2>Deputies</h2><p class='muted small'>none mapped yet</p>"
    crows = ""
    for c in d["cases"]:
        tid = str(c["task"])
        # Case 440 (Feng uid=469): all three buttons open a centered MODAL popup.
        # A numeric case can reconstruct its email thread + deliverables (via
        # /api/task -> showTask), so it gets "conversation" + "deliverables" + a
        # "case file" (the writeup, via showCaseFile, with a raw-download link inside).
        # Alphanumeric sub-cases (e.g. 384c) have no reconstructable numeric thread,
        # so they get the "case file" modal only (mirrors the Status workers table).
        oc_cf = e(f"pcCaseFile({json.dumps(tid)}, {json.dumps(c['path'])})")
        cf_btn = (f"<a onclick=\"{oc_cf}\" title='Case-file writeup (raw download inside)'>"
                  "&#128196; case file</a>")
        if tid.isdigit():
            oc_conv = e(f"pcOpen({tid}, 'conversation')")
            oc_deliv = e(f"pcOpen({tid}, 'deliverables')")
            acts = (f"<a onclick=\"{oc_conv}\" title='Reconstructed email thread for this case'>"
                    "&#128172; conversation</a>"
                    f"<a onclick=\"{oc_deliv}\" title='Files &amp; artifacts this case delivered'>"
                    "&#128230; deliverables</a>"
                    f"{cf_btn}")
        else:
            acts = cf_btn
        # Task 377 #1: Deputy column (empty '-' for pre-377 rows that carry none).
        crows += (f"<tr><td class='small'>{e(tid)}</td>"
                  f"<td class='small'>{e(c.get('deputy') or '-')}</td>"
                  f"<td class='small'>{e(c['summary'])}</td>"
                  f"<td class='small'><div class='viewacts'>{acts}</div></td></tr>")
    cases = (f"<h2>Case log <span class='muted small'>{len(d['cases'])} closed cases "
             "(append-only index)</span></h2>"
             "<div class='tablewrap'><table>"
             "<tr><th>Case</th><th>Deputy</th><th>Summary</th><th>View</th></tr>"
             f"{crows or '<tr><td colspan=4 class=small muted>no closed cases yet</td></tr>'}"
             "</table></div>"
             # Case 440 (Feng uid=469): the shared showTask()/showCaseFile() popup
             # renders INSIDE a centered MODAL overlay on top of everything — a dimmed
             # backdrop + a floating dialog, dismissed by the card's ✕, click-outside,
             # or Esc. Hidden until a Case-log button opens it.
             "<div id='pcmodal' class='pcmodal' hidden>"
             "<div class='pcmodal-backdrop'></div>"
             "<div class='pcmodal-dialog' role='dialog' aria-modal='true'>"
             "<div class='pcmodal-bar'><button type='button' class='pcmodal-x' "
             "onclick='pcClose()' aria-label='close'>&#10005; close</button></div>"
             "<div id='detail'></div></div></div>")
    # Task 377 #4: the per-precinct "Create new case" form (collapsed <details> so
    # it doesn't dominate). model optional / follow-up optional / description required
    # / multiple file+photo uploads. Submits multipart to the authed POST endpoint.
    ta_css = ("width:100%;padding:9px;border-radius:6px;border:1px solid var(--accent-border);"
              "background:var(--surface2);color:var(--fg);font:inherit;resize:vertical")
    # Case 557: the "Service" field picks a MODE — who does the work and who writes
    # the report — not a raw vendor. Exactly the three MODE_IDS, no empty "default"
    # entry (an empty option next to an explicit list rendered as
    # "Claude (default) / Claude / ChatGPT", the first two being the same thing, and
    # left hybrid mode unselectable). claude is preselected; each option carries its
    # blurb as a hover tooltip, and #svc-hint below the field shows the same line.
    # The POST field name stays "service" — the bridge reads record["service"] and
    # normalizes it as a mode.
    # Case 561: the WORK lane's service. This field now offers SERVICES, not modes.
    # The two single-agent mode ids are identical to their service ids, so the POST
    # value is still a valid mode and the server/bridge contract is unchanged — but
    # "Claude + ChatGPT" is deliberately GONE from this form: it meant a second
    # agent (one case, two contexts), and mixing vendors on a case is now expressed
    # by the work split below, which keeps one context and switches its model.
    # (The old mode remains reachable by an explicit `mode: claude+chatgpt` email
    # tag for anyone who still wants the two-agent behaviour.)
    service_opts = "".join(
        f"<option value='{e(s)}' title='{e(models.WORK_TYPES['work']['blurb'])}'"
        f"{' selected' if s == models.DEFAULT_SERVICE else ''}>"
        f"{e(models.service_label(s))}</option>"
        for s in models.SERVICE_IDS)
    # Case 557 (uid=671): TWO model selects, because Claude+ChatGPT is genuinely two
    # agents — the claude DEPUTY that does the work and the chatgpt WRITER that writes
    # the report — so there are two models to choose. `model` is the deputy's,
    # `writer_model` the writer's; the latter's wrapper is shown ONLY in hybrid mode.
    # Both are rendered server-side for the DEFAULT mode and then REBUILT by
    # syncMode() on change (never hidden/disabled — WebKit ignores optgroup.hidden).
    def _model_opts(aliases, cur=None):
        head = ("<option value=''>precinct default ("
                + e(models.label(cur or cur_model)) + ")</option>")
        return head + "".join(
            f"<option value='{m}'>{e(models.label(m))}</option>" for m in aliases)

    _dm = models.DEFAULT_MODE
    model_opts = _model_opts(models.aliases_for(models.deputy_service(_dm)))
    # the writer select starts empty-but-valid; syncMode fills it when hybrid is picked
    writer_opts = "<option value=''>default (GPT-5.6 Terra)</option>"
    # Case 561: the REPORT lane's service. "same as work" is FIRST and is the
    # default — a case that ignores the split runs entirely on one model, exactly
    # as before. Choosing the SAME service here is still meaningful: it is how you
    # ask for e.g. Opus to work and Fable to write.
    report_service_opts = (
        "<option value=''>same as work (no switch)</option>"
        + "".join(f"<option value='{s}'>{e(models.service_label(s))}</option>"
                  for s in models.SERVICE_IDS))
    report_model_opts = "<option value=''>service default</option>"
    # Case 561: the JUDGE's own service+model (Feng: "For the judge, also get user
    # select service/model"). Empty = the judge's registered default, which is how
    # every judge has run so far.
    judge_service_opts = (
        "<option value=''>default</option>"
        + "".join(f"<option value='{s}'>{e(models.service_label(s))}</option>"
                  for s in models.SERVICE_IDS))
    judge_model_opts = "<option value=''>default</option>"
    critic_opts = "<option value=''>none (default &mdash; no judge)</option>" + "".join(
        f"<option value='{e(c['id'])}'>{e(c.get('display_name') or c['id'])}</option>"
        for c in state.critics())
    create_card = (
        "<details class='card'><summary style='cursor:pointer;font-weight:600'>"
        f"&#43; Create new case in {e(d['name'])}</summary>"
        "<div class='small muted' style='margin:8px 0'>Spawns a deputy via the inbox handler, which "
        "emails an ACK with your form + any uploads attached. Only a task description is required; "
        "everything else has a working default.</div>"
        "<form id='ccform' enctype='multipart/form-data' "
        "style='display:flex;flex-direction:column;gap:12px;max-width:680px'>"
        f"<input type='hidden' name='precinct' value='{e(d['name'])}'>"
        # Case 561: THE WORK SPLIT. A case picks a service+model per work type; the
        # deputy launches on the work lane and switches ITSELF to the report lane
        # when it starts the deliverable document. This REPLACES the Case 557
        # Service (mode) picker, whose "Claude + ChatGPT" spawned a SECOND agent —
        # one case with two contexts, which is the design Feng rejected here.
        "<fieldset style='border:1px solid #3a3a3a;border-radius:8px;padding:10px 12px;margin:0'>"
        "<legend class='small' style='padding:0 6px'><b>Work split</b> "
        "<span class='muted'>&mdash; which model does what</span></legend>"
        "<div class='small muted' style='margin-bottom:8px'>The deputy is launched on the "
        "<b>work</b> model and switches itself to the <b>report</b> model when it starts writing "
        "a document for you &mdash; same case, same context, different model. Leave the report "
        "row on &ldquo;same as work&rdquo; to run the whole case on one model.</div>"
        "<div style='display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start'>"
        "<label class='small' style='flex:1;min-width:190px'>Work &mdash; service<br>"
        f"<select name='service' style='{field_css};width:100%'>{service_opts}</select></label>"
        "<label class='small' id='model-wrap' style='flex:1;min-width:190px'>"
        "<span id='model-cap'>Work model <span class='muted'>(does the work)</span></span><br>"
        f"<select name='model' style='{field_css};width:100%'>{model_opts}</select></label>"
        "</div>"
        "<div class='small muted' id='svc-hint' style='margin:4px 0 10px'>"
        f"{e(models.WORK_TYPES['work']['blurb'])}</div>"
        "<div style='display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start'>"
        "<label class='small' style='flex:1;min-width:190px'>Human report writing &mdash; service<br>"
        f"<select name='report_service' style='{field_css};width:100%'>{report_service_opts}</select></label>"
        "<label class='small' id='rmodel-wrap' style='flex:1;min-width:190px;display:none'>"
        "Report model<br>"
        f"<select name='report_model' style='{field_css};width:100%'>{report_model_opts}</select></label>"
        "</div>"
        "<div class='small muted' style='margin-top:4px'>"
        f"{e(models.WORK_TYPES['report']['blurb'])}</div>"
        "</fieldset>"
        # Case 557 (uid=671): the two-agent writer's model. No longer offered by this
        # form (Case 561 replaced that design) but kept in the DOM as an empty field,
        # so the POST shape and the server contract are unchanged.
        "<label class='small' id='wmodel-wrap' style='display:none'>"
        "<span id='wmodel-cap'>ChatGPT model <span class='muted'>(writes the report)</span></span><br>"
        f"<select name='writer_model' style='{field_css};width:100%'>{writer_opts}</select></label>"
        "<div style='display:flex;gap:12px;flex-wrap:wrap'>"
        "<label class='small' style='flex:1;min-width:200px'>Follow-up on task&nbsp;# "
        "<span class='muted'>(optional; empty = new task)</span><br>"
        f"<input type='text' name='parent' placeholder='e.g. 376' style='{field_css};width:100%'></label>"
        "</div>"
        "<label class='small'>Task description <span class='muted'>(required)</span><br>"
        f"<textarea name='description' rows='4' style='{ta_css}' "
        "placeholder='What should this case accomplish? Be specific about deliverables.'></textarea></label>"
        # Case 551: pick a JUDGE. Default is none -- when one is chosen the deputy
        # must iterate with it until it signs off before it may close the case.
        "<label class='small'>Judge <span class='muted'>(optional; default none. "
        "If set, the deputy must iterate with this judge until it signs off before "
        "closing the case &mdash; see <a href='/judges'>Judge</a>.)</span><br>"
        f"<select name='critic' style='{field_css};width:100%;max-width:340px'>{critic_opts}</select></label>"
        # Case 561: the judge's OWN service+model. Shown only once a judge is
        # picked — until then there is nothing for them to configure.
        "<div id='judge-models' style='display:none;gap:12px;flex-wrap:wrap'>"
        "<label class='small' style='flex:1;min-width:190px'>Judge &mdash; service<br>"
        f"<select name='judge_service' style='{field_css};width:100%'>{judge_service_opts}</select></label>"
        "<label class='small' style='flex:1;min-width:190px'>Judge model<br>"
        f"<select name='judge_model' style='{field_css};width:100%'>{judge_model_opts}</select></label>"
        "</div>"
        "<div class='small'>Attachments <span class='muted'>(optional — drag &amp; drop, "
        "paste a screenshot, or browse; max 10)</span>"
        # Case 421: paste/drag-drop drop zone + live previews. The file input is a
        # hidden browse-trigger; pasted/dropped/browsed files are collected in JS and
        # sent as multipart on submit (see _PRECINCT_DETAIL_JS).
        "<div id='ccdrop' class='dropzone' tabindex='0' role='button' aria-label='Add attachments'>"
        "<input type='file' id='ccfiles' multiple hidden>"
        "&#128206; <b>Drag &amp; drop</b> files here, <b>paste</b> a screenshot, or "
        "<span class='dz-browse'>browse</span></div>"
        "<div id='ccprev' class='attprev'></div>"
        "<div id='ccnote' class='small muted'></div></div>"
        "<div style='display:flex;gap:12px;align-items:center'>"
        "<button type='submit' id='ccsubmit'>Create case</button>"
        "<span id='ccstatus' class='small muted'></span></div>"
        "</form></details>"
    )
    # Task 376 #5: order is header -> model -> ledger -> CASE LOG -> deputies
    # (case files now come BEFORE deputies). Task 377 #4: create-case form after model.
    return _shell(f"Precinct: {name}",
                  head + model_card + create_card + ledger + cases + deputies,
                  _PRECINCT_DETAIL_JS, active="precincts")
