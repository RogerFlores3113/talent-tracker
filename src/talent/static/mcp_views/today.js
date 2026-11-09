const LABEL={interview:"Interview",interview_out:"Interview still on",interview_outcome:"How did it go",interview_result:"Interview result",suggestion:"To-do",stuck:"Stuck",thin:"Needs more candidates",stale:"May be out of date",ai_change:"AI change",shortlist:"Shortlist",followup:"Follow-up due",replied:"Replied",update_due:"Update due",client_chase:"Client feedback",feedback_owed:"Feedback owed",offer:"Offer",openings:"Openings"};
const GROUPS=[
 ["Interviews",["interview","interview_out","interview_outcome","interview_result"]],
 ["Feedback and follow-ups",["feedback_owed","followup","replied","client_chase","update_due","suggestion"]],
 ["People and jobs to look at",["stuck","thin","stale","offer","openings"]],
 ["To review",["ai_change","shortlist"]]];
const SNOOZE=["suggestion","stuck","thin","stale","followup","client_chase","update_due","feedback_owed","openings"];
function plural(n,w){return n+" "+w+(n===1?"":"s")}
function who(i){return i.name||"this person"}
function onJob(i){return i.job?" for "+i.job:""}
function ids(i){return " ("+[i.candidate_id?"candidate id "+i.candidate_id:null,i.job_id?"job id "+i.job_id:null,i.suggestion_id?"to-do id "+i.suggestion_id:null,i.interview_id?"interview id "+i.interview_id:null].filter(Boolean).join(", ")+")"}
/* what each kind offers: [button label, plain request] */
function actionsFor(i){const w=who(i),out=[];
 switch(i.kind){
  case"interview_outcome":out.push(["Record how it went","Record how "+w+"'s interview"+onJob(i)+" went. Ask me what happened, then save it."+ids(i)]);break;
  case"interview_out":out.push(["Keep it","Keep "+w+"'s interview"+onJob(i)+" on the calendar, they are still in the running."+ids(i)],["Cancel it","Cancel "+w+"'s interview"+onJob(i)+", they are out."+ids(i)]);break;
  case"interview_result":case"replied":if(i.next_stage)out.push(["Move to "+i.next_stage,"Move "+w+" to "+i.next_stage+onJob(i)+ids(i)]);break;
  case"feedback_owed":out.push(["Draft the requests","Draft the feedback requests for "+w+(i.job?" ("+i.job+")":"")+". I will send them."+ids(i)]);break;
  case"followup":out.push(["Show the follow-up","Show me the follow-up to paste to "+w+onJob(i)+ids(i)],["Done","I sent the follow-up to "+w+onJob(i)+". Log it."+ids(i)]);break;
  case"suggestion":out.push(["Done","Mark my to-do as done: "+(i.title||"")+ids(i)]);break;
  case"stuck":out.push(["What next?","What should I do about "+w+" being stuck"+(i.stage?" in "+i.stage:"")+onJob(i)+"?"+ids(i)]);break}
 if(SNOOZE.includes(i.kind))out.push(["Snooze","Snooze this on my Today list for a week: "+(i.title||LABEL[i.kind])+ids(i)]);
 return out}
function item(i,origin){
 const li=el("li",{class:"card"});
 const status=el("div",{class:"status small muted",role:"status"});
 const cUrl=i.candidate_id&&origin?origin+"/candidates/"+i.candidate_id:null;
 const jUrl=i.job_id&&origin?origin+"/jobs/"+i.job_id:null;
 const clock=(i.kind==="interview"||i.kind==="interview_out"||i.kind==="interview_outcome")&&i.when;
 let title;
 if(i.candidate_id&&i.name){title=el("button",{class:"link tl",type:"button","aria-label":"Open "+i.name+" summary: "+i.title},i.title);title.addEventListener("click",()=>openPerson(i.candidate_id,cUrl,title))}
 else if(jUrl){title=el("button",{class:"link tl",type:"button","aria-label":"Open the pipeline: "+i.title,onclick:()=>openLink(jUrl,"the pipeline")},i.title)}
 else title=el("span",{},i.title);
 li.append(el("div",{class:"kind"},LABEL[i.kind]||i.kind),el("div",{class:"ttl"},title));
 const meta=[];
 if(clock)meta.push(el("time",{datetime:i.when},when(i.when)));
 if(i.interviewer)meta.push("with "+i.interviewer);
 if(meta.length)li.append(el("div",{class:"small"},meta.flatMap((m,k)=>k?[" · ",m]:[m])));
 if(i.due_text)li.append(el("div",{class:"small"+(i.overdue?"":" muted")},i.due_text));
 if(i.detail)li.append(el("div",{class:"small muted"},i.detail));
 const acts=el("div",{class:"acts"});
 if(i.job&&jUrl&&i.candidate_id)acts.append(jobLink({job:i.job,job_id:i.job_id,origin},i.name));
 for(const[label,text]of actionsFor(i)){const b=el("button",{class:"btn",type:"button","aria-label":label+": "+(i.name?i.name+", ":"")+(i.title||LABEL[i.kind])},label);
  b.addEventListener("click",()=>askOrLink(text,b,status,cUrl||jUrl||(origin?origin+"/":null),i.name||null));acts.append(b)}
 if(acts.childNodes.length)li.append(acts);
 li.append(status);return li}
function section(label,items,origin,level){
 if(!items.length)return null;
 const box=el("section",{"aria-label":label+", "+items.length});
 box.append(el(level||"h2",{},label+" ("+items.length+")"),el("ul",{class:"plain"},items.map(i=>item(i,origin))));return box}
function groups(items,origin,level){const used=new Set();const out=[];
 for(const[label,kinds]of GROUPS){const xs=items.filter(i=>kinds.includes(i.kind));xs.forEach(i=>used.add(i));out.push(section(label,xs,origin,level))}
 out.push(section("Other",items.filter(i=>!used.has(i)),origin,level));return out}
function render(d){
 const wrap=el("div",{});const origin=appOrigin(d.app_url);
 const n=d.items.length;
 wrap.append(header({title:n?plural(n,"thing")+" to do":"Nothing needs you today",appUrl:d.app_url,fullscreen:false}));
 for(const s of groups(d.items,origin))if(s)wrap.append(s);
 if(d.next_week&&d.next_week.length){wrap.append(el("h2",{style:"margin-top:16px"},"Next week ("+d.next_week.length+")"));for(const s of groups(d.next_week,origin,"h3"))if(s)wrap.append(s)}
 if(d.stale_more)wrap.append(el("p",{class:"small muted"},d.stale_more+" more stale profiles are not shown."));
 return wrap}
