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
  let externalFocus=null,loadedSignature='',pages=new Map(),collapsed=new Set(),flashes=new Map(),flashTimer=null;
  const mobile=matchMedia('(max-width:720px)'),dialog=el('map-mobile-dialog');
  let returnFocus=null;
  const canvas=new window.SayoriMapCanvas(document.querySelector('.map-viewport'),el('map-zoom-space'),el('map-stage'),drawLines);
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
  function updatePickers(){
    const select=el('map-reply'),previous=select.value;
    if(document.activeElement!==select){select.replaceChildren(element('option','Без подсветки ответа'));select.firstChild.value='';
      for(const reply of graph.replies){const option=element('option',date(reply.created)+' · '+reply.prompt);option.value=reply.reply_id;select.append(option);}
      if(previous&&graph.trace?.reply_id===previous&&!graph.replies.some(reply=>reply.reply_id===previous)){const option=element('option',date(graph.trace.created)+' · Выбранный ответ');option.value=previous;select.append(option);}
      select.value=previous;
    }
    el('map-path').disabled=!select.value;
    const goals=el('map-goal'),goal=goals.value;
    if(document.activeElement!==goals){goals.replaceChildren(element('option','Все направления'));goals.firstChild.value='';
      for(const item of graph.goals||[]){const option=element('option',item.title);option.value=item.id;goals.append(option);}goals.value=goal;}
  }
  function query(focus=externalFocus){const params=new URLSearchParams({lazy:'1'});if(el('map-project').value)params.set('project_id',el('map-project').value);
    if(el('map-reply').value)params.set('reply_id',el('map-reply').value);if(el('map-goal').value)params.set('goal_id',el('map-goal').value);
    if(focus)params.set('focus',focus);return params.toString();}
  function pageQuery(parent,cursor){const params=new URLSearchParams({parent,limit:'24'});if(cursor)params.set('cursor',cursor);
    if(el('map-project').value)params.set('project_id',el('map-project').value);if(el('map-goal').value)params.set('goal_id',el('map-goal').value);return params.toString();}
  function merge(target,patch,keepExisting=false){
    const nodes=new Map(target.nodes.map(node=>[node.id,node]));for(const node of patch.nodes)if(!keepExisting||!nodes.has(node.id))nodes.set(node.id,node);target.nodes=[...nodes.values()];
    const key=edge=>edge.source+'|'+edge.target+'|'+edge.kind,edges=new Map(target.edges.map(edge=>[key(edge),edge]));for(const edge of patch.edges)edges.set(key(edge),edge);target.edges=[...edges.values()];
    target.bounded||=patch.bounded;
    if(target.goal_path&&patch.goal_path)target.goal_path.node_ids=[...new Set([...target.goal_path.node_ids,...patch.goal_path.node_ids])];
  }
  function pageState(page){return {...page,ids:new Set(page.node_ids),bundles:[],extra:0,requested:false};}
  function advance(target,response){for(const id of response.page.node_ids)target.ids.add(id);target.cursor=response.page.cursor;target.has_more=response.page.has_more;
    target.through=response.page.through;target.total=response.page.total;target.revision=response.page.revision;target.bundles.push(response);target.extra++;}
  async function loadGraph(focus=externalFocus,retry=0){
    const version=++graphVersion,signature=query(focus),old=signature===loadedSignature?pages:new Map();
    el('map-stage').setAttribute('aria-busy','true');
    try{
      const next=await api('graph?'+signature+(selected!=='brain'?'&retain='+encodeURIComponent(selected):''));if(version!==graphVersion)return;
      const cache=new Map((next.branch_pages||[]).map(page=>[page.parent,pageState(page)]));
      // Preserve materialized pages; re-read them only when their parent changed.
      for(const [parent,previous] of [...old].sort((a,b)=>Number(b[0].startsWith('project:'))-Number(a[0].startsWith('project:')))){
        if(!previous.requested||!next.nodes.some(node=>node.id===parent))continue;
        let current=cache.get(parent);
        if(!current){const response=await api('graph-children?'+pageQuery(parent,null));if(version!==graphVersion)return;
          merge(next,response);current=pageState(response.page);current.bundles.push(response);current.requested=true;cache.set(parent,current);}
        if(previous.revision===current.revision){for(const bundle of previous.bundles)merge(next,bundle,true);
          cache.set(parent,{...previous,total:current.total});
        }else{
          while(current.has_more&&(current.ids.size<previous.ids.size||previous.through&&(!current.through||current.through[0]>previous.through[0]||current.through[0]===previous.through[0]&&current.through[1]>previous.through[1]))){const response=await api('graph-children?'+pageQuery(parent,current.cursor));if(version!==graphVersion)return;
            merge(next,response);advance(current,response);}
          current.requested=true;
        }
      }
      if(version!==graphVersion)return;
      const same=signature===loadedSignature&&graph;
      if(same)markChanges(graph,next);else{flashes.clear();clearTimeout(flashTimer);}
      const view=canvas.capture();graph=next;pages=cache;loadedSignature=signature;externalFocus=focus;updatePickers();
      if(!graph.nodes.some(node=>node.id===selected)){selected=focus||'brain';closeCard();}
      renderGraph(view);await loadPassport(selected);
    }catch(error){
      if(version!==graphVersion)return;
      if(error.status===409&&retry<1)return loadGraph(focus,retry+1);
      if(error.status===404&&retry<1&&(focus||el('map-goal').value||el('map-reply').value)){
        externalFocus=null;el('map-goal').value='';el('map-reply').value='';path=false;selected='brain';closeCard();return loadGraph(null,retry+1);}
      throw error;
    }finally{if(version===graphVersion)el('map-stage').setAttribute('aria-busy','false');}
  }
  async function loadChildren(parent,retry=0){
    const version=++graphVersion,signature=query(),previous=pages.get(parent);
    el('map-stage').setAttribute('aria-busy','true');
    try{
    const response=await api('graph-children?'+pageQuery(parent,previous?.cursor)).catch(async error=>{
      if(version!==graphVersion)return null;
      if((error.status===409||error.status===404)&&retry<1){await loadGraph();if(graph.nodes.some(node=>node.id===parent))await loadChildren(parent,retry+1);return null;}throw error;});
    if(!response||version!==graphVersion||signature!==query())return;
    if(previous&&previous.revision!==response.page.revision){await loadGraph();if(retry<1&&graph.nodes.some(node=>node.id===parent))await loadChildren(parent,retry+1);return;}
    const view=canvas.capture();merge(graph,response);let current=pages.get(parent);
    if(!current){current=pageState(response.page);current.bundles.push(response);current.extra=1;pages.set(parent,current);}else advance(current,response);current.requested=true;
    collapsed.delete(parent);limit=Math.max(limit,graph.nodes.length);renderGraph(view);
    if(passport)renderPassport();
    }finally{if(version===graphVersion)el('map-stage').setAttribute('aria-busy','false');}
  }
  function markChanges(before,after){
    const old=new Map(before.nodes.map(node=>[node.id,node]));let found=false;
    const edges=(data,id)=>data.edges.filter(edge=>edge.source===id||edge.target===id).map(edge=>edge.source+'|'+edge.target+'|'+edge.kind).sort().join('\n');
    for(const node of after.nodes){const was=old.get(node.id);
      const kind=was&&(was.stamp!==node.stamp||edges(before,node.id)!==edges(after,node.id))?'updated':!was&&node.created&&before.snapshot_at&&node.created>before.snapshot_at?'new':null;
      if(kind){flashes.set(node.id,{kind,until:Date.now()+3500});found=true;}}
    if(found){clearTimeout(flashTimer);flashTimer=setTimeout(()=>{flashes.clear();renderGraph();},3600);}
  }
  function hierarchy(){
    const visible=new Set(['brain']);let changed=true;
    while(changed){changed=false;for(const node of graph.nodes){if(visible.has(node.id)||node.type==='message')continue;
      const parents=node.parent_ids||[];
      if(!parents.length||parents.some(id=>visible.has(id)&&!collapsed.has(id))){visible.add(node.id);changed=true;}}}
    for(const node of graph.nodes)if(node.type==='message'&&graph.edges.some(edge=>edge.source===node.id&&edge.kind==='grounds'&&visible.has(edge.target)))visible.add(node.id);
    return visible;
  }
  function ordered(nodes,layout=el('map-layout').value){
    const rank=node=>node.type==='brain'?0:node.type==='project'?1:node.type==='branch'?2:node.type==='progress'&&node.kind==='goal'?3:node.type==='question'?4:node.type==='progress'?5:node.type==='choice'?6:node.type==='memory'?7:8;
    return [...nodes].sort((a,b)=>layout==='sequence'?(a.created||'').localeCompare(b.created||'')||a.id.localeCompare(b.id):rank(a)-rank(b)||(a.created||'').localeCompare(b.created||'')||a.id.localeCompare(b.id));
  }
  function visibleNodes(){
    if(!graph)return [];
    const shown=hierarchy(),all=ordered(graph.nodes.filter(node=>shown.has(node.id)&&(node.type!=='message'||el('map-sources').checked||node.id===selected)));
    const mandatory=new Set([selected,...(path?graph.trace?.node_ids||[]:[])]);
    const chosen=all.slice(0,limit);for(const node of all)if(mandatory.has(node.id)&&!chosen.some(item=>item.id===node.id))chosen.push(node);
    return ordered(chosen);
  }
  function reveal(id){const queue=[id],seen=new Set();while(queue.length){const current=queue.pop();if(seen.has(current))continue;seen.add(current);
    for(const parent of graph.nodes.find(node=>node.id===current)?.parent_ids||[]){collapsed.delete(parent);queue.push(parent);}}}
  async function toggleBranch(id){
    if(collapsed.has(id)){collapsed.delete(id);if(!pages.has(id))await loadChildren(id);else renderGraph();}
    else{collapsed.add(id);const shown=hierarchy();if(!shown.has(selected)){selected=id;await loadPassport(id);}renderGraph();}
  }
  function renderNode(data){
    const wrapper=element('div',undefined,'map-node-wrap');wrapper.dataset.wrapperId=data.id;
    const card=button('',()=>selectNode(data.id),'map-node');card.dataset.nodeId=data.id;card.dataset.type=data.type;
    card.setAttribute('aria-pressed',String(data.id===selected));card.setAttribute('aria-label',(data.type_label||'Запись')+': '+data.title);
    card.classList.toggle('on-path',path&&!!graph.trace?.node_ids.includes(data.id));card.classList.toggle('on-goal',!!graph.goal_path?.node_ids.includes(data.id));
    card.classList.toggle('related',graph.edges.some(edge=>(edge.source===selected&&edge.target===data.id)||(edge.target===selected&&edge.source===data.id)));
    const flash=flashes.get(data.id);if(flash&&flash.until>Date.now()){
      card.classList.add('map-change-'+flash.kind);card.dataset.change=flash.kind;card.style.setProperty('--change-duration',(flash.until-Date.now())+'ms');
      card.append(element('span',flash.kind==='new'?'Добавлено':'Изменено','map-change-badge'));}
    card.append(element('span',data.type==='progress'?progressNames[data.kind]||'Прогресс':data.type_label||'Запись','map-node-type'),
      element('span',data.title,'map-node-title'),element('span',labels[data.status]||data.status||'','map-node-state'));wrapper.append(card);
    if(data.can_expand){const tools=element('div',undefined,'map-node-tools'),page=pages.get(data.id),folded=collapsed.has(data.id);
      const fold=button(folded?'Развернуть':'Свернуть',()=>toggleBranch(data.id),'quiet map-fold');fold.setAttribute('aria-label',(folded?'Развернуть':'Свернуть')+' ветку «'+data.title+'»');fold.setAttribute('aria-expanded',String(!folded));tools.append(fold);
      if(page)tools.append(element('small',page.ids.size+' / '+page.total,'map-page-count'));
      if(!folded&&(!page||page.has_more)){const more=button(page?'Загрузить ещё':'Загрузить записи',()=>loadChildren(data.id),'quiet map-load-children');more.dataset.parentId=data.id;tools.append(more);}
      wrapper.append(tools);
    }
    return wrapper;
  }
  function renderTree(container,nodes){
    const ids=new Set(nodes.map(node=>node.id)),children=new Map(),roots=[];
    for(const node of nodes){let parent=(node.parent_ids||[]).find(id=>ids.has(id));
      if(!parent&&node.type==='message')parent=graph.edges.find(edge=>edge.source===node.id&&edge.kind==='grounds'&&ids.has(edge.target))?.target;
      if(parent&&parent!==node.id){if(!children.has(parent))children.set(parent,[]);children.get(parent).push(node);}else roots.push(node);}
    const seen=new Set();function append(node,target){if(seen.has(node.id))return;seen.add(node.id);const group=element('div',undefined,'map-tree-group');group.append(renderNode(node));
      const next=children.get(node.id)||[];if(next.length){const body=element('div',undefined,'map-tree-children');for(const child of ordered(next))append(child,body);group.append(body);}target.append(group);}
    for(const root of roots)append(root,container);for(const node of nodes)if(!seen.has(node.id))append(node,container);
  }
  function renderGraph(saved=null){
    if(!graph)return;
    const view=saved||canvas.capture(),container=el('map-nodes'),nodes=visibleNodes(),layout=el('map-layout').value;
    container.replaceChildren();container.dataset.layout=layout;
    if(layout==='tree')renderTree(container,nodes);
    else for(const data of nodes){const wrapper=renderNode(data);if(layout==='sequence'){
      const row=element('article',undefined,'map-sequence-row'),time=element('time',date(data.created),'map-sequence-time');if(data.created)time.dateTime=data.created;row.append(time,wrapper);container.append(row);
    }else container.append(wrapper);}
    const shown=hierarchy(),available=graph.nodes.filter(node=>shown.has(node.id)&&(node.type!=='message'||el('map-sources').checked||node.id===selected)).length;
    el('map-count').textContent='Видно '+nodes.length+' из '+available+' загруженных узлов'+(graph.bounded?' · часть связей доступна при раскрытии веток':'');
    el('map-more').hidden=nodes.length>=available;
    const note=el('map-trace-note');
    if(path&&graph.trace){note.textContent='Подсвечены записи, переданные в контекст ответа. Недавних сообщений: '+graph.trace.recent_message_count+
      (graph.trace.changed_memories?' · после ответа изменено воспоминаний: '+graph.trace.changed_memories:'')+
      (graph.trace.missing_nodes?' · удалено или недоступно узлов: '+graph.trace.missing_nodes:'')+
      (graph.trace.legacy?' · старый ответ: сохранены только идентификаторы памяти':'')+'. Показано текущее состояние записей. Это путь данных, а не внутренние рассуждения модели.';}
    else note.textContent='Выберите узел для просмотра источника и связей. Открытые вопросы и гипотезы хранятся отдельно от фактов.';
    const goal=graph.goal_path;el('map-goal-note').hidden=!goal;
    if(goal)el('map-goal-note').textContent=goal.title+' · задач: '+goal.counts.tasks+', открытых: '+goal.counts.open_tasks+', решений: '+goal.counts.decisions+', сообщений о выполнении: '+goal.counts.done_reports+'. '+goal.note;
    el('map-layout-note').textContent=(layout==='sequence'?'Записи по времени создания; порядок не означает зависимость. ':layout==='tree'?'Вложенность по сохранённым связям; общая запись показана один раз. ':'')+'Колесо с Ctrl · масштаб жестом двух пальцев.';
    el('map-path').setAttribute('aria-pressed',String(path));canvas.sync();canvas.restore(view);
  }
  function drawLines(){
    if(!graph||mode!=='graph'||el('tab-map').hidden)return;
    const stage=el('map-stage'),svg=el('map-lines'),area=stage.getBoundingClientRect(),scale=canvas.zoom;
    if(!area.width)return;
    svg.replaceChildren();svg.setAttribute('viewBox','0 0 '+stage.offsetWidth+' '+stage.offsetHeight);
    const cards=new Map([...el('map-nodes').querySelectorAll('.map-node')].map(card=>[card.dataset.nodeId,card]));
    const trace=new Set(path?graph.trace?.node_ids||[]:[]),goal=new Set(graph.goal_path?.node_ids||[]);
    for(const edge of graph.edges){if(!cards.has(edge.source)||!cards.has(edge.target))continue;
      if(el('map-layout').value==='tree'&&!['contains','develops','belongs','remembers','grounds'].includes(edge.kind)&&edge.source!==selected&&edge.target!==selected&&!graph.goal_path)continue;
      const a=cards.get(edge.source).getBoundingClientRect(),b=cards.get(edge.target).getBoundingClientRect();
      const ac={x:(a.left+a.width/2-area.left)/scale,y:(a.top+a.height/2-area.top)/scale},bc={x:(b.left+b.width/2-area.left)/scale,y:(b.top+b.height/2-area.top)/scale};
      const dx=bc.x-ac.x,dy=bc.y-ac.y,horizontal=Math.abs(dx)>Math.abs(dy)*1.2;
      const x1=ac.x+(horizontal?Math.sign(dx)*a.width/scale/2:0),y1=ac.y+(horizontal?0:Math.sign(dy)*a.height/scale/2);
      const x2=bc.x-(horizontal?Math.sign(dx)*b.width/scale/2:0),y2=bc.y-(horizontal?0:Math.sign(dy)*b.height/scale/2);
      const line=document.createElementNS('http://www.w3.org/2000/svg','path');
      line.setAttribute('d',horizontal?`M${x1},${y1} C${(x1+x2)/2},${y1} ${(x1+x2)/2},${y2} ${x2},${y2}`:
        `M${x1},${y1} C${x1},${(y1+y2)/2} ${x2},${(y1+y2)/2} ${x2},${y2}`);
      if(trace.has(edge.source)&&trace.has(edge.target))line.classList.add('on-path');
      else if(goal.has(edge.source)&&goal.has(edge.target))line.classList.add('on-goal');
      else if(edge.source===selected||edge.target===selected)line.classList.add('related');svg.append(line);
    }
  }
  async function selectNode(id){selected=id;reveal(id);renderGraph();await loadPassport(id);if(selected===id&&mobile.matches)openCard();}
  function openCard(){
    if(!mobile.matches){el('map-passport').scrollIntoView({behavior:'smooth',block:'nearest'});return;}
    if(dialog.open)return;returnFocus=document.activeElement;el('map-mobile-content').append(el('map-passport'));dialog.showModal();el('map-close-card').focus();
  }
  function closeCard(){if(dialog.open)dialog.close();}
  dialog.addEventListener('close',()=>{el('map-passport-home').append(el('map-passport'));
    const target=returnFocus?.isConnected?returnFocus:[...el('map-nodes').querySelectorAll('.map-node')].find(card=>card.dataset.nodeId===selected);target?.focus({preventScroll:true});});
  mobile.addEventListener('change',()=>{if(!mobile.matches)closeCard();canvas.sync();});
  el('map-close-card').onclick=closeCard;el('map-open-card').onclick=openCard;
  async function loadPassport(id){
    const version=++passportVersion;
    if(passport?.id!==id){passport=null;blank(el('map-passport'),'Загрузка карточки…');}
    try{const next=await api('passport?node='+encodeURIComponent(id));if(version!==passportVersion||selected!==id)return;if(JSON.stringify(next)===JSON.stringify(passport))return;passport=next;renderPassport();}
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
        closeCard();tab('memory',{ids:[data.entity_id]});await loadMemories();const row=memoryRows.find(row=>row.id===data.entity_id);if(row)editMemory(row);
      }),button('Забыть',async()=>{await api('memory-delete',{id:data.entity_id});selected='brain';externalFocus=null;closeCard();await refresh();await loadGraph();},'quiet danger'));container.append(actions);
    }
    if(data.type==='branch')container.append(button(data.status==='paused'?'Продолжить ветку':'Приостановить ветку',async()=>{
      await api('development-branch',{id:data.entity_id,status:data.status==='paused'?'active':'paused'});await refresh();await loadGraph();
    }));
    if(['question','option','choice','lesson','hypothesis'].includes(data.type)){
      const actions=element('div',undefined,'actions');
      if(data.type==='question')actions.append(button(data.status==='resolved'?'Открыть вопрос снова':'Отметить решённым',async()=>{
        await api('development-entry',{id:data.entity_id,action:data.status==='resolved'?'reopen':'resolve'});await refresh();await loadGraph();
      }));
      actions.append(button('Удалить запись направления',async()=>{await api('development-entry',{id:data.entity_id,action:'delete'});selected='brain';externalFocus=null;closeCard();await refresh();await loadGraph();},'quiet danger'));container.append(actions);
      if(['question','hypothesis','option'].includes(data.type))container.append(element('p','Эта запись не является подтверждённым фактом.','muted fine'));
    }
    if(data.type==='progress'&&data.kind==='goal')container.append(button('Путь к результату',()=>openGoal(data.entity_id)));
    if(data.type==='message')container.append(button('Открыть сообщение',()=>{closeCard();return openMessage(data.chat_id,data.entity_id);}));
    if(data.sources?.length){container.append(element('h4','Источники · '+(data.source_count||data.sources.length)));
      for(const source of data.sources.slice(0,15)){const details=element('details');details.append(element('summary',date(source.created)),element('div',source.content,'map-source-text'),button('Открыть сообщение',()=>{closeCard();return openMessage(source.chat_id,source.id);},'quiet'));container.append(details);}
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
  function displayMode(next){mode=next;++auxVersion;for(const button of document.querySelectorAll('[data-map-view]'))button.setAttribute('aria-pressed',String(button.dataset.mapView===mode));
    el('map-graph-view').hidden=mode!=='graph';el('map-history-view').hidden=mode!=='history';el('map-sleep-view').hidden=mode!=='sleep';}
  async function setMode(next){closeCard();displayMode(next);if(mode==='history')await loadHistory();else if(mode==='sleep')await loadSleep();else await loadGraph();}
  async function show(){active=true;applyFilters();await setMode(mode);}
  async function open(id){tab('map');displayMode('graph');selected=id;limit=30;el('map-project').value='';el('map-goal').value='';el('map-reply').value='';path=false;
    if(id.startsWith('message:'))el('map-sources').checked=true;await loadGraph(id);reveal(id);renderGraph();if(mobile.matches)openCard();else el('map-passport').scrollIntoView({behavior:'smooth',block:'nearest'});}
  async function openReply(id){tab('map');closeCard();displayMode('graph');el('map-project').value='';el('map-goal').value='';externalFocus=null;path=true;
    const option=element('option','Выбранный ответ');option.value=id;el('map-reply').append(option);el('map-reply').value=id;
    await loadGraph(null);for(const key of graph.trace?.node_ids||[])reveal(key);renderGraph();}
  async function openGoal(id){tab('map');closeCard();displayMode('graph');el('map-project').value='';el('map-reply').value='';externalFocus=null;path=false;
    const option=element('option','Выбранная цель');option.value=id;el('map-goal').append(option);el('map-goal').value=id;selected='progress:'+id;
    await loadGraph(null);reveal(selected);renderGraph();}
  function stateChanged(next){applyFilters();const changed=revision!==next.development?.revision;revision=next.development?.revision;
    if(!changed||!active||el('tab-map').hidden)return;
    const reply=graph?.replies.find(reply=>reply.reply_id===el('map-reply').value);
    if(reply&&!next.chats.some(chat=>chat.id===reply.chat_id)){el('map-reply').value='';path=false;}
    if(loading){refreshQueued=true;return;}loading=true;
    action(async()=>{try{if(mode==='graph')await loadGraph();else if(mode==='history')await loadHistory();else await loadSleep();}
      finally{loading=false;if(refreshQueued){refreshQueued=false;revision=null;stateChanged(state);}}});
  }
  el('map-project').onchange=()=>action(async()=>{el('map-reply').value='';el('map-goal').value='';externalFocus=null;path=false;limit=30;selected='brain';await setMode(mode);});
  el('map-reply').onchange=()=>action(async()=>{el('map-project').value='';el('map-goal').value='';externalFocus=null;path=!!el('map-reply').value;await loadGraph(null);if(path)for(const key of graph.trace?.node_ids||[])reveal(key);renderGraph();});
  el('map-goal').onchange=()=>action(async()=>{el('map-reply').value='';externalFocus=null;path=false;await loadGraph(null);if(graph.goal_path)for(const key of graph.goal_path.node_ids)reveal(key);renderGraph();});
  el('map-layout').onchange=()=>renderGraph();
  el('map-path').onclick=()=>{path=!path;if(path)for(const key of graph.trace?.node_ids||[])reveal(key);renderGraph();};
  el('map-sources').onchange=()=>renderGraph();el('map-more').onclick=()=>{limit+=24;renderGraph();};
  el('map-zoom-in').onclick=()=>canvas.setZoom(canvas.zoom+.1);el('map-zoom-out').onclick=()=>canvas.setZoom(canvas.zoom-.1);el('map-zoom-reset').onclick=()=>canvas.setZoom(1);
  for(const button of document.querySelectorAll('[data-map-view]'))button.onclick=()=>action(()=>setMode(button.dataset.mapView));
  el('map-consolidate').onclick=()=>action(async()=>{el('map-consolidate').disabled=true;try{const result=await api('consolidate',{});renderSleep(result);await refresh();}finally{el('map-consolidate').disabled=false;}});
  window.BrainMap={show,open,openReply,openGoal,stateChanged};if(state)stateChanged(state);
})();
