/* Compare view: two to six people side by side, with an "At a glance" list. Read-only. DOM calls only.
   Also loaded inside the People and Saved searches views (their Compare button), where people.js's render wins and
   calls renderCompare for a compare result (see people.js). */
let CMP_BACK=null;
function cmpKeys(d){const gone=new Set(d.rows_empty||[]);return[["title","Title and company"],["location","Location"],["years","Years of experience"],["skills","Skills"],["jobs","Jobs and stages"],["source","Found via"],["contacted","Last contacted"],["links","Links"],["about","About"]].filter(r=>!gone.has(r[0]))}
function cmpSkills(d,p){
 const low=x=>String(x).toLowerCase();
 const shared=new Set((d.shared_skills||[]).map(low));
 const mine=p.tags||[];
 const groups=[["shared","Shared",mine.filter(t=>shared.has(low(t))).map(t=>({t}))],
  ["some","Some of them",(p.some_of_them||[]).map(x=>({t:x.skill,n:x.count,of:x.of}))],
  ["only","Only them",(p.only_them||[]).map(t=>({t}))]];
 const box=el("div",{});
 for(const[k,label,list]of groups){if(!list.length)continue;
  box.append(el("div",{class:"cmp-group","data-group":k},el("span",{class:"cmp-label "+k},label),
   el("span",{class:"chips"},list.map(c=>el("span",{class:"chip"},c.t,c.n?el("span",{class:"cmp-n"},", "+c.n+" of "+c.of+" have it"):null)))))}
 return box.childNodes.length?box:el("span",{class:"muted"},"No skills saved")}
function cmpAbout(p){
 const t=p.about||"";if(!t)return el("span",{class:"muted"},"Nothing saved");
 if(t.length<=160)return el("p",{class:"cmp-about"},t);
 const full=el("p",{class:"cmp-about",hidden:true},t);const clip=el("span",{},t.slice(0,160)+"...");
 const b=el("button",{class:"link",type:"button","aria-expanded":"false","aria-label":"Show more about "+p.name,onclick:()=>{const open=full.hidden;full.hidden=!open;clip.hidden=open;
  b.textContent=open?"less":"more";b.setAttribute("aria-expanded",open?"true":"false");b.setAttribute("aria-label",(open?"Show less about ":"Show more about ")+p.name)}},"more");
 return el("div",{},clip," ",b,full)}
function cmpCell(k,d,p){
 if(k==="title"){const r=role(p.title,p.company);return r||el("span",{class:"muted"},"Not saved")}
 if(k==="location")return p.location||el("span",{class:"muted"},"Not saved");
 if(k==="years")return p.years_text||el("span",{class:"muted"},"No work dates saved");
 if(k==="skills")return cmpSkills(d,p);
 if(k==="jobs"){const js=p.jobs||[];if(!js.length)return el("span",{class:"muted"},"Not in any job");
  return el("ul",{class:"plain"},js.map(j=>el("li",{},j.app_url?linkBtn(j.app_url,j.job,"Open the pipeline for "+j.job):j.job,el("div",{class:"small muted"},"Stage: "+j.stage))))}
 if(k==="source")return p.source||el("span",{class:"muted"},"Not saved");
 if(k==="contacted")return p.last_contacted_at?dateEl(p.last_contacted_at):el("span",{class:"never"},"None logged");
 if(k==="links"){const ls=(p.links||[]).filter(l=>safeUrl(l.url));if(!ls.length)return el("span",{class:"muted"},"None saved");
  return el("ul",{class:"plain"},ls.map(l=>el("li",{},linkBtn(l.url,l.label,l.label+" of "+p.name))))}
 if(k==="about")return cmpAbout(p);
 return""}
function cmpRemove(d,p,opts,btn){
 const rest=d.people.filter(x=>x.id!==p.id).map(x=>x.id);
 btn.disabled=true;announce("Removing "+p.name+".");
 callTool("compare_candidates",{candidate_ids:rest}).then(r=>{const nd=r&&r.structuredContent;
  if(r&&r.isError||!nd||!Array.isArray(nd.people)){btn.disabled=false;announce(said(r)||"Couldn't update the comparison.");return}
  ARGS={candidate_ids:rest};const m=document.getElementById("main");m.replaceChildren(renderCompare(nd,opts));
  const h=m.querySelector("h1");if(h){h.tabIndex=-1;h.focus()}announce(p.name+" removed. Comparing "+nd.people.length+" people.")})}
function renderCompare(d,opts){
 if(opts&&opts.onBack)CMP_BACK=opts.onBack;
 const keep=opts||{onBack:CMP_BACK};
 const ps=d.people||[];const wrap=el("div",{});
 wrap.append(header({title:"Comparing "+ps.length+" people",count:(d.missing&&d.missing.length)?d.missing.length+" chosen "+(d.missing.length===1?"person is":"people are")+" no longer saved":null,appUrl:d.app_url,appLabel:"Open in Talent Search",fullscreen:ps.length>0}));
 if(!ps.length){wrap.append(emptyState("Nothing to compare."));return wrap}
 if(d.left_out&&d.left_out.length)wrap.append(el("p",{class:"small muted"},"You chose "+(ps.length+d.left_out.length)+" people, so this shows the first "+ps.length+". Left out: "+d.left_out.map(x=>x.name).join(", ")+"."));
 const out=el("span",{class:"small",role:"status","aria-live":"polite"});
 const xl=el("button",{class:"btn",type:"button"},"Export xlsx");
 xl.addEventListener("click",()=>exportIds(ps.map(p=>p.id),xl,out,0));
 const bar=el("div",{class:"acts",style:"margin:0 0 6px"},
  CMP_BACK?el("button",{class:"btn",type:"button",onclick:()=>CMP_BACK()},"Back to the list"):null,
  d.share_url?el("button",{class:"btn",type:"button",onclick:()=>openLink(d.share_url,"the Share page")},"Share as xlsx"):null,
  canCall()?xl:null,out);
 wrap.append(bar);
 const g=el("section",{class:"cmp-glance","aria-labelledby":"cmp-glance-h"},el("h2",{id:"cmp-glance-h"},"At a glance"));
 const lines=d.at_a_glance||[];
 if(lines.length)g.append(el("ul",{},lines.map(l=>el("li",{},l))));else g.append(el("p",{class:"muted"},"Nothing stands out. They look much alike on what is saved."));
 wrap.append(g);
 const differs=new Set(d.rows_that_differ||[]);
 const canRemove=ps.length>2&&canCall();
 const head=el("tr",{},el("th",{scope:"col",class:"cmp-rowlabel"},el("span",{class:"sr"},"What is compared")),
  ps.map(p=>{const rm=canRemove&&p.id?el("button",{class:"cmp-rm",type:"button","aria-label":"Remove "+p.name+" from the comparison",title:"Remove "+p.name+" from the comparison"},el("span",{"aria-hidden":"true"},"\u00d7")):null;
   if(rm)rm.addEventListener("click",()=>cmpRemove(d,p,keep,rm));
   return el("th",{scope:"col",class:"cmp-person"},el("div",{class:"cmp-name"},avatar(p,true),el("span",{class:"cmp-nm"},p.id?personLink(p.name,p.app_url,p.id):p.name),rm))}));
 const keys=cmpKeys(d);
 const body=keys.map(([k,label])=>el("tr",{class:differs.has(k)?"differs":null},
  el("th",{scope:"row",class:"cmp-rowlabel"},label,differs.has(k)?el("span",{class:"cmp-diff",title:"This is not the same for everyone"},"differs"):null),
  ps.map(p=>el("td",{},cmpCell(k,d,p)))));
 const table=el("table",{class:"ctable",style:"--cols:"+ps.length},el("caption",{class:"sr"},ps.length+" people compared. Each column is a person and each row is one thing about them. Rows marked differs are not the same for everyone."),el("thead",{},head),el("tbody",{},body));
 wrap.append(el("div",{class:"cmp-wrap",tabindex:"0",role:"region","aria-label":"Comparison table, scrolls sideways"},table));
 if(d.years_missing_for_some&&keys.some(r=>r[0]==="years"))wrap.append(el("p",{class:"small muted"},"Add work history on a profile to see years of experience."));
 return wrap}
function render(d){return renderCompare(d)}
