let people = [];
let entries = [];
let periods = [];
let calendarSubscriptions = [];
let directIcalUrl = "";
let selectedQuick = null;
let selectedModal = null;
let selectedBatch = null;
let batchPreviewKey = null;
let exportPeople = [];
let exportPeopleAllSelected = true;
const YEAR_MONTH_NAMES = ["Januar","Februar","März","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"];
let yearMonths = Array.from({length:12},(_,i)=>i+1);
let editingId = null;

let entryScopes={
  quick:{all:true,ids:[]},
  modal:{all:true,ids:[]},
  batch:{all:true,ids:[]},
};
let scopeEditingMode="quick";
let scopeDraft={all:true,ids:[]};

function normalizeCompanyIds(ids){
  const valid=new Set(people.map(p=>Number(p.id)));
  return [...new Set((ids||[]).map(Number).filter(id=>valid.has(id)))];
}
function scopeState(mode){return entryScopes[mode]||entryScopes.quick;}
function setScopeState(mode,value={all:true,ids:[]}){
  entryScopes[mode]={all:Boolean(value.all),ids:normalizeCompanyIds(value.ids)};
  renderScopeSummary(mode);
  if(mode==="batch") resetBatchPreview();
}
function setScopeFromEntry(mode,e=null){
  setScopeState(mode,e?{all:Number(e.applies_to_all)!==0,ids:e.company_ids||[]}:{all:true,ids:[]});
}
function scopePayload(mode){
  const state=scopeState(mode);
  return {applies_to_all:state.all,company_ids:state.all?[]:normalizeCompanyIds(state.ids)};
}
function scopeSummaryText(state){
  if(state.all) return "Alle Unternehmen";
  const names=normalizeCompanyIds(state.ids).map(id=>people.find(p=>Number(p.id)===id)?.name).filter(Boolean);
  if(!names.length) return "Keine Unternehmen ausgewählt";
  if(names.length<=3) return names.join(" · ");
  return `${names.slice(0,2).join(" · ")} · +${names.length-2}`;
}
function renderScopeSummary(mode){
  const state=scopeState(mode);
  const text=scopeSummaryText(state);
  const el=qs(`#${mode}ScopeSummary`); if(el) el.textContent=text;
  const btn=qs(`#${mode}ScopeButton`); if(btn) btn.textContent=state.all?"Alle Unternehmen":`${state.ids.length} Unternehmen ausgewählt`;
}
function renderCompanyScopeGrid(){
  const grid=qs("#companyScopeGrid"); if(!grid) return;
  const query=(qs("#companyScopeSearch")?.value||"").trim().toLowerCase();
  const all=Boolean(scopeDraft.all);
  const selected=new Set(scopeDraft.ids.map(Number));
  grid.innerHTML=people.filter(p=>!query||p.name.toLowerCase().includes(query)).map(p=>`<label class="export-person-option company-scope-option ${all?"disabled":""}"><input type="checkbox" data-company-scope-id="${p.id}" ${selected.has(Number(p.id))?"checked":""} ${all?"disabled":""}><span class="dot" style="background:${esc(p.color)}"></span><span>${esc(p.name)}</span></label>`).join("")||'<div class="small">Keine Unternehmen gefunden.</div>';
  qsa("#companyScopeGrid [data-company-scope-id]").forEach(box=>box.addEventListener("change",()=>{
    const id=Number(box.dataset.companyScopeId);
    const set=new Set(scopeDraft.ids.map(Number));
    if(box.checked)set.add(id);else set.delete(id);
    scopeDraft.ids=[...set];
  }));
}
function openCompanyScopeModal(mode){
  scopeEditingMode=mode;
  const state=scopeState(mode);
  scopeDraft={all:Boolean(state.all),ids:[...state.ids]};
  const all=qs("#companyScopeAll"); if(all) all.checked=scopeDraft.all;
  const search=qs("#companyScopeSearch"); if(search) search.value="";
  renderCompanyScopeGrid();
  qs("#companyScopeModalBack")?.classList.add("open");
  document.body.classList.add("modal-open");
}
function closeCompanyScopeModal(){
  qs("#companyScopeModalBack")?.classList.remove("open");
  if(!qsa(".modalback.open").length) document.body.classList.remove("modal-open");
}
function applyCompanyScope(){
  if(!scopeDraft.all && !scopeDraft.ids.length) return toast("Mindestens ein Unternehmen auswählen oder Alle Unternehmen aktivieren");
  setScopeState(scopeEditingMode,scopeDraft);
  closeCompanyScopeModal();
}
function eventMatchesCompany(e,companyName){
  if(!companyName) return true;
  if(Number(e.applies_to_all)!==0) return true;
  return Array.isArray(e.company_names)&&e.company_names.includes(companyName);
}
function eventMatchesCompanySelection(e,names){
  if(exportPeopleAreAll()) return true;
  if(Number(e.applies_to_all)!==0) return true;
  return Array.isArray(e.company_names)&&e.company_names.some(name=>names.includes(name));
}


const qs = s => document.querySelector(s);
const qsa = s => [...document.querySelectorAll(s)];

async function api(url, options={}) {
  const headers=new Headers(options.headers||{});
  if(!(options.body instanceof FormData) && options.body!=null && !headers.has("Content-Type")) headers.set("Content-Type","application/json");
  let res;
  try{res=await fetch(url,{...options,headers,credentials:"same-origin"});}
  catch(_e){throw new Error("Server nicht erreichbar");}
  if(res.status===401){location.href="/login";throw new Error("Anmeldung erforderlich");}
  const type=res.headers.get("content-type")||"";
  const body=type.includes("json")?await res.json():await res.text();
  if(!res.ok) throw new Error(body?.error||body||`HTTP ${res.status}`);
  return body;
}

async function serverFetch(url,options={}){
  return fetch(url,{...options,credentials:"same-origin"});
}

function esc(s){ return String(s ?? "").replace(/[&<>"']/g, m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m])); }
function isoToday(){ const d=new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`; }
function formatDateValue(value){
  if(!value) return "Datum wählen";
  const m=String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : value;
}
function syncDateShell(input){
  const shell=input?.closest?.(".date-shell");
  const display=shell?.querySelector(".date-display");
  if(display) display.textContent=formatDateValue(input.value);
}
function syncAllDateShells(){ qsa('.date-shell input[type="date"]').forEach(syncDateShell); }
function openNativeDatePicker(input){
  if(!input || input.disabled) return;
  try{
    if(typeof input.showPicker==="function"){
      input.showPicker();
      return;
    }
  }catch(_e){}
  try{ input.focus({preventScroll:true}); }catch(_e){ try{ input.focus(); }catch(__e){} }
  try{ input.click(); }catch(_e){}
}
function ensureDatePickerButton(input){
  const shell=input?.closest?.(".date-shell");
  if(!shell || shell.querySelector(".date-picker-btn")) return;
  shell.classList.add("has-picker-button");
  const button=document.createElement("button");
  button.type="button";
  button.className="date-picker-btn";
  button.setAttribute("aria-label","Datum auswählen");
  button.setAttribute("title","Datum auswählen");
  button.addEventListener("click",event=>{
    event.preventDefault();
    event.stopPropagation();
    openNativeDatePicker(input);
  });
  shell.appendChild(button);
}
function upgradeDateInputs(){
  qsa('input[type="date"].input').forEach(input=>{
    if(input.closest(".date-shell")){
      syncDateShell(input);
      ensureDatePickerButton(input);
      return;
    }
    const shell=document.createElement("div");
    shell.className="date-shell";
    input.parentNode.insertBefore(shell,input);
    shell.appendChild(input);
    input.classList.add("date-native");
    const display=document.createElement("span");
    display.className="date-display";
    shell.appendChild(display);
    input.addEventListener("input",()=>syncDateShell(input));
    input.addEventListener("change",()=>syncDateShell(input));
    ensureDatePickerButton(input);
    syncDateShell(input);
  });
}
function formatTimeValue(value){
  return /^\d{2}:\d{2}$/.test(String(value||"")) ? String(value) : "--:--";
}
function timingPrefixFromInput(input){
  return String(input?.id||"").replace(/(?:Start|End)Time$/,"");
}
function syncTimeShell(input){
  const shell=input?.closest?.(".time-shell");
  const display=shell?.querySelector(".time-display");
  if(display) display.textContent=formatTimeValue(input.value);
  const prefix=timingPrefixFromInput(input);
  if(prefix) syncOvernight(prefix);
}
function upgradeTimeInputs(){
  qsa('input[type="time"].time-input').forEach(input=>{
    if(input.closest(".time-shell")){ syncTimeShell(input); return; }
    const shell=document.createElement("div");
    shell.className="time-shell";
    input.parentNode.insertBefore(shell,input);
    shell.appendChild(input);
    input.classList.add("time-native");
    const display=document.createElement("span");
    display.className="time-display";
    shell.appendChild(display);
    input.addEventListener("input",()=>syncTimeShell(input));
    input.addEventListener("change",()=>syncTimeShell(input));
    syncTimeShell(input);
  });
}
function monthShort(d){ return d.toLocaleDateString("de-CH",{month:"short"}).replace(".",""); }
function weekdayShort(d){ return d.toLocaleDateString("de-CH",{weekday:"short"}).replace(".",""); }
function personById(id){ return people.find(p=>Number(p.id)===Number(id)); }
function personColor(name){ return (people.find(p=>p.name===name)||{color:"#ececec"}).color; }
function entryEndDay(e){
  if(!e) return "";
  if(e.end_day) return e.end_day;
  if(Number(e.all_day)===0 && e.start_time && e.end_time && e.end_time < e.start_time) return addDaysIso(e.day,1);
  return e.day || "";
}
function entrySpansDays(e){
  return Boolean(e && Number(e.all_day)===0 && entryEndDay(e) && entryEndDay(e)>e.day);
}
function entryTimeLabel(e){
  if(!e || Number(e.all_day)!==0) return "";
  if(!e.start_time || !e.end_time) return "";
  const endDay=entryEndDay(e);
  if(!endDay || endDay===e.day) return `${e.start_time}–${e.end_time}`;
  return `${e.start_time} → ${formatDateValue(endDay)} ${e.end_time}`;
}
function syncOvernight(prefix){
  const range=qs(`#${prefix}TimeRange`);
  const start=qs(`#${prefix}StartTime`)?.value || "";
  const end=qs(`#${prefix}EndTime`)?.value || "";
  const startDay=qs(`#${prefix}Date`)?.value || "";
  const endDate=qs(`#${prefix}EndDate`);
  if(endDate){
    if(startDay){
      endDate.min=startDay;
      if(!endDate.value || endDate.value<startDay){ endDate.value=startDay; syncDateShell(endDate); }
    }
    const spans=Boolean(startDay && endDate.value && endDate.value>startDay);
    range?.classList.toggle("overnight",false);
    range?.classList.toggle("spans-days",spans);
    const hint=qs(`#${prefix}RangeHint`);
    if(hint){
      hint.hidden=!spans;
      hint.textContent=spans?`Mehrtägig: ${formatDateValue(startDay)} bis ${formatDateValue(endDate.value)}`:"";
    }
    return;
  }
  range?.classList.toggle("overnight",Boolean(start && end && end < start));
}
function autoAdvanceEndDate(prefix){
  const startDay=qs(`#${prefix}Date`)?.value || "";
  const endDate=qs(`#${prefix}EndDate`);
  const start=qs(`#${prefix}StartTime`)?.value || "";
  const end=qs(`#${prefix}EndTime`)?.value || "";
  if(!startDay || !endDate || !start || !end) return;
  if((!endDate.value || endDate.value===startDay) && end<start){
    endDate.value=addDaysIso(startDay,1);
    syncDateShell(endDate);
  }
  syncOvernight(prefix);
}
function syncTiming(prefix){
  const allDay=qs(`#${prefix}AllDay`);
  const range=qs(`#${prefix}TimeRange`);
  if(!allDay || !range) return;
  range.hidden=allDay.checked;
  const dateLabel=qs(`#${prefix}DateLabel`);
  if(dateLabel) dateLabel.textContent=allDay.checked?"Datum":"Von · Datum";
  const endDate=qs(`#${prefix}EndDate`);
  if(!allDay.checked && endDate){
    const startDay=qs(`#${prefix}Date`)?.value || "";
    if(startDay && (!endDate.value || endDate.value<startDay)){ endDate.value=startDay; syncDateShell(endDate); }
  }
  const hint=qs(`#${prefix}RangeHint`);
  if(hint && allDay.checked) hint.hidden=true;
  syncOvernight(prefix);
}
function setTiming(prefix, entry=null){
  const allDay=qs(`#${prefix}AllDay`);
  const start=qs(`#${prefix}StartTime`);
  const end=qs(`#${prefix}EndTime`);
  if(!allDay || !start || !end) return;
  allDay.checked=entry ? Number(entry.all_day)!==0 : true;
  start.value=entry?.start_time || "";
  end.value=entry?.end_time || "";
  const endDate=qs(`#${prefix}EndDate`);
  if(endDate){
    endDate.value=entry ? entryEndDay(entry) : (qs(`#${prefix}Date`)?.value || "");
    syncDateShell(endDate);
  }
  syncTimeShell(start);
  syncTimeShell(end);
  syncTiming(prefix);
}
function timingPayload(prefix){
  const allDay=qs(`#${prefix}AllDay`)?.checked ?? true;
  const payload={
    all_day:allDay,
    start_time:allDay ? "" : (qs(`#${prefix}StartTime`)?.value || ""),
    end_time:allDay ? "" : (qs(`#${prefix}EndTime`)?.value || ""),
  };
  const endDate=qs(`#${prefix}EndDate`);
  if(!allDay && endDate) payload.end_day=endDate.value || "";
  return payload;
}

async function loadPeople(){
  people=await api("/api/companies");
  for(const mode of ["quick","modal","batch"]){
    const st=scopeState(mode); st.ids=normalizeCompanyIds(st.ids);
    if(!st.all && !st.ids.length) st.all=true;
    renderScopeSummary(mode);
  }
  renderPersonFilter();
  renderPeopleSettings();
  renderYearCompanyFilter();
}
async function loadEntries(){
  entries = await api("/api/entries");
  renderAll();
}
async function loadPeriods(){
  periods = await api("/api/periods");
  renderPeriodSettings();
  renderYear();
  updateDateContext("quick");
  updateDateContext("modal");
}
async function loadCalendarSubscriptions(){
  calendarSubscriptions = await api("/api/calendar-subscriptions");
  renderCalendarSubscriptions();
}

function renderPersonButtons(target,mode){ /* Stiftungskalender: Termin gilt per Unternehmens-Multiselect. */ }
function renderBatchPersonSelect(){ /* ersetzt durch Unternehmens-Multiselect */ }
function selectPerson(mode,id){ /* Legacy-Hook; im Stiftungskalender nicht mehr verwendet. */ }

function yearOptions(select){
  const now = new Date().getFullYear();
  const years = [];
  for(let y=now-1;y<=now+2;y++) years.push(y);
  select.innerHTML=years.map(y=>`<option ${y===now?"selected":""}>${y}</option>`).join("");
}
function renderPersonFilter(){
  if(exportPeopleAllSelected||!exportPeople.length){
    exportPeople=people.map(p=>p.name);exportPeopleAllSelected=true;
  }else{
    exportPeople=exportPeople.filter(name=>people.some(p=>p.name===name));
    if(!exportPeople.length){exportPeople=people.map(p=>p.name);exportPeopleAllSelected=true;}
  }
  updateExportControls();
}
function renderYearCompanyFilter(){
  const select=qs("#yearCompanyFilter"); if(!select)return;
  const current=select.value;
  select.innerHTML='<option value="">Alle Unternehmen</option>'+people.map(p=>`<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
  if([...select.options].some(o=>o.value===current))select.value=current;
}
const PAGE_KEY = "stiftungskalender-current-page";
function rememberPage(name){try{sessionStorage.setItem(PAGE_KEY,name);}catch(_e){}}
function rememberedPage(){try{return sessionStorage.getItem(PAGE_KEY)||"home";}catch(_e){return "home";}}
function showPage(name,{persist=true,scroll=true}={}){
  const page=qs("#"+name+"Page");
  if(!page) name="home";
  qsa(".page").forEach(p=>p.classList.remove("active"));
  qs("#"+name+"Page")?.classList.add("active");
  qsa(".navbtn").forEach(b=>b.classList.toggle("active",b.dataset.page===name));
  if(persist) rememberPage(name);
  if(name==="list") renderList();
  if(name==="year"){ renderYear(); renderStatsByPerson(); }
  if(name==="settings"){ loadConfig(); renderPeriodSettings(); }
  if(name==="companies") renderPeopleSettings();
  if(scroll) window.scrollTo({top:0,behavior:"smooth"});
}
function renderNext(){
  const today=isoToday();
  const end=addDaysIso(today,6);
  const rows=[];
  for(const e of entries){
    if(e.day>=today && e.day<=end) rows.push({day:e.day,kind:1,html:itemHtml(e)});
    for(const day of entryContinuationDays(e)){
      if(day>=today && day<=end) rows.push({day,kind:0,html:continuationItemHtml(e,day)});
    }
  }
  rows.sort((a,b)=>a.day.localeCompare(b.day)||a.kind-b.kind);
  qs("#nextList").innerHTML = rows.length ? rows.map(r=>r.html).join("") : '<div class="small">Keine kommenden Einträge.</div>';
}
function itemHtml(e){
  const d=new Date(e.day+"T12:00:00");
  const scope=e.scope_label||"Alle Unternehmen";
  return `<div class="item" data-open-entry="${e.id}">
    <div class="datebox"><b>${d.getDate()}</b><span>${monthShort(d)}</span></div>
    <div><div class="who"><span class="dot" style="background:${esc(e.color)}"></span>${esc(e.title||e.person||"Termin")}</div>
    <div class="note">${weekdayShort(d)}${entryTimeLabel(e)?" · "+esc(entryTimeLabel(e)):""} · ${esc(scope)}${e.note?" · "+esc(e.note):""}</div></div>
    <button class="kebab" aria-label="Termin öffnen">›</button>
  </div>`;
}
function addDaysIso(day, amount=1){
  const d=new Date(day+"T12:00:00");
  d.setDate(d.getDate()+amount);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}
function entryContinuationDays(e){
  if(!entrySpansDays(e)) return [];
  const days=[];
  const finalDay=entryEndDay(e);
  let current=addDaysIso(e.day,1);
  while(current<=finalDay && days.length<3662){
    days.push(current);
    current=addDaysIso(current,1);
  }
  return days;
}
function continuationItemHtml(e,day){
  const d=new Date(day+"T12:00:00");
  const from=formatIsoDate(e.day);const finalDay=entryEndDay(e);
  const continuationText=day===finalDay?`bis ${e.end_time}`:"ganzer Tag";
  return `<div class="item continuation-item" data-open-entry="${e.id}">
    <div class="datebox continuation-date"><b>${d.getDate()}</b><span>${monthShort(d)}</span></div>
    <div><div class="who"><span class="dot" style="background:${esc(e.color)}"></span>${esc(e.title||e.person||"Termin")} <span class="continuation-badge">Fortsetzung</span></div>
    <div class="note">${weekdayShort(d)} · ${esc(continuationText)} · ${esc(e.scope_label||"Alle Unternehmen")} · vom ${esc(from)}${e.note?" · "+esc(e.note):""}</div></div>
    <button class="kebab" aria-label="Ursprünglichen Termin öffnen">›</button>
  </div>`;
}
function exportPeopleAreAll(){
  return people.length>0 && exportPeople.length===people.length && people.every(p=>exportPeople.includes(p.name));
}
function exportParams(){
  const params=new URLSearchParams();
  const year=qs("#filterYear").value;const search=qs("#filterSearch").value.toLowerCase().trim();
  if(year)params.set("year",year);if(search)params.set("q",search);
  if(!exportPeopleAreAll())exportPeople.forEach(name=>params.append("company",name));
  return params;
}

function calendarRangeParams(){
  const params=new URLSearchParams();
  const from=qs("#listRangeFrom")?.value||"";const to=qs("#listRangeTo")?.value||"";const search=qs("#filterSearch").value.toLowerCase().trim();
  if(from)params.set("from",from);if(to)params.set("to",to);if(search)params.set("q",search);
  if(!exportPeopleAreAll())exportPeople.forEach(name=>params.append("company",name));
  return params;
}
function setCalendarRangeForYear(year){
  if(!year) return;
  const from=qs("#listRangeFrom"), to=qs("#listRangeTo");
  if(from){from.value=`${year}-01-01`;syncDateShell(from);}
  if(to){to.value=`${year}-12-31`;syncDateShell(to);}
}
function updateExportControls(){
  const params=exportParams();
  qs("#listCsvLink").href=`/export.csv?${params.toString()}`;
  qs("#listPdfButton").dataset.url=`/export.pdf?${params.toString()}`;
  const rangeQuery=calendarRangeParams().toString();
  const icsButton=qs("#listIcsButton");if(icsButton)icsButton.dataset.url=`/export.ics?${rangeQuery}`;
  const button=qs("#listExportPeople");if(button){
    if(exportPeopleAreAll())button.textContent="Unternehmen: Alle";
    else if(exportPeople.length===1)button.textContent=`Unternehmen: ${exportPeople[0]}`;
    else button.textContent=`Unternehmen: ${exportPeople.length}`;
  }
}
function openExportPeopleModal(){
  const grid=qs("#exportPeopleGrid");
  grid.innerHTML=people.map(p=>`<label class="export-person-option"><input type="checkbox" value="${esc(p.name)}" ${exportPeople.includes(p.name)?"checked":""}><span class="dot" style="background:${esc(p.color)}"></span><span>${esc(p.name)}</span></label>`).join("");
  qs("#exportPeopleModalBack").classList.add("open");
  document.body.classList.add("modal-open");
}
function closeExportPeopleModal(){
  qs("#exportPeopleModalBack").classList.remove("open");
  if(!qs("#modalBack")?.classList.contains("open")&&!qs("#batchModalBack")?.classList.contains("open")) document.body.classList.remove("modal-open");
}
function setExportPeopleChecks(mode){
  const boxes=qsa('#exportPeopleGrid input[type="checkbox"]');
  if(mode==="all") boxes.forEach(b=>b.checked=true);
}
function applyExportPeopleSelection(){
  const chosen=qsa('#exportPeopleGrid input[type="checkbox"]:checked').map(b=>b.value);
  if(!chosen.length)return toast("Mindestens ein Unternehmen wählen");
  exportPeople=chosen;exportPeopleAllSelected=exportPeopleAreAll();closeExportPeopleModal();renderList();
  toast(exportPeopleAreAll()?"Alle Unternehmen ausgewählt":chosen.length===1?chosen[0]:`${chosen.length} Unternehmen ausgewählt`);
}
function yearMonthsAreAll(){
  return yearMonths.length===12 && Array.from({length:12},(_,i)=>i+1).every(m=>yearMonths.includes(m));
}
function updateYearMonthButton(){
  const button=qs("#yearMonthSelect");
  if(!button) return;
  if(yearMonthsAreAll()) button.textContent="Monate: Alle";
  else if(yearMonths.length===1) button.textContent=`Monat: ${YEAR_MONTH_NAMES[yearMonths[0]-1]}`;
  else button.textContent=`Monate: ${yearMonths.length}`;
}
function openYearMonthsModal(){
  const grid=qs("#yearMonthsGrid");
  grid.innerHTML=YEAR_MONTH_NAMES.map((name,i)=>`<label class="export-person-option month-option"><input type="checkbox" value="${i+1}" ${yearMonths.includes(i+1)?"checked":""}><span>${esc(name)}</span></label>`).join("");
  qs("#yearMonthsModalBack").classList.add("open");
  document.body.classList.add("modal-open");
}
function closeYearMonthsModal(){
  qs("#yearMonthsModalBack").classList.remove("open");
  if(!qs("#modalBack")?.classList.contains("open")&&!qs("#batchModalBack")?.classList.contains("open")&&!qs("#exportPeopleModalBack")?.classList.contains("open")) document.body.classList.remove("modal-open");
}
function setYearMonthChecks(mode){
  const boxes=qsa('#yearMonthsGrid input[type="checkbox"]');
  if(mode==="all") boxes.forEach(b=>b.checked=true);
}
function applyYearMonthSelection(){
  const chosen=qsa('#yearMonthsGrid input[type="checkbox"]:checked').map(b=>Number(b.value)).sort((a,b)=>a-b);
  if(!chosen.length) return toast("Mindestens einen Monat wählen");
  yearMonths=chosen;
  closeYearMonthsModal();
  updateYearMonthButton();
  renderYear();
  toast(yearMonthsAreAll()?"Alle Monate ausgewählt":chosen.length===1?YEAR_MONTH_NAMES[chosen[0]-1]:`${chosen.length} Monate ausgewählt`);
}
function yearMonthQuery(){
  const params=new URLSearchParams();
  if(!yearMonthsAreAll()) yearMonths.forEach(m=>params.append("month",String(m)));
  return params;
}
function renderList(){
  const year=qs("#filterYear").value;const search=qs("#filterSearch").value.toLowerCase().trim();
  const today=isoToday();const currentYear=today.slice(0,4);const hidePast=year===currentYear;
  let source=entries.slice();
  if(!exportPeopleAreAll())source=source.filter(e=>eventMatchesCompanySelection(e,exportPeople));
  if(search)source=source.filter(e=>(e.note||"").toLowerCase().includes(search)||(e.title||e.person||"").toLowerCase().includes(search)||(e.scope_label||"").toLowerCase().includes(search));
  const rows=[];
  for(const e of source){
    if(e.day.startsWith(year)&&(!hidePast||e.day>=today))rows.push({day:e.day,kind:1,html:itemHtml(e)});
    for(const continuationDay of entryContinuationDays(e))if(continuationDay.startsWith(year)&&(!hidePast||continuationDay>=today))rows.push({day:continuationDay,kind:0,html:continuationItemHtml(e,continuationDay)});
  }
  rows.sort((a,b)=>a.day.localeCompare(b.day)||a.kind-b.kind);
  qs("#fullList").innerHTML=rows.length?rows.map(r=>r.html).join(""):`<div class="small">${hidePast?"Keine Termine ab heute.":"Keine Termine."}</div>`;
  updateExportControls();
  const exportLabel=exportPeopleAreAll()?"alle Unternehmen":exportPeople.length===1?exportPeople[0]:`${exportPeople.length} Unternehmen`;
  qs("#listExportHint").textContent=`${year} · ${exportLabel}`;
}
function renderStats(){
  const today=isoToday();const year=String(new Date().getFullYear());
  qs("#statUpcoming").textContent=entries.filter(e=>entryEndDay(e)>=today).length;
  qs("#statPeople").textContent=people.length;
  qs("#statYear").textContent=entries.filter(e=>e.day.startsWith(year)).length;
}
function periodsForDay(day){
  if(!day) return [];
  return periods.filter(p=>p.start_day<=day && p.end_day>=day);
}
function updateDateContext(prefix){
  const input=qs(`#${prefix}Date`);
  const box=qs(`#${prefix}DateContext`);
  if(!input || !box) return;
  const matches=periodsForDay(input.value);
  if(!matches.length){box.innerHTML="";box.classList.remove("visible");return;}
  box.innerHTML=matches.map(p=>`<div class="date-context-item"><i class="bar-swatch" style="background:${esc(p.color)}"></i><span><b>${esc(periodKindName(p.kind))}:</b> ${esc(p.label)}</span></div>`).join("");
  box.classList.add("visible");
}
function periodKindName(kind){
  return kind==="vacation" ? "Ferien" : kind==="holiday" ? "Feiertag" : "Zeitraum";
}
function periodMapForYear(year){
  const map=new Map();
  const first=new Date(year,0,1,12);
  const last=new Date(year,11,31,12);
  for(const p of periods){
    let start=new Date(p.start_day+"T12:00:00");
    let end=new Date(p.end_day+"T12:00:00");
    if(end<first || start>last) continue;
    if(start<first) start=new Date(first);
    if(end>last) end=new Date(last);
    for(let d=new Date(start);d<=end;d.setDate(d.getDate()+1)){
      const iso=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
      if(!map.has(iso)) map.set(iso,[]);
      map.get(iso).push(p);
    }
  }
  return map;
}
function renderYear(){
  const year=Number(qs("#yearSelect").value);const company=qs("#yearCompanyFilter")?.value||"";
  const visibleMonths=yearMonths.map(m=>m-1);
  const filtered=entries.filter(e=>eventMatchesCompany(e,company));
  const byDay=new Map();
  for(const e of filtered){if(e.day.startsWith(String(year))){if(!byDay.has(e.day))byDay.set(e.day,[]);byDay.get(e.day).push(e);}}
  const continuationByDay=new Map();
  for(const e of filtered)for(const continuationDay of entryContinuationDays(e))if(continuationDay.startsWith(String(year))){if(!continuationByDay.has(continuationDay))continuationByDay.set(continuationDay,[]);continuationByDay.get(continuationDay).push(e);}
  const marks=periodMapForYear(year);const tableClass=visibleMonths.length===1?"year month-view":"year";
  let out=`<table class="${tableClass}"><colgroup><col class="day-col">${visibleMonths.map(()=>'<col class="month-col">').join("")}<col class="day-col"></colgroup><thead><tr><th>Tag</th>${visibleMonths.map(i=>`<th>${YEAR_MONTH_NAMES[i]}</th>`).join("")}<th>Tag</th></tr></thead><tbody>`;
  for(let day=1;day<=31;day++){
    out+=`<tr><td>${day}</td>`;
    for(const month of visibleMonths){
      const d=new Date(year,month,day,12);const valid=d.getFullYear()===year&&d.getMonth()===month&&d.getDate()===day;
      if(!valid){out+='<td class="invalid"></td>';continue;}
      const iso=`${year}-${String(month+1).padStart(2,"0")}-${String(day).padStart(2,"0")}`;
      const direct=byDay.get(iso)||[];const cont=continuationByDay.get(iso)||[];const dayMarks=marks.get(iso)||[];const weekend=d.getDay()===0||d.getDay()===6;
      const cards=[];
      for(const e of direct)cards.push({e,label:e.title||e.person||"Termin",continuation:false});
      for(const e of cont){const final=iso===entryEndDay(e);cards.push({e,label:`${e.title||e.person||"Termin"}${final&&e.end_time?` · bis ${e.end_time}`:""}`,continuation:true});}
      const visible=cards.slice(0,3);
      const stack=visible.map(x=>`<button type="button" class="year-event-pill ${x.continuation?"continuation":""}" style="background:${esc(x.e.color)}" data-open-entry="${x.e.id}" title="${esc(x.label)} · ${esc(x.e.scope_label||"Alle Unternehmen")}"><span>${esc(x.label)}</span></button>`).join("");
      const more=cards.length>3?`<span class="year-more">+${cards.length-3}</span>`:"";
      const markTitle=dayMarks.map(p=>`${periodKindName(p.kind)}: ${p.label}`).join(" · ");
      const rail=dayMarks.length?`<div class="period-rail" title="${esc(markTitle)}">${dayMarks.map(p=>`<span class="period-segment" style="background:${esc(p.color)}"></span>`).join("")}</div>`:"";
      const cellClass=[weekend?"weekend":"",dayMarks.length?"has-period":"",cards.length?"has-entry":""].filter(Boolean).join(" ");
      out+=`<td class="${cellClass}" ${cards.length?"":`data-prefill-date="${iso}"`}><div class="year-cell-content"><div class="year-event-stack">${stack}${more}</div>${rail}</div></td>`;
    }
    out+=`<td class="day-repeat">${day}</td></tr>`;
  }
  out+='</tbody></table>';qs("#yearTableWrap").innerHTML=out;
  const periodLegend=[];const seen=new Set();
  for(const p of periods.filter(p=>p.start_day<=`${year}-12-31`&&p.end_day>=`${year}-01-01`)){const key=`${p.kind}|${p.color}`;if(seen.has(key))continue;seen.add(key);periodLegend.push(`<span class="period-legend"><i class="bar-swatch" style="background:${esc(p.color)}"></i>${periodKindName(p.kind)}</span>`);}
  const companyLegend=company?`<span><i class="dot" style="background:${esc(people.find(p=>p.name===company)?.color||"#dcebe7")}"></i>${esc(company)} + globale Termine</span>`:`<span><i class="dot" style="background:#dcebe7"></i>Alle Unternehmen</span><span>Unternehmensbezogene Termine</span>`;
  qs("#legend").innerHTML='<span class="legend-title">Legende:</span>'+companyLegend+`<span><i class="dot" style="background:var(--weekend)"></i>Wochenende</span>`+periodLegend.join("");
  const params=yearMonthQuery();if(company)params.append("company",company);const query=params.toString();
  qs("#csvLink").href=`/export.csv?year=${year}${query?`&${query}`:""}`;updateYearMonthButton();
}

async function renderStatsByPerson(){
  const year=qs("#yearSelect").value;const data=await api(`/api/stats?year=${encodeURIComponent(year)}`);const rows=data.per_company||data.per_person||[];
  qs("#personStats").innerHTML=rows.length?rows.map(x=>{const base=`year=${encodeURIComponent(year)}&company=${encodeURIComponent(x.name)}`;return `<span class="stat-chip"><i class="dot" style="background:${esc(x.color)}"></i>${esc(x.name)}: ${x.count} <a class="stat-chip-link server-export" href="/export.csv?${base}" title="CSV exportieren">CSV</a> <button class="stat-chip-link pdf-share-button server-export" type="button" data-url="/export.pdf?${base}" title="PDF teilen oder drucken">PDF</button></span>`;}).join(""):'<span class="small">Noch keine Termine.</span>';
  
}
function renderAll(){ renderStats(); renderNext(); renderList(); renderYear(); renderStatsByPerson(); }

function openModal(id=null){
  editingId=id;const e=id?entries.find(x=>Number(x.id)===Number(id)):null;
  qs("#modalTitle").textContent=e?"Termin bearbeiten":"Termin eintragen";
  qs("#modalEventTitle").value=e?(e.title||e.person||""):"";
  qs("#modalDate").value=e?e.day:(qs("#quickDate").value||isoToday());syncDateShell(qs("#modalDate"));updateDateContext("modal");
  qs("#modalNote").value=e?e.note:"";setTiming("modal",e);setScopeFromEntry("modal",e);
  qs("#modalDuplicate").style.display=e?"block":"none";qs("#modalShare").style.display=e?"block":"none";qs("#modalDelete").style.display=e?"block":"none";
  qs("#modalBack").classList.add("open");document.body.classList.add("modal-open");
}
function syncBodyModalState(){
  document.body.classList.toggle("modal-open",Boolean(qs(".modalback.open")));
}
function closeModal(){
  qs("#modalBack")?.classList.remove("open");
  editingId=null;
  syncBodyModalState();
}
function prefillDate(day){openModal();qs("#modalDate").value=day;syncDateShell(qs("#modalDate"));updateDateContext("modal");}

function formatIsoDate(day){
  if(!day) return "";
  const d=new Date(day+"T12:00:00");
  return d.toLocaleDateString("de-CH",{day:"2-digit",month:"2-digit",year:"numeric"});
}
function resetBatchPreview(){
  batchPreviewKey=null;
  const box=qs("#batchPreviewBox");
  if(box){ box.hidden=true; box.innerHTML=""; }
  const create=qs("#batchCreate");
  if(create) create.disabled=true;
}
function batchPayload(){return {title:qs("#batchTitle").value.trim(),weekday:Number(qs("#batchWeekday").value),start_day:qs("#batchStart").value,end_day:qs("#batchEnd").value,note:qs("#batchNote").value.trim(),skip_vacations:qs("#batchSkipVacations")?.checked||false,skip_holidays:qs("#batchSkipHolidays")?.checked||false,...scopePayload("batch"),...timingPayload("batch")};}
function openBatchModal(){
  const now=new Date();qs("#batchTitle").value="";setScopeState("batch",{all:true,ids:[]});
  qs("#batchStart").value=isoToday();qs("#batchEnd").value=`${now.getFullYear()}-12-31`;syncDateShell(qs("#batchStart"));syncDateShell(qs("#batchEnd"));qs("#batchWeekday").value=String((now.getDay()+6)%7);qs("#batchNote").value="";
  if(qs("#batchSkipVacations"))qs("#batchSkipVacations").checked=false;if(qs("#batchSkipHolidays"))qs("#batchSkipHolidays").checked=false;setTiming("batch");resetBatchPreview();qs("#batchModalBack").classList.add("open");document.body.classList.add("modal-open");
}
function closeBatchModal(){
  qs("#batchModalBack").classList.remove("open");
  if(!qs("#modalBack")?.classList.contains("open")) document.body.classList.remove("modal-open");
  resetBatchPreview();
}
async function previewBatch(){
  const payload=batchPayload();if(!payload.title)return toast("Titel eingeben");if(!payload.applies_to_all&&!payload.company_ids.length)return toast("Unternehmen auswählen");if(!payload.start_day||!payload.end_day)return toast("Von und Bis wählen");
  try{const data=await api("/api/entries/batch/preview",{method:"POST",body:JSON.stringify(payload)});const weekday=qs("#batchWeekday").selectedOptions[0]?.textContent||"Wochentag";const skipped=[];if(data.vacation_count)skipped.push(`${data.vacation_count} Ferien`);if(data.holiday_count)skipped.push(`${data.holiday_count} Feiertag${data.holiday_count===1?"":"e"}`);qs("#batchPreviewBox").innerHTML=`<b>${data.matched_count} ${esc(weekday)}</b><span>${data.create_count} Termine${skipped.length?" · "+skipped.join(" · "):""}</span>`;qs("#batchPreviewBox").hidden=false;batchPreviewKey=JSON.stringify(payload);qs("#batchCreate").disabled=data.create_count===0;}catch(e){resetBatchPreview();toast(e.message);}
}
async function createBatch(){
  const payload=batchPayload();
  if(batchPreviewKey!==JSON.stringify(payload)){
    resetBatchPreview();
    return toast("Bitte Vorschau nochmals berechnen");
  }
  try{
    const data=await api("/api/entries/batch",{method:"POST",body:JSON.stringify(payload)});
    closeBatchModal();
    await loadEntries();
    toast(`${data.created_count} Einträge erstellt · ${data.skipped_count} übersprungen`);
  }catch(e){toast(e.message);}
}

function toast(msg){
  const t=qs("#toast");t.textContent=msg;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),1800);
}
async function saveEntry(day,title,note,timing,mode,id=null){
  const clean=String(title||"").trim();if(!clean)throw new Error("Titel eingeben");const scope=scopePayload(mode);if(!scope.applies_to_all&&!scope.company_ids.length)throw new Error("Unternehmen auswählen");
  const payload={day,title:clean,note,...timing,...scope};if(id)await api(`/api/entries/${id}`,{method:"PUT",body:JSON.stringify(payload)});else await api("/api/entries",{method:"POST",body:JSON.stringify(payload)});await loadEntries();
}

function renderPeopleSettings(){
  const box=qs("#peopleSettings");if(!box)return;
  box.innerHTML=people.length?people.map(p=>`<div class="person-compact-card"><button class="person-compact-main" type="button" data-person-edit="${p.id}" aria-label="${esc(p.name)} bearbeiten"><span class="person-color-dot" style="background:${esc(p.color)}"></span><span class="person-compact-text"><strong>${esc(p.name)}</strong><small>Unternehmenskalender verfügbar</small></span><span class="person-edit-mark">Bearbeiten</span></button><button class="person-delete-btn" type="button" data-person-delete="${p.id}" aria-label="${esc(p.name)} löschen">×</button></div>`).join(""):'<div class="small">Noch keine Unternehmen.</div>';
}
function openPersonEditor(id){const p=people.find(x=>Number(x.id)===Number(id));if(!p)return;qs("#personEditId").value=p.id;qs("#personEditName").value=p.name||"";qs("#personEditColor").value=p.color||"#ececec";if(qs("#personEditIcalTitle"))qs("#personEditIcalTitle").value="";qs("#personEditorBack").classList.add("open");document.body.classList.add("modal-open");}
function closePersonEditor(){
  qs("#personEditorBack")?.classList.remove("open");
  syncBodyModalState();
}
async function savePersonEditor(){const id=Number(qs("#personEditId").value);const name=qs("#personEditName").value.trim();if(!name)return toast("Name fehlt");try{await api(`/api/companies/${id}`,{method:"PUT",body:JSON.stringify({name,color:qs("#personEditColor").value})});closePersonEditor();await loadPeople();await loadEntries();toast("Unternehmen aktualisiert");}catch(e){toast(e.message);}}
async function deletePerson(id){if(!confirm("Unternehmen wirklich löschen? Zugeordnete Termine werden nicht gelöscht; sie bleiben als globale bzw. andere Zuordnungen erhalten."))return;try{await api(`/api/companies/${id}`,{method:"DELETE"});await loadPeople();await loadEntries();toast("Unternehmen gelöscht");}catch(e){toast(e.message);}}
function renderPeriodSettings(){
  const box=qs("#periodList");
  if(!box) return;
  const listedPeriods=periods.filter(p=>!String(p.source||"").startsWith("subscription:"));
  box.innerHTML=listedPeriods.length?listedPeriods.map(p=>{
    const same=p.start_day===p.end_day;
    const range=same?p.start_day:`${p.start_day} - ${p.end_day}`;
    return `<div class="period-item"><span class="bar-swatch large" style="background:${esc(p.color)}"></span><div><b>${esc(p.label)}</b><div class="small">${periodKindName(p.kind)} · ${esc(range)}${p.source==="ics"?" · ICS":""}</div></div><button class="mini danger" onclick="deletePeriod(${p.id})">×</button></div>`;
  }).join(""):'<div class="small">Noch keine Ferien oder Feiertage erfasst.</div>';
  
}
async function addPeriod(){
  const start=qs("#periodStart").value;
  const end=qs("#periodEnd").value;
  const kind=qs("#periodKind").value;
  const label=qs("#periodLabel").value.trim();
  const color=qs("#periodColor").value;
  if(!start||!end) return toast("Von und Bis wählen");
  try{
    await api("/api/periods",{method:"POST",body:JSON.stringify({start_day:start,end_day:end,kind,label,color})});
    qs("#periodLabel").value="";
    await loadPeriods();
    toast("Zeitraum gespeichert");
  }catch(e){toast(e.message);}
}
async function deletePeriod(id){
  if(!confirm("Zeitraum wirklich löschen?")) return;
  try{await api(`/api/periods/${id}`,{method:"DELETE"});await loadPeriods();toast("Zeitraum gelöscht");}
  catch(e){toast(e.message);}
}

function webcalUrl(url){
  return String(url||"").replace(/^https?:/i,"webcal:");
}
async function copyCalendarUrl(url, label="Kalenderlink"){
  try{
    await navigator.clipboard.writeText(url);
    toast(`${label} kopiert`);
  }catch(_e){
    toast("Link markieren und kopieren");
  }
}
function renderCalendarSubscriptions(){
  const box=qs("#subscriptionList"); if(!box) return;
  box.innerHTML=calendarSubscriptions.length?calendarSubscriptions.map(sub=>{
    const ok=(sub.last_status||"").startsWith("OK");
    return `<div class="subscription-item">
      <div class="subscription-item-head"><i class="bar-swatch large" style="background:${esc(sub.color)}"></i><div class="spacer"><b>${esc(sub.name)}</b><div class="subscription-url">${esc(sub.url)}</div></div><label class="toggle-row"><input class="sub-enabled" type="checkbox" data-id="${sub.id}" ${Number(sub.enabled)?"checked":""}> aktiv</label></div>
      <div class="subscription-status ${ok?"":"danger"}">${esc(sub.last_status||"Noch nicht synchronisiert")}${sub.last_sync_at?` · ${esc(new Date(sub.last_sync_at).toLocaleString("de-CH"))}`:""}</div>
      <div class="subscription-item-actions"><button class="secondary sub-sync" data-id="${sub.id}" type="button">Jetzt aktualisieren</button><button class="secondary danger sub-delete" data-id="${sub.id}" type="button">Löschen</button></div>
    </div>`;
  }).join(""):'<div class="small">Noch keine Kalender-Abos eingerichtet.</div>';
  qsa(".sub-sync").forEach(btn=>btn.addEventListener("click",()=>syncSubscription(Number(btn.dataset.id))));
  qsa(".sub-delete").forEach(btn=>btn.addEventListener("click",()=>deleteSubscription(Number(btn.dataset.id))));
  qsa(".sub-enabled").forEach(chk=>chk.addEventListener("change",()=>toggleSubscription(Number(chk.dataset.id),chk.checked)));
  
}
async function addSubscription(){
  const payload={name:qs("#subName").value.trim(),url:qs("#subUrl").value.trim(),kind:qs("#subKind").value,color:qs("#subColor").value};
  if(!payload.name||!payload.url) return toast("Name und Kalender-URL eingeben");
  try{
    const result=await api("/api/calendar-subscriptions",{method:"POST",body:JSON.stringify(payload)});
    qs("#subName").value="";qs("#subUrl").value="";
    await loadCalendarSubscriptions(); await loadPeriods();
    toast(result.warning?`Abo gespeichert · Sync: ${result.warning}`:`Abo gespeichert · ${result.imported||0} Termine`);
  }catch(e){toast(e.message);}
}
async function syncSubscription(id){
  try{const r=await api(`/api/calendar-subscriptions/${id}/sync`,{method:"POST",body:"{}"});await loadCalendarSubscriptions();await loadPeriods();toast(`${r.imported||0} Termine aktualisiert`);}catch(e){await loadCalendarSubscriptions();toast(e.message);}
}
async function toggleSubscription(id,enabled){
  const sub=calendarSubscriptions.find(x=>Number(x.id)===Number(id)); if(!sub) return;
  try{await api(`/api/calendar-subscriptions/${id}`,{method:"PUT",body:JSON.stringify({...sub,enabled})});await loadCalendarSubscriptions();await loadPeriods();toast(enabled?"Abo aktiviert":"Abo deaktiviert");}catch(e){toast(e.message);}
}
async function deleteSubscription(id){
  if(!confirm("Kalender-Abo und seine synchronisierten Markierungen löschen?")) return;
  try{await api(`/api/calendar-subscriptions/${id}`,{method:"DELETE"});await loadCalendarSubscriptions();await loadPeriods();toast("Kalender-Abo gelöscht");}catch(e){toast(e.message);}
}
function toggleQrBox(boxId,imgId,url){
  const box=qs(boxId), img=qs(imgId); if(!box||!img) return;
  const show=box.style.display==="none"||!box.style.display;
  box.style.display=show?"block":"none";
  if(show && !img.src) img.src=url+`?v=${Date.now()}`;
}

function renderPersonIcalFeeds(items=[]){
  const box=qs("#icalPersonList");if(!box)return;if(!items.length){box.innerHTML="";return;}
  box.innerHTML=`<div class="ical-section-label">Pro Unternehmen</div>`+items.map(item=>`<details class="ical-feed"><summary class="ical-feed-head"><span class="dot" style="background:${esc(item.color||"#ececec")}"></span><b>${esc(item.name)}</b></summary><div class="ical-feed-body"><div class="pathbox calendar-url-display">${esc(item.url)}</div><div class="ical-feed-actions"><button class="secondary ical-copy" type="button" data-url="${esc(item.url)}" data-name="${esc(item.name)}">Kopieren</button><a class="secondary link-button" href="${esc(webcalUrl(item.url))}">Abonnieren</a><button class="secondary person-qr" type="button" data-id="${item.id}">QR-Code</button><button class="secondary danger person-revoke" type="button" data-id="${item.id}" data-name="${esc(item.name)}">Freigabe widerrufen</button></div><div id="personQrBox-${item.id}" class="qr-box" style="display:none"><img id="personQrImage-${item.id}" alt="QR-Code ${esc(item.name)}"></div></div></details>`).join("");
  qsa(".ical-copy").forEach(btn=>btn.addEventListener("click",()=>copyCalendarUrl(btn.dataset.url,`${btn.dataset.name}-Link`)));
  qsa(".person-qr").forEach(btn=>btn.addEventListener("click",()=>toggleQrBox(`#personQrBox-${btn.dataset.id}`,`#personQrImage-${btn.dataset.id}`,`/api/companies/${btn.dataset.id}/calendar-qr.png`)));
  qsa(".person-revoke").forEach(btn=>btn.addEventListener("click",async()=>{if(!confirm(`Freigabe für ${btn.dataset.name} widerrufen? Der bisherige Kalender-Link funktioniert danach nicht mehr.`))return;try{await api(`/api/companies/${btn.dataset.id}/calendar-token/reset`,{method:"POST",body:"{}"});await loadConfig();toast("Freigabe widerrufen · neuen Link weitergeben");}catch(e){toast(e.message);}}));
}
async function loadConfig(){
  const c=await api("/api/config");qs("#dataFile").textContent=c.data_file+"  |  Backups: "+c.backup_dir;
  if(c.ical_enabled){directIcalUrl=c.ical_url||"";qs("#icalBox").textContent=c.ical_url;qs("#copyIcal").style.display="";qs("#copyIcal").dataset.url=c.ical_url;qs("#showGlobalQr").style.display="";qs("#showGlobalQr").dataset.url=c.ical_qr_url||"/api/calendar-qr.png";qs("#subscribeIcal").style.display="";qs("#subscribeIcal").href=webcalUrl(c.ical_url);qs("#icalSecurityHint").style.display="";renderPersonIcalFeeds(c.ical_company_urls||[]);}else{directIcalUrl="";qs("#icalBox").textContent="Nicht aktiviert. In Portainer ICAL_TOKEN setzen.";qs("#copyIcal").style.display="none";qs("#showGlobalQr").style.display="none";qs("#globalQrBox").style.display="none";qs("#subscribeIcal").style.display="none";qs("#icalSecurityHint").style.display="none";renderPersonIcalFeeds([]);}loadHistory();
}

function compactHistoryText(value, maxLen=120){
  const text=String(value||"").replace(/<[^>]*>/g," ").replace(/\s+/g," ").trim();
  return text.length>maxLen?text.slice(0,maxLen-1)+"…":text;
}


function historySnapshotPeriod(x){
  if(!x?.day) return "";
  const startDate=formatDateValue(x.day);
  const endDay=x.end_day||x.day;
  if(Number(x.all_day??1)===1){
    return endDay && endDay!==x.day
      ? `${startDate} bis ${formatDateValue(endDay)} · ganzer Tag`
      : `${startDate} · ganzer Tag`;
  }
  const startTime=x.start_time||"";
  const endTime=x.end_time||"";
  const start=`${startDate}${startTime?` ${startTime}`:""}`;
  const end=`${formatDateValue(endDay)}${endTime?` ${endTime}`:""}`;
  return `${start} bis ${end}`;
}

function historyDiffHtml(before,after){
  if(!before||!Object.keys(before).length||!after)return "";const rows=[];const add=(label,oldValue,newValue)=>{const oldText=String(oldValue??"").trim()||"–";const newText=String(newValue??"").trim()||"–";if(oldText===newText)return;rows.push(`<div class="history-diff-row"><span class="history-diff-label">${esc(label)}:</span> <span>${esc(oldText)} <b aria-label="wurde geändert zu">→</b> ${esc(newText)}</span></div>`);};add("Termin",before.title||before.person,after.title||after.person);add("Unternehmen",before.scope_label,after.scope_label);add("Zeitraum",historySnapshotPeriod(before),historySnapshotPeriod(after));add("Beschreibung",before.note,after.note);return rows.length?`<div class="history-diff">${rows.join("")}</div>`:"";
}


async function loadHistory(){
  const box=qs("#historyList"); if(!box) return;
  try{
    const rows=await api("/api/history");
    const labels={created:"Erstellt",updated:"Geändert",deleted:"Gelöscht",restored:"Wiederhergestellt"};
    box.innerHTML=rows.length?rows.map(h=>{
      const x=h.after||h.snapshot||{};
      const before=h.before||{};
      const diff=h.action==="updated"?historyDiffHtml(before,x):"";
      const legacyDetail=!diff && x.note?` · ${esc(compactHistoryText(x.note))}`:"";
      return `<div class="history-item"><div><b>${labels[h.action]||esc(h.action)}</b> · ${esc(formatDateValue(x.day||""))} · ${esc(x.title||x.person||"")}<div class="small history-time">${esc(new Date(h.created_at).toLocaleString("de-CH"))}${legacyDetail}</div>${diff}</div>${h.action==="deleted"?`<button class="secondary history-restore" data-id="${h.id}">Wiederherstellen</button>`:""}</div>`;
    }).join(""):'<div class="small">Noch keine Änderungen protokolliert.</div>';
    qsa(".history-restore").forEach(b=>b.addEventListener("click",async()=>{try{await api(`/api/history/${b.dataset.id}/restore`,{method:"POST",body:"{}"});await loadEntries();await loadHistory();toast("Wiederhergestellt");}catch(e){toast(e.message);}}));
  }catch(e){
    console.error("Änderungsverlauf konnte nicht geladen werden", e);
    box.innerHTML='<div class="small danger">Änderungsverlauf konnte nicht geladen werden.</div>';
  }
}



function filenameFromDisposition(value, fallback){
  if(!value) return fallback;
  const utf=value.match(/filename\*=UTF-8''([^;]+)/i);
  if(utf){try{return decodeURIComponent(utf[1].replace(/["']/g,""));}catch(_e){}}
  const plain=value.match(/filename="?([^";]+)"?/i);
  return plain?.[1] || fallback;
}

function openPdfDocument(url){
  if(!url) return;
  const win=window.open(url,"_blank","noopener");
  if(!win){
    // Popup blocked: navigating the current tab still guarantees access to the PDF.
    window.location.href=url;
  }
}

async function shareServerPdf(url, fallbackName="stiftungskalender.pdf"){
  try{
    toast("PDF wird erstellt …");
    const response=await serverFetch(url,{credentials:"same-origin",cache:"no-store"});
    if(response.status===401){location.href="/login";return;}
    if(!response.ok) throw new Error(`PDF konnte nicht erstellt werden (HTTP ${response.status})`);
    const blob=await response.blob();
    const filename=filenameFromDisposition(response.headers.get("content-disposition"),fallbackName);
    const file=new File([blob],filename,{type:"application/pdf"});

    if(navigator.share){
      const canShareFiles=!navigator.canShare || navigator.canShare({files:[file]});
      if(canShareFiles){
        try{
          await navigator.share({title:filename,files:[file]});
          return;
        }catch(error){
          if(error?.name==="AbortError") return;
          console.warn("Datei-Teilen fehlgeschlagen, verwende Download-Fallback",error);
        }
      }
    }

    const blobUrl=URL.createObjectURL(blob);
    const link=document.createElement("a");
    link.href=blobUrl;
    link.download=filename;
    link.rel="noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(()=>URL.revokeObjectURL(blobUrl),60000);
    toast("PDF geöffnet");
  }catch(error){
    console.error(error);
    toast(error.message || "PDF-Export fehlgeschlagen");
  }
}

async function shareServerFile(url, fallbackName, mimeType, preparing="Datei wird erstellt …"){
  try{
    toast(preparing);
    const response=await serverFetch(url,{credentials:"same-origin",cache:"no-store"});
    if(response.status===401){location.href="/login";return;}
    if(!response.ok) throw new Error(`Export fehlgeschlagen (HTTP ${response.status})`);
    const blob=await response.blob();
    const filename=filenameFromDisposition(response.headers.get("content-disposition"),fallbackName);
    const file=new File([blob],filename,{type:mimeType || blob.type || "application/octet-stream"});
    if(navigator.share){
      const canShareFiles=!navigator.canShare || navigator.canShare({files:[file]});
      if(canShareFiles){
        try{await navigator.share({title:filename,files:[file]});return;}
        catch(error){if(error?.name==="AbortError")return;}
      }
    }
    const blobUrl=URL.createObjectURL(blob);
    const link=document.createElement("a");
    link.href=blobUrl; link.download=filename; link.rel="noopener";
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(()=>URL.revokeObjectURL(blobUrl),60000);
    toast("Datei bereit");
  }catch(error){console.error(error);toast(error.message || "Export fehlgeschlagen");}
}


document.addEventListener("click",e=>{
  const openEntry=e.target.closest("[data-open-entry]");
  if(openEntry){
    e.preventDefault();
    openModal(Number(openEntry.dataset.openEntry));
    return;
  }

  const prefill=e.target.closest("[data-prefill-date]");
  if(prefill){
    e.preventDefault();
    prefillDate(prefill.dataset.prefillDate);
    return;
  }

  const editCompany=e.target.closest("[data-person-edit]");
  if(editCompany){
    e.preventDefault();
    openPersonEditor(Number(editCompany.dataset.personEdit));
    return;
  }

  const deleteCompany=e.target.closest("[data-person-delete]");
  if(deleteCompany){
    e.preventDefault();
    deletePerson(Number(deleteCompany.dataset.personDelete));
    return;
  }

  const pdfButton=e.target.closest(".pdf-share-button");
  if(pdfButton){
    e.preventDefault();
    openPdfDocument(pdfButton.dataset.url);
    return;
  }
});

document.addEventListener("DOMContentLoaded", async ()=>{
  yearOptions(qs("#yearSelect"));
  yearOptions(qs("#filterYear"));
  upgradeDateInputs();
  upgradeTimeInputs();
  qs("#quickDate").value=isoToday();
  syncDateShell(qs("#quickDate"));
  setCalendarRangeForYear(qs("#filterYear").value);
  qs("#quickDate").addEventListener("change",()=>{updateDateContext("quick");syncOvernight("quick");});
  qs("#modalDate").addEventListener("change",()=>{updateDateContext("modal");syncOvernight("modal");});
  setTiming("quick");
  ["quick","modal","batch"].forEach(mode=>renderScopeSummary(mode));
  ["quick","modal","batch"].forEach(prefix=>{
    qs(`#${prefix}AllDay`)?.addEventListener("change",()=>{
      syncTiming(prefix);
      if(prefix==="batch") resetBatchPreview();
    });
    qs(`#${prefix}StartTime`)?.addEventListener("change",()=>{autoAdvanceEndDate(prefix);syncOvernight(prefix);if(prefix==="batch")resetBatchPreview();});
    qs(`#${prefix}EndTime`)?.addEventListener("change",()=>{autoAdvanceEndDate(prefix);syncOvernight(prefix);if(prefix==="batch")resetBatchPreview();});
    qs(`#${prefix}EndDate`)?.addEventListener("change",()=>syncOvernight(prefix));
  });

  qs("#quickSave").addEventListener("click",async()=>{
    try{
      if(!qs("#quickDate").value)return toast("Datum wählen");
      if(!qs("#quickTitle").value.trim())return toast("Titel eingeben");
      const quickTiming=timingPayload("quick");
      if(!quickTiming.all_day && !quickTiming.end_day)return toast("Bis-Datum wählen");
      await saveEntry(qs("#quickDate").value,qs("#quickTitle").value,qs("#quickNote").value,quickTiming,"quick");
      qs("#quickTitle").value="";qs("#quickNote").value="";toast("Gespeichert");
    }catch(e){toast(e.message);}
  });
  qs("#openBatch").addEventListener("click",openBatchModal);
  qs("#listExportPeople").addEventListener("click",openExportPeopleModal);
  qs("#listPdfButton").addEventListener("click",()=>{
    const url=qs("#listPdfButton").dataset.url;
    if(url) openPdfDocument(url);
  });
  qs("#yearPdfButton").addEventListener("click",()=>{
    const year=qs("#yearSelect").value;
    const params=yearMonthQuery();
    const company=qs("#yearCompanyFilter")?.value||"";if(company)params.append("company",company);
    const query=params.toString();
    const url=`/export-year.pdf?year=${encodeURIComponent(year)}${query?`&${query}`:""}`;
    openPdfDocument(url);
  });
  qs("#yearPrintButton")?.addEventListener("click",()=>window.print());
  qs("#exportPeopleAll").addEventListener("click",()=>setExportPeopleChecks("all"));
  qs("#exportPeopleApply").addEventListener("click",applyExportPeopleSelection);
  qs("#batchPreview").addEventListener("click",previewBatch);
  qs("#batchCreate").addEventListener("click",createBatch);
  ["#batchWeekday","#batchStart","#batchEnd","#batchNote","#batchSkipVacations","#batchSkipHolidays"].forEach(sel=>{
    qs(sel).addEventListener(sel==="#batchNote"?"input":"change",resetBatchPreview);
  });
  qs("#modalSave").addEventListener("click",async()=>{
    try{
      if(!qs("#modalDate").value)return toast("Datum wählen");
      if(!qs("#modalEventTitle").value.trim())return toast("Titel eingeben");
      const modalTiming=timingPayload("modal");
      if(!modalTiming.all_day && !modalTiming.end_day)return toast("Bis-Datum wählen");
      await saveEntry(qs("#modalDate").value,qs("#modalEventTitle").value,qs("#modalNote").value,modalTiming,"modal",editingId);
      closeModal();toast("Gespeichert");
    }catch(e){toast(e.message);}
  });
  qs("#modalDuplicate").addEventListener("click",()=>{
    if(!editingId) return;
    const e=entries.find(x=>Number(x.id)===Number(editingId)); if(!e) return;
    editingId=null; qs("#modalTitle").textContent="Termin duplizieren";
    qs("#modalDate").value=addDaysIso(e.day,7); syncDateShell(qs("#modalDate"));
    if(qs("#modalEndDate")){ qs("#modalEndDate").value=addDaysIso(entryEndDay(e),7); syncDateShell(qs("#modalEndDate")); syncOvernight("modal"); }
    qs("#modalDelete").style.display="none"; qs("#modalShare").style.display="none"; qs("#modalDuplicate").style.display="none";
    toast("Neues Datum wählen und speichern");
  });
  qs("#modalShare").addEventListener("click",()=>{
    if(!editingId) return;
    const e=entries.find(x=>Number(x.id)===Number(editingId));
    const fallback=e?`stiftungskalender-${e.day}.ics`:`stiftungskalender.ics`;
    shareServerFile(`/api/entries/${editingId}/ics`,fallback,"text/calendar","Kalendereintrag wird erstellt …");
  });
  qs("#modalDelete").addEventListener("click",async()=>{
    if(!editingId||!confirm("Eintrag wirklich löschen?"))return;
    try{await api(`/api/entries/${editingId}`,{method:"DELETE"});closeModal();await loadEntries();toast("Gelöscht");}
    catch(e){toast(e.message);}
  });
  qs("#refreshHistory")?.addEventListener("click",loadHistory);
  qs("#filterYear").addEventListener("change",()=>{setCalendarRangeForYear(qs("#filterYear").value);renderList();});
  qs("#listRangeFrom")?.addEventListener("change",updateExportControls);
  qs("#listRangeTo")?.addEventListener("change",updateExportControls);
  qs("#listIcsButton")?.addEventListener("click",()=>{
    const from=qs("#listRangeFrom").value, to=qs("#listRangeTo").value;
    if(!from || !to) return toast("Von und Bis wählen");
    if(from>to) return toast("Von liegt nach Bis");
    shareServerFile(qs("#listIcsButton").dataset.url,`stiftungskalender-${from}-bis-${to}.ics`,"text/calendar","Kalenderdatei wird erstellt …");
  });
  qs("#filterSearch").addEventListener("input",renderList);
  qs("#yearSelect").addEventListener("change",()=>{renderYear();renderStatsByPerson();});
  qs("#yearCompanyFilter")?.addEventListener("change",renderYear);
  qs("#yearMonthSelect").addEventListener("click",openYearMonthsModal);
  qs("#yearMonthsAll").addEventListener("click",()=>setYearMonthChecks("all"));
  qs("#yearMonthsApply").addEventListener("click",applyYearMonthSelection);
  qs("#quickScopeButton")?.addEventListener("click",()=>openCompanyScopeModal("quick"));
  qs("#modalScopeButton")?.addEventListener("click",()=>openCompanyScopeModal("modal"));
  qs("#batchScopeButton")?.addEventListener("click",()=>openCompanyScopeModal("batch"));
  qs("#companyScopeClose")?.addEventListener("click",closeCompanyScopeModal);
  qs("#companyScopeModalBack")?.addEventListener("click",e=>{if(e.target===qs("#companyScopeModalBack"))closeCompanyScopeModal();});
  qs("#companyScopeAll")?.addEventListener("change",e=>{scopeDraft.all=e.target.checked;renderCompanyScopeGrid();});
  qs("#companyScopeSearch")?.addEventListener("input",renderCompanyScopeGrid);
  qs("#companyScopeNone")?.addEventListener("click",()=>{scopeDraft.all=false;scopeDraft.ids=[];if(qs("#companyScopeAll"))qs("#companyScopeAll").checked=false;renderCompanyScopeGrid();});
  qs("#companyScopeApply")?.addEventListener("click",applyCompanyScope);
  qs("#batchTitle")?.addEventListener("input",resetBatchPreview);
  qs("#companyBulkAdd")?.addEventListener("click",async()=>{const names=(qs("#companyBulkNames")?.value||"").split(/\r?\n/).map(x=>x.trim()).filter(Boolean);if(!names.length)return toast("Unternehmensnamen eingeben");try{const r=await api("/api/companies/bulk",{method:"POST",body:JSON.stringify({names})});qs("#companyBulkNames").value="";await loadPeople();toast(`${r.created||0} Unternehmen hinzugefügt`);}catch(e){toast(e.message);}});
  qs("#addPerson").addEventListener("click",async()=>{
    const name=qs("#newPerson").value.trim();if(!name)return;
    try{
      await api("/api/companies",{method:"POST",body:JSON.stringify({name,color:qs("#newColor").value})});
      qs("#newPerson").value="";await loadPeople();toast("Unternehmen hinzugefügt");
    }catch(e){toast(e.message);}
  });
  qs("#personEditorSave").addEventListener("click",savePersonEditor);

  qs("#copyIcal").addEventListener("click",()=>copyCalendarUrl(qs("#copyIcal").dataset.url));
  qs("#showGlobalQr").addEventListener("click",()=>toggleQrBox("#globalQrBox","#globalQrImage",qs("#showGlobalQr").dataset.url||"/api/calendar-qr.png"));
  qs("#periodStart").value=isoToday();
  qs("#periodEnd").value=isoToday();
  syncDateShell(qs("#periodStart"));
  syncDateShell(qs("#periodEnd"));
  qs("#addPeriod").addEventListener("click",addPeriod);
  qs("#periodKind").addEventListener("change",()=>{
    qs("#periodColor").value=qs("#periodKind").value==="holiday"?"#d65a6f":qs("#periodKind").value==="vacation"?"#f2a65a":"#80a4c2";
  });
  qs("#addSubscription").addEventListener("click",addSubscription);
  qs("#subKind").addEventListener("change",()=>{qs("#subColor").value=qs("#subKind").value==="holiday"?"#d65a6f":qs("#subKind").value==="vacation"?"#f2a65a":"#80a4c2";});
  qs("#icsImportForm").addEventListener("submit",async(e)=>{
    e.preventDefault();
    const fd=new FormData(e.target);
    try{
      const res=await serverFetch("/import.ics",{method:"POST",body:fd});
      const data=await res.json();
      if(!res.ok) throw new Error(data.error||"ICS Import fehlgeschlagen");
      await loadPeriods();
      toast(`${data.imported} Feiertage importiert${data.skipped?`, ${data.skipped} übersprungen`:""}`);
    }catch(err){toast(err.message);}
  });


  qs("#fullDataExport").addEventListener("click",()=>{
    shareServerFile("/export-data.json","stiftungskalender-backup.json","application/json");
  });
  qs("#fullDataImportForm").addEventListener("submit",async(e)=>{
    e.preventDefault();
    if(!confirm("Aktuelle Unternehmen, Termine, Ferien und Feiertage durch dieses Backup ersetzen? Vorher wird automatisch ein Sicherheitsbackup erstellt.")) return;
    const fd=new FormData(e.target);
    try{
      toast("Backup wird wiederhergestellt …");
      const res=await serverFetch("/import-data.json",{method:"POST",body:fd});
      const data=await res.json();
      if(!res.ok) throw new Error(data.error||"Import fehlgeschlagen");
      await loadPeople(); await loadEntries(); await loadPeriods(); await loadCalendarSubscriptions(); await loadConfig();
      e.target.reset();
      toast(`${data.companies??0} Unternehmen, ${data.entries} Termine, ${data.periods} Zeiträume, ${data.calendar_subscriptions||0} Abos wiederhergestellt`);
    }catch(err){toast(err.message);}
  });

  qs("#importForm").addEventListener("submit",async(e)=>{
    e.preventDefault();
    const fd=new FormData(e.target);
    try{
      const res=await serverFetch("/import.csv",{method:"POST",body:fd});
      const data=await res.json();
      if(!res.ok)throw new Error(data.error||"Import fehlgeschlagen");
      await loadPeople();await loadEntries();toast(`${data.imported} Zeilen importiert`);
    }catch(err){toast(err.message);}
  });

  try{await loadPeople();}catch(e){toast(e.message);}
  try{await loadEntries();}catch(e){toast(e.message);}
  try{await loadPeriods();}catch(e){toast(e.message);}
  try{await loadCalendarSubscriptions();}catch(e){toast(e.message);}
  try{await loadConfig();}catch(e){toast(e.message);}
  showPage(rememberedPage(),{persist:false,scroll:false});
});
