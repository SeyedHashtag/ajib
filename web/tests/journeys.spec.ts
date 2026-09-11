import {test, expect, type Page} from '@playwright/test';
import {createHmac, randomUUID} from 'node:crypto';

async function signIn(page:Page,user=123) {
  const data:Record<string,string>={auth_date:String(Math.floor(Date.now()/1000)),user:JSON.stringify({id:user,first_name:'Synthetic'}),query_id:randomUUID()};
  const secret=createHmac('sha256','WebAppData').update('123456:synthetic-browser-test-token').digest();
  data.hash=createHmac('sha256',secret).update(Object.keys(data).sort().map(k=>`${k}=${data[k]}`).join('\n')).digest('hex');
  // Independent synthetic visitors. Uvicorn accepts this only from the local
  // trusted proxy; production Nginx overwrites any incoming forwarded address.
  const response=await page.request.post('/api/v1/auth/telegram',{headers:{Origin:'http://127.0.0.1:5173','X-Forwarded-For':`192.0.2.${user%254+1}`},data:{init_data:new URLSearchParams(data).toString()}});
  expect(response.ok()).toBeTruthy();
}

test('public Persian website and all supported languages',async({page})=>{
  await page.goto('/');
  await expect(page.locator('h1')).toContainText('اتصال شما');
  await expect(page.locator('html')).toHaveAttribute('dir','rtl');
  await expect(page.locator('.plan-card')).toHaveCount(3);
  for(const lang of ['en','ru','tk','fa']) {
    await page.locator('.language-select select').selectOption(lang);
    await expect(page.locator('html')).toHaveAttribute('lang',lang);
  }
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
  await page.screenshot({path:`test-results/public-${test.info().project.name}.png`,fullPage:true});
});

test('customer can read owned account, configuration, and payment history',async({page})=>{
  await signIn(page);
  await page.goto('/app/accounts');
  await expect(page.locator('.account-card')).toContainText('s123a');
  await page.getByRole('button',{name:'Configuration',exact:true}).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('textbox')).toHaveValue(/hysteria2:\/\//);
  await page.getByRole('button',{name:'Close',exact:true}).click();
  await page.goto('/app/payments');
  await expect(page.locator('tbody')).toContainText('$1.20');
  await page.screenshot({path:`test-results/customer-${test.info().project.name}.png`,fullPage:true});
});

test('customer cannot navigate into admin data',async({page})=>{
  await signIn(page);
  await page.goto('/admin');
  await expect(page.getByRole('alert')).toContainText('Access is not available');
  expect((await page.request.get('/api/v1/admin/overview')).status()).toBe(403);
});

test('Telegram navigation uses its back button',async({page})=>{
  await signIn(page);
  await page.addInitScript(()=>{
    (window as any).__backShown=false;
    (window as any).Telegram={WebApp:{initData:'test',colorScheme:'dark',safeAreaInset:{top:24,bottom:18,left:0,right:0},ready(){},expand(){},BackButton:{show(){(window as any).__backShown=true;},hide(){(window as any).__backShown=false;},onClick(){},offClick(){}}}};
  });
  await page.goto('/app/accounts');
  await expect(page.locator('.account-card')).toBeVisible();
  expect(await page.evaluate(()=>(window as any).__backShown)).toBeTruthy();
  await expect(page.locator('html')).toHaveAttribute('data-telegram-theme','dark');
  expect(await page.locator('body').evaluate(el=>getComputedStyle(el).paddingTop)).toBe('24px');
  await page.screenshot({path:`test-results/telegram-${test.info().project.name}.png`,fullPage:true});
});

test('admin can switch roles and sign out on mobile and desktop',async({page})=>{
  await signIn(page,1);
  await page.goto('/app');
  await expect(page.locator('.role-select select')).toBeVisible();
  await page.locator('.role-select select').selectOption('admin');
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.locator('h1')).toContainText('Administrator');
  await page.getByRole('button',{name:'Sign out',exact:true}).click();
  await expect(page).toHaveURL(/\/$/);
  expect((await page.request.get('/api/v1/me')).status()).toBe(401);
});

test('referrals and trials respect the paused write gate',async({page})=>{
  await signIn(page);
  await page.goto('/app/referrals');
  await expect(page.getByRole('button',{name:'Create referral code',exact:true})).toBeDisabled();
  await expect(page.getByRole('heading',{name:'Withdrawal history',exact:true})).toBeVisible();
  await page.goto('/app/accounts');
  await expect(page.getByRole('button',{name:'Request a trial',exact:true})).toBeDisabled();
});

test('public downloads and administrator operations are available',async({page})=>{
  await page.goto('/guides');
  await expect(page.locator('.download-guide')).toHaveCount(4);
  await page.locator('.download-guide summary').first().click();
  await expect(page.locator('.download-guide').first().locator('a')).toHaveAttribute('href',/apps\.apple\.com/);
  await signIn(page,1);
  await page.goto('/admin/operations');
  await expect(page.getByRole('heading',{name:'Background worker',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Outstanding operations',exact:true})).toBeVisible();
});
