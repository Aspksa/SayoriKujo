'use strict';
(() => {
  const el=id=>document.getElementById(id),body=document.body;
  const mobile=matchMedia('(max-width:720px)'),sidebar=el('app-sidebar'),toggle=el('menu-toggle'),backdrop=el('nav-backdrop');
  let menuReturn=null,dialogReturn=null;
  const preference=(key,fallback)=>{try{return localStorage.getItem('sayori.ui.'+key)||fallback;}catch{return fallback;}};
  const save=(key,value)=>{try{localStorage.setItem('sayori.ui.'+key,value);}catch{}};
  let lastMobile=null;
  let compact=preference('sidebar','full')==='compact';
  function setMenu(open,restore=true){
    if(!mobile.matches)return;
    if(open){menuReturn=document.activeElement;body.classList.add('menu-open');backdrop.hidden=false;el('main-content').inert=true;toggle.setAttribute('aria-expanded','true');el('nav-close').focus();}
    else{body.classList.remove('menu-open');backdrop.hidden=true;el('main-content').inert=false;toggle.setAttribute('aria-expanded','false');if(restore&&menuReturn?.isConnected)menuReturn.focus({preventScroll:true});}
  }
  function layout(){
    body.classList.toggle('sidebar-compact',compact&&!mobile.matches);
    body.classList.remove('menu-open');backdrop.hidden=true;el('main-content').inert=false;
    sidebar.inert=mobile.matches;toggle.setAttribute('aria-label',mobile.matches?'Открыть меню':compact?'Развернуть меню':'Свернуть меню');toggle.setAttribute('aria-expanded',String(!mobile.matches&&!compact));
    if(lastMobile!==mobile.matches){const browser=el('chat-browser');if(browser)browser.open=!mobile.matches;const mapOptions=document.querySelector('.map-options');if(mapOptions)mapOptions.open=!mobile.matches;lastMobile=mobile.matches;}
  }
  if(sidebar&&toggle){
    toggle.onclick=()=>{if(mobile.matches){sidebar.inert=false;setMenu(true);}else{compact=!compact;save('sidebar',compact?'compact':'full');layout();}};
    const close=()=>{setMenu(false);sidebar.inert=mobile.matches;};el('nav-close').onclick=close;backdrop.onclick=close;
    sidebar.addEventListener('click',event=>{if(event.target.closest('a')&&mobile.matches)close();});
    document.addEventListener('keydown',event=>{
      if(!body.classList.contains('menu-open'))return;
      if(event.key==='Escape'){event.preventDefault();close();return;}
      if(event.key==='Tab'){
        const focusable=[...sidebar.querySelectorAll('button,a')].filter(x=>x.getClientRects().length&&!x.disabled),first=focusable[0],last=focusable.at(-1);
        if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
      }
    });mobile.addEventListener('change',layout);layout();
  }
  // Read-only launch history filters and home counters share the shell.
  if(body.dataset.page){
    const filter=el('history-filter');if(filter)filter.onchange=()=>{let shown=0;for(const card of document.querySelectorAll('.session-card')){card.hidden=!!filter.value&&card.dataset.status!==filter.value;if(!card.hidden)shown++;}el('history-visible').textContent='Показано сеансов: '+shown;el('history-no-results').hidden=shown!==0;};
    if(body.dataset.page==='home'){
      fetch('/api/brain/state').then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{
        for(const [id,value] of [['home-memory',data.growth.memories],['home-projects',data.projects.length],['home-links',data.neural.links],['home-messages',data.growth.messages]])el(id).textContent=value;
        el('home-state').textContent=data.has_key&&data.settings.model?'Cloud.ru настроен':'Настройте Cloud.ru для общения';
        el('home-state').dataset.connected=String(!!(data.has_key&&data.settings.model));
      }).catch(()=>{el('home-state').textContent='Не удалось прочитать состояние';for(const id of ['home-memory','home-projects','home-links','home-messages'])el(id).textContent='—';});
    }
    return;
  }
  const pages={chat:['Разговоры','ОБЩЕНИЕ','Мысли, которые остаются с вами.'],memory:['Память','ЗНАНИЯ','Важное из разговоров — в одном месте.'],map:['Карта развития','РАЗВИТИЕ','Смотрите, как связаны ваши идеи и знания.'],projects:['Проекты','ПЛАНЫ','Цели, решения и место, где вы остановились.'],profile:['Профиль','ПРЕДПОЧТЕНИЯ','Что стоит учитывать в общении с вами.'],checks:['Проверки памяти','НАДЁЖНОСТЬ','Поиск и источники сохранённых сведений.'],settings:['Cloud.ru','НАСТРОЙКИ','Подключение моделей и автоматическая память.']};
  const dialog=el('navigation-dialog'),search=el('navigation-search'),results=el('navigation-results');
  function closePalette(){dialog.close();if(dialogReturn?.isConnected&&!body.classList.contains('menu-open'))dialogReturn.focus({preventScroll:true});}
  function renderPalette(){
    results.replaceChildren();const query=search.value.trim().toLocaleLowerCase('ru-RU');
    for(const [key,[title,,description]] of Object.entries(pages)){
      if(![title,description].join(' ').toLocaleLowerCase('ru-RU').includes(query))continue;
      const button=document.createElement('button');button.type='button';const icon=document.querySelector('[data-tab="'+key+'"] .icon').cloneNode(true);button.append(icon,document.createTextNode(title));button.onclick=()=>{closePalette();tab(key);el('page-title').focus({preventScroll:true});};results.append(button);
    }
    for(const [label,path] of [['Главная','/'],['История запусков','/history']]){if(!label.toLocaleLowerCase('ru-RU').includes(query))continue;const link=document.createElement('a');link.href=path;link.textContent=label;results.append(link);}
    if(!results.children.length){const empty=document.createElement('p');empty.className='muted fine';empty.textContent='Такого раздела нет. Попробуйте другое название.';results.append(empty);}
  }
  function openPalette(){
    if(body.classList.contains('menu-open')){setMenu(false,false);sidebar.inert=mobile.matches;}
    if(dialog.open)return;dialogReturn=document.activeElement;search.value='';renderPalette();dialog.showModal();search.focus();
  }
  el('open-navigation').onclick=openPalette;el('close-navigation').onclick=closePalette;
  dialog.addEventListener('cancel',event=>{event.preventDefault();closePalette();});
  search.oninput=renderPalette;search.onkeydown=event=>{if(event.key==='Enter'){const first=results.querySelector('button,a');if(first){event.preventDefault();first.click();}}else if(event.key==='ArrowDown'){event.preventDefault();results.querySelector('button,a')?.focus();}};
  results.onkeydown=event=>{if(!['ArrowDown','ArrowUp'].includes(event.key))return;const items=[...results.querySelectorAll('button,a')],index=items.indexOf(document.activeElement);event.preventDefault();if(event.key==='ArrowUp'&&index<=0)search.focus();else items[(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length]?.focus();};
  document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLocaleLowerCase()==='k'){event.preventDefault();if(dialog.open)closePalette();else openPalette();}});
  el('chat-search').oninput=renderChatList;
  for(const id of ['projects-search','profile-search'])el(id).oninput=()=>{if(state)renderLearning();};
  function density(value){el('memories').dataset.density=value;el('memory-cards').setAttribute('aria-pressed',String(value==='cards'));el('memory-compact').setAttribute('aria-pressed',String(value==='compact'));save('memory-density',value);}
  el('memory-cards').onclick=()=>density('cards');el('memory-compact').onclick=()=>density('compact');density(preference('memory-density','cards')==='compact'?'compact':'cards');
  function hideKey(){el('api-key').type='password';el('reveal-key').textContent='Показать';el('reveal-key').setAttribute('aria-pressed','false');el('reveal-key').setAttribute('aria-label','Показать API-ключ');}
  el('reveal-key').onclick=()=>{const show=el('api-key').type==='password';el('api-key').type=show?'text':'password';el('reveal-key').textContent=show?'Скрыть':'Показать';el('reveal-key').setAttribute('aria-pressed',String(show));el('reveal-key').setAttribute('aria-label',show?'Скрыть API-ключ':'Показать API-ключ');};
  const dirty=()=>{el('settings-status').textContent='Есть несохранённые изменения';el('settings-status').dataset.dirty='true';};el('settings-form').addEventListener('input',dirty);el('settings-form').addEventListener('change',dirty);
  window.BrainDesign={
    hideKey,
    settingsSaved(){el('settings-status').textContent='Настройки сохранены';el('settings-status').dataset.dirty='false';hideKey();},
    tabChanged(name){const data=pages[name];if(!data)return;el('page-section').textContent=data[1];el('page-description').textContent=data[2];if(body.classList.contains('menu-open')){setMenu(false,false);sidebar.inert=mobile.matches;el('page-title').focus({preventScroll:true});}history.replaceState(null,'','#'+name);},
    stateChanged(data){const n=data.neural;if(!n)return;el('neural-summary').textContent=n.links+' автосвязей · '+n.vault+' закреплённых · в очереди '+n.pending;
      el('neural-status').textContent='Узлы памяти: '+n.nodes+' · Автосвязи: '+n.links+' · Особо долговременных: '+n.vault+' · В очереди: '+n.pending+' · База: '+(n.bytes/1048576).toFixed(1)+' МБ · Свободно: '+(n.free_bytes/1073741824).toFixed(1)+' ГБ'+(n.error?' · '+n.error:'')+'. Узлы и связи являются локальными записями знаний; веса облачной модели не меняются.';
    }
  };
  window.addEventListener('hashchange',()=>{const next=location.hash.slice(1);if(pages[next]&&next!==activeTab)tab(next);});
  if(state){window.BrainDesign.stateChanged(state);window.BrainDesign.tabChanged(activeTab);}
})();
