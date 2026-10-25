const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs/promises');

(async()=>{
  const child=spawn('python',['-u',path.join(__dirname,'brain_ui_server.py')],{stdio:['ignore','pipe','pipe']});
  let output='',stderr='';child.stderr.on('data',data=>{stderr+=data;});
  let browser,page,releaseStale;
  try{
    const url=await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(Error('QA server did not start: '+stderr)),20000);
      child.stdout.on('data',data=>{output+=data;const match=output.match(/http:\/\/127\.0\.0\.1:\d+\/brain/);if(match){clearTimeout(timeout);resolve(match[0]);}});
      child.on('exit',code=>{clearTimeout(timeout);reject(Error('QA server exited: '+code+' '+stderr));});
    });
    browser=await chromium.launch({channel:'chrome',headless:true,args:['--no-sandbox']});
    page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    const navigate=async name=>{
      if(await page.evaluate(()=>matchMedia('(max-width:720px)').matches&&!document.body.classList.contains('menu-open')))await page.locator('#menu-toggle').click();
      await page.locator('[data-tab='+name+']').click();
      await page.waitForFunction(value=>document.body.dataset.view===value,name);
    };
    await page.goto(url);
    await page.waitForFunction(()=>document.getElementById('key-status').textContent.includes('не сохранён'));
    await page.locator('#api-key').fill('test-private-key-1234567890');
    await page.locator('#model').fill('chat-model');
    await page.locator('#settings-form button[type=submit]').click();
    await page.waitForFunction(()=>document.getElementById('key-status').textContent.startsWith('Ключ сохранён.'));
    await navigate('chat');
    let responseLost=false;
    const lostResponse=async route=>{
      if(responseLost){await route.continue();return;}
      responseLost=true;
      await route.fetch(); // Commit the answer, then imitate failure to deliver its response.
      await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Временная ошибка чтения ответа'})});
    };
    await page.route('**/api/brain/chat',lostResponse);
    await page.locator('#message').fill('Мне нужен русский интерфейс.');
    await page.locator('#send').click();
    await page.locator('.bubble.assistant').waitFor();
    await page.waitForFunction(()=>!document.getElementById('send').disabled);
    if(await page.locator('#message').inputValue()!=='')throw Error('Saved answer was not reconciled after lost response');
    if(!await page.locator('#notice').evaluate(el=>el.hidden))throw Error('Saved answer is still reported as failure');
    await page.unroute('**/api/brain/chat',lostResponse);
    await page.waitForFunction(()=>document.getElementById('n-memory').textContent==='1',null,{timeout:20000});
    await page.locator('[data-memory-status=done]').waitFor();
    const screenshots=process.env.UI_SCREENSHOT_DIR;
    if(screenshots){await fs.mkdir(screenshots,{recursive:true});await page.screenshot({path:path.join(screenshots,'chat.png'),fullPage:true});}
    await page.getByRole('button',{name:'Что сохранено',exact:true}).click();
    await page.locator('#memory-scope').waitFor();
    await page.locator('#memories .memory-card').waitFor();
    if(await page.locator('#memories .memory-card').count()!==1)throw Error('Source filter returned unrelated memories');
    await page.locator('#clear-memory-scope').click();
    await page.waitForFunction(()=>document.getElementById('memory-scope').hidden);
    await page.getByRole('button',{name:'Исправить / подтвердить'}).click();
    await page.locator('#memory-tier').selectOption('core');
    await page.locator('#memory-editor button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#memories .memory-card')?.textContent.includes('Подтверждено'));
    if(screenshots)await page.screenshot({path:path.join(screenshots,'memory.png'),fullPage:true});
    await navigate('chat');
    // A state response captured before chat creation must not erase the new selection.
    const stale=await page.evaluate(()=>api('state'));
    let seenStale;
    const staleStarted=new Promise(resolve=>{seenStale=resolve;});
    const release=new Promise(resolve=>{releaseStale=resolve;});
    let intercepted=false;
    const handler=async route=>{
      if(intercepted){await route.continue();return;}
      intercepted=true;seenStale();await release;
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(stale)});
    };
    await page.route('**/api/brain/state',handler);
    await page.evaluate(()=>{
      window.__oldRefreshFinished=false;
      void refresh().finally(()=>{window.__oldRefreshFinished=true;});
    });
    await staleStarted;
    await page.locator('#new-chat').click();
    await page.waitForFunction(()=>document.querySelectorAll('.chat-item').length===2
      &&document.querySelector('.chat-item.active')?.textContent==='Новый разговор'
      &&!document.getElementById('send').disabled);
    releaseStale();
    await page.waitForFunction(()=>window.__oldRefreshFinished);
    if(await page.locator('.chat-item.active').textContent()!=='Новый разговор')throw Error('Stale refresh changed selected chat');
    await page.unroute('**/api/brain/state',handler);
    await page.locator('#message').fill('Что я предпочитаю?');
    await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('.bubble.assistant')?.textContent.includes('использовано записей памяти: 1'));
    await page.getByRole('button',{name:'использовано записей памяти: 1',exact:true}).click();
    await page.locator('#memory-scope').waitFor();
    await page.locator('#memories .memory-card').waitFor();
    if(await page.locator('#memories .memory-card').count()!==1)throw Error('Reply memory filter returned unrelated entries');
    await page.getByRole('button',{name:'История',exact:true}).click();
    await page.locator('#memory-history').waitFor();
    if(await page.locator('#versions .version-row').count()<2)throw Error('Memory version history is incomplete');
    await page.locator('#versions button').first().click();
    await page.waitForFunction(()=>document.getElementById('notice').textContent==='Версия памяти восстановлена.');
    await page.locator('#close-versions').click();
    await navigate('profile');
    if(await page.locator('#profile .memory-card').count()!==1)throw Error('Global profile was not generated');
    await navigate('chat');
    await page.locator('#message').fill('Проект Sakura. Цель: сделать помощницу. Осталось: добавить поиск. Остановились: на памяти.');
    await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('#chat-project option:checked')?.textContent==='Sakura',null,{timeout:20000});
    await page.locator('#message').fill('Сделано: добавлен поиск. Остановились: на проверках.');
    await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('#projects')?.textContent.includes('Остановились: на проверках.'),null,{timeout:20000});
    await navigate('projects');
    if(await page.locator('#projects .project-card').count()!==1)throw Error('Project was duplicated');
    const board=await page.locator('#projects').textContent();
    if(!board.includes('Сделано: добавлен поиск.')||board.includes('Осталось: добавить поиск.')||board.includes('Остановились: на памяти.'))throw Error('Project progress was not updated');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'projects.png'),fullPage:true});
    await page.getByRole('button',{name:'Память проекта',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('memory-scope-label').textContent.includes('Sakura'));
    await page.waitForFunction(()=>document.querySelectorAll('#memories .memory-card').length===1&&document.getElementById('memories').textContent.includes('Осталось: добавить поиск.'));
    if(!(await page.locator('#memories').textContent()).includes('Осталось: добавить поиск.'))throw Error('Project memory filter failed');
    await navigate('checks');
    await page.locator('#run-checks').click();
    await page.waitForFunction(()=>!document.getElementById('run-checks').disabled&&document.getElementById('checks-result').textContent.includes('Пройдено'));
    if(await page.locator('#checks-list .check-case').count()!==1)throw Error('Automatic checks were not generated');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'checks.png'),fullPage:true});
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#chat-project option:checked')?.textContent==='Sakura');
    await navigate('chat');await page.locator('#handoff > summary').click();
    if(!(await page.locator('#handoff-content').textContent()).includes('Остановились: на проверках.'))throw Error('Handoff was lost after reload');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'handoff.png'),fullPage:true});
    await page.locator('#message').fill('Ветка Голос. Хочу русскую озвучку. Какой тембр выбрать? Вариант: низкий голос. Вариант: мягкий голос.');
    await page.locator('#send').click();
    await page.waitForFunction(async()=>{const graph=await api('graph');return graph.nodes.some(node=>node.type==='question');},null,{timeout:20000});
    await page.waitForFunction(()=>!document.getElementById('send').disabled);
    await page.locator('#message').fill('Выбираю мягкий голос.');
    await page.locator('#send').click();
    await page.waitForFunction(async()=>{const graph=await api('graph');return graph.edges.some(edge=>edge.kind==='answers');},null,{timeout:20000});
    await page.waitForFunction(()=>!document.getElementById('send').disabled);
    await page.locator('.bubble.assistant').last().getByRole('button',{name:'Путь памяти',exact:true}).click();
    await page.locator('#map-nodes .map-node.on-path').first().waitFor();
    if(!(await page.locator('#map-trace-note').textContent()).includes('переданные в контекст'))throw Error('Actual memory trace missing');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'growth-path.png'),fullPage:true});
    await page.locator('#map-nodes .map-node[data-type=question]').click();
    await page.getByRole('button',{name:'Открыть вопрос снова',exact:true}).waitFor();
    if(!(await page.locator('#map-passport').textContent()).includes('Какой тембр выбрать?'))throw Error('Question passport is missing');
    await page.getByRole('button',{name:'Открыть вопрос снова',exact:true}).click();
    await page.getByRole('button',{name:'Отметить решённым',exact:true}).waitFor();
    await page.getByRole('button',{name:'Отметить решённым',exact:true}).click();
    await page.getByRole('button',{name:'Открыть вопрос снова',exact:true}).waitFor();
    await page.locator('#map-path').click();
    if(screenshots)await page.screenshot({path:path.join(screenshots,'growth-map.png'),fullPage:true});
    await page.locator('[data-map-view=history]').click();
    await page.locator('#map-history .map-event').first().waitFor();
    if(!(await page.locator('#map-history').textContent()).includes('Создана ветка развития'))throw Error('Growth history is missing');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'growth-history.png'),fullPage:true});
    await navigate('chat');
    for(const text of ['Я предпочитаю короткие ответы без длинных вступлений.','Я предпочитаю короткие ответы без длинных вступлений и повторов.']){
      await page.locator('#message').fill(text);await page.locator('#send').click();
      await page.waitForFunction(async text=>{const result=await api('memories');return result.memories.some(row=>row.content===text);},text,{timeout:20000});
      await page.waitForFunction(()=>!document.getElementById('send').disabled);
    }
    await navigate('map');
    await page.locator('[data-map-view=sleep]').click();
    await page.locator('#map-consolidate').click();
    await page.getByRole('button',{name:'Объединить в группу',exact:true}).waitFor();
    await page.getByRole('button',{name:'Объединить в группу',exact:true}).click();
    await page.getByRole('button',{name:'Отменить объединение',exact:true}).waitFor();
    if(screenshots)await page.screenshot({path:path.join(screenshots,'growth-sleep.png'),fullPage:true});
    await page.getByRole('button',{name:'Отменить объединение',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#map-suggestions')?.textContent.includes('Отменить объединение'));
    const preserved=await page.evaluate(async()=>{const result=await api('memories');return result.memories.filter(row=>row.content.startsWith('Я предпочитаю короткие ответы'));});
    if(preserved.length!==2||preserved.some(row=>!row.sources.length))throw Error('Grouping lost original entries or sources');
    await page.locator('[data-map-view=graph]').click();
    await page.locator('#map-nodes .map-node').first().waitFor();
    // The seven selected map improvements use live data and read-only pagination.
    await page.locator('#map-layout').selectOption('tree');
    await page.locator('.map-tree-children').first().waitFor();
    await page.locator('#map-goal').selectOption({index:1});
    await page.waitForFunction(()=>!document.getElementById('map-goal-note').hidden&&document.querySelector('.map-node.on-goal'));
    if(await page.locator('#map-nodes .map-node[data-type=question]').count())throw Error('Goal path included an unrelated branch');
    if(!(await page.locator('#map-goal-note').textContent()).includes('открытых: 0'))throw Error('Goal counters are incorrect');
    const goalProject=page.locator('.map-node-wrap').filter({has:page.locator('.map-node[data-type=project]')});
    if(await goalProject.locator('.map-load-children').count())throw Error('Goal path offered unrelated project pagination');
    await goalProject.locator('.map-fold').click();await goalProject.locator('.map-fold').click();
    await page.locator('.map-node[data-type=progress]').first().waitFor();
    if(screenshots)await page.screenshot({path:path.join(screenshots,'map-goal-tree.png'),fullPage:true});
    await page.locator('#map-layout').selectOption('sequence');
    const orderedTimes=await page.locator('.map-sequence-time[datetime]').evaluateAll(items=>items.map(item=>Date.parse(item.dateTime)));
    if(orderedTimes.some((value,index)=>index&&value<orderedTimes[index-1]))throw Error('Sequence is not chronological');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'map-sequence.png'),fullPage:true});
    await page.locator('#map-goal').selectOption('');
    await page.waitForFunction(()=>document.getElementById('map-goal-note').hidden&&document.querySelector('.map-node[data-type=question]'));
    await page.locator('#map-layout').selectOption('network');
    await page.locator('#map-zoom-in').click();
    if(await page.locator('#map-zoom-value').textContent()!=='110%')throw Error('Zoom button failed');
    await page.locator('.map-viewport').scrollIntoViewIfNeeded();
    const viewportBox=await page.locator('.map-viewport').boundingBox();
    await page.mouse.move(viewportBox.x+100,viewportBox.y+60);
    await page.keyboard.down('Control');await page.mouse.wheel(0,-120);await page.keyboard.up('Control');
    await page.waitForFunction(()=>parseInt(document.getElementById('map-zoom-value').textContent)>110);
    await page.locator('#map-zoom-reset').click();
    if(await page.locator('#map-zoom-value').textContent()!=='100%')throw Error('Zoom reset failed');
    const voice=await page.evaluate(async()=>{const result=await api('graph?lazy=1');return result.nodes.find(node=>node.type==='branch'&&node.title==='Голос').id;});
    const voiceWrap=page.locator('[data-wrapper-id="'+voice+'"]');
    await voiceWrap.locator('.map-fold').click();
    if(await page.locator('#map-nodes .map-node[data-type=question]').count())throw Error('Collapsed branch still shows children');
    await voiceWrap.locator('.map-fold').click();
    await page.locator('#map-nodes .map-node[data-type=question]').waitFor();
    const activeChat=await page.evaluate(()=>chatId);
    async function addBatch(batch){
      const text='Большая ветка Голос.\n'+Array.from({length:10},(_,index)=>'Проверка '+batch+'-'+index+': что уточнить?').join('\n');
      await page.evaluate(async ({chat,text})=>{await api('chat',{chat_id:chat,text,request_id:crypto.randomUUID().replaceAll('-','')});},{chat:activeChat,text});
      await page.waitForFunction(async text=>{const result=await api('messages?chat_id='+chatId);return result.messages.some(row=>row.content===text&&row.memory_job?.status==='done');},text,{timeout:20000});
    }
    await addBatch('A');await page.evaluate(()=>refresh());
    await page.waitForFunction(()=>document.querySelector('#map-nodes [data-change]'));
    await page.waitForFunction(()=>!document.querySelector('#map-nodes [data-change]'),null,{timeout:10000});
    await page.locator('#map-layout').selectOption('tree');await page.locator('#map-layout').selectOption('network');
    if(await page.locator('#map-nodes [data-change]').count())throw Error('Layout switching fabricated a saved-data change');
    await addBatch('B');await addBatch('C');await page.evaluate(()=>refresh());
    await voiceWrap.locator('.map-load-children').waitFor();
    let releasePage,seenPage;const pageStarted=new Promise(resolve=>{seenPage=resolve;});const pageRelease=new Promise(resolve=>{releasePage=resolve;});
    let heldPage=false;
    const holdPage=async route=>{if(heldPage){await route.continue();return;}heldPage=true;const response=await route.fetch();seenPage();await pageRelease;await route.fulfill({response});};
    await page.route('**/api/brain/graph-children?**',holdPage);
    try{
      await voiceWrap.locator('.map-load-children').click();await pageStarted;
      const before=await page.locator('.map-viewport').evaluate(view=>{view.scrollTop=400;const rect=view.getBoundingClientRect();let best=null,distance=Infinity;
        for(const card of view.querySelectorAll('.map-node')){const box=card.getBoundingClientRect();if(box.bottom<=rect.top||box.top>=rect.bottom)continue;const score=Math.abs(box.top-rect.top)+Math.abs(box.left-rect.left);if(score<distance){distance=score;best={id:card.dataset.nodeId,y:box.top-rect.top};}}return best;});
      releasePage();
      await page.waitForFunction(voice=>{const wrap=[...document.querySelectorAll('[data-wrapper-id]')].find(item=>item.dataset.wrapperId===voice);return parseInt(wrap.querySelector('.map-page-count').textContent)>=30;},voice);
      const after=await page.locator('.map-viewport').evaluate((view,id)=>{const card=[...view.querySelectorAll('.map-node')].find(item=>item.dataset.nodeId===id);return card.getBoundingClientRect().top-view.getBoundingClientRect().top;},before.id);
      if(Math.abs(after-before.y)>3)throw Error('Loading children moved the reading anchor: '+before.y+' -> '+after);
    }finally{releasePage();await page.unroute('**/api/brain/graph-children?**',holdPage);}
    const covered=await voiceWrap.locator('.map-page-count').textContent();
    await page.evaluate(()=>refresh());
    await page.waitForFunction(()=>document.getElementById('map-stage').getAttribute('aria-busy')==='false');
    if(await voiceWrap.locator('.map-page-count').textContent()!==covered)throw Error('Background refresh discarded loaded pages');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'map-large-branch.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.locator('.map-viewport').evaluate(view=>{view.scrollTop=0;view.scrollLeft=0;view.scrollIntoView({block:'start'});});
    const touch=await page.context().newCDPSession(page);await touch.send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:2});
    const box=await page.locator('.map-viewport').boundingBox();
    const fingers=distance=>[{x:box.x+170-distance,y:box.y+110,id:1},{x:box.x+170+distance,y:box.y+110,id:2}];
    await touch.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:fingers(45)});
    await touch.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:fingers(70)});
    await touch.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    await page.waitForFunction(()=>parseInt(document.getElementById('map-zoom-value').textContent)>100);
    await page.locator('#map-zoom-reset').click();
    const mobileTarget=page.locator('#map-nodes .map-node[data-type=branch]').first();await mobileTarget.scrollIntoViewIfNeeded();
    const mobilePosition=await page.locator('.map-viewport').evaluate(view=>({top:view.scrollTop,left:view.scrollLeft}));
    await mobileTarget.click();
    await page.locator('#map-mobile-dialog[open]').waitFor();
    if(!await page.locator('#map-mobile-dialog #map-passport').count())throw Error('Mobile passport is not in the dialog');
    const fullCard=await page.locator('#map-mobile-dialog').boundingBox();
    if(Math.abs(fullCard.x)>1||Math.abs(fullCard.y)>1||fullCard.width<389||fullCard.height<843)throw Error('Mobile card is not fullscreen');
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile dialog overflow');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'map-mobile-card.png'),fullPage:false});
    await page.locator('#map-close-card').click();
    await page.waitForFunction(()=>!document.getElementById('map-mobile-dialog').open);
    if(!await page.locator('#map-passport-home #map-passport').count())throw Error('Passport was not returned after closing');
    const mobileReturned=await page.locator('.map-viewport').evaluate(view=>({top:view.scrollTop,left:view.scrollLeft}));
    if(Math.abs(mobilePosition.top-mobileReturned.top)>2||Math.abs(mobilePosition.left-mobileReturned.left)>2)throw Error('Closing mobile card changed map position');
    await page.locator('#map-open-card').click();await page.locator('#map-mobile-dialog[open]').waitFor();await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!document.getElementById('map-mobile-dialog').open&&document.getElementById('map-passport').parentElement.id==='map-passport-home');
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Growth map overflow on mobile');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'growth-mobile.png'),fullPage:true});
    await navigate('chat');
    await page.setViewportSize({width:390,height:844});
    if(screenshots)await page.screenshot({path:path.join(screenshots,'mobile.png'),fullPage:true});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow on mobile');
    await navigate('projects');
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Project layout overflow on mobile');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'projects-mobile.png'),fullPage:true});
    // Durable memories, real lexical associations and paginated browsing.
    await page.setViewportSize({width:1440,height:1000});
    const neuralIds=await page.evaluate(async()=>{
      const a=await api('memory',{content:'Нейрокарта сохраняет важные звёздные знания',tier:'vault'});
      const b=await api('memory',{content:'Нейрокарта сохраняет важные звёздные решения',tier:'vault'});
      return [a.id,b.id];
    });
    await page.waitForFunction(async()=>{const n=(await api('state')).neural;return n.links>0&&n.pending===0;},null,{timeout:30000});
    await page.evaluate(async ids=>{await refresh();await window.BrainMap.open('memory:'+ids[0]);},neuralIds);
    await page.waitForFunction(()=>document.getElementById('map-passport').textContent.includes('Общие основы слов:'));
    if(screenshots)await page.screenshot({path:path.join(screenshots,'neural-map.png'),fullPage:true});
    await navigate('memory');
    await page.locator('#memory-search').fill('НЕЙРОКАРТА');
    await page.locator('#memory-filter').selectOption('vault');
    await page.waitForFunction(()=>document.querySelectorAll('#memories .memory-card').length===2);
    if(!await page.locator('#neural-status').textContent().then(text=>text.includes('Автосвязи:')))throw Error('Neural counters missing');
    await page.evaluate(async()=>{for(let i=0;i<53;i++)await api('memory',{content:'Страница долговременной памяти '+i,tier:'vault'});});
    await page.locator('#memory-search').fill('Страница долговременной');
    await page.waitForFunction(()=>document.querySelectorAll('#memories .memory-card').length===50);
    await page.locator('#memory-more').click();
    await page.waitForFunction(()=>document.querySelectorAll('#memories .memory-card').length===53);
    if(!await page.locator('#memory-more').isHidden())throw Error('Memory cursor did not terminate');
    await page.setViewportSize({width:390,height:844});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Neural memory mobile overflow');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'neural-memory-mobile.png'),fullPage:true});
    // Every module shares the new frame; navigation remains local and keyboard friendly.
    await page.setViewportSize({width:1440,height:1000});
    await navigate('chat');
    const currentChat=await page.evaluate(()=>chatId);
    await page.locator('#message').fill('Черновик остаётся при поиске и переходах');
    await page.locator('#chat-search').fill('несуществующий разговор 92841');
    if(await page.locator('#chats .chat-item').count())throw Error('Chat title filter did not filter');
    if(await page.evaluate(()=>chatId)!==currentChat)throw Error('Chat search changed active conversation');
    await page.locator('#chat-search').fill('');
    await page.keyboard.press('Control+k');await page.locator('#navigation-dialog[open]').waitFor();
    await page.locator('#navigation-search').fill('Профиль');await page.locator('#navigation-search').press('Enter');
    await page.waitForFunction(()=>document.body.dataset.view==='profile'&&!document.getElementById('navigation-dialog').open);
    if(!page.url().endsWith('#profile'))throw Error('Module URL did not reflect navigation');
    await navigate('chat');
    if(await page.locator('#message').inputValue()!=='Черновик остаётся при поиске и переходах')throw Error('Navigation lost message draft');
    await page.locator('#menu-toggle').click();
    if(!await page.evaluate(()=>document.body.classList.contains('sidebar-compact')))throw Error('Desktop sidebar did not fold');
    await page.reload();await page.waitForFunction(()=>state?.has_key&&document.body.dataset.view==='chat');
    if(!await page.evaluate(()=>document.body.classList.contains('sidebar-compact')))throw Error('Sidebar preference did not persist');
    await page.locator('#menu-toggle').click();
    await navigate('memory');await page.locator('#memory-search').fill('');await page.locator('#memory-filter').selectOption('');
    await page.locator('#memory-compact').click();
    if(await page.locator('#memories').getAttribute('data-density')!=='compact')throw Error('Compact memory layout missing');
    await page.reload();await page.waitForFunction(()=>state?.has_key&&document.body.dataset.view==='memory');
    if(await page.locator('#memories').getAttribute('data-density')!=='compact')throw Error('Memory view did not persist');
    await page.locator('#memory-cards').click();
    await navigate('settings');await page.locator('#api-key').fill('test-key-for-display-only');await page.locator('#reveal-key').click();
    if(await page.locator('#api-key').getAttribute('type')!=='text')throw Error('API key reveal did not work');
    await page.locator('#reveal-key').click();await page.locator('#api-key').fill('');
    if(await page.locator('#settings-status').getAttribute('data-dirty')!=='true')throw Error('Unsaved settings indicator missing');
    await page.locator('#settings-form button[type=submit]').click();
    await page.waitForFunction(()=>document.getElementById('settings-status').dataset.dirty==='false');
    if(await page.locator('#api-key').getAttribute('type')!=='password')throw Error('API key remained visible after save');
    const modules=['chat','memory','map','projects','profile','checks','settings'];
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000});
      for(const name of modules){
        await navigate(name);
        await page.evaluate(()=>scrollTo(0,0));
        await page.waitForTimeout(150);
        if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Redesign overflow '+name+' '+width);
        const duplicates=await page.evaluate(()=>{const ids=[...document.querySelectorAll('[id]')].map(x=>x.id);return ids.filter((id,index)=>ids.indexOf(id)!==index);});
        if(duplicates.length)throw Error('Duplicate IDs: '+duplicates.join(','));
        if(!await page.locator('[data-tab='+name+']').getAttribute('aria-current'))throw Error('Current navigation state missing');
        if(screenshots)await page.screenshot({path:path.join(screenshots,'design-'+name+'-'+width+'.png'),fullPage:name==='settings'});
      }
    }
    for(const width of [320,768]){
      await page.setViewportSize({width,height:900});
      for(const name of modules){await navigate(name);if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Narrow/tablet overflow '+name+' '+width);}
    }
    await page.setViewportSize({width:390,height:844});
    await page.locator('#menu-toggle').click();
    await page.locator('#app-sidebar .brand').focus();await page.keyboard.press('Shift+Tab');
    if(!await page.evaluate(()=>document.getElementById('app-sidebar').contains(document.activeElement)))throw Error('Focus escaped mobile menu');
    await page.keyboard.press('Escape');
    if(await page.evaluate(()=>document.body.classList.contains('menu-open')||document.getElementById('main-content').inert))throw Error('Mobile menu did not close cleanly');
    if(await page.evaluate(()=>document.activeElement.id)!=='menu-toggle')throw Error('Mobile menu focus was not restored');
    const portal=await page.context().newPage();portal.on('pageerror',error=>errors.push(error.message));
    for(const width of [1440,390,320]){
      await portal.setViewportSize({width,height:1000});
      await portal.goto(new URL('/',url).href);await portal.waitForFunction(()=>document.getElementById('home-memory').textContent!=='—');
      if(await portal.locator('.module-card').count()!==8)throw Error('Home module shortcuts missing');
      if(await portal.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Home overflow '+width);
      if(screenshots&&width!==320)await portal.screenshot({path:path.join(screenshots,'design-home-'+width+'.png'),fullPage:true});
      await portal.goto(new URL('/history',url).href);
      if(await portal.locator('.session-card').count()!==3)throw Error('History fixture did not load');
      await portal.locator('#history-filter').selectOption('failed');
      if(await portal.locator('.session-card:visible').count()!==1)throw Error('History status filter failed');
      await portal.locator('#history-filter').selectOption('running');
      if(!await portal.locator('#history-no-results').isVisible())throw Error('History empty filter state missing');
      await portal.locator('#history-filter').selectOption('');
      if(await portal.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('History overflow '+width);
      if(screenshots&&width!==320)await portal.screenshot({path:path.join(screenshots,'design-history-'+width+'.png'),fullPage:true});
    }
    await portal.goto(new URL('/brain#projects',url).href);
    await portal.waitForFunction(()=>state?.has_key&&document.body.dataset.view==='projects');
    await portal.close();
    if(errors.length)throw Error(errors.join('\n'));
    console.log('Browser QA passed: settings, lost-response recovery, per-message extraction progress, source/reply memory filters, stale refresh protection, cross-chat retrieval, project recognition/progress, versions/restore, profile, local checks, persisted handoff, development graph, source passports, actual memory traces, question resolution, growth history, local consolidation/group/undo, zoom/buttons/wheel/pinch, folding, tree/network/sequence, goal path, saved-data flashes, cursor paging/anchor/cache, fullscreen mobile passport, durable memory, explainable associations, memory cursor pagination, redesigned modules at desktop/mobile/tablet widths, command navigation, menu focus/persistence, chat title search/draft preservation, memory density, settings feedback/key masking, home shortcuts/counters, history filters, deep links, mobile layout.');
  }catch(error){
    if(page)console.error('Page notice:',await page.locator('#notice').textContent().catch(()=>''));
    if(page&&process.env.UI_SCREENSHOT_DIR){await fs.mkdir(process.env.UI_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR,'failure.png'),fullPage:true}).catch(()=>{});}
    throw error;
  }finally{
    if(releaseStale)releaseStale();
    if(browser)await browser.close();
    child.kill('SIGTERM');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
