/* Duplicates view (find_duplicates): one card for each person who may be saved more than once, with the people in it and the matching evidence marked.
   Never merges: the compare and merge page opens in Talent Search. */
const MATCH={"same email":["email"],"same LinkedIn":["linkedin"],"same name and phone":["name","phone"],"same phone":["phone"],"same name and company":["name","company"],
 "similar name and same company":["name","company"],"same name and company, but different emails":["name","company"],"similar name and company, but different emails":["name","company"]};
function matched(reasons){const s=new Set();for(const r of reasons||[])for(const f of MATCH[r]||[])s.add(f);return s}
function sideCard(c,hit,pairIdx,k){
 const box=el("div",{class:"side",role:"group","aria-label":"Person "+(k+1)+", "+(c.email||c.name||"no email saved")});
 const mk=(f,node)=>hit.has(f)&&node?el("mark",{},node):node;
 box.append(el("h3",{},mk("name",personLink(c.name||"Unnamed",c.app_url,c.id))));
 const dl=el("dl",{});const row=(t,v)=>{if(v==null||v==="")return;dl.append(el("dt",{},t),el("dd",{},v))};
 const role=[c.current_title,c.current_title&&c.current_company?" at ":null,c.current_company?mk("company",c.current_company):null].filter(x=>x!=null&&x!=="");
 row("Role",role.length?role:null);
 row("Email",c.email?mk("email",el("a",{href:"mailto:"+c.email,"aria-label":"Email "+(c.name||"them")},c.email)):null);
 row("LinkedIn",c.linkedin_url&&safeUrl(c.linkedin_url)?mk("linkedin",el("button",{class:"link",type:"button","aria-label":"LinkedIn profile of "+(c.name||"them"),onclick:()=>openLink(c.linkedin_url,"LinkedIn")},"LinkedIn")):null);
 row("Phone",c.phone?mk("phone",el("a",{href:"tel:"+String(c.phone).replace(/[^\d+]/g,""),"aria-label":"Call "+(c.name||"them")},c.phone)):null);
 row("Location",c.location);row("Source",c.source);row("Added",c.created_at?dateEl(c.created_at):null);
 box.append(dl);return box}
function groupCard(g,i,d){
 const ps=g.people||[];const hit=matched(g.reasons);
 const art=el("article",{class:"pair group","aria-labelledby":"g"+i});
 art.append(el("h2",{class:"ph",id:"g"+i},g.title||((g.name||"Unnamed")+", "+ps.length+" profiles")));
 art.append(el("ul",{class:"why","aria-label":"Why these may be the same person"},(g.reasons||[]).map(r=>el("li",{class:"chip"},r.charAt(0).toUpperCase()+r.slice(1))),
  g.maybe_different?el("li",{class:"chip maybe",title:"Nothing but the name agrees. They may be different people."},"Same name, maybe different people"):null));
 art.append(el("div",{class:"sides"},ps.map((c,k)=>{const side=sideCard(c,hit,i,k);
  if(c.keep)side.prepend(el("div",{class:"small keepnote"},"Most complete profile"));
  if(c.maybe_different)side.prepend(el("div",{class:"small muted"},"May be a different person"));return side})));
 const acts=el("div",{class:"acts"});
 const url=g.compare_and_merge_url||d.app_url;
 acts.append(el("button",{class:"btn primary",type:"button","aria-label":"Compare and merge "+(g.name||"these profiles")+" in Talent Search",onclick:()=>openLink(url,"the Duplicates page")},"Compare and merge in Talent Search"));
 const status=el("div",{class:"status small muted",role:"status"});
 const names=ps.map(x=>(x.name||"Unnamed")+" (id "+x.id+")").join(", ");
 const nope=el("button",{class:"btn",type:"button",needs:"message","aria-label":"Not the same people: "+names},"Not the same people");
 nope.addEventListener("click",()=>askOrLink("These are different people, not duplicates: "+names+". Please don't suggest them as the same person again, and remind me to mark them as different on the Duplicates page in Talent Search, because only I can.",nope,status,d.app_url,"Duplicates"));
 acts.append(nope);
 art.append(acts,status,el("div",{class:"hint"},"Nothing is merged from here. Merging, and marking people as different, happen in Talent Search."));
 return art}
function render(d){
 const wrap=el("div",{});const gs=d.groups||[];const total=d.total!=null?d.total:gs.length;
 wrap.append(header({title:"Possible duplicates",count:gs.length?(total===1?"1 group":total+" groups")+(gs.length<total?", showing the first "+gs.length:""):null,appUrl:d.app_url,appLabel:"Open in Talent Search",fullscreen:gs.length>0}));
 if(!gs.length){wrap.append(emptyState("No likely duplicates right now."));return wrap}
 wrap.append(el("p",{class:"small muted",style:"margin:0 0 6px"},"Each card is one person who may be saved more than once."));
 const list=el("div",{class:"pairs"});gs.forEach((g,i)=>list.append(groupCard(g,i,d)));wrap.append(list);
 return wrap}
