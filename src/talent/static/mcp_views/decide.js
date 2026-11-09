function iv(i){return el("div",{class:"pc"},el("div",{class:"row"},el("b",{},i.name),el("span",{class:"badge"},i.outcome)),
 el("div",{class:"small muted"},when(i.at)+(i.interviewer?" · with "+i.interviewer:"")),
 i.note?el("div",{class:"small"},"How it went: "+i.note):null,
 (i.feedback||[]).map(f=>el("div",{class:"small"},el("b",{},(f.by||"Interviewer")+": "),f.text)))}
function render(d){
 const wrap=el("div",{});const j=d.job||{};
 wrap.append(header({title:"Finalists: "+j.title+(j.company?" · "+j.company:""),appUrl:d.app_url,appLabel:"Decide in Talent Search"}));
 const ps=d.people||[];
 if(!ps.length){wrap.append(el("p",{class:"empty"},"Nobody has interviewed or reached a late stage yet."));return wrap}
 const board=el("div",{class:"board wrap"});
 for(const p of ps){const sub=[p.stage,p.days_in_process!=null?p.days_in_process+(p.days_in_process==1?" day":" days")+" in process":null].filter(Boolean).join(" · ");
  board.append(el("div",{class:"col"},el("h3",{},personLink(p.name,p.app_url,p.candidate_id),appLink(p.app_url,"Open "+p.name+" in Talent Search")),
   el("div",{class:"small muted"},sub),p.source?el("div",{class:"small muted"},"Source: "+p.source+(p.referrer?" (referred by "+p.referrer+")":"")):null,
   (p.interviews||[]).length?(p.interviews||[]).map(iv):el("div",{class:"small muted"},"No interviews logged."),
   (p.todos||[]).map(t=>el("div",{class:"small"},"To-do: "+t.text))))}
 wrap.append(board);
 if(d.more&&d.more.length)wrap.append(el("p",{class:"small muted"},"Also in late stages: "+d.more.map(m=>m.name).join(", ")));
 return wrap}
