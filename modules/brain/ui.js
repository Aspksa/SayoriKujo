'use strict';
const $ = id => document.getElementById(id);
let state = null, token = '', chatId = null, memoryRows = [], busy = false, loadingChat = false, activeTab = 'chat';
let refreshVersion = 0, chatReadVersion = 0, memoryReadVersion = 0, memoryScope = null, lastAttempt = null;
const kinds = {fact:'Сведения',preference:'Предпочтение',project:'Проект',task:'Задача'};
const tiers = {long:'Долговременная',core:'Постоянное ядро',archive:'Архивная'};
const statuses = {pending:'В очереди',processing:'Анализируется',done:'Готово',failed:'Ошибка анализа',cancelled:'Отменено'};
function node(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el; }
function notice(text, error=false) {$('notice').textContent=text;$('notice').hidden=!text;$('notice').classList.toggle('error',error);}
async function api(path, body) {const opts={headers:{'X-Sayori-Token':token}};if(body!==undefined){opts.method='POST';opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}const response=await fetch('/api/brain/'+path,opts);let data;try{data=await response.json();}catch{throw Error('Сервер не вернул JSON. Проверьте окно BAT.');}if(!response.ok)throw Error(data.error||'Не удалось выполнить действие.');if(data.csrf_token)token=data.csrf_token;return data;}
async function action(fn) {try{await fn();}catch(error){notice(error.message,true);}}
function tab(name, scope=null){
  activeTab=name;
  for(const el of document.querySelectorAll('.tab'))el.hidden=el.id!=='tab-'+name;
  for(const el of document.querySelectorAll('[data-tab]'))el.classList.toggle('selected',el.dataset.tab===name);
  $('page-title').textContent={chat:'Разговоры',memory:'Память',settings:'Cloud.ru'}[name];
  if(name==='memory'){
    memoryScope=scope;$('memory-scope').hidden=!scope;
    $('memory-scope-label').textContent=scope?(scope.message_id?'Память из выбранного сообщения':'Текущие записи, связанные с ответом'):'';
    if(scope){$('memory-search').value='';$('memory-filter').value='';}
    action(loadMemories);
  }
}
function fillSettings(){const c=state.settings;$('endpoint').value=c.endpoint;$('model').value=c.model;$('memory-model').value=c.memory_model;$('auto-memory').checked=c.auto_memory;$('key-status').textContent=(state.has_key?'Ключ сохранён. ':'Ключ не сохранён. ')+state.protection+(state.key_error?' · '+state.key_error:'');const list=$('model-list');list.replaceChildren(node('option','Выберите модель из списка'));list.firstChild.value='';for(const row of c.models||[]){const option=node('option',row.id+(row.chat_candidate?'':' · не текстовый чат'));option.value=row.id;option.disabled=!row.chat_candidate;list.append(option);}list.value=c.model;}
function renderState(){if(!state)return;for(const button of document.querySelectorAll('.analyze-memory'))button.disabled=busy||loadingChat;$('new-chat').disabled=busy||loadingChat;$('send').disabled=busy||loadingChat;const g=state.growth;$('n-memory').textContent=g.memories;$('n-sources').textContent=g.sources;$('n-messages').textContent=g.messages;$('n-processed').textContent=g.processed;$('connection').textContent=state.has_key&&state.settings.model?'Cloud.ru настроен':'Настройте Cloud.ru';$('chats').replaceChildren();for(const chat of state.chats){const button=node('button',chat.title,'chat-item'+(chat.id===chatId?' active':''));button.disabled=busy||loadingChat;button.onclick=()=>action(()=>selectChat(chat.id));$('chats').append(button);}const selected=state.chats.find(c=>c.id===chatId);$('chat-title').textContent=selected?selected.title:'Новый разговор';$('delete-chat').disabled=!selected||busy||loadingChat;$('jobs').replaceChildren();for(const job of state.jobs){const row=node('div',undefined,'job');row.append(node('span',statuses[job.status]+(job.status==='done'?' · новых записей: '+job.added:'')));if(job.error)row.append(node('span',job.error,'error'));$('jobs').append(row);}}
function applyState(next, settings=false){
  state=next;
  if(chatId&&!busy&&!loadingChat&&!state.chats.some(c=>c.id===chatId)){chatId=null;renderMessages([]);}
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
  renderMessages(result.messages,{preserveScroll});return result.messages;
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
    if(message.role==='user'){
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
async function loadMemories(){
  const version=++memoryReadVersion;
  const params=new URLSearchParams({q:$('memory-search').value});
  if(memoryScope?.message_id)params.set('message_id',memoryScope.message_id);
  if(memoryScope?.ids)params.set('ids',memoryScope.ids.join(','));
  const result=await api('memories?'+params);
  if(version!==memoryReadVersion)return;
  memoryRows=result.memories;renderMemories();
}
function editMemory(row=null){$('memory-editor').hidden=false;$('memory-id').value=row?.id||'';$('memory-content').value=row?.content||'';$('memory-kind').value=row?.kind||'fact';$('memory-tier').value=row?.tier||'long';$('memory-content').focus();$('memory-editor').scrollIntoView({behavior:'smooth',block:'nearest'});}
function renderMemories(){const container=$('memories');container.replaceChildren();const rows=memoryRows.filter(r=>!$('memory-filter').value||r.tier===$('memory-filter').value);for(const row of rows){const card=node('article',undefined,'memory-card panel');const tags=node('div',undefined,'tags');for(const text of [kinds[row.kind],tiers[row.tier],row.confirmed?'Подтверждено':'Из ваших слов',row.status==='conflict'?'Возможное противоречие':row.status==='superseded'?'Заменено':null].filter(Boolean))tags.append(node('span',text,'tag'));card.append(tags,node('p',row.content));if(row.note)card.append(node('p','Автоматическое толкование: '+row.note,'muted fine'));card.append(node('small',new Date(row.created).toLocaleString('ru-RU')));if(row.sources.length){const details=node('details');details.append(node('summary','Источники · '+row.sources.length));for(const source of row.sources){const item=node('div',source.content,'source');const button=node('button','Открыть разговор','quiet');button.onclick=()=>action(async()=>{tab('chat');await selectChat(source.chat_id);});item.append(button);details.append(item);}card.append(details);}const actions=node('div',undefined,'actions');const edit=node('button','Исправить / подтвердить');edit.onclick=()=>editMemory(row);const remove=node('button','Забыть','quiet danger');remove.onclick=()=>action(async()=>{await api('memory-delete',{id:row.id});await loadMemories();await refresh();});actions.append(edit,remove);card.append(actions);container.append(card);}if(!rows.length)container.append(node('div',memoryScope?'Связанных записей нет: устойчивые сведения не найдены, записи удалены или не подходят фильтру.':($('memory-search').value||$('memory-filter').value?'По этим условиям ничего не найдено.':'Здесь появятся важные сведения из ваших разговоров.'),'empty panel'));}
function settingsPayload(){return{endpoint:$('endpoint').value,model:$('model').value,memory_model:$('memory-model').value,auto_memory:$('auto-memory').checked,...($('api-key').value?{api_key:$('api-key').value}:{})};}
async function saveSettings(){const next=await api('settings',settingsPayload());++refreshVersion;applyState(next,true);$('api-key').value='';}
for(const button of document.querySelectorAll('[data-tab]'))button.onclick=()=>tab(button.dataset.tab);
$('new-chat').onclick=()=>action(async()=>{if(busy||loadingChat)return;loadingChat=true;renderState();try{const chat=await api('chats',{});chatId=chat.id;await refresh();await fetchChat(chatId);}finally{loadingChat=false;renderState();}});
$('delete-chat').onclick=()=>action(async()=>{if(!chatId)return;if(!confirm('Удалить разговор и его неподтверждённые воспоминания без других источников?'))return;await api('chat-delete',{id:chatId});chatId=null;await refresh();renderMessages([]);});
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
$('memory-editor').onsubmit=event=>{event.preventDefault();action(async()=>{await api('memory',{id:$('memory-id').value||undefined,content:$('memory-content').value,kind:$('memory-kind').value,tier:$('memory-tier').value});$('memory-editor').hidden=true;await loadMemories();await refresh();});};
$('clear-memory-scope').onclick=()=>tab('memory');
let searchTimer;$('memory-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>action(loadMemories),250);};$('memory-filter').onchange=renderMemories;
action(async()=>{await refresh(true);if(state.chats.length)await selectChat(state.chats[0].id);if(!state.has_key||!state.settings.model)tab('settings');});
setInterval(()=>{
  if(document.hidden)return;
  action(async()=>{
    const beforeJobs=JSON.stringify([state?.jobs,state?.analysis]),beforeMessages=state?.growth.messages,beforeProcessed=state?.growth.processed;
    if(!await refresh(!state))return;
    if(chatId&&!busy&&!loadingChat&&(beforeJobs!==JSON.stringify([state.jobs,state.analysis])||beforeMessages!==state.growth.messages)){
      await fetchChat(chatId,{preserveScroll:true});
    }
    if(activeTab==='memory'&&beforeProcessed!==state.growth.processed)await loadMemories();
  });
},5000);
