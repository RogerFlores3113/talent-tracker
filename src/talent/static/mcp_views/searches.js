/* Saved searches view. Run shows the People view inside this view (people.js is loaded first, see mcp_views.py). */
const SS={list:null,name:null,origin:"",saveTool:null,saveArgs:null};
function ssCard(s){
 const li=el("li",{class:"lrow"});
 li.append(el("h2",{class:"lt"},s.name,
  s.color?el("span",{class:"status-chip"},el("span",{class:"dot"+colorCls(s.color),"aria-hidden":"true"}),s.color+" tag"):null,
  s.new_since_opened?el("span",{class:"status-chip new"},s.new_since_opened+" new"):null));
 li.append(el("div",{class:"query","aria-label":"What it searches for"},s.search||"Everyone"));
 const facts=el("dl",{class:"facts",style:"display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:8px 0 0;font-size:.8125rem"});
 const fact=(k,v)=>{if(v==null||v==="")return;facts.append(el("dt",{class:"muted"},k),el("dd",{style:"margin:0"},v))};
 fact("Matches now",s.matches==null?"not counted":String(s.matches));
 fact("New since you last looked",s.new_since_opened!=null?String(s.new_since_opened):null);
 fact("Last opened in Talent Search",s.last_opened?dateEl(s.last_opened):"not yet");
 fact("Saved by",s.saved_by);
 li.append(facts);
 const acts=el("div",{class:"acts"});
 const run=el("button",{class:"btn primary",type:"button","aria-label":"Run the saved search "+s.name,onclick:()=>runSearch(s,run)},"Run");
 acts.append(run);
 if(s.app_url)acts.append(el("button",{class:"btn",type:"button","aria-label":"Open the saved search "+s.name+" in Talent Search",onclick:()=>openLink(s.app_url,"Talent Search")},"Open in Talent Search"));
 li.append(acts);return li}
function backToList(){const m=document.getElementById("main");SS.name=null;TOOL="list_saved_searches";ARGS={};if(SS.list)m.replaceChildren(listView(SS.list));const h=document.getElementById("vtitle");if(h)h.focus()}
function runSearch(s,btn){
 if(!canCall()){askClaude("Run my saved search "+s.name,btn);return}
 btn.disabled=true;announce("Running "+s.name+".");
 callTool("run_saved_search",{name_or_id:s.id,limit:10}).then(r=>{btn.disabled=false;
  const d=r&&r.structuredContent;
  if(r&&r.isError||!d||!Array.isArray(d.results)){announce(said(r)||"Couldn't run that search.");askClaude("Run my saved search "+s.name,btn);return}
  SS.name=s.name;TOOL="run_saved_search";ARGS={name_or_id:s.id};
  const m=document.getElementById("main");m.replaceChildren(renderPeople(d));addBack();
  const h=m.querySelector("h1");if(h){h.tabIndex=-1;h.focus()}announce(s.name+": "+d.results.length+" shown.")})}
function backBar(){return el("div",{class:"acts ssback",style:"margin:0 0 6px"},el("button",{class:"btn",type:"button",onclick:backToList},"Back to saved searches"),el("span",{class:"small muted"},"Saved search: "+SS.name))}
function addBack(){if(!SS.name)return;const m=document.getElementById("main");if(!m.querySelector(".ssback"))m.prepend(backBar())}
/* the People view redraws #main when you switch between cards and a table: keep the Back button there */
draw=function(focus){drawPeople(focus);addBack()};
function listView(d){
 const items=d.saved_searches||[];const wrap=el("div",{});
 const news=items.reduce((a,s)=>a+(s.new_since_opened||0),0);
 wrap.append(header({title:"Saved searches",count:items.length?items.length+(items.length===1?" search":" searches")+(news?", "+news+" new people":""):null,appUrl:d.app_url,appLabel:"Open in Talent Search",fullscreen:false,refresh:false}));
 if(!items.length){wrap.append(emptyState("No saved searches yet. Ask Claude to save one, and it will show up here."));return wrap}
 wrap.append(el("ul",{class:"plain","aria-label":"Saved searches"},items.map(ssCard)));return wrap}
render=function(d){
 if(Array.isArray(d.saved_searches)){SS.list=d;SS.name=null;return listView(d)}
 if(Array.isArray(d.results)){const n=renderPeople(d);if(SS.name)n.prepend(backBar());return n}
 return emptyState("Nothing to show.")};
