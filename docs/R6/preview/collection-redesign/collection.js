'use strict';
// Isolated design prototype. All records and mutations below are in memory.
// No API, credentials, localStorage or production business code is used.
const $ = s => document.querySelector(s);
const E = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const B = (label, action, icon, cls='', id='') => `<button class="btn ${cls}" data-cc="${action}" ${id?`data-id="${E(id)}"`:''}>${icon?I(icon):''}${E(label)}</button>`;
const D = (label,value,cls='') => `<dt>${label}</dt><dd class="${cls}">${value}</dd>`;
const states = {following:['持续追踪','good','circlecheck'],recovering:['补采中','blue','refresh'],paused:['已暂停','','pause'],error:['连接异常','bad','warning'],limited:['定时快照','','clock'],ready:['待首次采集','','clock']};
const sources = [
 {id:'machine-a',name:'机台 A · 主控日志',protocol:'SFTP',host:'192.168.10.21',remote:'/var/log/cpqpsgx/',root:'生产日志',target:'machine-a/main',state:'following',raw:'24.8 GB',files:128,coverage:100,lag:'已追上',mode:'持续增量',rule:'CPQPSGX 标准',maps:2},
 {id:'machine-b',name:'机台 B · 主控日志',protocol:'SFTP',host:'192.168.10.22',remote:'/var/log/cpqpsgx/',root:'生产日志',target:'machine-b/main',state:'following',raw:'18.2 GB',files:96,coverage:100,lag:'已追上',mode:'持续增量',rule:'CPQPSGX 标准',maps:1},
 {id:'diagnostics',name:'诊断服务器',protocol:'SMB',host:'diag.internal',remote:'//diag.internal/logs/',root:'设备诊断',target:'devices/diagnostics',state:'recovering',raw:'8.6 GB',files:364,coverage:72,lag:'历史归档中',mode:'持续增量',rule:'设备诊断',maps:3},
 {id:'test-bench',name:'测试台 · 运行日志',protocol:'SFTP',host:'192.168.10.43',remote:'/opt/testbench/logs/',root:'生产日志',target:'test-bench/runtime',state:'error',raw:'4.1 GB',files:42,coverage:100,lag:'SSH 主机密钥不匹配',mode:'持续增量',rule:'CPQPSGX 标准',maps:1},
 {id:'build',name:'构建服务',protocol:'HTTPS',host:'build.internal',remote:'https://build.internal/logs/',root:'研发归档',target:'build/nightly',state:'limited',raw:'3.4 GB',files:816,coverage:100,lag:'每 30 分钟同步',mode:'定时快照',rule:'通用方括号',maps:1},
 {id:'machine-c',name:'机台 C · 设备日志',protocol:'SFTP',host:'192.168.10.23',remote:'/var/log/device/',root:'生产日志',target:'machine-c/device',state:'paused',raw:'12.7 GB',files:84,coverage:96,lag:'手动暂停',mode:'持续增量',rule:'设备诊断',maps:2},
 {id:'legacy',name:'旧设备 · 历史日志',protocol:'SCP',host:'192.168.10.15',remote:'/data/logs/archive/',root:'研发归档',target:'legacy/archive',state:'limited',raw:'1.9 GB',files:32,coverage:100,lag:'稳定文件快照',mode:'稳定文件快照',rule:'通用方括号',maps:1},
 {id:'new-machine',name:'机台 D · 联调日志',protocol:'SFTP',host:'192.168.10.24',remote:'/var/log/commissioning/line-04/controller/diagnostics/session-current/',root:'设备诊断',target:'line-04/controller/diagnostics',state:'ready',raw:'0 B',files:0,coverage:null,lag:'尚未开始',mode:'持续增量',rule:'设备诊断',maps:1}
];
const roots = [
 {id:'production',name:'生产日志',path:'/srv/logbench/archive/production',index:'/srv/logbench/index/production',state:'online',free:'1.24 TB',maps:6},
 {id:'diagnostic',name:'设备诊断',path:'/mnt/diagnostics/archive',index:'/srv/logbench/index/diagnostics',state:'online',free:'428 GB',maps:4},
 {id:'research',name:'研发归档',path:'/mnt/research/logs',index:'/srv/logbench/index/research',state:'offline',free:null,maps:2}
];
const events = [
 ['14:32:18','机台 A · 主控日志','归档完成','good','提交 128 KB 增量，检查点已更新'],
 ['14:32:16','测试台 · 运行日志','连接异常','bad','SSH 主机密钥不匹配，已停止连接'],
 ['14:32:12','诊断服务器','历史补采','blue','devices/diagnostics · 已归档 8.6 GB'],
 ['14:31:54','机台 B · 主控日志','索引完成','good','device_io.log · 规则结果已切换'],
 ['14:31:03','研发归档','存储离线','bad','挂载不可用，该位置的写入已暂停'],
 ['14:30:20','构建服务','快照完成','good','build_20261108.log · 96 MB'],
 ['14:29:48','机台 C · 设备日志','手动暂停','','管理员暂停采集，保留已归档数据'],
 ['14:28:07','机台 D · 联调日志','预检通过','good','连接、目录与目标权限检查通过'],
 ['14:26:51','旧设备 · 历史日志','快照完成','good','archive_20261107.log · 18 MB'],
 ['14:24:09','诊断服务器','重新连接','blue','从已确认检查点继续']
];
const S = {tab:'sources',search:'',filter:'all',scenario:'normal',selected:null,notice:'',step:0,draft:null,probe:null,probeError:false};
const tabs = [['sources','远程日志源'],['storage','存储位置'],['cache','解析缓存'],['events','运行记录']];
const badge=(text,tone='',icon='')=>`<span class="cc-state ${tone}">${icon?I(icon):'<span class="cc-dot"></span>'}${E(text)}</span>`;
const stateBadge = s => badge(...states[s]);
const counts=()=>({sources:sources.length,storage:roots.length,cache:sources.filter(s=>s.files>0).length,events:events.length});
const isReadOnly=()=>S.scenario==='readonly';
function icons(){document.querySelectorAll('[data-icon]').forEach(e=>{e.innerHTML=I(e.dataset.icon);});}
function setNotice(text){S.notice=text;renderBanner();$('.cc-live').textContent=text;}
function renderBanner(){
 const b=$('#banner');
 if(S.scenario==='error') b.innerHTML=`<div class="cc-banner bad" role="alert">${I('warning')}<span>无法连接采集服务。列表未更新。</span>${B('重新连接','retry','refresh')}</div>`;
 else if(isReadOnly()) b.innerHTML=`<div class="cc-banner">${I('lock')}<span>当前身份为只读，无法修改采集配置。</span></div>`;
 else if(S.notice) b.innerHTML=`<div class="cc-banner" role="status">${I('info')}<span>${E(S.notice)}</span>${B('关闭','dismiss','close','ghost')}</div>`;
 else b.replaceChildren();
}
function render(){
 const count=counts();
 $('.cc-tabs').innerHTML=tabs.map(([id,label])=>`<button class="cc-tab" id="tab-${id}" role="tab" aria-controls="panel" aria-selected="${S.tab===id}" tabindex="${S.tab===id?0:-1}" data-cc="tab" data-id="${id}">${label}<span class="cc-tab-count">${S.scenario==='empty'?0:count[id]}</span></button>`).join('');
 $('#panel').setAttribute('aria-labelledby','tab-'+S.tab);
 $('#panel').setAttribute('aria-busy',S.scenario==='loading'?'true':'false');
 const create=$('#create');create.hidden=!['sources','storage'].includes(S.tab);create.disabled=isReadOnly();
 create.innerHTML=I('plus')+`<span>${S.tab==='storage'?'添加存储位置':'添加日志源'}</span>`;
 $('[data-cc=budget]').disabled=isReadOnly();
 const filters=S.tab==='sources'?[['all','全部状态'],...Object.entries(states).map(([id,v])=>[id,v[0]])]:S.tab==='storage'?[['all','全部状态'],['online','在线'],['offline','离线']]:S.tab==='cache'?[['all','全部状态'],['valid','可用'],['partial','待追上']]:[['all','全部级别'],['good','正常'],['bad','异常'],['blue','处理中']];
 $('#filter').innerHTML=filters.map(([id,l])=>`<option value="${id}" ${S.filter===id?'selected':''}>${l}</option>`).join('');
 renderData();renderBanner();icons();
}
function renderData(){
 $('#clear-search').hidden=!S.search;
 const all=S.tab==='storage'?roots:S.tab==='events'?events:S.tab==='cache'?sources.filter(s=>s.files>0):sources;
 const rows=all.filter(x=>JSON.stringify(x).toLowerCase().includes(S.search.toLowerCase())).filter(x=>S.filter==='all'||(S.tab==='events'?x[3]===S.filter:S.tab==='cache'?(S.filter==='valid'?x.coverage===100:x.coverage!==100):x.state===S.filter));
 $('#count').textContent=`${rows.length} 项`;
 $('#foot-count').textContent=`${S.scenario==='empty'?0:rows.length} / ${S.scenario==='empty'?0:all.length} 项`;
 let html='';
 if(S.scenario==='loading') html=`<div class="cc-empty" role="status">${I('refresh')}<strong>正在加载${tabs.find(t=>t[0]===S.tab)[1]}</strong></div>`;
 else if(S.scenario==='error') html=`<div class="cc-empty">${I('server')}<strong>暂时无法获取列表</strong></div>`;
 else if(S.scenario==='empty') html=`<div class="cc-empty">${I(S.tab==='storage'?'database':S.tab==='events'?'clock':'server')}<strong>${{sources:'尚未添加日志源',storage:'尚未添加存储位置',cache:'暂无解析缓存',events:'暂无运行记录'}[S.tab]}</strong>${['sources','storage'].includes(S.tab)?'<p>使用右上角的添加按钮开始配置。</p>':''}</div>`;
 else if(!rows.length) html=`<div class="cc-empty">${I('search')}<strong>没有匹配项</strong>${B('清除筛选','clear','close')}</div>`;
 else if(S.tab==='sources') html=table(['日志源','目录 / 存储位置','同步状态','归档量','索引覆盖','操作'],['26%','25%','16%','11%','11%','11%'],rows.map(x=>`<tr class="${S.selected===x.id?'cc-selected':''}"><td><div class="cc-source"><span class="cc-source-icon">${I('server')}</span><div class="cc-object"><button class="cc-name" data-cc="source" data-id="${x.id}">${E(x.name)}</button><span class="cc-sub">${E(x.protocol)} <span class="mono">${E(x.host)}</span></span></div></div></td><td><span class="cc-path">${E(x.remote)}</span><span class="cc-sub">${E(x.root)}${x.maps>1?`　+${x.maps-1} 个目录`:''}</span></td><td>${stateBadge(x.state)}<span class="cc-sub">${E(x.lag)}</span></td><td class="cc-numeric"><span class="mono">${x.raw}</span><span class="cc-sub">${x.files} 个文件</span></td><td class="cc-numeric">${x.coverage===null?'<span class="cc-sub">未建立</span>':`<span class="mono">${x.coverage}%</span><div class="cc-tinybar" role="progressbar" aria-label="${E(x.name)}索引覆盖" aria-valuenow="${x.coverage}" aria-valuemin="0" aria-valuemax="100"><span style="width:${x.coverage}%"></span></div>`}</td><td><div class="cc-actions">${B(x.state==='error'?'检查':'详情','source',null,'ghost sm',x.id)}<button class="btn icon ghost sm" data-cc="pause" data-id="${x.id}" aria-label="${x.state==='paused'?'继续':'暂停'} ${E(x.name)}" ${isReadOnly()?'disabled':''}>${I(x.state==='paused'?'play':'pause')}</button></div></td></tr>`).join(''));
 else if(S.tab==='storage') html=table(['存储位置','物理目录 / 索引目录','状态','可用空间','映射数','操作'],['19%','37%','12%','12%','8%','12%'],rows.map(x=>`<tr><td><div class="cc-source">${I('database')}<div class="cc-object"><strong>${E(x.name)}</strong><span class="cc-sub">${E(x.id)}</span></div></div></td><td><span class="cc-path">${E(x.path)}</span><span class="cc-sub mono">${E(x.index)}</span></td><td>${badge(x.state==='online'?'在线':'挂载离线',x.state==='online'?'good':'bad',x.state==='online'?'circlecheck':'warning')}</td><td class="cc-numeric mono">${x.free??'未知'}</td><td class="cc-numeric">${x.maps}</td><td><div class="cc-actions">${B('设置','root',null,'ghost sm',x.id)}</div></td></tr>`).join(''));
 else if(S.tab==='cache') html=table(['归档来源','原文大小','结构索引','规则结果','覆盖率','操作'],['27%','12%','15%','20%','12%','14%'],rows.map(x=>`<tr><td><button class="cc-name" data-cc="cache" data-id="${x.id}">${E(x.name)}</button><span class="cc-sub mono">${E(x.target)}/runtime.log</span></td><td class="cc-numeric mono">${x.raw}</td><td>${badge(x.coverage===100?'可复用':'建立中',x.coverage===100?'good':'blue')}</td><td><span>${E(x.rule)}</span><span class="cc-sub">${x.coverage===100?'当前版本有效':'已有结果可读'}</span></td><td class="cc-numeric mono">${x.coverage}%</td><td><div class="cc-actions">${B('详情','cache',null,'ghost sm',x.id)}</div></td></tr>`).join(''));
 else html=table(['时间','来源','事件','内容'],['12%','23%','14%','51%'],rows.map(x=>`<tr><td class="mono">${x[0]}</td><td>${E(x[1])}</td><td>${badge(x[2],x[3])}</td><td>${E(x[4])}</td></tr>`).join(''));
 $('#data').innerHTML=html;
 if(isReadOnly())$('#data').querySelectorAll('[data-cc=root]').forEach(b=>{b.disabled=true;});
 if(S.tab==='events'&&S.scenario==='normal')$('#count').innerHTML=B('导出示例记录','export','download','ghost sm');
}
function table(headers,widths,body){return `<table class="cc-table"><colgroup>${widths.map(w=>`<col style="width:${w}">`).join('')}</colgroup><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table>`;}
let detailTrigger=null;
function openDetail(id,cache=false){
 const x=sources.find(x=>x.id===id);if(!x)return;
 detailTrigger=document.activeElement;S.selected=id;renderData();
 const d=$('#detail');d.dataset.id=id;
 const maps=Array.from({length:x.maps},(_,i)=>`<div class="cc-map">${I('folder')}<div><div class="mono">${E(x.remote)}${i?'diagnostics/':''}</div><div class="cc-sub">存入 ${E(x.root)}</div><div class="mono">${E(x.target)}${i?'/diagnostics':''}</div><div class="cc-sub">${E(x.rule)}</div></div></div>`).join('');
 d.innerHTML=`<header class="cc-detail-head"><div class="grow"><h2 id="detail-title">${cache?'解析缓存 · ':''}${E(x.name)}</h2><span class="cc-sub">${E(x.protocol)} <span class="mono">${E(x.host)}</span></span></div><button class="btn icon ghost" data-cc="close-detail" aria-label="关闭详情">${I('close')}</button></header><div class="cc-detail-body">${x.state==='error'?`<div class="cc-banner bad" role="alert">${I('warning')}<span>SSH 主机密钥不匹配。请在服务端核实后重新预检。</span></div>`:''}<h3>${cache?'缓存状态':'采集状态'}</h3><div style="margin-bottom:12px">${stateBadge(x.state)}</div><div class="cc-track"><div><span>归档原文</span><strong class="mono">${x.raw}</strong></div><div><span>文件</span><strong class="mono">${x.files}</strong></div><div><span>索引覆盖</span><strong class="mono">${x.coverage===null?'未建立':x.coverage+'%'}</strong></div></div><h3>${cache?'索引信息':'连接与策略'}</h3><dl class="cc-facts">${D('主机',E(x.host),'mono')}${D('同步方式',E(x.mode))}${D('凭据','只读采集密钥')}${D('访问范围','仅管理员')}${D('历史范围','完整历史')}${D('检查间隔','2 秒 / 目录 60 秒')}${D('源 ID',E(x.id),'mono')}</dl><h3>目录映射 <span class="muted">${x.maps}</span></h3>${maps}${cache?'<p class="cc-field-note">重建只替换解析结果，保留归档原文。</p>':''}</div><footer class="cc-detail-foot">${B(cache?'重建规则结果':'编辑配置',cache?'rebuild':'edit',cache?'refresh':'edit','',id)}<span class="spacer"></span><button class="btn" data-cc="pause" data-id="${id}" ${isReadOnly()?'disabled':''}>${I(x.state==='paused'?'play':'pause')}${x.state==='paused'?'继续采集':'暂停采集'}</button></footer>`;
 d.querySelector('[data-cc=edit],[data-cc=rebuild]').disabled=isReadOnly();
 if(!d.open)d.showModal();
}
function closeDetail(){const id=S.selected;$('#detail').close();S.selected=null;renderData();document.querySelector(`[data-cc="source"][data-id="${id}"],[data-cc="cache"][data-id="${id}"]`)?.focus();}
function field(label,key,value='',type='text',extra=''){return `<div class="field"><label class="label" for="f-${key}">${label}</label><input class="input ${['host','remote','target','path','index'].includes(key)?'mono':''}" id="f-${key}" name="${key}" value="${E(value)}" type="${type}" ${extra}></div>`;}
function select(label,key,options,value){return `<div class="field"><label class="label" for="f-${key}">${label}</label><select class="select" id="f-${key}" name="${key}">${options.map(v=>`<option value="${E(v)}" ${v===value?'selected':''}>${E(v)}</option>`).join('')}</select></div>`;}
function openWizard(id){
 if(isReadOnly())return;
 if($('#detail').open)closeDetail();
 const x=sources.find(x=>x.id===id);
 S.draft=x?{...x,port:'22',credential:'只读采集密钥',poll:'2',scan:'60',history:'完整历史',access:'仅管理员'}:{name:'',host:'',port:'22',protocol:'SFTP',credential:'只读采集密钥',mode:'持续增量',history:'完整历史',access:'仅管理员',poll:'2',scan:'60',remote:'/var/log/',target:'',root:'生产日志',rule:'CPQPSGX 标准',include:'*.log, *.log.*',maps:1};
 S.step=0;S.probe=null;S.probeError=false;drawWizard();$('#editor').showModal();
}
function capture(){if(!S.draft)return;$('#editor').querySelectorAll('[name]').forEach(e=>{S.draft[e.name]=e.type==='checkbox'?e.checked:e.value;});}
function drawWizard(){
 const d=S.draft;const steps=['连接','目录映射','采集策略','预检'];
 let body='';
 if(S.step===0)body=field('日志源名称','name',d.name,'text','required maxlength="100" placeholder="例如：机台 A · 主控日志"')+select('传输协议','protocol',['SFTP','SMB','HTTPS','SCP'],d.protocol)+`<div class="cc-columns">${field('服务器地址','host',d.host,'text','required placeholder="192.168.10.21"')}${field('端口','port',d.port,'number','required min="1" max="65535"')}${select('凭据引用','credential',['只读采集密钥','诊断共享只读账号','构建服务 Token'],d.credential)}</div><label class="check"><input type="checkbox" name="verify" checked disabled>验证${d.protocol==='HTTPS'?' TLS 证书':'已登记的主机身份'}</label>`;
 if(S.step===1)body=Array.from({length:d.maps},(_,i)=>`<section class="cc-map-fields"><h3 class="cc-form-section">目录 ${i+1}<span class="spacer"></span>${i?B('移除','remove-map',null,'ghost sm',String(i)):''}</h3>${field('远程目录',i?'remote'+i:'remote',i?(d['remote'+i]??'/var/log/diagnostics/'):d.remote,'text','required')}<div class="cc-columns">${select('存储位置',i?'root'+i:'root',roots.filter(r=>r.state==='online').map(r=>r.name),i?(d['root'+i]??d.root):d.root)}${field('存储子目录',i?'target'+i:'target',i?(d['target'+i]??'diagnostics'):d.target,'text','required placeholder="machine-a/main"')}${select('解析规则',i?'rule'+i:'rule',['CPQPSGX 标准','设备诊断','通用方括号'],i?(d['rule'+i]??d.rule):d.rule)}${field('文件匹配',i?'include'+i:'include',(i?d['include'+i]:d.include)||'*.log, *.log.*')}</div><details><summary>排除模式与扫描深度</summary><div class="cc-columns">${field('排除模式','exclude'+i,d['exclude'+i]||'.logbench-*')}${field('扫描深度','depth'+i,d['depth'+i]??8,'number','min="0" max="32"')}</div></details></section>`).join('')+B('添加目录映射','add-map','plus','ghost');
 if(S.step===2)body=`<div class="cc-columns">${select('同步方式','mode',d.protocol==='SCP'?['稳定文件快照']:d.protocol==='HTTPS'?['定时同步','稳定文件快照']:['持续增量','定时同步','稳定文件快照'],d.mode)}${select('首次接入','history',['完整历史','仅接入后新增'],d.history)}${field('活动文件检查间隔（秒）','poll',d.poll,'number','min="1"')}${field('目录发现间隔（秒）','scan',d.scan,'number','min="1"')}${select('归档访问范围','access',['仅管理员','管理员与研发组'],d.access)}</div>${d.history==='仅接入后新增'?'<div class="cc-banner">历史内容不采集，归档将保留历史缺口标记。</div>':''}<details><summary>归档与恢复策略</summary><p>断线续采；保留轮转与截断前的归档；存储不可用时暂停写入。远端已删除或覆盖的内容无法补回。</p></details>`;
 if(S.step===3)body=`<dl class="cc-facts">${D('日志源',E(d.name))}${D('连接',E(d.protocol+' · '+d.host+':'+d.port),'mono')}${D('目录映射',d.maps+' 个')}${D('同步方式',E(d.mode))}${D('历史范围',E(d.history))}${D('访问范围',E(d.access))}</dl><div style="display:flex;align-items:center;gap:8px;margin-top:24px">${B('运行预检','probe','shield')}<label class="check"><input id="probe-error" type="checkbox" ${S.probeError?'checked':''}>模拟预检失败</label></div><div id="probe-results" style="margin-top:12px">${S.probe===null?'<p class="cc-field-note">通过预检后可保存。</p>':['连接与主机身份','目录发现与读取','存储目录写入权限'].map((l,i)=>`<div class="cc-probe-row ${S.probeError&&i===0?'bad':''}">${I(S.probeError&&i===0?'warning':'check')}<span>${l}</span><span class="spacer"></span><span>${S.probeError&&i===0?'主机密钥不匹配':'通过'}</span></div>`).join('')}</div>${S.probe===false?'<p class="cc-form-error" role="alert">请核实服务端主机密钥后重新预检。当前输入已保留。</p>':''}`;
 $('#editor').innerHTML=`<header class="cc-modal-head"><h2 id="editor-title">${d.id?'编辑日志源':'添加日志源'}</h2><span class="spacer"></span><button class="btn icon ghost" data-cc="cancel" aria-label="关闭日志源设置">${I('close')}</button></header><ol class="cc-steps">${steps.map((n,i)=>`<li ${i===S.step?'aria-current="step"':''}><span class="cc-stepno">${i+1}</span>${n}</li>`).join('')}</ol><form class="cc-modal-body" id="source-form">${body}<p id="form-error" class="cc-form-error" role="alert"></p></form><footer class="cc-modal-foot">${B('取消','cancel')}<span class="spacer"></span>${S.step?B('上一步','back','left'):''}${S.step<3?B('下一步','next',null,'primary'):`<button class="btn primary" data-cc="save-source" ${S.probe!==true?'disabled':''}>保存示例日志源</button>`}</footer>`;
}
function modal(title,body,save,action){
 const d=$('#editor');d.innerHTML=`<header class="cc-modal-head"><h2 id="editor-title">${title}</h2><span class="spacer"></span><button class="btn icon ghost" data-cc="cancel" aria-label="关闭${title}">${I('close')}</button></header><form class="cc-modal-body">${body}<p class="cc-form-error" id="form-error" role="alert"></p></form><footer class="cc-modal-foot">${B('取消','cancel')}<span class="spacer"></span>${B(save,action,null,'primary')}</footer>`;d.showModal();
}
function openRoot(id){const r=roots.find(r=>r.id===id);modal(r?'存储位置设置':'添加存储位置',field('位置名称','root-name',r?.name||'','text','required')+field('服务器绝对路径','path',r?.path||'','text',`required ${r?'readonly':''}`)+field('索引目录','index',r?.index||'','text','required')+(r?'<p class="cc-field-note">已有位置的物理目录只读；变更需单独迁移。</p>':'')+`<div id="root-probe" class="cc-inline-feedback"></div>`,'预检并保存示例','save-root');$('#editor').dataset.root=id||'';}
function openBudget(){modal('资源设置',`<div class="cc-columns">${[['全局传输并发',8],['每主机并发',2],['解析进程',4],['历史补采并发',2],['内存预算（GiB）',16]].map(([l,v],i)=>field(l,'budget'+i,v,'number','min="1" max="64" required')).join('')}</div><details><summary>其余预算 · 只读</summary><dl class="cc-facts">${[['查询槽位','8'],['CPU 总上限','8'],['热页缓存','4 GiB'],['上传槽位','2'],['单身份上传','1'],['单身份查询','2'],['交互保留位','2'],['读写块大小','服务端固定']].map(([k,v])=>D(k,v)).join('')}</dl></details>`,'保存示例设置','save-budget');}
function validate(){const form=$('#editor form');if(!form.reportValidity())return false;return true;}
function cancelEditor(){
 if(S.draft){capture();$('#editor').innerHTML=`<header class="cc-modal-head"><h2 id="editor-title">放弃当前配置？</h2></header><div class="cc-modal-body"><p>关闭后，本次未保存的输入将丢失。</p></div><footer class="cc-modal-foot">${B('继续编辑','keep-edit')}<span class="spacer"></span>${B('放弃更改','discard',null,'danger')}</footer>`;}
 else $('#editor').close();
}
document.addEventListener('click',e=>{
 const el=e.target.closest('[data-cc]');if(!el||el.disabled)return;const a=el.dataset.cc,id=el.dataset.id;
 if(a==='tab'){S.tab=id;S.search='';S.filter='all';S.notice='';$('#search').value='';render();$('#tab-'+id).focus();}
 if(a==='nav'){if(innerWidth>1100)$('#preview-app').classList.toggle('nav-collapsed');else setNotice('此预览仅展示采集中心；应用导航保持现有设计。');}
 if(a==='source'||a==='cache')openDetail(id,a==='cache');
 if(a==='close-detail')closeDetail();
 if(a==='create')S.tab==='storage'?openRoot():openWizard();
 if(a==='root'&&!isReadOnly())openRoot(id);
 if(a==='edit')openWizard(id);
 if(a==='budget')openBudget();
 if(a==='pause'){
  const x=sources.find(s=>s.id===id);x.state=x.state==='paused'?'following':'paused';x.lag=x.state==='paused'?'手动暂停':'已追上';
  renderData();if($('#detail').open)openDetail(id);setNotice(`示例：${x.name}${x.state==='paused'?'已暂停':'已继续'}。未向服务端提交。`);
 }
 if(a==='clear'){S.search='';S.filter='all';$('#search').value='';render();$('#search').focus();}
 if(a==='refresh'||a==='retry'){if(a==='retry'){S.scenario='normal';$('#scenario').value='normal';render();}setNotice('示例列表已刷新；未请求采集服务。');}
 if(a==='dismiss'){S.notice='';renderBanner();}
 if(a==='cancel')cancelEditor();
 if(a==='keep-edit')drawWizard();
 if(a==='discard'){S.draft=null;$('#editor').close();}
 if(a==='next'){if(!validate())return;capture();S.step++;drawWizard();$('#editor').querySelector('input,button')?.focus();}
 if(a==='back'){capture();S.step--;drawWizard();}
 if(a==='add-map'){capture();S.draft.maps++;drawWizard();}
 if(a==='remove-map'){capture();S.draft.maps--;drawWizard();}
 if(a==='probe'){S.probe=!S.probeError;drawWizard();$('.cc-live').textContent=S.probe?'示例预检通过':'示例预检失败，主机密钥不匹配';}
 if(a==='save-source'){
  if(S.probe!==true)return;const d=S.draft;const i=sources.findIndex(x=>x.id===d.id);const x={...d,id:d.id||'source-'+(sources.length+1),state:'ready',raw:'0 B',files:0,coverage:null,lag:'尚未开始'};
  if(i>=0)sources[i]={...sources[i],...d};else sources.push(x);
  S.draft=null;$('#editor').close();S.search='';$('#search').value='';S.filter='all';render();setNotice('示例配置已更新，仅保存在当前预览内存中。');
 }
 if(a==='save-root'){if(!validate())return;const d=$('#editor');const existing=roots.find(r=>r.id===d.dataset.root);const r={id:existing?.id||'root-'+roots.length,name:$('#f-root-name').value,path:$('#f-path').value,index:$('#f-index').value,state:'online',free:existing?.free??'未知',maps:existing?.maps??0};if(existing)Object.assign(existing,r);else roots.push(r);d.close();render();setNotice('示例存储位置已更新；未执行实际目录预检或写入。');}
 if(a==='save-budget'){if(!validate())return;$('#editor').close();setNotice('示例资源设置已确认；服务端预算未改变。');}
 if(a==='rebuild'){closeDetail();modal('重建规则结果',`<p>重建 <strong>${E(sources.find(s=>s.id===id).name)}</strong> 的解析结果。</p><p class="cc-field-note">保留归档原文，完成前继续使用旧结果。</p>`,'模拟重建','confirm-rebuild');}
 if(a==='confirm-rebuild'){$('#editor').close();setNotice('已演示重建操作；未创建服务端任务。');}
 if(a==='export'){const u=URL.createObjectURL(new Blob([JSON.stringify(events,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=u;link.download='logbench-preview-events.json';link.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}
});
$('#search').addEventListener('input',e=>{S.search=e.target.value;renderData();});
$('#filter').addEventListener('change',e=>{S.filter=e.target.value;renderData();});
$('#scenario').addEventListener('change',e=>{S.scenario=e.target.value;S.notice='';render();});
$('#editor').addEventListener('change',e=>{
 if(e.target.id==='probe-error'){S.probeError=e.target.checked;S.probe=null;drawWizard();}
 else if(e.target.name==='protocol'){capture();S.draft.port={SFTP:'22',SMB:'445',HTTPS:'443',SCP:'22'}[S.draft.protocol];S.draft.mode=S.draft.protocol==='SCP'?'稳定文件快照':S.draft.protocol==='HTTPS'?'定时同步':'持续增量';S.probe=null;drawWizard();}
 else if(e.target.name==='history'){capture();drawWizard();}
 else if(S.draft)S.probe=null;
});
$('#editor').addEventListener('submit',e=>{e.preventDefault();$('#editor [data-cc=next]')?.click();});
$('#editor').addEventListener('cancel',e=>{e.preventDefault();cancelEditor();});
$('#editor').addEventListener('close',()=>{S.draft=null;$('#create').focus();});
$('#detail').addEventListener('cancel',e=>{e.preventDefault();closeDetail();});
$('.cc-tabs').addEventListener('keydown',e=>{
 const keys=['ArrowLeft','ArrowRight','Home','End'];if(!keys.includes(e.key))return;e.preventDefault();const i=tabs.findIndex(t=>t[0]===S.tab);const next=e.key==='Home'?0:e.key==='End'?3:(i+(e.key==='ArrowRight'?1:3))%4;$('#tab-'+tabs[next][0]).click();
});
icons();render();
