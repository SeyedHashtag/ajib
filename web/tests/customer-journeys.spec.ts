import {test,expect,type Page} from '@playwright/test';
import {createHmac,randomUUID} from 'node:crypto';

async function login(page:Page,user:number,language='en') {
  const data:Record<string,string>={auth_date:String(Math.floor(Date.now()/1000)),user:JSON.stringify({id:user,first_name:'Synthetic'}),query_id:randomUUID()};
  const secret=createHmac('sha256','WebAppData').update('123456:synthetic-browser-test-token').digest();
  data.hash=createHmac('sha256',secret).update(Object.keys(data).sort().map(k=>`${k}=${data[k]}`).join('\n')).digest('hex');
  const response=await page.request.post('/api/v1/auth/telegram',{headers:{Origin:'http://127.0.0.1:5173','X-Forwarded-For':`192.0.2.${user%254+1}`},data:{init_data:new URLSearchParams(data).toString()}});
  expect(response.ok()).toBeTruthy();
  const identity=await response.json();
  expect((await page.request.put('/api/v1/me/language',{headers:{Origin:'http://127.0.0.1:5173','X-CSRF-Token':identity.csrf_token},data:{language}})).ok()).toBeTruthy();
}

test('card checkout and private receipt continue to review',async({page},info)=>{
  await login(page,info.project.name==='mobile'?501:401,'fa');
  await page.goto('/plans');
  await page.locator('.plan-card button').first().click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('combobox').first().selectOption('card');
  await dialog.locator('button.full').click();
  await expect(page).toHaveURL(/\/app\/payments\/web_/);
  await expect(page.locator('.payment-detail pre')).toContainText('0000');
  await page.locator('input[type=file]').setInputFiles({name:'receipt.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII=','base64')});
  await expect(page.locator('.payment-detail [role=status]')).toContainText('بررسی');
  await expect(page.locator('input[type=file]')).toHaveCount(0);
  expect(await page.locator('html').getAttribute('dir')).toBe('rtl');
});

test('crypto checkout uses the persisted invoice link',async({page},info)=>{
  await login(page,info.project.name==='mobile'?502:402);
  await page.goto('/plans');
  await page.locator('.plan-card button').first().click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByRole('combobox').first()).toHaveValue('crypto');
  await expect(dialog.locator('option[value=card]')).toHaveAttribute('disabled','');
  await dialog.locator('button.full').click();
  await expect(page.locator('.payment-detail [role=status]')).toHaveText('Waiting for payment');
  await expect(page.getByRole('link',{name:'Open payment page'})).toHaveAttribute('href',/https:\/\/payment.invalid\/web_/);
});

test('renewal choices come from the server and support keyboard selection',async({page})=>{
  await login(page,123);
  await page.goto('/app/accounts');
  await page.getByRole('button',{name:'Renew',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByLabel('Renewal option')).toHaveValue('reserved');
  await expect(dialog.locator('option[value=immediate]')).toHaveAttribute('disabled','');
  await dialog.getByRole('combobox',{name:'Plans',exact:true}).focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(dialog.getByRole('combobox',{name:'Plans',exact:true})).toHaveValue('60');
  await expect(dialog.locator('h3')).toContainText('60');
  await dialog.getByRole('button',{name:'Close',exact:true}).click();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
});

test('reserved activation polls after payment settlement and pauses in background',async({page})=>{
  await login(page,125);
  await page.clock.install();
  let requests=0;
  page.on('request',r=>{if(r.url().endsWith('/api/v1/payments/synthetic-reserved'))requests++;});
  await page.goto('/app/payments/synthetic-reserved');
  await expect(page.locator('.payment-detail [role=status]')).toContainText('Renewal reserved');
  const initial=requests;
  await page.clock.runFor(11000);
  await expect.poll(()=>requests).toBeGreaterThan(initial);
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
  const hidden=requests;
  await page.clock.runFor(21000);
  expect(requests).toBe(hidden);
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
  await expect.poll(()=>requests).toBeGreaterThan(hidden);
  await expect(page.getByRole('button',{name:'Refresh status'})).toBeEnabled();
});

test('attention is localized in four languages and exposes no unsafe retry',async({page})=>{
  await login(page,124);
  await page.goto('/app/payments/synthetic-attention');
  for(const language of ['fa','en','ru','tk']) {
    await page.locator('.language-select select').selectOption(language);
    await expect(page.locator('.payment-detail [role=status]')).toBeVisible();
    const content=await page.locator('.payment-detail').innerText();
    expect(content).not.toMatch(/uncertain|web_attention_reason|internal-marker|ajib/i);
    await expect(page.locator('.payment-detail input[type=file]')).toHaveCount(0);
    await expect(page.locator('.payment-detail .danger')).toHaveCount(0);
  }
});

test('trial request remains in the shared queue without a second request',async({page},info)=>{
  await login(page,info.project.name==='mobile'?902:802);
  await page.goto('/app/accounts');
  await page.getByRole('button',{name:'Request a trial',exact:true}).click();
  await expect(page.locator('.trial-card [role=status]')).toHaveText('Your request is in the shared trial queue.');
  await expect(page.getByRole('button',{name:'Request a trial',exact:true})).toHaveCount(0);
  await page.reload();
  await expect(page.locator('.trial-card [role=status]')).toHaveText('Your request is in the shared trial queue.');
});

test('referral code wallet and withdrawal preserve one pending request',async({page},info)=>{
  await login(page,info.project.name==='mobile'?701:601);
  await page.goto('/app/referrals');
  await page.getByRole('button',{name:'Create referral code',exact:true}).click();
  await expect(page.getByRole('button',{name:'Create referral code',exact:true})).toHaveCount(0);
  await page.locator('input[minlength="10"]').fill('synthetic-wallet-address');
  await page.getByRole('button',{name:'Save changes',exact:true}).click();
  await page.getByRole('button',{name:'Request withdrawal',exact:true}).click();
  await page.locator('.withdrawal-confirm button.button').click();
  await expect(page.locator('tbody')).toContainText('synthetic-wallet-address');
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.getByRole('button',{name:'Request withdrawal',exact:true})).toBeDisabled();
});
