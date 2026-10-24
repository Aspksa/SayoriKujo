const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs/promises');

(async()=>{
  const child=spawn('python',['-u',path.join(__dirname,'brain_ui_server.py')],{stdio:['ignore','pipe','pipe']});
  let output='',stderr='';child.stderr.on('data',data=>{stderr+=data;});
  let browser,page;
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
    await page.locator('#message').fill('Мне нужен русский интерфейс.');
    await page.locator('#send').click();
    await page.locator('.bubble.assistant').waitFor();
    await page.waitForFunction(()=>document.getElementById('n-memory').textContent==='1',null,{timeout:20000});
    const screenshots=process.env.UI_SCREENSHOT_DIR;
    if(screenshots){await fs.mkdir(screenshots,{recursive:true});await page.screenshot({path:path.join(screenshots,'chat.png'),fullPage:true});}
    await page.locator('[data-tab=memory]').click();
    await page.locator('.memory-card').waitFor();
    await page.getByRole('button',{name:'Исправить / подтвердить'}).click();
    await page.locator('#memory-tier').selectOption('core');
    await page.locator('#memory-editor button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('.memory-card')?.textContent.includes('Подтверждено'));
    if(screenshots)await page.screenshot({path:path.join(screenshots,'memory.png'),fullPage:true});
    await page.locator('[data-tab=chat]').click();
    await page.locator('#new-chat').click();
    await page.locator('#message').fill('Что я предпочитаю?');
    await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('.bubble.assistant')?.textContent.includes('использовано записей памяти: 1'));
    await page.setViewportSize({width:390,height:844});
    await page.locator('[data-tab=memory]').click();
    if(screenshots)await page.screenshot({path:path.join(screenshots,'mobile.png'),fullPage:true});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow on mobile');
    if(errors.length)throw Error(errors.join('\n'));
    console.log('Browser QA passed: settings, chat, automatic extraction, memory editing, cross-chat retrieval, mobile layout.');
  }catch(error){
    if(page)console.error('Page notice:',await page.locator('#notice').textContent().catch(()=>''));
    throw error;
  }finally{
    if(browser)await browser.close();
    child.kill('SIGTERM');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
