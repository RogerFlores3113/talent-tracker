/* Digest view: status_digest and client_report. The update as the client will see it, a Copy button that copies
   exactly that text, and the recruiter-only notes below it, apart. Job names link to the app. */
function linkify(line,jobs){
 const list=(jobs||[]).filter(j=>j.title&&j.app_url).sort((a,b)=>b.title.length-a.title.length);
 for(const j of list){let i=line.indexOf(j.title);
  while(i>0&&line[i-1]==="(")i=line.indexOf(j.title,i+1);  // a title inside a person's "(role at company)" is not the job
  if(i>=0)return[line.slice(0,i),jobLink({job:j.title,app_url:j.app_url}),line.slice(i+j.title.length)]}
 return[line]}
function isJobHead(line,jobs){return(jobs||[]).some(j=>j.title&&line.startsWith(j.title)&&/^[,·]|^ ?\(| ·/.test(line.slice(j.title.length,j.title.length+3)))}
/* the paste text, drawn as headings, short paragraphs and bullets (text only goes in with DOM calls) */
function previewOf(text,jobs){
 const box=el("div",{class:"preview",tabindex:"0","aria-label":"The update as the client will see it"});
 const lines=String(text||"").split("\n");let ul=null,first=true;
 for(const raw of lines){const line=raw.replace(/\s+$/,"");
  if(!line.trim()){ul=null;continue}
  if(/^\s*[-•]\s+/.test(line)){if(!ul){ul=el("ul");box.append(ul)}ul.append(el("li",{},linkify(line.replace(/^\s*[-•]\s+/,""),jobs)));continue}
  ul=null;
  if(first){box.append(el("h2",{},line));first=false;continue}
  if(isJobHead(line,jobs)){box.append(el("h2",{},linkify(line,jobs)));continue}
  if(/:$/.test(line)&&line.length<60){box.append(el("h3",{style:"font-size:.875rem;margin:10px 0 2px"},line));continue}
  box.append(el("p",{},linkify(line,jobs)))}
 return box}
function oneUpdate(u,jobs,idx,n){
 const sec=el("section",{"aria-label":u.for?"Update for "+u.for:"Update"});
 if(u.for)sec.append(el("p",{class:"small muted",style:"margin:10px 0 2px"},"For "+u.for+" (this line is not part of the update)"));
 const pv=previewOf(u.text,jobs);
 const st=el("span",{class:"small muted",role:"status"});
 const btn=el("button",{class:"btn primary",type:"button","aria-label":u.for?"Copy the update for "+u.for:"Copy update",onclick:()=>copyText(u.text,sec.querySelector(".acts"),st,"Copied. Paste it where you send updates.")},u.for?"Copy this update":"Copy update");
 sec.append(el("div",{class:"acts",style:"margin:0 0 8px"},btn,st),pv);return sec}
function render(d){
 const wrap=el("div",{});
 const jobs=d.job_links||[];
 const ups=d.update_text?[{for:null,text:d.update_text}]:(d.updates||[]).filter(u=>u&&u.text);
 const isClient=TOOL==="client_report"||(!d.update_text&&d.client);
 wrap.append(header({title:isClient?"Client update"+(d.client?": "+d.client:""):"Status update",count:d.since&&isClient?"Since "+day(d.since):null,appUrl:d.app_url,appLabel:"Open in Talent Search",fullscreen:false}));
 if(!ups.length){wrap.append(emptyState("There is no update to show right now."));
  if(d.last_sent_copy)wrap.append(oneUpdate({for:null,text:d.last_sent_copy},jobs));return wrap}
 wrap.append(el("p",{class:"small muted",style:"margin:0 0 6px"},"This is what the client sees. Only this part is copied."));
 ups.forEach((u,i)=>wrap.append(oneUpdate(u,jobs,i,ups.length)));
 const notes=(d.recruiter_notes||[]).slice();
 for(const o of d.hired_over_openings||[])notes.push(o.title+" has "+o.hired+" hired for "+o.openings+" opening"+(o.openings===1?"":"s")+". Check the number of openings.");
 const box=el("section",{class:"forme","aria-label":"For you, not in the update"},el("h2",{},"For you, not in the update"));
 if(notes.length)box.append(el("ul",{},notes.map(n=>el("li",{},n))));else box.append(el("p",{style:"margin:0"},"Nothing was left out."));
 wrap.append(box);return wrap}
