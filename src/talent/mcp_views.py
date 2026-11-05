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
    "shortlist": "ui://talent-search/shortlist.html",
    "saved": "ui://talent-search/batch-save.html",
    "decide": "ui://talent-search/finalists.html",
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
.acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;align-items:center}
.btn{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--chip);color:var(--text);border-radius:7px;padding:3px 10px;cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn:disabled{opacity:.55;cursor:default}
input.reason{font:inherit;font-size:13px;border:1px solid var(--line);border-radius:7px;padding:3px 8px;background:var(--surface);color:var(--text);min-width:180px;flex:1}
.status{font-size:12px;margin-top:6px}
.badge{display:inline-block;margin-left:8px;background:var(--chip);color:var(--muted);border-radius:999px;padding:1px 8px;font-size:12px;font-weight:600;vertical-align:middle}
"""

_JS = r"""
const V=__VERSION__;let nid=1;const pend={};let HOST={},META={};
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
 if(m.id!=null&&!m.method&&pend[m.id]){pend[m.id](m.error?{isError:true,content:[{type:"text",text:String(m.error.message||"That didn't work.")}]}:(m.result||{}));delete pend[m.id];return}
 if(m.method==="ui/notifications/tool-result"){META=(m.params&&m.params._meta)||{};const d=(m.params&&m.params.structuredContent)||null;root().replaceChildren(d?render(d):el("p",{class:"empty"},"Nothing to show."))}
 else if(m.method==="ui/notifications/host-context-changed")theme(m.params);
 else if(m.method==="ui/resource-teardown"&&m.id!=null)post({jsonrpc:"2.0",id:m.id,result:{}});
});
new ResizeObserver(()=>note("ui/notifications/size-changed",{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight})).observe(document.documentElement);
req("ui/initialize",{protocolVersion:"2026-01-26",clientInfo:{name:"talent-search",version:V},appCapabilities:{}}).then(r=>{HOST=(r&&r.hostCapabilities)||{};theme(r.hostContext);note("ui/notifications/initialized",{})});
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
function person(p,extra){const sub=[p.current_title,p.current_company&&("at "+p.current_company)].filter(Boolean).join(" ")+(p.location?" · "+p.location:"");
 return el("div",{class:"card"},el("div",{class:"row"},el("b",{},p.name),appLink(p.app_url,"Open "+p.name+" in Talent Search")),
  sub?el("div",{class:"muted small"},sub):null,
  el("div",{class:"contact"},p.email?el("span",{},p.email):null,profileLink(p.linkedin_url)),extra||null)}
function notSaved(c){
 const wrap=el("div",{});
 wrap.append(el("div",{class:"card"},el("div",{class:"row"},el("h1",{},c.incoming_name||"Not saved",el("span",{class:"badge"},"Not saved"))),
  c.warning?el("p",{},c.warning):null));
 if(c.existing)wrap.append(el("h2",{},"Already saved"),person(c.existing));
 if(c.possible_duplicates&&c.possible_duplicates.length){wrap.append(el("h2",{},"Saved with this name"));
  for(const d of c.possible_duplicates){const diff=d.differences||[];
   wrap.append(person(d,diff.length?el("ul",{class:"small"},diff.map(x=>el("li",{},x.field+": saved ",el("b",{},x.saved),", yours ",el("b",{},x.yours)))):el("div",{class:"small muted"},"Nothing you sent differs from what's saved.")))}}
 return wrap}
function render(c){
 if(!c.name&&!c.candidate&&(c.conflict||c.needs_confirmation))return notSaved(c);
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
 if(c.pipelines&&c.pipelines.length)card.append(el("h2",{},"Jobs"),el("ul",{},c.pipelines.map(p=>el("li",{},p.job_title+(p.job_company?" ("+p.job_company+")":"")+": ",el("b",{},p.stage)))));
 if(c.work_history&&c.work_history.length)card.append(el("h2",{},"Work history"),el("ul",{},c.work_history.map(w=>el("li",{},[w.title,w.company&&("at "+w.company)].filter(Boolean).join(" ")+((w.start||w.end)?" ("+(w.start||"")+" to "+(w.end||"now")+")":"")))));
 if(c.timeline&&c.timeline.length)card.append(el("h2",{},"Recent activity"),el("ul",{},c.timeline.slice(0,6).map(t=>el("li",{},el("span",{class:"kind"},t.kind)," ",day(t.at)+(t.body?": "+t.body:"")+(t.job_title?" ("+t.job_title+(t.job_company?", "+t.job_company:"")+")":"")+(t.interviewer?", with "+t.interviewer:""),
  t.outcome_note?el("div",{class:"small"},"How it went: "+t.outcome_note):null,(t.feedback||[]).map(f=>el("div",{class:"small"},"Feedback from "+(f.by||"an interviewer")+": "+f.text))))));
 return card}""",
    "results": r"""
function render(d){
 const wrap=el("div",{});
 const exact=d.exact_total!=null?d.exact_total:d.total;
 const head=d.closest?"No exact matches. Closest matches:":d.exact?"Exact match":exact+(exact===1?" match":" matches");
 if(d.corrected&&d.suggestion)wrap.append(el("div",{class:"small"},"Showing results for ",el("b",{},d.suggestion)));
 if(d.no_one_named)wrap.append(el("div",{class:"card small"},"No one named ",el("b",{},d.no_one_named)," is saved."+(d.results.length?" These people share some of the words, under other names.":"")));
 wrap.append(el("div",{class:"row small muted"},el("span",{},head),appLink(d.app_url,"Open search in Talent Search")));
 if(!d.results.length)wrap.append(el("p",{class:"empty"},"No saved candidates match. Try fewer or different words."));
 let shown=false;
 for(const c of d.results){const sub=[c.current_title,c.current_company&&("at "+c.current_company)].filter(Boolean).join(" ")+(c.location?" · "+c.location:"");
  if(c.closest&&!d.closest&&!shown){shown=true;wrap.append(el("h2",{},"Closest matches, with some of the words"))}
  wrap.append(openCard("card",c.app_url,el("div",{class:"row"},el("b",{},c.name),c.closest?el("span",{class:"badge"},"Closest match"):null,(c.other_name&&!d.no_one_named)?el("span",{class:"badge"},"A different name"):null,profileLink(c.linkedin_url)),sub?el("div",{class:"muted small"},sub):null,
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
const LABEL={interview:"Interview",interview_out:"Interview still on",interview_outcome:"How did it go",interview_result:"Interview result",suggestion:"Suggestion",stuck:"Stuck",thin:"Needs more candidates",stale:"May be out of date",ai_change:"AI change",shortlist:"Shortlist",update_due:"Update due",openings:"Openings"};
function render(d){
 const wrap=el("div",{});
 wrap.append(el("div",{class:"row"},el("h1",{},d.items.length?d.items.length+" thing"+(d.items.length===1?"":"s")+" to do":"Nothing needs you today"),appLink(d.app_url)));
 const card=i=>el("div",{class:"card"},el("div",{class:"kind"},LABEL[i.kind]||i.kind),el("div",{},i.title),
  ((i.kind==="interview"||i.kind==="interview_out"||i.kind==="interview_outcome")&&i.when)?el("div",{class:"small"},when(i.when)):null,i.due_text?el("div",{class:"small"+(i.overdue?"":" muted")},i.due_text):null,
  i.detail?el("div",{class:"small muted"},i.detail):null);
 for(const i of d.items)wrap.append(card(i));
 if(d.next_week&&d.next_week.length){wrap.append(el("h2",{},"Next week"));for(const i of d.next_week)wrap.append(card(i))}
 return wrap}""",
    "shortlist": r"""
const KEY="talent-search/shortlist";
function viewKey(){const k=META&&META[KEY];return k&&k.key?k:null}
function canAct(){return !!(HOST&&HOST.serverTools&&viewKey())}
function said(r){return((r&&r.content)||[]).filter(c=>c&&c.type==="text").map(c=>c.text).join(" ").replace(/^Error executing tool [\w-]+: /,"")}
let countsLine=null;
function countText(c){c=c||{};return(c.proposed||0)+" waiting · "+(c.kept||0)+" kept · "+(c.passed||0)+" passed on"}
function act(card,p,tool,args){
 const k=viewKey();const status=card.querySelector(".status");
 card.querySelectorAll("button.btn,input.reason").forEach(b=>b.disabled=true);
 status.textContent="Working on it…";
 req("tools/call",{name:tool,arguments:Object.assign({job_id:k.job_id,prospect_id:p.id,view_key:k.key},args)}).then(r=>{
  const d=(r&&r.structuredContent)||null;
  if(r&&r.isError||!d){status.textContent=said(r)||"That didn't work. Try again in Talent Search.";card.querySelectorAll("button.btn,input.reason").forEach(b=>b.disabled=false);return}
  if(d.counts&&countsLine)countsLine.textContent=countText(d.counts);
  const next=prospect(Object.assign({},d.prospect,d.needs_choice?{choices:d.matches||[]}:{}));
  next.querySelector(".status").textContent=d.message||said(r);
  card.replaceWith(next)});
}
function buttons(card,p){
 const box=el("div",{class:"acts"});
 const same=(p.choices&&p.choices.length?p.choices:(p.checks&&p.checks.same_name)||[]);
 const keep=(label,args,primary)=>el("button",{class:"btn"+(primary?" primary":""),onclick:()=>act(card,p,"keep_prospect",args)},label);
 if(same.length){for(const m of same)box.append(keep("Keep as #"+m.id,{candidate_id:m.id},false));box.append(keep("Keep as someone new",{as_new:true},false))}
 else box.append(keep("Keep",{},true));
 const pass=el("button",{class:"btn",onclick:()=>{
  const why=el("input",{class:"reason",type:"text",maxlength:"300",placeholder:"Reason (optional)","aria-label":"Why you're passing"});
  const go=el("button",{class:"btn",onclick:()=>act(card,p,"pass_prospect",{reason:why.value})},"Pass");
  const back=el("button",{class:"btn",onclick:()=>box.replaceWith(buttons(card,p))},"Cancel");
  why.addEventListener("keydown",e=>{if(e.key==="Enter")go.click()});
  const form=el("div",{class:"acts"},why,go,back);box.replaceWith(form);why.focus()}},"Pass");
 box.append(pass);return box}
function prospect(p){const sub=[p.title,p.company&&("at "+p.company)].filter(Boolean).join(" ")+(p.location?" · "+p.location:"");
 const badge=p.status==="passed"?"Passed":p.status==="kept"?"Kept":null;
 const card=el("div",{class:"card"},el("div",{class:"row"},el("b",{},p.name,badge?el("span",{class:"badge"},badge):null),profileLink(p.linkedin_url)),
  sub?el("div",{class:"muted small"},sub):null,p.fit?el("div",{class:"small"},p.fit):null,
  (p.tags&&p.tags.length)?el("div",{class:"chips"},p.tags.slice(0,8).map(t=>el("span",{class:"chip"},t))):null,
  p.pass_reason?el("div",{class:"small muted"},"Reason: "+p.pass_reason):null,
  (p.check_lines&&p.check_lines.length)?el("ul",{class:"small"},p.check_lines.map(x=>el("li",{},x))):null,
  p.choices&&p.choices.length?el("ul",{class:"small"},p.choices.map(m=>el("li",{},"#"+m.id+" "+m.name+(m.current_title?", "+m.current_title:"")+(m.current_company?" at "+m.current_company:"")+((m.pipelines||[]).length?". In "+m.pipelines.map(x=>x.job+": "+x.stage).join("; "):"")))):null);
 if((p.status||"proposed")==="proposed"&&canAct())card.append(buttons(card,p));
 card.append(el("div",{class:"status muted",role:"status"}));
 return card}
function render(d){
 const wrap=el("div",{});const rows=d.added||d.prospects||[];const c=d.counts||{};
 wrap.append(el("div",{class:"row"},el("h1",{},"Shortlist"+(d.job?": "+d.job.title+(d.job.company?" · "+d.job.company:""):"")),appLink(d.app_url,"Review in Talent Search")));
 countsLine=el("div",{class:"small muted"},countText(c));wrap.append(countsLine);
 if(d.added)wrap.append(el("h2",{},"Added now"));
 if(!rows.length)wrap.append(el("p",{class:"empty"},"Nobody here."));
 for(const p of rows)wrap.append(prospect(p));
 if(rows.some(p=>(p.status||"proposed")==="proposed"))wrap.append(el("p",{class:"small muted"},canAct()?"Keep saves them and adds them to the job. You can undo a keep in Talent Search, under Changes.":"Keep or pass each one in Talent Search."));
 if(d.skipped&&d.skipped.length){wrap.append(el("h2",{},"Skipped"),el("ul",{class:"small"},d.skipped.map(x=>el("li",{},x.name+": "+x.reason))))}
 return wrap}""",
    "saved": r"""
const FIELD={current_title:"title",current_company:"company",linkedin_url:"LinkedIn",work_history:"work history",email:"email",phone:"phone",location:"location"};
const GROUPS=[["saved","Saved"],["updated","Updated"],["unchanged","Already saved, nothing new"],["needs_answer","Needs your answer (nothing saved)"],["not_saved","Not saved"]];
function fields(xs){return (xs||[]).map(f=>FIELD[f]||String(f).replace(/_/g," ")).join(", ")}
function row(r){const bits=[];
 if(r.stage)bits.push(r.already_in_job?"already in "+r.stage:"added to "+r.stage);
 if(r.kept&&r.kept.length)bits.push("kept the saved "+fields(r.kept));
 if(r.held&&r.held.length)bits.push("held for you: "+fields(r.held));
 return openCard("pc",r.app_url,el("div",{class:"row"},el("b",{},r.name),r.saved_status&&r.saved_status!=="Saved"?el("span",{class:"badge"},r.saved_status):null),
  bits.length?el("div",{class:"small muted"},bits.join(" · ")):null,
  r.reason?el("div",{class:"small"},r.reason):null,
  (r.notes||[]).map(x=>el("div",{class:"small"},x)),
  r.pipeline_error?el("div",{class:"small"},"Not added to the job: "+r.pipeline_error):null)}
function render(d){
 const wrap=el("div",{});
 wrap.append(el("div",{class:"row"},el("h1",{},d.job?"Saved for "+d.job.title+(d.job.company?" · "+d.job.company:""):"Saved people"),appLink(d.app_url)));
 if(d.summary)wrap.append(el("div",{class:"small muted"},d.summary));
 if(d.warning)wrap.append(el("div",{class:"card small"},d.warning));
 for(const[k,label]of GROUPS){const rs=d[k]||[];if(!rs.length)continue;wrap.append(el("h2",{},label+" ("+rs.length+")"));for(const r of rs)wrap.append(row(r))}
 if(d.batch_id)wrap.append(el("p",{class:"small muted"},"You can undo this whole batch in Talent Search, under Changes."));
 return wrap}""",
    "decide": r"""
function iv(i){return el("div",{class:"pc"},el("div",{class:"row"},el("b",{},i.name),el("span",{class:"badge"},i.outcome)),
 el("div",{class:"small muted"},when(i.at)+(i.interviewer?" · with "+i.interviewer:"")),
 i.note?el("div",{class:"small"},"How it went: "+i.note):null,
 (i.feedback||[]).map(f=>el("div",{class:"small"},el("b",{},(f.by||"Interviewer")+": "),f.text)))}
function render(d){
 const wrap=el("div",{});const j=d.job||{};
 wrap.append(el("div",{class:"row"},el("h1",{},"Finalists: "+j.title+(j.company?" · "+j.company:"")),appLink(d.app_url,"Decide in Talent Search")));
 const ps=d.people||[];
 if(!ps.length){wrap.append(el("p",{class:"empty"},"Nobody has interviewed or reached a late stage yet."));return wrap}
 const board=el("div",{class:"board"});
 for(const p of ps){const sub=[p.stage,p.days_in_process!=null?p.days_in_process+" days in process":null].filter(Boolean).join(" · ");
  board.append(el("div",{class:"col"},el("h3",{},el("span",{},p.name),appLink(p.app_url,"Open")),
   el("div",{class:"small muted"},sub),p.source?el("div",{class:"small muted"},"Source: "+p.source+(p.referrer?" (referred by "+p.referrer+")":"")):null,
   (p.interviews||[]).length?(p.interviews||[]).map(iv):el("div",{class:"small muted"},"No interviews logged."),
   (p.todos||[]).map(t=>el("div",{class:"small"},"To-do: "+t.text))))}
 wrap.append(board);
 if(d.more&&d.more.length)wrap.append(el("p",{class:"small muted"},"Also in late stages: "+d.more.map(m=>m.name).join(", ")));
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
