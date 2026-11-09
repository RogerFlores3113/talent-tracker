/* People view: search results, one candidate, or a saved candidate. Cards, a carousel to flip through, and a table. */
const FIELD={current_title:"title",current_company:"company",linkedin_url:"LinkedIn",tags:"skills",work_history:"work history",last_contacted_at:"last contacted"};
function savedText(s){if(s==null||s===false)return null;if(typeof s==="string")return s;
 const changed=s.changed||s.fields||s.updated||[];
 if(s.created||s.status==="saved"||s.status==="created"||s.status==="new")return"Saved";
 if(Array.isArray(changed)&&changed.length)return"Updated: "+changed.map(f=>FIELD[f]||String(f).replace(/_/g," ")).join(", ");
 if(s.status==="unchanged"||s.status==="same")return"Already saved, nothing new";
 return s.label||s.text||(s.status?String(s.status):null)}
const S={people:[],total:0,idx:0,mode:"card",sel:new Set(),sort:null,dir:"asc",filter:"",d:null,origin:"",weFull:false,loading:false};
let HOLD=null,SHOWN=null;

/* a person or a conflict: the one-candidate shapes (get_candidate, save_candidate) */
function personRow(p,extra){return el("li",{},personCard(Object.assign({},p,{app_url:p.app_url}),{headingLevel:3,extra:extra||null}))}
function notSaved(c){
 const wrap=el("div",{});
 wrap.append(el("div",{class:"card"},el("h1",{},c.incoming_name||"Not saved",el("span",{class:"badge"},"Not saved")),
  c.warning?el("p",{},c.warning):null));
 if(c.existing)wrap.append(el("h2",{},"Already saved"),el("ul",{class:"plain"},personRow(c.existing)));
 if(c.possible_duplicates&&c.possible_duplicates.length){wrap.append(el("h2",{},"Saved with this name"));
  const ul=el("ul",{class:"plain"});
  for(const d of c.possible_duplicates){const diff=d.differences||[];
   ul.append(personRow(d,diff.length?el("ul",{class:"small"},diff.map(x=>el("li",{},x.field+": saved ",el("b",{},x.saved),", yours ",el("b",{},x.yours)))):el("div",{class:"small muted"},"Nothing you sent differs from what's saved.")))}
  wrap.append(ul)}
 return wrap}
function oneToCard(c){const origin=appOrigin(c.app_url);const out=Object.assign({},c);
 out.jobs=(c.pipelines||[]).map(p=>({job_id:p.job_id,job:p.job_title+(p.job_company?" ("+p.job_company+")":""),job_title:p.job_title,stage:p.stage,since:p.stage_changed_at,
  state:/^(rejected|declined|withdrawn|not a fit|out)/i.test(p.stage)?"lost":/^(hired|placed|started)/i.test(p.stage)?"hired":null,app_url:origin?origin+"/jobs/"+p.job_id:null}));
 out.activity=(c.timeline||[]).filter(t=>t.kind!=="change").slice(0,6).map(t=>({kind:t.kind,at:t.at,body:t.body,job_title:t.job_title,job_company:t.job_company,interviewer:t.interviewer,outcome_note:t.outcome_note,feedback:t.feedback}));
 return out}
function renderOne(c){
 const saved=savedText(c.saved_status);
 const wrap=el("div",{});
 wrap.append(header({title:saved?"Candidate: "+saved:"Candidate",appUrl:c.app_url,appLabel:"Open in Talent Search"}));
 wrap.append(personCard(oneToCard(c),{saved:saved,headingLevel:2}));
 return wrap}

/* lists */
function nextOffset(){return S.people.length}
function leadLines(d){
 const out=[];const exact=d.exact_total!=null?d.exact_total:d.total;
 if(d.corrected&&d.suggestion)out.push(el("p",{class:"small"},"Showing results for ",el("b",{},d.suggestion)));
 if(d.no_one_named)out.push(el("div",{class:"card small"},"No one named ",el("b",{},d.no_one_named)," is saved."+(d.results.length?" These people share some of the words, under other names.":"")));
 const head=d.closest?"No exact matches. Closest matches:":d.exact?"Exact match":exact+(exact===1?" match":" matches");
 out.push(el("p",{class:"small muted",style:"margin:0 0 6px"},head));
 return out}
function countLine(){const n=S.people.length,t=Math.max(S.total,n);return t>n?n+" of "+t+" people":(t===1?"1 person":t+" people")}
function active(){return S.people[S.idx]}
function mergedCard(p){const full=PCARDS[p.id]||{};const base=leanToCard(p,S.origin);
 return Object.assign({},base,full,{match:p.match,reason:p.reason,closest:p.closest,other_name:p.other_name,app_url:p.app_url||full.app_url,jobs:(full.jobs&&full.jobs.length?full.jobs:base.jobs)})}
function loadMore(btn){if(S.loading)return;S.loading=true;if(btn)btn.disabled=true;announce("Loading more people.");
 const tool=TOOL||(ARGS&&ARGS.name_or_id!=null?"run_saved_search":"search_candidates");
 callTool(tool,Object.assign({},ARGS||{},{limit:50,offset:nextOffset()})).then(r=>{S.loading=false;if(btn)btn.disabled=false;
  const d=r&&r.structuredContent;if(r&&r.isError||!d||!d.results){announce(said(r)||"Couldn't load more.");return}
  const have=new Set(S.people.map(p=>p.id));let add=0;for(const p of d.results)if(!have.has(p.id)){S.people.push(p);add++}
  S.total=d.total!=null?d.total:S.total;announce("Loaded "+add+" more. "+countLine()+".");draw(false)})}
function moreBtnSpec(){if(S.people.length>=S.total||!canCall())return null;
 const left=S.total-S.people.length;return{label:"Load next "+Math.min(50,left),onClick:loadMore}}

function sendSelection(){const chosen=S.people.filter(p=>S.sel.has(p.id));
 tellClaude({selected:chosen.map(p=>({id:p.id,name:p.name}))},chosen.length?"The recruiter selected "+chosen.length+" candidate"+(chosen.length===1?"":"s")+" in the view: "+peopleLabel(chosen)+".":"The recruiter has no candidates selected.")}

/* card mode */
function cardMode(d){
 const wrap=el("div",{});
 wrap.append(header({title:S.d.query?"Search: "+S.d.query:"Candidates",count:countLine(),appUrl:d.app_url,appLabel:"Open in Talent Search"}));
 for(const n of leadLines(d))wrap.append(n);
 if(!S.people.length){wrap.append(emptyState("No saved candidates match. Try fewer or different words."));return wrap}
 const many=S.people.length>1;
 const region=el("section",{class:"carousel",role:many?"region":null,"aria-roledescription":many?"carousel":null,"aria-label":many?"Candidates":null});
 const slideBox=el("div",{});
 const counter=el("span",{class:"count","aria-hidden":"true"});
 const prev=el("button",{class:"btn navbtn",type:"button","aria-label":"Previous candidate"},"‹");
 const next=el("button",{class:"btn navbtn",type:"button","aria-label":"Next candidate"},"›");
 const pick=el("input",{type:"checkbox"});const pickLab=el("label",{class:"pick"},pick,el("span",{},"Select"));
 const strip=el("ul",{class:"strip","aria-label":"Jump to a candidate"});
 const stripBtns=S.people.map((p,i)=>{const b=el("button",{type:"button","aria-label":p.name,title:p.name,onclick:()=>go(i,false)},avatar(p,true));strip.append(el("li",{},b));return b});
 function showSlide(){const p=active();const c=mergedCard(p);
  const card=personCard(c,{headingLevel:2});
  if(!PCARDS[p.id]&&canCall())card.append(el("span",{class:"skel","aria-hidden":"true"}));
  slideBox.replaceChildren(el("div",{role:many?"group":null,"aria-roledescription":many?"slide":null,"aria-label":many?(S.idx+1)+" of "+S.people.length+": "+p.name:null},card));
  counter.textContent=(S.idx+1)+" of "+S.people.length;
  prev.setAttribute("aria-disabled",S.idx===0?"true":"false");next.setAttribute("aria-disabled",S.idx===S.people.length-1?"true":"false");
  pick.checked=S.sel.has(p.id);pick.setAttribute("aria-label","Select "+p.name);
  stripBtns.forEach((b,i)=>b.setAttribute("aria-current",i===S.idx?"true":"false"));
  const cur=stripBtns[S.idx];if(cur&&cur.scrollIntoView&&document.activeElement===cur)cur.scrollIntoView({block:"nearest",inline:"nearest"});
  const ids=[];for(let k=S.idx-1;k<=S.idx+2;k++)if(S.people[k])ids.push(S.people[k].id);
  if(ids.some(i=>!PCARDS[i]))fetchCards(ids).then(()=>{if(active()&&active().id===p.id&&PCARDS[p.id]&&!slideBox.contains(document.activeElement)&&S.mode==="card")refill()})}
 function refill(){const p=active();const card=personCard(mergedCard(p),{headingLevel:2});const g=slideBox.firstChild;if(g)g.replaceChildren(card)}
 function go(i,announceIt){if(i<0||i>=S.people.length||i===S.idx)return;S.idx=i;showSlide();
  const p=active();announce("Candidate "+(i+1)+" of "+S.people.length+", "+p.name+(roleLine(p)?", "+roleLine(p):""))}
 prev.addEventListener("click",()=>go(S.idx-1));next.addEventListener("click",()=>go(S.idx+1));
 pick.addEventListener("change",()=>{const p=active();if(pick.checked)S.sel.add(p.id);else S.sel.delete(p.id);announce(S.sel.size+" selected");sendSelection()});
 onKeys(region,{ArrowLeft:e=>{e.preventDefault();go(S.idx-1)},ArrowRight:e=>{e.preventDefault();go(S.idx+1)},Home:e=>{e.preventDefault();go(0)},End:e=>{e.preventDefault();go(S.people.length-1)}});
 const tbtn=el("button",{class:"btn",type:"button",onclick:()=>setView("table")},"Show as table");
 const bar=el("div",{class:"cnav"});
 if(many)bar.append(prev,counter,next,pickLab,tbtn);
 const more=moreBtnSpec();if(more)bar.append(el("button",{class:"btn",type:"button",onclick:ev=>more.onClick(ev.currentTarget)},more.label));
 if(S.sel.size)bar.append(el("span",{class:"small muted",role:"status"},S.sel.size+" selected"));
 region.append(bar);if(many)region.append(strip);region.append(slideBox);showSlide();wrap.append(region);
 if(d.suggestion&&!d.corrected)wrap.append(el("p",{class:"small"},"Did you mean ",el("b",{},d.suggestion),"?"));
 return wrap}

/* table mode */
function tableMode(d){
 const wrap=el("div",{});
 wrap.append(header({title:S.d.query?"Search: "+S.d.query:"Candidates",count:countLine(),appUrl:d.app_url,appLabel:"Open in Talent Search"}));
 for(const n of leadLines(d))wrap.append(n);
 const back=el("button",{class:"btn",type:"button",onclick:()=>setView("card")},"Show as cards");
 wrap.append(el("div",{class:"tbar"},back));
 const t=peopleTable(S.people,S,{origin:S.origin,total:S.total,more:moreBtnSpec(),onSel:sendSelection,
  onOpen:p=>{S.idx=S.people.indexOf(p);setView("card")}});
 SHOWN=t;wrap.append(t.node);
 onKeys(wrap,{Escape:e=>{if(e.target&&e.target.tagName==="INPUT"&&e.target.type==="search"&&e.target.value)return;setView("card")}});
 return wrap}

function draw(focus){const m=document.getElementById("main");const d=S.d;
 const n=S.mode==="table"?tableMode(d):cardMode(d);m.replaceChildren(n);
 if(focus==="table"&&SHOWN)SHOWN.focusCaption();
 else if(focus==="card"){const h=m.querySelector("h2.name");if(h)h.focus()}}
function setView(mode){if(mode===S.mode)return;S.mode=mode;
 if(mode==="table"){S.weFull=false;if(canFullscreen()&&!isFull())setMode("fullscreen").then(ok=>{S.weFull=ok})}
 else if(S.weFull&&isFull()){setMode("inline");S.weFull=false}
 draw(mode);announce(mode==="table"?"Showing "+S.people.length+" candidates as a table.":"Showing cards. Candidate "+(S.idx+1)+" of "+S.people.length+".")}
window.onModeChange=()=>{};

function render(c){
 if(Array.isArray(c.results)){
  S.d=c;S.people=c.results.slice();S.total=c.total!=null?c.total:S.people.length;S.idx=0;S.mode="card";S.sort=null;S.dir="asc";S.filter="";S.origin=appOrigin(c.app_url);
  const ids=new Set(S.people.map(p=>p.id));for(const i of Array.from(S.sel))if(!ids.has(i))S.sel.delete(i);
  const m=cardMode(c);fetchCards(S.people.slice(0,3).map(p=>p.id));return m}
 if(!c.name&&!c.candidate&&(c.conflict||c.needs_confirmation))return notSaved(c);
 if(!c.name&&c.candidate)c=Object.assign({},c.candidate,{saved_status:c.saved_status,app_url:c.app_url||c.candidate.app_url});
 if(!c.name)return emptyState("Nothing to show.");
 return renderOne(c)}
