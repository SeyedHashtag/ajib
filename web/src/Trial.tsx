import {useEffect, useRef, useState} from 'react';
import {api, type Language} from './api';
import type {Text} from './i18n';

const words={
  en:{title:'Try your connection',description:'A 1 GB trial with a 30-day service period, subject to your existing eligibility.',request:'Request a trial',queued:'Your request is in the shared trial queue.',processing:'Your trial is being created.',uncertain:'Your request needs support review. Please do not request another trial.',completed:'Your trial is ready in My connections.',connected:'I have connected successfully',confirmed:'Connection confirmed',disabled:'Trial creation is paused. You can join the waiting list.',used:'Your trial eligibility has already been used.'},
  fa:{title:'اتصال خود را امتحان کنید',description:'تست ۱ گیگابایتی با دوره سرویس ۳۰ روزه، مطابق شرایط فعلی حساب شما.',request:'درخواست تست',queued:'درخواست شما در صف مشترک تست ثبت شده است.',processing:'تست شما در حال ساخت است.',uncertain:'درخواست شما نیاز به بررسی پشتیبانی دارد. لطفاً دوباره درخواست ندهید.',completed:'تست شما در بخش اتصال‌های من آماده است.',connected:'با موفقیت متصل شدم',confirmed:'اتصال تأیید شد',disabled:'ساخت تست متوقف است. می‌توانید به صف انتظار بپیوندید.',used:'سهمیه تست شما قبلاً استفاده شده است.'},
  ru:{title:'Проверьте подключение',description:'Пробный тариф 1 ГБ со сроком обслуживания 30 дней, согласно условиям вашего аккаунта.',request:'Запросить пробный тариф',queued:'Заявка находится в общей очереди.',processing:'Создаём пробное подключение.',uncertain:'Заявка требует проверки поддержки. Не отправляйте повторную заявку.',completed:'Пробный тариф готов в разделе подключений.',connected:'Подключение работает',confirmed:'Подключение подтверждено',disabled:'Создание приостановлено. Можно встать в очередь.',used:'Ваш пробный тариф уже использован.'},
  tk:{title:'Birikmäňizi synap görüň',description:'Hasabyňyzyň şertlerine laýyklykda 30 günlük hyzmat möhletli 1 GB synag.',request:'Synag sora',queued:'Haýyşyňyz umumy synag nobatynda.',processing:'Synagyňyz döredilýär.',uncertain:'Haýyşyňyza goldaw toparynyň barlagy gerek. Täze haýyş ibermäň.',completed:'Synagyňyz birikmeler bölüminde taýýar.',connected:'Üstünlikli birikdim',confirmed:'Birikme tassyklandy',disabled:'Synag döretmek saklandy. Garaşýanlaryň nobatyna goşulyp bilersiňiz.',used:'Synag mümkinçiligiňiz eýýäm ulanyldy.'},
};
type State={available:boolean;eligible:boolean;creation_disabled:boolean;queued:boolean;status:string;account:{username?:string;connected_at?:string}|null};

export function Trial({t,lang,writes,onComplete}:{t:Text;lang:Language;writes:boolean;onComplete:()=>void}) {
  const [data,setData]=useState<State>(),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const key=useRef(crypto.randomUUID()),refreshAccounts=useRef(onComplete);refreshAccounts.current=onComplete;
  const r=words[lang];
  useEffect(()=>{
    let stopped=false,previous='',inFlight=false;
    const refresh=async()=>{
      if(inFlight)return;inFlight=true;
      try {const value=await api<State>('/trial');if(!stopped){setData(value);if(previous && previous!=='completed' && value.status==='completed')refreshAccounts.current();previous=value.status;}}
      catch(e){if(!stopped)setError((e as Error).message);}finally{inFlight=false;}
    };
    void refresh();const timer=setInterval(()=>{if(!document.hidden)void refresh();},10000);
    return()=>{stopped=true;clearInterval(timer);};
  },[]);
  const submit=async(connected=false)=>{
    setBusy(true);setError('');
    try{await api(connected?'/trial/connected':'/trial',{method:'POST',headers:{'Idempotency-Key':key.current}});setData(await api<State>('/trial'));}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  };
  if(data&&!data.available)return null;
  const pending=data&&['queued','processing','uncertain'].includes(data.status);
  return <section className="card trial-card"><h2>{r.title}</h2><p>{r.description}</p>{error&&<p className="notice error" role="alert">{error}</p>}
    {!data?<p role="status">{t.loading}</p>:<>
      {pending?<p role="status">{r[data.status as 'queued'|'processing'|'uncertain']}</p>:data.account?<p>{r.completed}</p>:!data.eligible?<p>{r.used}</p>:data.creation_disabled?<p>{r.disabled}</p>:null}
      {data.eligible&&!pending&&<button className="button secondary" disabled={!writes||busy} onClick={()=>void submit()}>{busy?t.loading:r.request}</button>}
      {data.account?.username&&(data.account.connected_at?<p>{r.confirmed}</p>:<button className="text-button" disabled={!writes||busy} onClick={()=>void submit(true)}>{r.connected}</button>)}
    </>}
  </section>;
}
