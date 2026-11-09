/* Follow-ups view: who to nudge, grouped by how soon. Done goes through Claude. */
function todayStr(){const d=new Date();return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0")}
function dayDiff(s){const a=new Date(s+"T00:00:00"),b=new Date(todayStr()+"T00:00:00");return Math.round((a-b)/864e5)}
const FGROUPS=[["Overdue",n=>n<0],["Today",n=>n===0],["This week",n=>n>0&&n<=7],["Later",n=>n>7]];
function fuItem(f){
 const li=el("li",{class:"fu"});
 const nm=f.name||"this person";
 const jobName=f.job||"the job";
 li.append(el("div",{class:"row",style:"justify-content:flex-start;gap:6px 10px"},
  personLink(nm,f.candidate_url,f.candidate_id),
  el("span",{class:"muted"},"for"),f.app_url||f.job_id?jobLink({job:jobName,app_url:f.app_url},nm):el("span",{},jobName)));
 const n=dayDiff(f.due_on);
 li.append(el("div",{class:"small",style:"margin-top:2px"},el("b",{},f.label||"Follow-up"),f.steps?" (step "+f.step+" of "+f.steps+")":"",", ",
  el("span",{class:n<0?"status-chip late":"status-chip"},f.due||"due"),f.due_on?el("span",{class:"muted"}," · "+day(f.due_on+"T12:00:00")):null));
 if(f.draft){const pre=el("pre",{},f.draft);const st=el("span",{class:"small muted",role:"status"});
  li.append(el("details",{},el("summary",{},"Text to send"),pre,
   el("div",{class:"acts"},el("button",{class:"btn",type:"button",onclick:()=>copyText(f.draft,pre).then(ok=>{st.textContent=ok?"Copied.":"Select the text above and copy it.";announce(st.textContent)})},"Copy text"),st)))}
 const acts=el("div",{class:"acts"});
 const done=el("button",{class:"btn",type:"button","aria-label":"Ask Claude to mark the follow-up for "+nm+" done",onclick:()=>askClaude("Mark the follow-up \""+(f.label||"Follow-up")+"\" for "+nm+" (candidate id "+f.candidate_id+") on "+jobName+" done. I sent it.",done)},"Done");
 acts.append(done);
 if(f.app_url)acts.append(el("button",{class:"btn",type:"button","aria-label":"Open the outreach for "+jobName+" in Talent Search",onclick:()=>openLink(f.app_url,"Talent Search")},"Open in Talent Search"));
 li.append(acts);return li}
function render(d){
 const fs=d.followups||[];const wrap=el("div",{});
 wrap.append(header({title:fs.length?fs.length+(fs.length===1?" follow-up":" follow-ups")+" to send":"No follow-ups due",appUrl:d.app_url,fullscreen:false,refresh:false}));
 if(d.has_sequence===false){wrap.append(emptyState("This job has no follow-up steps yet. Set them on the job page in Talent Search."));return wrap}
 if(!fs.length){wrap.append(emptyState("Nothing is due. Nice and quiet."));return wrap}
 wrap.append(el("p",{class:"small muted",style:"margin:0 0 6px"},"Talent Search sends nothing. Copy the text, send it yourself, then press Done."));
 const used=new Set();
 for(const[title,test]of FGROUPS){const rows=fs.filter(f=>f.due_on&&test(dayDiff(f.due_on)));rows.forEach(f=>used.add(f));if(!rows.length)continue;
  wrap.append(el("section",{"aria-label":title},el("h2",{},title+" ("+rows.length+")"),el("div",{class:"lrow"},el("ul",{class:"plain"},rows.map(fuItem)))))}
 const rest=fs.filter(f=>!used.has(f));
 if(rest.length)wrap.append(el("section",{"aria-label":"No date"},el("h2",{},"No date ("+rest.length+")"),el("div",{class:"lrow"},el("ul",{class:"plain"},rest.map(fuItem)))));
 return wrap}
