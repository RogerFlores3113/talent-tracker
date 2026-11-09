const WEIGHT=[["strong yes",3],["lean yes",1],["yes",2],["mixed",0],["lean no",-1],["strong no",-3],["no",-2]];
function plural(n,w){return n+" "+w+(n===1?"":"s")}
function mood(tally){let sum=0,n=0;const re=/(strong yes|lean yes|strong no|lean no|yes|mixed|no)\s+(\d+)/gi;let m;
 while((m=re.exec(String(tally||"")))){const w=(WEIGHT.find(x=>x[0]===m[1].toLowerCase())||[0,0])[1],c=+m[2];sum+=w*c;n+=c}return n?sum/n:null}
function sessions(p){const ivs=p.interviews||[];const done=ivs.filter(i=>!i.upcoming).length;
 const left=p.still_to_come!=null?p.still_to_come:ivs.filter(i=>i.upcoming).length;return{done,left}}
function lastDone(p){const xs=(p.interviews||[]).filter(i=>!i.upcoming).map(i=>i.at).sort();return xs.length?xs[xs.length-1]:null}
function owedNames(p){return p.owed||[]}
const COLS=[
 {k:"name",l:"Name",v:p=>p.name},
 {k:"verdicts",l:"Verdicts",v:p=>mood(p.tally)},
 {k:"sessions",l:"Sessions",v:p=>sessions(p).done},
 {k:"owed",l:"Feedback owed",v:p=>owedNames(p).length},
 {k:"last",l:"Last interview",v:p=>lastDone(p),date:1},
 {k:"stage",l:"Stage",v:p=>p.stage}];
function sortRows(rows,st){const idx=new Map(rows.map((p,i)=>[p.candidate_id,i]));const col=COLS.find(c=>c.k===st.sort);if(!col)return rows.slice();
 const sign=st.dir==="desc"?-1:1;
 return rows.slice().sort((x,y)=>{let a=col.v(x),b=col.v(y);const ea=a==null||a==="",eb=b==null||b==="";
  if(ea&&eb)return idx.get(x.candidate_id)-idx.get(y.candidate_id);if(ea)return 1;if(eb)return-1;
  if(col.date){a=+new Date(a);b=+new Date(b)}else if(typeof a==="string"){a=a.toLowerCase();b=String(b).toLowerCase()}
  const c=a<b?-1:a>b?1:0;return c?c*sign:idx.get(x.candidate_id)-idx.get(y.candidate_id)})}
function quotes(p,d){
 const box=el("div",{class:"quotes"});
 const ivs=p.interviews||[];
 const meta=[p.source?"Source: "+p.source+(p.referrer?" (referred by "+p.referrer+")":""):null,p.days_in_process!=null?plural(p.days_in_process,"day")+" in process":null].filter(Boolean).join(" · ");
 if(meta)box.append(el("div",{class:"small muted"},meta));
 if(!ivs.length)box.append(el("div",{class:"small muted"},"No interviews logged for this job."));
 for(const i of ivs){
  box.append(el("div",{class:"iv"},el("b",{},i.name||"Interview"),el("span",{class:"small muted"}," · "+when(i.at)+(i.interviewer?" · with "+i.interviewer:"")+" · "+i.outcome),
   i.note?el("div",{class:"small"},"How it went: "+i.note):null,
   (i.feedback||[]).length?el("ul",{class:"plain fb"},i.feedback.map(f=>el("li",{},el("q",{},f.text),el("span",{class:"small muted"}," "+(f.by||"An interviewer")+(f.unlisted?" (not on the loop, not counted)":""))))):
    (i.owed&&i.owed.length?el("div",{class:"small"},"Feedback still owed by "+i.owed.join(", ")):null)))}
 for(const t of p.todos||[])box.append(el("div",{class:"small"},"To-do: "+t.text));
 return box}
function render(d){
 const wrap=el("div",{});const j=d.job||{};
 const ps=d.people||[];
 wrap.append(header({title:"Finalists: "+j.title+(j.company?" · "+j.company:""),count:ps.length?plural(ps.length,"finalist")+(d.more&&d.more.length?" · "+d.more.length+" more in late stages":""):null,appUrl:d.app_url,appLabel:"Decide in Talent Search"}));
 if(!ps.length){wrap.append(el("p",{class:"empty"},"Nobody has interviewed or reached a late stage yet."));return wrap}
 const jobName=(j.title||"the job")+(j.company?" ("+j.company+")":"");
 const st={sort:null,dir:"asc"};const open=new Set();
 const tbody=el("tbody");const live=el("div",{class:"sr",role:"status","aria-live":"polite"});
 const ths=COLS.map(c=>{const b=el("button",{class:"sort",type:"button"});
  b.addEventListener("click",()=>{if(st.sort===c.k)st.dir=st.dir==="asc"?"desc":"asc";else{st.sort=c.k;st.dir="asc"}fill();heads();announce("Sorted by "+c.l+", "+(st.dir==="asc"?"ascending":"descending"))});
  return{c,b,th:el("th",{scope:"col",class:"k-"+c.k},b)}});
 function heads(){for(const{c,b,th}of ths){const on=st.sort===c.k;th.setAttribute("aria-sort",on?(st.dir==="asc"?"ascending":"descending"):"none");
  b.replaceChildren(c.l,el("span",{class:"arr","aria-hidden":"true"},on?(st.dir==="asc"?" ▲":" ▼"):""));
  b.setAttribute("aria-label","Sort by "+c.l+(on?", sorted "+(st.dir==="asc"?"ascending":"descending"):""))}}
 function row(p,n){
  const s=sessions(p),owed=owedNames(p),ld=lastDone(p);
  const nm=p.name,url=p.app_url;
  const status=el("div",{class:"status small muted",role:"status"});
  const more=el("button",{class:"btn",type:"button","aria-expanded":open.has(p.candidate_id)?"true":"false","aria-controls":"fb-"+p.candidate_id,"aria-label":(open.has(p.candidate_id)?"Hide":"Show")+" feedback from "+nm},open.has(p.candidate_id)?"Hide feedback":"Feedback");
  more.addEventListener("click",()=>{if(open.has(p.candidate_id))open.delete(p.candidate_id);else open.add(p.candidate_id);fill();const b=tbody.querySelector('button[aria-controls="fb-'+p.candidate_id+'"]');if(b)b.focus()});
  const adv=el("button",{class:"btn",type:"button","aria-label":"Advance "+nm},"Advance");
  adv.addEventListener("click",()=>askOrLink("Advance "+nm+" to the next stage for "+jobName+" (candidate id "+p.candidate_id+", job id "+j.id+"). They are in "+p.stage+" now.",adv,status,url,nm));
  const rej=el("button",{class:"btn",type:"button","aria-label":"Reject "+nm},"Reject");
  const acts=el("div",{class:"acts",style:"margin:0"},more,adv,rej);
  rej.addEventListener("click",()=>{
   const yes=el("button",{class:"btn primary",type:"button"},"Yes, reject "+nm),no=el("button",{class:"btn",type:"button"},"Keep them in");
   const ask=el("div",{class:"acts",role:"group","aria-label":"Reject "+nm+"?",style:"margin:0"},el("span",{class:"small"},"Reject "+nm+"?"),yes,no);
   acts.hidden=true;acts.after(ask);yes.focus();
   no.addEventListener("click",()=>{ask.remove();acts.hidden=false;rej.focus()});
   ask.addEventListener("keydown",e=>{if(e.key==="Escape")no.click()});
   yes.addEventListener("click",()=>{ask.remove();acts.hidden=false;askOrLink("Reject "+nm+" for "+jobName+" (candidate id "+p.candidate_id+", job id "+j.id+"): move them to "+(d.out_stage||"the out stage")+".",rej,status,url,nm)})});
  const td=(k,label,...kids)=>el("td",{class:"k-"+k,"data-label":label},...kids);
  const main=el("tr",{class:open.has(p.candidate_id)?"sel":null},
   el("th",{scope:"row",class:"k-name"},personLink(nm,url,p.candidate_id),p.current_title?el("div",{class:"small muted"},role(p.current_title,p.current_company)):null),
   td("verdicts","Verdicts",p.tally||el("span",{class:"muted"},"None yet")),
   td("sessions","Sessions",s.done+" done, "+s.left+" left"+(p.open_sessions?" ("+p.open_sessions+" with no outcome)":"")),
   td("owed","Feedback owed",owed.length?el("span",{class:"chip owed",title:owed.join(", ")},owed.length+" ("+owed.join(", ")+")"):"None"),
   td("last","Last interview",ld?dateEl(ld):el("span",{class:"muted"},"None yet")),
   td("stage","Stage",p.stage),
   td("acts",null,acts,status));
  const rows=[main];
  if(open.has(p.candidate_id))rows.push(el("tr",{class:"qrow",id:"fb-"+p.candidate_id},el("td",{colspan:"7"},quotes(p,d))));
  return rows}
 function fill(){tbody.replaceChildren(...sortRows(ps,st).flatMap(row))}
 const all=el("button",{class:"btn",type:"button"},"Show all feedback");
 all.addEventListener("click",()=>{const on=open.size<ps.length;open.clear();if(on)ps.forEach(p=>open.add(p.candidate_id));all.textContent=on?"Hide all feedback":"Show all feedback";fill()});
 const relBtn=el("button",{class:"btn",type:"button","aria-pressed":"true"},"Furthest along first");
 relBtn.addEventListener("click",()=>{st.sort=null;st.dir="asc";fill();heads();announce("Back to the order the list came in: furthest along first.")});
 const table=el("table",{class:"ptable ftable"},el("caption",{},"Finalists for "+jobName+": click a name for their summary, a heading to sort."),
  el("thead",{},el("tr",{},...ths.map(t=>t.th),el("th",{scope:"col",class:"k-acts"},"Actions"))),tbody);
 heads();fill();
 wrap.append(el("div",{class:"tbar"},relBtn,all,live),el("div",{class:"tablewrap"},table));
 wrap.append(el("p",{class:"small muted"},"Advance and Reject send your request to Claude in the chat. Nothing changes until it does it, and you can undo a move in Talent Search under Changes."));
 if(d.more&&d.more.length){const origin=appOrigin(d.app_url);
  wrap.append(el("h2",{},"Also in late stages"),el("ul",{class:"plain"},d.more.map(m=>el("li",{},personLink(m.name,origin?origin+"/candidates/"+m.candidate_id:null,m.candidate_id)," ",el("span",{class:"muted small"},m.stage||"")))),
   el("p",{class:"small muted"},"Ask Claude to compare any of them with the finalists."))}
 return wrap}
