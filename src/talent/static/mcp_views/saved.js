const FIELD={current_title:"title",current_company:"company",linkedin_url:"LinkedIn",work_history:"work history",email:"email",phone:"phone",location:"location"};
const GROUPS=[["saved","Saved"],["updated","Updated"],["unchanged","Already saved, nothing new"],["needs_answer","Needs your answer (nothing saved)"],["not_saved","Not saved"]];
function fields(xs){return (xs||[]).map(f=>FIELD[f]||String(f).replace(/_/g," ")).join(", ")}
function row(r){const bits=[];
 if(r.stage)bits.push(r.already_in_job?"already in "+r.stage:"added to "+r.stage);
 if(r.kept&&r.kept.length)bits.push("kept the saved "+fields(r.kept));
 if(r.held&&r.held.length)bits.push("held for you: "+fields(r.held));
 return openCard("pc",r.app_url,el("div",{class:"row"},el("b",{},r.name),r.saved_status&&r.saved_status!=="Saved"?el("span",{class:"badge"},r.saved_status):null),
  bits.length?el("div",{class:"small muted"},bits.join(" · ")):null,
  r.reason?el("div",{class:"small"},r.reason):null,
  (r.notes||[]).map(x=>el("div",{class:"small"},x)),
  r.pipeline_error?el("div",{class:"small"},"Not added to the job: "+r.pipeline_error):null)}
function render(d){
 const wrap=el("div",{});
 wrap.append(header({title:d.job?"Saved for "+d.job.title+(d.job.company?" · "+d.job.company:""):"Saved people",count:d.summary||null,appUrl:d.app_url,fullscreen:false,refresh:false}));
 if(d.warning)wrap.append(el("div",{class:"card small"},d.warning));
 for(const[k,label]of GROUPS){const rs=d[k]||[];if(!rs.length)continue;wrap.append(el("h2",{},label+" ("+rs.length+")"));for(const r of rs)wrap.append(row(r))}
 if(d.batch_id)wrap.append(el("p",{class:"small muted"},"You can undo this whole batch in Talent Search, under Changes."));
 return wrap}
