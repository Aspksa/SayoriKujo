'use strict';
const $ = id => document.getElementById(id);
let state = null, token = '', chatId = null, memoryRows = [], busy = false, loadingChat = false, activeTab = 'chat';
let refreshVersion = 0, chatReadVersion = 0, memoryReadVersion = 0, memoryScope = null, lastAttempt = null, currentProject = null, historyMemoryId = null;
const kinds = {fact:'Сведения',preference:'Предпочтение',project:'Проект',task:'Задача'};
const tiers = {long:'Долговременная',core:'Постоянное ядро',vault:'Особо долговременная',archive:'Архивная'};
const progressLabels={goal:'Цель',decision:'Решения',done:'Сделано · со слов пользователя',todo:'Осталось',stopped:'Место остановки'};
const statuses = {pending:'В очереди',processing:'Анализируется',done:'Готово',failed:'Ошибка анализа',cancelled:'Отменено'};
function node(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el; }
function notice(text, error=false) {$('notice').textContent=text;$('notice').hidden=!text;$('notice').classList.toggle('error',error);}
async function api(path, body) {const opts={headers:{'X-Sayori-Token':token}};if(body!==undefined){opts.method='POST';opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}const response=await fetch('/api/brain/'+path,opts);let data;try{data=await response.json();}catch{throw Error('Сервер не вернул JSON. Проверьте окно BAT.');}if(!response.ok){const error=Error(data.error||'Не удалось выполнить действие.');error.status=response.status;throw error;}if(data.csrf_token)token=data.csrf_token;return data;}
async function action(fn) {try{await fn();}catch(error){notice(error.message,true);}}
function tab(name, scope=null){
  activeTab=name;document.body.dataset.view=name;document.title=({chat:'Разговоры',memory:'Память',map:'Карта развития',projects:'Проекты',profile:'Профиль',checks:'Проверки памяти',settings:'Cloud.ru'}[name]||'Мозги')+' · SayoriKujo';
  for(const el of document.querySelectorAll('.tab'))el.hidden=el.id!=='tab-'+name;
  for(const el of document.querySelectorAll('[data-tab]')){el.classList.toggle('selected',el.dataset.tab===name);if(el.dataset.tab===name)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');}
  $('page-title').textContent={chat:'Разговоры',memory:'Память',map:'Карта развития',projects:'Проекты',profile:'Профиль',checks:'Проверки памяти',settings:'Cloud.ru'}[name];
  window.BrainDesign?.tabChanged(name);
  if(name==='map')action(()=>window.BrainMap?.show());
  if(name==='memory'){
    memoryScope=scope;$('memory-scope').hidden=!scope;
    $('memory-scope-label').textContent=scope?(scope.project_id?'Память проекта '+(scope.project_name||''):scope.message_id?'Память из выбранного сообщения':'Текущие записи, связанные с ответом'):'';
    if(scope){$('memory-search').value='';$('memory-filter').value='';}
    action(loadMemories);
  }
}
function fillSettings(){const c=state.settings;$('endpoint').value=c.endpoint;$('model').value=c.model;$('memory-model').value=c.memory_model;$('auto-memory').checked=c.auto_memory;$('auto-consolidation').checked=c.auto_consolidation;$('auto-neurons').checked=c.auto_neurons;$('auto-vault').checked=c.auto_vault;$('key-status').textContent=(state.has_key?'Ключ сохранён. ':'Ключ не сохранён. ')+state.protection+(state.key_error?' · '+state.key_error:'');const list=$('model-list');list.replaceChildren(node('option','Выберите модель из списка'));list.firstChild.value='';for(const row of c.models||[]){const option=node('option',row.id+(row.chat_candidate?'':' · не текстовый чат'));option.value=row.id;option.disabled=!row.chat_candidate;list.append(option);}list.value=c.model;window.BrainDesign?.settingsSaved();}
function renderChatList(){
  const list=$('chats'),scroll=list.scrollTop,query=$('chat-search').value.trim().toLocaleLowerCase('ru-RU');
  list.replaceChildren();$('chat-count').textContent=state?.chats.length||0;
  const chats=(state?.chats||[]).filter(chat=>chat.title.toLocaleLowerCase('ru-RU').includes(query));
  for(const chat of chats){const button=node('button',chat.title,'chat-item'+(chat.id===chatId?' active':''));button.title=chat.title;button.disabled=busy||loadingChat;button.onclick=()=>action(()=>selectChat(chat.id));list.append(button);}
  if(!chats.length)list.append(node('div',query?'Разговоры не найдены':'Здесь появится история','chat-empty-search'));
  list.scrollTop=scroll;
}
function renderState(){if(!state)return;for(const button of document.querySelectorAll('.analyze-memory'))button.disabled=busy||loadingChat;$('new-chat').disabled=busy||loadingChat;$('send').disabled=busy||loadingChat;const g=state.growth;window.BrainDesign?.stateChanged(state);$('n-memory').textContent=g.memories;$('n-sources').textContent=g.sources;$('n-messages').textContent=g.messages;$('n-processed').textContent=g.processed;$('connection').textContent=state.has_key&&state.settings.model?'Cloud.ru настроен':'Настройте Cloud.ru';$('connection').dataset.connected=String(!!(state.has_key&&state.settings.model));$('nav-memory-count').textContent=g.memories;renderChatList();const selected=state.chats.find(c=>c.id===chatId);$('chat-title').textContent=selected?selected.title:'Новый разговор';$('delete-chat').disabled=!selected||busy||loadingChat;$('jobs').replaceChildren();for(const job of state.jobs){const row=node('div',undefined,'job');row.append(node('span',statuses[job.status]+(job.status==='done'?' · новых записей: '+job.added:'')));if(job.error)row.append(node('span',job.error,'error'));$('jobs').append(row);}renderLearning();window.BrainMap?.stateChanged(state);}
function applyState(next, settings=false){
  state=next;
  if(chatId&&!busy&&!loadingChat&&!state.chats.some(c=>c.id===chatId)){chatId=null;renderMessages([]);renderHandoff(null);}
  renderState();if(settings)fillSettings();
}
async function refresh(settings=false){
  const version=++refreshVersion;
  const next=await api('state');
  if(version!==refreshVersion)return false;
  applyState(next,settings);return true;
}
async function fetchChat(id, {preserveScroll=false, allowBusy=false}={}){
  const version=++chatReadVersion;
  const result=await api('messages?chat_id='+encodeURIComponent(id));
  if(chatId!==id||version!==chatReadVersion||(busy&&!allowBusy))return null;
  renderMessages(result.messages,{preserveScroll});renderHandoff(result.summary);return result.messages;
}
async function selectChat(id){if(busy||loadingChat)return;loadingChat=true;chatId=id;renderState();try{await fetchChat(id);}finally{loadingChat=false;renderState();}}
function renderMessages(messages, {preserveScroll=false}={}){
  const view=$('messages'),previousTop=view.scrollTop;
  const atBottom=view.scrollHeight-view.clientHeight-view.scrollTop<64;
  view.replaceChildren();
  if(!messages.length)view.append(node('div','Расскажите, что важно сохранить.','empty'));
  for(const message of messages){
    const bubble=node('article',undefined,'bubble '+message.role);
    bubble.dataset.messageId=message.id;
    bubble.append(node('div',message.role==='user'?'ВЫ':'САЁРИ','role'));
    bubble.append(node('div',message.content,'message-text'));
    const meta=node('div',undefined,'message-meta');
    if(message.status==='pending')meta.append(node('span','Ожидание ответа…'));
    if(message.error)meta.append(node('span',message.error,'error'));
    if(message.model)meta.append(node('span',message.model));
    if(message.used_memory?.length){
      const used=node('button','использовано записей памяти: '+message.used_memory.length,'memory-reference');
      used.title='Открыть текущие записи, связанные с этим ответом';
      used.onclick=()=>tab('memory',{ids:message.used_memory});meta.append(used);
    }
    if(message.role==='assistant'&&message.status==='done'){
      const path=node('button','Путь памяти','memory-reference');
      path.onclick=()=>action(()=>window.BrainMap?.openReply(message.id));meta.append(path);
    }
    if(message.role==='user'){
      if(message.project_context){const context=message.project_context;meta.append(node('div',context.name?'Проект: '+context.name:context.reason,'project-context'));}
      const job=message.memory_job;
      if(job){
        const progress=node('div','Память: '+statuses[job.status]+(job.status==='done'?' · новых записей: '+job.added:''),'memory-progress');
        progress.dataset.memoryStatus=job.status;
        if(job.status==='failed')progress.classList.add('error');
        meta.append(progress);
        if(job.error)meta.append(node('div',job.error,'error'));
        if(job.status==='done'){
          const saved=node('button','Что сохранено','memory-reference');
          saved.onclick=()=>tab('memory',{message_id:message.id});meta.append(saved);
        }
      }
      if(!job||!['pending','processing'].includes(job.status)){
        const retry=node('button',job?'Повторить разбор':'Разобрать в память','analyze-memory');
        retry.title='Дополнительный запрос к Cloud.ru';retry.disabled=busy||loadingChat;
        retry.onclick=()=>action(async()=>{
          if(busy||loadingChat)return;
          retry.disabled=true;
          try{
            await api('analyze',{message_id:message.id});
            notice('Анализ добавлен в очередь. Это отдельный запрос к Cloud.ru.');
            await refresh();await fetchChat(message.chat_id,{preserveScroll:true});
          }finally{if(retry.isConnected)retry.disabled=busy||loadingChat;}
        });meta.append(retry);
      }
    }
    bubble.append(meta);view.append(bubble);
  }
  view.scrollTop=preserveScroll&&!atBottom?previousTop:view.scrollHeight;
}
let memoryCursor=null;
async function loadMemories(append=false,preserve=false){
  const version=++memoryReadVersion;
  const params=new URLSearchParams({q:$('memory-search').value,paged:'1',tier:$('memory-filter').value});if(append&&memoryCursor)params.set('cursor',memoryCursor);
  if(memoryScope?.project_id)params.set('project_id',memoryScope.project_id);
  if(memoryScope?.message_id)params.set('message_id',memoryScope.message_id);
  if(memoryScope?.ids)params.set('ids',memoryScope.ids.join(','));
  const target=preserve?memoryRows.length:0;
  let result=await api('memories?'+params);
  while(!append&&result.has_more&&result.memories.length<target){
    if(version!==memoryReadVersion)return;
    params.set('cursor',result.cursor);const next=await api('memories?'+params);
    result={...next,memories:[...result.memories,...next.memories]};
  }
  if(version!==memoryReadVersion)return;
  memoryCursor=result.cursor;memoryRows=append?[...new Map([...memoryRows,...result.memories].map(row=>[row.id,row])).values()]:result.memories;$('memory-more').hidden=!result.has_more;renderMemories();
}
function editMemory(row=null){$('memory-editor').hidden=false;$('memory-id').value=row?.id||'';$('memory-content').value=row?.content||'';$('memory-kind').value=row?.kind||'fact';$('memory-tier').value=row?.tier||'long';fillProjectSelect($('memory-project'),row?.projects?.[0]?.id||'');$('memory-content').focus();$('memory-editor').scrollIntoView({behavior:'smooth',block:'nearest'});}
function renderMemories(){const container=$('memories');container.replaceChildren();const rows=memoryRows.filter(r=>!$('memory-filter').value||r.tier===$('memory-filter').value);$('memory-result-count').textContent='Показано записей: '+rows.length+(memoryCursor?' · есть ещё':'');for(const row of rows){const card=node('article',undefined,'memory-card panel');const tags=node('div',undefined,'tags');for(const text of [kinds[row.kind],tiers[row.tier],row.completed_task?'Задача закрыта · со слов пользователя':null,...(row.projects||[]).map(project=>project.name),row.confirmed?'Подтверждено':'Из ваших слов',row.status==='conflict'?'Возможное противоречие':row.status==='superseded'?'Заменено':null].filter(Boolean))tags.append(node('span',text,'tag'));card.append(tags,node('p',row.content));if(row.note)card.append(node('p','Автоматическое толкование: '+row.note,'muted fine'));card.append(node('small',new Date(row.created).toLocaleString('ru-RU')));if(row.sources.length){const details=node('details');details.append(node('summary','Источники · '+(row.source_count??row.sources.length)));for(const source of row.sources){const item=node('div',source.content,'source');const button=node('button','Открыть разговор','quiet');button.onclick=()=>action(async()=>{tab('chat');await selectChat(source.chat_id);});item.append(button);details.append(item);}if(row.source_count>row.sources.length)details.append(node('p','Показаны последние 20 источников. Остальные сохранены в базе.','muted fine'));card.append(details);}const actions=node('div',undefined,'actions');const edit=node('button','Исправить / подтвердить');edit.onclick=()=>editMemory(row);const remove=node('button','Забыть','quiet danger');remove.onclick=()=>action(async()=>{await api('memory-delete',{id:row.id});await loadMemories();await refresh();});const history=node('button','История','quiet');history.onclick=()=>action(()=>openVersions(row.id));const passport=node('button','На карте','quiet');passport.onclick=()=>action(()=>window.BrainMap?.open('memory:'+row.id));actions.append(edit,history,passport,remove);card.append(actions);container.append(card);}if(!rows.length)container.append(node('div',memoryScope?'Связанных записей нет: устойчивые сведения не найдены, записи удалены или не подходят фильтру.':($('memory-search').value||$('memory-filter').value?'По этим условиям ничего не найдено.':'Здесь появятся важные сведения из ваших разговоров.'),'empty panel'));}
function settingsPayload(){return{endpoint:$('endpoint').value,model:$('model').value,memory_model:$('memory-model').value,auto_memory:$('auto-memory').checked,auto_consolidation:$('auto-consolidation').checked,auto_neurons:$('auto-neurons').checked,auto_vault:$('auto-vault').checked,...($('api-key').value?{api_key:$('api-key').value}:{})};}
async function saveSettings(){const next=await api('settings',settingsPayload());++refreshVersion;applyState(next,true);$('api-key').value='';window.BrainDesign?.hideKey();}
for(const button of document.querySelectorAll('[data-tab]'))button.onclick=()=>tab(button.dataset.tab);
$('new-chat').onclick=()=>action(async()=>{if(busy||loadingChat)return;loadingChat=true;renderState();try{const chat=await api('chats',{});chatId=chat.id;await refresh();await fetchChat(chatId);}finally{loadingChat=false;renderState();}});
$('delete-chat').onclick=()=>action(async()=>{if(!chatId)return;if(!confirm('Удалить разговор и его неподтверждённые воспоминания без других источников?'))return;await api('chat-delete',{id:chatId});chatId=null;renderHandoff(null);await refresh();renderMessages([]);});
$('chat-form').onsubmit=event=>{
  event.preventDefault();if(busy||loadingChat||!state)return;
  action(async()=>{
    const text=$('message').value.trim();if(!text)return;
    busy=true;++chatReadVersion;renderState();
    $('chat-status').textContent='Саёри отвечает…';
    let attempt=null,requestError=null,complete=false;
    const clearSentDraft=()=>{if($('message').value.trim()===text)$('message').value='';};
    try{
      if(!chatId){const chat=await api('chats',{});chatId=chat.id;}
      attempt=lastAttempt?.chat_id===chatId&&lastAttempt.text===text
        ?lastAttempt:{chat_id:chatId,text,request_id:crypto.randomUUID().replaceAll('-','')};
      lastAttempt=attempt;
      const result=await api('chat',attempt);
      complete=true;clearSentDraft();renderMessages(result.messages);notice('');
    }catch(error){requestError=error;}
    finally{
      try{
        await refresh();
        if(chatId){
          const messages=await fetchChat(chatId,{allowBusy:true});
          const saved=messages?.find(message=>message.request_id===attempt?.request_id);
          if(saved?.status==='done'){complete=true;clearSentDraft();notice('');}
          if(saved?.status==='error')lastAttempt=null;
        }
      }catch(error){
        if(!requestError&&!complete)requestError=error;
        if(complete)notice('Ответ сохранён; состояние не удалось обновить. Обновите страницу.');
      }finally{
        if(complete)lastAttempt=null;
        busy=false;renderState();
        $('chat-status').textContent=state?.settings.auto_memory?'Автопамять включена · анализ тарифицируется отдельно':'Автопамять выключена';
      }
    }
    if(requestError&&!complete)throw requestError;
  });
};
$('message').onkeydown=event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey))$('chat-form').requestSubmit();};
$('settings-form').onsubmit=event=>{event.preventDefault();action(async()=>{await saveSettings();notice('Настройки сохранены.');});};
$('load-models').onclick=()=>action(async()=>{await saveSettings();$('load-models').disabled=true;try{await api('models',{});await refresh(true);notice('Список получен. Нетекстовые модели отключены; проверка всех моделей не запускалась.');}finally{$('load-models').disabled=false;}});
$('model-list').onchange=()=>{if($('model-list').value)$('model').value=$('model-list').value;};
$('test-model').onclick=()=>action(async()=>{await saveSettings();$('test-model').disabled=true;try{const result=await api('test',{});notice('Ответ модели: '+result.answer);}finally{$('test-model').disabled=false;}});
$('delete-key').onclick=()=>action(async()=>{const next=await api('settings',{delete_key:true});++refreshVersion;applyState(next,true);$('api-key').value='';notice('Ключ удалён.');});
$('new-memory').onclick=()=>editMemory();$('cancel-memory').onclick=()=>{$('memory-editor').hidden=true;};
$('memory-editor').onsubmit=event=>{event.preventDefault();action(async()=>{await api('memory',{id:$('memory-id').value||undefined,content:$('memory-content').value,kind:$('memory-kind').value,tier:$('memory-tier').value,project_id:$('memory-project').value||null});$('memory-editor').hidden=true;await loadMemories();await refresh();});};
$('clear-memory-scope').onclick=()=>tab('memory');
let searchTimer;$('memory-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>action(loadMemories),250);};$('memory-filter').onchange=()=>action(()=>loadMemories());$('memory-more').onclick=()=>action(async()=>{$('memory-more').disabled=true;try{await loadMemories(true);}finally{$('memory-more').disabled=false;}});
action(async()=>{await refresh(true);if(state.chats.length)await selectChat(state.chats[0].id);const requested=location.hash.slice(1);if(['chat','memory','map','projects','profile','checks','settings'].includes(requested))tab(requested);else tab(!state.has_key||!state.settings.model?'settings':'chat');});
setInterval(()=>{
  if(document.hidden)return;
  action(async()=>{
    const beforeJobs=JSON.stringify([state?.jobs,state?.analysis]),beforeMessages=state?.growth.messages,beforeProcessed=state?.growth.processed,beforeVault=state?.neural?.vault;
    if(!await refresh(!state))return;
    if(chatId&&!busy&&!loadingChat&&(beforeJobs!==JSON.stringify([state.jobs,state.analysis])||beforeMessages!==state.growth.messages)){
      await fetchChat(chatId,{preserveScroll:true});
    }
    if(activeTab==='memory'&&(beforeProcessed!==state.growth.processed||beforeVault!==state.neural?.vault))await loadMemories(false,true);
  });
},5000);

function fillProjectSelect(select,selected){
  if(document.activeElement===select)return;
  select.replaceChildren(node('option',select.id==='memory-project'?'Общее / личное':'Не определён'));
  select.firstChild.value='';
  for(const project of state?.projects||[]){const option=node('option',project.name);option.value=project.id;select.append(option);}
  select.value=selected||'';
}
async function openMessage(chat,message){
  if(busy||loadingChat)return;
  tab('chat');await selectChat(chat);
  const target=[...document.querySelectorAll('[data-message-id]')].find(el=>el.dataset.messageId===message);
  if(target){target.scrollIntoView({behavior:'smooth',block:'center'});target.classList.add('source-highlight');setTimeout(()=>target.classList.remove('source-highlight'),2000);}
}
function entryList(container,entries,limit=3){
  for(const [kind,label] of Object.entries(progressLabels)){
    const selected=entries.filter(entry=>entry.kind===kind).slice(0,limit);
    if(!selected.length)continue;
    container.append(node('h4',label));
    for(const entry of selected){const item=node('div',undefined,'project-entry');item.append(node('p',entry.content));const source=node('button','Источник','quiet');source.onclick=()=>action(()=>openMessage(entry.chat_id,entry.message_id));item.append(source);container.append(item);}
  }
}
function renderHandoff(summary){
  currentProject=summary?.project?.id||null;
  fillProjectSelect($('chat-project'),currentProject);
  $('handoff').hidden=!summary;
  const container=$('handoff-content');container.replaceChildren();
  if(!summary)return;
  container.append(node('p','Сохранено сообщений: '+summary.message_count+(summary.project?' · Проект '+summary.project.name:''),'muted fine'));
  if(summary.last_user)container.append(node('p','Последнее сообщение пользователя: '+summary.last_user.content,'handoff-excerpt'));
  if(summary.project)entryList(container,summary.project.entries,2);
  if(summary.memories.length){const button=node('button','Важные записи из разговора','quiet');button.onclick=()=>tab('memory',{ids:summary.memories.map(row=>row.id)});container.append(button);}
}
function renderLearning(){
  fillProjectSelect($('chat-project'),currentProject);$('chat-project').disabled=busy||loadingChat||!chatId;
  fillProjectSelect($('memory-project'),$('memory-project').value);
  const projects=$('projects'),projectQuery=$('projects-search').value.trim().toLocaleLowerCase('ru-RU');projects.replaceChildren();
  const visibleProjects=(state.projects||[]).filter(project=>project.name.toLocaleLowerCase('ru-RU').includes(projectQuery));
  for(const project of visibleProjects){
    const card=node('article',undefined,'panel project-card');card.dataset.projectId=project.id;
    const heading=node('div',undefined,'project-heading'),symbol=node('span',project.name.slice(0,1).toLocaleUpperCase('ru-RU'),'project-symbol'),title=node('div');symbol.setAttribute('aria-hidden','true');title.append(node('h3',project.name),node('small','Отдельный контекст и память'));heading.append(symbol,title);card.append(heading);
    const stats=node('div',undefined,'project-stats');
    for(const [kind,label] of [['goal','целей'],['todo','записей задач'],['done','отчётов о выполнении']]){const item=node('span');item.append(node('strong',project.entries.filter(entry=>entry.kind===kind).length),node('span',label));stats.append(item);}card.append(stats);
    entryList(card,project.entries);if(!project.entries.length)card.append(node('p','Прогресс появится из следующих сообщений.','muted fine'));
    const actions=node('div',undefined,'actions'),memory=node('button','Память проекта'),map=node('button','На карте','quiet');memory.onclick=()=>tab('memory',{project_id:project.id,project_name:project.name});map.onclick=()=>action(()=>window.BrainMap?.open('project:'+project.id));actions.append(memory,map);card.append(actions);projects.append(card);
  }
  if(!visibleProjects.length)projects.append(node('div',projectQuery?'По этому названию проектов нет.':'Назовите проект в чате и расскажите его цель. После анализа здесь появится карточка.','empty panel'));
  const profile=$('profile'),profileQuery=$('profile-search').value.trim().toLocaleLowerCase('ru-RU');profile.replaceChildren();
  const preferences=state.profile||[],summary=$('profile-summary');summary.replaceChildren();
  for(const [value,label] of [[preferences.length,'предпочтений'],[preferences.filter(p=>p.confirmed).length,'подтверждено вами']]){const item=node('span',undefined,'profile-stat');item.append(node('strong',value),node('span',label));summary.append(item);}
  const visiblePreferences=preferences.filter(p=>p.content.toLocaleLowerCase('ru-RU').includes(profileQuery));
  for(const preference of visiblePreferences){const card=node('article',undefined,'panel memory-card');card.append(node('span',preference.confirmed?'Подтверждено вами':preference.mentions>1?'Упоминаний: '+preference.mentions:'Из ваших слов','tag'),node('p',preference.content));const edit=node('button','Открыть запись памяти');edit.onclick=()=>tab('memory',{ids:[preference.id]});card.append(edit);profile.append(card);}
  if(!visiblePreferences.length)profile.append(node('div',profileQuery?'Предпочтения не найдены.':'Здесь появятся общие предпочтения из ваших сообщений.','empty panel'));
  renderChecks(state.exams);
}
function renderChecks(result){
  const box=$('checks-result'),list=$('checks-list');box.replaceChildren();list.replaceChildren();
  if(!result?.total)box.append(node('p','Пока недостаточно активных записей для проверки.'));
  else{box.append(node('strong','Пройдено '+result.passed+' из '+result.total));const meter=node('meter');meter.min=0;meter.max=result.total;meter.value=result.passed;meter.setAttribute('aria-label','Доля успешных проверок памяти');box.append(meter,node('p','Последняя проверка: '+new Date(result.created).toLocaleString('ru-RU'),'muted fine'));}
  if(result?.error)box.append(node('p',result.error,'error'));
  for(const check of result?.details||[]){const row=node('article',undefined,'panel check-case');row.dataset.passed=String(check.passed);row.append(node('strong',check.passed?'Пройдено':'Нужна проверка',check.passed?'check-ok':'error'),node('p',check.question),node('small',(check.grouped?'Найден подтверждённый вами повтор':check.retrieved?'Запись найдена':'Запись не попала в контекст')+' · '+(check.grounded?'Источник или ручное подтверждение есть':'Не найдено подтверждение источником')));if(check.memory_id){const open=node('button','Открыть запись','quiet');open.onclick=()=>tab('memory',{ids:[check.memory_id]});row.append(open);}list.append(row);}
}
async function openVersions(identity){
  historyMemoryId=identity;
  const result=await api('versions?id='+encodeURIComponent(identity));
  if(historyMemoryId!==identity)return;
  $('memory-history').hidden=false;const list=$('versions');list.replaceChildren();
  for(const version of result.versions){const row=node('article',undefined,'version-row');row.append(node('strong',version.reason),node('small',new Date(version.created).toLocaleString('ru-RU')),node('p',version.snapshot.content));const restore=node('button','Восстановить эту версию');restore.onclick=()=>action(async()=>{restore.disabled=true;try{const result=await api('restore-version',{id:version.id});await refresh();await loadMemories();await openVersions(result.id);notice('Версия памяти восстановлена.');}finally{if(restore.isConnected)restore.disabled=false;}});row.append(restore);list.append(row);}
  if(!result.versions.length)list.append(node('p','Сохранённых версий нет.'));
  $('memory-history').scrollIntoView({behavior:'smooth',block:'nearest'});
}
$('close-versions').onclick=()=>{historyMemoryId=null;$('memory-history').hidden=true;};
$('run-checks').onclick=()=>action(async()=>{$('run-checks').disabled=true;try{const result=await api('checks',{});renderChecks(result);await refresh();}finally{$('run-checks').disabled=false;}});
$('chat-project').onchange=()=>action(async()=>{if(busy||loadingChat||!chatId)return;const identity=chatId,project=$('chat-project').value||null;loadingChat=true;renderState();try{await api('set-project',{chat_id:identity,project_id:project});await refresh();await fetchChat(identity);}finally{loadingChat=false;renderState();}});
