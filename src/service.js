/* R5 → authenticated service adapter. Shared writes are revision-checked server requests;
   local imports, views, rule drafts and preferences never upload automatically. */
const Net={me:window.LB_SESSION,rules:[],bindings:[],directories:[],trash:[],admin:null,adminTab:'users',busy:false,online:true,release:null,nextRelease:null,loading:new Map(),initialized:false};
const isAdmin=()=>!!Net.me.capabilities.admin;
const ROLE_NAMES={viewer:'访客',member:'成员',admin:'管理员'};
const relDir=p=>p==='data'?'':String(p||'').replace(/^data\//,'');
const sid=p=>'srv-'+btoa(unescape(encodeURIComponent(p))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
const activeRemote=()=>Net.rules.filter(x=>!x.deletedAt);
async function api(path,method='GET',data=null){
  const headers={};if(data!==null)headers['Content-Type']='application/json';
  if(method!=='GET'&&method!=='HEAD')headers['X-LogBench-CSRF']=Net.me.csrfToken;
  let response;
  try{response=await fetch('/api/v1'+path,{method,headers,body:data===null?undefined:JSON.stringify(data),cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(20000)});}catch(e){Net.online=false;throw Error('无法连接服务器。当前文件与未保存草稿仍保留，请重试。');}
  const value=await response.json();Net.online=true;
  if(!response.ok){const err=Error(value.error?.message||('请求失败 '+response.status));err.code=value.error?.code;err.status=response.status;
    if(err.code==='USER_DISABLED')blockIdentity('当前 IP 已被管理员停用，已暂停服务操作。');
    throw err;}
  return value;
}
function blockIdentity(message){flushState();if($('#identity-block'))return;const overlay=document.createElement('div');overlay.id='identity-block';overlay.className='service-overlay';const panel=document.createElement('section');panel.className='service-boot';const title=document.createElement('h1');title.textContent='身份发生变化';const text=document.createElement('p');text.textContent=message;const button=document.createElement('button');button.className='btn primary';button.textContent='重新确认身份';button.onclick=()=>location.reload();panel.append(title,text,button);overlay.append(panel);document.body.append(overlay);}
function remoteProfile(item){const p=clone(item.profile);p.id=item.id;p.name=item.name;p.desc=item.description;p.version=String(item.revision);p.status='共享';p.deleted=!!item.deletedAt;p._serverRevision=item.revision;p._private=false;return p;}
async function getShared(){const [r,b]=await Promise.all([api('/rules'),api('/bindings')]);let deleted=[];if(isAdmin())deleted=(await api('/rules?deleted=1')).items;Net.rules=[...r.items,...deleted];Net.bindings=b.items;}
function reconcileProfiles(){
  const old=S.profiles||[],drafts=S.dirtyProfiles||new Set();
  const shared=Net.rules.filter(x=>Array.isArray(x.profile?.extractors)).map(remoteProfile);
  if(!shared.some(x=>!x.deleted))throw Error('服务端没有可用的 R5 规则。请通过管理员初始化规则库。');
  const privateProfiles=old.filter(x=>x._private||(!x._serverRevision&&!Net.rules.some(r=>r.id===x.id)&&bootSnapshot));
  privateProfiles.forEach(x=>{x._private=true;x.status='仅我';});
  const previousApplied=S.appliedProfiles||[];
  S.appliedProfiles=[...shared.map(clone),...privateProfiles.map(x=>clone(previousApplied.find(a=>a.id===x.id)||x))];
  S.profiles=[...shared.map(x=>{const draft=old.find(p=>p.id===x.id);return draft&&drafts.has(x.id)?draft:clone(x);}),...privateProfiles];
  if(!S.profiles.some(x=>x.id===S.profile))S.profile=S.profiles.find(x=>!x.deleted).id;
  S.dirty=!!drafts.has(S.profile);S.nodeRules=profile().nodes||[];
  save('profiles',S.appliedProfiles);
}
const resolvePersonal=Engine.resolveProfile;
Engine.resolveProfile=function(f,profiles,folders={},bindings={}){
  const valid=id=>profiles.find(p=>p.id===id&&!p.deleted);
  if(valid(bindings[f.id]))return{profile:valid(bindings[f.id]),source:'个人文件覆盖',origin:f.name};
  const own=Object.entries(folders).filter(([k,id])=>k.startsWith(f.source+':')&&Engine.isWithin(f.path,k.slice(f.source.length+1))&&valid(id)).sort((a,b)=>b[0].length-a[0].length);
  if(own.length)return{profile:valid(own[0][1]),source:'个人目录覆盖',origin:own[0][0].slice(f.source.length+1)};
  if(f.source==='server'){
    const full=f.serverPath||relDir(f.path),dir=relDir(f.path);
    const matches=Net.bindings.filter(b=>valid(b.ruleId)&&(b.scope==='default'||b.scope==='file'&&f.serverPath&&b.path===full||b.scope==='directory'&&(!b.path||Engine.isWithin(dir,b.path)))).sort((a,b)=>(b.scope==='file'?1e6:b.scope==='directory'?b.path.length+1:0)-(a.scope==='file'?1e6:a.scope==='directory'?a.path.length+1:0));
    if(matches.length){const b=matches[0];return{profile:valid(b.ruleId),source:b.scope==='file'?'共享文件指定':b.scope==='directory'?'共享目录继承':'服务器默认',origin:b.path?'data/'+b.path:'默认规则'};}
  }
  const fallback=Net.bindings.find(b=>b.scope==='default'&&valid(b.ruleId));
  if(fallback)return{profile:valid(fallback.ruleId),source:'服务器默认',origin:'默认规则'};
  return resolvePersonal(f,profiles,{},{});
};
async function refreshCatalog(){
  const result=await api('/catalog'),before=new Map(files.filter(f=>f.source==='server').map(f=>[f.id,f]));
  Net.directories=result.items.filter(x=>x.type==='directory');
  const incoming=result.items.filter(x=>x.type==='file').map(x=>{
    const id=sid(x.path),old=before.get(id),parent=x.path.includes('/')?x.path.slice(0,x.path.lastIndexOf('/')):'';
    return{...(old||{}),id,name:x.name,path:'data'+(parent?'/'+parent:''),source:'server',serverPath:x.path,records:old?.records||[],text:old?.text,size:x.size,modified:x.modified,favorite:!!old?.favorite,deleted:false,fingerprint:x.fingerprint,stale:!!old?.loaded&&old.loadedFingerprint!==x.fingerprint};
  });
  const gone=S.opened.filter(id=>before.has(id)&&!incoming.some(f=>f.id===id));
  files=[...incoming,...files.filter(f=>f.source==='local')];
  if(gone.length){S.opened=S.opened.filter(id=>!gone.includes(id));if(gone.includes(S.file))S.file=S.opened[0]||null;if(S.inspector&&!files.some(f=>f.records.some(r=>r.id===S.inspector)))S.inspector=null;}
  syncBindingLabels();indexRecords();return incoming;
}
async function ensureFilesLoaded(ids,{force=false}={}){
  const wanted=[...new Set(ids)].map(id=>files.find(f=>f.id===id&&!f.deleted)).filter(Boolean);
  let budget=files.reduce((n,f)=>n+(f.text?new Blob([f.text]).size:0),0);
  const added=wanted.filter(f=>f.source==='server'&&!f.text).reduce((n,f)=>n+(f.size||0),0);
  if(budget+added>128*1024*1024)throw Error('选择范围超过 128 MiB 原文窗口，请先关闭部分文件或缩小范围；未开始读取。');
  for(const f of wanted){if(f.source!=='server'||f.loaded&&!force&&!f.stale)continue;
    if(f.size>64*1024*1024)throw Error(f.name+' 超过单文件 64 MiB，请拆分后放入日志库。');
    if(budget+(f.text?0:f.size)>128*1024*1024)throw Error('打开的原文合计超过 128 MiB，请先关闭部分服务器文件或缩小选择范围。');
    if(Net.loading.has(f.id)){await Net.loading.get(f.id);continue;}
    const job=(async()=>{
      const response=await fetch('/api/v1/files/content?path='+encodeURIComponent(f.serverPath),{cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(60000)});
      if(!response.ok){let v={};try{v=await response.json()}catch{}throw Error(v.error?.message||'读取日志失败');}
      const length=Number(response.headers.get('Content-Length'));if(length>64*1024*1024)throw Error('文件更新后超过单文件读取限制。');
      const oldSize=f.text?new Blob([f.text]).size:0;const raw=await response.arrayBuffer();if(raw.byteLength>64*1024*1024)throw Error('文件超过单文件读取限制。');
      if(budget-(f.text?new Blob([f.text]).size:0)+raw.byteLength>128*1024*1024)throw Error('文件在读取期间增大，超过原文窗口；保留原阅读结果。');
      let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(raw)}catch{throw Error(f.name+' 不是有效的 UTF-8 文本，请先转换编码。');}
      if(text.includes('\u0000'))throw Error(f.name+' 包含二进制内容，未作为日志打开。');
      const parsed=await parseInWorker(text,f.id),p=Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile;
      const output=await processJobs([{id:f.id,records:parsed,profile:p}]);
      f.text=text;f.records=output[0].records;f.loaded=true;f.loadedFingerprint=f.fingerprint;f.profile=p.name;f.stale=false;f.parseError=null;budget=budget-oldSize+raw.byteLength;
    })();Net.loading.set(f.id,job);try{await job}finally{Net.loading.delete(f.id);}
  }
  indexRecords();syncBindingLabels();
}
async function reapplyLoaded(){
  for(const f of files.filter(f=>!f.deleted&&f.records?.length)){
    const p=Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile;
    const old=f.records;
    try{f.records=(await processJobs([{id:f.id,records:old,profile:p}]))[0].records;f.profile=p.name;f.parseError=null;}catch(e){f.parseError=e.message;notify(f.name+' 规则未能应用，保留此前结果：'+e.message,true);}
  }
  syncBindingLabels();indexRecords();S.queryHits=null;
}
const restoreCacheOffline=restoreCache;
restoreCache=async function(){
  await restoreCacheOffline();
  // Server state is authoritative. Browser snapshots cannot restore deleted server files or publish drafts.
  reconcileProfiles();for(const f of files)if(f.source==='server')f.deleted=false;
  try{await ensureFilesLoaded([...S.opened,...(S.compare?.ids||[]),...selectedGlobalIds()]);await reapplyLoaded();}catch(e){notify(e.message,true);}
  if(S.savedAnchor)pendingAnchor=S.savedAnchor;S.inspector=recordIndex.has(S.inspector)?S.inspector:null;
  render(false);if(S.search||S.rules.some(x=>x.enabled&&x.query))runSearch(false);flushState();
};
async function startConnectedService(){
  storageState.ready=false;$('#app').inert=true;
  try{await getShared();reconcileProfiles();await refreshCatalog();await restoreCache();Net.initialized=true;Net.release=(await api('/health')).release;updateServiceStatus();
    if(S.view==='admin')await loadAdmin();
  }catch(e){$('#app').inert=false;storageState.ready=false;dialog('工作区尚未载入',`<p class="service-error">${E(e.message)}</p><p>没有清除本机缓存，也没有改写共享数据。请检查服务后重新连接。</p>`,B('重新连接','service-reload','refresh','primary'));}
}
const openFileOffline=openFile;
openFile=async function(id){
  try{if(id==='merged')await ensureFilesLoaded(S.opened);else if(id==='global')await ensureFilesLoaded(selectedGlobalIds());else await ensureFilesLoaded([id]);openFileOffline(id);}catch(e){notify(e.message,true);}
};
const restoreViewOffline=restoreView;
restoreView=async function(v){try{const ids=new Set([...(v.files||[]),...(v.compare?.ids||[])]);for(const f of files)if((v.global?.folders||[]).some(k=>k.startsWith(f.source+':')&&Engine.isWithin(f.path,k.slice(f.source.length+1))))ids.add(f.id);await ensureFilesLoaded([...ids]);restoreViewOffline(v);}catch(e){notify('视角未应用：'+e.message,true);}};
const openPickerOffline=openPicker;
openPicker=async function(kind='global'){try{await refreshCatalog();openPickerOffline(kind);$('.picker-time')?.insertAdjacentHTML('beforeend','<p class="service-remote-note">未打开的服务器文件将在应用时读取。时间按日志原值筛选。</p>');}catch(e){notify(e.message,true);}};
const applyPickerOffline=applyPicker;
applyPicker=async function(){
  if(!S.picker||Net.busy)return;const p=S.picker,button=$('[data-act="apply-picker"]');
  try{const a=timeFormValue('global-start'),b=timeFormValue('global-end');if(a>b)throw Error('结束时间不能早于起始时间。');if(!p.ids.size)throw Error('至少选择一个文件。');if(p.kind==='compare'&&(p.ids.size<2||p.ids.size>4))throw Error('时间对比请选择 2–4 个文件。');Net.busy=true;button.disabled=true;button.textContent='读取并解析…';await ensureFilesLoaded([...p.ids]);applyPickerOffline();}catch(e){if($('#picker-error'))$('#picker-error').textContent=e.message;else notify(e.message,true);}finally{Net.busy=false;if(button.isConnected){button.disabled=false;button.textContent='应用';}}
};
const renderPickTreeOffline=renderPickTree;
renderPickTree=function(){return renderPickTreeOffline().replaceAll('无有效时间 –  · 0 条','应用时读取时间与记录').replaceAll('服务器样例','服务器日志库');};
const renderR5=render;
render=function(keep=true){renderR5(keep);$('.app-status .status-right')?.lastElementChild?.replaceChildren(document.createTextNode('LogBench '+Net.me.version));updateServiceStatus();if(S.view==='rules'&&!isAdmin()){
  for(const act of (profile()?._private?['new-profile','profile-trash']:['new-profile','copy-profile','delete-profile','restore-profile','save-profile','profile-trash']))$$('[data-act="'+act+'"]').forEach(b=>{b.disabled=true;b.title='管理员可发布共享规则；你仍可编辑草稿和试跑';});
}};
function updateServiceStatus(){const foot=$('.app-status .row');if(!foot)return;let status=$('#service-status');if(!status){for(const n of [...foot.childNodes])if(n.nodeType===3)n.textContent='';status=document.createElement('button');status.id='service-status';status.className='link service-state';status.dataset.act='service-state';foot.prepend(status);}status.textContent=Net.nextRelease?'新版本可用 · 保存并刷新':Net.online?'服务已连接 · '+ROLE_NAMES[Net.me.user.role]:'服务暂不可用 · 本机阅读保留';status.classList.toggle('bad',!Net.online);status.classList.toggle('upgrade',!!Net.nextRelease);}
renderHeader=function(){return`<header class="lb-header">${B('','toggle-nav','menu','icon ghost','aria-label="展开或收起导航"')}<div class="brand"><span class="brandmark">${I('logo')}</span><span class="brandname">LogBench</span></div><div class="header-path"><span>分析工作区</span>${I('chevron','sm')}<span class="current">${{logs:'日志浏览',files:'文件库',rules:'规则管理',admin:'管理后台',compare:'时间对比'}[S.view]||'日志浏览'}</span></div><div class="header-right"><button class="prototype-tag" data-act="about">v${E(Net.me.version)}</button><span class="privacy-note">${I('lock','sm')} 本机解析</span>${B('打开日志','import','upload','primary','id="import-button"')}<button class="avatar" data-act="identity" aria-label="查看当前身份" title="${E(Net.me.user.name)} · ${E(Net.me.user.ip)}">${E(Net.me.user.name.slice(0,1))}</button></div></header>`;};
const renderSidebarOffline=renderSidebar;
renderSidebar=function(){return renderSidebarOffline().replaceAll('服务器样例','服务器日志库').replaceAll('服务器示例','服务器日志库').replaceAll('演示','管理').replace('研发用户',E(Net.me.user.name));};
const renderRulesOffline=renderRules;
renderRules=function(){let html=renderRulesOffline().replace('本机原型规则库',isAdmin()?'共享规则库 · 修订冲突保护':'共享规则只读 · 可试跑草稿');html=html.replace(B('回收站','profile-trash','trash','ghost'),B('刷新共享库','service-refresh-rules','refresh','ghost')+B('回收站','profile-trash','trash','ghost'));if(S.dirty)html=html.replace('<div class="rule-work-head">','<div class="rule-work-head">'+B('放弃草稿','service-discard-draft','restore','ghost sm'));return html;};
const renderRuleEditorOffline=renderRuleEditor;
renderRuleEditor=function(p){return renderRuleEditorOffline(p).replaceAll('当前原型','当前服务');};
const bindingDialogOffline=bindingDialog,directoryDialogOffline=directoryDialog,commitBindingOffline=commitBinding;
function insertScope(target){
  const shared=target.kind==='directory'?target.source==='server':target.ids.every(id=>files.find(f=>f.id===id)?.source==='server');
  if(shared&&isAdmin())$('.dialog-body').insertAdjacentHTML('afterbegin',`<div class="service-scope"><strong>作用范围</strong><label><input type="radio" name="bind-scope" value="personal" checked>仅我</label><label><input type="radio" name="bind-scope" value="shared">所有用户</label></div>`);
  const hints=$$('.dialog-body .hint');for(const h of hints)if(h.textContent.includes('本 HTML'))h.textContent='默认仅保存个人覆盖；管理员选择“所有用户”会修改服务器共享绑定。';
}
bindingDialog=function(ids){bindingDialogOffline(ids);insertScope(S.bindingTarget);};
directoryDialog=function(path,source){directoryDialogOffline(path,source);insertScope(S.bindingTarget);};
commitBinding=async function(button){
  if($('input[name="bind-scope"]:checked')?.value!=='shared')return commitBindingOffline(button);
  const target=S.bindingTarget,ruleId=$('#binding-rule').value,inherit=$('#binding-inherit').checked;
  if(!isAdmin())return notify('需要管理员权限。',true);
  if(S.appliedProfiles.find(p=>p.id===ruleId)?._private)return notify('个人规则不能绑定到共享目录；请先由管理员发布。',true);
  const items=target.kind==='directory'?[{scope:'directory',path:relDir(target.path)}]:target.ids.map(id=>({scope:'file',path:files.find(f=>f.id===id).serverPath}));
  button.disabled=true;let done=0;
  try{for(const item of items){const prior=Net.bindings.find(b=>b.scope===item.scope&&b.path===item.path);if(inherit&&!prior)continue;await api('/bindings',inherit?'DELETE':'PUT',{...item,ruleId,expectedRevision:prior?.revision||0});done++;}await getShared();reconcileProfiles();await reapplyLoaded();$('#dialog').close();render();notify('已更新 '+done+' 项共享绑定。个人覆盖仍优先。');}
  catch(e){$('#binding-error').textContent=(done?'已提交 '+done+' 项，其余未提交。':'')+e.message;await getShared();}finally{button.disabled=false;}
};
saveProfile=async function(){
  if(S.ruleBusy)return;const p=clone(profile());p.nodes=clone(S.nodeRules);
  try{Engine.validate(p);if(!p._private&&!isAdmin())throw Error('仅管理员可发布共享规则。');}catch(e){notify(e.message,true);return;}
  S.ruleBusy=true;render();
  try{
    // Validate computation before committing shared configuration. A failed Worker leaves the draft intact.
    const targets=files.filter(f=>!f.deleted&&f.records.length&&resolved(f,S.dirBindings,S.fileBindings,S.appliedProfiles).profile.id===p.id);
    await processJobs(targets.map(f=>({id:f.id,records:f.records,profile:p})));
    if(p._private){p.version=String(Number(p.version||0)+1);S.appliedProfiles=S.appliedProfiles.map(x=>x.id===p.id?clone(p):x);S.profiles=S.profiles.map(x=>x.id===p.id?clone(p):x);}
    else{const result=await api('/rules/'+encodeURIComponent(p.id),'PUT',{name:p.name,description:p.desc||'',profile:p,expectedRevision:p._serverRevision});const saved=remoteProfile(result.item);S.profiles=S.profiles.map(x=>x.id===p.id?saved:x);S.dirtyProfiles?.delete(p.id);await getShared();reconcileProfiles();}
    S.dirtyProfiles?.delete(p.id);S.dirty=false;S.testResult=null;save('profiles',S.appliedProfiles);await reapplyLoaded();notify(p._private?'个人规则已保存并应用。':'共享规则已发布；已重新提取当前打开的绑定文件。');
  }catch(e){notify(e.code==='REVISION_CONFLICT'?'服务器规则已被修改。当前草稿保留，请刷新共享库后对照修订，或复制草稿为新规则。':e.message,true);}
  finally{S.ruleBusy=false;render();}
};
function libraryDirectories(){
  const source=S.libSource==='local'?'local':'server',paths=new Set(source==='server'?['data',...Net.directories.map(d=>'data/'+d.path)]:['本机缓存',...files.filter(f=>f.source==='local').map(f=>f.path)]);
  for(const p of [...paths]){let parts=p.split('/');while(parts.length>1){parts.pop();paths.add(parts.join('/'));}}
  const root=source==='server'?'data':'本机缓存';
  function draw(path,depth){const children=[...paths].filter(x=>x!==path&&x.slice(0,x.lastIndexOf('/'))===path).sort((a,b)=>a.localeCompare(b,'zh-CN'));const folded=S.collapsedDirs.has(source+':'+path),count=files.filter(f=>!f.deleted&&f.source===source&&Engine.isWithin(f.path,path)).length;
    return`<div class="service-directory-row" style="padding-left:${depth*12}px">${children.length?B('','service-dir-fold',folded?'chevron':'down','icon ghost sm dir-expand',`data-path="${E(source+':'+path)}" aria-label="${folded?'展开':'折叠'} ${E(path)}"`):'<span style="width:22px;flex:none"></span>'}<button class="service-directory ${S.folder===path?'selected':''}" data-act="folder" data-value="${E(path)}">${I('folder','sm')}<span class="folder-label">${E(path.split('/').at(-1))}</span><small>${count}</small></button></div>${folded?'':children.map(x=>draw(x,depth+1)).join('')}`;}
  return draw(root,0);
}
renderFiles=function(){
  const source=S.libSource;let list=files.filter(f=>source==='trash'?f.source==='local'&&f.deleted:!f.deleted&&(source==='favorites'?f.favorite:f.source===source));
  if(source==='trash')list=[...list,...Net.trash.map(t=>({id:'trash-'+t.id,name:t.path.split('/').at(-1),path:'data/'+t.path,source:'server',deleted:true,trashId:t.id,isDirectory:t.type==='directory',modified:t.deletedAt,size:0,records:[]}))];
  if(S.folder&&!['trash','favorites'].includes(source))list=list.filter(f=>Engine.isWithin(f.path,S.folder));
  if(S.libQuery)list=list.filter(f=>(f.name+' '+f.path+' '+f.profile).toLowerCase().includes(S.libQuery.toLowerCase()));
  list.sort((a,b)=>Number(b.favorite)-Number(a.favorite)||a.path.localeCompare(b.path,'zh-CN')||a.name.localeCompare(b.name,'zh-CN'));
  S.libPage=Math.max(0,Math.min(S.libPage||0,Math.ceil(list.length/200)-1));const page=list.slice(S.libPage*200,S.libPage*200+200),path=S.folder||(source==='local'?'本机缓存':'data');
  const directory=['local','server'].includes(source),binding=directory?Engine.resolveProfile({id:'__dir__',source,path},S.appliedProfiles,S.dirBindings,{}):null;
  return`<div class="page-heading"><h1>文件库</h1><span class="badge">${fmt(files.filter(f=>!f.deleted).length)} 个文件</span><span class="spacer"></span><span class="hint">本机导入不上传 · 服务器目录共享</span>${B('刷新目录','service-refresh-files','refresh','ghost')}${source==='server'&&isAdmin()?B('新建文件夹','new-folder','plus'):''}</div><section class="page-content"><div class="library-top"><div class="source-tabs" aria-label="文件来源">${[['server','server','服务器日志库'],['local','laptop','本机临时'],['favorites','star','收藏'],['trash','trash','回收站']].map(([id,ic,n])=>`<button class="source-tab ${source===id?'active':''}" data-act="lib-source" data-value="${id}">${I(ic)}${n}</button>`).join('')}</div><div class="search-box" style="max-width:300px;margin-left:auto;flex:1;min-width:130px">${I('search')}<input id="file-search" type="search" value="${E(S.libQuery)}" placeholder="搜索文件名、路径或规则" aria-label="搜索文件库"></div></div><div class="library-shell"><aside class="folder-pane"><div class="folder-title">${I(source==='local'?'laptop':'server','sm')}目录</div><button class="folder-item ${!S.folder?'selected':''}" data-act="folder" data-value="">${I('folder')}全部文件<span class="folder-count">${fmt(list.length)}</span></button>${directory?libraryDirectories():`<p class="hint" style="padding:12px">${source==='trash'?'本机回收站仅属于当前身份；服务器回收站需要管理员。':'收藏只保存到当前身份的本机工作区。'}</p>`}</aside><div class="service-file-main">${directory?`<div class="directory-rulebar"><span>${I('sliders','sm')}目录默认规则</span><button class="binding" data-act="directory-rule" data-source="${source}" data-path="${E(path)}">${E(binding.profile.name)}${I('down','sm')}</button><span class="hint">${E(binding.source)}</span><span class="spacer"></span>${source==='server'&&S.folder&&S.folder!=='data'&&isAdmin()?B('删除目录','service-delete-directory','trash','ghost sm service-danger',`data-path="${E(relDir(S.folder))}"`):''}</div>`:''}<div class="library-breadcrumb">${I('folder','sm')}<span>${E(S.folder||({local:'本机缓存',server:'data/',favorites:'我的收藏',trash:'回收站'}[source]))}</span><span class="spacer"></span>${B('打开选中','open-selected','external','sm',S.libSelected.size?'':'disabled')}${source!=='trash'?B('批量规则','bind-selected','sliders','sm',S.libSelected.size?'':'disabled'):''}</div><div class="service-file-scroll" data-scroll="library"><table><thead><tr><th style="width:28px"><input type="checkbox" data-service-check-all aria-label="选择当前页"></th><th>文件</th><th>生效规则</th><th>大小 / 记录</th><th>修改时间</th><th>操作</th></tr></thead><tbody>${page.map(f=>{const b=!f.deleted?resolved(f,S.dirBindings,S.fileBindings,S.appliedProfiles):null;return`<tr><td><input type="checkbox" data-service-file="${E(f.id)}" ${S.libSelected.has(f.id)?'checked':''} ${f.deleted?'disabled':''} aria-label="选择 ${E(f.name)}"></td><td><button class="link file-name service-table-name" data-act="${f.deleted?'restore-file':'open-file'}" data-id="${E(f.id)}">${I(f.isDirectory?'folder':'file','sm')} ${E(f.name)}</button><div class="file-path">${E(f.path)}${f.stale?' · 内容已更新，重新打开读取新版本':''}</div></td><td>${b?`<button class="binding" data-act="bind-file" data-id="${E(f.id)}">${E(b.profile.name)}${I('down','sm')}</button><div class="file-path">${E(b.source)}</div>`:'—'}</td><td class="mono">${bytes(f.size||0)}<div class="file-path">${f.records?.length?fmt(f.records.length)+' 条':f.source==='server'&&!f.deleted?'打开时解析':'—'}</div></td><td class="mono">${E(String(f.modified||'').replace('T',' ').slice(0,19))}</td><td><div class="file-actions">${f.deleted?B('恢复','restore-file','restore','sm',`data-id="${E(f.id)}"`):B('', 'favorite-file','star','icon ghost sm '+(f.favorite?'starred':''),`data-id="${E(f.id)}" aria-label="${f.favorite?'取消收藏':'收藏'} ${E(f.name)}"`)+B('','download-file','download','icon ghost sm',`data-id="${E(f.id)}" aria-label="下载 ${E(f.name)}"`)+(f.source==='local'||isAdmin()?B('','delete-file','trash','icon ghost sm',`data-id="${E(f.id)}" aria-label="删除 ${E(f.name)}"`):'')}</div></td></tr>`;}).join('')}</tbody></table>${!page.length?`<div class="service-empty"><strong>${S.libQuery?'没有匹配的文件':source==='server'?'服务器目录尚无日志':'此处还没有文件'}</strong><p>${source==='server'?'将 UTF-8 日志放入部署目录 data/，点击“刷新目录”。':'使用“打开日志”导入本机文件，或调整当前条件。'}</p></div>`:''}</div><div class="service-page-foot"><span>${fmt(list.length)} 个结果 · 每页 200 个文件元数据</span><span class="spacer"></span>${B('上一页','service-lib-page','left','sm',`data-value="-1" ${S.libPage===0?'disabled':''}`)}<span>${S.libPage+1} / ${Math.max(1,Math.ceil(list.length/200))}</span>${B('下一页','service-lib-page','chevron','sm',`data-value="1" ${(S.libPage+1)*200>=list.length?'disabled':''}`)}</div></div></div></section>`;
};
async function refreshTrash(){Net.trash=isAdmin()?(await api('/files/trash')).items:[];}
deleteFiles=function(ids){const fs=ids.map(id=>files.find(f=>f.id===id)).filter(Boolean);if(!fs.length)return;if(fs.some(f=>f.source==='server')&&!isAdmin())return notify('服务器文件删除需要管理员权限。',true);S.pendingDelete=ids;dialog('移入回收站',`<p>将 ${fs.length} 个文件移入回收站，可恢复。</p><p class="hint">服务器文件将从共享 data/ 目录移走，影响所有用户。本机文件只在你的浏览器中标记删除，不改动电脑原始文件。</p><div class="mono">${fs.slice(0,8).map(f=>E(f.name)).join('<br>')}</div>`,B('取消','close-dialog')+B('移入回收站','confirm-delete-files','trash','danger'));};
async function deleteSelected(){let done=0;try{for(const id of S.pendingDelete||[]){const f=files.find(f=>f.id===id);if(!f)continue;if(f.source==='server')await api('/files?path='+encodeURIComponent(f.serverPath),'DELETE',{});else{f.deleted=true;await cachePut(f);}S.opened=S.opened.filter(x=>x!==id);done++;}$('#dialog').close();S.libSelected.clear();await refreshCatalog();await refreshTrash();if(!S.opened.includes(S.file))S.file=S.opened[0]||null;render();notify('已移入回收站 '+done+' 个文件。');}catch(e){notify('已完成 '+done+' 个；其余未处理：'+e.message,true);await refreshCatalog();render();}}
async function restoreDeleted(id){if(id.startsWith('trash-'))await api('/files/restore','POST',{id:id.slice(6)});else{const f=files.find(f=>f.id===id);if(!f)return;f.deleted=false;await cachePut(f);}await refreshCatalog();await refreshTrash();render();notify('已恢复，不覆盖同名文件。');}
async function downloadFile(id){const f=files.find(f=>f.id===id);if(!f)return;if(f.source==='local'){download(f.name,f.text||'','text/plain;charset=utf-8');return;}const a=document.createElement('a');a.href='/api/v1/files/content?path='+encodeURIComponent(f.serverPath);a.download=f.name;document.body.append(a);a.click();a.remove();}
async function loadAdmin(){if(!isAdmin()){Net.admin=null;render();return;}const [users,settings,audit,stats]=await Promise.all([api('/admin/users'),api('/admin/config'),api('/admin/audit?limit=100'),api('/admin/stats')]);Net.admin={users,settings,audit,stats};S.users=users.items||[];render();}
renderAdmin=function(){
  if(!isAdmin())return`<div class="page-heading"><h1>当前身份</h1><span class="badge">${ROLE_NAMES[Net.me.user.role]}</span></div><section class="settings-body"><div class="settings-block"><div class="settings-row"><strong>${E(Net.me.user.name)}</strong><code>${E(Net.me.user.ip)}</code><span>${Net.me.identityMode==='mcchecker'?'外部人员信息':'LogBench 独立身份'}</span></div></div><p class="hint">共享文件、规则和用户管理需要管理员权限。本机文件、个人视角与缓存可以正常使用。</p></section>`;
  const a=Net.admin;if(!a)return`<div class="page-heading"><h1>管理后台</h1></div><div class="service-empty">正在读取管理信息…${B('重试','service-admin-refresh','refresh')}</div>`;
  const c=a.settings.config;let body='';
  if(Net.adminTab==='users')body=`<div class="row" style="margin-bottom:12px"><h3>用户与 IP</h3><span class="spacer"></span>${a.users.readOnly?'<span class="badge">外部身份只读</span>':B('添加用户','new-user','plus','primary sm')}</div><div class="service-admin-wrap"><table class="settings-table service-admin-table"><thead><tr><th>姓名</th><th>IP</th><th>权限</th><th>状态</th><th>操作</th></tr></thead><tbody>${(a.users.items||[]).map((u,i)=>`<tr><td>${E(u.name)}</td><td class="mono">${E(u.ip)}</td><td>${ROLE_NAMES[u.role]||E(u.role)}</td><td>${u.enabled===false?'已停用':'可访问'}</td><td>${a.users.readOnly?'由来源系统管理':B('编辑','service-user-edit','edit','ghost sm',`data-index="${i}"`)}</td></tr>`).join('')}</tbody></table></div><p class="hint" style="margin-top:14px">未知 IP 默认为访客，可读共享日志。IP 不是强认证；同一出口 IP 视为同一身份。仅用于可信内网。</p>`;
  if(Net.adminTab==='connection')body=`<h3>身份来源</h3><div class="service-settings-form"><label class="field"><span class="label">用户来源</span><select class="select" id="service-identity-mode"><option value="local" ${c.identityMode==='local'?'selected':''}>LogBench 独立 IP 用户表</option><option value="mcchecker" ${c.identityMode==='mcchecker'?'selected':''}>只读复用 MCChecker 用户信息</option></select></label><label class="field"><span class="label">MCChecker 部署目录（服务器绝对路径）</span><input class="input mono" id="service-mc-dir" value="${E(c.mccheckerDirectory||'')}" placeholder="/opt/MCChecker"></label><label class="field"><span class="label">自定义 data 目录（可空）</span><input class="input mono" id="service-mc-data" value="${E(c.mccheckerDataDirectory||'')}" placeholder="留空读取部署目录下 data/"></label><p class="hint">仅读取 ip_mapping.json 与 admin_users.json，不导入或执行来源代码，不写回文件。切换前检查当前 IP 仍为管理员。</p><div id="service-preview-result" class="service-code" hidden></div><div class="row">${B('预检连接','service-identity-preview','shield')}${B('保存并启用','service-identity-save','check','primary')}</div></div>`;
  if(Net.adminTab==='audit')body=`<h3>最近 100 条操作记录</h3><div class="service-admin-wrap"><table class="settings-table service-admin-table"><thead><tr><th>时间</th><th>操作人 IP</th><th>动作</th><th>对象</th></tr></thead><tbody>${(a.audit.items||[]).map(x=>`<tr><td class="mono">${E(x.at)}</td><td>${E(x.actor)}</td><td>${E(x.action)}</td><td>${E(x.target)}</td></tr>`).join('')}</tbody></table></div>`;
  if(Net.adminTab==='system')body=`<h3>运行与存储</h3><pre class="service-code">${E(JSON.stringify({version:Net.me.version,release:Net.release,runtime:a.settings.runtime,statistics:a.stats},null,2))}</pre><div class="settings-block"><div class="settings-row"><div><strong>覆盖更新</strong><small>run.py 默认监视后端和前端源码，稳定后校验并更换服务进程。</small></div><span>数据与本机缓存保留</span></div><div class="settings-row"><div><strong>本机工作区</strong><small>当前身份的文件、草稿、视角和显示设置。</small></div>${B('存储与备份','r5-storage','laptop','sm')}</div></div><p class="hint">发布前请备份。更新可能短暂断开服务；旧页面不会强制刷新，顶部状态提示新版本。</p>`;
  return`<div class="page-heading"><h1>管理后台</h1><span class="badge blue">管理员</span><span class="spacer"></span>${B('刷新','service-admin-refresh','refresh','ghost')}</div><section class="settings-body"><nav class="service-admin-tabs">${[['users','用户'],['connection','身份来源'],['audit','审计'],['system','运行状态']].map(([id,n])=>B(n,'service-admin-tab',null,'ghost '+(Net.adminTab===id?'active':''),`data-value="${id}"`)).join('')}</nav>${body}</section>`;
};
function identityConfig(){return{identityMode:$('#service-identity-mode').value,mccheckerDirectory:$('#service-mc-dir').value.trim(),mccheckerDataDirectory:$('#service-mc-data').value.trim()};}
function editUser(index){const u=index<0?{ip:'',name:'',role:'member',enabled:true,revision:0}:Net.admin.users.items[index];S.userDraft=clone(u);dialog(index<0?'添加用户':'编辑用户',`<div class="filter-grid"><label class="field"><span class="label">IP 地址</span><input class="input mono" id="service-user-ip" value="${E(u.ip)}" ${u.revision?'readonly':''} placeholder="192.168.1.10"></label><label class="field"><span class="label">姓名</span><input class="input" id="service-user-name" value="${E(u.name)}"></label><label class="field"><span class="label">权限</span><select class="select" id="service-user-role">${Object.entries(ROLE_NAMES).map(([v,n])=>`<option value="${v}" ${v===u.role?'selected':''}>${n}</option>`).join('')}</select></label><label class="check"><input type="checkbox" id="service-user-enabled" ${u.enabled!==false?'checked':''}>允许访问</label></div><p class="hint">不允许通过页面停用或降级自己的管理员身份。人员配置按修订号检查并发修改。</p><div id="service-user-error" class="form-error" role="alert"></div>`,B('取消','close-dialog')+(u.revision&&u.ip!==Net.me.user.ip?B('删除登记','service-user-delete','trash','danger'):'')+B('保存','service-user-save','check','primary'));}
about=function(){dialog('LogBench '+Net.me.version,`<p>R5 阅读工作台 · 独立前后端服务</p><p class="hint">服务器提供共享日志库、规则版本与绑定、IP 权限、回收站和审计。本机日志、个人视角和草稿保存在当前身份的浏览器缓存，不自动上传。</p><p class="hint">解析、Traceback、筛选与时间对比在浏览器 Worker 内执行。单文件 64 MiB，打开原文总量 128 MiB；服务器文件总库不受这两个阅读窗口限制。</p><p class="hint">更新应用代码不会清理 data/、var/ 或浏览器数据。建议使用固定 URL，并定期导出本机备份。</p>`);};
async function freshRulesAndRender(){await getShared();reconcileProfiles();await reapplyLoaded();render();}
async function refreshEverything(){await getShared();reconcileProfiles();await refreshCatalog();await ensureFilesLoaded(S.opened,{force:true});await reapplyLoaded();render();}
async function sharedRuleAction(action){
  const p=profile();
  if(p._private&&['confirm-delete-profile','restore-profile','copy-profile'].includes(action)){
    if(action==='copy-profile'){const copy=clone(p);copy.id='private-'+Date.now();copy.name+=' · 副本';copy.deleted=false;S.profiles.push(copy);S.appliedProfiles.push(clone(copy));S.profile=copy.id;}
    else{const deleted=action==='confirm-delete-profile';if(deleted&&(Object.values(S.fileBindings).includes(p.id)||Object.values(S.dirBindings).includes(p.id)))throw Error('请先移除个人文件或目录绑定。');p.deleted=deleted;const q=S.appliedProfiles.find(x=>x.id===p.id);if(q)q.deleted=deleted;$('#dialog').close();}
    save('profiles',S.appliedProfiles);render();return;
  }
  if(!isAdmin())throw Error('管理员权限不足。');
  if(action==='create-profile'){
    const name=$('#new-profile-name').value.trim();if(!name)throw Error('请填写规则名称。');const d={...Engine.defaults(),name,format:'bracket'};const {item}=await api('/rules','POST',{name,description:'自定义提取与判定',profile:d});S.profile=item.id;S.profileTrash=false;S.ruleSection='parse';$('#dialog').close();
  }else if(action==='copy-profile'){
    const copy=clone(p);delete copy._serverRevision;delete copy._private;copy.name=p.name+' · 副本';const {item}=await api('/rules','POST',{name:copy.name,description:p.desc||'',profile:copy});S.profile=item.id;S.profileTrash=false;
  }else if(action==='confirm-delete-profile'){
    await api('/rules/'+p.id,'DELETE',{expectedRevision:p._serverRevision});S.dirtyProfiles?.delete(p.id);$('#dialog').close();S.profileTrash=false;S.profile=Net.rules.find(q=>!q.deletedAt&&q.id!==p.id)?.id||p.id;
  }else if(action==='restore-profile'){await api('/rules/'+p.id+'/restore','POST',{expectedRevision:p._serverRevision});S.profileTrash=false;}
  S.dirty=false;await freshRulesAndRender();notify('规则库已更新。');
}
// Intercept service-owned actions before the legacy document handlers. Local-only interactions remain R5.
const SERVICE_ACTIONS=new Set(['nav','identity','open-file','open-selected','download-file','confirm-delete-files','restore-file','new-folder','confirm-folder','create-profile','copy-profile','confirm-delete-profile','restore-profile','profile-trash','connection','new-user','backend-required','test-sample','close-file','service-refresh-files','service-refresh-rules','service-dir-fold','service-lib-page','service-delete-directory','service-confirm-directory-delete','service-admin-refresh','service-admin-tab','service-user-edit','service-user-save','service-identity-preview','service-identity-save','service-reload','service-state','service-discard-draft','service-discard-confirm','service-user-delete','service-user-delete-confirm','picker-full-time']);
window.addEventListener('click',async e=>{
  const el=e.target.closest('[data-act]');if(!el||!SERVICE_ACTIONS.has(el.dataset.act))return;
  const act=el.dataset.act,id=el.dataset.id,v=el.dataset.value;e.preventDefault();e.stopImmediatePropagation();
  if(Net.busy&&!['service-reload','service-state'].includes(act))return;
  try{switch(act){
    case'nav':S.view=v;S.inspector=null;render(false);if(v==='files'){await refreshCatalog();if(S.libSource==='trash')await refreshTrash();render();}if(v==='admin')await loadAdmin();if(v==='rules'){await getShared();reconcileProfiles();render();}break;
    case'identity':dialog('当前身份',`<div class="settings-block"><div class="settings-row"><strong>${E(Net.me.user.name)}</strong><code>${E(Net.me.user.ip)}</code><span class="badge">${ROLE_NAMES[Net.me.user.role]}</span></div></div><p class="hint">服务端确认的 IP 身份。个人文件与设置使用独立本机命名空间；不会因页面切换清空。</p>`,B('关闭','close-dialog')+B('管理后台','nav','settings','', 'data-value="admin"'));break;
    case'open-file':await openFile(id);break;
    case'open-selected':{const ids=[...S.libSelected].filter(id=>files.some(f=>f.id===id&&!f.deleted));await ensureFilesLoaded(ids);S.opened=[...new Set([...S.opened,...ids])];await openFile(ids.length===1?ids[0]:'merged');break;}
    case'close-file':{const f=files.find(f=>f.id===id);S.opened=S.opened.filter(x=>x!==id);if(f?.source==='server'&&!S.compare.ids.includes(id)&&!selectedGlobalIds().includes(id)){f.records=[];f.text=undefined;f.loaded=false;}if(S.file===id)S.file=S.opened[0]||null;S.inspector=null;indexRecords();render();break;}
    case'download-file':await downloadFile(id);break;
    case'confirm-delete-files':await deleteSelected();break;
    case'restore-file':await restoreDeleted(id);break;
    case'new-folder':if(!isAdmin())throw Error('需要管理员权限。');dialog('新建服务器目录',`<p class="hint">父目录：${E(S.folder||'data')}</p><label class="field"><span class="label">文件夹名称</span><input class="input" id="folder-name" placeholder="例如：设备 A"></label>`,B('取消','close-dialog')+B('创建','confirm-folder','plus','primary'));break;
    case'confirm-folder':{const name=$('#folder-name').value.trim();if(!name||/[\\/]/.test(name))throw Error('请输入不包含路径分隔符的文件夹名。');const parent=relDir(S.folder||'data');await api('/files/directory','POST',{path:(parent?parent+'/':'')+name});$('#dialog').close();await refreshCatalog();render();break;}
    case'service-refresh-files':Net.busy=true;await refreshEverything();await refreshTrash();render();notify('目录和已打开文件已刷新。');break;
    case'service-refresh-rules':await freshRulesAndRender();notify('共享规则已刷新，未保存草稿仍保留。');break;
    case'service-dir-fold':S.collapsedDirs.has(el.dataset.path)?S.collapsedDirs.delete(el.dataset.path):S.collapsedDirs.add(el.dataset.path);render();break;
    case'service-lib-page':S.libPage=Math.max(0,(S.libPage||0)+Number(v));render();break;
    case'service-delete-directory':S.directoryDelete=el.dataset.path;dialog('删除共享目录',`<p>将目录 <strong>${E('data/'+S.directoryDelete)}</strong> 与全部子文件移入服务器回收站，影响所有用户。</p><p class="hint">可以恢复。若原路径已存在同名内容，恢复不会覆盖。</p>`,B('取消','close-dialog')+B('移入回收站','service-confirm-directory-delete','trash','danger'));break;
    case'service-confirm-directory-delete':await api('/files?path='+encodeURIComponent(S.directoryDelete),'DELETE',{});$('#dialog').close();S.folder='';await refreshCatalog();await refreshTrash();render();break;
    case'create-profile':case'copy-profile':case'confirm-delete-profile':case'restore-profile':await sharedRuleAction(act);break;
    case'profile-trash':if(!isAdmin())throw Error('需要管理员权限。');S.profileTrash=!S.profileTrash;await getShared();reconcileProfiles();S.profile=S.profiles.find(p=>!!p.deleted===S.profileTrash)?.id||S.profile;render();break;
    case'new-user':editUser(-1);break;
    case'service-user-edit':editUser(Number(el.dataset.index));break;
    case'service-user-save':try{await api('/admin/users','PUT',{ip:$('#service-user-ip').value.trim(),name:$('#service-user-name').value.trim(),role:$('#service-user-role').value,enabled:$('#service-user-enabled').checked,expectedRevision:S.userDraft.revision||0});$('#dialog').close();await loadAdmin();}catch(err){$('#service-user-error').textContent=err.message;}break;
    case'service-discard-draft':dialog('放弃当前规则草稿',`<p>将丢弃“${E(profile().name)}”的未发布修改，载入已保存版本（共享规则取服务器最新版本）。其他规则草稿不受影响。</p>`,B('取消','close-dialog')+B('放弃并载入','service-discard-confirm','restore','danger'));break;
    case'service-discard-confirm':{const id=S.profile;await getShared();if(profile()._private){const saved=S.appliedProfiles.find(x=>x.id===id);if(saved)S.profiles=S.profiles.map(x=>x.id===id?clone(saved):x);}S.dirtyProfiles?.delete(id);reconcileProfiles();$('#dialog').close();await reapplyLoaded();render();break;}
    case'service-user-delete':dialog('删除用户登记',`<p>移除 ${E(S.userDraft.ip)} 的姓名与权限。此 IP 将退回访客而非禁止访问；需要阻止访问时请使用“停用”。</p>`,B('取消','close-dialog')+B('删除登记','service-user-delete-confirm','trash','danger'));break;
    case'service-user-delete-confirm':await api('/admin/users?ip='+encodeURIComponent(S.userDraft.ip),'DELETE',{expectedRevision:S.userDraft.revision});$('#dialog').close();await loadAdmin();break;
    case'connection':S.view='admin';Net.adminTab='connection';$('#dialog').close();await loadAdmin();break;
    case'service-admin-refresh':await loadAdmin();break;
    case'service-admin-tab':Net.adminTab=v;render();break;
    case'service-identity-preview':{const result=await api('/admin/identity-preview','POST',{config:identityConfig()});const box=$('#service-preview-result');box.hidden=false;box.textContent=JSON.stringify(result,null,2);break;}
    case'service-identity-save':{const config=identityConfig();await api('/admin/identity-preview','POST',{config});await api('/admin/config','PUT',{config,expectedRevision:Net.admin.settings.revision});Net.me=await api('/me');window.LB_SESSION=Net.me;await loadAdmin();notify('身份来源已生效；每次请求重新检查权限。');break;}
    case'backend-required':throw Error('该旧入口不可用，请在管理后台操作。');
    case'test-sample':{const f=files.find(x=>x.records?.length&&!x.deleted);S.testInput=f?f.records.slice(0,20).map(r=>r.raw.join('\n')).join('\n'):'[2026-10-26 11:39:15.39765329] [EVENT] [APP-A] [6169] [41413] [algorithm.py:1228] [100] [0:0:0] get_setpoint() <CPQPSGX> get current PPe: 4.5mJ';render();break;}
    case'picker-full-time':if(!S.picker?.ids.size)throw Error('请先选择文件。');el.disabled=true;await ensureFilesLoaded([...S.picker.ids]);{const b=boundsOf(files.filter(f=>S.picker.ids.has(f.id)&&!f.deleted));updatePickerTime(b.start,b.end);refreshPickerTree();}break;
    case'service-reload':flushState();location.reload();break;
    case'service-state':if(Net.nextRelease){flushState();await writeSnapshot(stateSnapshot());location.reload();}else{await pollIdentity();notify(Net.online?'服务连接正常。':'服务暂不可用。',!Net.online);}break;
  }}catch(err){notify(err.message,true);}finally{Net.busy=false;if(el.isConnected)el.disabled=false;updateServiceStatus();scheduleStateSave();}
},true);
window.addEventListener('change',e=>{const el=e.target;if(el.dataset.serviceFile){e.stopImmediatePropagation();el.checked?S.libSelected.add(el.dataset.serviceFile):S.libSelected.delete(el.dataset.serviceFile);render();}if(el.hasAttribute('data-service-check-all')){e.stopImmediatePropagation();for(const cb of $$('[data-service-file]'))el.checked?S.libSelected.add(cb.dataset.serviceFile):S.libSelected.delete(cb.dataset.serviceFile);render();}},true);
window.addEventListener('click',e=>{const el=e.target.closest('[data-act]');if(el?.dataset.act==='lib-source'){S.libPage=0;if(el.dataset.value==='trash')setTimeout(()=>refreshTrash().then(()=>render()).catch(err=>notify(err.message,true)),0);}if(el?.dataset.act==='folder')S.libPage=0;},true);
async function pollIdentity(){if(!Net.initialized)return;try{const me=await api('/me');if(me.user.id!==Net.me.user.id){blockIdentity('访问身份已变化。请刷新后进入对应工作区；不会将旧身份缓存迁移给新身份。');return;}const changed=me.user.role!==Net.me.user.role;Net.me=me;const h=await api('/health');if(Net.release&&h.release!==Net.release)Net.nextRelease=h.release;if(changed)render();}catch{}updateServiceStatus();}
setInterval(pollIdentity,15000);
window.addEventListener('focus',pollIdentity);
const renderLogTableConnected=renderLogTable;
renderLogTable=function(rows){if(!S.opened.length&&!S.globalSelection)return`<div class="empty"><span class="empty-ico">${I('file','lg')}</span><strong>打开日志开始分析</strong><p>本机日志仅在浏览器缓存中保存；服务器日志从共享文件库按需读取。</p><div class="row">${B('打开本机日志','import','upload','primary')}${B('浏览服务器文件','nav','folder','','data-value="files"')}</div></div>`;return renderLogTableConnected(rows);};
window.LogBenchService={api,refresh:refreshEverything,ensureFilesLoaded,refreshRules:freshRulesAndRender,loadAdmin,getState:()=>Net};
