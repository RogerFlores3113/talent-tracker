const KEY="talent-search/shortlist";
function viewKey(){const k=META&&META[KEY];return k&&k.job_id?k:null}
function canAct(){return !!(HOST&&HOST.serverTools&&viewKey())}
function cardKey(ids){return req("tools/call",{name:"shortlist_card_key",arguments:{job_id:viewKey().job_id,prospect_ids:ids}})}
let countsLine=null,stageSel=null,bulkBox=null,ORIGIN="",MODE="cards",ROWS=[],listHost=null,modeBtn=null,tableApi=null;const CARDS={};
const TST={sel:new Set(),sort:null,dir:"asc",filter:""};
function countText(c){c=c||{};return(c.proposed||0)+" waiting · "+(c.kept||0)+" kept · "+(c.passed||0)+" passed on"}
function stageArg(){return stageSel&&stageSel.value&&stageSel.selectedIndex>0?{stage:stageSel.value}:{}}
function call(tool,ids,args){const k=viewKey();return cardKey(ids).then(kr=>{const key=kr&&!kr.isError&&kr.structuredContent&&kr.structuredContent.key;if(!key)return kr;
 return new Promise(go=>setTimeout(go,450)).then(()=>req("tools/call",{name:tool,arguments:Object.assign({job_id:k.job_id,view_key:key},args)}))})}
function remember(p,extra){ROWS=ROWS.map(x=>x.id===p.id?Object.assign({},x,p,extra||{}):x)}
function swap(p,extra,msg){remember(p,extra);const old=CARDS[p.id];const next=prospect(Object.assign({},ROWS.find(x=>x.id===p.id)||p,extra||{}));if(msg)next.querySelector(".status").textContent=msg;if(old)old.replaceWith(next);return next}
function act(card,p,tool,args){
 const status=card.querySelector(".status");
 card.querySelectorAll("button.btn,input").forEach(b=>b.disabled=true);
 status.textContent="Working on it…";
 call(tool,[p.id],Object.assign({prospect_id:p.id},tool==="keep_prospect"?stageArg():{},args)).then(r=>{
  const d=(r&&r.structuredContent)||null;
  if(r&&r.isError||!d){status.textContent=said(r)||"That didn't work. Try again in Talent Search.";card.querySelectorAll("button.btn,input").forEach(b=>b.disabled=false);return}
  if(d.counts&&countsLine)countsLine.textContent=countText(d.counts);
  swap(Object.assign({},d.prospect,d.candidate_url?{candidate_url:d.candidate_url}:{}),d.needs_choice?{choices:d.matches||[]}:{},d.message||said(r));refreshBulk()});
}
function waitingIds(only){return Object.values(CARDS).filter(c=>c.dataset.waiting==="1"&&(!only||(c.querySelector("input.pick")||{}).checked)).map(c=>+c.dataset.id)}
let BUSY=false;
function bulk(tool,ids,args,status,box){
 box=box||bulkBox;
 if(BUSY)return;
 if(!ids.length){status.textContent="Nobody picked yet.";return}
 BUSY=true;
 box.querySelectorAll("button.btn,input,select").forEach(b=>b.disabled=true);status.textContent="Working on it…";
 call(tool,ids,Object.assign({prospect_ids:ids},args)).then(r=>{
  BUSY=false;const d=(r&&r.structuredContent)||null;box.querySelectorAll("button.btn,input,select").forEach(b=>b.disabled=false);
  if(r&&r.isError||!d){status.textContent=said(r)||"That didn't work. Try again in Talent Search.";return}
  if(d.counts&&countsLine)countsLine.textContent=countText(d.counts);
  for(const p of d.prospects||[]){if(MODE==="table"){remember(p);TST.sel.delete(p.id)}else swap(p)}
  if(MODE==="table"){drawList();if(tableApi)tableApi.focusCaption()}
  status.textContent=d.message||said(r);announce(status.textContent);refreshBulk()});
}
function refreshBulk(){if(!bulkBox)return;const n=waitingIds(false).length,k=waitingIds(true).length;
 bulkBox.style.display=(MODE==="cards"&&n>1)?"":"none";const all=bulkBox.querySelector(".keep-all");if(all)all.textContent="Keep all ("+n+")";const sel=bulkBox.querySelector(".keep-sel");if(sel){sel.textContent=k?"Keep selected ("+k+")":"Keep selected";sel.disabled=!k}}
function bulkBar(d){
 const status=el("div",{class:"status muted",role:"status"});
 const box=el("div",{class:"card"});
 const why=el("input",{class:"reason",type:"text",maxlength:"300",placeholder:"Reason for passing on all (optional)","aria-label":"Why you're passing on all"});
 const row=el("div",{class:"acts"},
  el("button",{class:"btn primary keep-all",onclick:()=>bulk("keep_prospects",waitingIds(false),stageArg(),status)},"Keep all"),
  el("button",{class:"btn keep-sel",onclick:()=>bulk("keep_prospects",waitingIds(true),stageArg(),status)},"Keep selected"),
  el("button",{class:"btn",onclick:()=>{row.replaceWith(passRow);why.focus()}},"Pass all"));
 const passRow=el("div",{class:"acts"},why,
  el("button",{class:"btn",onclick:()=>{bulk("pass_prospects",waitingIds(false),{reason:why.value},status);passRow.replaceWith(row)}},"Pass on all"),
  el("button",{class:"btn",onclick:()=>passRow.replaceWith(row)},"Cancel"));
 why.addEventListener("keydown",e=>{if(e.key==="Enter")passRow.querySelector("button.btn").click()});
 box.append(row,status);return box}
function sameLabel(m){return"Same as "+m.name+(m.current_company?", "+m.current_company:m.current_title?", "+m.current_title:"")}
function buttons(card,p){
 const box=el("div",{class:"acts"});
 const same=(p.choices&&p.choices.length?p.choices:(p.checks&&p.checks.same_name)||[]);
 const keep=(label,args,primary)=>el("button",{class:"btn"+(primary?" primary":""),onclick:()=>act(card,p,"keep_prospect",args)},label);
 if(same.length){for(const m of same)box.append(keep(sameLabel(m),{candidate_id:m.id},false));box.append(keep("Someone new",{as_new:true},false))}
 else box.append(keep("Keep",{},true));
 const pass=el("button",{class:"btn",onclick:()=>{
  const why=el("input",{class:"reason",type:"text",maxlength:"300",placeholder:"Reason (optional)","aria-label":"Why you're passing"});
  const go=el("button",{class:"btn",onclick:()=>act(card,p,"pass_prospect",{reason:why.value})},"Pass");
  const back=el("button",{class:"btn",onclick:()=>box.replaceWith(buttons(card,p))},"Cancel");
  why.addEventListener("keydown",e=>{if(e.key==="Enter")go.click()});
  const form=el("div",{class:"acts"},why,go,back);box.replaceWith(form);why.focus()}},"Pass");
 box.append(pass);return box}
function cardUrl(id){return ORIGIN&&id?ORIGIN+"/candidates/"+id:null}
/* A saved person this prospect matches: who they are, where they work, which jobs and stages, and a way to open their card. */
function savedBlock(m,lead){
 const rl=role(m.current_title,m.current_company);
 const jobs=(m.pipelines||[]).map(x=>x.job+": "+x.stage);
 return el("div",{class:"savedbox small"},lead+" ",personLink(m.name,cardUrl(m.id),m.id),rl?", "+rl:"",". ",jobs.length?"In "+jobs.join("; ")+".":"Not in any job yet.")}
function savedBlocks(p){const out=[];const c=p.checks||{};
 if(c.saved)out.push(savedBlock(c.saved,c.saved.same_name===false?"Already saved under a different name:":"Already saved as"));
 for(const m of c.same_name||[])out.push(savedBlock(m,"Same name as saved"));
 if(!out.length)for(const m of p.choices||[])out.push(savedBlock(m,"Could be"));
 return out}
function prospect(p){const sub=role(p.title,p.company)+(p.location?" · "+p.location:"");
 const waiting=(p.status||"proposed")==="proposed";
 const badge=p.status==="passed"?"Passed":p.status==="kept"?("Kept"+(p.stage?", in "+p.stage:"")):null;
 const lines=(p.check_lines||[]).filter(x=>!/^(Already saved as|Same name as saved) /.test(x));
 const top=lines.filter(x=>/^Passed on for /.test(x)),rest=lines.filter(x=>!/^Passed on for /.test(x));
 const pick=waiting&&canAct()?el("input",{type:"checkbox",class:"pick","aria-label":"Select "+p.name}):null;
 if(pick)pick.addEventListener("change",refreshBulk);
 const hist=(p.work_history||[]);
 const links=linksRow({name:p.name,linkedin_url:p.linkedin_url,links:p.links},false);
 const card=el("div",{class:"card"},
  top.length?el("div",{class:"small"},el("b",{},top.join(". "))):null,
  el("div",{class:"row"},el("b",{},pick,pick?" ":null,p.name,badge?el("span",{class:"badge"},badge):null)),
  sub?el("div",{class:"muted small"},sub):null,p.fit?el("div",{class:"small"},p.fit):null,
  links.childNodes.length?links:null,
  ...savedBlocks(p),
  (p.tags&&p.tags.length)?el("div",{class:"chips"},p.tags.slice(0,8).map(t=>el("span",{class:"chip"},t))):null,
  (p.about||hist.length)?el("details",{class:"small"},el("summary",{},"About and work history"),p.about?el("p",{},p.about):null,
   hist.length?el("ul",{},hist.map(w=>el("li",{},[w.title,w.company&&("at "+w.company)].filter(Boolean).join(" ")+((w.start||w.end)?" ("+(w.start||"")+" to "+(w.end||"now")+")":"")))):null):null,
  p.pass_reason?el("div",{class:"small muted"},"Reason: "+p.pass_reason):null,
  rest.length?el("ul",{class:"small"},rest.map(x=>el("li",{},x))):null,
  p.status==="kept"&&p.candidate_url?el("div",{class:"small"},appLink(p.candidate_url,"Open "+p.name+" in Talent Search")):null);
 card.dataset.id=p.id;card.dataset.waiting=waiting?"1":"0";
 if(waiting&&canAct())card.append(buttons(card,p));
 card.append(el("div",{class:"status muted",role:"status"}));
 CARDS[p.id]=card;return card}

/* ---- table mode: the people table, with Keep selected and Pass selected */
function statusWord(p){const s=p.status||"proposed";const c=p.checks||{};
 if(s==="kept")return"Kept"+(p.stage?", in "+p.stage:"");if(s==="passed")return"Passed";
 return"Waiting"+((c.saved||(c.same_name||[]).length)?" (saved)":"")}
function toRow(p){return{id:p.id,name:p.name,current_title:p.title,current_company:p.company,location:p.location,match:p.fit,tags:p.tags,email:p.email,
 jobs:[{stage:statusWord(p),state:p.status==="passed"?"lost":null}],_p:p}}
function openProspect(row,returnTo){
 const main=document.getElementById("main"),drawer=document.getElementById("drawer");const p=row._p;
 const back=()=>{drawer.replaceChildren();drawer.hidden=true;main.hidden=false;drawList();if(returnTo&&returnTo.isConnected)returnTo.focus()};
 main.hidden=true;drawer.hidden=false;
 const bb=el("button",{class:"btn",type:"button",onclick:back},"Back to the table");
 drawer.replaceChildren(el("div",{class:"drawer"},el("div",{class:"acts",style:"margin:0 0 8px"},bb),prospect(ROWS.find(x=>x.id===p.id)||p)));
 drawer.addEventListener("keydown",e=>{if(e.key==="Escape")back()},{once:true});bb.focus();announce(p.name+", opened.")}
let tableStatus=null,passForm=null,tableBox=null;
function tableExtra(chosen){const ids=chosen.map(r=>r._p).filter(p=>(p.status||"proposed")==="proposed").map(p=>p.id);
 if(!ids.length||!canAct())return[];
 return[el("button",{class:"btn primary",type:"button","aria-label":"Keep "+ids.length+" selected",onclick:()=>bulk("keep_prospects",ids,stageArg(),tableStatus,tableBox)},"Keep selected ("+ids.length+")"),
  el("button",{class:"btn",type:"button","aria-label":"Pass on "+ids.length+" selected",onclick:()=>{passForm.hidden=false;passForm.dataset.ids=ids.join(",");passForm.querySelector("input").focus()}},"Pass selected ("+ids.length+")")]}
function tableChrome(){
 tableStatus=el("div",{class:"status muted",role:"status"});
 const why=el("input",{class:"reason",type:"text",maxlength:"300",placeholder:"Reason for passing (optional)","aria-label":"Why you're passing on these"});
 const go=el("button",{class:"btn",type:"button",onclick:()=>{const ids=(passForm.dataset.ids||"").split(",").filter(Boolean).map(Number);passForm.hidden=true;bulk("pass_prospects",ids,{reason:why.value},tableStatus,tableBox);why.value=""}},"Pass on them");
 const no=el("button",{class:"btn",type:"button",onclick:()=>{passForm.hidden=true}},"Cancel");
 why.addEventListener("keydown",e=>{if(e.key==="Enter")go.click();if(e.key==="Escape"){e.stopPropagation();no.click()}});
 passForm=el("div",{class:"acts",hidden:true},why,go,no);
 tableBox=el("div",{},passForm,tableStatus);return tableBox}
function drawList(){
 if(!listHost)return;
 for(const k of Object.keys(CARDS))delete CARDS[k];
 if(modeBtn){modeBtn.textContent=MODE==="table"?"Show as cards":"Show as a table";modeBtn.setAttribute("aria-pressed",MODE==="table"?"true":"false")}
 if(tableBox)tableBox.hidden=MODE!=="table";
 if(MODE==="table"&&ROWS.length){
  const rows=ROWS.map(toRow);tableApi=peopleTable(rows,TST,{onOpen:r=>openProspect(r,null),onSel:()=>{},origin:ORIGIN,prospects:true,matchLabel:"Fit",hide:["contacted","added","updated","color"],bulkExtra:tableExtra,total:rows.length});
  listHost.replaceChildren(tableApi.node)}
 else{listHost.replaceChildren(...ROWS.map(prospect))}
 refreshBulk()}
function render(d){
 const wrap=el("div",{});ROWS=(d.added||d.prospects||[]).slice();const c=d.counts||{};ORIGIN=appOrigin(d.app_url);MODE="cards";
 wrap.append(header({title:"Shortlist"+(d.job?": "+d.job.title+(d.job.company?" · "+d.job.company:""):""),appUrl:d.app_url,appLabel:"Review in Talent Search",refresh:false}));
 countsLine=el("div",{class:"small muted"},countText(c));wrap.append(countsLine);
 const waiting=ROWS.some(p=>(p.status||"proposed")==="proposed");
 const ks=d.keep_stages||[];
 if(ROWS.length>1){modeBtn=el("button",{class:"btn",type:"button","aria-pressed":"false"},"Show as a table");
  modeBtn.addEventListener("click",()=>{MODE=MODE==="table"?"cards":"table";drawList();announce(MODE==="table"?"Showing a table. Pick people, then keep or pass them.":"Showing cards.")});
  wrap.append(el("div",{class:"tbar"},modeBtn))}
 if(waiting&&canAct()&&ks.length>1){stageSel=el("select",{"aria-label":"Keep into",class:"field"},ks.map(s=>el("option",{value:s},s)));
  wrap.append(el("div",{class:"small acts"},el("span",{},"Keep into"),stageSel,el("span",{class:"muted"},"(pick "+ks[1]+" if you already messaged them)")))}
 if(waiting&&canAct()){bulkBox=bulkBar(d);wrap.append(bulkBox)}
 if(d.added)wrap.append(el("h2",{},"Added now"));
 if(!ROWS.length)wrap.append(el("p",{class:"empty"},"Nobody here."));
 wrap.append(tableChrome());
 listHost=el("div",{});wrap.append(listHost);drawList();
 if(waiting)wrap.append(el("p",{class:"small muted"},canAct()?"Keep saves them and adds them to the job. You can undo a keep in Talent Search, under Changes.":"Keep or pass each one in Talent Search."));
 if(d.skipped&&d.skipped.length){wrap.append(el("h2",{},"Skipped"),el("ul",{class:"small"},d.skipped.map(x=>el("li",{},x.name+": "+x.reason))))}
 return wrap}
