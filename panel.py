"""VerseVPS web dashboard. Made by Verseedit.

Runs inside the same process as the Discord bot (see main.py), so it shares the VPS locks with it.
Login = the username + password the bot sent to the owner by DM.
"""
import logging
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps
from urllib.parse import quote

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, session
from flask.sessions import SecureCookieSessionInterface
from jinja2 import DictLoader

import core as C

log = logging.getLogger("versevps.web")

app = Flask(__name__)
app.secret_key = C.secret_key()
app.config.update(
    SESSION_COOKIE_NAME="verse_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=C.MAX_UPLOAD_MB * 1024 * 1024 + 1024 * 1024,
)
if C.trust_proxy():  # behind Cloudflare Tunnel / Replit / Codespaces ...
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


class _Sessions(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):  # "Secure" cookie automatically when served over https
        return request.is_secure


app.session_interface = _Sessions()

# ---------------------------------------------------------------- security
_fails = defaultdict(deque)
_JSON_LIMIT = C.MAX_EDIT * 2 + 4096  # every JSON endpoint; only /api/upload may be big


def client_ip():
    if C.trust_proxy():
        return request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"
    return request.remote_addr or "?"


def limited(key):
    q, now = _fails[key], time.time()
    while q and now - q[0] > 300:
        q.popleft()
    if not q:
        _fails.pop(key, None)
    return len(q) >= 8


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.context_processor
def inject():
    return {"csrf": csrf_token(), "credit": C.CREDIT}


@app.before_request
def guard():
    # reject huge non-upload bodies BEFORE Flask reads them into memory
    if request.path != "/api/upload" and (request.content_length or 0) > _JSON_LIMIT:
        abort(413)
    if request.method == "POST":
        expected = session.get("csrf")
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
        if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
            abort(400, "CSRF check failed - reload the page")


@app.after_request
def headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
    return resp


def current_account():
    acct = C.get_account(session.get("did") or 0)
    if not acct or session.get("pwh") != C.session_tag(acct):  # unknown account or password was changed
        return None
    return acct


def account_required(fn):
    """Logged-in account (no specific VPS needed)."""
    @wraps(fn)
    def wrapper(*a, **kw):
        acct = current_account()
        if not acct:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify(error="Not logged in"), 401
            return redirect("/login")
        g.acct = acct
        return _guarded(fn, a, kw)
    return wrapper


def vps_required(fn):
    """Logged-in account + one of ITS VPS, chosen by the X-VPS-ID header / ?vps= (default: the first one)."""
    @wraps(fn)
    def wrapper(*a, **kw):
        acct = current_account()
        if not acct:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify(error="Not logged in"), 401
            return redirect("/login")
        g.acct = acct
        mine = C.for_user(acct["discord_id"])
        want = (request.headers.get("X-VPS-ID") or request.args.get("vps") or "").strip().lower()
        row = next((r for r in mine if r["id"] == want), None) if want else (mine[0] if mine else None)
        if not row:
            return jsonify(error="You have no such VPS. Use !deploy in Discord."), 400
        g.vps = row
        return _guarded(fn, a, kw)
    return wrapper


def _guarded(fn, a, kw):
    try:
        return fn(*a, **kw)
    except C.Busy as exc:
        return jsonify(error=str(exc)), 409
    except (C.VPSError, ValueError) as exc:
        return jsonify(error=str(exc)[:300]), 400
    except Exception:  # noqa: BLE001
        log.exception("error in %s", request.path)
        return jsonify(error="Internal error (is the VPS running?)"), 500


# -------------------------------------------------------------------- auth
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = client_ip()
        user = (request.form.get("username") or "").strip().lower()[:64]
        if limited(ip) or limited("u:" + user):
            error = "Too many attempts. Try again in a few minutes."
        else:
            acct = C.check_login(user, request.form.get("password", ""))
            if acct:
                session.clear()
                session.permanent = True
                session["did"], session["pwh"] = acct["discord_id"], C.session_tag(acct)
                return redirect("/")
            _fails[ip].append(time.time())
            _fails["u:" + user].append(time.time())
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


@app.route("/")
@account_required
def index():
    rows = C.for_user(g.acct["discord_id"])
    return render_template("panel.html", acct=g.acct, vlist=[C.public_view(r) for r in rows], info=C.backend_info(),
                           hostname=C.VPS_HOSTNAME)


# --------------------------------------------------------------------- API
@app.get("/api/vps")
@account_required
def api_vps():
    return jsonify(vps=[C.public_view(r) for r in C.for_user(g.acct["discord_id"])])


@app.get("/api/ssh")
@vps_required
def api_ssh():
    return jsonify(ssh=C.ssh_info(g.vps), username=g.acct["username"])


@app.post("/api/sshx")
@vps_required
def api_sshx():
    return jsonify(link=C.sshx_link(g.vps))  # starts sshx inside the VPS; up to ~45 s


@app.get("/api/stats")
@vps_required
def api_stats():
    return jsonify(C.stats(g.vps))


@app.post("/api/power")
@vps_required
def api_power():
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "reinstall":
        C.reinstall(g.vps)
    elif action in ("start", "stop", "restart"):
        C.power(g.vps, action)
    else:
        return jsonify(error="Invalid action"), 400
    return jsonify(ok=True)


@app.post("/api/exec")
@vps_required
def api_exec():
    d = request.get_json(silent=True) or {}
    cmd = str(d.get("cmd", ""))[:4000]
    if not cmd.strip():
        return jsonify(out="", cwd=d.get("cwd") or "", code=0)
    out, cwd, rc = C.run_cmd(g.vps, cmd, d.get("cwd"))
    return jsonify(out=out, cwd=cwd, code=rc)


@app.get("/api/files")
@vps_required
def api_files():
    path, entries = C.fs_ls(g.vps, request.args.get("path"))
    return jsonify(path=path, entries=entries)


@app.get("/api/file")
@vps_required
def api_file_read():
    path, content = C.fs_read_text(g.vps, request.args.get("path"))  # size is checked BEFORE reading
    return jsonify(path=path, content=content)


@app.post("/api/file")
@vps_required
def api_file_write():
    d = request.get_json(silent=True) or {}
    C.fs_write_text(g.vps, d.get("path"), str(d.get("content", "")))
    return jsonify(ok=True)


@app.get("/api/download")
@vps_required
def api_download():
    name, size, gen = C.fs_open_download(g.vps, request.args.get("path"))  # streamed in 64KB chunks
    return Response(gen, mimetype="application/octet-stream", headers={
        "Content-Length": str(size),
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})


@app.post("/api/upload")
@vps_required
def api_upload():
    folder = request.form.get("path") or None
    limit = C.quota_left(g.vps)
    saved = []
    for f in request.files.getlist("files"):
        fname = (f.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not fname or fname in (".", ".."):
            continue
        base = C.norm(folder or C.backend_for(g.vps).home(g.vps)).rstrip("/")
        n = C.fs_write_stream(g.vps, f"{base}/{fname}", f.stream, limit)  # chunked: no full-file RAM copy
        if limit is not None:
            limit = max(0, limit - n)
        saved.append(fname)
    return jsonify(ok=True, saved=saved)


@app.post("/api/delete")
@vps_required
def api_delete():
    C.fs_delete(g.vps, (request.get_json(silent=True) or {}).get("path"))
    return jsonify(ok=True)


@app.post("/api/mkdir")
@vps_required
def api_mkdir():
    C.fs_mkdir(g.vps, (request.get_json(silent=True) or {}).get("path"))
    return jsonify(ok=True)


# --------------------------------------------------------------- templates
BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf" content="{{ csrf }}"><title>{% block title %}VerseVPS{% endblock %}</title>
<style>
:root{--bg:#0e1116;--card:#161b22;--line:#2a313c;--tx:#e6edf3;--mut:#8b949e;--acc:#5865f2;--ok:#3fb950;--bad:#f85149;--warn:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,sans-serif}
a{color:var(--acc);text-decoration:none}.wrap{max-width:1000px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(230px,1fr))}
input,textarea,button{font:inherit;color:var(--tx);background:#0b0f14;border:1px solid var(--line);border-radius:8px;padding:9px 12px}
button{cursor:pointer;background:var(--acc);border-color:var(--acc)}button.sec{background:#21262d;border-color:var(--line)}
button.bad{background:var(--bad);border-color:var(--bad)}button:disabled{opacity:.5;cursor:wait}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.sp{flex:1}.mut{color:var(--mut)}
.badge{padding:2px 9px;border-radius:99px;font-size:12px;background:#21262d}.Running{color:var(--ok)}.Stopped{color:var(--bad)}
.err{color:var(--bad)}h1,h2{margin:.2em 0}.note{border:1px solid var(--warn);color:var(--warn);border-radius:10px;padding:8px 12px;margin:10px 0;font-size:13px}
footer{text-align:center;color:var(--mut);font-size:12px;padding:18px}
</style></head><body{% block bodyattr %}{% endblock %}>{% block body %}{% endblock %}
<footer>VerseVPS &middot; {{ credit }}</footer></body></html>"""

LOGIN = """{% extends "base.html" %}{% block body %}
<div class="wrap" style="max-width:420px;margin-top:10vh"><div class="card">
<h1>🖥️ VerseVPS</h1><p class="mut">Log in with the username and password the bot sent to your Discord DMs.</p>
<form method="post" class="row" style="flex-direction:column;align-items:stretch">
<input type="hidden" name="csrf" value="{{ csrf }}">
<input name="username" placeholder="Username" autocomplete="username" autocapitalize="off" required autofocus>
<input type="password" name="password" placeholder="Password" autocomplete="current-password" required>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<button>Log in</button></form></div></div>{% endblock %}"""

PANEL = r"""{% extends "base.html" %}{% block title %}VerseVPS dashboard{% endblock %}
{% block body %}
<style>
.tabs{display:flex;gap:6px;margin:12px 0}.tab{background:#21262d;border-color:var(--line)}.tab.on{background:var(--acc)}
.bar{height:8px;background:#21262d;border-radius:9px;overflow:hidden;margin:6px 0 2px}.bar i{display:block;height:100%;background:var(--acc);width:0}
#term{background:#000;border-radius:10px;padding:10px;height:60vh;overflow:auto;font:13px/1.4 ui-monospace,Menlo,monospace}
#tout{white-space:pre-wrap;word-break:break-all;margin:0}#tline{display:flex;gap:6px}#tin{flex:1;background:transparent;border:0;padding:0;outline:0;font:inherit;color:#fff}
table{width:100%;border-collapse:collapse}td,th{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left}
tr.f:hover{background:#1c222b}.lnk{cursor:pointer;color:var(--acc)}
#modal{position:fixed;inset:0;background:#000a;display:none;padding:12px}#modal .card{max-width:900px;margin:auto;height:100%;display:flex;flex-direction:column}
#edtxt{flex:1;width:100%;font:13px ui-monospace,monospace;resize:none;margin:8px 0}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#21262d;border:1px solid var(--line);padding:10px 16px;border-radius:10px;display:none;z-index:9}
</style>
<div class="wrap">
{% if not vlist %}
<div class="row"><h1>Hello {{ acct.username }}</h1><span class="sp"></span>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="sec">Log out</button></form></div>
<div class="card" style="margin-top:12px">You have no VPS yet. Run <b>!deploy</b> in Discord, then reload this page.</div></div>
{% else %}
<div class="row"><h1>{{ acct.username }}</h1>
<select id="vsel" style="background:#0b0f14;color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
{% for v in vlist %}<option value="{{ v.id }}">{{ v.id }} &middot; {{ v.ram }} / {{ v.disk }} / {{ v.cpu }} vCPU</option>{% endfor %}</select>
<span id="st" class="badge">…</span><span class="mut" id="ipv"></span><span class="sp"></span>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="sec">Log out</button></form></div>
<div class="mut" id="meta"></div>
{% for n in info.notes %}<div class="note">{{ n }}</div>{% endfor %}
<div class="tabs"><button class="tab on" data-t="ov">Overview</button><button class="tab" data-t="tm">Terminal</button><button class="tab" data-t="fm">Files</button><button class="tab" data-t="ac">Access</button></div>

<section id="ov"><div class="card"><div class="row">
<button onclick="power('start')">▶ Start</button><button class="sec" onclick="power('stop')">■ Stop</button>
<button class="sec" onclick="power('restart')">⟳ Restart</button><span class="sp"></span>
<button class="bad" onclick="power('reinstall')">Reinstall</button></div></div>
<div class="grid" style="margin-top:12px">
<div class="card"><b>CPU</b> <span id="cpuv" class="mut"></span><div class="bar"><i id="cpub"></i></div></div>
<div class="card"><b>RAM</b> <span id="ramv" class="mut"></span><div class="bar"><i id="ramb"></i></div></div>
<div class="card"><b>SSD</b> <span id="dskv" class="mut"></span><div class="bar"><i id="dskb"></i></div></div>
<div class="card"><b>Uptime</b> <div id="upv" class="mut">-</div><div class="mut" id="osv"></div></div></div></section>

<section id="tm" hidden><div id="term" onclick="document.getElementById('tin').focus()"><pre id="tout"></pre>
<div id="tline"><span id="tp" style="color:#3fb950"></span><input id="tin" autocomplete="off" autocapitalize="off" spellcheck="false"></div></div>
<p class="mut">One command at a time (5 min limit). Interactive programs (vim/top) need SSH or the SSHX terminal (Access tab).
Start background jobs like this: <code>nohup mycmd &gt;/dev/null 2&gt;&amp;1 &amp;</code></p></section>

<section id="ac" hidden><div class="card">
<h2>SSH (root)</h2><div id="sshbox" class="mut">Loading…</div></div>
<div class="card" style="margin-top:12px"><h2>SSHX web terminal</h2>
<p class="mut">Starts <b>sshx.io</b> inside your VPS and gives you a link to a full terminal in any browser. Anyone with the link is root, keep it private.</p>
<button id="sshxbtn" onclick="sshx()">Start SSHX terminal</button><div id="sshxout" style="margin-top:10px;word-break:break-all"></div></div></section>

<section id="fm" hidden><div class="card"><div class="row">
<input id="fpath" class="sp"><button onclick="ls(document.getElementById('fpath').value)">Go</button>
<button class="sec" onclick="ls(parent(fp))">⬆ Up</button><button class="sec" onclick="mkdir()">+ Folder</button>
<button class="sec" onclick="newFile()">+ File</button><button onclick="document.getElementById('up').click()">⬆ Upload</button>
<input type="file" id="up" multiple hidden></div>
<div style="overflow-x:auto;margin-top:8px"><table><thead><tr><th>Name</th><th>Size</th><th>Modified</th><th></th></tr></thead><tbody id="fl"></tbody></table></div></div></section>
</div>
<div id="modal"><div class="card"><div class="row"><b id="edname" class="sp"></b><button onclick="saveFile()">Save</button>
<button class="sec" onclick="document.getElementById('modal').style.display='none'">Close</button></div><textarea id="edtxt" spellcheck="false"></textarea></div></div>
<div id="toast"></div>
{% endif %}
<script>window.VLIST={{ vlist|tojson }};window.HOST={{ hostname|tojson }};</script>
{% raw %}<script>
if(!window.VLIST.length)throw 0;
let CUR=window.VLIST[0].id;
const CSRF=document.querySelector('meta[name=csrf]').content;
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function call(path,body){
  const o=body===undefined?{headers:{'X-VPS-ID':CUR}}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF,'X-VPS-ID':CUR},body:JSON.stringify(body)};
  const r=await fetch('/api/'+path,o);
  if(r.status===401){location='/login';return}
  let d={};try{d=await r.json()}catch(e){}
  if(!r.ok)throw new Error(d.error||r.statusText);return d}
function toast(m,bad){const t=$('#toast');t.textContent=m;t.style.color=bad?'#f85149':'#e6edf3';t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',4000)}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x===b));
  ['ov','tm','fm','ac'].forEach(id=>document.getElementById(id).hidden=id!==b.dataset.t);
  if(b.dataset.t==='tm')$('#tin').focus();if(b.dataset.t==='fm'&&!fp)ls('');if(b.dataset.t==='ac')loadSsh()});
const fb=n=>{if(n==null)return'-';const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?1:0)+' '+u[i]};
const bar=(id,p)=>$('#'+id).style.width=Math.max(0,Math.min(100,p||0))+'%';
async function stats(){try{const d=await call('stats');if(!d)return;
  const st=$('#st');st.textContent=d.status;st.className='badge '+d.status;$('#ipv').textContent=d.ip||'';
  $('#cpuv').textContent=d.cpu_pct+'% of '+d.cpu_cores+' vCPU';bar('cpub',d.cpu_pct);
  $('#ramv').textContent=fb(d.mem_used)+' / '+fb(d.mem_limit);bar('ramb',d.mem_used/d.mem_limit*100);
  $('#dskv').textContent=fb(d.disk_used)+' / '+fb(d.disk_limit);bar('dskb',d.disk_used/d.disk_limit*100);
  const u=d.uptime;$('#upv').textContent=u==null?'-':Math.floor(u/86400)+'d '+Math.floor(u%86400/3600)+'h '+Math.floor(u%3600/60)+'m';
  $('#osv').textContent=d.os}catch(e){}}
setInterval(stats,4000);stats();
async function power(a){
  if(a==='reinstall'&&!confirm('Reinstall wipes ALL data on this VPS. Continue?'))return;
  const bs=document.querySelectorAll('#ov button');bs.forEach(b=>b.disabled=true);toast(a+'…');
  try{await call('power',{action:a});toast('Done')}
  catch(e){toast(e.message,true)}finally{bs.forEach(b=>b.disabled=false);stats()}}
// ---- terminal
let cwd='',hist=[],hi=0;const tout=$('#tout'),tin=$('#tin');
const V=()=>window.VLIST.find(v=>v.id===CUR);
const setp=()=>{const v=V();$('#tp').textContent=v.root?('root@'+window.HOST+':'+((!cwd||cwd==='/root')?'~':cwd)+'# '):((cwd||'~')+' $ ')};setp();
function println(t){tout.textContent+=t;$('#term').scrollTop=1e9}
tin.addEventListener('keydown',async e=>{
  if(e.key==='ArrowUp'){if(hi>0)tin.value=hist[--hi];e.preventDefault()}
  else if(e.key==='ArrowDown'){if(hi<hist.length-1){tin.value=hist[++hi]}else{hi=hist.length;tin.value=''}e.preventDefault()}
  else if(e.key==='Enter'){const c=tin.value;tin.value='';println($('#tp').textContent+c+'\n');
    if(!c.trim())return;hist.push(c);hi=hist.length;
    if(c.trim()==='clear'){tout.textContent='';return}
    tin.disabled=true;
    try{const d=await call('exec',{cmd:c,cwd});println(d.out.replace(/^\n/,'')+(d.out.endsWith('\n')||!d.out?'':'\n'));cwd=d.cwd;setp()}
    catch(err){println('[error] '+err.message+'\n')}finally{tin.disabled=false;tin.focus()}}});
// ---- files
let fp='';const parent=p=>p.replace(/\/[^\/]*\/?$/,'')||'/';
const join=(a,b)=>(a==='/'?'':a)+'/'+b;
async function ls(p,quiet){try{const d=await call('files?path='+encodeURIComponent(p||''));fp=d.path;$('#fpath').value=fp;
  $('#fl').innerHTML=d.entries.map((e,i)=>'<tr class="f"><td><span class="lnk" data-i="'+i+'">'+(e.type==='dir'?'📁':e.type==='link'?'🔗':'📄')+' '+esc(e.name)+'</span></td><td>'+(e.type==='dir'?'':fb(e.size))+'</td><td class="mut">'+esc(e.mtime)+'</td><td>'+(e.type==='dir'?'':'<span class="lnk" data-d="'+i+'">⬇</span> ')+'<span class="lnk" data-x="'+i+'">🗑</span></td></tr>').join('')||'<tr><td class="mut">Empty</td></tr>';
  $('#fl').onclick=async ev=>{const t=ev.target,E=d.entries;
    if(t.dataset.i!==undefined){const e=E[t.dataset.i],full=join(fp,e.name);
      if(e.type==='dir')return ls(full);
      if(e.type==='link'&&await ls(full,true))return;
      openFile(full)}
    else if(t.dataset.d!==undefined)window.open('/api/download?vps='+CUR+'&path='+encodeURIComponent(join(fp,E[t.dataset.d].name)));
    else if(t.dataset.x!==undefined){const n=E[t.dataset.x].name;if(confirm('Delete '+n+'?'))
      try{await call('delete',{path:join(fp,n)});ls(fp)}catch(e){toast(e.message,true)}}}}
  catch(e){if(!quiet)toast(e.message,true);return false}return true}
$('#fpath').addEventListener('keydown',e=>{if(e.key==='Enter')ls(e.target.value)});
let editing='';
async function openFile(p){try{const d=await call('file?path='+encodeURIComponent(p));editing=p;$('#edname').textContent=p;$('#edtxt').value=d.content;$('#modal').style.display='block'}catch(e){toast(e.message,true)}}
async function saveFile(){try{await call('file',{path:editing,content:$('#edtxt').value});toast('Saved');ls(fp)}catch(e){toast(e.message,true)}}
function newFile(){const n=prompt('New file name');if(!n)return;editing=join(fp,n);$('#edname').textContent=editing;$('#edtxt').value='';$('#modal').style.display='block'}
async function mkdir(){const n=prompt('Folder name');if(!n)return;try{await call('mkdir',{path:join(fp,n)});ls(fp)}catch(e){toast(e.message,true)}}
$('#up').onchange=e=>{const files=[...e.target.files];e.target.value='';if(!files.length)return;
  const f=new FormData();f.append('path',fp);for(const x of files)f.append('files',x);
  const x=new XMLHttpRequest();x.open('POST','/api/upload');x.setRequestHeader('X-CSRF-Token',CSRF);x.setRequestHeader('X-VPS-ID',CUR);
  x.upload.onprogress=ev=>{if(ev.lengthComputable)toast('Uploading '+Math.round(ev.loaded/ev.total*100)+'%')};
  x.onload=()=>{let d={};try{d=JSON.parse(x.responseText)}catch(e){}
    if(x.status===200){toast('Uploaded '+d.saved.length+' file(s)');ls(fp)}else toast(d.error||('Upload failed ('+x.status+')'),true)};
  x.onerror=()=>toast('Upload failed',true);x.send(f)};
// ---- vps switcher + access tab
function meta(){const v=V();$('#meta').textContent=(v.root?'root@'+window.HOST:v.os)+' \u00b7 '+v.ram+' RAM \u00b7 '+v.disk+' SSD \u00b7 '+v.cpu+' vCPU \u00b7 '+v.os+' \u00b7 '+v.backend}
meta();
$('#vsel').onchange=e=>{CUR=e.target.value;cwd='';fp='';tout.textContent='';setp();meta();$('#fl').innerHTML='';
  $('#sshxout').textContent='';$('#sshbox').textContent='Loading\u2026';stats();
  const t=document.querySelector('.tab.on').dataset.t;if(t==='fm')ls('');if(t==='ac')loadSsh()};
async function loadSsh(){try{const d=await call('ssh');const s=d.ssh;
  $('#sshbox').innerHTML=s?('<table><tr><td>Host / IP</td><td><b>'+esc(s.host)+'</b></td></tr><tr><td>Port</td><td><b>'+s.port+'</b></td></tr><tr><td>Username</td><td><b>root</b></td></tr><tr><td>Password</td><td>the password of your account <b>'+esc(d.username)+'</b></td></tr><tr><td>Command</td><td><code>'+esc(s.cmd)+'</code></td></tr></table>'):'This VPS mode has no SSH.'}
  catch(e){$('#sshbox').textContent=e.message}}
async function sshx(){const b=$('#sshxbtn');b.disabled=true;$('#sshxout').textContent='Starting\u2026 (up to 45 s)';
  try{const d=await call('sshx',{});const a=document.createElement('a');a.href=d.link;a.target='_blank';a.rel='noopener';a.textContent=d.link;$('#sshxout').textContent='';$('#sshxout').appendChild(a)}
  catch(e){$('#sshxout').textContent=e.message;$('#sshxout').className='err'}finally{b.disabled=false}}
</script>{% endraw %}{% endblock %}"""

app.jinja_loader = DictLoader({"base.html": BASE, "login.html": LOGIN, "panel.html": PANEL})


# ------------------------------------------------------------------- server
def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((C.PANEL_BIND, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def serve():
    port = next((p for p in range(C.PORT, C.PORT + 20) if _port_free(p)), C.PORT)
    C.ACTIVE_PORT = port
    C.write_status(port=port)

    def announce():
        C.write_status(panel=C.panel_url())
    threading.Thread(target=announce, daemon=True).start()
    log.info("Dashboard listening on %s:%s", C.PANEL_BIND, port)
    try:
        from waitress import serve as wserve
        wserve(app, host=C.PANEL_BIND, port=port, threads=16, ident="VerseVPS", channel_timeout=300,
               max_request_body_size=C.MAX_UPLOAD_MB * 1024 * 1024 + 2 * 1024 * 1024)
    except ImportError:
        from werkzeug.serving import make_server
        make_server(C.PANEL_BIND, port, app, threaded=True).serve_forever()
