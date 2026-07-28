"""HTML for the dashboard. Login is server-rendered (escaped); the status/history
pages are thin shells filled by fetch() from the JSON APIs (all dynamic values go
in via textContent on the client, so they are XSS-safe)."""
import html

_CSS = """
* { box-sizing: border-box; }
body { margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:#0f1420; color:#e6e9ef; }
a { color:#6cb6ff; text-decoration:none; } a:hover { text-decoration:underline; }
header { display:flex; align-items:center; gap:16px; padding:10px 18px;
         background:#161c2b; border-bottom:1px solid #263049; position:sticky; top:0; z-index:5; }
header .brand { font-weight:700; letter-spacing:.3px; }
header nav a { margin-right:14px; font-weight:600; }
header .spacer { flex:1; }
.pill { display:inline-flex; align-items:center; gap:6px; padding:2px 9px; border-radius:20px;
        font-size:12px; font-weight:600; background:#20293d; }
.dot { width:8px; height:8px; border-radius:50%; background:#5a6b8c; display:inline-block; }
.dot.ok { background:#3fb950; } .dot.bad { background:#f85149; }
main { padding:18px; max-width:1200px; margin:0 auto; }
h2 { font-size:15px; text-transform:uppercase; letter-spacing:.5px; color:#9db2d6;
     margin:22px 0 8px; border-bottom:1px solid #263049; padding-bottom:4px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:6px 9px; border-bottom:1px solid #1e273b; vertical-align:top; }
th { color:#8fa3c7; font-weight:600; font-size:11px; text-transform:uppercase; }
tr:hover td { background:#161d2c; }
.badge { padding:1px 7px; border-radius:5px; font-size:11px; font-weight:700; }
.b-running{background:#12351d;color:#5ce07e;} .b-done{background:#1b2740;color:#9db2d6;}
.b-failed{background:#3a1518;color:#ff7b72;} .b-parked{background:#33280f;color:#e3b341;}
.b-pending{background:#1a2b3d;color:#6cb6ff;} .b-gpu{background:#2a1a3d;color:#c58bff;}
.card { background:#141b29; border:1px solid #263049; border-radius:8px; padding:12px 14px; margin-bottom:10px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:10px; }
.muted { color:#8093b0; } .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.banner { background:#3a1518; border:1px solid #7d2b30; color:#ffb4b0; padding:10px 14px;
          border-radius:8px; margin-bottom:14px; font-weight:600; }
.flag { color:#e3b341; font-weight:700; }
button, .btn { background:#20406b; color:#dbe7ff; border:1px solid #2f5488; padding:5px 11px;
        border-radius:6px; cursor:pointer; font-size:13px; }
button:hover { background:#295084; }
.cal { display:grid; grid-template-columns:repeat(7,1fr); gap:5px; max-width:520px; }
.cal .h { text-align:center; color:#8093b0; font-size:11px; font-weight:700; }
.cal .day { min-height:46px; border:1px solid #24304a; border-radius:6px; padding:4px; cursor:pointer;
            background:#131a28; position:relative; }
.cal .day:hover { border-color:#3d68a8; }
.cal .day.empty { background:transparent; border:none; cursor:default; }
.cal .day.has { background:#16243a; }
.cal .day.sel { outline:2px solid #6cb6ff; }
.cal .day .n { font-size:11px; color:#9db2d6; }
.cal .day .c { position:absolute; right:4px; bottom:3px; font-size:11px; font-weight:700; color:#6cb6ff; }
.two { display:grid; grid-template-columns:540px 1fr; gap:18px; align-items:start; }
@media(max-width:1000px){ .two{grid-template-columns:1fr;} }
.thread .msg { border-left:3px solid #2f5488; padding:5px 10px; margin:7px 0; background:#131a28; border-radius:0 6px 6px 0; }
.thread .msg.in { border-left-color:#3fb950; }
.lin { list-style:none; padding-left:0; } .lin li { padding:3px 0; }
.lin .arrow { color:#8093b0; } .lin .cur { font-weight:700; color:#e3b341; }
.small { font-size:12px; } .nowrap{white-space:nowrap;}
code.cmd{ display:block; white-space:pre-wrap; word-break:break-all; background:#0d1320; padding:6px 8px;
          border-radius:5px; border:1px solid #22304a; color:#b9c7e0; }
/* ---- lineage forest ---- */
.legend { display:flex; flex-wrap:wrap; gap:16px; align-items:center; margin:4px 0 14px; font-size:12px; color:#9db2d6; }
.legend .k { display:inline-flex; align-items:center; gap:7px; }
.cbar { width:4px; height:15px; border-radius:1px; flex:none; display:inline-block; }
.cbar.bh{background:#3fb950;} .cbar.bm{background:#e3b341;} .cbar.bl{background:#8a94a6;} .cbar.broot{background:#6cb6ff;}
details.tree, details.singles { margin-bottom:6px; }
details.tree > summary, details.singles > summary { cursor:pointer; list-style:none; padding:5px 2px; outline:none; }
details.tree > summary::-webkit-details-marker, details.singles > summary::-webkit-details-marker { display:none; }
details.tree > summary::before, details.singles > summary::before { content:'▾'; color:#8093b0; margin-right:6px; }
details.tree:not([open]) > summary::before, details.singles:not([open]) > summary::before { content:'▸'; }
ul.tree-ul { list-style:none; margin:0; padding-left:15px; border-left:1px solid #24304a; }
ul.tree-ul.root { border-left:none; padding-left:0; }
.tnode { display:flex; align-items:center; gap:8px; padding:2px 0; }
a.tlink { color:#cdd7ea; } a.tlink:hover { color:#6cb6ff; }
.bchip { font-size:10px; padding:1px 6px; border-radius:9px; background:#20293d; color:#8fa3c7; white-space:nowrap; }
.singlewrap { display:flex; flex-wrap:wrap; gap:5px; margin-top:8px; }
a.schip { font-size:12px; padding:1px 7px; border-radius:5px; background:#1b2740; color:#9db2d6; font-family:ui-monospace,monospace; }
a.schip:hover { background:#20406b; color:#dbe7ff; text-decoration:none; }
.empty-state { text-align:center; color:#6b7688; padding:34px 16px; font-style:italic; }
"""

_NAV = """
<header>
  <span class="brand">&#129302; Claude Infra</span>
  <nav><a href="/">Status</a><a href="/history">History</a><a href="/lineage">Lineage</a></nav>
  <span id="daemons" class="small muted"></span>
  <span class="spacer"></span>
  <a href="/logout" class="small">Logout</a>
</header>
"""


def _shell(title, body, script=""):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{_NAV}<main>{body}</main><script>{script}</script></body></html>"
    )


def login_page(error=""):
    err = f"<div class='banner'>{html.escape(error)}</div>" if error else ""
    body = (
        "<div style='max-width:340px;margin:12vh auto;' class='card'>"
        "<h2 style='border:none;margin-top:0'>Sign in</h2>"
        f"{err}"
        "<form method='post' action='/login'>"
        "<input type='password' name='password' placeholder='Password' autofocus "
        "style='width:100%;padding:9px;border-radius:6px;border:1px solid #2f5488;"
        "background:#0d1320;color:#e6e9ef;margin-bottom:10px'>"
        "<button type='submit' style='width:100%'>Enter</button>"
        "</form></div>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Sign in</title><style>{_CSS}</style></head>"
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
        field = ("width:100%;padding:9px;border-radius:6px;border:1px solid #2f5488;"
                 "background:#0d1320;color:#e6e9ef;margin-bottom:10px")
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
        "<h2 style='border:none;margin-top:0'>Register dashboard</h2>"
        f"{inner}</div>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Register</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def status_page():
    body = (
        "<div id='limit'></div>"
        "<h2>Active Workers <span id='wc' class='muted small'></span></h2>"
        "<div id='workers'>loading&hellip;</div>"
        "<h2>GPU</h2><div id='gpu'>loading&hellip;</div>"
        "<h2>Job Manager &mdash; Active Jobs</h2><div id='jobs'>loading&hellip;</div>"
        "<p class='muted small' id='updated'></p>"
    )
    return _shell("Infra Status", body, _STATUS_JS)


def history_page():
    body = (
        "<h2>Job History</h2>"
        "<div class='two'>"
        "<div><div class='card'><div style='display:flex;align-items:center;gap:10px;margin-bottom:8px'>"
        "<button id='prev'>&#8592;</button><b id='mlabel'></b><button id='next'>&#8594;</button></div>"
        "<div id='cal' class='cal'></div>"
        "<p class='muted small'>Click a day to list what was launched.</p></div>"
        "<div id='dayview'></div></div>"
        "<div id='detail'><div class='card muted'>Select a job or task to see details.</div></div>"
        "</div>"
    )
    return _shell("Job History", body, _HISTORY_JS)


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
    d.appendChild(hd); d.appendChild(el('div',null,m.subject||''));
    if(m.snippet) d.appendChild(el('div','small muted',m.snippet)); conv.appendChild(d);});
  c.appendChild(conv);
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
  var wt=el('table'); wt.innerHTML='<tr><th>worker</th><th>state</th><th>description</th>'+
    '<th>requester</th><th class=nowrap>mail</th><th>rl</th></tr>';
  s.workers.forEach(function(w){var tr=el('tr');
    tr.appendChild(td(w.name,'mono'));
    var st=el('td'); var cls=w.state==='running'?'b-running':(w.state==='failed'?'b-failed':
      (String(w.state).indexOf('wait')>=0?'b-parked':'b-done'));
    st.appendChild(el('span','badge '+cls, w.state_label||w.state)); tr.appendChild(st);
    tr.appendChild(td(w.desc||'',''));
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
  for(var i=0;i<start;i++) cal.appendChild(el('div','day empty'));
  for(var d=1; d<=dim; d++){
    var key=cur.getFullYear()+'-'+String(cur.getMonth()+1).padStart(2,'0')+'-'+String(d).padStart(2,'0');
    var n=counts[key]||0; var cell=el('div','day'+(n?' has':'')+(key===selDay?' sel':''));
    cell.appendChild(el('div','n',String(d)));
    if(n) cell.appendChild(el('div','c',String(n)));
    (function(k){cell.onclick=function(){selDay=k; draw(); loadDay(k);};})(key);
    cal.appendChild(cell);
  }
}
async function loadDay(day){
  var dv=document.getElementById('dayview'); dv.innerHTML='<div class="card muted">loading…</div>';
  var data=await getJSON('/api/history/day?date='+day); if(!data) return;
  dv.innerHTML=''; var c=el('div','card');
  c.appendChild(el('h2',null,'Launched on '+day));
  // tasks
  if(data.tasks.length){ c.appendChild(el('div','muted small','TASKS'));
    var tt=el('table'); tt.innerHTML='<tr><th>task</th><th>title</th></tr>';
    data.tasks.forEach(function(t){var tr=el('tr'); tr.style.cursor='pointer';
      tr.appendChild(td('#'+t.task_id,'mono')); tr.appendChild(td(t.title,''));
      tr.onclick=function(){showTask(t.task_id);}; tt.appendChild(tr);});
    c.appendChild(tt);}
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
  if(!data.tasks.length && !data.jobs.length) c.appendChild(el('div','muted','Nothing launched.'));
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
    return _shell("Task Lineage", body, _LINEAGE_JS)


_LINEAGE_JS = _COMMON_JS + r"""
getJSON('/api/status').then(function(s){ if(s) daemonsBar(s.daemons); });
var DOT={ 'explicit-chain':'bh','parent-field':'bh','followup-phrase':'bm','subject-thread':'bl' };
var BLABEL={ 'explicit-chain':'explicit chain','parent-field':'parent_task field',
  'followup-phrase':'follow-up phrasing','subject-thread':'same subject' };
function nodeLi(n){
  var li=el('li');
  var row=el('div','tnode');
  row.appendChild(el('span','cbar '+(DOT[n.basis]||'broot')));
  var a=el('a','tlink','#'+n.task_id+'  '+(n.title||''));
  a.href='javascript:void(0)'; a.title=n.title||'';
  a.onclick=(function(id){return function(){showTask(id);};})(n.task_id);
  row.appendChild(a);
  if(n.basis) row.appendChild(el('span','bchip',BLABEL[n.basis]||n.basis));
  li.appendChild(row);
  if(n.children && n.children.length){
    var ul=el('ul','tree-ul');
    n.children.forEach(function(c){ ul.appendChild(nodeLi(c)); });
    li.appendChild(ul);
  }
  return li;
}
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
    var ul=el('ul','tree-ul root'); ul.appendChild(nodeLi(t)); d.appendChild(ul);
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
