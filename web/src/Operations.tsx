import {useEffect, useState} from 'react';
import {api,type Language} from './api';
import type {Text} from './i18n';

const words={
  en:{worker:'Background worker',unknown:'No heartbeat received',seen:'Last heartbeat',queue:'Outstanding operations',retries:'Highest notification retry count',scope:'Storefront',kind:'Operation',paused:'Paused'},
  fa:{worker:'پردازشگر پس‌زمینه',unknown:'هنوز پیام سلامت دریافت نشده',seen:'آخرین پیام سلامت',queue:'عملیات ناتمام',retries:'بیشترین تعداد تلاش ارسال اعلان',scope:'فروشگاه',kind:'عملیات',paused:'متوقف'},
  ru:{worker:'Фоновый обработчик',unknown:'Нет сигнала состояния',seen:'Последний сигнал',queue:'Незавершённые операции',retries:'Максимальное число повторов уведомления',scope:'Магазин',kind:'Операция',paused:'Приостановлено'},
  tk:{worker:'Fon işçisi',unknown:'Ýagdaý habary gelmedi',seen:'Soňky ýagdaý habary',queue:'Tamamlanmadyk işler',retries:'Bildirişiň iň köp gaýtalama sany',scope:'Dükan',kind:'Iş',paused:'Saklandy'},
};
type Data={operations:{id:string;scope:string;user_id:string;kind:string;status:string;created_at:number}[];worker:{heartbeat_at:number;last_success_at:number|null;last_error:string|null;writes_enabled:number}|null;notifications:{pending:number;oldest_due_at:number|null;maximum_attempts:number|null}};
export function Operations({t,lang}:{t:Text;lang:Language}) {
  const [data,setData]=useState<Data>(),[error,setError]=useState('');const r=words[lang];
  const refresh=()=>api<Data>('/admin/operations').then(value=>{setData(value);setError('');}).catch(e=>setError(e.message));
  useEffect(()=>{void refresh();},[]);
  const date=(seconds:number)=>new Date(seconds*1000).toLocaleString(lang==='tk'?'en':lang);
  return <><div className="page-title"><h1>{t.attention}</h1><button className="text-button" onClick={()=>void refresh()}>{t.retry}</button></div>{error&&<div className="notice error" role="alert">{error}</div>}
    {data&&<><div className="rewards-grid"><section className="card detail-card"><h2>{r.worker}</h2>{data.worker?<><p>{r.seen}: <bdi>{date(data.worker.heartbeat_at)}</bdi></p>{Date.now()/1000-data.worker.heartbeat_at>180&&<p className="notice">{t.attention}</p>}{!data.worker.writes_enabled&&<p>{r.paused}</p>}{data.worker.last_error&&<p className="notice error">{data.worker.last_error}</p>}</>:<p className="notice">{r.unknown}</p>}</section><section className="card detail-card"><h2>{t.notifications}</h2><p>{data.notifications.pending}</p><p>{r.retries}: {data.notifications.maximum_attempts||0}</p></section></div>
      <section className="section"><h2>{r.queue}</h2>{data.operations.length===0?<p>{t.empty}</p>:<div className="card table-wrap"><table><thead><tr><th>{t.details}</th><th>{r.scope}</th><th>{t.account}</th><th>{r.kind}</th><th>{t.status}</th><th>{t.date}</th></tr></thead><tbody>{data.operations.map(item=><tr key={item.id}><td><bdi>{item.id}</bdi></td><td>{item.scope}</td><td>{item.user_id}</td><td>{item.kind}</td><td>{item.status}</td><td><bdi>{date(item.created_at)}</bdi></td></tr>)}</tbody></table></div>}</section></>}
    {!data&&!error&&<p role="status">{t.loading}</p>}
  </>;
}
