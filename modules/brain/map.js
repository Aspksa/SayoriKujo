'use strict';
(() => {
  const el=id=>document.getElementById(id);
  const labels={active:'Актуально',paused:'Приостановлено',local:'Локально',reported:'Со слов пользователя',
    completed:'Задача выполнена · со слов пользователя',open:'Нужно уточнить',resolved:'Решено',selected:'Выбрано',proposed:'Вариант',conflict:'Возможное противоречие',
    superseded:'Заменено',done:'Сохранено',pending:'Ожидание'};
  const relationLabels={contains:'Содержит',develops:'Направление',belongs:'Относится к проекту',remembers:'Сохранено в памяти',
    grounds:'Источник',closes:'Закрывает задачу',supersedes:'Заменяет',answers:'Отвечает на вопрос',selects:'Выбирает вариант',
    duplicate:'Объединено вами как повтор',conflicts:'Возможно противоречит'};
  const progressNames={goal:'Цель',decision:'Решение',done:'Сделано',todo:'Осталось',stopped:'Место остановки'};
  let graph=null,selected='brain',passport=null,mode='graph',active=false,limit=30,path=false;
  let graphVersion=0,passportVersion=0,auxVersion=0,revision=null,loading=false,refreshQueued=false;
  let externalFocus=null;
  function element(tag,text,cls){const result=document.createElement(tag);if(text!==undefined)result.textContent=text;if(cls)result.className=cls;return result;}
  function button(text,fn,cls=''){const result=element('button',text,cls);result.type='button';result.onclick=()=>action(async()=>{if(result.disabled)return;result.disabled=true;try{await fn();}finally{if(result.isConnected)result.disabled=false;}});return result;}
  function date(value){return value?new Date(value).toLocaleString('ru-RU'):'—';}
  function blank(container,text){container.replaceChildren(element('div',text,'map-empty'));}
  function applyFilters(){
    const select=el('map-project'),current=select.value;
    if(document.activeElement!==select){select.replaceChildren(element('option','Вся память'));select.firstChild.value='';
      for(const project of state?.projects||[]){const option=element('option',project.name);option.value=project.id;select.append(option);}
      select.value=(state?.projects||[]).some(project=>project.id===current)?current:'';
    }
  }
  function updateReplies(){
    const select=el('map-reply'),previous=select.value;
    el('map-path').disabled=!select.value;
    if(document.activeElement===select)return;
    select.replaceChildren(element('option','Без подсветки ответа'));select.firstChild.value='';
    for(const reply of graph.replies){const option=element('option',date(reply.created)+' · '+reply.prompt);option.value=reply.reply_id;select.append(option);}
    if(previous&&graph.trace?.reply_id===previous&&!graph.replies.some(reply=>reply.reply_id===previous)){const option=element('option',date(graph.trace.created)+' · Выбранный ответ');option.value=previous;select.append(option);}
    select.value=previous;
    el('map-path').disabled=!select.value;
  }
  function query(focus=null){const params=new URLSearchParams();if(el('map-project').value)params.set('project_id',el('map-project').value);
    if(el('map-reply').value)params.set('reply_id',el('map-reply').value);if(focus)params.set('focus',focus);return params.toString();}
  async function loadGraph(focus=null){
    const version=++graphVersion;
    const next=await api('graph?'+query(focus));
    if(version!==graphVersion)return;
    graph=next;externalFocus=focus;updateReplies();
    if(!graph.nodes.some(node=>node.id===selected))selected=focus||'brain';
    renderGraph();await loadPassport(selected);
  }
  function visibleNodes(){
    if(!graph)return [];
    const selectedEdges=graph.edges.filter(edge=>edge.source===selected||edge.target===selected);
    const neighbors=new Set(selectedEdges.flatMap(edge=>[edge.source,edge.target]));
    const trace=new Set(path?graph.trace?.node_ids||[]:[]);
    return graph.nodes.filter(node=>node.type!=='message'||el('map-sources').checked||node.id===selected)
      .sort((a,b)=>priority(b)-priority(a)||a.title.localeCompare(b.title,'ru')).slice(0,limit);
    function priority(node){return node.id===selected?100:trace.has(node.id)?90:neighbors.has(node.id)?80:
      node.type==='brain'?70:node.type==='project'?60:node.type==='branch'?50:node.type==='question'?40:node.type==='choice'?35:
      node.type==='progress'?30:node.type==='memory'?20:10;}
  }
  function renderGraph(){
    if(!graph)return;
    const container=el('map-nodes'),nodes=visibleNodes();container.replaceChildren();
    const trace=new Set(path?graph.trace?.node_ids||[]:[]);
    const neighbors=new Set(graph.edges.filter(edge=>edge.source===selected||edge.target===selected).flatMap(edge=>[edge.source,edge.target]));
    for(const data of nodes){const card=button('',()=>selectNode(data.id),'map-node');card.dataset.nodeId=data.id;card.dataset.type=data.type;
      card.setAttribute('aria-pressed',String(data.id===selected));card.setAttribute('aria-label',(data.type_label||'Запись')+': '+data.title);
      card.classList.toggle('on-path',trace.has(data.id));card.classList.toggle('related',neighbors.has(data.id));
      card.append(element('span',data.type==='progress'?progressNames[data.kind]||'Прогресс':data.type_label||'Запись','map-node-type'),
        element('span',data.title,'map-node-title'),element('span',labels[data.status]||data.status||'','map-node-state'));container.append(card);
    }
    const available=graph.nodes.filter(node=>node.type!=='message'||el('map-sources').checked||node.id===selected).length;
    el('map-count').textContent='Показано '+nodes.length+' из '+available+' узлов'+(graph.bounded?' · загружена часть памяти; используйте проект и переходы по связям':'');
    el('map-more').hidden=nodes.length>=available;
    const note=el('map-trace-note');
    if(path&&graph.trace){note.textContent='Подсвечены записи, переданные в контекст ответа. Недавних сообщений: '+graph.trace.recent_message_count+
      (graph.trace.changed_memories?' · после ответа изменено воспоминаний: '+graph.trace.changed_memories:'')+
      (graph.trace.missing_nodes?' · удалено или недоступно узлов: '+graph.trace.missing_nodes:'')+
      (graph.trace.legacy?' · старый ответ: сохранены только идентификаторы памяти':'')+'. Показано текущее состояние записей. Это путь данных, а не внутренние рассуждения модели.';}
    else note.textContent='Выберите узел для просмотра источника и связей. Открытые вопросы и гипотезы хранятся отдельно от фактов.';
    el('map-path').setAttribute('aria-pressed',String(path));requestAnimationFrame(drawLines);
  }
  function drawLines(){
    if(!graph||mode!=='graph'||el('tab-map').hidden)return;
    const stage=el('map-stage'),svg=el('map-lines'),area=stage.getBoundingClientRect();
    if(!area.width)return;
    svg.replaceChildren();svg.setAttribute('viewBox','0 0 '+area.width+' '+stage.scrollHeight);
    const cards=new Map([...el('map-nodes').children].map(card=>[card.dataset.nodeId,card]));
    const trace=new Set(path?graph.trace?.node_ids||[]:[]);
    for(const edge of graph.edges){if(!cards.has(edge.source)||!cards.has(edge.target))continue;
      const a=cards.get(edge.source).getBoundingClientRect(),b=cards.get(edge.target).getBoundingClientRect();
      const ac={x:a.left+a.width/2-area.left,y:a.top+a.height/2-area.top},bc={x:b.left+b.width/2-area.left,y:b.top+b.height/2-area.top};
      const dx=bc.x-ac.x,dy=bc.y-ac.y,horizontal=Math.abs(dx)>Math.abs(dy)*1.2;
      const x1=ac.x+(horizontal?Math.sign(dx)*a.width/2:0),y1=ac.y+(horizontal?0:Math.sign(dy)*a.height/2);
      const x2=bc.x-(horizontal?Math.sign(dx)*b.width/2:0),y2=bc.y-(horizontal?0:Math.sign(dy)*b.height/2);
      const line=document.createElementNS('http://www.w3.org/2000/svg','path');
      line.setAttribute('d',horizontal?`M${x1},${y1} C${(x1+x2)/2},${y1} ${(x1+x2)/2},${y2} ${x2},${y2}`:
        `M${x1},${y1} C${x1},${(y1+y2)/2} ${x2},${(y1+y2)/2} ${x2},${y2}`);
      if(trace.has(edge.source)&&trace.has(edge.target))line.classList.add('on-path');
      else if(edge.source===selected||edge.target===selected)line.classList.add('related');svg.append(line);
    }
  }
  async function selectNode(id){selected=id;renderGraph();await loadPassport(id);}
  async function loadPassport(id){
    const version=++passportVersion;
    try{const next=await api('passport?node='+encodeURIComponent(id));if(version!==passportVersion||selected!==id)return;passport=next;renderPassport();}
    catch(error){if(version!==passportVersion)return;passport=null;blank(el('map-passport'),error.message);}
  }
  function renderPassport(){
    const data=passport,container=el('map-passport');container.replaceChildren();if(!data)return;
    container.append(element('div',data.type_label||'Общая память','eyebrow'),element('h3',data.title||'Общая память'));
    container.append(element('p',labels[data.status]||data.status||'','map-content-status'));
    container.append(element('div',data.content,'map-full-content'));
    if(data.type==='memory'){
      container.append(element('p',(data.confirmed?'Подтверждено вами':'Из сообщения пользователя')+' · '+(tiers[data.tier]||data.tier)+' · '+(data.scope==='global'?'Общее':'Проектная запись'),'muted fine'));
      container.append(element('p','Создано: '+date(data.created)+'\nИзменено: '+date(data.updated)+'\nПередано в контекст ответов: '+data.usage.count+(data.usage.last_used?'\nПоследний раз: '+date(data.usage.last_used):''),'muted fine'));
      if(data.note)container.append(element('p','Толкование анализатора: '+data.note,'muted fine'));
      const actions=element('div',undefined,'actions');actions.append(button('Исправить / подтвердить',async()=>{
        tab('memory',{ids:[data.entity_id]});await loadMemories();const row=memoryRows.find(row=>row.id===data.entity_id);if(row)editMemory(row);
      }),button('Забыть',async()=>{await api('memory-delete',{id:data.entity_id});selected='brain';await refresh();await loadGraph();},'quiet danger'));container.append(actions);
    }
    if(data.type==='branch')container.append(button(data.status==='paused'?'Продолжить ветку':'Приостановить ветку',async()=>{
      await api('development-branch',{id:data.entity_id,status:data.status==='paused'?'active':'paused'});await refresh();await loadGraph();
    }));
    if(['question','option','choice','lesson','hypothesis'].includes(data.type)){
      const actions=element('div',undefined,'actions');
      if(data.type==='question')actions.append(button(data.status==='resolved'?'Открыть вопрос снова':'Отметить решённым',async()=>{
        await api('development-entry',{id:data.entity_id,action:data.status==='resolved'?'reopen':'resolve'});await refresh();await loadGraph();
      }));
      actions.append(button('Удалить запись направления',async()=>{await api('development-entry',{id:data.entity_id,action:'delete'});selected='brain';await refresh();await loadGraph();},'quiet danger'));container.append(actions);
      if(['question','hypothesis','option'].includes(data.type))container.append(element('p','Эта запись не является подтверждённым фактом.','muted fine'));
    }
    if(data.type==='message')container.append(button('Открыть сообщение',()=>openMessage(data.chat_id,data.entity_id)));
    if(data.sources?.length){container.append(element('h4','Источники · '+(data.source_count||data.sources.length)));
      for(const source of data.sources.slice(0,15)){const details=element('details');details.append(element('summary',date(source.created)),element('div',source.content,'map-source-text'),button('Открыть сообщение',()=>openMessage(source.chat_id,source.id),'quiet'));container.append(details);}
      if((data.source_count||data.sources.length)>15)container.append(element('p','Показаны последние 15 источников.','muted fine'));
    }else if(data.type!=='brain'&&data.type!=='message')container.append(element('p',data.confirmed?'Основание: ручное подтверждение.':'Исходный источник недоступен.','muted fine'));
    const relations=graph?.edges.filter(edge=>edge.source===data.id||edge.target===data.id)||[];
    if(relations.length){container.append(element('h4','Связи'));
      for(const edge of relations.slice(0,15)){const other=edge.source===data.id?edge.target:edge.source,otherNode=graph.nodes.find(node=>node.id===other);
        if(otherNode)container.append(button((edge.target===data.id?({contains:'Часть',develops:'Направление проекта',grounds:'Источник',answers:'Получен ответ',selects:'Выбран решением',closes:'Закрыто',supersedes:'Заменено'}[edge.kind]||relationLabels[edge.kind]||edge.kind):(edge.kind==='grounds'?'Обосновывает':relationLabels[edge.kind]||edge.kind))+' · '+otherNode.title,()=>selectNode(other),'map-related'));
      }
    }
    if(data.versions?.length){const details=element('details');details.append(element('summary','Версии · последние '+data.versions.length));
      for(const version of data.versions){const row=element('div',undefined,'map-version');row.append(element('small',date(version.created)+' · '+version.reason),element('p',version.snapshot.content),
        button('Восстановить',async()=>{const result=await api('restore-version',{id:version.id});selected='memory:'+result.id;await refresh();await loadGraph(selected);}));details.append(row);}container.append(details);
    }
  }
  async function loadHistory(){const version=++auxVersion;const params=new URLSearchParams();if(el('map-project').value)params.set('project_id',el('map-project').value);
    const result=await api('growth-history?'+params);if(version!==auxVersion||mode!=='history')return;
    const container=el('map-history');container.replaceChildren();for(const event of result.events){const row=element('article',undefined,'map-event panel');row.append(element('small',date(event.created)));
      const content=element('div');content.append(button(event.label,()=>open(event.node_id)),element('p',event.title));row.append(content);container.append(row);}
    if(!result.events.length)blank(container,'История появится после сохранения сведений и изменений памяти.');
  }
  function renderSleep(result){const status=el('map-sleep-status'),summary=result.state;
    status.textContent=(state?.settings.auto_consolidation?'Автоматика включена. ':'Автоматика выключена. ')+(summary.last_run?'Последний запуск: '+date(summary.last_run)+' · рассмотрено записей: '+summary.examined:'Проверка ещё не выполнялась.')+(summary.error?' '+summary.error:'');
    const container=el('map-suggestions');container.replaceChildren();
    for(const suggestion of result.suggestions){const card=element('article',undefined,'map-suggestion panel');card.dataset.suggestionId=suggestion.id;
      card.append(element('h4',suggestion.kind==='similar'?'Похожие формулировки':suggestion.kind==='conflict'?'Возможное противоречие':'Кандидат в архив'),element('blockquote',suggestion.first.content));
      if(suggestion.second)card.append(element('blockquote',suggestion.second.content));
      const actions=element('div',undefined,'actions');
      const act=async actionName=>{await api('suggestion',{id:suggestion.id,action:actionName});await refresh();await loadSleep();};
      if(suggestion.status==='applied'){
        card.append(element('p',suggestion.kind==='similar'?'Объединено в группу. Обе записи и источники сохранены.':'Перенесено в архив. Предыдущий уровень можно восстановить в версиях.','muted'));
        if(suggestion.kind==='similar')actions.append(button('Отменить объединение',()=>act('undo')));
      }else{
        if(suggestion.kind==='similar')actions.append(button('Объединить в группу',()=>act('group')));
        if(suggestion.kind==='archive')actions.append(button('В архив',()=>act('archive')));
        if(suggestion.kind==='conflict')for(const [index,memory] of [suggestion.first,suggestion.second].entries())actions.append(button('Считать актуальной '+(index+1),async()=>{
          const result=await api('memories?ids='+memory.id),row=result.memories[0];if(!row)throw Error('Запись удалена.');
          await api('memory',{id:row.id,content:row.content,kind:row.kind,tier:row.tier,project_id:row.projects[0]?.id||null});await refresh();await loadSleep();
        }));
        actions.append(button('Оставить без изменений',()=>act('dismiss'),'quiet'));
      }
      actions.append(button('Открыть первую запись',()=>open('memory:'+suggestion.first.id),'quiet'));
      if(suggestion.second)actions.append(button('Открыть вторую запись',()=>open('memory:'+suggestion.second.id),'quiet'));card.append(actions);container.append(card);
    }
    if(!result.suggestions.length)blank(container,'Подходящих предложений пока нет. Записи не изменены.');
  }
  async function loadSleep(){const version=++auxVersion;const result=await api('consolidation');if(version!==auxVersion||mode!=='sleep')return;renderSleep(result);}
  async function setMode(next){mode=next;++auxVersion;for(const button of document.querySelectorAll('[data-map-view]'))button.setAttribute('aria-pressed',String(button.dataset.mapView===mode));
    el('map-graph-view').hidden=mode!=='graph';el('map-history-view').hidden=mode!=='history';el('map-sleep-view').hidden=mode!=='sleep';
    if(mode==='history')await loadHistory();else if(mode==='sleep')await loadSleep();else await loadGraph();
  }
  async function show(){active=true;applyFilters();await setMode(mode);}
  async function open(id){tab('map');mode='graph';selected=id;limit=30;for(const button of document.querySelectorAll('[data-map-view]'))button.setAttribute('aria-pressed',String(button.dataset.mapView==='graph'));
    el('map-graph-view').hidden=false;el('map-history-view').hidden=true;el('map-sleep-view').hidden=true;el('map-project').value='';
    if(id.startsWith('message:'))el('map-sources').checked=true;await loadGraph(id);el('map-passport').scrollIntoView({behavior:'smooth',block:'nearest'});
  }
  async function openReply(id){tab('map');mode='graph';el('map-project').value='';path=true;
    // The option may not exist yet when invoked from an older conversation.
    const option=element('option','Выбранный ответ');option.value=id;el('map-reply').append(option);el('map-reply').value=id;
    el('map-graph-view').hidden=false;el('map-history-view').hidden=true;el('map-sleep-view').hidden=true;
    for(const button of document.querySelectorAll('[data-map-view]'))button.setAttribute('aria-pressed',String(button.dataset.mapView==='graph'));
    await loadGraph();
  }
  function stateChanged(next){applyFilters();const changed=revision!==next.development?.revision;revision=next.development?.revision;
    if(!changed||!active||el('tab-map').hidden)return;
    const reply=graph?.replies.find(reply=>reply.reply_id===el('map-reply').value);
    if(reply&&!next.chats.some(chat=>chat.id===reply.chat_id)){el('map-reply').value='';path=false;}
    if(loading){refreshQueued=true;return;}loading=true;
    action(async()=>{try{await setMode(mode);}finally{loading=false;if(refreshQueued){refreshQueued=false;revision=null;stateChanged(state);}}});
  }
  el('map-project').onchange=()=>action(async()=>{el('map-reply').value='';path=false;limit=30;selected='brain';await setMode(mode);});
  el('map-reply').onchange=()=>action(async()=>{el('map-project').value='';path=!!el('map-reply').value;await loadGraph();});
  el('map-path').onclick=()=>{path=!path;renderGraph();};
  el('map-sources').onchange=renderGraph;el('map-more').onclick=()=>{limit+=24;renderGraph();};
  for(const button of document.querySelectorAll('[data-map-view]'))button.onclick=()=>action(()=>setMode(button.dataset.mapView));
  el('map-consolidate').onclick=()=>action(async()=>{el('map-consolidate').disabled=true;try{const result=await api('consolidate',{});renderSleep(result);await refresh();}finally{el('map-consolidate').disabled=false;}});
  new ResizeObserver(drawLines).observe(el('map-nodes'));
  window.BrainMap={show,open,openReply,stateChanged};if(state)stateChanged(state);
})();
