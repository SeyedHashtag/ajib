import {useEffect, useRef, useState} from 'react';
import {api, type Language} from './api';
import type {Text} from './i18n';
import {rewardText} from './rewards-i18n';

type Summary = {
  code:string|null; invited_count:number; available_balance_cents:number; wallet:string|null;
  minimum_withdrawal:number; actions_available:boolean;
  withdrawals:{id:string;amount:number;status:string;requested_at:string;wallet:string}[];
  recruitment:{reseller_id:string;reward_amount:number}[];
};

export function Rewards({t,lang,writes}:{t:Text;lang:Language;writes:boolean}) {
  const r=rewardText[lang];
  const [data,setData]=useState<Summary>(),[error,setError]=useState(''),[busy,setBusy]=useState(false),[saved,setSaved]=useState(false);
  const [wallet,setWallet]=useState(''),[code,setCode]=useState(''),[confirm,setConfirm]=useState(false);
  const keys=useRef(new Map<string,string>());
  const money=(amount:number)=>new Intl.NumberFormat(lang==='tk'?'en':lang,{style:'currency',currency:'USD'}).format(amount);
  const refresh=async()=>{const result=await api<Summary>('/referrals');setData(result);setWallet(result.wallet||'');};
  useEffect(()=>{void refresh().catch(e=>setError(e.message));},[]);
  const action=async(path:string,payload:object={},method='POST')=>{
    if(busy)return;
    setBusy(true);setError('');setSaved(false);
    const request=path+JSON.stringify(payload);
    if(!keys.current.has(request))keys.current.set(request,crypto.randomUUID());
    try {
      await api('/referrals/'+path,{method,body:JSON.stringify(payload),headers:{'Idempotency-Key':keys.current.get(request)!}});
      keys.current.delete(request);setConfirm(false);setSaved(true);await refresh();
    } catch(e){setError((e as Error).message);} finally{setBusy(false);}
  };
  const disabled=!writes||busy||!data?.actions_available;
  const canWithdraw=data && data.available_balance_cents>=data.minimum_withdrawal*100 && data.wallet && !data.withdrawals.some(w=>w.status==='pending');
  return <>
    <div className="page-title"><h1>{t.referrals}</h1><p>{t.inviteText}</p></div>
    {error && <div className="notice error" role="alert">{error}<button className="text-button" onClick={()=>void refresh().then(()=>setError('')).catch(e=>setError(e.message))}>{t.retry}</button></div>}
    {saved && <p role="status">{t.saved}</p>}
    {!data && !error && <p role="status">{t.loading}</p>}
    {data && <>
      <div className="metric-grid"><div className="card metric"><p>{t.invited}</p><strong>{data.invited_count}</strong></div><div className="card metric"><p>{t.rewards}</p><strong><bdi>{money(data.available_balance_cents/100)}</bdi></strong></div></div>
      {!data.actions_available && <div className="notice">{r.bot}</div>}
      <div className="rewards-grid">
        <section className="card detail-card"><h2>{t.referralCode}</h2>{data.code ? <p><bdi>{data.code}</bdi></p> : <button className="button secondary" disabled={disabled} onClick={()=>void action('code')}>{r.generate}</button>}
          <form onSubmit={e=>{e.preventDefault();void action('attribution',{code});}}><label>{r.attribution}<input value={code} maxLength={64} required onChange={e=>setCode(e.target.value)} autoCapitalize="none" dir="ltr"/></label><button className="button secondary" disabled={disabled||!code}>{r.apply}</button></form>
        </section>
        <section className="card detail-card"><h2>{r.wallet}</h2><p>{r.walletHelp}</p>
          <form onSubmit={e=>{e.preventDefault();void action('wallet',{address:wallet},'PUT');}}><label>{r.wallet}<input value={wallet} required minLength={10} maxLength={256} onChange={e=>setWallet(e.target.value)} autoCapitalize="none" spellCheck={false} dir="ltr"/></label><button className="button secondary" disabled={disabled||wallet===data.wallet}>{t.save}</button></form>
          <p>{r.minimum}: <bdi>{money(data.minimum_withdrawal)}</bdi></p>
          {!confirm ? <button className="button" disabled={disabled||!canWithdraw} onClick={()=>setConfirm(true)}>{r.withdraw}</button> : <div className="withdrawal-confirm"><h3>{r.confirm}</h3><p><bdi>{money(data.available_balance_cents/100)}</bdi></p><p><bdi>{data.wallet}</bdi></p><div className="account-actions"><button className="button" disabled={disabled} onClick={()=>void action('withdrawals')}>{r.confirm}</button><button className="text-button" disabled={busy} onClick={()=>setConfirm(false)}>{t.close}</button></div></div>}
        </section>
      </div>
      {data.recruitment.length>0 && <section className="section"><h2>{r.recruitment}</h2>{data.recruitment.map(item=><div className="card review-card" key={item.reseller_id}><bdi>#{item.reseller_id} · {money(item.reward_amount)}</bdi><div className="account-actions"><button className="button secondary" disabled={disabled} onClick={()=>void action('recruitment',{reseller_id:item.reseller_id,choice:'cash'})}>{r.cash}</button><button className="button secondary" disabled={disabled} onClick={()=>void action('recruitment',{reseller_id:item.reseller_id,choice:'credit'})}>{r.credit}</button></div></div>)}</section>}
      <section className="section"><h2>{r.history}</h2>{data.withdrawals.length===0 ? <p>{t.empty}</p> : <div className="card table-wrap"><table><thead><tr><th>{t.amount}</th><th>{t.status}</th><th>{t.date}</th><th>{r.wallet}</th></tr></thead><tbody>{data.withdrawals.map(item=><tr key={item.id}><td><bdi>{money(item.amount)}</bdi></td><td>{r[item.status as keyof typeof r]||item.status}</td><td><bdi>{new Date(item.requested_at).toLocaleDateString(lang==='tk'?'en':lang)}</bdi></td><td><bdi>{item.wallet}</bdi></td></tr>)}</tbody></table></div>}</section>
    </>}
  </>;
}
