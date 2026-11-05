"""In-chat views for MCP Apps hosts (Claude and others that support `text/html;profile=mcp-app`).

Each view is a self-contained HTML page with no external requests. It talks to
the host over postMessage (JSON-RPC): `ui/initialize`, then waits for the
`ui/notifications/tool-result` carrying the tool's structuredContent, and
renders it with DOM calls only (never innerHTML with data), so candidate text
can't inject markup. Hosts without MCP Apps just show the tool's text content.
"""
from __future__ import annotations

import json

from . import __version__

MIME = "text/html;profile=mcp-app"
URIS = {
    "candidate": "ui://talent-search/candidate.html",
    "results": "ui://talent-search/search-results.html",
    "pipeline": "ui://talent-search/pipeline-board.html",
    "today": "ui://talent-search/action-items.html",
}

_CSS = r"""
:root{--bg:transparent;--surface:#fff;--text:#1c1c1a;--muted:#6b6b66;--line:#e4e3de;--accent:#2f5bd3;--mark:#fff1a8;--chip:#f0efe9}
:root[data-theme=dark]{--surface:#1f1f1d;--text:#ecebe6;--muted:#a3a29b;--line:#33322f;--accent:#7c9cff;--mark:#5a4b00;--chip:#2a2a27}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--surface:#1f1f1d;--text:#ecebe6;--muted:#a3a29b;--line:#33322f;--accent:#7c9cff;--mark:#5a4b00;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;padding:4px;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:8px}
.muted{color:var(--muted)}.small{font-size:12px}h1{font-size:17px;margin:0}h2{font-size:13px;margin:12px 0 4px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.chip{background:var(--chip);border-radius:999px;padding:1px 8px;font-size:12px}
mark{background:var(--mark);color:inherit;border-radius:2px}a,button.link{color:var(--accent);cursor:pointer;background:none;border:0;padding:0;font:inherit;text-decoration:underline}
.row{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.board{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}.col{min-width:150px;flex:1;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px}
.col h3{font-size:12px;margin:0 0 6px;display:flex;justify-content:space-between}.pc{border:1px solid var(--line);border-radius:8px;padding:6px 8px;margin-bottom:6px;font-size:13px}
ul{margin:4px 0;padding-left:18px}.kind{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.col.closed{opacity:.6;min-width:120px;flex:.6}.empty{color:var(--muted);padding:12px}.click{cursor:pointer}.click:hover{border-color:var(--accent)}
.contact{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:6px;font-size:13px}
.badge{display:inline-block;margin-left:8px;background:var(--chip);color:var(--muted);border-radius:999px;padding:1px 8px;font-size:12px;font-weight:600;vertical-align:middle}
"""

_JS = r"""
const V=__VERSION__;let nid=1;const pend={};
function post(m){window.parent.postMessage(m,"*")}
function req(method,params){const id=nid++;return new Promise(r=>{pend[id]=r;post({jsonrpc:"2.0",id,method,params})})}
function note(method,params){post({jsonrpc:"2.0",method,params:params||{}})}
function theme(ctx){if(ctx&&ctx.theme)document.documentElement.dataset.theme=ctx.theme}
function el(tag,attrs,...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs||{})){if(v==null)continue;if(k==="class")e.className=v;else if(k==="onclick")e.addEventListener("click",v);else e.setAttribute(k,v)}
 for(const k of kids.flat()){if(k==null||k===false)continue;e.append(k instanceof Node?k:document.createTextNode(String(k)))}return e}
function marked(s){const out=[];String(s||"").split("**").forEach((p,i)=>out.push(i%2?el("mark",{},p):p));return out}
function openApp(url){if(!url)return;req("ui/open-link",{url}).catch(()=>{})}
function safeUrl(u){try{const x=new URL(String(u||""));return /^https?:$/.test(x.protocol)?x:null}catch(e){return null}}
function profileLink(u,fallback){const x=safeUrl(u);if(!x)return null;const li=/(^|\.)linkedin\.com$/i.test(x.hostname);
 return el("button",{class:"link",onclick:()=>openApp(x.href)},li?"LinkedIn":(fallback||"Profile"))}
function openCard(cls,url,...kids){const e=el("div",{class:cls+(url?" click":""),title:url?"Open in Talent Search":null},...kids);
 if(url)e.addEventListener("click",ev=>{if(!(ev.target instanceof HTMLButtonElement))openApp(url)});return e}
function appLink(url,label){return url?el("button",{class:"link small",onclick:()=>openApp(url)},label||"Open in Talent Search"):null}
function day(iso){if(!iso)return"";const d=new Date(iso);return isNaN(d)?iso:d.toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"})}
function when(iso){if(!iso)return"";const d=new Date(iso);return isNaN(d)?iso:d.toLocaleString(undefined,{weekday:"short",month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})}
const root=()=>document.getElementById("root");
window.addEventListener("message",ev=>{if(ev.source!==window.parent)return;const m=ev.data;if(!m||m.jsonrpc!=="2.0")return;
 if(m.id!=null&&!m.method&&pend[m.id]){pend[m.id](m.result||{});delete pend[m.id];return}
 if(m.method==="ui/notifications/tool-result"){const d=(m.params&&m.params.structuredContent)||null;root().replaceChildren(d?render(d):el("p",{class:"empty"},"Nothing to show."))}
 else if(m.method==="ui/notifications/host-context-changed")theme(m.params);
 else if(m.method==="ui/resource-teardown"&&m.id!=null)post({jsonrpc:"2.0",id:m.id,result:{}});
});
new ResizeObserver(()=>note("ui/notifications/size-changed",{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight})).observe(document.documentElement);
req("ui/initialize",{protocolVersion:"2026-01-26",clientInfo:{name:"talent-search",version:V},appCapabilities:{}}).then(r=>{theme(r.hostContext);note("ui/notifications/initialized",{})});
"""

_RENDER = {
    "candidate": r"""
const FIELD={current_title:"title",current_company:"company",linkedin_url:"LinkedIn",tags:"skills",work_history:"work history",last_contacted_at:"last contacted"};
function savedText(s){if(s==null||s===false)return null;if(typeof s==="string")return s;
 const changed=s.changed||s.fields||s.updated||[];
 if(s.created||s.status==="saved"||s.status==="created"||s.status==="new")return"Saved";
 if(Array.isArray(changed)&&changed.length)return"Updated: "+changed.map(f=>FIELD[f]||String(f).replace(/_/g," ")).join(", ");
 if(s.status==="unchanged"||s.status==="same")return"Already saved, nothing new";
 return s.label||s.text||(s.status?String(s.status):null)}
function render(c){
 if(!c.name&&c.candidate)c=Object.assign({},c.candidate,{saved_status:c.saved_status,app_url:c.app_url||c.candidate.app_url});
 const saved=savedText(c.saved_status);
 const sub=[c.current_title,c.current_company&&("at "+c.current_company)].filter(Boolean).join(" ")+(c.location?" · "+c.location:"");
 const card=el("div",{class:"card"},
  el("div",{class:"row"},el("h1",{},c.name,saved?el("span",{class:"badge"},saved):null),appLink(c.app_url)),
  sub?el("div",{class:"muted"},sub):null,
  el("div",{class:"contact"},c.email?el("span",{},c.email):null,c.phone?el("span",{},c.phone):null,
   profileLink(c.linkedin_url),profileLink(c.website,"Website"),c.source?el("span",{class:"muted"},"Source: "+c.source):null),
  (c.tags&&c.tags.length)?el("div",{class:"chips"},c.tags.map(t=>el("span",{class:"chip"},t))):null,
  c.about?el("p",{},c.about):null,
  el("div",{class:"small muted"},"Last contacted: "+(c.last_contacted_at?day(c.last_contacted_at):"never")+" · Updated "+day(c.updated_at)),
 );
 if(c.pipelines&&c.pipelines.length)card.append(el("h2",{},"Jobs"),el("ul",{},c.pipelines.map(p=>el("li",{},p.job_title+": ",el("b",{},p.stage)))));
 if(c.work_history&&c.work_history.length)card.append(el("h2",{},"Work history"),el("ul",{},c.work_history.map(w=>el("li",{},[w.title,w.company&&("at "+w.company)].filter(Boolean).join(" ")+((w.start||w.end)?" ("+(w.start||"")+" to "+(w.end||"now")+")":"")))));
 if(c.timeline&&c.timeline.length)card.append(el("h2",{},"Recent activity"),el("ul",{},c.timeline.slice(0,6).map(t=>el("li",{},el("span",{class:"kind"},t.kind)," ",day(t.at)+(t.body?": "+t.body:"")))));
 return card}""",
    "results": r"""
function render(d){
 const wrap=el("div",{});
 const exact=d.exact_total!=null?d.exact_total:d.total;
 const head=d.closest?"No exact matches. Closest matches:":d.exact?"Exact match":exact+(exact===1?" match":" matches");
 if(d.corrected&&d.suggestion)wrap.append(el("div",{class:"small"},"Showing results for ",el("b",{},d.suggestion)));
 wrap.append(el("div",{class:"row small muted"},el("span",{},head),appLink(d.app_url,"Open search in Talent Search")));
 if(!d.results.length)wrap.append(el("p",{class:"empty"},"No saved candidates match. Try fewer or different words."));
 let shown=false;
 for(const c of d.results){const sub=[c.current_title,c.current_company&&("at "+c.current_company)].filter(Boolean).join(" ")+(c.location?" · "+c.location:"");
  if(c.closest&&!d.closest&&!shown){shown=true;wrap.append(el("h2",{},"Closest matches, with some of the words"))}
  wrap.append(openCard("card",c.app_url,el("div",{class:"row"},el("b",{},c.name),c.closest?el("span",{class:"badge"},"Closest match"):null,profileLink(c.linkedin_url)),sub?el("div",{class:"muted small"},sub):null,
   c.reason?el("div",{class:"small muted"},c.reason):null,
   c.match?el("div",{class:"small"},marked(c.match)):null,(c.tags&&c.tags.length)?el("div",{class:"chips"},c.tags.slice(0,6).map(t=>el("span",{class:"chip"},t))):null))}
 if(d.suggestion&&!d.corrected)wrap.append(el("p",{class:"small"},"Did you mean ",el("b",{},d.suggestion),"?"));
 return wrap}""",
    "pipeline": r"""
function render(d){
 const wrap=el("div",{});
 wrap.append(el("div",{class:"row"},el("h1",{},d.job.title+(d.job.company?" · "+d.job.company:"")),appLink(d.app_url)));
 const n=d.counts||{active:d.stages.filter(s=>s.outcome==="open").reduce((a,s)=>a+s.candidates.length,0)};
 const sum=[n.active+" in the running"];if(n.hired!=null)sum.push(d.openings>1?n.hired+" of "+d.openings+" hired":n.hired+" hired");if(n.out)sum.push(n.out+" out");
 wrap.append(el("div",{class:"small muted"},sum.join(" · ")+(d.job.status&&d.job.status!=="open"?" · "+d.job.status.replace("_"," "):"")));
 const board=el("div",{class:"board"});
 for(const s of d.stages){board.append(el("div",{class:"col"+(s.outcome!=="open"?" closed":"")},el("h3",{},el("span",{},s.name+(s.outcome==="hired"?" (hired)":s.outcome==="lost"?" (out)":"")),el("span",{class:"muted"},s.candidates.length)),
  s.candidates.length?s.candidates.map(c=>openCard("pc",c.app_url,el("b",{},c.name),c.current_title?el("div",{class:"muted small"},c.current_title):null,
   el("div",{class:"muted small"},(c.days_in_stage!=null?c.days_in_stage+(c.days_in_stage===1?" day":" days")+" in stage":"since "+day(c.since))),
   c.next_interview?el("div",{class:"small"},"Interview "+when(c.next_interview)):null)):el("div",{class:"muted small"},"Empty")))}
 wrap.append(board);return wrap}""",
    "today": r"""
const LABEL={interview:"Interview",suggestion:"Suggestion",stuck:"Stuck",thin:"Needs more candidates",stale:"May be out of date"};
function render(d){
 const wrap=el("div",{});
 wrap.append(el("div",{class:"row"},el("h1",{},d.items.length?d.items.length+" thing"+(d.items.length===1?"":"s")+" to do":"Nothing needs you today"),appLink(d.app_url)));
 const card=i=>el("div",{class:"card"},el("div",{class:"kind"},LABEL[i.kind]||i.kind),el("div",{},i.title),
  (i.kind==="interview"&&i.when)?el("div",{class:"small"},when(i.when)):null,i.due_text?el("div",{class:"small"+(i.overdue?"":" muted")},i.due_text):null,
  i.detail?el("div",{class:"small muted"},i.detail):null);
 for(const i of d.items)wrap.append(card(i));
 if(d.next_week&&d.next_week.length){wrap.append(el("h2",{},"Next week"));for(const i of d.next_week)wrap.append(card(i))}
 return wrap}""",
}


def view_html(name: str) -> str:
    js = _JS.replace("__VERSION__", json.dumps(__version__))
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<style>{_CSS}</style></head><body><div id='root'><p class='empty'>Loading…</p></div>"
        f"<script>{_RENDER[name]}\n{js}</script></body></html>"
    )


def tool_meta(name: str) -> dict:
    return {"ui": {"resourceUri": URIS[name]}}
