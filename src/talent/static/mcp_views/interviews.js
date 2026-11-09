/* Interviews view (list_interviews): a week agenda by day. Nothing is written here: Record feedback, Reschedule and
   Cancel send the person's own request to the chat (askClaude), which does it through the normal tools. */
const VERDICTS=[["strong yes","Strong yes"],["yes","Yes"],["lean yes","Lean yes"],["mixed","Mixed"],["lean no","Lean no"],["no","No"],["strong no","Strong no"]];
function goodTz(z){if(!z)return undefined;try{new Intl.DateTimeFormat(undefined,{timeZone:z});return z}catch(e){return undefined}}
function clock(iso,tz,zone){const d=parseD(iso);if(!d)return"";try{return d.toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit",timeZone:tz,timeZoneName:zone?"short":undefined})}catch(e){return d.toLocaleTimeString()}}
function dayKey(iso,tz){const d=parseD(iso);if(!d)return"";try{return new Intl.DateTimeFormat("en-CA",{timeZone:tz,year:"numeric",month:"2-digit",day:"2-digit"}).format(d)}catch(e){return iso.slice(0,10)}}
function dayName(iso,tz){const d=parseD(iso);try{return d.toLocaleDateString(undefined,{weekday:"long",month:"short",day:"numeric",timeZone:tz})}catch(e){return day(iso)}}
function timeText(s,tz){if(s.all_day)return"All day";return clock(s.at,tz)+(s.end?" to "+clock(s.end,tz,true):"")}
function stateOf(s,now){
 if(s.status==="cancelled")return{key:"cancelled",text:"Cancelled"};
 if((s.feedback_owed_by&&s.feedback_owed_by.length)||s.needs_outcome)return{key:"owed",text:s.feedback_overdue?"Feedback owed, overdue":"Feedback owed"};
 if(s.status&&s.status!=="scheduled")return{key:"done",text:"Done"+(s.result?": "+s.result:"")};
 return{key:"scheduled",text:"Scheduled"}}
function flat(d){const out=[];for(const lp of d.loops||[])for(const s of lp.sessions||[])out.push(Object.assign({},s,{cand:lp.name,cand_id:lp.candidate_id,cand_url:lp.candidate_url,job:lp.job,job_id:lp.job_id,job_url:lp.job_url}));
 return out.sort((a,b)=>String(a.at).localeCompare(String(b.at)))}
function sessionName(s){return s.name&&s.name!=="Interview"?s.name:"interview"}
function sessionWords(s,tz){return "interview id "+s.interview_id+" ("+sessionName(s)+") for "+s.cand+(s.job?" ("+s.job+")":"")+(s.all_day?"":" on "+dayName(s.at,tz)+" at "+clock(s.at,tz,true))}

function feedbackForm(s,tz,btn,box){
 let verdict=null;const names=s.interviewers||[];
 const form=el("form",{class:"fbform","aria-label":"Record feedback on "+s.cand+"'s "+sessionName(s)});
 const vbox=el("div",{class:"opts",role:"group","aria-label":"Verdict, optional"});
 const vbtns=VERDICTS.map(([v,l])=>{const b=el("button",{class:"btn",type:"button","aria-pressed":"false"},l);
  b.addEventListener("click",()=>{verdict=verdict===v?null:v;vbtns.forEach((x,i)=>x.setAttribute("aria-pressed",VERDICTS[i][0]===verdict?"true":"false"))});return b});
 vbox.append(...vbtns);
 const id="fb"+s.interview_id;
 let whoNode,whoVal;
 if(names.length>1){const sel=el("select",{id:id+"w"},names.map(n=>el("option",{value:n},n)));whoNode=el("div",{},el("label",{for:id+"w"},"Who is this feedback from?"),sel);whoVal=()=>sel.value}
 else if(names.length===1){whoNode=el("div",{class:"small muted"},"From "+names[0]);whoVal=()=>names[0]}
 else{const inp=el("input",{type:"text",id:id+"w",maxlength:"200"});whoNode=el("div",{},el("label",{for:id+"w"},"Who is this feedback from? (optional)"),inp);whoVal=()=>inp.value.trim()}
 const note=el("textarea",{id:id+"n",maxlength:"4000",required:"true"});
 const st=el("div",{class:"status small",role:"status"});
 const send=el("button",{class:"btn primary",type:"submit"},"Send to Claude");
 const cancel=el("button",{class:"btn",type:"button",onclick:()=>{form.remove();btn.setAttribute("aria-expanded","false");btn.focus()}},"Cancel");
 form.append(whoNode,el("div",{},el("div",{class:"small",id:id+"v"},"Verdict (optional)"),vbox),el("div",{},el("label",{for:id+"n"},"What did they say?"),note),el("div",{class:"acts",style:"margin:0"},send,cancel),st);
 form.addEventListener("submit",ev=>{ev.preventDefault();const text=note.value.trim();
  if(!text){setStatus(st,"Write what they said first.");note.focus();return}
  const w=whoVal();
  const msg="Please save this interview feedback: "+sessionWords(s,tz)+". Interviewer: "+(w||"not named")+". Verdict: "+(verdict||"none given")+". What they said: \""+text.replace(/"/g,"'")+"\". Use add_interview_feedback with that interviewer, verdict and text.";
  askClaude(msg,send).then(ok=>{if(ok){setStatus(st,"Sent to Claude. It will save the feedback and tell you.");send.disabled=true}else setStatus(st,"Your chat app wouldn't send that. You can type it in the chat, or open the interview in Talent Search.")})});
 form.addEventListener("keydown",e=>{if(e.key==="Escape"){e.stopPropagation();cancel.click()}});
 return form}

function sessionItem(s,tz,clashes){
 const li=el("li",{class:"session"+(clashes.length?" clash":"")});
 const stt=stateOf(s);const nm=s.cand;
 li.append(el("div",{class:"toprow"},el("span",{class:"time"},timeText(s,tz),zoneNote(s,tz)),el("span",{class:"state "+stt.key},stt.text)));
 li.append(el("div",{class:"who"},personLink(nm,s.cand_url,s.cand_id)));
 const what=el("div",{class:"what"},sessionName(s)==="interview"?"Interview":s.name);
 if(s.job){what.append(" for ",jobLink({job:s.job,app_url:s.job_url},nm))}
 li.append(what);
 if(s.interviewers&&s.interviewers.length)li.append(el("div",{class:"with"},"With "+s.interviewers.join(", ")));
 else li.append(el("div",{class:"with"},"No interviewer named"));
 if(s.feedback_owed_by&&s.feedback_owed_by.length)li.append(el("div",{class:"small"},"Feedback still owed by "+s.feedback_owed_by.join(", ")));
 for(const c of clashes)li.append(el("div",{class:"warn",role:"note"},el("b",{},"Clash: "),c));
 const acts=el("div",{class:"acts"});
 const started=parseD(s.at)&&parseD(s.at)<=new Date();
 const box=el("div",{});
 if(s.status!=="cancelled"&&started&&!s.all_day){const fb=el("button",{class:"btn primary",type:"button","aria-expanded":"false","aria-label":"Record feedback on "+nm+"'s "+sessionName(s)},"Record feedback");
  fb.addEventListener("click",()=>{if(box.firstChild){box.replaceChildren();fb.setAttribute("aria-expanded","false");return}
   const f=feedbackForm(s,tz,fb,box);box.append(f);fb.setAttribute("aria-expanded","true");f.querySelector("button").focus()});
  acts.append(fb)}
 if(s.status!=="cancelled"){
  const rs=el("button",{class:"btn",type:"button","aria-label":"Reschedule "+nm+"'s "+sessionName(s)},"Reschedule");
  rs.addEventListener("click",()=>askClaude("Please reschedule "+sessionWords(s,tz)+". Ask me what new time I want, then use update_interview.",rs));
  const cn=el("button",{class:"btn",type:"button","aria-label":"Cancel "+nm+"'s "+sessionName(s)},"Cancel");
  cn.addEventListener("click",()=>{if(box.querySelector(".sure"))return;
   const yes=el("button",{class:"btn primary",type:"button"},"Yes, cancel it");const no=el("button",{class:"btn",type:"button"},"Keep it");
   const row=el("div",{class:"sure",role:"group","aria-label":"Confirm cancelling"},"Cancel this interview? It stays on the timeline and can be put back.",yes,no);
   yes.addEventListener("click",()=>{askClaude("Please cancel "+sessionWords(s,tz)+". Use update_interview to cancel it (it can be put back later).",yes).then(ok=>{if(ok){row.replaceChildren(el("span",{role:"status"},"Sent to Claude."))}})});
   no.addEventListener("click",()=>{row.remove();cn.focus()});
   box.append(row);yes.focus()});
  acts.append(rs,cn)}
 if(s.app_url)acts.append(el("button",{class:"btn",type:"button","aria-label":"Open "+nm+"'s "+sessionName(s)+" in Talent Search",onclick:()=>openLink(s.app_url,"the interview")},"Open in Talent Search"));
 li.append(acts,box);return li}

function zoneNote(s,tz){if(!s.candidate_tz||s.all_day)return null;const z=goodTz(s.candidate_tz);if(!z)return null;
 const mine=clock(s.at,tz,true),theirs=clock(s.at,z,true);if(mine===theirs)return null;
 return el("span",{class:"zone"},theirs+" for "+firstName(s.cand)+" ("+s.candidate_tz.replace(/_/g," ")+")")}

function render(d){
 const wrap=el("div",{});const tz=goodTz(d.tz);
 const ss=flat(d);
 const start=d.start?new Date(d.start+"T12:00:00"):null;
 wrap.append(header({title:"Interviews"+(start?", week of "+start.toLocaleDateString(undefined,{month:"short",day:"numeric"}):""),count:(d.total||ss.length)+((d.total||ss.length)===1?" session":" sessions"),appUrl:d.app_url,appLabel:"Open in Talent Search"}));
 if(!ss.length){wrap.append(emptyState("No interviews booked this week."));return wrap}
 const clashBy={};for(const c of d.clashes||[])for(const id of c.interview_ids||[])(clashBy[id]=clashBy[id]||[]).push(c.text);
 if((d.clashes||[]).length)wrap.append(el("div",{class:"card",role:"note"},el("b",{},(d.clashes.length)+(d.clashes.length===1?" clash":" clashes")+" this week. "),"Sessions with a clash are marked below."));
 const nowIso=new Date().toISOString();const today=dayKey(nowIso,tz),tomorrow=dayKey(new Date(Date.now()+864e5).toISOString(),tz);
 const groups=[];for(const s of ss){const k=dayKey(s.at,tz);let g=groups[groups.length-1];if(!g||g.k!==k){g={k,at:s.at,items:[]};groups.push(g)}g.items.push(s)}
 for(const g of groups){const label=g.k===today?"Today":g.k===tomorrow?"Tomorrow":dayName(g.at,tz);
  const sec=el("section",{class:"day","aria-labelledby":"d"+g.k});
  sec.append(el("h2",{class:"dayh",id:"d"+g.k},label,(g.k===today||g.k===tomorrow)?el("span",{class:"when"},dayName(g.at,tz)):null));
  sec.append(el("ul",{class:"sessions","aria-label":label+", "+g.items.length+(g.items.length===1?" session":" sessions")},g.items.map(s=>sessionItem(s,tz,clashBy[s.interview_id]||[]))));
  wrap.append(sec)}
 if((d.load||[]).length){wrap.append(el("h2",{},"Interviewer load this week"),el("ul",{class:"load"},d.load.map(l=>el("li",{},l.interviewer+": "+l.sessions+(l.sessions===1?" session":" sessions")))))}
 return wrap}
