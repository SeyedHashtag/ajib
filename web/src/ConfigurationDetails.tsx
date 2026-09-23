import {useState} from 'react';
import {Check, Copy} from 'lucide-react';
import type {Configuration, Language} from './api';
import type {Text} from './i18n';

const words = {
  en: {subscription: 'Subscription', connection: 'Connection', download: 'Download configurations', copyError: 'Select the configuration text to copy it.'},
  fa: {subscription: 'اشتراک', connection: 'اتصال', download: 'دانلود پیکربندی‌ها', copyError: 'برای کپی، متن پیکربندی را انتخاب کنید.'},
  ru: {subscription: 'Подписка', connection: 'Подключение', download: 'Скачать конфигурации', copyError: 'Выделите текст конфигурации, чтобы скопировать его.'},
  tk: {subscription: 'Abuna', connection: 'Birikme', download: 'Sazlamalary ýükle', copyError: 'Göçürmek üçin sazlamanyň tekstini saýlaň.'},
};

export function ConfigurationDetails({base, values, lang, t}: {base:string; values:Configuration; lang:Language; t:Text}) {
  const entries = Object.entries(values).sort(([a], [b]) => Number(b === 'sub_url') - Number(a === 'sub_url'))
    .filter(([,value], index, all) => all.findIndex(([,other]) => other === value) === index);
  const [selected, setSelected] = useState(entries[0]?.[0] || '');
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState('');
  const [qrFailed, setQrFailed] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const text = words[lang];
  if (!entries.length) return <p>{t.unavailable}</p>;
  return <>
    <label>{t.configuration}<select value={selected} onChange={event => {setSelected(event.target.value);setCopied(false);setError('');setQrFailed(false);}}>
      {entries.map(([key], index) => <option key={key} value={key}>{key === 'sub_url' ? text.subscription : `${text.connection} ${new Intl.NumberFormat(lang).format(index + (entries[0][0] === 'sub_url' ? 0 : 1))}`}</option>)}
    </select></label>
    <img className="qr" hidden={qrFailed} alt={t.configuration} onLoad={() => setQrFailed(false)} onError={() => setQrFailed(true)} src={`${base}/qr?key=${encodeURIComponent(selected)}&refresh=${refresh}`}/>
    {qrFailed && <p role="alert">{t.unavailable} <button className="text-button" onClick={() => {setQrFailed(false);setRefresh(value => value + 1);}}>{t.retry}</button></p>}
    <label>{t.configuration}<textarea readOnly dir="ltr" value={values[selected]}/></label>
    {error && <p role="alert">{error}</p>}
    <button className="button secondary" onClick={async () => {
      try {await navigator.clipboard.writeText(values[selected]);setCopied(true);setError('');}
      catch {setError(text.copyError);}
    }}>{copied ? <Check size={16}/> : <Copy size={16}/>} {copied ? t.copied : t.copy}</button>
    <a className="button secondary" href={`${base}/configuration.txt`} download="connections.txt">{text.download}</a>
  </>;
}
