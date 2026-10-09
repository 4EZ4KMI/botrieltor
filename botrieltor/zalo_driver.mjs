#!/usr/bin/env node
import { createInterface } from 'node:readline';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const profile = process.argv[2];
const port = Number(process.argv[3] || 49222);
const endpoint = `http://127.0.0.1:${port}`;
let socket = null;
let sequence = 0;
let activeSource = '';
const pending = new Map();

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function conversationPaneExpression() {
  return `(() => {
    const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>3&&r.height>3&&r.top<innerHeight&&r.bottom>0&&r.left>innerWidth*.22&&s.visibility!=='hidden'&&s.display!=='none'};
    const candidates=[...document.querySelectorAll('div')].filter(e=>visible(e)&&e.scrollHeight>e.clientHeight+100);
    candidates.sort((a,b)=>(b.clientWidth*b.clientHeight)-(a.clientWidth*a.clientHeight));
    return candidates[0]||null;
  })()`;
}
function normalizeChatName(value) {
  return String(value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
}
function canonicalChatName(value) {
  return normalizeChatName(value).toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
}
async function readActiveChatName(expected='') {
  const expectedKey = canonicalChatName(expected);
  return evaluate(`(() => {
    const norm=s=>String(s||'').normalize('NFC').replace(/\\s+/g,' ').trim();
    const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>3&&r.height>3&&r.top>=0&&r.top<150&&r.left>innerWidth*.22&&s.visibility!=='hidden'&&s.display!=='none'};
    const key=s=>norm(s).toLocaleLowerCase().replace(/[^\\p{L}\\p{N}]+/gu,'');
    const expectedKey=${literal(expectedKey)};
    if([...document.querySelectorAll('.chat-onboard__header')].some(visible))return '';
    const selectors=['#header .header-title'];
    const candidates=[...new Set(selectors.flatMap(selector=>[...document.querySelectorAll(selector)]))].filter(visible)
      .map((e,index)=>({text:norm(e.textContent),priority:index})).filter(x=>x.text.length>0&&x.text.length<200);
    if(expectedKey){const match=candidates.find(item=>key(item.text)===${literal(expectedKey)});if(match)return match.text;}
    if(candidates.length){candidates.sort((a,b)=>a.priority-b.priority||a.text.length-b.text.length);return candidates[0].text;}
    return '';
  })()`);
}
async function activeChatMatches(name) {
  return canonicalChatName(await readActiveChatName(name)) === canonicalChatName(name);
}
async function waitForActiveChat(name) {
  for (let attempt = 0; attempt < 20; attempt++) {
    const observed = await readActiveChatName(name);
    if (canonicalChatName(observed) === canonicalChatName(name)) return observed;
    await delay(250);
  }
  const observed = await readActiveChatName(name);
  throw new Error(`Zalo не подтвердил группу «${normalizeChatName(name)}» (в заголовке «${normalizeChatName(observed)}»). Снимок не сохранён.`);
}
async function json(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`CDP HTTP ${response.status}`);
  return response.json();
}
async function chromeAvailable() {
  try { await json(endpoint + '/json/version'); return true; } catch { return false; }
}
function chromeCommand() {
  if (process.platform === 'win32') {
    const candidates = [
      process.env.CHROME_PATH,
      process.env.LOCALAPPDATA && join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      process.env.PROGRAMFILES && join(process.env.PROGRAMFILES, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      (process.env['ProgramFiles(x86)'] || process.env['PROGRAMFILES(X86)']) &&
        join(process.env['ProgramFiles(x86)'] || process.env['PROGRAMFILES(X86)'], 'Google', 'Chrome', 'Application', 'chrome.exe'),
      'chrome.exe',
    ].filter(Boolean);
    return candidates.find(candidate => candidate === 'chrome.exe' || existsSync(candidate)) || 'chrome.exe';
  }
  return process.platform === 'darwin' ? '/usr/bin/open' : 'google-chrome';
}
async function ensureChrome() {
  if (await chromeAvailable()) return;
  const command = chromeCommand();
  const args = process.platform === 'darwin'
    ? ['-na', 'Google Chrome', '--args', `--user-data-dir=${profile}`,
      `--remote-debugging-port=${port}`, `--remote-allow-origins=${endpoint}`,
      '--no-first-run', '--no-default-browser-check', 'https://chat.zalo.me/']
    : [`--user-data-dir=${profile}`, `--remote-debugging-port=${port}`,
      `--remote-allow-origins=${endpoint}`, '--no-first-run',
      '--no-default-browser-check', 'https://chat.zalo.me/'];
  let launchError = '';
  const child = spawn(command, args, {detached:true, stdio:'ignore', windowsHide:true});
  child.once('error', error => { launchError = error.message || String(error); });
  child.unref();
  for (let i = 0; i < 80; i++) {
    await delay(250);
    try { await json(endpoint + '/json/version'); return; } catch {}
  }
  throw new Error(launchError
    ? `Не удалось запустить Google Chrome (${launchError}). Установите Chrome или задайте CHROME_PATH.`
    : 'Управляемый Chrome не запустился.');
}
async function target({createIfMissing=false}={}) {
  if (!(await chromeAvailable())) return null;
  let pages = (await json(endpoint + '/json/list')).filter(x => x.type === 'page');
  let page = pages.find(x => x.url.includes('zalo.me'));
  if (!page && createIfMissing) {
    page = await json(endpoint + '/json/new?https%3A%2F%2Fchat.zalo.me%2F', {method: 'PUT'});
    await delay(1000);
  }
  return page;
}
async function connect() {
  if (socket && socket.readyState === WebSocket.OPEN) return;
  const page = await target();
  if (!page) throw new Error('Управляемая вкладка Zalo не открыта. Нажмите «Открыть профиль Zalo».');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  socket.onmessage = event => {
    const message = JSON.parse(String(event.data));
    if (!message.id || !pending.has(message.id)) return;
    const {resolve, reject} = pending.get(message.id); pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message)); else resolve(message.result);
  };
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  await send('Runtime.enable');
}
async function send(method, params={}, timeoutMs=15000) {
  await connect();
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params}));
    setTimeout(() => { if (pending.delete(id)) reject(new Error(`CDP timeout: ${method}`)); }, timeoutMs);
  });
}
async function evaluate(expression, awaitPromise=false, timeoutMs=15000) {
  const result = await send('Runtime.evaluate', {expression, awaitPromise, returnByValue: true}, timeoutMs);
  if (result.exceptionDetails) {
    const details = result.exceptionDetails;
    const description = details.exception?.description || details.exception?.value || details.text || 'Ошибка сценария Zalo';
    const location = Number.isInteger(details.lineNumber)
      ? ` (строка ${details.lineNumber + 1}${Number.isInteger(details.columnNumber) ? `:${details.columnNumber + 1}` : ''})`
      : '';
    throw new Error(`${description}${location}`);
  }
  return result.result?.value;
}
async function pressEscape() {
  const key={key:'Escape',code:'Escape',windowsVirtualKeyCode:27,nativeVirtualKeyCode:53};
  await send('Input.dispatchKeyEvent', {type:'keyDown', ...key});
  await send('Input.dispatchKeyEvent', {type:'keyUp', ...key});
}
async function closePhotoViewer(force=false) {
  if (force) {
    try { await pressEscape(); } catch {}
    let marked=false;
    try { marked=await evaluate('Boolean(window.__botrieltorPhotoViewer)'); } catch {}
    if(!marked)return true;
  }
  for (let attempt=0;attempt<3;attempt++) {
    try { await pressEscape(); } catch {}
    await delay(120);
    let state;
    try {
      state=await evaluate(`(() => {
        const marker=window.__botrieltorPhotoViewer;
        if(!marker)return {known:false,open:false};
        const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>3&&r.height>3&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth&&s.visibility!=='hidden'&&s.display!=='none'};
        const extras=[...document.images].filter(img=>!marker.before.has(img)&&visible(img));
        const target=marker.target;
        const rect=target?.getBoundingClientRect();
        const grew=Boolean(rect&&visible(target)&&((rect.width>marker.width*1.5)||(rect.height>marker.height*1.5)));
        const dialog=[...document.querySelectorAll('[role="dialog"],[class*="photo-view" i],[class*="image-view" i],[class*="lightbox" i]')].some(visible);
        const open=extras.length>0||grew||dialog;
        if(!open){delete window.__botrieltorPhotoViewer;return {known:true,open:false};}
        let root=extras[0]||target;
        for(let level=0;level<8&&root?.parentElement;level++){
          const parent=root.parentElement,box=parent.getBoundingClientRect(),style=getComputedStyle(parent);
          root=parent;
          if(style.position==='fixed'||box.width*box.height>innerWidth*innerHeight*.55)break;
        }
        const close=[...(root?.querySelectorAll('button,[role="button"],[aria-label],[title],[class*="close" i]')||[])].filter(e=>{
          if(!visible(e))return false;
          const label=[e.innerText,e.getAttribute('aria-label'),e.getAttribute('title'),e.getAttribute('data-tooltip'),e.className].join(' ').toLowerCase();
          return /close|đóng|thoát|quay lại|back/i.test(label);
        });
        close.sort((a,b)=>{
          const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();
          const xs=(x.left>innerWidth*.65&&x.top<innerHeight*.35)?0:1;
          const ys=(y.left>innerWidth*.65&&y.top<innerHeight*.35)?0:1;
          return xs-ys||x.width*x.height-y.width*y.height;
        });
        if(close.length)close[0].click();
        return {known:true,open:true,closeClicked:close.length>0};
      })()`);
    } catch { continue; }
    if(!state?.known||!state.open)return true;
    if(state.closeClicked)await delay(180);
  }
  return false;
}
async function status() {
  const page = await target();
  if (!page) return {ready:false, login_required:false, unavailable:true, url:'', title:''};
  if (!page.url.startsWith('https://chat.zalo.me')) return {ready:false, login_required:true, url:page.url};
  await connect();
  const state = await evaluate(`(() => {
    const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>3&&r.height>3&&s.visibility!=='hidden'&&s.display!=='none'};
    const rows=[...document.querySelectorAll('.conv-item')].filter(visible);
    const list=document.querySelector('#conversationList');
    const listVisible=Boolean(list&&visible(list));
    const text=(document.body?.innerText||'').slice(0,3000);
    const loginPrompt=/Quét mã QR|Scan (?:the )?QR|Đăng nhập Zalo|Log in to Zalo|Sign in to Zalo/i.test(text);
    return {title:document.title,text,inputs:document.querySelectorAll('input').length,
      listVisible,visibleConversationRows:rows.length,loginPrompt};
  })()`);
  const ready = !state.loginPrompt && state.listVisible && state.visibleConversationRows > 0;
  return {ready, login_required:!ready, login_prompt:state.loginPrompt, url:page.url, title:state.title};
}
function literal(value) { return JSON.stringify(String(value)); }
async function waitForZaloReady(timeoutMs=20000) {
  const deadline=Date.now()+timeoutMs;
  let current={ready:false};
  while(Date.now()<deadline) {
    try {
      current=await status();
      if(current.ready||current.login_prompt)return current;
    } catch {}
    await delay(400);
  }
  return current;
}
async function bindSource(source) {
  const current = await waitForZaloReady();
  if (!current.ready) throw new Error(current.login_prompt
    ? 'Войдите в Zalo в окне BotRieltor Agent.'
    : 'Список чатов Zalo не загрузился за 20 секунд.');
  await closePhotoViewer(true);
  const name = normalizeChatName(source.name);
  const canonicalName = canonicalChatName(name);
  
  // 1. Check if already open
  const alreadyOpen = await readActiveChatName(name);
  if (canonicalChatName(alreadyOpen) === canonicalName) {
    activeSource = name;
    return {external_ref: `zalo:${createHash('sha256').update(source.kind + ':' + name).digest('hex')}`, observed_name: alreadyOpen, observed_kind: source.kind};
  }
  const initialActive = await readActiveChatName();
  await send('Input.dispatchKeyEvent', {type:'keyDown', key:'Escape', code:'Escape'});
  await send('Input.dispatchKeyEvent', {type:'keyUp', key:'Escape', code:'Escape'});
  const resetSearch = await evaluate(`(() => {
    const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>3&&r.height>3&&s.visibility!=='hidden'&&s.display!=='none'};
    const norm=s=>String(s||'').normalize('NFC').replace(/\\s+/g,' ').trim().toLocaleLowerCase();
    const close=[...document.querySelectorAll('button,[role="button"],[class*="close" i],[aria-label*="close" i],[title*="close" i]')].filter(e=>visible(e)&&
      (norm(e.innerText)==='close'||norm(e.getAttribute('aria-label'))==='close'||norm(e.getAttribute('title'))==='close'||/close/i.test(String(e.className||''))));
    close.sort((a,b)=>{const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();return x.width*x.height-y.width*y.height});
    if(close.length)close[0].click();
    const inputs=[...document.querySelectorAll('input')].filter(visible);
    const input=inputs.find(e=>/tìm|search|поиск/i.test(e.placeholder||''))||inputs[0];
    if(!input)return {closed:close.length>0,closeCount:close.length,cleared:false};
    const previous=String(input.value||'');
    const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    setter.call(input,''); input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true}));
    return {closed:close.length>0,closeCount:close.length,cleared:true,previous,value:String(input.value||''),placeholder:input.placeholder||''};
  })()`);
  if (resetSearch?.closed||resetSearch?.cleared) await delay(1500);

  // 2. Try direct click in left conversation list
  const directClick = await evaluate(`(() => {
    const norm = s => String(s).normalize('NFC').replace(/\\s+/g, ' ').trim();
    const target = norm(${literal(name)});
    const visible = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 3 && r.height > 3 && r.top >= 0 && r.bottom <= innerHeight && s.visibility !== 'hidden' && s.display !== 'none'; };
    const key=s=>norm(s).toLocaleLowerCase().replace(/[^\\p{L}\\p{N}]+/gu,'');
    const wrappers=[...document.querySelectorAll('.msg-item')].filter(visible);
    let rows=wrappers.map(wrapper=>wrapper.querySelector('.conv-item')||wrapper);
    rows.push(...[...document.querySelectorAll('.conv-item')].filter(e=>visible(e)&&!e.closest('.msg-item')));
    rows=rows.filter(row=>{const title=row.querySelector('.conv-item-title__name');return title&&key(title.textContent)===${literal(canonicalName)};});
    const isPinned=row=>row.classList.contains('pinned')||Boolean(row.closest('.msg-item.pinned'));
    if (rows.length === 1 && isPinned(rows[0])) {
      const row=rows[0];
      const r = row.getBoundingClientRect();
      return {ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2, method:'pinned-conversation-row',row:{text:norm(row.textContent),className:String(row.className||''),attrs:{id:row.id||'',dataId:row.getAttribute('data-id')||'',role:row.getAttribute('role')||'',ariaSelected:row.getAttribute('aria-selected')||''}}};
    }
    return {ok:false,matchCount:rows.length,pinnedCount:rows.filter(isPinned).length,visibleNames:[...document.querySelectorAll('.conv-item-title__name,.truncate')].filter(visible).map(e=>norm(e.textContent)).slice(0,40)};
  })()`);
  if (directClick?.ok) {
    await send('Input.dispatchMouseEvent', {type:'mouseMoved', x:directClick.x, y:directClick.y});
    await send('Input.dispatchMouseEvent', {type:'mousePressed', x:directClick.x, y:directClick.y, button:'left', clickCount:1});
    await send('Input.dispatchMouseEvent', {type:'mouseReleased', x:directClick.x, y:directClick.y, button:'left', clickCount:1});
    try {
      const observed = await waitForActiveChat(name);
      activeSource = name;
      return {external_ref: `zalo:${createHash('sha256').update(source.kind + ':' + name).digest('hex')}`, observed_name: observed, observed_kind: source.kind};
    } catch {
      const after = await readActiveChatName();
      if (after && canonicalChatName(after) !== canonicalChatName(initialActive) && canonicalChatName(after) !== canonicalChatName(name)) {
        throw new Error(`Выбор «${name}» открыл «${after}», привязка отменена. Снимок не сохранён.`);
      }
    }
  }

  // 3. Search fallback
  const search = await evaluate(`(() => {
    const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>3&&r.height>3&&s.visibility!=='hidden'&&s.display!=='none'};
    const inputs=[...document.querySelectorAll('input')].filter(visible);
    const input=inputs.find(e=>/tìm|search|поиск/i.test(e.placeholder||''))||inputs[0];
    if(!input)return {ok:false};
    const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    setter.call(input,${literal(name)}); input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true})); input.focus();
    return {ok:true};
  })()`);
  if (!search?.ok) throw new Error('Не найдено поле поиска Zalo.');
  await delay(2500);
  const match = await evaluate(`(() => {
    const norm=s=>String(s).normalize('NFC').replace(/\\s+/g,' ').trim();
    const target=norm(${literal(name)});
    const key=s=>norm(s).toLocaleLowerCase().replace(/[^\\p{L}\\p{N}]+/gu,'');
    const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>3&&r.height>3&&r.top>=0&&r.bottom<=innerHeight&&s.visibility!=='hidden'&&s.display!=='none'};
    const wrappers=[...document.querySelectorAll('.msg-item')].filter(visible);
    let rows=wrappers.map(wrapper=>wrapper.querySelector('.conv-item')||wrapper);
    rows.push(...[...document.querySelectorAll('.conv-item')].filter(e=>visible(e)&&!e.closest('.msg-item')));
    rows=rows.filter(row=>{const title=row.querySelector('.conv-item-title__name');return title&&key(title.textContent)===${literal(canonicalName)};});
    const isPinned=row=>row.classList.contains('pinned')||Boolean(row.closest('.msg-item.pinned'));
    if(rows.length!==1||!isPinned(rows[0]))return {count:rows.length,pinned:false,visibleNames:[...document.querySelectorAll('.conv-item-title__name,.truncate')].filter(visible).map(e=>norm(e.textContent)).slice(0,40)};
    const row=rows[0];
    const attrs={}; for(let e=row,n=0;e&&n<6;e=e.parentElement,n++)for(const a of e.attributes||[])if(/id|conv|chat|thread|data-/i.test(a.name))attrs[a.name]=a.value;
    const r=row.getBoundingClientRect(); return {count:1,attrs,x:r.left+r.width/2,y:r.top+r.height/2};
  })()`);
  if (match.count !== 1 || !match.pinned) throw new Error(`Чат «${name}» не найден как единственная закреплённая группа (совпадений: ${match.count}). Снимок не сохранён.`);
  await send('Input.dispatchMouseEvent', {type:'mouseMoved', x:match.x, y:match.y});
  await send('Input.dispatchMouseEvent', {type:'mousePressed', x:match.x, y:match.y, button:'left', clickCount:1});
  await send('Input.dispatchMouseEvent', {type:'mouseReleased', x:match.x, y:match.y, button:'left', clickCount:1});
  let observed;
  try {
    observed = await waitForActiveChat(name);
  } catch {
    const after = await readActiveChatName();
    throw new Error(`Не открыл группу «${name}»: в заголовке «${after}». Привязка отменена, снимок не сохранён.`);
  }
  activeSource = name;
  return {external_ref:`zalo:${createHash('sha256').update(source.kind + ':' + name).digest('hex')}`, observed_name:observed, observed_kind:source.kind};
}
async function snapshotChat() {
  const current = await status();
  if (!current.ready) throw new Error('Требуется вход в Zalo.');
  return evaluate(`(() => {
    const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>3&&r.height>3&&s.visibility!=='hidden'&&s.display!=='none'};
    const pane=${conversationPaneExpression()}||document.body;
    let text=(pane.innerText||'').slice(-100000);
    const paneRect=pane.getBoundingClientRect();
    const inViewport=e=>{if(!visible(e))return false;const r=e.getBoundingClientRect();return r.bottom>=paneRect.top-3500&&r.top<=paneRect.bottom+3500&&r.right>=paneRect.left-50&&r.left<=paneRect.right+50};

    // Grab profile / business description if right sidebar is available
    const aside = document.querySelector('aside, .chat-info');
    const asideText = aside ? (aside.innerText || '').trim() : '';
    if (asideText) {
      text = '--- THÔNG TIN HỒ SƠ & KINH DOANH ---\\n' + asideText + '\\n--- TIN NHẮN ---\\n' + text;
    }

    const rawMedia=[...document.querySelectorAll('img,video')].filter(inViewport);
    const imageSources=e=>{
      // Zalo can replace a compressed preview with its original after the
      // message has rendered. Keep every exposed source so download can retry
      // alternatives instead of committing the first thumbnail URL.
      const candidates=[];
      const add=(url,score=0)=>{url=String(url||'').trim();if(url&&!url.startsWith('data:'))candidates.push({url,score})};
      if(e.tagName.toLowerCase()==='img'){
        for(const attr of ['data-original','data-src','data-full-src','data-url'])add(e.getAttribute(attr),500);
        const srcset=e.getAttribute('srcset')||e.getAttribute('data-srcset')||'';
        for(const part of srcset.split(',')){
          const pieces=part.trim().split(/\s+/);const descriptor=pieces[1]||'';
          const score=descriptor.endsWith('w')?(400+(parseInt(descriptor,10)||0)):
            descriptor.endsWith('x')?(400+Math.round((parseFloat(descriptor)||0)*1000)):400;
          add(pieces[0],score);
        }
        const anchor=e.closest('a[href]');if(anchor)add(anchor.href,1000000);
      }
      add(e.currentSrc||e.src||'',100);
      candidates.sort((a,b)=>b.score-a.score);
      return [...new Set(candidates.map(item=>item.url))];
    };
    const bestImageUrl=e=>imageSources(e)[0]||'';
    const media=[];
    for (let i = 0; i < rawMedia.length && media.length < 200; i++) {
      const e = rawMedia[i];
      const src = bestImageUrl(e);
      if (!src || src.startsWith('data:')) continue;
      const sources = imageSources(e);
      const isAvatar = src.includes('ava-talk') || src.includes('avatar') || src.includes('iconlike');
      const w = e.naturalWidth || e.videoWidth || e.clientWidth || e.width || 0;
      const h = e.naturalHeight || e.videoHeight || e.clientHeight || e.height || 0;
      if (!isAvatar && (e.tagName.toLowerCase() === 'video' || w >= 60 || h >= 60 || src.startsWith('blob:'))) {
        media.push({
          index: media.length,
          kind: e.tagName.toLowerCase(),
          src,
          sources,
          alt: e.alt || '',
          w,
          h
        });
      }
    }

    const mediaIndex=new Map(media.map(x=>[x.index,x]));
    const selectors='[data-msgid],[data-message-id],[data-id*="msg"],[class*="message"],[class*="msg-item"],[class*="chat-item"]';
    let nodes=[...pane.querySelectorAll(selectors)].filter(inViewport).filter(e => !e.classList.contains('message-view__scroll__inner') && e.id !== 'messageViewScroll');
    nodes=nodes.filter(e=>!nodes.some(other=>other!==e&&other.contains(e)&&String(other.innerText||'').trim()===String(e.innerText||'').trim()));
    if(!nodes.length)nodes=[...pane.querySelectorAll('div')].filter(e=>{if(!inViewport(e))return false;const r=e.getBoundingClientRect();const t=String(e.innerText||'').trim();const own=[...e.children].filter(visible).length;return r.left>innerWidth*.22&&r.height>=18&&r.height<800&&t.length>0&&t.length<5000&&own<12});
    const blocks=nodes.slice(-500).map((e,index)=>{const r=e.getBoundingClientRect();const attrs={};for(const a of e.attributes||[])if(/^data-|^id$|^role$|^aria-label$/i.test(a.name))attrs[a.name]=String(a.value).slice(0,500);const bubble=e.querySelector('[id*="bb_msg_id_"]')||e;if(bubble.id)attrs['bubble_id']=bubble.id;const m=(bubble.id||'').match(/(\\d{12,14})/);if(m)attrs['timestamp_ms']=m[1];const timeNode=e.querySelector('time[datetime], [data-time], [data-timestamp], [title]');const visibleTime=timeNode?.getAttribute('datetime')||timeNode?.getAttribute('data-time')||timeNode?.getAttribute('data-timestamp')||timeNode?.getAttribute('title')||'';if(visibleTime)attrs['sent_at']=String(visibleTime).slice(0,100);const senderNode=e.querySelector('[data-sender], [class*="sender-name"], [class*="user-name"], [class*="nickname"]');const sender=senderNode?.getAttribute('data-sender')||senderNode?.innerText||senderNode?.getAttribute('title')||'';const refs=[...e.querySelectorAll('img,video')].map(x=>{const src=bestImageUrl(x);return media.find(m=>m.src===src)?.index}).filter(Number.isInteger);return {index,tag:e.tagName.toLowerCase(),className:String(e.className||'').slice(0,500),attrs,sender:String(sender).trim().slice(0,200),sent_at:String(visibleTime).slice(0,100),text:String(e.innerText||'').trim().slice(0,10000),media_indexes:[...new Set(refs)],rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}}});
    return {text,blocks,media,scrollTop:pane.scrollTop,scrollHeight:pane.scrollHeight,clientHeight:pane.clientHeight,
      paneId:pane.id||'',paneClass:String(pane.className||'').slice(0,300),
      paneRect:{x:Math.round(paneRect.x),y:Math.round(paneRect.y),width:Math.round(paneRect.width),height:Math.round(paneRect.height)},
      url:location.href,title:document.title};
  })()`);
}
async function collectChunk(source, mode, cursor={}) {
  const name = normalizeChatName(source.name);
  if(!await closePhotoViewer(true))throw new Error('Не удалось закрыть просмотрщик фото Zalo; чтение чата остановлено безопасно.');
  if (activeSource !== name || !(await activeChatMatches(name))) await bindSource(source);
  const observed = await waitForActiveChat(name);
  if (mode === 'collect_incremental') {
    await evaluate(`(() => { const pane=${conversationPaneExpression()}; if(pane)pane.scrollTop=pane.scrollHeight; })()`);
    await delay(600);
  }
  if (mode === 'collect_initial_scan') {
    await evaluate(`(() => { const pane=${conversationPaneExpression()}; if(!pane)return; if(!${Boolean(cursor.initial_scan_started)})pane.scrollTop=pane.scrollHeight; else if(Number.isFinite(${Number(cursor.initial_scan_position) || 0}))pane.scrollTop=${Number(cursor.initial_scan_position) || 0}; })()`);
    if (!cursor.initial_scan_started) await delay(500);
  }
  const snapshot = await snapshotChat();
  snapshot.activeChatName = await waitForActiveChat(name);
  let complete = true;
  let scanCursor = {};
  if (mode === 'collect_initial_scan') {
    const progress = await evaluate(`(() => {
      const pane=${conversationPaneExpression()};
      if(!pane)return {before:null,top:null,height:0,client:0,paneId:'',paneClass:'',samePosition:false,sameIdentity:false};
      const before=pane.scrollTop;
      const samePosition=Math.abs(before-(${Number(snapshot.scrollTop) || 0}))<=8;
      const paneId=pane.id||'';
      const paneClass=String(pane.className||'').slice(0,300);
      const expectedId=${literal(snapshot.paneId||'')};
      const expectedClass=${literal(snapshot.paneClass||'')};
      const rect=pane.getBoundingClientRect();
      const expectedRect=${JSON.stringify(snapshot.paneRect||null)};
      const sameGeometry=Boolean(expectedRect)&&Math.abs(rect.x-expectedRect.x)<=20&&Math.abs(rect.y-expectedRect.y)<=20&&Math.abs(rect.width-expectedRect.width)<=20&&Math.abs(rect.height-expectedRect.height)<=20;
      const sameIdentity=(!expectedId||paneId===expectedId)&&(!expectedClass||paneClass===expectedClass);
      pane.scrollTop=Math.max(0,before-Math.max(400,pane.clientHeight*.72));
      return {before,top:pane.scrollTop,height:pane.scrollHeight,client:pane.clientHeight,paneId,paneClass,samePosition,sameIdentity,sameGeometry};
    })()`);
    const toEpochMs = block => {
      const attrs=block.attrs||{};
      const raw=String(attrs.timestamp_ms||'').trim();
      if (/^\\d{12,14}$/.test(raw)) {
        const numeric=Number(raw);
        return numeric < 100000000000 ? numeric*1000 : numeric;
      }
      const visible=String(block.sent_at||attrs.sent_at||'').trim();
      const parsed=visible ? Date.parse(visible) : NaN;
      return Number.isFinite(parsed) ? parsed : null;
    };
    const blockTimes=(snapshot.blocks||[]).map(toEpochMs);
    const times=blockTimes.filter(Number.isFinite);
    const cutoff = Date.parse(cursor.initial_scan_cutoff||'');
    const reachedByTime = Number.isFinite(cutoff) && times.some(value=>value <= cutoff);
    const reachedTop = progress.top !== null && progress.top <= 2 &&
      (progress.before <= 2 || progress.top < progress.before) && progress.samePosition && progress.sameIdentity && progress.sameGeometry;
    const trustedHistoryTop = reachedTop && times.length > 0;
    complete = trustedHistoryTop || reachedByTime;
    const undatedCount = blockTimes.filter((value,index)=>value===null &&
      (String((snapshot.blocks||[])[index]?.text||'').trim() || ((snapshot.blocks||[])[index]?.media_indexes||[]).length)).length;
    scanCursor = {initial_scan_started:true, initial_scan_position:progress.samePosition && progress.sameIdentity && progress.sameGeometry ? progress.top : snapshot.scrollTop,
                  initial_scan_scroll_height:progress.height, initial_scan_oldest_timestamp_ms:times.length?Math.min(...times):null,
                  cutoff_reached:Boolean(reachedByTime), history_top_reached:Boolean(trustedHistoryTop),
                  undated_count:Number(cursor.undated_count||0)+undatedCount};
    if (!complete) await delay(350);
  }
  if (mode === 'collect_archive') {
    const movement = await evaluate(`(() => { const c=[...document.querySelectorAll('div')].filter(e=>e.scrollHeight>e.clientHeight+100&&e.getBoundingClientRect().left>innerWidth*.22).sort((a,b)=>(b.clientWidth*b.clientHeight)-(a.clientWidth*a.clientHeight)); const p=c[0]; if(!p)return {before:0,after:0}; const before=p.scrollTop; p.scrollTop=Math.max(0,before-Math.max(500,p.clientHeight*.8)); return {before,after:p.scrollTop}; })()`);
    complete = movement.before <= 5;
    if (!complete) await delay(350);
  }
  return {snapshot, complete, cursor:scanCursor};
}
async function downloadMedia(url) {
  return evaluate(`(async()=>{const r=await fetch(${literal(url)});if(!r.ok)throw new Error('media '+r.status);const b=await r.arrayBuffer();const bytes=new Uint8Array(b);let binary='';const step=32768;for(let i=0;i<bytes.length;i+=step)binary+=String.fromCharCode(...bytes.subarray(i,i+step));return {mime:r.headers.get('content-type')||'',data:btoa(binary),size:bytes.length}})()`, true);
}
async function hydratePhotoFromViewer(entry) {
  let value;
  const encodedEntry = Buffer.from(JSON.stringify(entry)).toString('base64');
  try {
    value = await evaluate(`(async()=>{
    const entry=JSON.parse(atob('${encodedEntry}'));
    const good=(width,height)=>Math.min(width,height)>=500&&width*height>=480000&&Math.max(width,height)>=800;
    const sources=img=>[...new Set([img.currentSrc,img.src,img.getAttribute('data-original'),img.getAttribute('data-src'),img.getAttribute('data-full-src'),img.closest('a[href]')?.href]
      .filter(Boolean).concat((img.getAttribute('srcset')||img.getAttribute('data-srcset')||'').split(',').map(part=>part.trim().split(/\\s+/)[0]).filter(Boolean)))];
    const visible=img=>{const r=img.getBoundingClientRect(),s=getComputedStyle(img);return r.width>10&&r.height>10&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth&&s.visibility!=='hidden'&&s.display!=='none'&&Number(s.opacity||1)>.15};
    const target=[...document.images].find(img=>sources(img).some(url=>entry.candidates.includes(url)));
    if(!target)return {attempted:false,ok:false};
    const before=new WeakSet([...document.images]);
    const base=target.getBoundingClientRect();
    window.__botrieltorPhotoViewer={before,target,width:base.width,height:base.height};
    const anchor=target.closest('a[href]');
    if(anchor)document.addEventListener('click',event=>{if(anchor.contains(event.target))event.preventDefault()},{capture:true,once:true});
    // Open the exact thumbnail once. Poll the rendered viewer image without
    // refetching candidate URLs while Zalo is still resolving the original.
    target.click();
    const end=Date.now()+30000;
    while(Date.now()<end){
      const modalRoots=[...document.querySelectorAll('[role="dialog"],[class*="photo-view" i],[class*="image-view" i],[class*="lightbox" i],[class*="viewer" i]')].filter(visible);
      const modalImages=modalRoots.flatMap(root=>[...root.querySelectorAll('img')]).filter(img=>visible(img)&&(img===target||!before.has(img)));
      const candidates=modalImages.length?modalImages:[...document.images].filter(img=>visible(img)&&(img===target||!before.has(img)));
      candidates.sort((a,b)=>{
        const active=img=>{let node=img,score=0;for(let i=0;i<6&&node;i++,node=node.parentElement){const label=String(node.className||'')+' '+String(node.getAttribute?.('aria-current')||'');if(/active|selected|current/i.test(label))score=1;}return score};
        const activeDiff=active(b)-active(a);if(activeDiff)return activeDiff;
        const ar=a.getBoundingClientRect(),br=b.getBoundingClientRect();
        return (br.width*br.height)-(ar.width*ar.height);
      });
      const image=candidates.find(img=>good(img.naturalWidth||0,img.naturalHeight||0));
      if(image){
        const width=image.naturalWidth,height=image.naturalHeight;
        const candidate=sources(image).find(url=>url&&!url.startsWith('data:'))||'';
        if(candidate){
          try{
            const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),12000);
            let response;
            try{response=await fetch(candidate,{signal:controller.signal});}finally{clearTimeout(timer)}
            if(response.ok){
              const blob=await response.blob(),bitmap=await createImageBitmap(blob);
              const fileWidth=bitmap.width,fileHeight=bitmap.height;bitmap.close();
              if(good(fileWidth,fileHeight)){
                const bytes=new Uint8Array(await blob.arrayBuffer());
                let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
                return {attempted:true,ok:true,url:entry.url,downloaded_url:candidate,mime:blob.type||response.headers.get('content-type')||'',data:btoa(binary),size:bytes.length,width:fileWidth,height:fileHeight};
              }
            }
          }catch{}
        }
        // If Zalo rendered the original but its URL cannot be fetched directly,
        // export that same selected image once at its natural dimensions.
        try{
          const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
          const context=canvas.getContext('2d');if(!context)throw new Error('canvas unavailable');
          context.drawImage(image,0,0);
          const blob=await new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error('canvas export failed')),'image/png'));
          const bytes=new Uint8Array(await blob.arrayBuffer());let binary='';
          for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
          return {attempted:true,ok:true,url:entry.url,downloaded_url:candidate,mime:'image/png',data:btoa(binary),size:bytes.length,width:canvas.width,height:canvas.height,fallback:'canvas'};
        }catch{}
        return {attempted:true,ok:false,error:'viewer image could not be saved'};
      }
      await new Promise(resolve=>setTimeout(resolve,500));
    }
    return {attempted:true,ok:false,error:'full-resolution image did not load in viewer after 30 seconds'};
    })()`, true, 45000);
  } catch (error) {
    await closePhotoViewer();
    throw error;
  }
  if(value?.attempted&&!await closePhotoViewer())throw new Error('Не удалось закрыть просмотрщик Zalo после фотографии.');
  return value;
}
async function downloadMediaBatch(entries) {
  const safe = (Array.isArray(entries) ? entries : []).map(value=>typeof value==='string'
    ? {url:value,candidates:[value]}
    : {url:String(value?.url||''),candidates:Array.isArray(value?.candidates)?value.candidates.map(String):[],kind:String(value?.kind||'image')})
    .filter(value=>value.url).slice(0, 8);
  const encodedEntries = Buffer.from(JSON.stringify(safe)).toString('base64');
  const result = await evaluate(`(async()=>{
    const entries=JSON.parse(atob('${encodedEntries}'));
    const encode=bytes=>{let binary='';const step=32768;for(let i=0;i<bytes.length;i+=step)binary+=String.fromCharCode(...bytes.subarray(i,i+step));return btoa(binary)};
    const imageSources=img=>{
      const values=[img.currentSrc,img.src,img.getAttribute('data-original'),img.getAttribute('data-src'),img.getAttribute('data-full-src')];
      const srcset=img.getAttribute('srcset')||img.getAttribute('data-srcset')||'';
      for(const part of srcset.split(',')){const value=part.trim().split(/\\s+/)[0];if(value)values.push(value)}
      if(img.closest('a[href]')?.href)values.push(img.closest('a[href]').href);
      return [...new Set(values.filter(Boolean))];
    };
    const findThumb=candidates=>[...document.images].find(img=>imageSources(img).some(value=>candidates.includes(value)));
    const goodDimensions=(width,height)=>Math.min(width,height)>=500&&width*height>=480000&&Math.max(width,height)>=800;
    const fetchPhoto=async url=>{
      const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),2500);
      try{
        const response=await fetch(url,{signal:controller.signal});if(!response.ok)throw new Error('media '+response.status);
        const blob=await response.blob();const bitmap=await createImageBitmap(blob);
        const width=bitmap.width,height=bitmap.height;bitmap.close();
        if(!width||!height)throw new Error('invalid image dimensions');
        return {url,blob,mime:blob.type||response.headers.get('content-type')||'',width,height};
      }finally{clearTimeout(timer)}
    };
    const save=async(item,primaryUrl)=>{
      const bytes=new Uint8Array(await item.blob.arrayBuffer());
      return {url:primaryUrl,ok:true,mime:item.mime,data:encode(bytes),size:bytes.length,width:item.width,height:item.height,downloaded_url:item.url};
    };
    const items=new Array(entries.length);
    let next=0;
    const downloadOne=async()=>{
      while(next<entries.length){
        const index=next++;
        const entry=entries[index],url=entry.url;
        let thumb=findThumb(entry.candidates);let candidates=[...new Set([url,...entry.candidates])];
        const tried=new Set();let best=null,lastError='';
        // Try every source Zalo exposes. If it initially serves a thumbnail,
        // allow its lazy image selection to settle and inspect the DOM again.
        for(let pass=0;pass<5;pass++){
          if(pass>0)await new Promise(resolve=>setTimeout(resolve,500));
          if(thumb){
            const current=imageSources(thumb);
            candidates=[...new Set([...current,...candidates])];
          }
          let foundFull=false;
          for(const candidate of candidates){
            if(tried.has(candidate))continue;
            if(tried.size>=4)break;
            tried.add(candidate);
            try{
              const item=await fetchPhoto(candidate);
              if(!best||item.width*item.height>best.width*best.height)best=item;
              if(goodDimensions(item.width,item.height)){best=item;foundFull=true;break;}
            }catch(error){lastError=error?.message||String(error);}
          }
          if(foundFull)break;
          if(!thumb)thumb=findThumb(candidates);
        }
        if(best)items[index]=await save(best,url);
        else items[index]={url,ok:false,error:lastError||'image source unavailable'};
      }
    };
    await Promise.all(Array.from({length:Math.min(4,entries.length)},()=>downloadOne()));
    return {items};
  })()`, true);
  for (let index=0;index<safe.length;index++) {
    const item=result.items?.[index],entry=safe[index];
    if (!item?.ok || entry.kind==='video' || goodPhotoDimensions(item.width,item.height)) continue;
    try {
      const upgraded=await hydratePhotoFromViewer(entry);
      if(upgraded?.ok)result.items[index]=upgraded;
    } catch {}
  }
  return result;
}
function goodPhotoDimensions(width,height) {
  return Math.min(Number(width)||0,Number(height)||0)>=500 &&
    (Number(width)||0)*(Number(height)||0)>=480000 && Math.max(Number(width)||0,Number(height)||0)>=800;
}
async function handle(request) {
  if (request.action === 'open') {
    await ensureChrome();
    const page = await target({createIfMissing:true});
    if (page) await send('Page.bringToFront');
    return status();
  }
  if (request.action === 'status') return status();
  if (request.action === 'bind') return bindSource(request.source || {});
  if (request.action === 'snapshot') return snapshotChat();
  if (request.action === 'collect') return collectChunk(request.source || {}, request.mode || 'collect_incremental', request.cursor || {});
  if (request.action === 'download_media') return downloadMedia(request.url || '');
  if (request.action === 'download_media_batch') return downloadMediaBatch(request.items || request.urls || []);
  if (request.action === 'close') { if (socket) socket.close(); return {closed:true}; }
  throw new Error('Неизвестная команда Zalo driver.');
}

const input = createInterface({input:process.stdin, crlfDelay:Infinity});
input.on('line', async line => {
  let request;
  try { request=JSON.parse(line); const result=await handle(request); process.stdout.write(JSON.stringify({id:request.id,result})+'\n'); }
  catch (error) { process.stdout.write(JSON.stringify({id:request?.id,error:error.message||String(error)})+'\n'); }
  if (process.stdin.readableEnded) { if (socket) socket.close(); process.exit(0); }
});
