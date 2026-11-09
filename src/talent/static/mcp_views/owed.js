/* Feedback owed view (list_feedback_owed): who owes feedback, grouped by interviewer. Copy nudge only copies text;
   Log feedback and Mark chased send the person's own request to the chat. */
function nudgeText(p){const first=firstName(p.name);const items=p.items||[];
 const one=i=>i.candidate+(i.job?" for "+i.job:"")+(i.day?" from "+i.day:"");
 if(items.length===1)return"Hi "+first+", could you add your feedback on "+one(items[0])+"?";
 return"Hi "+first+", could you add your feedback on these interviews?\n"+items.map(i=>"- "+one(i)).join("\n")}
function lateText(i){const n=i.days_overdue||0;
 if(n>0)return{text:n+(n===1?" day overdue":" days overdue"),over:true};
 return{text:i.chased===false?"Session was recently, not due yet":"Due now",over:false}}
function itemRow(p,i){
 const li=el("li",{});const late=lateText(i);const what=i.session&&i.session!=="Interview"?i.session:"interview";
 li.append(el("div",{},personLink(i.candidate,i.candidate_url,i.candidate_id),i.job?[" for ",jobLink({job:i.job,app_url:i.job_url},i.candidate)]:null));
 li.append(el("div",{class:"small muted"},what+", "+(i.day||"")+" · ",el("span",{class:"late"+(late.over?" over":"")},late.text)));
 const acts=el("div",{class:"acts"});
 const log=el("button",{class:"btn",type:"button",needs:"message","aria-label":"Log feedback from "+p.name+" on "+i.candidate},"Log feedback");
 log.addEventListener("click",()=>askClaude("Please log feedback from "+p.name+" on "+i.candidate+(i.job?" ("+i.job+")":"")+", interview id "+i.interview_id+" ("+what+", "+i.day+"). Ask me what they said and their verdict, then use add_interview_feedback.",log));
 const mc=el("button",{class:"btn",type:"button",needs:"message","aria-label":"Mark "+p.name+" as chased for "+i.candidate},"Mark chased");
 mc.addEventListener("click",()=>askClaude("Please note that I chased "+p.name+" for feedback on "+i.candidate+(i.job?" ("+i.job+")":"")+", interview id "+i.interview_id+" ("+what+", "+i.day+"). Add it as a note on "+i.candidate+" (candidate id "+i.candidate_id+").",mc));
 acts.append(log,mc);li.append(acts);return li}
function personBlock(p,idx,d){
 const sec=el("section",{class:"who","aria-labelledby":"w"+idx});const items=p.items||[];
 sec.append(el("h2",{class:"wh",id:"w"+idx},p.name,el("span",{class:"n"},"owes "+items.length+(items.length===1?" piece":" pieces")+" of feedback")));
 if(p.email)sec.append(el("div",{class:"small muted"},p.email));
 const text=nudgeText(p);const box=el("div",{class:"nudge"},text);
 const st=el("span",{class:"small",role:"status"});
 const copy=el("button",{class:"btn primary",type:"button","aria-label":"Copy a reminder to send to "+p.name},"Copy nudge");
 copy.addEventListener("click",()=>copyText(text,box,st,"Copied. Paste it into your message to "+firstName(p.name)+"."));
 sec.append(box,el("div",{class:"acts"},copy,st));
 sec.append(el("ul",{class:"owes","aria-label":"What "+p.name+" owes"},items.map(i=>itemRow(p,i))));
 return sec}
function render(d){
 const wrap=el("div",{});const ps=(d.people||[]).filter(p=>(p.items||[]).length);
 const n=ps.reduce((a,p)=>a+p.items.length,0);
 wrap.append(header({title:"Feedback owed",count:n?ps.length+(ps.length===1?" interviewer owes ":" interviewers owe ")+n+(n===1?" piece":" pieces")+" of feedback":null,appUrl:d.app_url,appLabel:"Open in Talent Search",fullscreen:ps.length>0}));
 if(!ps.length){wrap.append(emptyState("Nobody owes feedback right now."));return wrap}
 const list=el("div",{class:"owedwrap"});ps.forEach((p,i)=>list.append(personBlock(p,i,d)));wrap.append(list);
 return wrap}
