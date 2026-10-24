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
    await page.goto(url);
    await page.waitForFunction(()=>document.getElementById('key-status').textContent.includes('не сохранён'));
    await page.locator('#api-key').fill('test-private-key-1234567890');
    await page.locator('#model').fill('chat-model');
    await page.locator('#settings-form button[type=submit]').click();
    await page.waitForFunction(()=>document.getElementById('key-status').textContent.startsWith('Ключ сохранён.'));
    await page.locator('[data-tab=chat]').click();
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
    await page.locator('[data-tab=chat]').click();
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
    await page.locator('[data-tab=profile]').click();
    if(await page.locator('#profile .memory-card').count()!==1)throw Error('Global profile was not generated');
    await page.locator('[data-tab=chat]').click();
    await page.locator('#message').fill('Проект Sakura. Цель: сделать помощницу. Осталось: добавить поиск. Остановились: на памяти.');
    await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('#chat-project option:checked')?.textContent==='Sakura',null,{timeout:20000});
    await page.locator('#message').fill('Сделано: добавлен поиск. Остановились: на проверках.');
    await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('#projects')?.textContent.includes('Остановились: на проверках.'),null,{timeout:20000});
    await page.locator('[data-tab=projects]').click();
    if(await page.locator('#projects .project-card').count()!==1)throw Error('Project was duplicated');
    const board=await page.locator('#projects').textContent();
    if(!board.includes('Сделано: добавлен поиск.')||board.includes('Осталось: добавить поиск.')||board.includes('Остановились: на памяти.'))throw Error('Project progress was not updated');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'projects.png'),fullPage:true});
    await page.getByRole('button',{name:'Память проекта',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('memory-scope-label').textContent.includes('Sakura'));
    await page.waitForFunction(()=>document.querySelectorAll('#memories .memory-card').length===1&&document.getElementById('memories').textContent.includes('Осталось: добавить поиск.'));
    if(!(await page.locator('#memories').textContent()).includes('Осталось: добавить поиск.'))throw Error('Project memory filter failed');
    await page.locator('[data-tab=checks]').click();
    await page.locator('#run-checks').click();
    await page.waitForFunction(()=>!document.getElementById('run-checks').disabled&&document.getElementById('checks-result').textContent.includes('Пройдено'));
    if(await page.locator('#checks-list .check-case').count()!==2)throw Error('Automatic checks were not generated');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'checks.png'),fullPage:true});
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#chat-project option:checked')?.textContent==='Sakura');
    await page.locator('#handoff > summary').click();
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
    await page.locator('[data-tab=chat]').click();
    for(const text of ['Я предпочитаю короткие ответы без длинных вступлений.','Я предпочитаю короткие ответы без длинных вступлений и повторов.']){
      await page.locator('#message').fill(text);await page.locator('#send').click();
      await page.waitForFunction(async text=>{const result=await api('memories');return result.memories.some(row=>row.content===text);},text,{timeout:20000});
      await page.waitForFunction(()=>!document.getElementById('send').disabled);
    }
    await page.locator('[data-tab=map]').click();
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
    await page.setViewportSize({width:390,height:844});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Growth map overflow on mobile');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'growth-mobile.png'),fullPage:true});
    await page.locator('[data-tab=chat]').click();
    await page.setViewportSize({width:390,height:844});
    if(screenshots)await page.screenshot({path:path.join(screenshots,'mobile.png'),fullPage:true});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow on mobile');
    await page.locator('[data-tab=projects]').click();
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Project layout overflow on mobile');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'projects-mobile.png'),fullPage:true});
    if(errors.length)throw Error(errors.join('\n'));
    console.log('Browser QA passed: settings, lost-response recovery, per-message extraction progress, source/reply memory filters, stale refresh protection, cross-chat retrieval, project recognition/progress, versions/restore, profile, local checks, persisted handoff, development graph, source passports, actual memory traces, question resolution, growth history, local consolidation/group/undo, mobile layout.');
  }catch(error){
    if(page)console.error('Page notice:',await page.locator('#notice').textContent().catch(()=>''));
    throw error;
  }finally{
    if(releaseStale)releaseStale();
    if(browser)await browser.close();
    child.kill('SIGTERM');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
