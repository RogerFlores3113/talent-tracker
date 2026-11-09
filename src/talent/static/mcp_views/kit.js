/* Talent Search view kit. Every in-chat view loads this first, then its own <view>.js, which defines render(data).
   Rules: DOM calls only (never put data into markup), plain wording, real controls, no external requests.

   Bridge (talks to the chat app over postMessage, JSON-RPC)
     HOST      host capabilities from ui/initialize (HOST.serverTools, HOST.openLinks ...)
     CTX       host context, merged on every change (theme, displayMode, availableDisplayModes, toolInfo ...)
     ARGS      the arguments of the tool call this view shows (from tool-input)
     META      the _meta of the tool result
     TOOL      the tool's name when the host says it (CTX.toolInfo.tool.name)
     req(method, params) -> Promise (errors arrive as {isError:true, content:[...]}, never as a rejection)
     callTool(name, args) -> Promise of the tool result (tools/call; only when HOST.serverTools)
     openLink(url, label)     open a link in the person's browser; if the chat app refuses, shows the URL with a Copy button
     askClaude(text, btn)     sends a message as the person (ui/message); hides btn and says so if the host refuses
     tellClaude(obj, text)    tells the model something without a message (ui/update-model-context)
     canFullscreen() / setMode("fullscreen"|"inline") / fullscreenButton()
     announce(text)           polite live-region announcement; setStatus(node, text) for a local status line
   Components (return DOM nodes)
     header({title, count, appUrl, appLabel, fullscreen, refresh})  title, count line, Refresh, fullscreen, Open in Talent Search
     personCard(c, opts)      one person's summary card (article); opts: {headingLevel, saved, onAsk, extra}
     peopleTable(rows, st, h) real sortable table with selection; see people.js for the state it expects
     linksRow(c)              Open profile, LinkedIn, Website and every saved link, each named by host and person
     jobLink(job, who)        a job (and stage) as a link to its pipeline page in the app
     avatar(c, small)         initials in a ring coloured by the colour tag
     dateEl(iso) / rel(iso)   relative date with the exact date in the title; day(iso), when(iso) for plain text
     emptyState(msg) loadingState(msg) errorState(msg, retry)
     personLink(name, url, id)   a name that opens that person's card inside the view (and links to the app)
     openPerson(id, appUrl, returnTo)   shows a person's card in place of the view, with a Back button
     openCard(cls, url, ...kids)        a clickable block (legacy); candidate urls open the card in the view
   Helpers: el(tag, attrs, ...kids), marked("a **b**"), safeUrl, copyText(text, nodeToSelect), appOrigin(url), hostLabel(url), onKeys(node, map)
*/
const V=__VERSION__;let nid=1;const pend={};let HOST={},CTX={},META={},ARGS=null,TOOL=null,DATA=null,FAILED={};
const PCARDS={};
function post(m){window.parent.postMessage(m,"*")}
function req(method,params){const id=nid++;return new Promise(r=>{pend[id]=r;post({jsonrpc:"2.0",id,method,params})})}
function note(method,params){post({jsonrpc:"2.0",method,params:params||{}})}
function el(tag,attrs,...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs||{})){if(v==null||v===false)continue;if(k==="class")e.className=v;else if(k==="onclick")e.addEventListener("click",v);else e.setAttribute(k,v===true?"":v)}
 for(const k of kids.flat()){if(k==null||k===false)continue;e.append(k instanceof Node?k:document.createTextNode(String(k)))}return e}
function marked(s){const out=[];String(s||"").split("**").forEach((p,i)=>out.push(i%2?el("mark",{},p):p));return out}
const root=()=>document.getElementById("main");
function said(r){return((r&&r.content)||[]).filter(c=>c&&c.type==="text").map(c=>c.text).join(" ").replace(/^Error executing tool [\w-]+: /,"")}

/* ---- live region and status */
function announce(t){const l=document.getElementById("live");if(!l)return;l.textContent="";setTimeout(()=>{l.textContent=t},30)}
function setStatus(n,t){if(n)n.textContent=t||"";if(t)announce(t)}

/* ---- host context: theme, host style variables, display mode */
const VARMAP={"--color-background-primary":"--surface","--color-text-primary":"--text","--color-text-secondary":"--muted","--color-border-primary":"--line","--color-background-secondary":"--chip","--font-sans":"--font"};
function applyCtx(){const d=document.documentElement;
 if(CTX.theme)d.dataset.theme=CTX.theme;
 const vars=(CTX.styles&&CTX.styles.variables)||{};
 for(const[k,v]of Object.entries(vars)){if(VARMAP[k]&&typeof v==="string"&&v&&v.length<200&&!/[;{}]/.test(v))d.style.setProperty(VARMAP[k],v)}
 d.dataset.mode=CTX.displayMode||"inline";
 if(CTX.locale)d.lang=String(CTX.locale).slice(0,16);
 TOOL=(CTX.toolInfo&&CTX.toolInfo.tool&&CTX.toolInfo.tool.name)||TOOL}
function mergeCtx(p){if(!p)return;const was=CTX.displayMode;CTX=Object.assign({},CTX,p);if(p.styles)CTX.styles=Object.assign({},CTX.styles||{},p.styles);applyCtx();
 if(was!==CTX.displayMode){document.querySelectorAll("button[data-fs]").forEach(b=>{b.textContent=isFull()?"Exit full screen":"Full screen";b.setAttribute("aria-pressed",isFull()?"true":"false")});if(typeof window.onModeChange==="function")window.onModeChange(CTX.displayMode)}}
function canFullscreen(){return Array.isArray(CTX.availableDisplayModes)&&CTX.availableDisplayModes.includes("fullscreen")&&!FAILED.mode}
function isFull(){return CTX.displayMode==="fullscreen"}
function setMode(m){if(m==="fullscreen"&&!canFullscreen())return Promise.resolve(false);
 return req("ui/request-display-mode",{mode:m}).then(r=>{if(r&&r.isError){FAILED.mode=1;return false}mergeCtx({displayMode:(r&&r.mode)||m});return true})}
function fullscreenButton(){if(!canFullscreen())return null;
 const b=el("button",{class:"btn",type:"button","data-fs":"1","aria-pressed":isFull()?"true":"false"},isFull()?"Exit full screen":"Full screen");
 b.addEventListener("click",()=>setMode(isFull()?"inline":"fullscreen").then(()=>{b.textContent=isFull()?"Exit full screen":"Full screen";b.setAttribute("aria-pressed",isFull()?"true":"false")}));return b}

/* ---- links, with a way out when the chat app won't open them */
function safeUrl(u){try{const x=new URL(String(u||""));return /^https?:$/.test(x.protocol)?x:null}catch(e){return null}}
function appOrigin(u){const x=safeUrl(u);return x?x.origin:""}
function isLocal(x){return /^(127\.0\.0\.1|localhost|\[::1\])$/.test(x.hostname)}
const HOSTS=[[/(^|\.)linkedin\.com$/,"LinkedIn"],[/(^|\.)github\.com$/,"GitHub"],[/(^|\.)gitlab\.com$/,"GitLab"],[/(^|\.)(x|twitter)\.com$/,"X"],[/(^|\.)scholar\.google\./,"Google Scholar"],[/(^|\.)stackoverflow\.com$/,"Stack Overflow"],[/(^|\.)medium\.com$/,"Medium"],[/(^|\.)dribbble\.com$/,"Dribbble"],[/(^|\.)behance\.net$/,"Behance"],[/(^|\.)kaggle\.com$/,"Kaggle"],[/(^|\.)huggingface\.co$/,"Hugging Face"],[/(^|\.)substack\.com$/,"Substack"],[/(^|\.)youtube\.com$/,"YouTube"],[/(^|\.)orcid\.org$/,"ORCID"]];
function hostLabel(u){const x=safeUrl(u);if(!x)return"Link";for(const[re,n]of HOSTS)if(re.test(x.hostname))return n;return"Portfolio"}
function copyText(text,node){
 const sel=()=>{if(!node)return;const r=document.createRange();r.selectNodeContents(node);const s=window.getSelection();s.removeAllRanges();s.addRange(r)};
 try{if(navigator.clipboard&&navigator.clipboard.writeText)return navigator.clipboard.writeText(text).then(()=>true,()=>{sel();return false})}catch(e){}
 sel();return Promise.resolve(false)}
function showLinkNote(url,label,local){
 const box=document.getElementById("linknote");if(!box)return;
 const u=el("span",{class:"url"},url);
 const st=el("span",{class:"small muted",role:"status"});
 const copy=el("button",{class:"btn",type:"button"},"Copy link");
 copy.addEventListener("click",()=>copyText(url,u).then(ok=>{st.textContent=ok?"Copied.":"Select the link above and copy it."}));
 const close=el("button",{class:"btn",type:"button"},"Close");
 close.addEventListener("click",()=>{box.replaceChildren()});
 box.replaceChildren(el("div",{class:"linknote"},
  el("div",{},"Your chat app didn't open "+(label?label+" ":"the link ")+"for you."+(local?" This link only works on this computer while Talent Search is running.":" Copy it into your browser instead.")),
  u,el("div",{class:"acts"},copy,close,st)));
 announce("Your chat app didn't open the link. The address is shown with a Copy button.");copy.focus()}
function openLink(url,label){const x=safeUrl(url);if(!x)return Promise.resolve(false);
 const local=isLocal(x);
 if(HOST&&HOST.openLinks===undefined&&Object.keys(HOST).length&&!("openLinks"in HOST)){showLinkNote(x.href,label,local);return Promise.resolve(false)}
 return req("ui/open-link",{url:x.href}).then(r=>{if(r&&r.isError){showLinkNote(x.href,label,local);return false}return true})}
const openApp=openLink;
function linkBtn(url,text,aria,cls){return el("button",{class:cls||"link",type:"button","aria-label":aria||null,onclick:()=>openLink(url,text)},text)}
function appLink(url,label){return url?el("button",{class:"link small",type:"button",onclick:()=>openLink(url,label||"Talent Search")},label||"Open in Talent Search"):null}
function profileLink(u,fallback,who){const x=safeUrl(u);if(!x)return null;const li=/(^|\.)linkedin\.com$/i.test(x.hostname);
 const text=li?"LinkedIn":(fallback||"Profile");
 return linkBtn(x.href,text,who?(li?"LinkedIn profile of "+who:text+" of "+who):null)}

/* ---- messages to Claude */
function askClaude(text,btn){return req("ui/message",{role:"user",content:{type:"text",text}}).then(r=>{
 if(r&&r.isError){if(btn)btn.hidden=true;announce("Your chat app wouldn't send that message. You can type it in the chat yourself.");return false}
 announce("Sent to Claude.");return true})}
function tellClaude(obj,text){if(FAILED.tell)return Promise.resolve(false);
 return req("ui/update-model-context",{content:[{type:"text",text}],structuredContent:obj}).then(r=>{if(r&&r.isError){FAILED.tell=1;return false}return true})}
function callTool(name,args){return req("tools/call",{name,arguments:args||{}})}
function canCall(){return !!(HOST&&HOST.serverTools)}

/* ---- dates */
function parseD(iso){if(!iso)return null;const d=new Date(iso);return isNaN(d)?null:d}
function day(iso){const d=parseD(iso);if(!iso)return"";return d?d.toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"}):iso}
function when(iso){const d=parseD(iso);if(!iso)return"";return d?d.toLocaleString(undefined,{weekday:"short",month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}):iso}
function rel(iso){const d=parseD(iso);if(!d)return iso||"";const n=Math.round((d-Date.now())/864e5);
 if(n===0)return"today";if(n===-1)return"yesterday";if(n===1)return"tomorrow";
 if(n<0){const a=-n;if(a<14)return a+" days ago";if(a<60)return Math.round(a/7)+" weeks ago";if(a<730)return Math.round(a/30)+" months ago";return Math.round(a/365)+" years ago"}
 if(n<14)return"in "+n+" days";if(n<60)return"in "+Math.round(n/7)+" weeks";return"in "+Math.round(n/30)+" months"}
function dateEl(iso,empty){if(!parseD(iso))return empty==null?"":empty;return el("time",{datetime:iso,title:when(iso)},rel(iso))}
function daysSince(iso){const d=parseD(iso);return d?Math.max(0,Math.floor((Date.now()-d)/864e5)):null}

/* ---- states */
function emptyState(msg){return el("p",{class:"empty"},msg)}
function loadingState(msg){return el("div",{class:"empty",role:"status"},msg||"Loading...",el("span",{class:"skel"}),el("span",{class:"skel",style:"width:40%"}))}
function errorState(msg,retry){return el("div",{class:"card",role:"alert"},msg||"That didn't work.",retry?el("div",{class:"acts"},el("button",{class:"btn",type:"button",onclick:retry},"Try again")):null)}
function onKeys(node,map){node.addEventListener("keydown",e=>{const t=e.target;if(t&&/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)&&e.key!=="Escape")return;const f=map[e.key];if(f&&!e.altKey&&!e.ctrlKey&&!e.metaKey){f(e)}})}

/* ---- people: avatar, links, card */
const COLORS=["red","orange","yellow","green","blue","purple"];
function initials(n){const p=String(n||"?").trim().split(/\s+/).filter(Boolean);return((p[0]||"?")[0]+(p.length>1?p[p.length-1][0]:"")).toUpperCase()}
function colorCls(c){return COLORS.includes(c)?" c-"+c:""}
function avatar(c,small){return el("span",{class:"avatar"+(small?" sm":"")+colorCls(c&&c.color),"aria-hidden":"true"},initials(c&&c.name))}
function firstName(n){return String(n||"").trim().split(/\s+/)[0]||"them"}
function roleLine(c){return[c.current_title,c.current_company&&("at "+c.current_company)].filter(Boolean).join(" ")}
function jobLink(j,who){const text=j.job||j.job_title||"a job";
 const url=j.app_url||(j.origin&&j.job_id?j.origin+"/jobs/"+j.job_id:null);
 return url?linkBtn(url,text,"Open the "+text+" pipeline"+(who?" ("+who+" is in it)":"")):el("span",{},text)}
function linksRow(c,primary){
 const row=el("div",{class:"linksrow"});
 const nm=c.name||"them";
 if(c.app_url&&primary!==false)row.append(el("button",{class:"btn primary",type:"button","aria-label":"Open "+nm+" in Talent Search",onclick:()=>openLink(c.app_url,"the profile")},"Open profile"));
 const seen=new Set();
 const add=(u,text,aria)=>{const x=safeUrl(u);if(!x||seen.has(x.href))return;seen.add(x.href);row.append(el("button",{class:"btn",type:"button","aria-label":aria,onclick:()=>openLink(x.href,text)},text))};
 if(c.linkedin_url)add(c.linkedin_url,"LinkedIn","LinkedIn profile of "+nm);
 if(c.website)add(c.website,"Website","Website of "+nm);
 for(const u of c.links||[]){const l=hostLabel(u);add(u,l,l+" of "+nm)}
 return row}
function stageOf(c){const j=(c.jobs||[])[0];return j||null}
function stageText(j){return j?(j.stage+(j.job?", "+j.job:"")):""}
function stageChip(j){if(!j)return null;const d=daysSince(j.since);const out=j.state==="lost"||j.state==="hired";
 return el("span",{class:"stagechip"+(out?" out":"")},j.stage+(j.state==="lost"?" (out)":"")+(j.job?" for "+j.job:"")+(d!=null?" · "+(d===0?"today":d+(d===1?" day":" days")):""))}
function isSample(c){return /sample/i.test(String(c.source||""))}
function personCard(c,o){o=o||{};const lvl=o.headingLevel||2;const nm=c.name||"Candidate";
 const card=el("article",{class:"pcard","aria-label":nm});
 const heading=el("h"+lvl,{class:"name",tabindex:"-1"},c.app_url?el("button",{class:"link name-link",type:"button","aria-label":"Open "+nm+" in Talent Search",onclick:()=>openLink(c.app_url,"the profile")},nm):nm);
 const badges=[];
 if(o.saved)badges.push(el("span",{class:"badge"},o.saved));
 if(c.closest)badges.push(el("span",{class:"badge",title:"They match some of your words, not all of them."},"Partial match"));
 if(c.other_name)badges.push(el("span",{class:"badge"},"A different name"));
 if(isSample(c))badges.push(el("span",{class:"badge"},"Sample data"));
 const place=c.location;
 const who=el("div",{class:"who"},el("div",{class:"row",style:"justify-content:flex-start;align-items:center"},heading,...badges),
  roleLine(c)?el("div",{class:"sub"},roleLine(c)):null,
  (place||c.color)?el("div",{class:"meta"},place||null,(place&&c.color)?" · ":null,c.color?el("span",{},el("span",{class:"dot"+colorCls(c.color),"aria-hidden":"true"}),c.color+" tag"):null):null,
  stageChip(stageOf(c)));
 card.append(el("div",{class:"top"},avatar(c),who));
 if(c.match||c.reason)card.append(el("div",{class:"why"},c.reason?el("div",{class:"muted"},c.reason):null,c.match?el("div",{},marked(c.match)):null,
  c.closest?el("div",{class:"small muted"},"Partial match: this person has some of your words but not all of them."):null));
 const contact=el("div",{class:"contact"});
 if(c.email)contact.append(el("a",{href:"mailto:"+c.email,"aria-label":"Email "+nm},c.email));
 if(c.phone)contact.append(el("a",{href:"tel:"+String(c.phone).replace(/[^\d+]/g,""),"aria-label":"Call "+nm},c.phone));
 if(contact.childNodes.length)card.append(contact);
 card.append(linksRow(c));
 if(c.tags&&c.tags.length)card.append(el("ul",{class:"chips","aria-label":"Skills"},c.tags.map(t=>el("li",{class:"chip"},t))));
 const facts=el("dl",{class:"facts"});
 const fact=(k,v)=>{if(v==null||v==="")return;facts.append(el("dt",{},k),el("dd",{},v))};
 fact("Last contacted",c.last_contacted_at?dateEl(c.last_contacted_at):(o.partial?null:"never"));
 fact("Added",c.created_at?dateEl(c.created_at):null);fact("Source",c.source);
 fact("Next interview",c.next_interview?el("time",{datetime:c.next_interview},when(c.next_interview)):null);
 if(facts.childNodes.length)card.append(facts);
 if(c.jobs&&c.jobs.length)card.append(el("h"+(lvl+1),{class:"muted small",style:"margin:10px 0 2px"},"Jobs"),el("ul",{class:"plain"},c.jobs.map(j=>el("li",{},jobLink(j,nm),": ",el("b",{},j.stage),j.state==="lost"?" (out)":""))));
 if(c.activity&&c.activity.length)card.append(el("h"+(lvl+1),{class:"muted small",style:"margin:10px 0 2px"},"Recent activity"),el("ul",{class:"plain"},c.activity.map(t=>el("li",{},el("span",{class:"kind"},t.kind)," ",day(t.at)+(t.body?": "+t.body:"")+(t.job_title?" ("+t.job_title+(t.job_company?", "+t.job_company:"")+")":"")+(t.interviewer?", with "+t.interviewer:""),
  t.outcome_note?el("div",{class:"small"},"How it went: "+t.outcome_note):null,(t.feedback||[]).map(f=>el("div",{class:"small"},"Feedback from "+(f.by||"an interviewer")+": "+f.text))))));
 if(c.about){const long=c.about.length>220;
  if(long)card.append(el("details",{},el("summary",{},"About "+firstName(nm)),el("p",{style:"margin:4px 0"},c.about)));
  else card.append(el("p",{style:"margin:8px 0 0"},c.about))}
 if(c.work_history&&c.work_history.length)card.append(el("details",{},el("summary",{},"Work history ("+c.work_history.length+")"),el("ul",{},c.work_history.map(w=>el("li",{},[w.title,w.company&&("at "+w.company)].filter(Boolean).join(" ")+((w.start||w.end)?" ("+(w.start||"")+" to "+(w.end||"now")+")":""))))));
 const acts=el("div",{class:"acts"});
 const ask=el("button",{class:"btn",type:"button",onclick:()=>askClaude("Tell me more about "+nm+" (candidate id "+c.id+") and what to do next.",ask)},"Ask Claude about "+firstName(nm));
 acts.append(ask);if(o.extra)acts.append(o.extra);
 card.append(acts);
 return card}

/* ---- header */
function header(o){
 const tools=el("div",{class:"tools"});
 if(o.refresh!==false&&canRefresh())tools.append(el("button",{class:"btn",type:"button",onclick:ev=>refresh(ev.currentTarget)},"Refresh"));
 if(o.fullscreen!==false&&canFullscreen())tools.append(fullscreenButton());
 if(o.appUrl)tools.append(appLink(o.appUrl,o.appLabel));
 return el("div",{class:"vhead"},el("div",{},el("h1",{id:"vtitle",tabindex:"-1"},o.title),o.count?el("div",{class:"count"},o.count):null),tools)}

/* ---- a person opened inside another view */
function leanToCard(p,origin){const c=Object.assign({},p);
 c.jobs=(p.jobs||[]).map(j=>Object.assign({},j,{app_url:j.app_url||(origin&&j.job_id?origin+"/jobs/"+j.job_id:null)}));return c}
function fetchCards(ids){const need=ids.filter(i=>!PCARDS[i]);if(!need.length||!canCall())return Promise.resolve();
 return callTool("candidate_cards",{candidate_ids:need.slice(0,20)}).then(r=>{const d=r&&r.structuredContent;if(d&&d.cards)for(const c of d.cards)PCARDS[c.id]=c})}
function openPerson(id,appUrl,returnTo){
 const main=document.getElementById("main"),drawer=document.getElementById("drawer");
 if(!drawer||!id||!canCall()){if(appUrl)openLink(appUrl,"the profile");return}
 const back=()=>{drawer.replaceChildren();drawer.hidden=true;main.hidden=false;if(returnTo&&returnTo.isConnected)returnTo.focus()};
 main.hidden=true;drawer.hidden=false;drawer.replaceChildren(loadingState("Loading summary..."));
 fetchCards([id]).then(()=>{const c=PCARDS[id];
  if(!c){drawer.replaceChildren(errorState("Couldn't load that summary.",()=>openPerson(id,appUrl,returnTo)),el("button",{class:"btn",type:"button",onclick:back},"Back"));return}
  const bb=el("button",{class:"btn",type:"button",onclick:back},"Back");
  drawer.replaceChildren(el("div",{class:"drawer"},el("div",{class:"acts",style:"margin:0 0 8px"},bb),personCard(c,{headingLevel:2})));
  drawer.addEventListener("keydown",e=>{if(e.key==="Escape")back()},{once:true});bb.focus();announce(c.name+", summary opened.")})}
function personLink(name,url,id){const m=id||(/\/candidates\/(\d+)/.exec(String(url||""))||[])[1];
 const b=el("button",{class:"link",type:"button","aria-label":"Open "+name+" summary"},name);
 b.addEventListener("click",()=>openPerson(+m,url,b));return b}
function openCard(cls,url,...kids){const m=/\/candidates\/(\d+)/.exec(String(url||""));const e=el("div",{class:cls+(url?" click":"")},...kids);
 if(url){const first=e.querySelector("b");const nm=first?first.textContent:"candidate";
  const open=el("button",{class:"link small",type:"button","aria-label":"Open "+nm+" summary"},"Summary");open.addEventListener("click",()=>m?openPerson(+m[1],url,open):openLink(url,"the profile"));e.append(open);
  e.addEventListener("click",ev=>{if(!(ev.target instanceof HTMLButtonElement||ev.target instanceof HTMLAnchorElement))open.click()})}
 return e}

/* ---- refresh and results */
const READ_TOOLS=["search_candidates","run_saved_search","get_candidate","job_pipeline","list_action_items","list_prospects","finalists","decide_view"];
function canRefresh(){return canCall()&&TOOL&&ARGS&&READ_TOOLS.includes(TOOL)}
function refresh(btn){if(btn)btn.disabled=true;announce("Refreshing.");
 callTool(TOOL,ARGS).then(r=>{if(btn)btn.disabled=false;const d=r&&r.structuredContent;if(r&&r.isError||!d){announce(said(r)||"Couldn't refresh.");return}showResult(d,r._meta)})}
function showResult(d,meta){DATA=d;if(meta)META=meta;let n;
 try{n=render(d)}catch(e){n=errorState("Couldn't show this.")}
 const m=document.getElementById("main");m.replaceChildren(n||emptyState("Nothing to show."));
 const dr=document.getElementById("drawer");if(dr){dr.replaceChildren();dr.hidden=true;m.hidden=false}}

window.addEventListener("message",ev=>{if(ev.source!==window.parent)return;const m=ev.data;if(!m||m.jsonrpc!=="2.0")return;
 if(m.id!=null&&!m.method&&pend[m.id]){pend[m.id](m.error?{isError:true,content:[{type:"text",text:String(m.error.message||"That didn't work.")}]}:(m.result||{}));delete pend[m.id];return}
 const p=m.params||{};
 if(m.method==="ui/notifications/tool-input"){ARGS=p.arguments||{};if(!DATA)document.getElementById("main").replaceChildren(loadingState("Working on it..."))}
 else if(m.method==="ui/notifications/tool-input-partial"){if(!DATA)document.getElementById("main").replaceChildren(loadingState("Working on it..."))}
 else if(m.method==="ui/notifications/tool-cancelled"){document.getElementById("main").replaceChildren(emptyState("That was cancelled, so there is nothing to show."))}
 else if(m.method==="ui/notifications/tool-result"){META=p._meta||{};const d=p.structuredContent||null;
  if(d)showResult(d,p._meta);else document.getElementById("main").replaceChildren(p.isError?errorState(said(p)||"That didn't work."):emptyState("Nothing to show."))}
 else if(m.method==="ui/notifications/host-context-changed")mergeCtx(p);
 else if(m.method==="ui/resource-teardown"&&m.id!=null)post({jsonrpc:"2.0",id:m.id,result:{}});
});
let lastW=0,lastH=0;
new ResizeObserver(()=>{const w=document.documentElement.scrollWidth,h=document.documentElement.scrollHeight;
 if(isFull()){if(w!==lastW){lastW=w;note("ui/notifications/size-changed",{width:w})}return}
 if(w===lastW&&h===lastH)return;lastW=w;lastH=h;note("ui/notifications/size-changed",{width:w,height:h})}).observe(document.documentElement);
req("ui/initialize",{protocolVersion:"2026-01-26",clientInfo:{name:"talent-search",version:V},appCapabilities:{availableDisplayModes:["inline","fullscreen"]}}).then(r=>{
 if(r&&r.isError){document.getElementById("main").replaceChildren(emptyState("This chat app couldn't start the view. The answer is in the text above."));return}
 HOST=(r&&r.hostCapabilities)||{};mergeCtx(r&&r.hostContext);note("ui/notifications/initialized",{})});

/* ---- people table: peopleTable(rows, st, h)
   rows: lean people (id, name, current_title, current_company, location, jobs[], match, last_contacted_at, created_at, updated_at, color, email, app_url)
   st:   {sel:Set of ids, sort:null|key, dir:"asc"|"desc", filter:""}, kept by the caller so it survives a redraw
   h:    {onOpen(person), onSel(), origin, more:{label,onClick}|null, total:Number}
   Returns {node, focusCaption()}. A plain table with a caption, sort buttons inside th, name as th scope=row. */
function firstJob(p){return(p.jobs||[])[0]||null}
function plainMatch(p){return String(p.match||p.reason||"").split("**").join("")}
const TCOLS=[
 {k:"name",l:"Name",v:p=>p.name},
 {k:"title",l:"Title",v:p=>p.current_title},
 {k:"company",l:"Company",v:p=>p.current_company},
 {k:"location",l:"Location",v:p=>p.location},
 {k:"stage",l:"Stage",v:p=>stageText(firstJob(p))},
 {k:"match",l:"Match",v:plainMatch},
 {k:"contacted",l:"Last contacted",v:p=>p.last_contacted_at,date:1},
 {k:"added",l:"Added",v:p=>p.created_at,date:1,wide:1},
 {k:"updated",l:"Updated",v:p=>p.updated_at,date:1,wide:1},
 {k:"color",l:"Color",v:p=>{const i=COLORS.indexOf(p.color);return i<0?null:i},wide:1}];
function cmpVals(a,b,date){if(date){a=parseD(a);b=parseD(b);a=a?+a:null;b=b?+b:null}
 if(typeof a==="string")a=a.toLowerCase();if(typeof b==="string")b=b.toLowerCase();
 return a<b?-1:a>b?1:0}
function sortedRows(rows,st){const idx=new Map(rows.map((p,i)=>[p.id,i]));let out=rows.slice();
 const f=(st.filter||"").trim().toLowerCase();
 if(f)out=out.filter(p=>[p.name,p.current_title,p.current_company,p.location,stageText(firstJob(p)),plainMatch(p),(p.tags||[]).join(" ")].join(" ").toLowerCase().includes(f));
 const col=TCOLS.find(c=>c.k===st.sort);
 if(col){const sign=st.dir==="desc"?-1:1;
  out.sort((x,y)=>{const a=col.v(x),b=col.v(y),ea=a==null||a==="",eb=b==null||b==="";
   if(ea&&eb)return idx.get(x.id)-idx.get(y.id);if(ea)return 1;if(eb)return-1;
   const c=cmpVals(a,b,col.date);return c?c*sign:idx.get(x.id)-idx.get(y.id)})}
 return out}
function selNames(rows,st){return rows.filter(p=>st.sel.has(p.id))}
function peopleLabel(ps){return ps.map(p=>p.name+" (id "+p.id+")").join(", ")}
function peopleTable(rows,st,h){
 const wrap=el("div",{});
 const status=el("div",{class:"sel",role:"status","aria-live":"polite"});
 const caption=el("caption",{tabindex:"-1"});
 const tbody=el("tbody");
 const allBox=el("input",{type:"checkbox"});
 const filter=el("input",{class:"field",type:"search","aria-label":"Filter these people",placeholder:"Filter these",value:st.filter||"",style:"max-width:14rem;min-width:8rem"});
 const bulk=el("span",{class:"tbar",style:"margin:0"});
 const askRow=el("div",{class:"tbar",hidden:true});
 const stat2=el("span",{class:"small muted",role:"status"});
 let shown=[],last=-1;
 const relBtn=el("button",{class:"btn",type:"button",onclick:()=>{st.sort=null;st.dir="asc";fill();heads();announce("Sorted by relevance, the order the search gave.")}},"Relevance");
 const ths=TCOLS.map(c=>{const b=el("button",{class:"sort",type:"button"});b.addEventListener("click",()=>{
   if(st.sort===c.k)st.dir=st.dir==="asc"?"desc":"asc";else{st.sort=c.k;st.dir="asc"}
   fill();heads();announce("Sorted by "+c.l+", "+(st.dir==="asc"?"ascending":"descending"))});
  return{c,b,th:el("th",{scope:"col",class:c.wide?"wide":null},b)}});
 function heads(){for(const{c,b,th}of ths){const on=st.sort===c.k;
   th.setAttribute("aria-sort",on?(st.dir==="asc"?"ascending":"descending"):"none");
   b.replaceChildren(c.l,el("span",{class:"arr","aria-hidden":"true"},on?(st.dir==="asc"?" ▲":" ▼"):""));
   b.setAttribute("aria-label","Sort by "+c.l+(on?", sorted "+(st.dir==="asc"?"ascending":"descending"):""))}
  relBtn.setAttribute("aria-pressed",st.sort?"false":"true")}
 function selUI(){const n=st.sel.size;status.textContent=n?n+" selected":"";
  const inShown=shown.filter(p=>st.sel.has(p.id)).length;
  allBox.checked=!!shown.length&&inShown===shown.length;allBox.indeterminate=inShown>0&&inShown<shown.length;
  allBox.setAttribute("aria-label","Select all "+shown.length);
  tbody.querySelectorAll("input.rowpick").forEach(b=>{const on=st.sel.has(+b.dataset.id);b.checked=on;b.closest("tr").classList.toggle("sel",on)});
  const chosen=selNames(rows,st);bulk.replaceChildren();
  if(n){const emails=chosen.filter(p=>p.email).map(p=>p.email);
   bulk.append(
    el("button",{class:"btn",type:"button",onclick:()=>{askRow.hidden=false;askIn.focus()}},"Ask Claude about these "+n),
    el("button",{class:"btn",type:"button",onclick:()=>openLink((h.origin||"")+"/candidates?ids="+chosen.map(p=>p.id).join(","),"the Candidates page")},"Open "+n+" in Talent Search"),
    el("button",{class:"btn",type:"button",onclick:()=>{const t=emails.join(", ");copyText(t).then(ok=>{stat2.textContent=ok?"Copied "+emails.length+" email"+(emails.length===1?"":"s")+(emails.length<n?" ("+(n-emails.length)+" had none saved).":"."):"Couldn't copy. "+(t?"Select them here: "+t:"None of them has an email saved.");announce(stat2.textContent)})}},"Copy "+emails.length+" email"+(emails.length===1?"":"s")))}
  else askRow.hidden=true}
 const askIn=el("input",{class:"field",type:"text","aria-label":"What should Claude do with them?",placeholder:"What should Claude do with them?",maxlength:"500"});
 const sendAsk=()=>{const t=askIn.value.trim();if(!t){askIn.focus();return}const chosen=selNames(rows,st);
  askClaude("For these "+chosen.length+" candidates: "+peopleLabel(chosen)+": "+t,sendBtn).then(ok=>{if(ok){askIn.value="";askRow.hidden=true;stat2.textContent="Sent to Claude."}})};
 const sendBtn=el("button",{class:"btn primary",type:"button",onclick:sendAsk},"Send to Claude");
 askIn.addEventListener("keydown",e=>{if(e.key==="Enter")sendAsk();if(e.key==="Escape"){e.stopPropagation();askRow.hidden=true}});
 askRow.append(askIn,sendBtn,el("button",{class:"btn",type:"button",onclick:()=>{askRow.hidden=true}},"Cancel"));
 function setSel(p,on){if(on)st.sel.add(p.id);else st.sel.delete(p.id)}
 function fill(){shown=sortedRows(rows,st);tbody.replaceChildren();
  caption.textContent=shown.length+" candidate"+(shown.length===1?"":"s")+(shown.length<rows.length?" shown of "+rows.length:"")+(h.total&&h.total>rows.length?" (the search found "+h.total+")":"");
  shown.forEach((p,i)=>{
   const pick=el("input",{type:"checkbox",class:"rowpick","data-id":p.id,"aria-label":"Select "+p.name});
   pick.addEventListener("click",ev=>{const on=pick.checked;
    if(ev.shiftKey&&last>=0){const[a,b]=[Math.min(last,i),Math.max(last,i)];for(let j=a;j<=b;j++)setSel(shown[j],on)}else setSel(p,on);
    last=i;selUI();if(h.onSel)h.onSel()});
   const open=el("button",{class:"link",type:"button","aria-label":"Show card for "+p.name,onclick:()=>h.onOpen(p)},p.name);
   const j=firstJob(p);
   const td=(label,...kids)=>el("td",{"data-label":label},...kids);
   const wd=(label,...kids)=>el("td",{class:"wide","data-label":label},...kids);
   const txt=(label,v)=>{const t=el("td",{"data-label":v?label:null,title:v||null},v||"");return t};
   const mt=el("td",{class:"match","data-label":plainMatch(p)?"Match":null,title:plainMatch(p)||null},p.match?marked(p.match):(p.reason||""));
   tbody.append(el("tr",{},el("td",{class:"pickcell"},pick),el("th",{scope:"row"},open),
    txt("Title",p.current_title),txt("Company",p.current_company),txt("Location",p.location),
    el("td",{"data-label":j?"Stage":null,title:j?stageText(j):null,class:j&&(j.state==="lost"||j.state==="hired")?"out":null},j?stageText(j):""),
    mt,td(p.last_contacted_at?"Last contacted":null,dateEl(p.last_contacted_at)),wd(p.created_at?"Added":null,dateEl(p.created_at)),wd(p.updated_at?"Updated":null,dateEl(p.updated_at)),
    el("td",{class:"wide","data-label":p.color?"Color":null},p.color?el("span",{},el("span",{class:"dot"+colorCls(p.color),"aria-hidden":"true"}),p.color):"")))});
  if(!shown.length)tbody.append(el("tr",{},el("td",{colspan:"11"},"Nobody matches that filter.")));
  selUI()}
 filter.addEventListener("input",()=>{st.filter=filter.value;fill()});
 allBox.addEventListener("click",()=>{for(const p of shown)setSel(p,allBox.checked);selUI();if(h.onSel)h.onSel()});
 const none=el("button",{class:"btn",type:"button",onclick:()=>{for(const p of shown)st.sel.delete(p.id);selUI();if(h.onSel)h.onSel()}},"Select none");
 const all=el("button",{class:"btn",type:"button",onclick:()=>{for(const p of shown)st.sel.add(p.id);selUI();if(h.onSel)h.onSel()}},"Select all shown");
 const table=el("table",{class:"ptable"},caption,el("thead",{},el("tr",{},el("th",{scope:"col",class:"pickcell"},allBox),...ths.map(t=>t.th))),tbody);
 const tools=el("div",{class:"tbar"},filter,relBtn,all,none,status,h.more?el("button",{class:"btn",type:"button",onclick:ev=>h.more.onClick(ev.currentTarget)},h.more.label):null);
 wrap.append(tools,bulk,askRow,stat2,el("div",{class:"tablewrap"},table));
 heads();fill();
 return{node:wrap,focusCaption:()=>caption.focus()}}
