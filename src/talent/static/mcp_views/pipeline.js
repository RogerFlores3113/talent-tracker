function plural(n,w){return n+" "+w+(n===1?"":"s")}
function daysText(n){return n==null?null:n===0?"today":plural(n,"day")}
function jobName(j){return(j.title||"the job")+(j.company?" ("+j.company+")":"")}
function chip(text,cls,title){return el("li",{class:"chip"+(cls?" "+cls:""),title:title||null},text)}
function pcard(c,s,d,stages){
 const nm=c.name,job=d.job||{};
 const status=el("div",{class:"status small muted",role:"status"});
 const chips=el("ul",{class:"chips","aria-label":"About "+nm},
  chip(s.name+(c.days_in_stage!=null?" · "+daysText(c.days_in_stage):"")),
  c.verdicts?chip(c.verdicts,"verdict","How the interviews went: "+c.verdicts):null,
  c.feedback_owed?chip("Feedback owed ("+c.feedback_owed+")","owed","Still owed by "+(c.owed_by||[]).join(", ")):null,
  c.next_interview?chip("Interview "+when(c.next_interview),"next"):null);
 const card=el("li",{class:"pc"},el("div",{class:"pname"},personLink(nm,c.app_url,c.candidate_id)),
  c.current_title||c.current_company?el("div",{class:"muted small"},role(c.current_title,c.current_company)):null,
  chips);
 if(s.outcome==="open"||s.outcome==="lost"){
  const opts=stages.filter(x=>x.name!==s.name);
  const sel=el("select",{class:"field moveto","aria-label":"Move "+nm+" to another stage"},el("option",{value:""},"Move to..."),opts.map(x=>el("option",{value:x.name},x.name)));
  const acts=el("div",{class:"acts"});
  const go=el("button",{class:"btn",type:"button",needs:"message","aria-label":"Move "+nm,disabled:true},"Move");
  sel.addEventListener("change",()=>{go.disabled=!sel.value});  // picking only sets the value; Move asks first
  go.addEventListener("click",()=>{const to=sel.value;if(!to)return;
   confirmSend(acts,"Move "+firstName(nm)+" to "+to+"? Claude makes the move when you send this.","Move "+nm+" to "+to+" for "+jobName(job)+" (candidate id "+c.candidate_id+", job id "+job.id+").",status,c.app_url,nm,()=>sel.focus())});
  acts.append(sel,go);card.append(acts)}
 card.append(status);
 return card}
function render(d){
 const wrap=el("div",{});const job=d.job||{};
 const n=d.counts||{active:d.stages.filter(s=>s.outcome==="open").reduce((a,s)=>a+s.candidates.length,0)};
 const sum=[n.active+" in the running"];if(n.hired!=null)sum.push(d.openings>1?n.hired+" of "+d.openings+" hired":n.hired+" hired");if(n.out)sum.push(n.out+" out");
 const line=d.headline||(sum.join(" · "));
 wrap.append(header({title:job.title+(job.company?" · "+job.company:""),count:line+(job.status&&job.status!=="open"?" · "+job.status.replace("_"," "):""),appUrl:d.app_url,fullscreen:d.stages.some(s=>(s.total!=null?s.total:s.candidates.length)>0)}));
 const fs=wrap.querySelector("button[data-fs]");if(fs)fs.classList.add("primary");
 const per=d.stages.filter(s=>(s.total!=null?s.total:s.candidates.length)>0);
 const left=new Set(d.left_out||[]);
 if(per.length){
  wrap.append(el("ul",{class:"chips stagecounts","aria-label":"People in each stage"},per.map(s=>{const t=s.total!=null?s.total:s.candidates.length;
   return el("li",{class:"chip",title:left.has(s.name)?"Not in the status update text":null},s.name,el("span",{class:"sr"},","),"\u00a0"+t+(left.has(s.name)?" *":""))})));
  if(per.some(s=>left.has(s.name)))wrap.append(el("p",{class:"small legend",style:"margin:2px 0 6px"},el("b",{},"* "),"A star means the stage is left out of the status update text you paste to a manager: "+per.filter(s=>left.has(s.name)).map(s=>s.name).join(", ")+". The people are still here."))}
 else wrap.append(emptyState("Nobody is in this job's pipeline yet."));
 const board=el("div",{class:"board wrap"});
 for(const s of d.stages){const t=s.total!=null?s.total:s.candidates.length;
  board.append(el("section",{class:"col"+(s.outcome!=="open"?" closed":""),"aria-label":s.name+", "+t+(t===1?" person":" people")},
   el("h3",{},el("span",{},s.name+(s.outcome==="hired"?" (hired)":s.outcome==="lost"?" (out)":"")),el("span",{class:"muted"},t)),
   s.candidates.length?el("ul",{class:"plain"},s.candidates.map(c=>pcard(c,s,d,d.stages))):el("div",{class:"muted small"},"Empty"),
   t>s.candidates.length?el("div",{class:"muted small"},"Showing "+s.candidates.length+" of "+t+". Ask for more in the chat."):null))}
 wrap.append(board);return wrap}
