const LABEL={interview:"Interview",interview_out:"Interview still on",interview_outcome:"How did it go",interview_result:"Interview result",suggestion:"Suggestion",stuck:"Stuck",thin:"Needs more candidates",stale:"May be out of date",ai_change:"AI change",shortlist:"Shortlist",followup:"Follow-up due",replied:"Replied",update_due:"Update due",client_chase:"Client feedback",feedback_owed:"Feedback owed",offer:"Offer",openings:"Openings"};
function render(d){
 const wrap=el("div",{});
 wrap.append(header({title:d.items.length?d.items.length+" thing"+(d.items.length===1?"":"s")+" to do":"Nothing needs you today",appUrl:d.app_url,fullscreen:false}));
 const card=i=>el("div",{class:"card"},el("div",{class:"kind"},LABEL[i.kind]||i.kind),el("div",{},i.title),
  ((i.kind==="interview"||i.kind==="interview_out"||i.kind==="interview_outcome")&&i.when)?el("div",{class:"small"},when(i.when)):null,i.due_text?el("div",{class:"small"+(i.overdue?"":" muted")},i.due_text):null,
  i.detail?el("div",{class:"small muted"},i.detail):null);
 for(const i of d.items)wrap.append(card(i));
 if(d.next_week&&d.next_week.length){wrap.append(el("h2",{},"Next week"));for(const i of d.next_week)wrap.append(card(i))}
 return wrap}
