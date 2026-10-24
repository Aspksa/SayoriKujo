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
    await page.setViewportSize({width:390,height:844});
    if(screenshots)await page.screenshot({path:path.join(screenshots,'mobile.png'),fullPage:true});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow on mobile');
    await page.locator('[data-tab=projects]').click();
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Project layout overflow on mobile');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'projects-mobile.png'),fullPage:true});
    if(errors.length)throw Error(errors.join('\n'));
    console.log('Browser QA passed: settings, lost-response recovery, per-message extraction progress, source/reply memory filters, stale refresh protection, cross-chat retrieval, project recognition/progress, versions/restore, profile, local checks, persisted handoff, mobile layout.');
  }catch(error){
    if(page)console.error('Page notice:',await page.locator('#notice').textContent().catch(()=>''));
    throw error;
  }finally{
    if(releaseStale)releaseStale();
    if(browser)await browser.close();
    child.kill('SIGTERM');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
