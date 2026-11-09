/* Jobs view: one card per job from list_jobs. Sort by recent activity, title or number in play. */
const JS={sort:"recent"};
function stageKind(n){n=String(n).toLowerCase();return /^(hired|placed|started|accepted)/.test(n)?"hired":/^(reject|declin|withdr|not a fit|out|client passed|passed)/.test(n)?"out":"open"}
function jobLine(j){return j.title+(j.company?" ("+j.company+")":"")}
function statusWord(s){return String(s||"open").replace("_"," ")}
function stageBar(j){const st=Object.entries(j.stage_counts||{}).filter(([,n])=>n>0);
 if(!st.length)return el("div",{class:"small muted",style:"margin-top:8px"},"No one in the pipeline yet.");
 const total=st.reduce((a,[,n])=>a+n,0);
 const bar=el("div",{class:"stagebar","aria-hidden":"true"},st.map(([k,n])=>el("span",{class:stageKind(k)==="open"?null:stageKind(k),style:"flex:"+n+" 1 0",title:k+": "+n})));
 const chips=el("ul",{class:"stagechips","aria-label":"People in each stage"},st.map(([k,n])=>el("li",{},k,el("b",{},n))));
 return el("div",{},bar,chips)}
function jobCard(j){
 const url=j.app_url;
 const card=el("li",{class:"lrow"});
 card.append(el("h2",{class:"lt"},url?jobLink({job:j.title,app_url:url}):j.title,
  el("span",{class:"status-chip "+(j.status||"open")},statusWord(j.status))));
 card.append(el("div",{class:"sub"},[j.company,j.location,j.hiring_manager&&("Hiring manager "+j.hiring_manager)].filter(Boolean).join(" · ")||"No client named"));
 const facts=el("dl",{class:"facts",style:"display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:8px 0 0;font-size:.8125rem"});
 const fact=(k,v)=>{if(v==null||v==="")return;facts.append(el("dt",{class:"muted"},k),el("dd",{style:"margin:0"},v))};
 const seats=j.openings||1,hired=j.hired_count||0;
 fact("In the pipeline",String(j.active_count||0)+(j.active_count===1?" person":" people"));
 fact("Hired",seats>1?hired+" of "+seats:String(hired));
 fact("Last activity",j.updated_at?dateEl(j.updated_at):null);
 fact("Next interview",j.next_interview?el("time",{datetime:j.next_interview},when(j.next_interview)):"None booked");
 card.append(facts,stageBar(j));
 if(j.pending_review&&j.pending_review.length)card.append(el("div",{class:"small",style:"margin-top:6px"},"Waiting for your review: "+j.pending_review.join("; ")));
 const acts=el("div",{class:"acts"});
 const ask=el("button",{class:"btn",type:"button","aria-label":"Ask Claude to show the pipeline for "+jobLine(j),onclick:()=>askClaude("Show the pipeline for "+jobLine(j)+" (job id "+j.id+")",ask)},"Show pipeline");
 acts.append(ask);
 if(url)acts.append(el("button",{class:"btn",type:"button","aria-label":"Open "+jobLine(j)+" in Talent Search",onclick:()=>openLink(url,"the job")},"Open job"));
 card.append(acts);return card}
function sortJobs(js){const a=js.slice();
 if(JS.sort==="title")a.sort((x,y)=>String(x.title).localeCompare(String(y.title)));
 else if(JS.sort==="open")a.sort((x,y)=>(y.active_count||0)-(x.active_count||0)||String(x.title).localeCompare(String(y.title)));
 else a.sort((x,y)=>String(y.updated_at||"").localeCompare(String(x.updated_at||"")));
 return a}
function render(d){
 const jobs=d.jobs||[];const wrap=el("div",{});
 const open=jobs.filter(j=>j.status==="open").length;
 wrap.append(header({title:"Jobs",count:jobs.length?jobs.length+(jobs.length===1?" job":" jobs")+(open!==jobs.length?", "+open+" open":""):null,appUrl:d.app_url,appLabel:"Open in Talent Search",fullscreen:false,refresh:false}));
 if(!jobs.length){wrap.append(emptyState("No jobs here yet. Ask Claude to create one, or add it in Talent Search."));return wrap}
 const list=el("ul",{class:"plain","aria-label":"Jobs"});
 const draw=()=>{list.replaceChildren(...sortJobs(jobs).map(jobCard))};
 const names={recent:"Recent activity",title:"Title",open:"Most people in process"};
 const btns=Object.entries(names).map(([k,l])=>{const b=el("button",{class:"btn",type:"button","aria-pressed":JS.sort===k?"true":"false"},l);
  b.addEventListener("click",()=>{JS.sort=k;btns.forEach(x=>x.setAttribute("aria-pressed",x===b?"true":"false"));draw();announce("Sorted by "+l.toLowerCase()+".")});return b});
 wrap.append(el("div",{class:"vtools",role:"group","aria-label":"Sort jobs"},el("span",{class:"muted"},"Sort by"),el("span",{class:"seg"},btns)));
 draw();wrap.append(list);return wrap}
