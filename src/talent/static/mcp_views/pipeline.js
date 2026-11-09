function render(d){
 const wrap=el("div",{});
 const n=d.counts||{active:d.stages.filter(s=>s.outcome==="open").reduce((a,s)=>a+s.candidates.length,0)};
 const sum=[n.active+" in the running"];if(n.hired!=null)sum.push(d.openings>1?n.hired+" of "+d.openings+" hired":n.hired+" hired");if(n.out)sum.push(n.out+" out");
 const line=d.headline||(sum.join(" · "));
 wrap.append(header({title:d.job.title+(d.job.company?" · "+d.job.company:""),count:line+(d.job.status&&d.job.status!=="open"?" · "+d.job.status.replace("_"," "):""),appUrl:d.app_url}));
 const board=el("div",{class:"board wrap"});
 for(const s of d.stages){board.append(el("div",{class:"col"+(s.outcome!=="open"?" closed":"")},el("h3",{},el("span",{},s.name+(s.outcome==="hired"?" (hired)":s.outcome==="lost"?" (out)":"")),el("span",{class:"muted"},s.candidates.length)),
  s.candidates.length?s.candidates.map(c=>openCard("pc",c.app_url,el("b",{},c.name),c.current_title?el("div",{class:"muted small"},c.current_title):null,
   el("div",{class:"muted small"},(c.days_in_stage!=null?c.days_in_stage+(c.days_in_stage===1?" day":" days")+" in stage":"since "+day(c.since))),
   c.next_interview?el("div",{class:"small"},"Interview "+when(c.next_interview)):null)):el("div",{class:"muted small"},"Empty")))}
 wrap.append(board);return wrap}
