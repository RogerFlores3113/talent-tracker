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
     askOrLink(text, ctl, statusNode, url, label)  askClaude for a change (move, advance, mark done); on refusal hides ctl and offers the app page
     tellClaude(obj, text)    tells the model something without a message (ui/update-model-context)
     canFullscreen() / setMode("fullscreen"|"inline") / fullscreenButton()
     announce(text)           polite live-region announcement; setStatus(node, text) for a local status line
   Components (return DOM nodes)
     header({title, count, appUrl, appLabel, fullscreen, refresh})  title, count line, Refresh, fullscreen, Open in Talent Search
     personCard(c, opts)      one person's summary card (article); opts: {headingLevel, saved, onAsk, extra}
     peopleTable(rows, st, h) real sortable table with selection; see people.js for the state it expects
     linksRow(c)              Open profile, LinkedIn, Website and every saved link, each named by host and person
     role(title, company)     "Title at Company", or whichever one there is
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
const PCARDS={};let ZONE=undefined,ZONE_SAID=false,ABS_USED=false;
/* what the chat app says it can do: a host that lists its abilities and leaves "message" or "updateModelContext" out can't do it */
function hostCan(what){const k=what==="message"?"message":what==="tell"?"updateModelContext":what;
 if(!HOST||!Object.keys(HOST).length)return true;return HOST[k]!=null&&HOST[k]!==false}
function goodZone(z){if(!z||typeof z!=="string")return undefined;try{new Intl.DateTimeFormat(undefined,{timeZone:z});return z}catch(e){return undefined}}
function zoneShort(){try{const p=new Intl.DateTimeFormat("en-US",{timeZone:ZONE,timeZoneName:"short"}).formatToParts(new Date()).find(x=>x.type==="timeZoneName");return p?p.value:ZONE}catch(e){return ZONE||""}}
function useZone(d){ZONE=goodZone(d&&(d.time_zone||d.tz))||goodZone(CTX&&CTX.timeZone);ZONE_SAID=!!goodZone(d&&(d.time_zone||d.tz))}
function post(m){window.parent.postMessage(m,"*")}
function req(method,params){const id=nid++;return new Promise(r=>{pend[id]=r;post({jsonrpc:"2.0",id,method,params})})}
function note(method,params){post({jsonrpc:"2.0",method,params:params||{}})}
function el(tag,attrs,...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs||{})){if(v==null||v===false)continue;if(k==="needs"){if(!hostCan(v))e.hidden=true;continue}
  if(k==="class")e.className=v;else if(k==="onclick")e.addEventListener("click",v);else e.setAttribute(k,v===true?"":v)}
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
const LINK_LABELS=__LINK_LABELS__;
function hostLabel(u){const x=safeUrl(u);if(!x)return"Portfolio";let h=x.hostname.toLowerCase().replace(/^www\./,"");const parts=h.split(".");
 for(let i=0;i<parts.length-1;i++){const n=LINK_LABELS[parts.slice(i).join(".")];if(n)return n}return"Portfolio"}
/* copyText(text, node, status, okText): copies; when the chat app blocks it, says so and shows the text in a box that is
   selected, so Ctrl+C or Cmd+C works. Resolves true when it was copied. */
function copyBox(text,near){
 const one=!/\n/.test(text)&&text.length<100;
 const f=one?el("input",{type:"text",readonly:"true",value:text,class:"field","aria-label":"The text to copy"}):el("textarea",{readonly:"true",rows:String(Math.min(10,text.split("\n").length+1)),class:"copyarea","aria-label":"The text to copy"},text);
 const close=el("button",{class:"btn",type:"button"},"Close");
 const box=el("div",{class:"linknote copynote",role:"group","aria-label":"Copy by hand"},el("div",{role:"status"},"The chat app blocked copying. The text is selected below: press Ctrl+C or Cmd+C."),f,el("div",{class:"acts"},close));
 close.addEventListener("click",()=>{box.remove();if(near&&near.isConnected&&near.focus)near.focus()});
 box.addEventListener("keydown",e=>{if(e.key==="Escape"){e.stopPropagation();close.click()}});
 const old=document.querySelector(".copynote");if(old)old.remove();
 if(near&&near.isConnected&&near.parentNode)near.parentNode.insertBefore(box,near.nextSibling);else{const ln=document.getElementById("linknote");if(ln)ln.append(box);else document.body.append(box)}
 f.focus();f.select();return box}
function copyText(text,node,status,okText){
 const fail=()=>{copyBox(text,node);if(status)status.textContent="The chat app blocked copying. The text is selected below.";return false};
 const done=()=>{if(status){status.textContent=okText||"Copied.";announce(status.textContent)}return true};
 try{if(navigator.clipboard&&navigator.clipboard.writeText)return navigator.clipboard.writeText(text).then(done,fail)}catch(e){}
 return Promise.resolve(fail())}
function showLinkNote(url,label,local){
 const box=document.getElementById("linknote");if(!box)return;
 const u=el("span",{class:"url"},url);
 const st=el("span",{class:"small muted",role:"status"});
 const copy=el("button",{class:"btn",type:"button"},"Copy link");
 copy.addEventListener("click",()=>copyText(url,box.firstChild,st,"Copied."));
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
/* Export xlsx for these people (shared by the People table and the Compare view): export_candidates_xlsx makes a file in
   Downloads (no email, phone or notes) or, when you have to choose, gives the Share page link. Text only, no HTML. */
function exportIds(ids,btn,out,notLoaded,kind){
 if(!ids.length){out.textContent="There is nobody to export yet.";return}
 const sent=ids.slice(0,200);
 btn.disabled=true;out.textContent="Making the file...";
 callTool("export_candidates_xlsx",{candidate_ids:sent,for_hiring_manager:true,kind:kind||"shortlist"}).then(r=>{btn.disabled=false;
  const d=r&&r.structuredContent;
  if(!d||(r&&r.isError)){out.textContent=said(r)||"Couldn't make the file.";return}
  const cut=ids.length>sent.length?" The first 200 of "+ids.length+" are in it.":"";
  const more=notLoaded>0?" ("+notLoaded+" more not loaded; open in Talent Search to export all)":"";
  const nf=d.not_found&&d.not_found.length?" Not found and left out: "+d.not_found.join(", ")+".":"";
  if(d.written){const cs=el("span",{class:"small muted",role:"status"});
   out.replaceChildren("Saved "+d.people+(d.people===1?" person":" people")+" to ",el("code",{class:"path"},d.path),". It has no email, phone or notes."+cut+nf+more+" ",
    el("button",{class:"link small",type:"button",onclick:()=>copyText(d.path,out,cs,"Path copied.")},"Copy path")," ",cs," ",appLink(d.share_url,"Open in Talent Search"))}
  else out.replaceChildren("No file made yet. Pick what goes in on the Share page. ",appLink(d.share_url,"Open the Share page"));
  announce(out.textContent)},()=>{btn.disabled=false;out.textContent="Couldn't make the file."})}
function profileLink(u,fallback,who){const x=safeUrl(u);if(!x)return null;const li=/(^|\.)linkedin\.com$/i.test(x.hostname);
 const text=li?"LinkedIn":(fallback||"Profile");
 return linkBtn(x.href,text,who?(li?"LinkedIn profile of "+who:text+" of "+who):null)}

/* ---- messages to Claude */
function markSent(btn){if(!btn||!btn.isConnected||btn.tagName!=="BUTTON")return;btn.disabled=true;
 if(!btn.nextSibling||!btn.nextSibling.classList||!btn.nextSibling.classList.contains("sentnote"))btn.after(el("span",{class:"sentnote small muted",role:"status"},"Sent to Claude"))}
function askClaude(text,btn){return req("ui/message",{role:"user",content:{type:"text",text}}).then(r=>{
 if(r&&r.isError){if(btn)btn.hidden=true;announce("Your chat app wouldn't send that message. You can type it in the chat yourself.");return false}
 announce("Sent to Claude.");markSent(btn);return true})}
/* askOrLink: askClaude for a button or select that would change something. Says what happened in the status node;
   if the chat app refuses, the control is hidden and an "Open ... in Talent Search" button takes its place. */
function askOrLink(text,ctl,status,url,label){return askClaude(text,ctl).then(ok=>{
 if(!status)return ok;
 if(ok){status.textContent="Sent to Claude. It will do this in the chat, and you can undo it in Talent Search.";return true}
 status.replaceChildren("Your chat app wouldn't send that. ",url?el("button",{class:"link",type:"button",onclick:()=>openLink(url,label||"Talent Search")},label?"Open "+label+" in Talent Search":"Open it in Talent Search"):"Type it in the chat yourself.");return false})}
/* confirmSend: a pick or button that changes something doesn't send at once. A line in place asks, with Send and Cancel. */
function confirmSend(anchor,question,text,status,url,label,after,yesLabel,only){
 const yes=el("button",{class:"btn primary",type:"button"},yesLabel||"Send"),no=el("button",{class:"btn",type:"button"},"Cancel");
 const row=el("div",{class:"acts sure",role:"group","aria-label":question},el("span",{},question),yes,no);
 const close=()=>{row.remove();if(anchor&&anchor.isConnected){anchor.hidden=false;if(after)after();else if(anchor.focus)anchor.focus()}};
 no.addEventListener("click",close);row.addEventListener("keydown",e=>{if(e.key==="Escape"){e.stopPropagation();close()}});
 yes.addEventListener("click",()=>{yes.disabled=no.disabled=true;askOrLink(text,null,status,url,label).then(ok=>{row.remove();if(anchor&&anchor.isConnected)anchor.hidden=false;
  if(ok){if(only)only.disabled=true;else if(anchor)anchor.querySelectorAll("button,select").forEach(b=>b.disabled=true);if(status){status.tabIndex=-1;status.focus()}}else if(anchor&&anchor.focus)anchor.focus()})});
 anchor.hidden=true;anchor.after(row);yes.focus();return row}
function tellClaude(obj,text){if(FAILED.tell||!hostCan("tell"))return Promise.resolve(false);
 return req("ui/update-model-context",{content:[{type:"text",text}],structuredContent:obj}).then(r=>{if(r&&r.isError){FAILED.tell=1;return false}return true})}
function callTool(name,args){return req("tools/call",{name,arguments:args||{}})}
function canCall(){return !!(HOST&&HOST.serverTools)}

/* ---- dates */
function parseD(iso){if(!iso)return null;const d=new Date(iso);return isNaN(d)?null:d}
function day(iso){const d=parseD(iso);if(!iso)return"";ABS_USED=true;try{return d?d.toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric",timeZone:ZONE}):iso}catch(e){return d.toLocaleDateString()}}
function when(iso){const d=parseD(iso);if(!iso)return"";ABS_USED=true;try{return d?d.toLocaleString(undefined,{weekday:"short",month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZone:ZONE}):iso}catch(e){return d.toLocaleString()}}
function dayKeyZ(d){try{return new Intl.DateTimeFormat("en-CA",{timeZone:ZONE,year:"numeric",month:"2-digit",day:"2-digit"}).format(d)}catch(e){return d.toISOString().slice(0,10)}}
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
function initials(n){const p=String(n||"").split(/\s+/).map(w=>w.replace(/[^\p{L}]/gu,"")).filter(Boolean);
 if(!p.length)return"?";return(Array.from(p[0])[0]+(p.length>1?Array.from(p[p.length-1])[0]:"")).toUpperCase()}
function colorCls(c){return COLORS.includes(c)?" c-"+c:""}
function avatar(c,small){return el("span",{class:"avatar"+(small?" sm":"")+colorCls(c&&c.color),"aria-hidden":"true"},initials(c&&c.name))}
function firstName(n){return String(n||"").trim().split(/\s+/)[0]||"them"}
function role(t,c){return t&&c?t+" at "+c:(t||c||"")}
function roleLine(c){return role(c.current_title,c.current_company)}
function jobLink(j,who,plain){const text=j.job||j.job_title||"a job";
 const url=j.app_url||(j.origin&&j.job_id?j.origin+"/jobs/"+j.job_id:null);
 if(!url)return el("span",{},text);
 const b=linkBtn(url,text,plain?null:"Open the "+text+" pipeline"+(who?" ("+who+" is in it)":""));if(plain)b.title="Open the pipeline in Talent Search";return b}
function linksRow(c,primary){
 const row=el("div",{class:"linksrow"});
 const nm=c.name||"them";
 if(c.app_url&&primary!==false)row.append(el("button",{class:"btn primary",type:"button","aria-label":"Open "+nm+" in Talent Search",onclick:()=>openLink(c.app_url,"the profile")},"Open profile"));
 const seen=new Set();
 const add=(u,text,aria)=>{const x=safeUrl(u);if(!x||seen.has(x.href))return;seen.add(x.href);row.append(el("button",{class:"btn",type:"button","aria-label":aria,onclick:()=>openLink(x.href,text)},text))};
 if(c.linkedin_url)add(c.linkedin_url,"LinkedIn","LinkedIn profile of "+nm);
 if(c.website)add(c.website,"Website","Website of "+nm);
 for(const x of c.links||[]){const u=typeof x==="string"?x:(x&&x.url);const l=(x&&x.label)||hostLabel(u);add(u,l,l+" of "+nm)}
 return row}
/* email (mailto and Copy), phone, for a saved person or prospect */
function contactRow(c){const nm=c.name||"them";const box=el("div",{class:"contact"});
 if(c.email){const st=el("span",{class:"small muted",role:"status"});
  box.append(el("a",{href:"mailto:"+c.email,"aria-label":"Email "+nm},c.email),
   el("button",{class:"btn",type:"button","aria-label":"Copy the email of "+nm,onclick:ev=>copyText(c.email,ev.currentTarget,st,"Copied.")},"Copy"),st)}
 if(c.phone)box.append(el("a",{href:"tel:"+String(c.phone).replace(/[^\d+]/g,""),"aria-label":"Call "+nm},c.phone));
 return box.childNodes.length?box:null}
function stageOf(c){const j=(c.jobs||[])[0];return j||null}
function stageText(j){return j?(j.stage+(j.job?", "+j.job:"")):""}
function stageChip(j,who){if(!j)return null;const d=daysSince(j.since);const out=j.state==="lost"||j.state==="hired";
 const text=j.stage+(j.state==="lost"?" (out)":"")+(j.job?" for "+j.job:"")+(d!=null?" · "+(d===0?"today":d+(d===1?" day":" days")):"");
 const url=j.app_url||(j.origin&&j.job_id?j.origin+"/jobs/"+j.job_id:null);
 if(url)return linkBtn(url,text,"Open the pipeline: "+text+(who?" ("+who+")":""),"link stagechip"+(out?" out":""));
 return el("span",{class:"stagechip"+(out?" out":"")},text)}
function isSample(c){return /sample/i.test(String(c.source||""))}
function personCard(c,o){o=o||{};const lvl=o.headingLevel||2;const nm=c.name||"Candidate";
 const card=el("article",{class:"pcard","aria-label":nm});
 const heading=el("h"+lvl,{class:"name",tabindex:"-1"},c.app_url?el("button",{class:"link name-link",type:"button",title:"Open "+nm+" in Talent Search",onclick:()=>openLink(c.app_url,"the profile")},nm):nm);
 const badges=[];
 if(o.saved)badges.push(el("span",{class:"badge"},o.saved));
 if(c.closest)badges.push(el("span",{class:"badge",title:"They match some of your words, not all of them."},"Partial match"));
 if(c.other_name)badges.push(el("span",{class:"badge"},"A different name"));
 if(isSample(c))badges.push(el("span",{class:"badge"},"Sample data"));
 const place=c.location;
 const who=el("div",{class:"who"},el("div",{class:"row",style:"justify-content:flex-start;align-items:center"},heading,...badges),
  roleLine(c)?el("div",{class:"sub"},roleLine(c)):null,
  (place||c.color)?el("div",{class:"meta"},place||null,(place&&c.color)?" · ":null,c.color?el("span",{},el("span",{class:"dot"+colorCls(c.color),"aria-hidden":"true"}),c.color+" tag"):null):null,
  stageChip(stageOf(c),nm));
 card.append(el("div",{class:"top"},avatar(c),who));
 if(c.match||c.reason)card.append(el("div",{class:"why"},c.reason?el("div",{class:"muted"},c.reason):null,c.match?el("div",{},marked(c.match)):null,
  c.closest?el("div",{class:"small muted"},"Partial match: this person has some of your words but not all of them."):null));
 const contact=el("div",{class:"contact"});
 if(c.email)contact.append(el("a",{href:"mailto:"+c.email,"aria-label":"Email "+nm},c.email));
 if(c.phone)contact.append(el("a",{href:"tel:"+String(c.phone).replace(/[^\d+]/g,""),"aria-label":"Call "+nm},c.phone));
 if(contact.childNodes.length)card.append(contact);
 card.append(linksRow(c));
 if(c.tags&&c.tags.length){
  if(o.onSkill){const hl=hlList(o.hl);const sum=hlSummary(c,hl);  // clickable skills: the same picks as the table
   if(sum)card.append(el("div",{class:"small tophint"+(hlTop(c,hl)?"":" muted")},sum));
   const lit=c.tags.filter(t=>hl.some(x=>x.skill===normKey(t)));const rest=c.tags.filter(t=>!lit.includes(t));
   card.append(el("ul",{class:"chips skillchips","aria-label":"Skills of "+nm},[...lit,...rest].map(t=>el("li",{},pickChip(t,o.hl,(n,ti)=>o.onSkill(n,ti))))))}
  else card.append(el("ul",{class:"chips","aria-label":"Skills"},c.tags.map(t=>el("li",{class:"chip"},t))))}
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
 const ask=el("button",{class:"btn",type:"button",needs:"message",onclick:()=>askClaude("Tell me more about "+nm+" (candidate id "+c.id+") and what to do next.",ask)},"Ask Claude about "+firstName(nm));
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
const CARD_WAIT={};  // person id -> the request already asking for their card (one request per person at a time)
function fetchCards(ids){const mine=ids.filter(i=>!PCARDS[i]);if(!mine.length||!canCall())return Promise.resolve();
 const need=mine.filter(i=>!CARD_WAIT[i]);
 if(need.length){const p=callTool("candidate_cards",{candidate_ids:need.slice(0,20)}).then(r=>{const d=r&&r.structuredContent;if(d&&d.cards)for(const c of d.cards)PCARDS[c.id]=c;
   for(const i of need)delete CARD_WAIT[i]},()=>{for(const i of need)delete CARD_WAIT[i]});
  for(const i of need)CARD_WAIT[i]=p}
 return Promise.all(mine.map(i=>CARD_WAIT[i]).filter(Boolean))}
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

function setNotice(t){const n=document.getElementById("notice");if(n)n.textContent=t||""}
/* ---- refresh and results */
const READ_TOOLS=["search_candidates","run_saved_search","get_candidate","job_pipeline","list_action_items","list_prospects","finalists","decide_view","compare_finalists","compare_candidates","list_jobs","list_saved_searches","list_interviews","status_digest","client_report","list_feedback_owed","find_duplicates","list_followups"];
function canRefresh(){return canCall()&&TOOL&&ARGS&&READ_TOOLS.includes(TOOL)}
function refresh(btn){if(btn)btn.disabled=true;announce("Refreshing.");
 callTool(TOOL,ARGS).then(r=>{if(btn)btn.disabled=false;const d=r&&r.structuredContent;if(r&&r.isError||!d){announce(said(r)||"Couldn't refresh.");return}showResult(d,r._meta)})}
function showResult(d,meta){DATA=d;if(meta)META=meta;let n;useZone(d);ABS_USED=false;setNotice("");
 try{n=render(d)}catch(e){n=errorState("Couldn't show this.")}
 const m=document.getElementById("main");m.replaceChildren(n||emptyState("Nothing to show."));
 if(ABS_USED&&ZONE_SAID&&ZONE){const h=m.querySelector(".vhead");const t=el("p",{class:"small muted zonenote",style:"margin:-4px 0 6px"},"Times in "+zoneShort());if(h)h.after(t);else m.prepend(t)}
 const dr=document.getElementById("drawer");if(dr){dr.replaceChildren();dr.hidden=true;m.hidden=false}
 setTimeout(()=>{try{sendSize()}catch(e){}},250)}

window.addEventListener("message",ev=>{if(ev.source!==window.parent)return;const m=ev.data;if(!m||m.jsonrpc!=="2.0")return;
 if(m.id!=null&&!m.method&&pend[m.id]){pend[m.id](m.error?{isError:true,content:[{type:"text",text:String(m.error.message||"That didn't work.")}]}:(m.result||{}));delete pend[m.id];return}
 const p=m.params||{};
 if(m.method==="ui/notifications/tool-input"){ARGS=p.arguments||{};setNotice("");if(!DATA)document.getElementById("main").replaceChildren(loadingState("Working on it..."))}
 else if(m.method==="ui/notifications/tool-input-partial"){if(!DATA)document.getElementById("main").replaceChildren(loadingState("Working on it..."))}
 else if(m.method==="ui/notifications/tool-cancelled"){
  if(DATA){setNotice("Claude stopped that request. What you see is from before.");announce("Claude stopped that request. What you see is from before.")}
  else document.getElementById("main").replaceChildren(emptyState("That was cancelled, so there is nothing to show."))}
 else if(m.method==="ui/notifications/tool-result"){META=p._meta||{};const d=p.structuredContent||null;
  if(d)showResult(d,p._meta);else document.getElementById("main").replaceChildren(p.isError?errorState(said(p)||"That didn't work."):emptyState("Nothing to show."))}
 else if(m.method==="ui/notifications/host-context-changed")mergeCtx(p);
 else if(m.method==="ui/resource-teardown"&&m.id!=null)post({jsonrpc:"2.0",id:m.id,result:{}});
});
let lastW=0,lastH=0;
/* the height the view needs: whichever is larger, the page or the bottom of the last thing on it, so the last block is never clipped */
function viewHeight(){const de=document.documentElement,b=document.body;const cs=getComputedStyle(b);
 const bottom=b.getBoundingClientRect().bottom+(window.scrollY||0)+(parseFloat(cs.paddingBottom)||0)+(parseFloat(cs.marginBottom)||0);
 return Math.ceil(Math.max(de.scrollHeight,bottom))}
function sendSize(){const w=document.documentElement.scrollWidth,h=viewHeight();if(isFull()){if(w!==lastW){lastW=w;note("ui/notifications/size-changed",{width:w})}return}
 if(w===lastW&&h===lastH)return;lastW=w;lastH=h;note("ui/notifications/size-changed",{width:w,height:h})}
{const ro=new ResizeObserver(sendSize);ro.observe(document.documentElement);ro.observe(document.body)}

req("ui/initialize",{protocolVersion:"2026-01-26",clientInfo:{name:"talent-search",version:V},appCapabilities:{availableDisplayModes:["inline","fullscreen"]}}).then(r=>{
 if(r&&r.isError){document.getElementById("main").replaceChildren(emptyState("This chat app couldn't start the view. The answer is in the text above."));return}
 HOST=(r&&r.hostCapabilities)||{};mergeCtx(r&&r.hostContext);note("ui/notifications/initialized",{})});

/* ---- people table: peopleTable(rows, st, h)
   rows: lean people (id, name, current_title, current_company, location, jobs[], match, last_contacted_at, created_at, updated_at, color, email, app_url)
   st:   {sel:Set of ids, sort:null|key, dir:"asc"|"desc", filter:""}, kept by the caller so it survives a redraw
   h:    {onOpen(person), onSel(), origin, more:{label,onClick}|null, total:Number,
          matchLabel:"Fit" (rename the Match column), hide:["added","updated","color",...] (hide columns),
          prospects:true (rows are Shortlist prospects: no "Open in Talent Search" for ids), bulkExtra(chosen)->[nodes] (more buttons when rows are picked)}
   Returns {node, focusCaption()}. A plain table with a caption, sort buttons inside th, name as th scope=row. */
function firstJob(p){return(p.jobs||[])[0]||null}
function plainMatch(p){return String(p.match||p.reason||"").split("**").join("")}
/* pipeline order as a number: the server's stage_rank when it sent one, else New, Screening, Interview, Offer, Hired, and exits last */
const STAGE_ORDER=[/^(new|sourced|applied|lead|prospect)/i,/screen/i,/interview|onsite|panel|assessment/i,/offer/i,/^(hired|placed|started|accepted)/i];
function stageRank(p){if(typeof p.stage_rank==="number")return p.stage_rank;const j=firstJob(p);if(!j)return null;
 if(typeof j.stage_rank==="number")return j.stage_rank;
 if(j.state==="lost"||/^(reject|declin|withdr|not a fit|out|client passed|passed)/i.test(j.stage||""))return 1000;
 const i=STAGE_ORDER.findIndex(r=>r.test(j.stage||""));return i<0?50:i}
function jobText(p){const js=p.jobs||[];if(!js.length)return"";const j=js[0];return(j.job||j.job_title||"")+(js.length>1?" +"+(js.length-1)+" more":"")}
function hasEmail(p){return!!(p.email||p.has_email)}
/* The order is what a recruiter reads first: Name, Title, Skills, then the job. opt:1 columns sit behind the Columns button. */
const TCOLS=[
 {k:"name",l:"Name",v:p=>p.name},
 {k:"title",l:"Title",v:p=>p.current_title},
 {k:"skills",l:"Skills",nosort:1,v:p=>(p.tags||[]).length},
 {k:"job",l:"Job",v:p=>jobText(p)},
 {k:"stage",l:"Stage",v:p=>stageRank(p)},
 {k:"email",l:"Email",v:p=>p.email||(p.has_email?"~":null)},
 {k:"linkedin",l:"LinkedIn",v:p=>p.linkedin_url?"a":null},
 {k:"match",l:"Match",v:plainMatch},
 {k:"contacted",l:"Last contacted",opt:1,full:1,v:p=>p.last_contacted_at,date:1},
 {k:"company",l:"Company",opt:1,full:1,v:p=>p.current_company},
 {k:"location",l:"Location",opt:1,full:1,v:p=>p.location},
 {k:"updated",l:"Updated",opt:1,full:1,v:p=>p.updated_at,date:1},
 {k:"added",l:"Added",opt:1,full:1,v:p=>p.created_at,date:1},
 {k:"color",l:"Color",opt:1,full:1,v:p=>{const i=COLORS.indexOf(p.color);return i<0?null:i}}];
/* highlights: a fixed color per pick (first pick red, then yellow, green, blue, purple, orange), kept while it stays picked.
   A pick is a skill ("python") or a job title ("title:data engineer"). People with more picks come first; on a tie the
   earlier pick wins. Nobody is hidden. Same rules as the Candidates page. Matching here is by skill tag (any case) and
   by the person's current title (case and spacing ignored); the server's own match is the one the app uses. */
const HL_PALETTE=["red","yellow","green","blue","purple","orange"];
function hlList(st){return st.hl||(st.hl=[])}
function normKey(t){return String(t||"").toLowerCase().split(/\s+/).filter(Boolean).join(" ")}
function hlIsTitle(x){return x.skill.indexOf("title:")===0}
function hlName(x){return hlIsTitle(x)?x.skill.slice(6):x.skill}
function titleCase(s){return String(s).replace(/(^|[\s/-])(\p{L})/gu,(m,a,b)=>a+b.toUpperCase())}
/* what a pick is called on screen and to a screen reader: "python" or "title: Data Engineer" */
function hlText(x){return hlIsTitle(x)?"title: "+titleCase(hlName(x)):x.skill}
function hlToggle(st,name,isTitle){const k=(isTitle?"title:":"")+normKey(name),l=hlList(st);const i=l.findIndex(x=>x.skill===k);
 if(i>=0){l.splice(i,1);return false}
 if(l.length>=HL_PALETTE.length)return null;
 l.push({skill:k,color:HL_PALETTE.find(c=>!l.some(x=>x.color===c))});return true}
/* the picks the server echoes ({name,color,kind}) as the view's own list */
function hlSeed(st,d){st.hl=((d&&d.highlight)||[]).filter(x=>x&&x.name).slice(0,HL_PALETTE.length).map(x=>({skill:(x.kind==="title"?"title:":"")+normKey(x.name),color:HL_PALETTE.includes(x.color)?x.color:"red"}))}
/* the picks as the tool's highlight argument, in click order, each with its color so a color stays with its pick */
function hlArgs(st){return hlList(st).map(x=>x.skill+":"+x.color)}
function hlFlags(p,l){const have=new Set((p.tags||[]).map(t=>normKey(t)));const ti=normKey(p.current_title||p.title);
 return l.map(h=>hlIsTitle(h)?(ti!==""&&ti===hlName(h)):have.has(h.skill))}
function hlCount(p,l){return hlFlags(p,l).filter(Boolean).length}
function hlTop(p,l){const f=hlFlags(p,l);const i=f.indexOf(true);return i<0?null:l[i]}
function hlOrder(rows,st){const l=hlList(st);if(!l.length)return rows;
 const idx=new Map(rows.map((p,i)=>[p,i]));const key=new Map(rows.map(p=>{const f=hlFlags(p,l);return[p,[-f.filter(Boolean).length,...f.map(x=>x?0:1)]]}));
 return rows.slice().sort((a,b)=>{const x=key.get(a),y=key.get(b);for(let i=0;i<x.length;i++)if(x[i]!==y[i])return x[i]-y[i];return idx.get(a)-idx.get(b)})}
function hlEdge(p,l){const t=hlTop(p,l);return t?t.color:null}
/* "Top match: python, red. 2 of 2", as on the Candidates page */
function hlSummary(p,l){if(!l.length)return null;const n=hlCount(p,l),t=hlTop(p,l);
 return t?"Top match: "+hlText(t)+", "+t.color+". "+n+" of "+l.length:"No highlighted picks. 0 of "+l.length}
/* how many people are in the top tier: the most picks anyone has */
function hlTierText(rows,st){const l=hlList(st);if(!l.length)return"The usual order is back.";
 let best=0,n=0;for(const p of rows){const c=hlCount(p,l);if(c>best){best=c;n=1}else if(c===best&&c>0)n++}
 return best?n+(n===1?" person is":" people are")+" in the top tier, with "+best+" of "+l.length+".":"Nobody has any of them."}
/* a pick made (or taken back): toggles it and says what happened. Returns {ok, text} */
function hlApply(st,name,isTitle,rows){const r=hlToggle(st,name,isTitle);
 if(r===null)return{ok:false,text:"You can highlight up to "+HL_PALETTE.length+" picks at once. Remove one first."};
 const k=(isTitle?"title:":"")+normKey(name);const x=hlList(st).find(y=>y.skill===k);
 const label=hlText({skill:k});
 if(r){const n=rows.filter(p=>hlFlags(p,[x])[0]).length;
  return{ok:true,added:true,text:"Highlighting "+label+" in "+x.color+". "+n+(n===1?" person has it":" people have it")+", shown first. "+hlTierText(rows,st)}}
 return{ok:true,added:false,text:"Removed the highlight on "+label+". "+hlTierText(rows,st)}}
/* a skill chip that is a pick button; the name carries the color and number once picked */
function pickChip(name,st,onPick,tag){const k=normKey(name),l=hlList(st);const i=l.findIndex(x=>x.skill===k);const h=i>=0?l[i]:null;
 return el(tag||"button",{type:"button",class:"chip skillbtn"+(h?" hl":""),"data-skill":k,"data-hl":h?h.color:null,"aria-pressed":h?"true":"false",
  "aria-label":h?name+", highlighted "+h.color+", pick "+(i+1)+" (remove)":null,
  title:h?"Remove the highlight":"Highlight this skill and move people who have it to the top",onclick:()=>onPick(name,false)},name)}
/* the bar of picks: sticky, one button per pick to remove it, and Clear */
function hlBarNode(st,onPick,onClear){
 const bar=el("div",{class:"hlbar",role:"group","aria-label":"Highlighted picks",hidden:true});
 function draw(){const l=hlList(st);bar.hidden=!l.length;bar.replaceChildren();if(!l.length)return;
  bar.append(el("strong",{class:"small"},"Highlighted:"),...l.map((x,i)=>el("button",{type:"button",class:"chip hl removable","data-hl":x.color,"data-skill":x.skill,"aria-label":"Remove highlight: "+hlText(x)+" ("+x.color+")",title:"Remove this highlight",onclick:()=>onPick(hlName(x),hlIsTitle(x),"bar")},hlText(x)+" ",el("span",{"aria-hidden":"true"},"\u00d7"))),
   el("button",{type:"button",class:"btn",onclick:onClear},"Clear"),
   el("span",{class:"small muted"},"People with more of these come first; when it's a tie, the pick you made first wins. Nobody is hidden."))}
 return{node:bar,draw}}
function cmpVals(a,b,date){if(date){a=parseD(a);b=parseD(b);a=a?+a:null;b=b?+b:null}
 if(typeof a==="string")a=a.toLowerCase();if(typeof b==="string")b=b.toLowerCase();
 return a<b?-1:a>b?1:0}
function sortedRows(rows,st){const idx=new Map(rows.map((p,i)=>[p.id,i]));let out=rows.slice();
 const f=(st.filter||"").trim().toLowerCase();
 if(f)out=out.filter(p=>[p.name,p.current_title,p.current_company,p.location,stageText(firstJob(p)),plainMatch(p),(p.tags||[]).join(" "),p.email].join(" ").toLowerCase().includes(f));
 if(st.hasEmail)out=out.filter(hasEmail);
 const col=TCOLS.find(c=>c.k===st.sort);
 if(col){const sign=st.dir==="desc"?-1:1;
  out.sort((x,y)=>{const a=col.v(x),b=col.v(y),ea=a==null||a==="",eb=b==null||b==="";
   if(ea&&eb)return idx.get(x.id)-idx.get(y.id);if(ea)return 1;if(eb)return-1;
   const c=cmpVals(a,b,col.date);return c?c*sign:idx.get(x.id)-idx.get(y.id)})}
 return hlOrder(out,st)}
function selNames(rows,st){return rows.filter(p=>st.sel.has(p.id))}
/* the ticked people in the order the screen shows them (the order the web Compare and Export take too), then any the filter hides */
function selOrdered(shown,rows,st){const seen=new Set(shown.map(p=>p.id));return[...shown.filter(p=>st.sel.has(p.id)),...rows.filter(p=>!seen.has(p.id)&&st.sel.has(p.id))]}
function peopleLabel(ps,noId){return ps.map(p=>noId?p.name:p.name+" (id "+p.id+")").join(", ")}
function peopleTable(rows,st,h){
 const wrap=el("div",{});
 const colLabel=c=>(c.k==="match"&&h.matchLabel)||c.l;
 const status=el("div",{class:"sel",role:"status","aria-live":"polite"});
 const caption=el("caption",{tabindex:"-1"});
 const tbody=el("tbody");
 const allBox=el("input",{type:"checkbox"});
 const filter=el("input",{class:"field tfilter",type:"search","aria-label":"Filter these people",placeholder:"Filter these",value:st.filter||"",style:"max-width:14rem;min-width:8rem"});
 const emailTog=el("button",{class:"btn",type:"button","aria-pressed":st.hasEmail?"true":"false"},"Has an email");
 const bulk=el("span",{class:"tbar",style:"margin:0"});
 const askRow=el("div",{class:"tbar",hidden:true});
 const askMsg=el("span",{class:"small",role:"alert",style:"color:var(--danger)"});
 const stat2=el("span",{class:"small muted",role:"status"});
 let shown=[],last=-1;
 const relBtn=el("button",{class:"btn",type:"button",onclick:()=>{st.sort=null;st.dir="asc";fill();heads();announce("Sorted by relevance, the order the search gave.")}},"Relevance");
 const ths=TCOLS.map(c=>{if(c.nosort)return{c,b:null,th:el("th",{scope:"col",class:"k-"+c.k},c.l)};
  const b=el("button",{class:"sort",type:"button"});b.addEventListener("click",()=>{
   if(st.sort===c.k)st.dir=st.dir==="asc"?"desc":"asc";else{st.sort=c.k;st.dir="asc"}
   fill();heads();announce("Sorted by "+colLabel(c)+", "+(st.dir==="asc"?"ascending":"descending"))});
  return{c,b,th:el("th",{scope:"col",class:"k-"+c.k},b)}});
 function heads(){for(const{c,b,th}of ths){if(!b)continue;const on=st.sort===c.k;
   th.setAttribute("aria-sort",on?(st.dir==="asc"?"ascending":"descending"):"none");
   b.replaceChildren(colLabel(c),el("span",{class:"arr","aria-hidden":"true"},on?(st.dir==="asc"?" ▲":" ▼"):""));
   b.setAttribute("aria-label","Sort by "+colLabel(c)+(on?", sorted "+(st.dir==="asc"?"ascending":"descending"):""))}
  relBtn.setAttribute("aria-pressed",st.sort?"false":"true")}
 function counts(){const chosen=selNames(rows,st);const inShown=shown.filter(p=>st.sel.has(p.id)).length;return{n:chosen.length,hidden:chosen.length-inShown,inShown}}
 function selText(){const c=counts();return c.n+" selected"+(c.hidden?", "+c.hidden+" hidden by the filter":"")}
 const chosenNow=()=>selOrdered(shown,rows,st);
 function selUI(){const c=counts(),n=c.n;status.textContent=n?selText()+(c.hidden?". The buttons below use all "+n+".":""):"";
  allBox.checked=!!shown.length&&c.inShown===shown.length;allBox.indeterminate=c.inShown>0&&c.inShown<shown.length;
  allBox.setAttribute("aria-label","Select all "+shown.length+" shown");
  tbody.querySelectorAll("input.rowpick").forEach(b=>{const on=st.sel.has(+b.dataset.id);b.checked=on;b.closest("tr").classList.toggle("sel",on)});
  const chosen=chosenNow();bulk.replaceChildren();
  if(n){const emails=chosen.filter(p=>p.email).map(p=>p.email);
   bulk.append(
    el("button",{class:"btn askbtn",type:"button",needs:"message",onclick:()=>{askRow.hidden=false;askMsg.textContent="";askIn.focus()}},"Ask Claude about these "+n),
    h.prospects?null:el("button",{class:"btn",type:"button",onclick:()=>{announce("Opening "+n+(n===1?" person":" people")+" in Talent Search.");openLink((h.origin||"")+"/candidates?ids="+chosen.map(p=>p.id).join(","),"the Candidates page")}},"Open "+n+" in Talent Search"),
    el("button",{class:"btn",type:"button",onclick:()=>{const t=emails.join(", ");
     if(!t){stat2.textContent="None of them has an email saved.";announce(stat2.textContent);return}
     copyText(t,stat2,stat2,"Copied "+emails.length+" email"+(emails.length===1?"":"s")+(emails.length<n?" ("+(n-emails.length)+" had none saved).":".")+"")}},"Copy "+emails.length+" email"+(emails.length===1?"":"s")),
    ...(h.bulkExtra?h.bulkExtra(chosen):[]))}
  else{askRow.hidden=true}}
 function changed(){selUI();if(!counts().n)announce("0 selected");if(h.onSel)h.onSel()}
 const askIn=el("input",{class:"field",type:"text","aria-label":"What should Claude do with them?",placeholder:"What should Claude do with them?",maxlength:"500"});
 const closeAsk=()=>{askRow.hidden=true;const b=bulk.querySelector(".askbtn");if(b)b.focus()};
 const sendAsk=()=>{const t=askIn.value.trim();if(!t){askMsg.textContent="Type what you want Claude to do first.";askIn.focus();return}askMsg.textContent="";const chosen=chosenNow();
  askClaude("For these "+chosen.length+" candidates: "+peopleLabel(chosen,h.prospects)+": "+t,null).then(ok=>{if(ok){askIn.value="";askRow.hidden=true;stat2.textContent="Sent to Claude.";const b=bulk.querySelector(".askbtn");if(b)b.focus()}else{askMsg.textContent="Your chat app wouldn't send that. You can type it in the chat yourself."}})};
 const sendBtn=el("button",{class:"btn primary",type:"button",onclick:sendAsk},"Send to Claude");
 askIn.addEventListener("keydown",e=>{if(e.key==="Enter")sendAsk();if(e.key==="Escape"){e.stopPropagation();closeAsk()}});
 askRow.append(askIn,sendBtn,el("button",{class:"btn",type:"button",onclick:closeAsk},"Cancel"),askMsg);
 function setSel(p,on){if(on)st.sel.add(p.id);else st.sel.delete(p.id)}
 const picksOn=!!h.skills;  // the People table has picks and the Columns button; the Shortlist and Decide tables keep every column
 function pickTitleBtn(p){const t=p.current_title;if(!t)return"";
  const k="title:"+normKey(t),l=hlList(st),i=l.findIndex(x=>x.skill===k),x=i>=0?l[i]:null;
  return el("button",{type:"button",class:"titlebtn"+(x?" chip hl":""),"data-hl":x?x.color:null,"data-title":normKey(t),"aria-pressed":x?"true":"false",
   "aria-label":x?"title: "+titleCase(normKey(t))+", highlighted "+x.color+", pick "+(i+1)+" (remove)":"Highlight title: "+t,
   title:x?"Remove the highlight":"Highlight this title and move people who have it to the top",onclick:()=>pickAny(t,true,"row")},t)}
 function fill(){shown=sortedRows(rows,st);tbody.replaceChildren();
  caption.textContent=shown.length+" candidate"+(shown.length===1?"":"s")+(shown.length<rows.length?" shown of "+rows.length:"")+(h.total&&h.total>rows.length?" (the search found "+h.total+")":"");
  shown.forEach((p,i)=>{
   const pick=el("input",{type:"checkbox",class:"rowpick","data-id":p.id,"aria-label":"Select "+p.name});
   pick.addEventListener("click",ev=>{const on=pick.checked;
    if(ev.shiftKey&&last>=0){const[a,b]=[Math.min(last,i),Math.max(last,i)];for(let j=a;j<=b;j++)setSel(shown[j],on)}else setSel(p,on);
    last=i;changed()});
   const open=el("button",{class:"link",type:"button","aria-label":"Show card for "+p.name,onclick:()=>h.onOpen(p)},p.name);
   const j=firstJob(p);const hl=hlList(st);
   const mine=(p.tags||[]).map(String);
   const lit=mine.filter(t=>hl.some(x=>x.skill===normKey(t))).sort((a,b)=>hl.findIndex(x=>x.skill===normKey(a))-hl.findIndex(x=>x.skill===normKey(b)));
   const rest=mine.filter(t=>!lit.includes(t));const cut=Math.max(4,lit.length);const vis=[...lit,...rest].slice(0,cut);
   const sum=hlSummary(p,hl);
   const skillCell=el("td",{class:"k-skills","data-label":mine.length?"Skills":null},
    sum?el("div",{class:"small tophint"+(hlTop(p,hl)?"":" muted")},sum):null,
    mine.length?el("ul",{class:"chips skillchips","aria-label":"Skills of "+p.name},
    vis.map(t=>el("li",{},pickChip(t,st,(n,ti)=>pickAny(n,ti,"row")))),
    mine.length>vis.length?el("li",{class:"small muted"},"+"+(mine.length-vis.length)):null):null);
   const td=(k,label,...kids)=>el("td",{class:"k-"+k,"data-label":label},...kids);
   const txt=(k,label,v)=>el("td",{class:"k-"+k,"data-label":v?label:null,title:v||null},v||"");
   const titleCell=el("td",{class:"k-title","data-label":p.current_title?"Title":null,title:p.current_title||null},picksOn?pickTitleBtn(p):(p.current_title||""));
   const mt=el("td",{class:"match k-match","data-label":plainMatch(p)?(h.matchLabel||"Match"):null,title:plainMatch(p)||null},el("div",{class:"clamp"},p.match?marked(p.match):(p.reason||"")));
   const em=el("td",{class:"k-email","data-label":hasEmail(p)?"Email":null},hasEmail(p)?el("span",{class:"chk",title:p.email||"Has an email","aria-label":"Has an email"},"✓"):null,
    p.email?el("a",{class:"addr",href:"mailto:"+p.email,"aria-label":"Email "+p.name},p.email):null);
   const li=el("td",{class:"k-linkedin","data-label":p.linkedin_url?"LinkedIn":null},p.linkedin_url&&safeUrl(p.linkedin_url)?linkBtn(p.linkedin_url,"LinkedIn","LinkedIn profile of "+p.name):null);
   const edge=hlEdge(p,hl);
   tbody.append(el("tr",{class:edge?"hlrow":null,"data-edge":edge},el("td",{class:"pickcell"},pick),el("th",{scope:"row",class:"k-name"},open),
    titleCell,skillCell,
    el("td",{class:"k-job","data-label":j?"Job":null,title:(p.jobs||[]).map(x=>x.job||x.job_title).join("; ")||null},jobText(p)),
    el("td",{"data-label":j?"Stage":null,title:j?stageText(j):null,class:"k-stage"+(j&&(j.state==="lost"||j.state==="hired")?" out":"")},j?j.stage:""),
    em,li,mt,td("contacted",p.last_contacted_at?"Last contacted":null,dateEl(p.last_contacted_at)),
    txt("company","Company",p.current_company),txt("location","Location",p.location),
    td("updated",p.updated_at?"Updated":null,dateEl(p.updated_at)),td("added",p.created_at?"Added":null,dateEl(p.created_at)),
    el("td",{class:"k-color","data-label":p.color?"Color":null},p.color?el("span",{},el("span",{class:"dot"+colorCls(p.color),"aria-hidden":"true"}),p.color):"")))});
  if(!shown.length)tbody.append(el("tr",{},el("td",{colspan:"15"},"Nobody matches that filter.")));
  selUI();edges()}
 filter.addEventListener("input",()=>{st.filter=filter.value;fill()});
 emailTog.addEventListener("click",()=>{st.hasEmail=!st.hasEmail;emailTog.setAttribute("aria-pressed",st.hasEmail?"true":"false");fill();announce(st.hasEmail?"Showing only people with an email. "+shown.length+" shown.":"Showing everyone. "+shown.length+" shown.")});
 allBox.addEventListener("click",()=>{for(const p of shown)setSel(p,allBox.checked);changed()});
 const none=el("button",{class:"btn",type:"button",onclick:()=>{st.sel.clear();changed()}},"Select none");
 const all=el("button",{class:"btn",type:"button",onclick:()=>{for(const p of shown)st.sel.add(p.id);changed()}},"Select all shown");
 const bar=hlBarNode(st,(n,ti,from)=>pickAny(n,ti,from),()=>{st.hl=[];refresh();if(h.onPick)h.onPick();announce("Highlights cleared. The usual order is back.");caption.focus({preventScroll:true})});
 function refresh(){bar.draw();const keep=scroller.scrollLeft;fill();scroller.scrollLeft=keep;edges()}
 function pickAny(name,isTitle,from){const r=hlApply(st,name,isTitle,rows);announce(r.text);if(!r.ok)return;
  refresh();if(h.onPick)h.onPick();
  const k=(isTitle?"title:":"")+normKey(name);
  if(r.added){const b=bar.node.querySelector('button[data-skill="'+CSS.escape(k)+'"]');if(b)b.focus({preventScroll:true})}
  else{const nb=bar.node.querySelector("button.removable");(nb||caption).focus({preventScroll:true})}}
 /* columns: what is always out of the way (no data, or the caller hides it) and what the person turns on or off */
 const staticHide=new Set([...(h.hide||[]),...(rows.some(p=>COLORS.includes(p.color))?[]:["color"]),...(h.linkedin?[]:["linkedin"]),...(!picksOn||rows.some(p=>plainMatch(p))?[]:["match"]),...(h.skills&&rows.some(p=>(p.tags||[]).length)?[]:["skills"])]);
 const optKeys=TCOLS.filter(c=>c.opt&&!staticHide.has(c.k)).map(c=>c.k);
 const colsNow=()=>st.cols||new Set(isFull()?optKeys:[]);  // a wide view starts with every column; the choice is kept once made
 const table=el("table",{class:"ptable"+(h.prospects?" prospects":"")},caption,el("thead",{},el("tr",{},el("th",{scope:"col",class:"pickcell"},allBox),...ths.map(t=>t.th))),tbody);
 function applyCols(){const on=colsNow();for(const c of TCOLS)table.classList.toggle("hide-"+c.k,!!(staticHide.has(c.k)||(picksOn&&c.opt&&!on.has(c.k))))}
 let colsBtn=null,colsPanel=null;
 if(picksOn&&optKeys.length){
  colsBtn=el("button",{class:"btn",type:"button","aria-expanded":"false","aria-controls":"colspanel"},"Columns");
  colsPanel=el("div",{class:"colspanel",id:"colspanel",role:"group","aria-label":"Columns to show",hidden:true},
   el("span",{class:"small muted"},"Show more columns:"),
   ...optKeys.map(k=>{const c=TCOLS.find(x=>x.k===k);const box=el("input",{type:"checkbox","data-col":k,checked:colsNow().has(k)?"":null});
    box.checked=colsNow().has(k);
    box.addEventListener("change",()=>{const cur=new Set(colsNow());if(box.checked)cur.add(k);else cur.delete(k);st.cols=cur;applyCols();edges();
     announce(c.l+" column "+(box.checked?"shown":"hidden")+".")});
    return el("label",{class:"colopt"},box,el("span",{},c.l))}));
  const toggle=open=>{colsPanel.hidden=!open;colsBtn.setAttribute("aria-expanded",open?"true":"false")};
  colsBtn.addEventListener("click",()=>{toggle(colsPanel.hidden);if(!colsPanel.hidden){const f=colsPanel.querySelector("input");if(f)f.focus()}});
  colsPanel.addEventListener("keydown",e=>{if(e.key==="Escape"){e.stopPropagation();toggle(false);colsBtn.focus()}})}
 applyCols();
 const tools=el("div",{class:"tbar"},filter,emailTog,relBtn,all,none,colsBtn,status,h.more?el("button",{class:"btn",type:"button",onclick:ev=>h.more.onClick(ev.currentTarget)},h.more.label):null);
 const scroller=el("div",{class:"tablewrap",tabindex:"-1"},table);
 const eL=el("span",{class:"edge l","aria-hidden":"true"}),eR=el("span",{class:"edge r","aria-hidden":"true"});
 const hint=el("p",{class:"small muted scrollhint",hidden:true},"There are more columns to the right. Scroll sideways to see them.");
 function edges(){const max=scroller.scrollWidth-scroller.clientWidth;const x=scroller.scrollLeft;const more=max>4&&x<max-2;
  eL.classList.toggle("on",x>2);eR.classList.toggle("on",more);hint.hidden=!more}
 scroller.addEventListener("scroll",edges,{passive:true});
 if(window.ResizeObserver){const ro=new ResizeObserver(edges);ro.observe(scroller);ro.observe(table)}
 wrap.append(tools,colsPanel,bulk,askRow,stat2,bar.node,hint,el("div",{class:"tablebox"},scroller,eL,eR));
 heads();bar.draw();fill();
 return{node:wrap,focusCaption:()=>caption.focus(),scroller,edges,chosen:chosenNow,shown:()=>shown}}
