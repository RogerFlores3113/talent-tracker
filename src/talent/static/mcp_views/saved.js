const FIELD={current_title:"title",current_company:"company",linkedin_url:"LinkedIn",work_history:"work history",email:"email",phone:"phone",location:"location"};
const GROUPS=[["saved","Saved"],["updated","Updated"],["unchanged","Already saved, nothing new"],["needs_answer","Needs your answer (nothing saved)"],["not_saved","Not saved"]];
let ORIGIN="",JOB=null;
function fields(xs){return (xs||[]).map(f=>FIELD[f]||String(f).replace(/_/g," ")).join(", ")}
function cardUrl(id){return ORIGIN&&id?ORIGIN+"/candidates/"+id:null}
function roleOf(c){return role(c.current_title,c.current_company)}
function nameEl(c){return c.id?personLink(c.name,c.app_url||cardUrl(c.id),c.id):el("b",{},c.name)}
/* a saved person who saved or updated: facts, a no-contact badge, links */
function row(r){const bits=[];
 if(r.stage)bits.push(r.already_in_job?"already in "+r.stage:"added to "+r.stage);
 if(r.kept&&r.kept.length)bits.push("kept the saved "+fields(r.kept));
 if(r.held&&r.held.length)bits.push("held for you: "+fields(r.held));
 const facts=[roleOf(r),r.location].filter(Boolean).join(" · ");
 const links=linksRow({name:r.name,app_url:r.app_url,linkedin_url:r.linkedin_url,links:r.links});
 return el("li",{class:"card"},
  el("div",{class:"row"},el("b",{},nameEl(r)),el("span",{},
   r.saved_status&&r.saved_status!=="Saved"?el("span",{class:"badge"},r.saved_status):null,
   r.no_contact?el("span",{class:"badge",title:"No email and no LinkedIn link are saved, so there is no way to reach them yet."},"No contact info"):null)),
  facts?el("div",{class:"small"},facts):null,
  bits.length?el("div",{class:"small muted"},bits.join(" · ")):null,
  (r.notes||[]).map(x=>el("div",{class:"small"},x)),
  r.pipeline_error?el("div",{class:"small"},"Not added to the job: "+r.pipeline_error):null,
  links.childNodes.length?links:null)}
/* a possible match for someone who was not saved: who they are, and how to answer */
function match(m,incoming,status){
 const diffs=(m.differences||[]).map(x=>el("li",{},x.field+": saved "+x.saved+", you sent "+x.yours));
 const id=m.id;const nm=m.name;
 const same=el("button",{class:"btn",type:"button","aria-label":"Same person: update "+nm+" (id "+id+") with what you sent for "+incoming},"Same person");
 same.addEventListener("click",()=>askOrLink("Update "+nm+" (candidate id "+id+") with what I sent for "+incoming+", they are the same person.",same,status,cardUrl(id),nm));
 const facts=[roleOf(m),m.location].filter(Boolean).join(" · ");
 return el("li",{class:"match"},el("div",{},nameEl(m)," ",el("span",{class:"small muted"},"(id "+id+")")),
  facts?el("div",{class:"small"},facts):null,
  (m.email||m.linkedin_url)?el("div",{class:"small muted"},[m.email,m.linkedin_url&&"LinkedIn saved"].filter(Boolean).join(" · ")):null,
  diffs.length?el("ul",{class:"small"},diffs):null,
  el("div",{class:"acts"},same,m.app_url||cardUrl(id)?linkBtn(m.app_url||cardUrl(id),"Open in Talent Search","Open "+nm+" in Talent Search","btn"):null))}
function answerRow(r){
 const status=el("div",{class:"status small muted",role:"status"});
 const people=(r.possible_duplicates&&r.possible_duplicates.length)?r.possible_duplicates:(r.existing?[r.existing]:[]);
 const li=el("li",{class:"card"},el("div",{class:"row"},el("b",{},r.name),el("span",{class:"badge"},"Nothing saved yet")),
  r.reason?el("div",{class:"small"},r.reason):null);
 if(people.length){
  li.append(el("h3",{class:"muted small",style:"margin:8px 0 2px"},people.length===1?"The saved person it could be":"The "+people.length+(r.more_duplicates?"+":"")+" saved people it could be"));
  const mk=m=>match(m,r.name,status);
  const first=people.slice(0,5),rest=people.slice(5);
  li.append(el("ul",{class:"plain matches"},first.map(mk)));
  if(rest.length)li.append(el("details",{class:"small"},el("summary",{},"Show "+rest.length+" more"+(r.more_duplicates?" (and "+r.more_duplicates+" not listed)":"")),el("ul",{class:"plain matches"},rest.map(mk))))}
 const anyway=el("button",{class:"btn primary",type:"button","aria-label":"Save "+r.name+" as someone new"},"Someone new, save "+r.name);
 anyway.addEventListener("click",()=>askOrLink("Save "+r.name+" as a new person anyway (create_anyway), they are not any of the saved people with that name"+(JOB?" and add them to "+JOB:"")+".",anyway,status,null,null));
 li.append(el("div",{class:"acts"},anyway),status);
 return li}
function plainRow(r){return el("li",{class:"card"},el("b",{},r.name),r.reason?el("div",{class:"small"},r.reason):null)}
function render(d){
 const wrap=el("div",{});ORIGIN=appOrigin(d.app_url);JOB=d.job?d.job.title+(d.job.company?" ("+d.job.company+")":""):null;
 wrap.append(header({title:d.job?"Saved for "+d.job.title+(d.job.company?" · "+d.job.company:""):"Saved people",count:d.summary||null,appUrl:d.app_url,fullscreen:false,refresh:false}));
 if(d.warning)wrap.append(el("div",{class:"card small"},d.warning));
 for(const[k,label]of GROUPS){const rs=d[k]||[];if(!rs.length)continue;
  wrap.append(el("h2",{},label+" ("+rs.length+")"),el("ul",{class:"plain"},rs.map(k==="needs_answer"?answerRow:k==="not_saved"?plainRow:row)))}
 if(d.batch_id)wrap.append(el("p",{class:"small muted"},"You can undo this whole batch in Talent Search, under Changes."));
 return wrap}
