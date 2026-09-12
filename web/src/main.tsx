import {useEffect, useRef, useState, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {BrowserRouter, Link, NavLink, useLocation, useNavigate} from 'react-router-dom';
import {ArrowUpRight, ArrowRight, Check, ChevronDown, Copy, Globe2, LayoutDashboard, Link2, LoaderCircle, LogOut, Menu, MessageCircle, Plus, ReceiptText, ShieldCheck, Users, Wallet, X} from 'lucide-react';
import {api, ApiError, safeLink, setCsrf, type Account, type Identity, type Language, type Payment, type Plan, type Store} from './api';
import {dictionaries, type Text} from './i18n';
import './style.css';
import {Rewards} from './Rewards';
import {Trial} from './Trial';
import {Operations} from './Operations';

type TelegramApp = {initData: string; ready(): void; expand(): void; colorScheme: string;
  safeAreaInset?:{top:number;bottom:number;left:number;right:number};contentSafeAreaInset?:{top:number;bottom:number;left:number;right:number};
  onEvent?(event:string,callback:()=>void):void;offEvent?(event:string,callback:()=>void):void;
  BackButton: {show(): void; hide(): void; onClick(cb: () => void): void; offClick(cb: () => void): void}};
declare global {interface Window {Telegram?: {WebApp: TelegramApp}}}

function useData<T>(path: string | null) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let alive = true;
    setData(undefined); setError(''); setLoading(true);
    if (!path) {setLoading(false); return;}
    api<T>(path).then(value => {if (alive) setData(value);}).catch(e => {if (alive) setError(e.message);}).finally(() => {if (alive) setLoading(false);});
    return () => {alive = false;};
  }, [path, revision]);
  return {data, error, loading, refresh: () => setRevision(n => n + 1)};
}

function Feedback({error, loading, retry, t}: {error?: string; loading?: boolean; retry?: () => void; t: Text}) {
  if (error) return <div className="notice error" role="alert">{error}{retry && <button className="text-button" onClick={retry}>{t.retry}</button>}</div>;
  return loading ? <div className="loading" role="status"><LoaderCircle className="spin" size={20}/>{t.loading}</div> : null;
}
function Empty({children}: {children: ReactNode}) {return <div className="empty"><Link2 size={28}/><p>{children}</p></div>;}
function Money({amount, lang}: {amount?: number | string; lang: Language}) {return <bdi>{new Intl.NumberFormat(lang === 'tk' ? 'en' : lang, {style: 'currency', currency: 'USD'}).format(Number(amount || 0))}</bdi>;}
function DateText({value, lang}: {value?: string | null; lang: Language}) {return <bdi>{value && !isNaN(Date.parse(value)) ? new Intl.DateTimeFormat(lang === 'tk' ? 'en' : lang, {dateStyle: 'medium'}).format(new Date(value)) : '—'}</bdi>;}
function Modal({title, children, close, t}: {title: string; children: ReactNode; close: () => void; t: Text}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {const dialog = ref.current; dialog?.showModal(); return () => dialog?.close();}, []);
  return <dialog ref={ref} onCancel={e => {e.preventDefault(); close();}}><div className="modal-header"><h2>{title}</h2><button className="icon-button" aria-label={t.close} onClick={close}><X/></button></div>{children}</dialog>;
}

function Application() {
  const location = useLocation(), navigate = useNavigate();
  const match = location.pathname.match(/^\/s\/([a-z0-9-]+)(\/.*)?$/);
  const slug = match?.[1], prefix = slug ? `/s/${slug}` : '';
  const page = (slug ? match?.[2] || '/' : location.pathname).replace(/\/$/, '') || '/';
  const [lang, setLang] = useState<Language>(() => {
    const saved = localStorage.getItem('ajib-language');
    return saved && saved in dictionaries ? saved as Language : 'fa';
  });
  const t = dictionaries[lang];
  const [identity, setIdentity] = useState<Identity | null>(null), [authLoading, setAuthLoading] = useState(true), [authError, setAuthError] = useState('');
  const [menu, setMenu] = useState(false);
  const store = useData<Store>(`/storefront${slug ? `?slug=${encodeURIComponent(slug)}` : ''}`);
  const planData = useData<Plan[]>(`/plans${slug ? `?storefront=${encodeURIComponent(slug)}` : ''}`);
  const [selected, setSelected] = useState<Plan>(), [renewal, setRenewal] = useState<Account>();
  const href = (path: string) => prefix + path;
  async function loadIdentity() {
    const value = await api<Identity>('/me');
    if (value.scope !== (slug ? store.data?.scope : 'main')) {
      setCsrf(value.csrf_token); await api('/auth/logout', {method: 'POST'});
      setIdentity(null); return;
    }
    setCsrf(value.csrf_token); setIdentity(value); setLang(value.language);
  }
  useEffect(() => {
    if (!store.data) return;
    let alive = true;
    setIdentity(null); setAuthLoading(true); setAuthError('');
    (async () => {
      try {await loadIdentity();}
      catch (e) {
        const tg = window.Telegram?.WebApp;
        if (e instanceof ApiError && e.status === 401 && tg?.initData) {
          try {await api('/auth/telegram', {method:'POST', body:JSON.stringify({init_data:tg.initData, storefront:slug})}); await loadIdentity();}
          catch (error) {if (alive) setAuthError((error as Error).message);}
        } else if (!(e instanceof ApiError && e.status === 401) && alive) setAuthError((e as Error).message);
      } finally {if (alive) setAuthLoading(false);}
    })();
    return () => {alive = false;};
  }, [store.data?.scope]);
  useEffect(() => {document.documentElement.lang = lang; document.documentElement.dir = lang === 'fa' ? 'rtl' : 'ltr'; localStorage.setItem('ajib-language', lang);}, [lang]);
  useEffect(() => {
    setMenu(false);
    const tg = window.Telegram?.WebApp;
    if (!tg?.initData) return;
    tg.ready(); tg.expand();
    const updateTheme=()=>{document.documentElement.dataset.telegramTheme=tg.colorScheme;};
    const updateInsets=()=>{for(const edge of ['top','bottom','left','right'] as const){
      const pixels=Math.max(tg.safeAreaInset?.[edge]||0,tg.contentSafeAreaInset?.[edge]||0);
      document.documentElement.style.setProperty(`--tg-safe-${edge}`,`${Math.min(300,Math.max(0,pixels))}px`);
    }};
    updateTheme();updateInsets();tg.onEvent?.('themeChanged',updateTheme);tg.onEvent?.('safeAreaChanged',updateInsets);tg.onEvent?.('contentSafeAreaChanged',updateInsets);
    const back = () => {if(window.history.state?.idx>0)navigate(-1);else navigate(href(page==='/app'?'/':'/app'));};
    if (page === '/') tg.BackButton.hide(); else tg.BackButton.show();
    tg.BackButton.onClick(back); return () => {tg.BackButton.offClick(back);tg.offEvent?.('themeChanged',updateTheme);tg.offEvent?.('safeAreaChanged',updateInsets);tg.offEvent?.('contentSafeAreaChanged',updateInsets);};
  }, [page]);
  async function changeLanguage(value: Language) {
    setLang(value);
    if (identity?.writes_enabled) {
      try {await api('/me/language', {method:'PUT', body:JSON.stringify({language:value})});}
      catch (e) {setAuthError((e as Error).message);}
    }
  }
  function choose(plan: Plan, account?: Account) {
    if (!identity) {navigate(href('/login')); return;}
    setSelected(plan); setRenewal(account);
  }
  const portal = page.startsWith('/app') || page.startsWith('/reseller') || page.startsWith('/admin');
  const role = page.startsWith('/admin') ? 'admin' : page.startsWith('/reseller') ? 'reseller' : 'customer';
  const nav = role === 'customer' ? [ ['/app', t.overview, LayoutDashboard], ['/app/accounts',t.connections,Link2], ['/app/payments',t.payments,ReceiptText], ['/app/referrals',t.referrals,Users] ] as const
    : role === 'reseller' ? [['/reseller',t.overview,LayoutDashboard],['/reseller/customers',t.customers,Users],['/reseller/storefront',t.storefront,Globe2]] as const
      : [['/admin',t.overview,LayoutDashboard],['/admin/payments',t.reviews,ReceiptText],['/admin/operations',t.attention,LoaderCircle],['/admin/audit',t.audit,ShieldCheck]] as const;
  const title = store.data?.title || 'ajib';
  const privateContent = () => {
    if (authLoading) return <Feedback loading t={t}/>;
    if (!identity) return <Login t={t} slug={slug} onSuccess={async () => {await loadIdentity(); navigate(href('/app'));}}/>;
    if (role !== 'customer' && !identity.roles.includes(role) && !(role === 'admin' && identity.roles.includes('reviewer') && page.startsWith('/admin/payments'))) return <Feedback error="Access is not available for this account." t={t}/>;
    if (page === '/app') return <Overview t={t} lang={lang} href={href}/>;
    if (page === '/app/accounts') return <Connections t={t} lang={lang} plans={planData.data || []} choose={choose} writes={identity.writes_enabled}/>;
    if (page === '/app/payments') return <Payments t={t} lang={lang} href={href}/>;
    if (page.startsWith('/app/payments/')) return <PaymentDetails id={page.split('/')[3]} t={t} lang={lang} writes={identity.writes_enabled}/>;
    if (page === '/app/referrals') return <Rewards t={t} lang={lang} writes={identity.writes_enabled}/>;
    if (page.startsWith('/reseller')) return <Reseller page={page} t={t} lang={lang} writes={identity.writes_enabled}/>;
    if (page === '/admin/operations') return <Operations t={t} lang={lang}/>;
    if (page.startsWith('/admin')) return <Admin page={page} t={t} lang={lang} writes={identity.writes_enabled}/>;
    return <Empty>{t.empty}</Empty>;
  };
  return <>
    <a className="skip-link" href="#main">{t.continue}</a>
    <header className="header"><Link to={href('/')} className="brand"><span className="brand-symbol">a</span><b>{title}</b></Link>
      <nav className="public-nav" aria-label={t.home}>{[['/plans',t.plans],['/guides',t.guides],['/support',t.support]].map(([path,label]) => <NavLink key={path} to={href(path)}>{label}</NavLink>)}</nav>
      <div className="header-actions"><label className="language-select"><Globe2 size={16}/><select aria-label={t.language} value={lang} onChange={e => void changeLanguage(e.target.value as Language)}><option value="fa">فارسی</option><option value="en">English</option><option value="tk">Türkmençe</option><option value="ru">Русский</option></select></label>
      <Link className="button small" to={href(identity ? '/app' : '/login')}>{identity ? t.account : t.login}<ArrowUpRight size={16}/></Link>
      <button className="icon-button mobile-menu" aria-label={t.home} aria-expanded={menu} onClick={() => setMenu(!menu)}><Menu/></button></div>
    </header>
    {menu && <nav className="mobile-nav">{[['/plans',t.plans],['/guides',t.guides],['/faq',t.faq],['/support',t.support]].map(([path,label]) => <Link key={path} to={href(path)}>{label}</Link>)}</nav>}
    <div className={portal && identity ? 'workspace' : 'public-main'}>
      {portal && identity && <aside className="sidebar"><div className="identity"><span className="avatar"><Users size={22}/></span><div><strong>{title}</strong><small><bdi>#{identity.user_id}</bdi></small></div></div>
        <label className="role-select"><select aria-label={t.account} value={role} onChange={e => navigate(href(e.target.value === 'customer' ? '/app' : e.target.value === 'reviewer' ? '/admin/payments' : `/${e.target.value}`))}>{identity.roles.map(r => <option key={r} value={r}>{t[r as keyof Text] || r}</option>)}</select><ChevronDown size={16}/></label>
        <nav aria-label={t.account}>{nav.map(([path,label,Icon]) => <NavLink key={path} end to={href(path)}><Icon size={19}/>{label}</NavLink>)}<Link to={href('/plans')}><Plus size={19}/>{t.plans}</Link></nav>
        <div className="sidebar-bottom"><Link to={href('/support')}><MessageCircle size={18}/>{t.support}</Link><button onClick={async () => {try {await api('/auth/logout',{method:'POST'}); setIdentity(null); setCsrf(''); navigate(href('/'));} catch(e) {setAuthError((e as Error).message);}}}><LogOut size={18}/>{t.logout}</button></div></aside>}
      <main id="main" className={portal && identity ? 'portal-main' : ''}>
        <Feedback error={store.error || authError} t={t}/>
        {!portal && store.data?.writes_enabled === false && <div className="notice">{t.pilot} {store.data.bot_username && <a className="plain-link" href={`https://t.me/${store.data.bot_username}`} target="_blank" rel="noreferrer">{t.openTelegram}</a>}</div>}
        {identity && portal && !identity.writes_enabled && <div className="notice">{t.paused}</div>}
        {store.error ? null : portal ? privateContent() : page === '/' ? <>
          <section className="hero"><div><span className="eyebrow"><span/>{t.eyebrow}</span><h1>{t.hero}</h1><p>{t.intro}</p><div className="hero-actions"><Link className="button" to={href('/plans')}>{t.browse}<ArrowRight size={18}/></Link><Link className="plain-link" to={href('/guides')}>{t.guides}</Link></div><div className="hero-note"><ShieldCheck size={17}/>{t.connectionReadyText}</div></div><div className="hero-art" aria-hidden="true"><div className="orbit orbit-one"/><div className="orbit orbit-two"/><div className="orbit orbit-three"/><div className="connection-core"><Link2 size={58}/></div><span className="orbit-dot one"/><span className="orbit-dot two"/><div className="connection-label"><span/>{t.connectionReady}</div></div></section>
          <section className="section"><div className="section-heading"><h2>{t.plans}</h2><Link to={href('/plans')}>{t.browse}<ArrowUpRight size={16}/></Link></div><PlanCards data={planData} t={t} lang={lang} choose={choose}/></section>
          <section className="support-strip"><div><h2>{t.supportTitle}</h2><p>{t.supportText}</p></div><Link className="button secondary" to={href('/support')}>{t.support}<MessageCircle size={18}/></Link></section>
        </> : page === '/plans' ? <section className="section"><PageTitle title={t.choosePlan} text={t.choosePlanText}/><PlanCards data={planData} t={t} lang={lang} choose={choose}/></section>
        : page === '/login' ? <Login t={t} slug={slug} onSuccess={async () => {await loadIdentity(); navigate(href('/app'));}}/>
        : page === '/guides' ? <section className="section narrow"><PageTitle title={t.connectionGuide}/>{[1,2,3].map(n => <article className="guide-step" key={n}><span>{n.toString().padStart(2,'0')}</span><div><h2>{t[`guide${n}` as keyof Text]}</h2><p>{t[`guide${n}Text` as keyof Text]}</p></div></article>)}<Downloads t={t} lang={lang}/></section>
        : page === '/faq' ? <section className="section narrow"><PageTitle title={t.faq}/>{[1,2,3].map(n => <details className="faq" key={n}><summary>{t[`question${n}` as keyof Text]}</summary><p>{t[`answer${n}` as keyof Text]}</p></details>)}</section>
        : page === '/support' ? <section className="section narrow"><PageTitle title={t.supportTitle} text={t.supportText}/><div className="card support-card"><MessageCircle size={32}/><p>{store.data?.support?.[lang] || store.data?.support?.text || t.noSupport}</p>{store.data?.bot_username && <a className="button" href={`https://t.me/${store.data.bot_username}`} target="_blank" rel="noreferrer">{t.openTelegram}<ArrowUpRight size={17}/></a>}</div></section>
        : <Empty>{t.empty}</Empty>}
      </main>
    </div>
    {!portal && <footer><span className="brand"><b>{title}</b></span><p>{t.privacy}</p><Link to={href('/faq')}>{t.faq}</Link><Link to={href('/support')}>{t.support}</Link></footer>}
    {selected && identity && <Checkout t={t} lang={lang} plan={selected} account={renewal} writes={identity.writes_enabled} close={() => setSelected(undefined)} done={id => {setSelected(undefined); navigate(href(`/app/payments/${id}`));}}/>}
  </>;
}

function PageTitle({title,text}: {title:string;text?:string}) {return <div className="page-title"><h1>{title}</h1>{text && <p>{text}</p>}</div>;}
function PlanCards({data,t,lang,choose}: {data:ReturnType<typeof useData<Plan[]>>;t:Text;lang:Language;choose:(p:Plan)=>void}) {
  return <><Feedback {...data} retry={data.refresh} t={t}/><div className="plan-grid">{data.data?.map((p,index) => <article className="plan-card" key={p.id}><span className="plan-index">{String(index+1).padStart(2,'0')}</span><h3><bdi>{p.traffic_gb}</bdi> <span>{t.gb}</span></h3><p>{p.days} {t.days}{p.unlimited ? ` · ${t.unlimited}` : ''}</p><div className="plan-price"><Money amount={p.price} lang={lang}/></div><button className="button secondary" onClick={() => choose(p)}>{t.select}<ArrowUpRight size={17}/></button></article>)}</div>{data.data?.length === 0 && <Empty>{t.empty}</Empty>}</>;
}

function Login({t,slug,onSuccess}: {t:Text;slug?:string;onSuccess:()=>Promise<void>}) {
  const [challenge,setChallenge] = useState<{challenge:string;telegram_url:string}>(), [error,setError] = useState(''), [busy,setBusy] = useState(false);
  useEffect(() => {
    if (!challenge) return;
    let stopped = false, inFlight = false;
    const until = Date.now()+300000;
    const timer = setInterval(async () => {
      if (stopped || inFlight) return;
      if (Date.now()>until) {clearInterval(timer); setError(t.loginExpired); setChallenge(undefined); return;}
      inFlight = true;
      try {const result = await api<{status:string}>('/auth/consume',{method:'POST',body:JSON.stringify({challenge:challenge.challenge})}); if (result.status === 'authenticated' && !stopped) {clearInterval(timer); stopped = true; await onSuccess();}}
      catch(e) {if (!stopped) {setError((e as Error).message); clearInterval(timer); setChallenge(undefined);}}
      finally {inFlight = false;}
    },2000);
    return () => {stopped = true; clearInterval(timer);};
  },[challenge]);
  return <section className="login-card card"><div className="large-icon"><MessageCircle size={34}/></div><h1>{t.signedOut}</h1><p>{t.loginHelp}</p><Feedback error={error} t={t}/>{challenge ? <><a className="button" href={safeLink(challenge.telegram_url)} target="_blank" rel="noreferrer">{t.openTelegram}<ArrowUpRight size={18}/></a><div className="loading"><LoaderCircle className="spin" size={17}/>{t.waiting}</div></> : <button className="button" disabled={busy} onClick={async () => {setBusy(true); setError(''); try {setChallenge(await api('/auth/challenge',{method:'POST',body:JSON.stringify({storefront:slug})}));} catch(e) {setError((e as Error).message);} finally {setBusy(false);}}}>{busy ? t.loading : t.login}</button>}</section>;
}

function Overview({t,lang,href}: {t:Text;lang:Language;href:(s:string)=>string}) {
  const accounts = useData<Account[]>('/accounts'), payments = useData<Payment[]>('/payments'), credits = useData<{available:number}>('/credits');
  return <><PageTitle title={t.welcome} text={t.welcomeText}/><div className="metric-grid"><Metric title={t.activeConnections} value={accounts.data?.length ?? '—'} icon={<Link2/>}/><Metric title={t.availableCredit} value={<Money amount={credits.data?.available} lang={lang}/>} icon={<Wallet/>}/><Metric title={t.payments} value={payments.data?.length ?? '—'} icon={<ReceiptText/>}/></div><div className="section-heading"><h2>{t.connections}</h2><Link to={href('/app/accounts')}>{t.details}<ArrowUpRight size={16}/></Link></div><Feedback {...accounts} retry={accounts.refresh} t={t}/>{accounts.data?.length ? accounts.data.slice(0,3).map(a => <div className="connection-row card" key={`${a.server_id}:${a.username}`}><Link2/><strong><bdi>{a.username}</bdi></strong><span>{t.expires}: <DateText value={a.expires_at} lang={lang}/></span></div>) : !accounts.loading && <Empty>{t.noAccounts}</Empty>}<div className="section-heading"><h2>{t.recentPayments}</h2><Link to={href('/app/payments')}>{t.details}<ArrowUpRight size={16}/></Link></div><Feedback {...payments} t={t}/><PaymentTable data={payments.data?.slice(0,5) || []} t={t} lang={lang} href={href}/></>;
}
function Metric({title,value,icon}: {title:string;value:ReactNode;icon:ReactNode}) {return <div className="metric card"><span className="metric-icon">{icon}</span><p>{title}</p><strong>{value}</strong></div>;}

function Downloads({t,lang}:{t:Text;lang:Language}) {
  const state=useData<{id:string;platform:string;label:string;url:string;details:string}[]>(`/downloads?language=${lang}`);
  return <><Feedback {...state} t={t}/>{state.data?.map(app=><details className="faq download-guide" key={`${app.platform}:${app.id}`}><summary><bdi>{app.platform}</bdi> · {app.label}</summary><p>{app.details.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part,i)=>part.startsWith('**')?<strong key={i}>{part.slice(2,-2)}</strong>:part.startsWith('`')?<code key={i}>{part.slice(1,-1)}</code>:part)}</p><a className="button secondary" href={safeLink(app.url)} target="_blank" rel="noreferrer">{app.label}<ArrowUpRight size={16}/></a></details>)}</>;
}

function Connections({t,lang,plans,choose,writes}: {t:Text;lang:Language;plans:Plan[];choose:(p:Plan,a:Account)=>void;writes:boolean}) {
  const state = useData<Account[]>('/accounts');
  const [config,setConfig] = useState<{account:Account;values:Record<string,string>}>(),[error,setError] = useState(''),[copied,setCopied] = useState(false),[opening,setOpening] = useState('');
  return <><PageTitle title={t.connections}/><Trial t={t} lang={lang} writes={writes} onComplete={state.refresh}/><Feedback {...state} error={state.error || error} retry={state.refresh} t={t}/>{state.data?.length === 0 && <Empty>{t.noAccounts}</Empty>}<div className="connection-grid">{state.data?.map(a => <article className="card account-card" key={`${a.server_id}:${a.username}`}><div className="account-top"><span className="metric-icon"><Link2/></span><span className={`badge ${a.available ? 'good' : 'warning'}`}>{a.available ? a.state : t.unavailable}</span></div><h2><bdi>{a.username}</bdi></h2><div className="usage-line"><span>{t.usage}</span><bdi>{(a.used_bytes/1073741824).toFixed(1)} / {(a.limit_bytes/1073741824).toFixed(0)} {t.gb}</bdi></div><progress max={Math.max(a.limit_bytes,1)} value={a.used_bytes} aria-label={t.usage}/><p className="expiry">{t.expires}<DateText value={a.expires_at} lang={lang}/></p><div className="account-actions"><button disabled={!a.available || opening === a.username} className="button secondary" onClick={async () => {setOpening(a.username);setError('');try {const values = await api<Record<string,string>>(`/accounts/${encodeURIComponent(a.server_id)}/${encodeURIComponent(a.username)}/configuration`);setConfig({account:a,values});setCopied(false);} catch(e){setError((e as Error).message);} finally{setOpening('');}}}>{t.configuration}</button><button className="text-button" disabled={!writes || !a.available || !plans.length} onClick={() => choose(plans.find(p => p.traffic_gb === a.limit_bytes/1073741824) || plans[0],a)}>{t.renew}</button></div></article>)}</div>{config && <Modal title={t.configuration} t={t} close={() => setConfig(undefined)}><img className="qr" alt={t.configuration} src={`/api/v1/accounts/${encodeURIComponent(config.account.server_id)}/${encodeURIComponent(config.account.username)}/qr`}/>{Object.entries(config.values).map(([key,value]) => <div key={key}><label>{key}<textarea readOnly dir="ltr" value={value}/></label><button className="button secondary" onClick={async () => {try{await navigator.clipboard.writeText(value);setCopied(true);}catch{setError('Copy is unavailable. Select the configuration text to copy it.');}}}>{copied ? <Check size={16}/> : <Copy size={16}/>} {copied ? t.copied : t.copy}</button></div>)}</Modal>}</>;
}

function Checkout({t,lang,plan,account,writes,close,done}: {t:Text;lang:Language;plan:Plan;account?:Account;writes:boolean;close:()=>void;done:(id:string)=>void}) {
  const [method,setMethod] = useState<'crypto'|'card'>('crypto'), [reserved,setReserved] = useState(false),[busy,setBusy] = useState(false),[error,setError] = useState('');
  const key = useRef(crypto.randomUUID());
  return <Modal title={account ? t.renew : t.checkout} close={close} t={t}><div className="checkout-summary"><h3>{plan.traffic_gb} {t.gb} / {plan.days} {t.days}</h3><strong><Money amount={plan.price} lang={lang}/></strong>{account && <p><bdi>{account.username}</bdi></p>}</div><label>{t.paymentMethod}<select disabled={busy} value={method} onChange={e => {setMethod(e.target.value as 'crypto'|'card');key.current=crypto.randomUUID();}}><option value="crypto">{t.crypto}</option>{lang==='fa' && <option value="card">{t.card}</option>}</select></label>{account && <label className="checkbox"><input type="checkbox" checked={reserved} onChange={e => {setReserved(e.target.checked);key.current=crypto.randomUUID();}}/>{t.reserve}</label>}<Feedback error={error} t={t}/><button className="button full" disabled={busy || !writes} onClick={async () => {setBusy(true);setError('');try {const result=await api<Payment>('/orders',{method:'POST',headers:{'Idempotency-Key':key.current},body:JSON.stringify({plan_id:plan.id,method,username:account?.username,server_id:account?.server_id,reserved})});done(result.id);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>{busy?t.loading:t.continue}<ArrowRight size={18}/></button></Modal>;
}

function PaymentTable({data,t,lang,href}: {data:Payment[];t:Text;lang:Language;href:(s:string)=>string}) {
  if (!data.length) return <Empty>{t.empty}</Empty>;
  return <div className="table-wrap card"><table><thead><tr><th>{t.plans}</th><th>{t.amount}</th><th>{t.status}</th><th>{t.date}</th><th>{t.details}</th></tr></thead><tbody>{data.map(p => <tr key={p.id}><td><bdi>{p.plan_gb || '—'}</bdi></td><td><Money amount={p.price} lang={lang}/></td><td><span className="badge">{p.status}</span></td><td><DateText value={p.created_at} lang={lang}/></td><td><Link to={href(`/app/payments/${p.id}`)}>{t.details}<ArrowUpRight size={14}/></Link></td></tr>)}</tbody></table></div>;
}
function Payments({t,lang,href}: {t:Text;lang:Language;href:(s:string)=>string}) {const state=useData<Payment[]>('/payments');return <><PageTitle title={t.payments}/><Feedback {...state} retry={state.refresh} t={t}/>{state.data && <PaymentTable data={state.data} t={t} lang={lang} href={href}/>}</>;}
function PaymentDetails({id,t,lang,writes}: {id:string;t:Text;lang:Language;writes:boolean}) {
  const state=useData<Payment>(`/payments/${encodeURIComponent(id)}`),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(() => {if (!state.data || ['completed','cancelled','rejected','uncertain'].includes(state.data.status || '')) return;const timer=setInterval(state.refresh,10000);return()=>clearInterval(timer);},[state.data?.status]);
  const p=state.data;
  return <><PageTitle title={t.paymentDetails}/><Feedback {...state} error={state.error || error} retry={state.refresh} t={t}/>{p && <div className="card payment-detail"><span className="badge">{p.status}</span><h2><Money amount={p.price} lang={lang}/></h2><p className="muted"><bdi>{p.id}</bdi></p>{safeLink(p.payment_url) && <a className="button" target="_blank" rel="noreferrer" href={safeLink(p.payment_url)}>{t.payNow}<ArrowUpRight size={17}/></a>}{p.status==='waiting_receipt' && <><p><bdi>{p.converted_amount?.toLocaleString()} {p.converted_currency}</bdi></p><pre dir="ltr">{p.card_number}</pre><p>{t.receiptHelp}</p><label className="button secondary">{busy?t.loading:t.upload}<input className="file-input" type="file" accept="image/png,image/jpeg" disabled={busy || !writes} onChange={async e=>{const file=e.target.files?.[0];if(!file)return;setBusy(true);setError('');try{const form=new FormData();form.append('file',file);await api(`/payments/${id}/receipt`,{method:'POST',body:form});state.refresh();}catch(err){setError((err as Error).message);}finally{setBusy(false);}}}/></label><button className="text-button danger" disabled={!writes || busy} onClick={async()=>{setBusy(true);try{await api(`/payments/${id}/cancel`,{method:'POST'});state.refresh();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>{t.cancelOrder}</button></>}</div>}</>;
}


function Reseller({page,t,lang,writes}: {page:string;t:Text;lang:Language;writes:boolean}) {
  const summary=useData<{debt:number;balance:{available:number};credit_policy:{effective_limit:number}}>('/reseller');
  const [q,setQ]=useState(''), [slug,setSlug]=useState(''),[title,setTitle]=useState(''),[error,setError]=useState(''),[saved,setSaved]=useState(''),[busy,setBusy]=useState(false);
  const customers=useData<{username:string;server_id:string}[]>(page==='/reseller/customers'?`/reseller/customers?q=${encodeURIComponent(q)}`:null);
  if(page==='/reseller/storefront')return <><PageTitle title={t.storefront}/><form className="card detail-card" onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{const result=await api<{path:string}>('/reseller/storefront',{method:'PUT',body:JSON.stringify({slug,title})});setSaved(result.path);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}><label>{t.storefrontTitle}<input value={title} maxLength={80} required onChange={e=>setTitle(e.target.value)}/></label><label>{t.storefrontSlug}<input value={slug} dir="ltr" pattern="[a-z0-9]+(-[a-z0-9]+)*" minLength={3} maxLength={60} required onChange={e=>setSlug(e.target.value)}/></label><Feedback error={error} t={t}/>{saved && <p role="status">{t.saved}: <Link to={saved}>{saved}</Link></p>}<button className="button" disabled={!writes || busy}>{busy?t.loading:t.save}</button></form></>;
  if(page==='/reseller/customers')return <><PageTitle title={t.customers}/><input className="search" aria-label={t.search} placeholder={t.search} value={q} onChange={e=>setQ(e.target.value)}/><Feedback {...customers} t={t}/>{customers.data?.map(c=><div className="connection-row card" key={`${c.server_id}:${c.username}`}><Users size={20}/><bdi>{c.username}</bdi></div>)}{customers.data?.length===0 && <Empty>{t.empty}</Empty>}</>;
  return <><PageTitle title={t.reseller}/><Feedback {...summary} t={t}/>{summary.data && <div className="metric-grid"><Metric title={t.prepaid} value={<Money amount={summary.data.balance.available} lang={lang}/>} icon={<Wallet/>}/><Metric title={t.debt} value={<Money amount={summary.data.debt} lang={lang}/>} icon={<ReceiptText/>}/><Metric title={t.credit} value={<Money amount={summary.data.credit_policy.effective_limit} lang={lang}/>} icon={<ShieldCheck/>}/></div>}</>;
}

function Admin({page,t,lang,writes}: {page:string;t:Text;lang:Language;writes:boolean}) {
  const isAudit=page==='/admin/audit',isReviews=page.startsWith('/admin/payments');
  const overview=useData<Record<string,number>>(!isAudit && !isReviews?'/admin/overview':null), reviews=useData<Payment[]>(isReviews?'/admin/payments':null),audit=useData<{id:number;actor:string;action:string;resource:string}[]>(isAudit?'/admin/audit':null);
  const [decision,setDecision]=useState<{id:string;approve:boolean}>(),[reason,setReason]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  return <><PageTitle title={isAudit?t.audit:isReviews?t.reviews:t.admin}/><Feedback error={overview.error || reviews.error || audit.error || error} loading={isAudit?audit.loading:isReviews?reviews.loading:overview.loading} t={t}/>{overview.data && <div className="metric-grid">{[['resellers',t.reseller],['pending_payments',t.pending],['pending_notifications',t.notifications],['uncertain_operations',t.attention]].map(([key,label])=><Metric key={key} title={label} value={overview.data![key]} icon={<LayoutDashboard/>}/>)}</div>}{reviews.data?.map(p=><article className="card review-card" key={p.id}><div><h2><Money amount={p.price} lang={lang}/></h2><p><bdi>{p.id}</bdi></p></div>{p.receipt_id && <a target="_blank" rel="noreferrer" href={`/api/v1/receipts/${p.receipt_id}`}><img className="receipt-preview" alt={t.upload} src={`/api/v1/receipts/${p.receipt_id}`}/></a>}{p.review_in_telegram ? <p>{t.legacyReview}</p>:<div className="account-actions"><button className="button" disabled={!writes} onClick={()=>{setDecision({id:p.id,approve:true});setReason('');}}>{t.approve}</button><button className="button secondary danger" disabled={!writes} onClick={()=>{setDecision({id:p.id,approve:false});setReason('');}}>{t.reject}</button></div>}</article>)}{reviews.data?.length===0 && <Empty>{t.empty}</Empty>}{audit.data && <div className="table-wrap card"><table><thead><tr><th>{t.account}</th><th>{t.details}</th><th>{t.payments}</th></tr></thead><tbody>{audit.data.map(row=><tr key={row.id}><td>{row.actor}</td><td>{row.action}</td><td><bdi>{row.resource}</bdi></td></tr>)}</tbody></table></div>}{decision && <Modal title={t.reviewConfirm} close={()=>setDecision(undefined)} t={t}><p><bdi>{decision.id}</bdi> — {decision.approve?t.approve:t.reject}</p><label>{t.reason}<textarea minLength={3} maxLength={500} value={reason} onChange={e=>setReason(e.target.value)}/></label><Feedback error={error} t={t}/><button className="button" disabled={busy || reason.trim().length<3} onClick={async()=>{setBusy(true);setError('');try{await api(`/admin/payments/${decision.id}/review`,{method:'POST',body:JSON.stringify({approve:decision.approve,reason})});setDecision(undefined);reviews.refresh();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>{busy?t.loading:t.reviewConfirm}</button></Modal>}</>;
}

createRoot(document.getElementById('root')!).render(<BrowserRouter><Application/></BrowserRouter>);
