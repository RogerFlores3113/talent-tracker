/* Duplicates view (find_duplicates): each possible pair as two compact cards with the matching evidence marked.
   Never merges: the compare and merge page opens in Talent Search. */
const MATCH={"same email":["email"],"same LinkedIn":["linkedin"],"same name and phone":["name"],"same name and company":["name","company"],
 "similar name and same company":["name","company"],"same name and company, but different emails":["name","company"],"similar name and company, but different emails":["name","company"]};
function matched(reasons){const s=new Set();for(const r of reasons||[])for(const f of MATCH[r]||[])s.add(f);return s}
function sideCard(c,hit,pairIdx,k){
 const box=el("div",{class:"side"});
 const mk=(f,node)=>hit.has(f)&&node?el("mark",{},node):node;
 box.append(el("h3",{},mk("name",personLink(c.name||"Unnamed",c.app_url,c.id))));
 const dl=el("dl",{});const row=(t,v)=>{if(v==null||v==="")return;dl.append(el("dt",{},t),el("dd",{},v))};
 const role=[c.current_title,c.current_title&&c.current_company?" at ":null,c.current_company?mk("company",c.current_company):null].filter(x=>x!=null&&x!=="");
 row("Role",role.length?role:null);
 row("Email",c.email?mk("email",el("a",{href:"mailto:"+c.email,"aria-label":"Email "+(c.name||"them")},c.email)):null);
 row("LinkedIn",c.linkedin_url&&safeUrl(c.linkedin_url)?mk("linkedin",el("button",{class:"link",type:"button","aria-label":"LinkedIn profile of "+(c.name||"them"),onclick:()=>openLink(c.linkedin_url,"LinkedIn")},"LinkedIn")):null);
 row("Location",c.location);row("Source",c.source);row("Added",c.created_at?dateEl(c.created_at):null);
 box.append(dl);return box}
function pairRow(p,i,d){
 const a=(p.people||[])[0]||{},b=(p.people||[])[1]||{};const hit=matched(p.reasons);
 const art=el("article",{class:"pair","aria-labelledby":"p"+i});
 art.append(el("h2",{class:"ph",id:"p"+i},"Pair "+(i+1)+": "+(a.name||"Unnamed")+" and "+(b.name||"Unnamed")));
 art.append(el("ul",{class:"why","aria-label":"Why these may be the same person"},(p.reasons||[]).map(r=>el("li",{class:"chip"},r.charAt(0).toUpperCase()+r.slice(1)))));
 art.append(el("div",{class:"sides"},sideCard(a,hit,i,0),sideCard(b,hit,i,1)));
 const acts=el("div",{class:"acts"});
 if(p.compare_and_merge_url)acts.append(el("button",{class:"btn primary",type:"button","aria-label":"Compare and merge "+(a.name||"")+" and "+(b.name||"")+" in Talent Search",onclick:()=>openLink(p.compare_and_merge_url,"the compare page")},"Compare and merge in Talent Search"));
 if(d.app_url)acts.append(el("button",{class:"btn",type:"button","aria-label":"Not the same person: open Duplicates in Talent Search to mark "+(a.name||"")+" and "+(b.name||"")+" as different people",onclick:()=>openLink(d.app_url,"the duplicates page")},"Not the same person"));
 art.append(acts,el("div",{class:"hint"},"Nothing is merged from here. Merging and marking a pair as different happen in Talent Search."));
 return art}
function render(d){
 const wrap=el("div",{});const ps=d.pairs||[];const total=d.total!=null?d.total:ps.length;
 wrap.append(header({title:"Possible duplicates",count:ps.length?(total===1?"1 pair":total+" pairs")+(ps.length<total?", showing the first "+ps.length:""):null,appUrl:d.app_url,appLabel:"Open in Talent Search"}));
 if(!ps.length){wrap.append(emptyState("No likely duplicates right now."));return wrap}
 const list=el("div",{class:"pairs"});ps.forEach((p,i)=>list.append(pairRow(p,i,d)));wrap.append(list);
 return wrap}
