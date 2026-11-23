/* Declarative prototype rule engine. Values stay lossless strings. No eval. */
function LB4EngineFactory(){
'use strict';
const clone=x=>JSON.parse(JSON.stringify(x));
// Traceback is a view over lossless source text, never an evaluated Python object.
function parseTraceback(r){
 const source=(r.traceLines || [{text:r.message,line:r.line},...(r.body||[]).map((text,i)=>({text,line:r.bodyLineNos?.[i]??r.line+i+1}))]);
 const first=source.findIndex(x=>/Traceback \(most recent call last\):/.test(x.text));
 if(first<0)return null;
 const rows=source.slice(first),group=rows.some(x=>/Exception Group Traceback|\b(?:Base)?ExceptionGroup:/.test(x.text));
 const sections=[];let section=null,frame=null,relation=null,groupException=null;
 for(const item of rows){
  const text=item.text.replace(/\x1b\[[0-9;]*m/g,'');
  if(/The above exception was the direct cause/.test(text)){relation='cause';frame=null;continue;}
  if(/During handling of the above exception/.test(text)){relation='context';frame=null;continue;}
  if(/Traceback \(most recent call last\):/.test(text)){section={relation,frames:[],exception:null,extra:[],line:item.line};sections.push(section);relation=null;frame=null;continue;}
  if(!section)continue;
  const match=text.match(/^\s*(?:\|\s*)?File ["'](.+)["'], line (\d+)(?:, in (.*))?$/);
  if(match){frame={path:match[1],line:Number(match[2]),fn:match[3]||'',sourceLine:item.line,code:[]};section.frames.push(frame);continue;}
  const exc=text.match(/^\s*(?:\|\s*)?([A-Za-z_][\w.]*):\s*(.*)$/)||text.match(/^((?:[\w.]+(?:Error|Exception|Interrupt|Exit|Warning|Group)|StopIteration|StopAsyncIteration))$/);
  // Unindented exception lines are distinguished from indented source code.
  if(exc && (group || !/^\s/.test(text))){section.exception={type:exc[1],message:exc[2]||'',sourceLine:item.line};if(/(?:Base)?ExceptionGroup$/.test(exc[1])&&!groupException)groupException=section.exception;frame=null;continue;}
  if(frame && /^\s+\S/.test(text))frame.code.push({text:text.replace(/^ {4}/,''),line:item.line});
  else if(text.trim())section.extra.push(item);
 }
 const frames=sections.flatMap(s=>s.frames),exception=groupException||sections.filter(s=>s.exception).at(-1)?.exception||null;
 return {sections,frames,exception,group,incomplete:!exception,rawLines:rows.map(x=>x.text),startLine:rows[0].line,endLine:rows.at(-1).line};
}
const markerShapes=['circle','diamond','square','triangle','star','flag','hexagon','pin','bolt','cross'];
const markerColors=['#2563eb','#0891b2','#047857','#15803d','#a16207','#c2410c','#b91c1c','#be185d','#7e22ce','#4338ca','#475569','#1f2937'];
const ops=['equals','notEquals','contains','notContains','startsWith','endsWith','regex','nonzero','gt','gte','lt','lte','exists','missing'];
const fieldNames=['level','logger','message','body','raw','fn','src','pid','tid','phase','traceback.type'];
function conditionLabel(q){const names={equals:'等于',notEquals:'不等于',contains:'包含',notContains:'不包含',startsWith:'开头为',endsWith:'结尾为',regex:'匹配正则',nonzero:'非零',gt:'>',gte:'≥',lt:'<',lte:'≤',exists:'存在',missing:'不存在'};return `${q.field} ${names[q.op]||q.op}${['nonzero','exists','missing'].includes(q.op)?'':' '+String(q.value??'')}`;}
function normalizeNode(q,i=0){return {...q,id:q.id||'node-'+i,name:q.name||'未命名节点',type:q.type||'custom',combine:q.combine==='any'?'any':'all',conditions:q.conditions?clone(q.conditions):[{field:'raw',op:'regex',value:q.pattern||'',caseSensitive:false}],shape:q.shape||({start:'circle',end:'square',error:'diamond',warning:'triangle',custom:'star'}[q.type]||'star'),color:q.color||({start:'#047857',end:'#475569',error:'#b91c1c',warning:'#a16207',custom:'#7e22ce'}[q.type]||'#7e22ce')};}
function nodeConditionValues(r,field){
 if(field==='traceback.type')return r.traceback?.sections.filter(x=>x.exception).map(x=>({value:x.exception.type,line:x.exception.sourceLine,source:'traceback'}))||[];
 if(field.startsWith('fields.'))return (r.fieldMeta||[]).filter(x=>x.key===field.slice(7)).map(x=>({value:x.value,line:x.line,source:x.source,extractor:x.rule,extractorId:x.ruleId}));
 const value=field==='body'?(r.body||[]).join('\n'):field==='raw'?(r.raw||[]).join('\n'):r[field];
 if(value===undefined||value===null||value==='')return [];
 return [{value:String(value),line:field==='body'?(r.bodyLineNos?.[0]??r.line+1):r.line,source:field}];
}
function testCondition(r,q){const vs=nodeConditionValues(r,q.field),c=String(q.value??''),fold=v=>q.caseSensitive?String(v):String(v).toLocaleLowerCase();
 if(q.op==='missing')return {matched:!vs.length,evidence:!vs.length?[{field:q.field,value:'（字段不存在）',line:r.line,source:q.field}]:[]};
 const rx=q.op==='regex'?compile(c,'条件',q.caseSensitive?'':'i'):null;
 const hits=vs.filter(v=>{const text=clean(v.value);switch(q.op){case'equals':return fold(text)===fold(c);case'notEquals':return fold(text)!==fold(c);case'contains':return fold(text).includes(fold(c));case'notContains':return !fold(text).includes(fold(c));case'startsWith':return fold(text).startsWith(fold(c));case'endsWith':return fold(text).endsWith(fold(c));case'exists':return true;default:return match(text,q,rx);}});
 return {matched:!!hits.length,evidence:hits.map(x=>({...x,field:q.field,condition:conditionLabel(q)}))};
}

function defaults(){return {extractors:[{id:'kv',name:'通用键值',enabled:true,source:'both',pattern:'(?:^|\\b)(?<key>[A-Za-z_][A-Za-z0-9_.]*)\\s*[:：=]\\s*(?<value>[^,，;\\n]+)',key:'',group:'value',type:'auto',unit:''}],anomalies:[{id:'raw-error',name:'原始错误等级',enabled:true,field:'level',op:'regex',value:'^(ERROR|FATAL)$',severity:'error'},{id:'raw-warn',name:'原始告警等级',enabled:true,field:'level',op:'regex',value:'^(WARNING|WARN)$',severity:'warning'},{id:'rpc',name:'RPC 调用失败',enabled:true,field:'fields.rpc_success',op:'equals',value:'false',severity:'error'},{id:'code',name:'非零错误码',enabled:true,field:'fields.err_code',op:'nonzero',value:'',severity:'error'}],nodes:[{name:'模块开始',type:'start',enabled:true,pattern:'started|initialization started'},{name:'模块结束',type:'end',enabled:true,pattern:'completed|会话结束'}]};}
function compile(pattern,name,flags='i'){if(typeof pattern!=='string'||pattern.length>2000)throw Error(name+'：表达式应为 1–2000 字符');try{return new RegExp(pattern,flags)}catch(e){throw Error(name+'：正则表达式无效（'+e.message+'）')}}
function validate(p){for(const q of p.extractors||[])if(q.enabled!==false){if(!q.name?.trim())throw Error('提取规则需要名称');compile(q.pattern,q.name,'gim');if(!['message','body','both','raw'].includes(q.source))throw Error(q.name+'：请选择提取来源');}
for(const q of p.anomalies||[])if(q.enabled!==false){if(!q.name?.trim())throw Error('异常规则需要名称');if(q.op==='regex')compile(q.value,q.name);if(['gt','gte','lt','lte'].includes(q.op)&&!Number.isFinite(Number(q.value)))throw Error(q.name+'：阈值必须是有限数字');}
for(const [i,old] of (p.nodes||[]).entries())if(old.enabled!==false){const q=normalizeNode(old,i);if(!old.name?.trim())throw Error('时间线规则需要名称');if(!q.conditions.length||q.conditions.length>12)throw Error(q.name+'：需要 1–12 个匹配条件');if(!markerShapes.includes(q.shape)||!/^#[0-9a-f]{6}$/i.test(q.color))throw Error(q.name+'：形状或颜色无效');for(const c of q.conditions){if(!ops.includes(c.op))throw Error(q.name+'：不支持的条件');if(!fieldNames.includes(c.field)&&!/^fields\.[A-Za-z_][\w.]*$/.test(c.field))throw Error(q.name+'：字段名称无效');if(c.op==='regex'){if(!c.value)throw Error(q.name+'：正则不能为空');compile(c.value,q.name,c.caseSensitive?'':'i');}if(['gt','gte','lt','lte'].includes(c.op)&&!Number.isFinite(Number(c.value)))throw Error(q.name+'：阈值须为有限数字');}}return true;}
function clean(v){return String(v??'').trim().replace(/[;,，]$/,'').replace(/(0x[\da-f]+)\.$/i,'$1');}
function infer(v,type){if(type&&type!=='auto')return type;if(/^(true|false)$/i.test(v))return'boolean';if(/^[-+]?0x[\da-f]+$/i.test(v))return'hex';if(/^[-+]?\d+(\.\d+)?(e[-+]?\d+)?$/i.test(v))return'number';return'string';}
function values(r,field){if(field.startsWith('fields.'))return r.fields.filter(k=>k[0]===field.slice(7)).map(k=>k[1]);return [field==='body'?r.body.join('\n'):field==='raw'?r.raw.join('\n'):String(r[field]??'')];}
function compareNumber(v,c,op){v=clean(v);c=clean(c);let x,y;if(/^[-+]?\d+$/.test(v)&&/^[-+]?\d+$/.test(c)){x=BigInt(v);y=BigInt(c)}else{x=Number(v);y=Number(c);if(!Number.isFinite(x)||!Number.isFinite(y))return false}return op==='gt'?x>y:op==='gte'?x>=y:op==='lt'?x<y:x<=y;}
function match(v,q,rx){v=clean(v);const c=String(q.value??'').trim();switch(q.op){case'equals':return v.toLowerCase()===c.toLowerCase();case'contains':return v.toLowerCase().includes(c.toLowerCase());case'regex':return rx.test(v);case'nonzero':try{if(/^(?:0x[\da-f]+|[-+]?\d+)$/i.test(v))return BigInt(v)!==0n;return /^[-+]?\d+\.\d+$/.test(v)&&Number(v)!==0}catch{return false}case'exists':return v!=='';case'gt':case'gte':case'lt':case'lte':return compareNumber(v,c,q.op);default:return false;}}
function applyProfile(records,profile){validate(profile);const ex=(profile.extractors||[]).filter(q=>q.enabled!==false).map(q=>({...q,rx:compile(q.pattern,q.name,'gim')}));const ans=(profile.anomalies||[]).filter(q=>q.enabled!==false).map(q=>({...q,rx:q.op==='regex'?compile(q.value,q.name):null}));const ns=(profile.nodes||[]).map(normalizeNode).filter(q=>q.enabled!==false);
return records.map(src=>{const r={...src,fields:[],fieldMeta:[],issue:null,issueSeverity:null,issueMatches:[],nodeMatches:[],appliedRule:{id:profile.id||'default',name:profile.name||'默认配置',version:profile.version||'1.0'},traceback:parseTraceback(src),event:null};
for(const q of ex){let inputs=q.source==='raw'?[{text:r.raw.join('\n'),source:'raw',line:r.line}]:[...(q.source!=='body'?[{text:r.message,source:'message',line:r.line}]:[]),...(q.source!=='message'?(r.body||[]).map((text,i)=>({text,source:'body',line:r.bodyLineNos?.[i]??r.line+i+1})):[])];
for(const input of inputs){q.rx.lastIndex=0;let m,count=0;while((m=q.rx.exec(input.text))!==null){if(++count>512)break;const key=q.key?.trim()||m.groups?.key||m[1];const value=m.groups?.[q.group||'value']??m[Number(q.group)||2];if(key&&value!==undefined){const v=clean(value);r.fields.push([String(key),v]);r.fieldMeta.push({key:String(key),value:v,type:infer(v,q.type),unit:q.unit||'',rule:q.name,ruleId:q.id||q.name,pattern:q.pattern,source:input.source,line:input.line,index:m.index});}if(m[0].length===0)q.rx.lastIndex++;}}
}
for(const q of ans){const h=testCondition(r,q);if(h.matched)r.issueMatches.push({id:q.id||q.name,name:q.name,severity:q.severity||'error',field:q.field,op:q.op,compare:q.value||'',value:h.evidence[0]?.value,condition:conditionLabel(q),evidence:h.evidence,category:'errors'});}
r.issueMatches.sort((a,b)=>(a.severity==='error'?0:1)-(b.severity==='error'?0:1));if(r.issueMatches.length){r.issue=r.issueMatches.map(q=>q.name).join(' · ');r.issueSeverity=r.issueMatches[0].severity;r.event={type:r.issueSeverity,label:(r.originalEvent&&['原始错误等级','原始告警等级'].includes(r.issueMatches[0].name))?r.originalEvent.label:r.issueMatches[0].name,phase:r.phaseId};}
for(const q of ns){const results=q.conditions.map(c=>testCondition(r,c));if(q.combine==='all'?results.every(x=>x.matched):results.some(x=>x.matched)){r.nodeMatches.push({id:q.id,name:q.name,type:q.type,shape:q.shape,color:q.color,combine:q.combine,conditions:clone(q.conditions),evidence:results.flatMap(x=>x.evidence),category:'nodes'});}}
if(r.nodeMatches.length){const q=r.nodeMatches[0];r.event={type:q.type,label:q.name,phase:r.phaseId,shape:q.shape,color:q.color,ruleId:q.id};}
if(r.traceback)r.kind='traceback';else if(r.body?.length&&r.fields.length>1)r.kind='kv';else if(r.kind==='kv'||r.kind==='traceback')r.kind='text';return r;});}
function isWithin(path,parent){return path===parent||(parent.endsWith(':')?path.startsWith(parent):path.startsWith(parent.replace(/\/$/,'')+'/'));}
function resolveProfile(f,profiles,folders={},bindings={}){const valid=id=>profiles.find(p=>p.id===id&&!p.deleted);let p=valid(bindings[f.id]);if(p)return{profile:p,source:'文件单独指定',origin:f.name};const entries=Object.entries(folders).filter(([k,id])=>k.startsWith(f.source+':')&&isWithin(f.path,k.slice(f.source.length+1))&&valid(id)).sort((a,b)=>b[0].length-a[0].length);if(entries.length)return{profile:valid(entries[0][1]),source:'继承目录',origin:entries[0][0].slice(f.source.length+1)};return{profile:profiles.find(p=>!p.deleted)||profiles[0],source:'工作区默认',origin:'默认规则'};}
function filterTime(rows,a,b){return rows.filter(r=>r.key&&(!a||r.key>=a)&&(!b||r.key<=b));}
function align(fs,a,b){const map=new Map();for(const f of fs)for(const r of filterTime(f.records,a,b)){if(!map.has(r.key))map.set(r.key,{key:r.key,cells:Object.fromEntries(fs.map(x=>[x.id,[]]))});map.get(r.key).cells[f.id].push(r);}return[...map.values()].sort((x,y)=>x.key.localeCompare(y.key));}
class Heights{constructor(vals){this.values=Float64Array.from(vals);this.tree=new Float64Array(vals.length+1);for(let i=0;i<vals.length;i++){let j=i+1;this.tree[j]+=vals[i];const p=j+(j&-j);if(p<this.tree.length)this.tree[p]+=this.tree[j];}}sum(n){let s=0;for(let i=Math.min(n,this.values.length);i>0;i-=i&-i)s+=this.tree[i];return s;}get total(){return this.sum(this.values.length);}set(i,v){const d=v-this.values[i];this.values[i]=v;for(let j=i+1;j<this.tree.length;j+=j&-j)this.tree[j]+=d;}indexAt(offset){let i=0,s=0,bit=1;while(bit<this.tree.length)bit*=2;for(bit>>=1;bit;bit>>=1){let j=i+bit;if(j<this.tree.length&&s+this.tree[j]<=offset){i=j;s+=this.tree[j];}}return Math.min(i,Math.max(0,this.values.length-1));}}
return{parseTraceback,normalizeNode,markerShapes,markerColors,conditionLabel,testCondition,fieldNames,ops,defaults,validate,applyProfile,resolveProfile,isWithin,filterTime,align,Heights,clone};}
if(typeof module!=='undefined')module.exports=LB4EngineFactory();

/* LogBench R3: isolated, offline design-review prototype. Server data is synthetic. */
(()=>{'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
const E=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths={menu:'M4 5h16M4 12h16M4 19h16',logo:'M4 16V8m5 10V5m5 10V9m5 7V3',logs:'M4 4h16v16H4zM8 8l3 3-3 3m5 0h3',file:'M14 3H5v18h14V8zM14 3v5h5M8 12h8m-8 4h6',folder:'M3 6h7l2 2h9v12H3zM3 6V4h7l2 2h8',sliders:'M4 6h5m4 0h7M4 12h10m4 0h2M4 18h2m4 0h10M9 3v6m5 0v6M6 15v6',search:'M10.5 18a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15Zm5.5-2 5 5',chevron:'m9 5 7 7-7 7',down:'m6 9 6 6 6-6',left:'m15 5-7 7 7 7',up:'m6 15 6-6 6 6',close:'m6 6 12 12M18 6 6 18',plus:'M12 4v16M4 12h16',download:'M12 3v12m-5-5 5 5 5-5M4 15v5h16v-5',upload:'M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4',clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4v5l3 2',calendar:'M5 5h14v16H5zM8 3v4m8-4v4M5 10h14m-10 4h2m2 0h2m-6 3h2',filter:'M4 5h16l-6 7v7l-4 2v-9z',layers:'m12 3 10 5-10 5L2 8zm-10 9 10 5 10-5M2 16l10 5 10-5',display:'M4 4h16v12H4zM8 21h8m-4-5v5',more:'M5 12h.01M12 12h.01M19 12h.01',star:'m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z',bookmark:'M6 3h12v18l-6-4-6 4z',warning:'m12 3 10 18H2zm0 5v6m0 3h.01',error:'M12 3 3 12l9 9 9-9zM12 7v6m0 3h.01',check:'m5 12 4 4L19 6',circlecheck:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-4 9 3 3 5-6',info:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 8v6m0-10h.01',lock:'M6 10h12v11H6zM8 10V7a4 4 0 0 1 8 0v3m-4 4v3',user:'M8 7a4 4 0 1 0 8 0 4 4 0 0 0-8 0ZM4 21v-3a8 8 0 0 1 16 0v3',shield:'m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6',settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',timeline:'M6 3v18M6 6h12M6 12h9M6 18h12M17 3v6m-3 0v6m3 0v6',collapse:'m5 7 7 5 7-5m-14 10 7-5 7 5',expand:'m5 8 7-5 7 5m-14 8 7 5 7-5',copy:'M9 8h11v13H9zM15 8V3H4v13h5',code:'m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18',table:'M3 4h18v16H3zM3 9h18M9 4v16',pin:'m9 3 6 0-1 7 4 3v2H6v-2l4-3zM12 15v7',link:'m9 15 6-6M7 13l-2 2a3 3 0 0 0 4 4l3-3m0-8 3-3a3 3 0 0 1 4 4l-2 2',trash:'M4 6h16M9 3h6l1 3M6 6l1 15h10l1-15M10 10v7m4-7v7',restore:'M4 10a8 8 0 1 1 1 8M4 4v6h6',server:'M3 4h18v6H3zm0 10h18v6H3zM7 7h.01M7 17h.01',laptop:'M5 4h14v12H5zM2 20h20l-3-4H5z',eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z',arrow:'M4 12h16m-6-6 6 6-6 6',refresh:'M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5m-4 8a8 8 0 0 0 14 3l3-3m0 5v-5h-5',save:'M4 3h13l4 4v14H4zM8 3v6h9V3M8 21v-8h9v8',play:'m7 4 13 8-13 8z',external:'M14 3h7v7m0-7L10 14M11 4H4v16h16v-7',wrap:'M3 5h18M3 10h14a4 4 0 0 1 0 8h-5m3-3-3 3 3 3M3 16h4',book:'M4 4h7a3 3 0 0 1 3 3v14a3 3 0 0 0-3-3H4zm10 3a3 3 0 0 1 3-3h4v14h-4a3 3 0 0 0-3 3',phase:'M4 4h7v7H4zm9 9h7v7h-7zM7 11v6h6',edit:'m15 4 5 5M4 20l4-1L21 6l-4-4L4 15z',clear:'M3 12h18m-9-9v18',cloud:'M7 18a5 5 0 1 1 1-10 6 6 0 0 1 11-1 5 5 0 0 1 0 11z'};
const I=(name,cls='')=>`<svg class="i ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.info}"/></svg>`;
const B=(label,act,ico,cls='',extra='')=>`<button class="btn ${cls}" data-act="${act}" ${extra}>${ico?I(ico):''}${label?`<span class="button-label">${label}</span>`:''}</button>`;
const fmt=n=>new Intl.NumberFormat('zh-CN').format(n), bytes=n=>n<1024?`${n} B`:n<1048576?`${(n/1024).toFixed(1)} KB`:`${(n/1048576).toFixed(1)} MB`;
const sessionStore=new Map();
function load(k,d){if(sessionStore.has(k))return sessionStore.get(k);try{const raw=localStorage.getItem('logbench-r6-'+k)??localStorage.getItem('logbench-r6-preview-r4-'+k);const value=raw===null?d:JSON.parse(raw)??d;return Array.isArray(d)&&!Array.isArray(value)?d:typeof d==='string'&&typeof value!=='string'?d:typeof d==='boolean'&&typeof value!=='boolean'?d:value;}catch{return d;}}
function save(k,v){sessionStore.set(k,JSON.parse(JSON.stringify(v)));try{localStorage.setItem('logbench-r6-'+k,JSON.stringify(v));}catch(e){if(typeof storageIssue==='function')storageIssue('设置暂未确认落盘，请查看本机存储状态。');}if(typeof scheduleStateSave==='function')scheduleStateSave();} 
const norm=t=>{const m=String(t||'').replace('T',' ').match(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?$/);return m?`${m[1]} ${m[2]}.${(m[3]||'').padEnd(9,'0')}`:null};
const short=(t,p='sec')=>{if(!t)return'未设置';const s=t.split(' ')[1]||t;return p==='raw'?s:p==='ms'?s.slice(0,12):s.slice(0,8)};
const duration=(a,b)=>{const n=Math.max(0,(new Date(b.slice(0,19).replace(' ','T'))-new Date(a.slice(0,19).replace(' ','T')))/1000);return n>=60?`${Math.floor(n/60)}分${Math.round(n%60)}秒`:`${Math.round(n)}秒`};
const FIELDS=[['burstEuvEnergyMean','9.414220809936523'],['burstEuvEnergy3sigma','0.21338501572608948'],['burstPpEnergyMean','4.501'],['burstDropletCount','420'],['burstOffsetX','-1.5'],['isValid','true'],['burstEuvEnergyMin','9.101'],['burstEuvEnergyMax','9.725'],['burstMpEnergyMean','86.31'],['burstMpEnergy3sigma','0.821'],['burstStartTime','1234567890123456789'],['burstEndTime','1234567890173456789'],['burstOffsetY','2.5'],['euvConversionEfficiency','0.050'],['euvPulseWidth','0.00012'],['euvPositionX','0.11'],['euvPositionY','-0.13'],['burstPpEnergy3sigma','0.023']];
const PHASES=[{id:'boot',name:'初始化',from:0,to:22},{id:'recipe',name:'配方准备',from:23,to:48},{id:'loop',name:'闭环调整',from:49,to:142},{id:'report',name:'结果上报',from:143,to:167}];
function makeDemo(id='f1',variant=0){let line=1;const recs=[];for(let i=0;i<168;i++){const sec=11*3600+39*60+15+Math.round(i*654/167);const time=`${String(Math.floor(sec/3600)).padStart(2,'0')}:${String(Math.floor(sec%3600/60)).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`;const ts=`2026-10-26 ${time}.${String(39765329+i*271).padStart(8,'0')}`;const phase=PHASES.find(p=>i>=p.from&&i<=p.to);let level='EVENT',src='algorithm.py',srcLine=1228,fn='get_setpoint',msg=`get current PPe: ${(4.50+(i%9)*.01).toFixed(2)}mJ`,body=[],fields=[],event=null,kind='text',issue=null;
if(i%4===1){fn='set_setpoint_now';srcLine=1241;msg='PARAM_A call success!! name = GLOBAL_DELAY, value = 3000ns'}
if(i%4===2){src='acquisition.py';srcLine=188;fn='get_frame';msg=`frame_id: ${12004+i}, exposure_us: 100, status: ready`}
if(i%4===3){srcLine=947;fn='iterate';msg=`iteration = ${i}, energy = ${(9.3+(i%7)*.02).toFixed(4)}, converged = false`}
if(i%4===0&&i>30){fn='get_setpoint';msg=`get current DGSS_y: ${(200.63+(i%6)*.02).toFixed(2)}um`}
if([5,32,56,88,114,135,159].includes(i)){fn='get_burst_data_info';srcLine=1284;fields=FIELDS.map(x=>[...x]);fields[0][1]=String(9.414220809936523+i*.00011);msg=`${fields[0][0]}: ${fields[0][1]}`;body=fields.slice(1).map(([k,v])=>`${k}: ${v}`);kind='kv'}
if(i===0){src='bootstrap.py';srcLine=46;fn='initialize';msg='Module initialization started. config_path: /opt/APP-A/config';event={type:'start',label:'模块初始化',phase:phase.id}}
if(i===11){level='WARNING';msg='PPe deviation exceeds threshold: actual = 4.82mJ, expected = 4.50mJ';event={type:'warning',label:'能量偏差超限',phase:phase.id};issue='原始 WARNING 等级'}
if(i===12){level='ERROR';src='device_io.py';srcLine=241;fn='wait_response';msg='Timeout waiting for device response: timeout = 3000ms, attempt = 1';event={type:'error',label:'设备响应超时',phase:phase.id};issue='原始 ERROR 等级'}
if(i===13||i===91){src='rpc_client.py';srcLine=208;fn='report_event';msg='Finish to report event, rpc_success: False, err_code: 0x8001.';issue='RPC 返回失败 · rpc_success = False';event={type:'error',label:'RPC 返回失败',phase:phase.id}}
if(i===14){src='device_io.py';srcLine=255;fn='retry';msg='Retry scheduled: attempt = 2, backoff = 200ms'}
if(i===22){src='bootstrap.py';srcLine=78;fn='initialize';msg='Module initialization completed. status: ready';event={type:'end',label:'初始化完成',phase:phase.id}}
if(i===23){src='recipe.py';srcLine=140;fn='set_recipe';msg="set recipe：{'mp2scam_delay': 100.0, 'GLOBAL_DELAY': 3000, 'mode': 'auto'}";event={type:'start',label:'开始加载配方',phase:phase.id}}
if(i===37){src='recipe.py';srcLine=172;msg="axis_list: ['EPD', 'Scam', 'Pcam']";fn='prepare_axes'}
if(i===48){src='recipe.py';srcLine=208;fn='apply_recipe';msg='Recipe applied. revision: R12, parameters: 42';event={type:'end',label:'配方应用完成',phase:phase.id}}
if(i===49){fn='optimize';srcLine=822;msg='Optimization module started. target: EUV, iterations: 24';event={type:'start',label:'开始闭环调整',phase:phase.id}}
if(i===72){level='WARNING';msg='Energy variance approaching limit: 3sigma = 0.281, threshold = 0.300';event={type:'warning',label:'能量波动接近阈值',phase:phase.id};issue='原始 WARNING 等级'}
if(i===90){level='ERROR';fn='validate_result';srcLine=1420;msg='Calibration failed: axis = DGSS_y, deviation = 12.85um';body=['Traceback (most recent call last):','  File "algorithm.py", line 1420, in validate_result','    raise CalibrationError("axis deviation out of range")','CalibrationError: DGSS_y deviation = 12.85um'];kind='stack';issue='原始 ERROR 等级';event={type:'error',label:'轴向校准失败',phase:phase.id}}
if(i===142){fn='optimize';srcLine=1520;msg='Optimization module completed. converged: true, iteration: 24';event={type:'end',label:'闭环调整结束',phase:phase.id}}
if(i===143){src='report.py';srcLine=90;fn='begin_report';msg='Report module started. destination: local';event={type:'start',label:'开始结果上报',phase:phase.id}}
if(i===151){fn='choose_index';srcLine=1154;msg='**********best index mask = 0     False';body=Array.from({length:7},(_,j)=>`${j+1}     ${j===5?'True':'False'}`);kind='mask'}
if(i===156){src='report.py';srcLine=158;fn='send_report';level='ERROR';msg='Report delivery failed: err_code = 0x0204, retryable = true';issue='原始 ERROR 等级';event={type:'error',label:'上报失败，可重试',phase:phase.id}}
if(i===167){src='report.py';srcLine=174;fn='send_report';msg='Report module completed. rpc_success: True, err_code: 0x0.';event={type:'end',label:'会话结束',phase:phase.id}}
if(variant){src=variant===1?'device_io.py':src;fn=variant===1?'poll_status':fn;msg=variant===1?`Device status: channel = ${i%4}, latency = ${7+i%12}ms, ready = true`:msg;level='EVENT';body=[];fields=[];kind='text';issue=null;event=null}
const logger=variant===1?'DEVICE-IO':'APP-A',header=`[${ts}] [${level}] [${logger}] [6169] [41413] [${src}:${srcLine}] [100] [0:0:0] ${fn}() <CPQPSGX> ${msg}`;const raw=[header,...body];recs.push({id:`${id}-${i}`,fileId:id,ordinal:i,line,lineEnd:line+body.length,ts,key:norm(ts),level,logger,src,srcLine,fn,pid:'6169',tid:'41413',message:msg,body,fields,kind,issue,event,phase:phase.name,phaseId:phase.id,raw});line+=raw.length}return recs}
let files=[{id:'f1',name:'cpqpsgx_20261026.log',path:'data/设备 A/2026-10-26',source:'server',profile:'CPQPSGX 标准',records:makeDemo('f1'),modified:'10-26 11:50',favorite:true},{id:'f2',name:'device_io.log',path:'data/设备 A/2026-10-26',source:'server',profile:'通用方括号',records:makeDemo('f2',1),modified:'10-26 11:50',favorite:false}];
['startup.log','rpc_trace.log','calibration.log','report.log','maintenance.log','device_b.log','cpqpsgx_20261025.log','system.log','validation.log','baseline.log'].forEach((name,i)=>{const id='lib'+i;files.push({id,name,path:i<4?'data/设备 A/2026-10-26':i<7?'data/设备 A/2026-10-25':'data/设备 B/2026-10-26',source:'server',profile:i%3===0?'CPQPSGX 标准':'通用方括号',records:makeDemo(id,i%2).slice(0,12+i*3),modified:'10-26 11:50',favorite:i===2})});
files.forEach(f=>{f.text=f.records.map(r=>r.raw.join('\n')).join('\n');f.size=new Blob([f.text]).size});
const baseProfiles=[{id:'cpq',name:'CPQPSGX 标准',version:'1.2',status:'共享',desc:'方括号日志 · 多行结构体 · 模块节点',format:'bracket',deleted:false},{id:'general',name:'通用方括号',version:'1.0',status:'共享',desc:'时间、等级、软件与来源字段',format:'bracket',deleted:false},{id:'device',name:'设备诊断',version:'1.1',status:'共享',desc:'设备状态 · 超时与错误码',format:'bracket',deleted:false}];
const defaultNodes=[{type:'start',name:'模块开始',pattern:'started|initialization started',enabled:true},{type:'end',name:'模块结束',pattern:'completed|会话结束',enabled:true},{type:'error',name:'错误与调用失败',pattern:'ERROR|rpc_success: False',enabled:true},{type:'warning',name:'告警节点',pattern:'WARNING|WARN',enabled:true}];
const S={view:'logs',file:'f1',opened:['f1','f2'],scope:'all',search:'',flags:{case:false,whole:false,regex:false},level:'',software:'',fn:'',module:'',queryHits:null,searching:false,searchError:'',rules:[],ruleMode:'or',ruleTab:0,page:1,pageSize:60,precision:load('precision','sec'),theme:load('theme','ink'),compact:load('compact',false),wrap:false,sidebar:true,timeline:true,timelineTab:'events',tlOnlyIssues:false,selected:null,expanded:new Set(['f1-5']),bodyModes:{},fullFields:new Set(),bookmarks:new Set(),draft:{start:null,end:null,keys:[]},applied:null,folded:new Set(),inspector:null,inspectorTab:'fields',contextSize:100,contextUnit:'lines',recents:load('ranges',[]),favoriteOnly:false,libSource:'server',folder:'',libQuery:'',libSelected:new Set(),folderOpen:true,collapsedDirs:new Set(),profiles:load('profiles',baseProfiles),profile:'cpq',ruleSection:'nodes',profileTrash:false,nodeRules:load('nodes',defaultNodes),dirty:false,testResult:null,colorOverrides:load('colors',{}),users:[{ip:'10.0.0.21',name:'研发用户',role:'管理员'},{ip:'10.0.0.22',name:'测试用户',role:'成员'}]};
const currentFile=()=>S.opened.length?(files.find(f=>f.id===S.file&&!f.deleted)||files.find(f=>S.opened.includes(f.id)&&!f.deleted)):null;
const baseRecords=()=>allBaseRecords();
const recById=id=>recordIndex.get(id);
const issueOf=r=>r.issue||null;
function filtered(){return baseRecords().filter(r=>(!S.level||(S.level==='ISSUE'?issueOf(r):r.level===S.level))&&(!S.software||r.logger===S.software)&&(!S.fn||r.fn.includes(S.fn))&&(!S.module||r.phase===S.module)&&(!S.applied||(r.key>=S.applied.start&&r.key<=S.applied.end))&&(S.scope!=='issues'||issueOf(r))&&(S.scope!=='bookmarks'||S.bookmarks.has(r.id))&&(S.scope!=='multiline'||r.body.length)&&(S.queryHits===null||S.queryHits[r.id]?.pass))}
function rangeBounds(){const b=baseRecords().filter(r=>r.key).sort((a,b)=>a.key.localeCompare(b.key));return b.length?{start:b[0].key,end:b[b.length-1].key}:{start:norm('2026-10-26 00:00:00'),end:norm('2026-10-26 23:59:59')}}
function intervals(){const b=S.applied||rangeBounds(),keys=[b.start,...S.draft.keys.filter(t=>t>b.start&&t<b.end),b.end].sort();return keys.slice(0,-1).map((a,i)=>({id:a+'|'+keys[i+1],start:a,end:keys[i+1],index:i,records:filtered().filter(r=>r.key>a&&r.key<keys[i+1])}))}
function pointLabel(r){return S.draft.start===r.key?'A':S.draft.end===r.key?'B':S.draft.keys.includes(r.key)?'K':''}
function notify(text,error=false,action=null){clearTimeout(notify.timer);$('#toasts').innerHTML=`<div class="toast ${error?'error':''}" role="status">${I(error?'warning':'circlecheck')}<span>${E(text)}</span>${action?`<button class="link" data-act="${action.act}">${E(action.label)}</button>`:''}</div>`;notify.timer=setTimeout(()=>{$('#toasts').innerHTML=''},5000)}
function preserve(){const a=document.activeElement, pos={id:a?.id,selection:a?.selectionStart,end:a?.selectionEnd,scroll:{}};$$('[data-scroll]').forEach(el=>pos.scroll[el.dataset.scroll]=[el.scrollTop,el.scrollLeft]);return pos}
function restore(pos,keep){if(keep)$$('[data-scroll]').forEach(el=>{const p=pos.scroll[el.dataset.scroll];if(p){el.scrollTop=p[0];el.scrollLeft=p[1]}});if(pos.id){const el=document.getElementById(pos.id);if(el){el.focus({preventScroll:true});try{el.setSelectionRange(pos.selection,pos.end)}catch{}}}}
function renderLegacy(keep=true){const pos=preserve();document.body.dataset.theme=S.theme;const root=$('#app');root.className=`lb-app ${!S.sidebar?'nav-collapsed':''} ${S.compact?'compact':''} ${S.wrap?'wrap-lines':''}`;root.innerHTML=`${renderHeader()}<div class="mobile-scrim" data-act="toggle-nav"></div>${renderSidebar()}<main class="main">${S.view==='logs'?renderLogWorkspace():S.view==='files'?renderFiles():S.view==='rules'?renderRules():renderAdmin()}<footer class="app-status"><span class="row">${I('lock','sm')} ${S.view==='logs'&&currentFile()?.source==='local'?'本机文件 · 未上传服务器':'服务未连接'}</span><span class="status-right"><span class="extra">${S.view==='logs'?'时区未指定 · 保留日志原始时间':'本机文件不上传'}</span><span>LogBench / R6</span></span></footer></main>`;restore(pos,keep);applyColorOverrides();}
function renderHeader(){return`<header class="lb-header">${B('','toggle-nav','menu','icon ghost','aria-label="展开或收起导航"')}<div class="brand"><span class="brandmark">${I('logo')}</span><span class="brandname">LogBench</span></div><div class="header-path"><span>分析工作区</span>${I('chevron','sm')}<span class="current">${{logs:'日志浏览',files:'文件库',rules:'规则管理',admin:'管理后台',compare:'时间对比'}[S.view]}</span></div><div class="header-right"><button class="prototype-tag" data-r6="datalink">未连接服务</button><span class="privacy-note">${I('lock','sm')} 本机文件不上传</span><button class="btn icon ghost" data-r6="backup" aria-label="备份与恢复">${I('save')}</button>${B('打开日志','import','upload','primary','id="import-button"')}<button class="avatar" data-act="identity" aria-label="查看当前身份">研</button></div></header>`}
function renderSidebarLegacy(){return`<aside class="sidebar"><nav class="nav-section" aria-label="主导航">${[['logs','logs','日志浏览'],['files','folder','文件库'],['rules','sliders','规则管理']].map(([id,ic,l])=>`<button class="nav-button ${S.view===id?'active':''}" data-act="nav" data-value="${id}" ${S.view===id?'aria-current="page"':''}>${I(ic)}<span>${l}</span>${id==='files'?`<span class="nav-counter">${files.filter(f=>!f.deleted).length}</span>`:''}</button>`).join('')}</nav><hr class="nav-divider"><div class="section-head">已打开<span class="number">${S.opened.length}</span><span class="spacer"></span>${B('','nav','folder','icon ghost sm','data-value="files" aria-label="在文件库中管理文件"')}</div><div class="sidebar-files" data-scroll="sidefiles">${S.opened.map(id=>files.find(f=>f.id===id)).filter(Boolean).map(f=>`<div class="side-file ${S.file===f.id?'active':''}" data-act="open-file" data-id="${f.id}" tabindex="0" role="button" aria-label="打开 ${E(f.name)}">${I('file')}<div class="grow"><div class="fname ellipsis" title="${E(f.name)}">${E(f.name)}</div><div class="fmeta">${f.source==='local'?'本机临时':'服务端'}<span style="margin:0 5px">·</span>${fmt(f.records.length)} 条</div></div><button class="close-file btn icon ghost sm" data-act="close-file" data-id="${f.id}" aria-label="关闭 ${E(f.name)}">${I('close','sm')}</button></div>`).join('')}</div><div class="section-head" style="margin-top:17px">阅读视角<span class="spacer"></span>${B('','save-view','plus','icon ghost sm','aria-label="保存当前阅读视角"')}</div>${[['all','logs','完整日志'],['issues','error','异常优先'],['multiline','layers','多行数据块'],['bookmarks','bookmark','我的书签']].map(([id,ic,l])=>`<button class="side-view ${S.scope===id&&S.view==='logs'?'active':''}" data-act="scope" data-value="${id}">${I(ic)}${l}${id==='bookmarks'?`<span class="spacer"></span><span class="mono">${S.bookmarks.size||''}</span>`:''}</button>`).join('')}${load('views',[]).map((x,i)=>`<button class="side-view" data-act="saved-view" data-index="${i}">${I('eye')}<span class="ellipsis">${E(x.name)}</span></button>`).join('')}<div class="sidefoot"><button class="nav-button ${S.view==='admin'?'active':''}" data-act="nav" data-value="admin">${I('settings')}管理后台</button><button class="nav-button" data-act="help">${I('book')}操作帮助</button><div class="sidefoot-meta"><span class="dot"></span>规则与文件保持原始来源</div></div></aside>`}
function renderFileTabsLegacy(){const open=S.opened.map(id=>files.find(f=>f.id===id)).filter(Boolean);return`<div class="file-tabs" role="tablist" aria-label="已打开的日志">${open.length>1?`<button class="file-tab ${S.file==='merged'?'active':''}" data-act="open-file" data-id="merged" role="tab" aria-selected="${S.file==='merged'}">${I('layers')}合并视图 <span class="muted">${open.length}</span></button>`:''}${open.map(f=>`<div class="file-tab ${S.file===f.id?'active':''}" data-act="open-file" data-id="${f.id}" role="tab" aria-selected="${S.file===f.id}" tabindex="${S.file===f.id?0:-1}">${I('file')}<span class="tab-name" title="${E(f.name)}">${E(f.name)}</span><span class="badge">${f.source==='local'?'本机':'服务端'}</span><button class="tab-close" data-act="close-file" data-id="${f.id}" aria-label="关闭 ${E(f.name)}">${I('close')}</button></div>`).join('')}${B('','nav','plus','icon ghost','data-value="files" aria-label="从文件库打开日志" style="margin:3px 6px"')}<span class="spacer"></span><div class="tab-tools"><span class="tab-date">${E(baseRecords()[0]?.ts.slice(0,10)||'')}</span>${B('','toggle-timeline','timeline',`icon ghost sm ${S.timeline?'active':''}`,'id="timeline-button" aria-label="显示或隐藏时间线" title="时间线"')}${B('','fold-blocks','collapse','icon ghost sm','aria-label="收起已展开的多行块" title="收起多行块"')}</div></div>`}
function renderLogWorkspaceLegacy(){const base=baseRecords(),rows=filtered(),issues=base.filter(issueOf).length;return`${renderFileTabs()}<section class="workspace" aria-label="日志工作台"><div class="toolbar"><div class="search-box">${I('search')}<input id="main-search" type="search" value="${E(S.search)}" placeholder="搜索正文、字段或来源…" aria-label="搜索日志"><div class="search-flags">${[['case','Aa','区分大小写'],['whole','Ab','整词匹配'],['regex','.*','正则表达式']].map(([id,l,a])=>`<button class="flag ${id==='whole'?'whole':''}" data-act="search-flag" data-value="${id}" aria-label="${a}" title="${a}" aria-pressed="${S.flags[id]}">${l}</button>`).join('')}</div></div>${B('筛选','filters','filter',(S.level||S.software||S.fn||S.module)?'active':'','id="filter-btn"')}${B(S.applied?`${short(S.applied.start)} – ${short(S.applied.end)}`:'时间范围','time-panel','calendar',S.applied?'time-active':'','id="time-button"')}${B('多规则'+(S.rules.length?` ${S.rules.length}`:''),'multi','layers',S.rules.length?'active':'','id="multi-button"')}<span class="sep"></span>${B('显示','display','display','','id="display-btn"')}${B('','export','download','icon ghost','id="export-btn" aria-label="导出筛选结果" title="导出筛选结果"')}</div>${S.searchError?`<div class="inline-error" role="alert">${I('warning')}${E(S.searchError)}<button class="link" data-act="clear-search" style="margin-left:auto">清除搜索</button></div>`:''}${renderRuleStrip()}<div class="workpanel ${!S.timeline?'hide-timeline':''} ${S.inspector?'inspecting':''}">${renderTimeline()}<div class="log-column"><div class="reader-scroll" id="log-scroll" data-scroll="logs">${renderLogTable(rows)}</div>${renderPager(rows)}</div>${S.inspector?renderInspector():''}</div></section>`}
function renderRuleStrip(){let parts='';if(S.level)parts+=`<span class="filter-chip">等级 ${E(S.level)}<button data-act="clear-level" aria-label="清除等级筛选">${I('close')}</button></span>`;if(S.software||S.fn||S.module)parts+=`<span class="filter-chip">${E([S.software,S.fn,S.module].filter(Boolean).join(' · '))}<button data-act="clear-filter" aria-label="清除字段筛选">${I('close')}</button></span>`;if(S.rules.length){parts+=`<div class="segmented" aria-label="多规则显示模式">${[['or','合并 OR'],['and','同时满足'],['tabs','分标签']].map(([id,l])=>`<button data-act="rule-mode" data-value="${id}" class="${S.ruleMode===id?'active':''}" aria-pressed="${S.ruleMode===id}">${l}</button>`).join('')}</div>`+S.rules.map((r,i)=>`<button class="match-chip c${i%3+1} ${!r.enabled?'off':''}" data-act="rule-tab" data-index="${i}" ${S.ruleMode==='tabs'?`aria-pressed="${S.ruleTab===i}"`:''}>${S.ruleMode==='tabs'&&S.ruleTab===i?I('check','sm'):''}<span class="mono">${i+1}</span>${E(r.name||r.query)}${S.ruleMode==='tabs'?` <span class="muted">${baseRecords().filter(x=>S.queryHits?.[x.id]?.matched?.includes(i)).length}</span>`:''}</button>`).join('')}
return parts?`<div class="rule-strip">${parts}<span class="spacer"></span><button class="link" data-act="clear-all-filters">清除筛选</button></div>`:''}
function renderTimelineLegacy(){const recs=baseRecords(),events=recs.filter(r=>(r.event||pointLabel(r))&&(!S.tlOnlyIssues||pointLabel(r)||['error','warning'].includes(r.event?.type))).map(r=>r.event?r:{...r,event:{type:'custom',label:pointLabel(r)==='K'?'自定义关键点':pointLabel(r)==='A'?'选定起点':'选定终点',phase:r.phaseId}}),groups=[...new Set(events.map(r=>r.phaseId||'other'))];return`<aside class="timeline ${S.mobileTimeline?'mobile-open':''}" aria-label="时间线"><div class="timeline-head">${I('timeline')}<strong>时间线</strong><span class="badge" style="font-size:10px;padding:0 4px">${events.length}</span><span class="spacer"></span>${B('','timeline-rules','sliders','icon ghost sm','aria-label="配置时间线节点规则" title="节点规则"')}</div><div class="timeline-controls"><div class="segmented">${[['events','事件'],['segments','区间']].map(([id,l])=>`<button data-act="timeline-tab" data-value="${id}" class="${S.timelineTab===id?'active':''}" aria-pressed="${S.timelineTab===id}">${l}</button>`).join('')}</div>${B('','timeline-issues','filter',`icon ghost sm ${S.tlOnlyIssues?'active':''}`,'aria-label="时间线仅显示异常节点" title="仅异常"')}</div><div class="timeline-date"><span>${recs[0]?.ts.slice(0,10)||'未载入'}</span><span>事件序列</span></div><div class="timeline-scroll" data-scroll="timeline">${S.timelineTab==='segments'?renderSegments():groups.map(g=>{const entries=events.filter(r=>(r.phaseId||'other')===g),all=recs.filter(r=>(r.phaseId||'other')===g),label=entries[0]?.phase||'日志事件';return`<section class="phase"><div class="phase-header"><strong>${E(label)}</strong><span class="phase-line"></span><span class="duration">${all.length?duration(all[0].ts,all.at(-1).ts):''}</span></div><div class="tl-nodes">${entries.map(r=>{const pt=pointLabel(r);return`<div class="tl-node ${r.event.type} ${S.selected===r.id?'current':''} ${pt?'marked':''} ${S.applied&&(r.key<S.applied.start||r.key>S.applied.end)?'outside':''}" data-node="${r.id}"><div class="tl-mark">${pt?`<span class="pin-icon ${pt==='K'?'key':''}">${pt}</span>`:`<span class="node-dot ${r.event.type}"></span>`}</div><div class="grow"><div class="tl-time-row"><button class="tl-time" data-act="point" data-id="${r.id}" title="设为起点、终点或关键点">${short(r.ts)}</button>${r.event.type==='error'?'<span class="tiny-state">错误</span>':''}</div><button class="tl-label" data-act="locate" data-id="${r.id}">${E(r.event.label)}</button>${r.event.type==='error'?`<div class="tl-note">${E(r.src)}:${r.srcLine} · L${r.line}</div>`:''}</div></div>`}).join('')}</div></section>`}).join('')||`<div class="hint" style="padding:12px">当前规则未识别到节点。<button class="link" data-act="timeline-rules">配置节点规则</button></div>`}</div>${renderRangeDraft()}<div class="timeline-foot">${I('clock')}点节点定位 · 点时间选段</div></aside>`}
function renderRangeDraft(){if(!S.draft.start&&!S.draft.end&&!S.draft.keys.length)return'';const valid=S.draft.start&&S.draft.end&&S.draft.start<=S.draft.end;return`<div class="range-draft"><div class="range-draft-head">区间选择<span class="spacer"></span><span class="badge blue" style="font-size:9px">${S.applied&&S.applied.start===S.draft.start&&S.applied.end===S.draft.end?'已应用':'待应用'}</span></div>${[['start','A'],['end','B']].map(([key,l])=>`<div class="range-bound"><span class="point-label">${l}</span>${short(S.draft[key])}${S.draft[key]?`<button class="remove" data-act="remove-point" data-value="${key}" aria-label="清除${l==='A'?'起点':'终点'}">${I('close')}</button>`:''}</div>`).join('')}${S.draft.keys.length?`<div class="key-count">${I('pin','sm')}${S.draft.keys.length} 个关键点 · 可分段折叠</div>`:''}${S.draft.start&&S.draft.end&&!valid?'<div class="hint text-danger">终点早于起点，请重新选择。</div>':''}<div class="range-actions">${B('清除','clear-time',null,'sm')}${B('应用区间','apply-time',null,'primary sm',!valid?'disabled':'')}</div></div>`}
function renderSegments(){const ints=intervals();const manual=`<div class="segment-actions">${B('全部折叠','fold-all',null,'ghost sm')}${B('全部展开','unfold-all',null,'ghost sm')}</div>${ints.map(it=>`<div class="section-interval"><button data-act="fold-interval" data-id="${E(it.id)}">${I(S.folded.has(it.id)?'chevron':'down','sm')}<span><div class="ir-title">${short(it.start)}–${short(it.end)}</div><div class="ir-meta">${it.records.length} 条记录 · ${S.folded.has(it.id)?'已折叠':'已展开'}</div></span></button></div>`).join('')}<div class="hint" style="font-size:10px;margin:12px 5px">在时间点菜单添加 K 关键点，可进一步划分区间。折叠不改变导出内容。</div>`;return S.file==='merged'?renderFileIntervals()+`<details class="manual-intervals" ${S.draft.keys.length?'open':''}><summary>关键点区间</summary>${manual}</details>`:manual;}
function tokenText(text){const rx=/(\b[A-Za-z_][A-Za-z0-9_.]*(?=\s*[:=：]))|(\b0x[0-9a-fA-F]+\b|-?\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b)|(\b(?:true|false)\b)/gi;let at=0,out='',m;while((m=rx.exec(text))){out+=E(text.slice(at,m.index));out+=`<span class="tok ${m[3]?'boolean':m[2]?'number':'key'}">${E(m[0])}</span>`;at=m.index+m[0].length}return out+E(text.slice(at))}
function highlighted(r){const spans=S.queryHits?.[r.id]?.marks||[];if(!spans.length)return tokenText(r.message);const cuts=[0,r.message.length,...spans.flatMap(s=>[s.a,s.b])].filter((n,i,a)=>a.indexOf(n)===i).sort((a,b)=>a-b);return cuts.slice(0,-1).map((a,i)=>{const b=cuts[i+1],hits=spans.filter(s=>a>=s.a&&b<=s.b),part=r.message.slice(a,b);return hits.length?`<mark class="search-highlight c${hits[0].c} ${hits.length>1?'overlap':''}" title="${E(hits.map(h=>h.name).join('、'))}">${E(part)}</mark>`:tokenText(part)}).join('')}
function renderLogTableLegacy(rows){if(!rows.length)return`<div class="empty"><span class="empty-ico">${I('search','lg')}</span><strong>${baseRecords().length?'没有匹配的日志':'尚未打开日志'}</strong><p>${baseRecords().length?'调整时间范围或搜索条件。原始文件和已保存的视角不会改变。':'打开本地日志，或从文件库选择文件。'}</p>${B(baseRecords().length?'清除筛选':'打开日志',baseRecords().length?'clear-all-filters':'import',baseRecords().length?'refresh':'upload')}</div>`;
const pages=Math.max(1,Math.ceil(rows.length/S.pageSize));S.page=Math.min(pages,Math.max(1,S.page));let shown=rows.slice((S.page-1)*S.pageSize,S.page*S.pageSize),lastPhase=shown[0]?.phase,html='',seen=new Set(),ints=intervals();for(const r of shown){const folded=ints.find(it=>S.folded.has(it.id)&&r.key>it.start&&r.key<it.end);if(folded){if(!seen.has(folded.id)){html+=`<tr class="fold-row"><td colspan="7"><button data-act="fold-interval" data-id="${E(folded.id)}">${I('chevron','sm')} ${short(folded.start)}–${short(folded.end)} · 已折叠 ${folded.records.length} 条${I('expand','sm')}</button></td></tr>`;seen.add(folded.id)}continue}
if(r.phase!==lastPhase){html+=`<tr class="phase-row"><td colspan="7">${I('phase')}${E(r.phase||'日志记录')}<span class="phase-row-meta">${r.ts.slice(0,10)} · PID ${E(r.pid)}</span></td></tr>`;lastPhase=r.phase}
const point=pointLabel(r),issue=issueOf(r),ruleIssue=issue&&!['ERROR','WARNING','WARN','FATAL'].includes(r.level);html+=`<tr class="log-row ${S.selected===r.id?'selected':''} ${issue&&!['WARNING','WARN'].includes(r.level)?'error':''}" data-record="${r.id}" data-act="select-record" data-id="${r.id}" tabindex="0" aria-selected="${S.selected===r.id}"><td class="numcol"><span class="line-cell">${r.body.length?`<button class="expander" data-act="expand-block" data-id="${r.id}" aria-label="${S.expanded.has(r.id)?'收起':'展开'} L${r.line} 多行块" aria-expanded="${S.expanded.has(r.id)}">${I(S.expanded.has(r.id)?'down':'chevron')}</button>`:''}<span>${r.line}</span></span></td><td><button class="log-time" data-act="point" data-id="${r.id}" title="${E(r.ts)} · 选择时间点">${short(r.ts,S.precision)}</button>${point?`<span class="time-bound-inline">${point}</span>`:''}</td><td><span class="level ${E(r.level)}">${['ERROR','WARNING'].includes(r.level)?I(r.level==='ERROR'?'error':'warning','sm'):'<span class="dot"></span>'}${E(r.level)}</span></td><td class="software">${E(r.logger)}</td><td><span class="source-code" title="${E(r.fn)}()">${E(r.src)}<b>:${r.srcLine}</b></span></td><td class="message-cell"><div class="message-inner"><span class="message-text" title="${E(r.message)}">${highlighted(r)}</span>${ruleIssue?`<span class="rule-alert">规则异常</span>`:''}${r.body.length?`<button class="body-counter" data-act="expand-block" data-id="${r.id}">${r.kind==='kv'?r.fields.length+' 字段':r.kind==='mask'?'索引掩码':r.raw.length+' 行'}</button>`:''}${S.queryHits?.[r.id]?.matched?.length?`<span class="match-count" title="${E(S.queryHits[r.id].matched.map(i=>S.rules[i]?.name||'筛选').join('、'))}">${S.queryHits[r.id].matched.map(i=>`<i class="c${i%3+1}">${i+1}</i>`).join('')}</span>`:''}${S.bookmarks.has(r.id)?I('bookmark','sm'):''}</div></td><td style="padding:0 3px"> <button class="row-more" data-act="inspect" data-id="${r.id}" aria-label="查看 L${r.line} 记录详情" title="记录详情">${I('more')}</button></td></tr>`;
if(r.body.length&&S.expanded.has(r.id))html+=`<tr class="block-row"><td colspan="7">${renderBody(r)}</td></tr>`}
const timeW=S.precision==='raw'?173:S.precision==='ms'?111:88;return`<table class="log-table" aria-label="日志记录"><colgroup><col style="width:48px"><col style="width:${timeW}px"><col style="width:91px"><col style="width:77px"><col style="width:151px"><col><col style="width:27px"></colgroup><thead><tr><th scope="col">行号</th><th scope="col">时间</th><th scope="col">原始等级</th><th scope="col">软件</th><th scope="col">脚本 : 行号</th><th scope="col">日志内容</th><th scope="col"><span aria-label="操作">···</span></th></tr></thead><tbody>${html}</tbody></table>`}
function renderBodyLegacy(r){const mode=S.bodyModes[r.id]||'fields',fields=S.fullFields.has(r.id)?r.fields:r.fields.slice(0,6);return`<section class="inline-block"><div class="block-head">${I(r.kind==='kv'?'table':r.kind==='mask'?'layers':'code')}<strong>${r.kind==='kv'?'BurstDataInfo':r.kind==='mask'?'Best index mask':'异常堆栈'}</strong><span>${r.kind==='kv'?r.fields.length+' 个字段':r.raw.length+' 行原文'}</span><span class="spacer"></span><button class="reader-link" data-act="body-mode" data-id="${r.id}">${mode==='raw'?'结构化查看':'查看原文'}</button><button class="reader-link" data-act="copy-record" data-id="${r.id}" aria-label="复制完整记录">${I('copy','sm')}</button></div>${mode==='raw'?`<pre class="raw-block">${E(r.raw.join('\n'))}</pre>`:r.kind==='kv'?`<div class="kv-grid">${fields.map(([k,v])=>`<div class="kv"><div class="kv-label" title="${E(k)}">${E(k)}</div><div class="kv-value ${/^(true|false)$/i.test(v)?'bool':''}" title="${E(v)}">${E(v.length>16&&/^\d+\.\d+$/.test(v)?Number(v).toPrecision(10):v)}</div></div>`).join('')}</div><div class="block-foot"><span>原文 L${r.line}–${r.lineEnd} · 完整精度已保留</span><span class="spacer"></span><button class="reader-link" data-act="full-fields" data-id="${r.id}">${S.fullFields.has(r.id)?'收起次要字段':`展开全部 ${r.fields.length} 个字段`}</button>${I('down','sm')}</div>`:r.kind==='mask'?`<div class="mask-grid">${Array.from({length:8},(_,i)=>`<span class="mask-item ${i===6?'true':''}">${i} · ${i===6?'True':'False'}</span>`).join('')}</div>`:`<pre class="raw-block">${E(r.body.join('\n'))}</pre>`}</section>`}
function renderPagerLegacy(rows){const pages=Math.max(1,Math.ceil(rows.length/S.pageSize));return`<div class="page-foot"><span>${fmt(rows.length)} 条记录</span><span>${S.selected?`已选原文 L${recById(S.selected)?.line||''}`:'按原始时间排序'}</span><div class="paging"><select id="page-size" aria-label="每页记录数">${[60,100,200].map(n=>`<option value="${n}" ${S.pageSize===n?'selected':''}>${n} 条 / 页</option>`).join('')}</select><button data-act="prev-page" aria-label="上一页" ${S.page<=1?'disabled':''}>${I('left')}</button><span class="mono">${S.page} / ${pages}</span><button data-act="next-page" aria-label="下一页" ${S.page>=pages?'disabled':''}>${I('chevron')}</button></div></div>`}
function renderInspectorLegacy(){const r=recById(S.inspector);if(!r)return'';const f=files.find(f=>f.id===r.fileId);return`<aside class="inspector" aria-label="记录详情"><div class="inspector-head">${I('file')}记录详情<span class="badge" style="font-size:10px">L${r.line}</span><span class="spacer"></span>${B('','inspect-prev','left','icon ghost sm','aria-label="上一条记录"')}${B('','inspect-next','chevron','icon ghost sm','aria-label="下一条记录"')}${B('','close-inspector','close','icon ghost sm','aria-label="关闭记录详情"')}</div><div class="inspector-tabs">${[['fields','概览'],['raw','原文'],['context','上下文']].map(([id,l])=>`<button data-act="inspector-tab" data-value="${id}" class="${S.inspectorTab===id?'active':''}">${l}</button>`).join('')}</div><div class="inspector-body" data-scroll="inspector">${S.inspectorTab==='fields'?`<div class="row"><span class="badge ${r.level==='ERROR'?'red':r.level==='WARNING'?'amber':''}">${E(r.level)}</span>${issueOf(r)?'<span class="badge red">异常记录</span>':''}</div><h3 class="detail-heading">${E(r.event?.label||r.fn+'()')}</h3>${issueOf(r)?`<div class="detail-rule">${I('error','sm')} ${E(issueOf(r))}<small>保留原始等级；判定来源与原始日志分别呈现。</small></div>`:''}<dl class="detail-meta"><dt>完整时间</dt><dd class="mono">${E(r.ts)}</dd><dt>源文件</dt><dd>${E(f?.name)}</dd><dt>原文行号</dt><dd class="mono">L${r.line}${r.lineEnd!==r.line?'–'+r.lineEnd:''}</dd><dt>软件 / 进程</dt><dd class="mono">${E(r.logger)} · ${E(r.pid)}</dd><dt>线程</dt><dd class="mono">${E(r.tid)}</dd><dt>来源</dt><dd class="mono">${E(r.src)}:${r.srcLine}</dd><dt>函数</dt><dd class="mono">${E(r.fn)}()</dd><dt>生效规则</dt><dd>${E(f?.profile||'CPQPSGX 标准')}<br><small class="muted">${f?.override?'文件单独指定':'继承目录规则'}</small></dd></dl><div class="detail-section"><h4>消息正文</h4><div class="detail-raw">${E(r.message)}</div></div>${r.fields.length?`<div class="detail-section"><h4>提取字段 · ${r.fields.length}</h4><dl class="detail-meta" style="grid-template-columns:1fr">${r.fields.map(([k,v])=>`<div><dt class="mono">${E(k)}</dt><dd class="mono" style="margin-top:5px">${E(v)}</dd></div>`).join('')}</dl></div>`:''}`:S.inspectorTab==='raw'?`<div class="hint" style="margin-bottom:12px">${E(f?.name)} · 原始记录，不应用显示精度截断。</div><div class="detail-raw">${E(r.raw.join('\n'))}</div>`:renderContext(r,f)}</div><div class="inspector-foot">${B(S.bookmarks.has(r.id)?'取消书签':'添加书签','bookmark','bookmark','sm',`data-id="${r.id}"`)}${B('复制原文','copy-record','copy','sm',`data-id="${r.id}"`)}<span class="spacer"></span>${B('定位','locate','external','ghost sm',`data-id="${r.id}"`)}</div></aside>`}
function renderContext(r,f){const lines=f.text.split('\n');let start,end;if(S.contextUnit==='records'){const idx=f.records.findIndex(x=>x.id===r.id);start=f.records[Math.max(0,idx-S.contextSize)].line;end=f.records[Math.min(f.records.length-1,idx+S.contextSize)].lineEnd}else{start=Math.max(1,r.line-S.contextSize);end=Math.min(lines.length,r.lineEnd+S.contextSize);f.records.forEach(x=>{if(x.line<start&&x.lineEnd>=start)start=x.line;if(x.line<=end&&x.lineEnd>end)end=x.lineEnd})}return`<div class="context-config"><span>前后各</span><input id="context-size" class="input" type="number" min="0" max="1000" value="${S.contextSize}" aria-label="上下文数量"><select id="context-unit" class="select" style="height:27px;font-size:11px" aria-label="上下文单位"><option value="lines" ${S.contextUnit==='lines'?'selected':''}>物理行</option><option value="records" ${S.contextUnit==='records'?'selected':''}>记录</option></select></div><p class="hint" style="font-size:11px">源文件 L${start}–${end}，已补齐多行块。上下文不受当前搜索筛选影响。</p><div class="context-lines">${lines.slice(start-1,end).map((text,i)=>`<div class="context-line ${i+start>=r.line&&i+start<=r.lineEnd?'focus':''}"><span class="ln">${i+start}</span><span>${E(text)}</span></div>`).join('')}</div>`}
// One file library: root filtering composes with the existing file actions.
function libraryRuleName(f){const local=S.fileBindings?.[f.id]||Object.keys(S.dirBindings||{}).some(k=>k.startsWith(f.source+':')&&(f.path===k.slice(f.source.length+1)||((f.path||'').startsWith(k.slice(f.source.length+1)+'/')||(k.endsWith(':')&&(f.path||'').startsWith(k.slice(f.source.length+1))))));if(local)return Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile?.name||'未指定规则';const rule=f.serverRule??f.profile;if(rule&&typeof rule==='object'){const id=rule.ruleId||rule.id;return rule.name||rule.profile?.name||[...(S.profiles||[]),...(S.appliedProfiles||[])].find(p=>p.id===id)?.name||id||'目录继承';}return typeof rule==='string'?rule:'目录继承';}
function libraryRecordCount(f){return f.lazy||f.loaded===false?'未解析':`${f.records.length} 条记录`;}
function libraryFolders(list){if(S.libSource==='local')return `<div class="folder-title">本机临时文件</div><button class="folder-item selected" data-act="folder" data-value="">${I('laptop')}全部临时文件</button>`;if(S.libSource!=='server')return `<div class="folder-title">${S.libSource==='trash'?'回收站':'收藏'}</div><button class="folder-item selected" data-act="folder" data-value="">${I('folder')}全部文件<span class="folder-count">${list.length}</span></button>`;const roots=R6.roots.length?R6.roots:[...new Map(files.filter(f=>f.source==='server').map(f=>[f.rootId,{id:f.rootId,name:f.rootLabel||f.rootId,state:'unknown'}])).values()];return `<div class="folder-title">存储位置</div><button class="folder-item ${R6.libraryRoot==='all'?'selected':''}" data-r6="root-browse" data-id="all">${I('layers')}所有位置<span class="folder-count">${files.filter(f=>f.source==='server'&&!f.deleted).length}</span></button>`+roots.map(r=>{const fs=files.filter(f=>f.rootId===r.id&&!f.deleted),dirs=[...new Set([...fs.flatMap(f=>{const rel=f.relPath||'',parts=rel.split('/').slice(0,-1);return parts.map((_,i)=>parts.slice(0,i+1).join('/'));}),...(S.extraFolders||[]).filter(p=>p.startsWith(r.name+':')).map(p=>p.slice(r.name.length+1))])].sort();return `<button class="folder-item ${R6.libraryRoot===r.id&&!S.folder?'selected':''}" data-r6="root-browse" data-id="${E(r.id)}">${I(r.state==='offline'?'warning':'database')}<span class="ellipsis">${E(r.name)}</span><span class="folder-count">${r.state==='offline'?'离线':fs.length}</span></button>`+(R6.libraryRoot===r.id?dirs.map(dir=>{const f=fs.find(f=>f.relPath?.startsWith(dir+'/')),prefix=f?f.path.slice(0,f.path.length-f.relPath.length):r.name+':',key=prefix+dir;return `<button class="folder-item library-directory ${S.folder===key?'selected':''}" style="padding-left:${20+12*dir.split('/').length}px" data-act="folder" data-value="${E(key)}">${I('folder')}<span class="ellipsis">${E(dir.split('/').pop())}</span></button>`;}).join(''):'');}).join('');}
function libraryBinding(f){const label=libraryRuleName(f);const local=S.fileBindings?.[f.id]||Object.keys(S.dirBindings||{}).some(k=>k.startsWith(f.source+':')&&(f.path===k.slice(f.source.length+1)||((f.path||'').startsWith(k.slice(f.source.length+1)+'/')||(k.endsWith(':')&&(f.path||'').startsWith(k.slice(f.source.length+1))))));if(f.source==='server'&&f.serverRule&&!local)return {name:label,source:({personal_file:'我的文件规则',personal_directory:'我的目录规则',shared_file:'共享文件规则',shared_directory:'共享目录规则',mapping_default:'采集目录规则',service_default:'服务端默认'}[f.serverRule.layer]||'服务端规则'),origin:f.serverRule.explanation||f.serverRule.layer||'服务端'};const b=Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings);return {name:label,source:b.source,origin:b.origin};}
function renderFilesLegacy(){let list=files.filter(f=>S.libSource==='trash'?f.deleted:!f.deleted&&(S.libSource==='favorites'?f.favorite:f.source===S.libSource));if(S.libSource==='server'&&R6.libraryRoot!=='all')list=list.filter(f=>f.rootId===R6.libraryRoot);if(S.folder)list=list.filter(f=>Engine.isWithin(f.path,S.folder));if(S.libQuery)list=list.filter(f=>(f.name+' '+f.path+' '+libraryRuleName(f)).toLowerCase().includes(S.libQuery.toLowerCase()));const dirs=[...new Set([...files.filter(f=>!f.deleted&&f.source==='server').map(f=>f.path),...(S.extraFolders||[])])];return`<div class="page-heading"><h1>文件库</h1><span class="badge">${files.filter(f=>!f.deleted).length} 个文件</span><span class="spacer"></span><span class="hint">本机文件不上传</span>${B('新建文件夹','new-folder','plus','','aria-label="新建文件夹"')}</div><section class="page-content"><div class="library-top"><div class="source-tabs" aria-label="文件来源">${[['server','server','服务器日志库'],['local','laptop','本机临时'],['favorites','star','收藏'],['trash','trash','回收站']].map(([id,ic,l])=>`<button class="source-tab ${S.libSource===id?'active':''}" data-act="lib-source" data-value="${id}" aria-pressed="${S.libSource===id}">${I(ic)}${l}</button>`).join('')}</div><div class="search-box" style="max-width:300px;margin-left:auto;flex:1;min-width:150px">${I('search')}<input id="file-search" type="search" value="${E(S.libQuery)}" placeholder="搜索文件名、路径或规则" aria-label="搜索文件库"></div></div><div class="library-shell"><aside class="folder-pane">${libraryFolders(list)}</aside><div class="library-main"><div class="library-breadcrumb">${I(S.libSource==='local'?'laptop':'folder')}<button class="link" data-act="folder" data-value="">${{server:(R6.roots.find(r=>r.id===R6.libraryRoot)?.name||'所有存储位置'),local:'本机临时',favorites:'收藏',trash:'回收站'}[S.libSource]}</button>${S.folder?`${I('chevron')}<span>${E(S.folder.split(':').slice(1).join(':')||S.folder)}</span>`:''}<span class="spacer"></span><span style="font-size:11px">${list.length} 个文件</span></div>${S.libSelected.size?`<div class="bulk-bar"><span>已选 ${S.libSelected.size} 个文件</span>${B('打开选中','open-selected','logs','sm')}${B('指定规则','bulk-bind','sliders','sm')}${B('删除','bulk-delete','trash','ghost sm')}<span class="spacer"></span><button class="link" data-act="clear-file-selection">取消选择</button></div>`:''}<div class="file-table-wrap" data-scroll="library">${list.length?`<table class="file-table" aria-label="文件列表"><colgroup><col style="width:34px"><col style="width:35%"><col style="width:19%"><col style="width:12%"><col style="width:13%"><col style="width:105px"></colgroup><thead><tr><th><input type="checkbox" id="select-all-files" aria-label="选择所有当前显示文件" ${list.every(f=>S.libSelected.has(f.id))?'checked':''}></th><th>文件名</th><th>生效规则</th><th>大小 / 记录</th><th>修改时间</th><th style="text-align:right">操作</th></tr></thead><tbody>${list.map(f=>`<tr class="${S.libSelected.has(f.id)?'selected':''}"><td><input type="checkbox" data-file-check="${f.id}" aria-label="选择 ${E(f.name)}" ${S.libSelected.has(f.id)?'checked':''}></td><td><div class="filename-cell"><span class="file-format">${I('file')}</span><div class="grow"><button class="filename ellipsis" data-act="open-file" data-id="${f.id}" title="${E(f.name)}">${E(f.name)}</button><div class="file-path ellipsis" title="${E(f.path)}">${E(f.path)}</div></div></div></td><td><button class="binding ${f.override?'override':''}" data-act="bind-file" data-id="${f.id}" title="为文件指定规则">${I(f.override?'sliders':'link')}${E(libraryRuleName(f))}</button><div class="file-path" style="margin-top:4px">${f.override?'文件单独指定':'继承目录'}</div></td><td><div class="size-cell">${bytes(f.size)}</div><div class="file-path" style="margin-top:5px">${libraryRecordCount(f)}</div></td><td><span class="size-cell">${f.modified}</span></td><td><div class="table-actions">${S.libSource==='trash'?B('恢复','restore-file','restore','sm',`data-id="${f.id}"`):`${B('','favorite-file','star',`icon ghost sm ${f.favorite?'starred':''}`,`data-id="${f.id}" aria-label="${f.favorite?'取消收藏':'收藏'} ${E(f.name)}"`)}${B('','download-file','download','icon ghost sm',`data-id="${f.id}" aria-label="下载 ${E(f.name)}"`)}${B('','delete-file','trash','icon ghost sm',`data-id="${f.id}" aria-label="删除 ${E(f.name)}"`)}`}</div></td></tr>`).join('')}</tbody></table>`:`<div class="empty"><span class="empty-ico">${I(S.libSource==='local'?'upload':'folder','lg')}</span><strong>${S.libQuery?'没有匹配的文件':S.libSource==='local'?'还没有本机临时文件':'此处没有文件'}</strong><p>${S.libSource==='local'?'选择或拖入 .log / .txt 文件，仅在当前浏览器中读取。':'尝试切换目录、清除搜索或恢复回收站中的文件。'}</p>${B(S.libSource==='local'?'打开日志':'清除筛选',S.libSource==='local'?'import':'clear-file-search',S.libSource==='local'?'upload':'refresh')}</div>`}</div><div class="library-foot">${I('lock','sm')}${S.libSource==='local'?'本机来源 · 未上传服务器':'共享日志库 · 来自 /api/v2/library'}<span class="spacer"></span><span>${list.filter(f=>f.favorite).length} 个收藏</span></div></div></div></section>`}
function renderRulesLegacy(){const ps=S.profiles.filter(p=>!!p.deleted===S.profileTrash),p=S.profiles.find(p=>p.id===S.profile)||ps[0]||S.profiles[0];return`<div class="page-heading"><h1>规则管理</h1><span class="badge">${S.profiles.filter(p=>!p.deleted).length} 套规则</span><span class="spacer"></span>${B('回收站','profile-trash','trash',S.profileTrash?'active':'ghost')}${B('新建规则','new-profile','plus','primary')}</div><section class="rule-library-layout"><aside class="profiles"><div class="profiles-toolbar">${I('sliders')} ${S.profileTrash?'已删除规则':'共享规则库'}<span class="spacer"></span></div><div class="profile-list" data-scroll="profiles">${ps.map(p=>`<button class="profile-item ${S.profile===p.id?'active':''}" data-act="choose-profile" data-id="${p.id}"><div class="profile-title">${I('file','sm')}${E(p.name)}</div><div class="profile-meta"><span class="mono">v${E(p.version)}</span><span>·</span><span>${p.deleted?'已删除':E(p.status)}</span></div><p class="profile-description">${E(p.desc)}</p></button>`).join('')||'<div class="hint" style="padding:12px">回收站为空。</div>'}</div><div class="profiles-footer">文件或目录可单独指定规则。<br>修改先试跑，再保存。</div></aside><div class="rule-work"><div class="rule-work-head"><h2>${E(p.name)}</h2><span class="badge">v${E(p.version)}</span>${S.dirty?'<span class="badge amber">未保存</span>':'<span class="badge green">已保存</span>'}<span class="spacer"></span>${B('复制','copy-profile','copy','ghost sm')}${B('','delete-profile','trash','icon ghost sm','aria-label="删除当前规则"')}${p.deleted?B('恢复规则','restore-profile','restore','primary sm'):B('保存规则','save-profile','save','primary sm')}</div><div class="rule-work-tabs" aria-label="规则类别">${[['parse','解析格式'],['fields','字段提取'],['nodes','时间线节点'],['errors','异常判定'],['display','显示配色']].map(([id,l])=>`<button data-act="rule-section" data-value="${id}" class="${S.ruleSection===id?'active':''}">${l}</button>`).join('')}</div><div class="rule-editor-split"><div class="rule-editor" data-scroll="rule-editor">${renderRuleEditor(p)}</div><aside class="test-pane"><div class="test-pane-head">${I('play','sm')}手动试跑<span class="spacer"></span><button class="link" data-act="test-sample">填入样例</button></div><label for="rule-test-input" class="label">日志样本</label><textarea class="textarea" id="rule-test-input" spellcheck="false" placeholder="粘贴一段原始日志，包含头部与多行正文…">${E(S.testInput??files[0].records.slice(11,15).map(r=>r.raw.join('\n')).join('\n'))}</textarea><div class="row" style="margin-top:10px">${B('运行试跑','run-test','play','primary block')}</div>${S.testResult?`<div class="test-stats"><span><b>${S.testResult.records}</b> 条记录</span><span><b>${S.testResult.nodes.length}</b> 个节点</span><span><b>${S.testResult.unparsed}</b> 行未匹配</span></div><div class="test-result">${S.testResult.nodes.map(n=>`<div class="test-result-row">${I(n.type==='error'?'error':n.type==='warning'?'warning':'circlecheck','sm')}<div><strong>${E(n.label)}</strong><div class="mono" style="margin-top:3px;font-size:10px">${short(n.ts)} · L${n.line}</div></div></div>`).join('')||'<div class="test-result-row">样本中没有命中已启用的节点规则。</div>'}</div>`:`<p class="hint" style="margin-top:13px;font-size:11px">仅对这段样本运行，不替换工作区中的日志，也不发布共享规则。</p>`}</aside></div></div></section>`}
function renderRuleEditorLegacy(p){if(S.ruleSection==='nodes')return`<h3>节点识别</h3><p class="description">从日志中标记阶段起止与异常。原始记录保持不变，时间线只增加导航节点。</p><div class="row" style="font-size:10px;color:#657d99;border-bottom:1px solid #e3eaf3;padding-bottom:9px"><span style="width:18px"></span><span style="flex:1">节点名称 / 类型</span><span style="flex:1.5">正文或等级匹配 · 正则</span><span style="width:28px"></span></div>${S.nodeRules.map((r,i)=>`<div class="rule-row"><input type="checkbox" data-node-check="${i}" aria-label="启用 ${E(r.name)}" ${r.enabled?'checked':''}><div><div class="rule-shape"><span class="node-dot ${r.type}" style="box-shadow:none"></span><span class="rule-name">${E(r.name)}</span></div><small>${{start:'阶段起点',end:'阶段终点',error:'异常节点',warning:'告警节点',custom:'自定义关键点'}[r.type]}</small></div><input class="input" value="${E(r.pattern)}" data-node-pattern="${i}" aria-label="${E(r.name)}匹配表达式"><button class="btn icon ghost sm" data-act="delete-node-rule" data-index="${i}" aria-label="删除 ${E(r.name)}">${I('close')}</button></div>`).join('')}<div style="margin-top:15px">${B('添加节点规则','add-node-rule','plus','sm')}</div><div class="rule-examples"><span class="text-blue">模块开始</span> → 初始化完成 → <span class="text-danger">设备响应超时</span> → <span class="text-good">模块结束</span><div class="hint" style="font:11px/1.8 var(--ud-font);margin-top:7px">节点显示在原始时间上；重叠命中保留异常优先级。</div></div>`;
if(S.ruleSection==='parse')return`<h3>头部与记录边界</h3><p class="description">使用命名字段描述日志头部。未匹配的内容保留原文，不静默丢弃。</p><div class="filter-grid"><div class="field"><label class="label" for="profile-name">规则名称</label><input class="input" id="profile-name" value="${E(p.name)}"></div><div class="field"><label class="label" for="encoding">文本编码</label><select class="select" id="encoding"><option>UTF-8</option></select></div></div><div class="field"><label class="label" for="header-pattern">头部模式</label><textarea class="code-input" id="header-pattern" spellcheck="false">${E(p.headerPattern||'^\\[(?<ts>[^\\]]+)\\] \\[(?<level>\\w+)\\] \\[(?<logger>[^\\]]+)\\] ... (?<message>.*)$')}</textarea><div class="hint" style="font-size:11px;margin-top:7px">使用内置方括号解析器；不执行任意解析代码。</div></div><div class="field"><label class="label">多行归属</label><select class="select" id="body-policy"><option>续行归属上一条记录，保留原文行号</option></select></div><label class="check"><input type="checkbox" checked data-lb-pref="keepUnmatched">保留未匹配行与空行来源</label>`;
if(S.ruleSection==='fields')return`<h3>字段提取</h3><p class="description">头部正文和续行共同参与提取，重复键保留原始位置。</p><div class="rule-row"><input type="checkbox" checked><div class="rule-name">标识符键值<small>name = value / key: value</small></div><input class="input" value="[:：=]" aria-label="字段分隔符"><span></span></div><div class="rule-row"><input type="checkbox" checked><div class="rule-name">类型识别<small>数字、布尔、十六进制</small></div><span class="badge">保留原始字符串</span><span></span></div><div class="rule-row"><input type="checkbox" checked><div class="rule-name">结构体摘要<small>优先展示关键字段</small></div><input class="input" value="burstEuvEnergyMean, isValid" aria-label="摘要字段"><span></span></div><div class="rule-examples">burstEuvEnergyMean: <span class="text-good">9.414220809936523</span><br>isValid: <span style="color:#7658b8">true</span></div>`;
if(S.ruleSection==='errors')return`<h3>异常判定</h3><p class="description">原始等级与派生异常分开。EVENT 中的 RPC 失败可进入异常视角，不改写原始等级。</p>${[['原始等级','ERROR / FATAL / WARNING'],['RPC 调用失败','rpc_success: False'],['非零错误码','err_code != 0 && err_code != 0x0']].map(([n,c])=>`<div class="rule-row"><input type="checkbox" checked><div class="rule-name">${n}</div><input class="input" value="${E(c)}" aria-label="${n}条件"><span></span></div>`).join('')}<div class="field" style="margin-top:22px"><label class="label">默认异常上下文</label><div class="row"><span class="hint">前后各</span><input class="input" value="100" type="number" min="0" style="width:80px" aria-label="默认上下文数量"><select class="select" aria-label="默认上下文单位"><option>物理行</option><option>记录</option></select></div></div><div class="hint">上下文读取原文件，不受阅读列表的筛选条件限制。</div>`;
return`<h3>阅读配色</h3><p class="description">颜色按信息角色分配。红色只提示错误，普通 EVENT 和辅助元数据保持中性。</p><div class="theme-options">${themeCards()}</div><div class="role-color-grid">${[['--tok-key','字段名','#244b91'],['--tok-num','数值','#00695c'],['--tok-bool','布尔值','#7543a3'],['--reader-ink','普通正文','#26354a'],['--reader-error','错误','#a82935'],['--reader-warn','告警','#965307']].map(([key,n,color])=>`<label class="role-color-row"><input type="color" value="${E(S.colorOverrides[S.theme]?.[key]||color)}" data-color-key="${key}" aria-label="${n}颜色"><span>${n}</span><code>${E(S.colorOverrides[S.theme]?.[key]||color)}</code></label>`).join('')}</div><div class="row" style="margin-top:17px">${B('恢复本套默认色','reset-colors','restore','sm')}<span class="hint" style="font-size:11px">仅影响日志阅读面</span></div><div class="rule-examples">${tokenText('frame_id: 12004, energy: 9.4142, isValid: true')}</div>`}
function renderAdmin(){return`<div class="page-heading"><h1>管理后台</h1><span class="badge green">本地预览</span><span class="spacer"></span></div><section class="settings-body"><div class="callout">${I('info')}<div>身份与权限由服务端提供；前端不代替鉴权。</div></div><h3 class="settings-title">身份与权限</h3><p class="hint" style="margin:0 0 15px">基于访问 IP 识别身份；用户操作、文件缓存与个人阅读设置分开归属。</p><div class="settings-block"><div class="settings-row"><div><strong>用户来源</strong><small>独立配置或只读复用</small></div><div><select class="select" id="user-source"><option>LogBench 独立用户表</option><option>复用 MCChecker 用户信息</option></select></div>${B('连接设置','connection','settings','sm')}</div><div class="settings-row"><div><strong>当前身份</strong><small>当前用户</small></div><div class="row"><span class="avatar">研</span><span>研发用户</span><code>10.0.0.21</code><span class="badge blue">管理员</span></div></div></div><div class="row" style="margin:22px 0 12px"><h3 class="settings-title" style="margin:0">用户列表</h3><span class="badge">${S.users.length}</span><span class="spacer"></span>${B('添加用户','new-user','plus','sm')}</div><div class="settings-block"><table class="settings-table"><thead><tr><th>用户</th><th>IP 地址</th><th>权限</th><th>状态</th></tr></thead><tbody>${S.users.map(u=>`<tr><td>${E(u.name)}</td><td class="mono">${E(u.ip)}</td><td><span class="badge ${u.role==='管理员'?'blue':''}">${E(u.role)}</span></td><td class="muted">已启用</td></tr>`).join('')}</tbody></table></div><h3 class="settings-title" style="margin-top:23px">存储与隔离</h3><div class="settings-block"><div class="settings-row"><div><strong>服务器日志库</strong><small>按目录与文件继承解析规则</small></div><div class="mono">./data/</div><span class="badge">服务器共享</span></div><div class="settings-row"><div><strong>本机临时文件</strong><small>保存在当前浏览器会话</small></div><div>默认不上传日志正文</div><span class="badge green">本机隔离</span></div><div class="settings-row"><div><strong>个人阅读状态</strong><small>时间收藏、主题、规则草稿</small></div><div>浏览器本地存储</div><span class="badge">个人设置</span></div></div></section>`}
function themeCards(){return[['ink','墨白 · 高对比','#fff'],['paper','暖纸 · 高对比','#fffdf8'],['graphite','石墨','#1b2230'],['nord','Nord','#2e3440']].map(([id,label,bg])=>`<button class="theme-option ${S.theme===id?'active':''}" data-act="theme" data-value="${id}" aria-pressed="${S.theme===id}"><div class="theme-preview" style="background:${bg}"><div class="sample-line"><i></i><i></i><i></i></div><div class="sample-line" style="margin-left:8px"><i style="width:36px"></i><i style="width:19px"></i></div><div class="sample-line"><i style="width:46px"></i><i></i></div></div><div class="theme-label">${label}${S.theme===id?I('check','sm'):''}</div></button>`).join('')}
let popAnchor=null,popKind=null,popReturn=null;
function closePop(focus=true){const p=$('#popover');p.classList.add('hidden');p.innerHTML='';popKind=null;if(focus&&popReturn?.isConnected)popReturn.focus({preventScroll:true})}
function pop(content,anchor,kind='',cls=''){const p=$('#popover');popReturn=anchor||document.activeElement;popAnchor=anchor;popKind=kind;p.className='popover '+cls;p.innerHTML=content;p.setAttribute('role','dialog');p.setAttribute('aria-label',({time:'时间范围',point:'时间点操作',display:'阅读显示',multi:'多规则筛选',filters:'字段筛选'})[kind]||'操作面板');const box=anchor?.getBoundingClientRect()||{left:window.innerWidth/2,top:100,bottom:130};const w=p.offsetWidth,h=p.offsetHeight;let left=Math.max(12,Math.min(window.innerWidth-w-12,box.right?box.right-w:box.left)),top=box.bottom+7;if(top+h>window.innerHeight-12)top=Math.max(12,box.top-h-7);p.style.left=left+'px';p.style.top=top+'px';setTimeout(()=>{const el=$('input,button,select,textarea',p);el?.focus({preventScroll:true})},0)}
function popHead(title){return`<div class="popover-head">${title}<span class="spacer"></span>${B('','close-pop','close','icon ghost sm','aria-label="关闭面板"')}</div>`}
function showTime(anchor){const b=rangeBounds(),a=S.draft.start||S.applied?.start||b.start,z=S.draft.end||S.applied?.end||b.end;S.timeForm={start:a,end:z};pop(`${popHead('时间范围')}<div class="time-panel-layout"><aside class="time-presets"><h4>快速选择</h4>${[['full','全部时间'],['initial','初始化阶段'],['loop','闭环调整'],['before-error','首个错误前后 30 秒']].map(([id,l])=>`<button class="preset" data-act="time-preset" data-value="${id}">${I(id==='before-error'?'error':'clock')}${l}</button>`).join('')}</aside><div class="popover-body"><label class="label" for="start-date"><span class="point-key">A</span>起始时间</label><div class="date-time-row"><input class="input" type="date" id="start-date" value="${a.slice(0,10)}" aria-label="起始日期"><input class="input mono" type="time" step="1" id="start-time" value="${short(a)}" aria-label="起始时间"></div><label class="label" for="end-date"><span class="point-key">B</span>结束时间</label><div class="date-time-row"><input class="input" type="date" id="end-date" value="${z.slice(0,10)}" aria-label="结束日期"><input class="input mono" type="time" step="1" id="end-time" value="${short(z)}" aria-label="结束时间"></div><details style="font-size:11px;color:#617894;margin-bottom:10px"><summary style="cursor:pointer">小数秒精度</summary><div class="filter-grid" style="margin-top:8px"><div class="field"><label class="label" for="start-frac">A 小数秒</label><input class="input mono" id="start-frac" value="${a.split('.')[1]||'000000000'}" inputmode="numeric" maxlength="9"></div><div class="field"><label class="label" for="end-frac">B 小数秒</label><input class="input mono" id="end-frac" value="${z.split('.')[1]||'000000000'}" inputmode="numeric" maxlength="9"></div></div></details><div class="range-caption">${I('info','sm')}按日志原始时间筛选，不转换时区。</div><div id="time-error" class="hint text-danger" role="alert" style="margin-top:6px"></div><div class="time-bottom-tabs"><button data-act="recent-tab" data-value="all" class="${!S.favoriteOnly?'active':''}">最近使用</button><button data-act="recent-tab" data-value="favorites" class="${S.favoriteOnly?'active':''}">已收藏</button></div><div id="range-recents">${renderRecents()}</div></div></div><div class="popover-foot"><button class="link" data-act="clear-time">清除时间筛选</button><span class="spacer"></span>${B('取消','close-pop',null,'sm')}${B('应用范围','apply-time-form','check','primary sm')}</div>`,anchor,'time','time-panel')}
function renderRecents(){const list=S.recents.filter(r=>!S.favoriteOnly||r.favorite);return list.length?list.slice(0,5).map(r=>`<div class="recent-range"><button data-act="apply-recent" data-id="${r.id}"><span>${r.start.slice(5,10)}　${short(r.start)} – ${short(r.end)}</span></button><button class="btn icon ghost sm ${r.favorite?'starred':''}" data-act="favorite-range" data-id="${r.id}" aria-label="${r.favorite?'取消收藏':'收藏'}时间范围">${I('star','sm')}</button></div>`).join(''):`<div class="hint" style="font-size:11px;padding:9px 0">${S.favoriteOnly?'尚无收藏。最近使用的范围可点击星标收藏。':'应用过的时间范围将显示在这里。'}</div>`}
function showPointR4(id,anchor){const r=recById(id);if(!r)return;const has=pointLabel(r);pop(`<div class="menu"><div class="menu-label mono">${E(r.ts)}<div style="font:10px var(--ud-font);margin-top:5px">原文 L${r.line} · ${E(r.src)}</div></div>${[['start','A','设为起点'],['end','B','设为终点'],['key','K','添加关键点']].map(([role,key,l])=>`<button class="menu-item" data-act="set-point" data-value="${role}" data-id="${id}"><span class="point-key ${key==='K'?'k':''}">${key}</span>${l}${has===key?I('check','sm'):''}</button>`).join('')}${has?`<div class="menu-sep"></div><button class="menu-item danger" data-act="clear-record-point" data-id="${id}">${I('close')}清除此节点</button>`:''}<div class="menu-sep"></div><button class="menu-item" data-act="inspect" data-id="${id}">${I('file')}查看完整记录<span class="shortcut">Enter</span></button></div>`,anchor,'point')}
function showDisplayR4(anchor){pop(`${popHead('阅读显示')}<div class="popover-body"><div class="label">日志配色</div><div class="theme-options">${themeCards()}</div><div class="display-row"><label for="density">行密度</label><select id="density" class="select"><option value="standard" ${!S.compact?'selected':''}>标准 · 36px</option><option value="compact" ${S.compact?'selected':''}>紧凑 · 29px</option></select></div><div class="display-row"><label for="display-precision">时间精度</label><select id="display-precision" class="select"><option value="sec" ${S.precision==='sec'?'selected':''}>秒</option><option value="ms" ${S.precision==='ms'?'selected':''}>毫秒</option><option value="raw" ${S.precision==='raw'?'selected':''}>原始精度</option></select></div><div class="display-row"><label class="check"><input type="checkbox" id="wrap-checkbox" ${S.wrap?'checked':''}>正文自动换行</label></div><div class="display-row"><label class="check"><input type="checkbox" id="timeline-checkbox" ${S.timeline?'checked':''}>显示时间线</label></div><div class="hint" style="font-size:11px;border-top:1px solid #e3eaf3;padding-top:11px">仅调整阅读面；导航与管理页面保持统一浅色。</div></div><div class="popover-foot"><button class="link" data-act="color-rules">编辑元素配色</button><span class="spacer"></span>${B('完成','close-pop',null,'primary sm')}</div>`,anchor,'display','display-panel')}
function showFilters(anchor){const base=baseRecords();pop(`${popHead('字段筛选')}<div class="popover-body"><div class="filter-grid"><div class="field"><label class="label" for="filter-level">原始等级</label><select class="select" id="filter-level"><option value="">全部等级</option>${['EVENT','WARNING','ERROR','DEBUG','INFO'].map(l=>`<option ${S.level===l?'selected':''}>${l}</option>`).join('')}<option value="ISSUE" ${S.level==='ISSUE'?'selected':''}>所有异常（含规则）</option></select></div><div class="field"><label class="label" for="filter-software">软件</label><select class="select" id="filter-software"><option value="">全部软件</option>${[...new Set(base.map(r=>r.logger))].map(l=>`<option ${S.software===l?'selected':''}>${E(l)}</option>`).join('')}</select></div><div class="field"><label class="label" for="filter-module">模块 / 阶段</label><select class="select" id="filter-module"><option value="">全部模块</option>${[...new Set(base.map(r=>r.phase))].map(l=>`<option ${S.module===l?'selected':''}>${E(l)}</option>`).join('')}</select></div><div class="field"><label class="label" for="filter-fn">函数包含</label><input class="input" id="filter-fn" value="${E(S.fn)}" placeholder="get_setpoint"></div></div></div><div class="popover-foot">${B('重置','clear-filter',null,'sm')}<span class="spacer"></span>${B('应用筛选','apply-filters','check','primary sm')}</div>`,anchor,'filters','filter-panel')}
function showMulti(anchor){S.ruleDraft=JSON.parse(JSON.stringify(S.rules));if(!S.ruleDraft.length)S.ruleDraft=[{query:'timeout',name:'timeout',field:'all',enabled:true,case:false,whole:false,regex:false},{query:'rpc_success: False',name:'RPC 失败',field:'all',enabled:true,case:false,whole:false,regex:false}];showMultiContent(anchor)}
function showMultiContent(anchor){pop(`${popHead('多规则筛选')}<div class="popover-body"><div class="rule-filter-top"><span>结果显示</span><div class="segmented">${[['or','任一命中 OR'],['and','全部命中 AND'],['tabs','分标签页']].map(([id,l])=>`<button data-act="draft-rule-mode" data-value="${id}" class="${(S.draftRuleMode||S.ruleMode)===id?'active':''}">${l}</button>`).join('')}</div></div><div>${S.ruleDraft.map((r,i)=>`<div class="filter-rule-row"><input type="checkbox" data-query-enable="${i}" aria-label="启用筛选规则 ${i+1}" ${r.enabled?'checked':''}><span class="rule-number c${i%3+1}">${i+1}</span><input class="input" data-query="${i}" value="${E(r.query)}" aria-label="筛选规则 ${i+1}"><select class="select" data-query-field="${i}" aria-label="规则 ${i+1} 匹配范围">${[['all','全部内容'],['message','正文'],['body','多行正文'],['src','脚本'],['level','等级']].map(([id,n])=>`<option value="${id}" ${r.field===id?'selected':''}>${n}</option>`).join('')}</select><div class="flags">${[['case','Aa','大小写'],['whole','Ab','整词'],['regex','.*','正则']].map(([key,l,n])=>`<button class="flag" data-act="query-flag" data-index="${i}" data-value="${key}" aria-pressed="${!!r[key]}" aria-label="规则 ${i+1} ${n}">${l}</button>`).join('')}</div><button class="btn icon ghost sm" data-act="delete-query" data-index="${i}" aria-label="删除规则 ${i+1}">${I('close')}</button></div>`).join('')}</div><div class="row" style="margin-top:12px">${B('添加规则','add-query','plus','sm')}<span class="spacer"></span><span class="hint" style="font-size:11px">每条规则独立高亮</span></div><p class="rule-helper">合并结果不重复记录；分标签页分别查看每条规则。顶部搜索与时间条件始终共同生效。</p></div><div class="popover-foot">${B('清空规则','clear-rules',null,'sm')}<span class="spacer"></span>${B('取消','close-pop',null,'sm')}${B('应用筛选','apply-rules','check','primary sm')}</div>`,anchor||popAnchor,'multi','rule-panel')}
function dialog(title,body,footer='',wide=false){closePop(false);const d=$('#dialog');d.className='dialog'+(wide?' wide':'');d.innerHTML=`<div class="dialog-head"><h2>${title}</h2><span class="spacer"></span>${B('','close-dialog','close','icon ghost sm','aria-label="关闭对话框"')}</div><div class="dialog-body">${body}</div><div class="dialog-foot">${footer||B('关闭','close-dialog',null,'primary')}</div>`;if(!d.open)d.showModal()}

// Search is isolated so a pathological regular expression can be cancelled.
function searchWorkerMain(){
 const esc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 function compile(r){let s=r.regex?r.query:esc(r.query);if(r.whole)s='(?<![\\p{L}\\p{N}_])(?:'+s+')(?![\\p{L}\\p{N}_])';return new RegExp(s,'gu'+(r.case?'':'i'))}
 function find(rx,text){rx.lastIndex=0;const a=[];let m;while((m=rx.exec(text))&&a.length<300){a.push({a:m.index,b:m.index+m[0].length});if(!m[0].length)rx.lastIndex++}return a}
 self.onmessage=e=>{try{const {records,query,flags,rules,mode,tab,kind}=e.data;if(kind==='test'){const rx=rules.filter(r=>r.enabled).map(r=>({...r,re:new RegExp(r.pattern,'i')}));const hits=[];for(const r of records){const n=rx.filter(x=>x.re.test(r.level+' '+r.message)).sort((a,b)=>({error:0,warning:1,start:2,end:3,custom:4}[a.type]-{error:0,warning:1,start:2,end:3,custom:4}[b.type]))[0];if(n)hits.push({id:r.id,label:n.name,type:n.type,ts:r.ts,line:r.line})}self.postMessage({nodes:hits});return}
 const main=query?compile({query,...flags}):null,rs=rules.map((r,i)=>({...r,i,re:r.enabled&&r.query?compile(r):null}));const result={};for(const r of records){const all=[r.message,r.body.join('\n'),r.raw.join('\n'),r.src,r.fn,r.logger,r.level,r.pid,r.tid,r.phase].join('\n');const mainMatch=!main||find(main,all).length>0;const matched=[],marks=[];if(main)marks.push(...find(main,r.message).map(x=>({...x,c:1,name:'顶部搜索'})));for(const rule of rs){if(!rule.re)continue;const t=rule.field==='all'?all:rule.field==='body'?r.body.join('\n'):String(r[rule.field]||'');if(find(rule.re,t).length){matched.push(rule.i);if(['all','message'].includes(rule.field))marks.push(...find(rule.re,r.message).map(x=>({...x,c:rule.i%3+1,name:rule.name||rule.query})))}}
 const enabled=rs.filter(r=>r.re);let ok=!enabled.length||(mode==='and'?enabled.every(x=>matched.includes(x.i)):mode==='tabs'?matched.includes(tab):matched.length>0);result[r.id]={pass:mainMatch&&ok,matched,marks};}self.postMessage({result})
 }catch(error){self.postMessage({error:error.message})}};
}
let queryWorker=null,queryTimer=null,queryEpoch=0,debounce=null;
function newWorker(){const url=URL.createObjectURL(new Blob(['('+searchWorkerMain.toString()+')()'],{type:'text/javascript'}));const w=new Worker(url);URL.revokeObjectURL(url);return w}
function runSearch(reset=true){if(reset)S.page=1;const epoch=++queryEpoch;clearTimeout(queryTimer);queryWorker?.terminate();S.searchError='';if(!S.search.trim()&&!S.rules.some(r=>r.enabled&&r.query)){S.queryHits=null;S.searching=false;render();return}S.searching=true;render();try{queryWorker=newWorker();queryTimer=setTimeout(()=>{if(epoch!==queryEpoch)return;queryWorker.terminate();S.searching=false;S.searchError='搜索已超时，已保留上一份结果。请简化正则表达式后重试。';render()},1800);queryWorker.onmessage=e=>{if(epoch!==queryEpoch)return;clearTimeout(queryTimer);queryWorker.terminate();S.searching=false;if(e.data.error)S.searchError='表达式无效：'+e.data.error;else S.queryHits=e.data.result;render()};queryWorker.onerror=()=>{clearTimeout(queryTimer);S.searching=false;S.searchError='搜索工作线程不可用。请使用 Chrome / Edge 打开此 HTML。';render()};queryWorker.postMessage({records:baseRecords(),query:S.search,flags:S.flags,rules:S.rules,mode:S.ruleMode,tab:S.ruleTab})}catch(e){S.searching=false;S.searchError='无法启动后台搜索：'+e.message;render()}}
function applyColorOverrides(){for(const k of ['--tok-key','--tok-num','--tok-bool','--reader-ink','--reader-error','--reader-warn'])document.body.style.removeProperty(k);Object.entries(S.colorOverrides[S.theme]||{}).forEach(([k,v])=>document.body.style.setProperty(k,v))}
function getFields(message,body){const pairs=[];const take=(text)=>{const m=text.trim().match(/^([A-Za-z_][A-Za-z0-9_.]*)\s*[:：=]\s*(.*)$/);if(m)pairs.push([m[1],m[2]])};take(message);body.forEach(take);return pairs}
function parseLocal(text,id){
 const lines=text.replace(/^\uFEFF/,'').split(/\r?\n/);if(lines.at(-1)==='')lines.pop();
 const records=[];let cur=null;
 const header=/^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[(.+):(\d+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+(\S+)\(\)(?:[ \t]+<[^>]+>)?[ \t]?(.*)$/;
 const isTrace=s=>/Traceback \(most recent call last\):/.test(s);
 const isChain=s=>/^(?:The above exception|During handling of the above exception)/.test(s.trim());
 function updateTrace(s){if(isChain(s))cur.traceChainPending=true;else if(isTrace(s)){cur.traceComplete=false;cur.traceChainPending=false;cur.hasTrace=true;}else if(isTerminal(s))cur.traceComplete=true;}
 const isTerminal=s=>/^[A-Za-z_][\w.]*:/.test(s)||/^(?:[A-Za-z_][\w.]*(?:Error|Exception|Interrupt)|SystemExit|GeneratorExit|StopIteration)\s*$/.test(s);
 const isContinuation=s=>isTerminal(s)||!s.trim()||/^\s/.test(s)||/^(?:File ["']|Traceback \(|The above exception|During handling|[A-Za-z_][\w.]*:\s|[\^~]+$|[+|\-]{2,})/.test(s);
 function push(){if(!cur)return;cur.fields=getFields(cur.message,cur.body);cur.kind=cur.hasTrace?'stack':cur.fields.length>1?'kv':/index mask/.test(cur.message)?'mask':'text';records.push(cur);cur=null;}
 function unparsed(line,i){return{id:id+'-'+records.length,fileId:id,ordinal:records.length,line:i+1,lineEnd:i+1,ts:'',key:null,level:'UNPARSED',logger:'—',src:'—',srcLine:0,fn:'unparsed',pid:'',tid:'',message:line,body:[],bodyLineNos:[],raw:[line],traceLines:[{text:line,line:i+1}],hasTrace:isTrace(line),fields:[],phase:'未匹配原文',phaseId:'unparsed',kind:'text',event:null,issue:null};}
 for(let i=0;i<lines.length;i++){const line=lines[i],m=line.match(header);if(!m&&isTrace(line)&&cur?.traceComplete&&!cur.traceChainPending){push();cur=unparsed(line,i);continue;}if(m){
  // Merge per-line stamped stack output only when adjacent and identity-consistent.
  const tracing=cur&&cur.hasTrace;
  if(tracing&&cur.pid===m[4]&&cur.tid===m[5]&&cur.logger===m[3]&&(isChain(m[11])||isTrace(m[11])?(!cur.traceComplete||cur.traceChainPending||isChain(m[11])):cur.traceComplete?!m[11].trim():isContinuation(m[11]))){cur.body.push(m[11]);cur.bodyLineNos.push(i+1);cur.traceLines.push({text:m[11],line:i+1});cur.raw.push(line);cur.lineEnd=i+1;updateTrace(m[11]);continue;}
  push();cur={id:id+'-'+records.length,fileId:id,ordinal:records.length,line:i+1,lineEnd:i+1,ts:m[1],key:norm(m[1]),level:m[2].toUpperCase(),logger:m[3],pid:m[4],tid:m[5],src:m[6],srcLine:Number(m[7]),fn:m[10],message:m[11],body:[],bodyLineNos:[],raw:[line],traceLines:[{text:m[11],line:i+1}],hasTrace:isTrace(m[11]),fields:[],phase:'导入日志',phaseId:'import',kind:'text',event:null,issue:null};
 }else if(cur&&!/^\[\d{4}-/.test(line)&&!(cur.level==='UNPARSED'&&!isContinuation(line))){cur.body.push(line);cur.bodyLineNos.push(i+1);cur.traceLines.push({text:line,line:i+1});cur.raw.push(line);cur.lineEnd=i+1;if(cur.hasTrace||isTrace(line))updateTrace(line);
 }else{push();cur=unparsed(line,i);if(!isTrace(line))push();}}
 push();return records;
}
async function importFilesLegacy(input){const list=Array.from(input||[]);if(!list.length)return;let added=0,last=null;for(const file of list){if(file.size>10*1024*1024){notify(file.name+' 超过单文件 10 MB 限制。',true);continue}const text=await file.text();const id='local-'+Date.now()+'-'+added,records=parseLocal(text,id);const f={id,name:file.name,path:'本机临时',source:'local',profile:'内置方括号',records,text,size:file.size,modified:new Date().toLocaleTimeString('zh-CN',{hour12:false}),favorite:false};files.push(f);S.opened.push(id);last=id;added++}if(added){S.file=last;S.view='logs';S.scope='all';clearAllFilters(false);S.inspector=null;S.selected=null;render(false);notify(`已在本机读取 ${added} 个文件，未上传。关闭页面后需重新打开原始文件。`)}$('#file-input').value=''}
function download(name,text,type='text/plain;charset=utf-8'){const url=URL.createObjectURL(new Blob([text],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1500)}
async function copyRecord(id){const r=recById(id);if(!r)return;try{await navigator.clipboard.writeText(r.raw.join('\n'));notify('已复制完整原始记录')}catch{dialog('复制原始记录',`<textarea class="textarea mono" id="copy-text" style="height:230px" readonly>${E(r.raw.join('\n'))}</textarea><p class="hint">浏览器未允许剪贴板访问，选中后使用 Ctrl+C 复制。</p>`);$('#copy-text').select()}}
function addRecent(start,end){const id=start+'|'+end,prev=S.recents.find(r=>r.id===id);S.recents=[{id,start,end,favorite:prev?.favorite||false},...S.recents.filter(r=>r.id!==id)];const fav=S.recents.filter(r=>r.favorite),others=S.recents.filter(r=>!r.favorite).slice(0,8);S.recents=[...fav,...others].sort((a,b)=>(a.id===id?-1:b.id===id?1:0));save('ranges',S.recents)}
function applyRange(start,end){start=norm(start);end=norm(end);if(!start||!end||start>end){notify('起止时间无效，结束时间必须不早于起始时间。',true);return false}S.draft.start=start;S.draft.end=end;S.applied={start,end};S.page=1;S.folded.clear();addRecent(start,end);closePop(false);render(false);notify('已应用时间范围 · '+filtered().length+' 条记录');return true}
function timeFormValue(prefix){const date=$('#'+prefix+'-date'),time=$('#'+prefix+'-time'),frac=$('#'+prefix+'-frac');if(!date.value||!date.validity.valid||!time.value||!time.validity.valid)throw new Error('请选择有效日期，并填写时、分、秒。');const t=time.value.length===5?time.value+':00':time.value,f=frac.value.trim();if(!/^\d{0,9}$/.test(f))throw new Error('小数秒必须为 0–9 位数字。');return norm(date.value+' '+t+(f?'.'+f:''))}
function setTimeFields(a,b){$('#start-date').value=a.slice(0,10);$('#start-time').value=short(a);$('#start-frac').value=a.split('.')[1]||'';$('#end-date').value=b.slice(0,10);$('#end-time').value=short(b);$('#end-frac').value=b.split('.')[1]||'';$('#time-error').textContent=''}
function clearAllFilters(doRender=true){S.search='';S.queryHits=null;S.rules=[];S.level='';S.software='';S.fn='';S.module='';S.scope='all';S.applied=null;S.draft={start:null,end:null,keys:[]};S.folded.clear();S.page=1;S.searchError='';S.searching=false;queryEpoch++;queryWorker?.terminate();clearTimeout(queryTimer);if(doRender){closePop(false);render(false)}}
function openFile(id){if(id==='merged'){S.file=id}else{const f=files.find(f=>f.id===id);if(!f||f.deleted)return;if(!S.opened.includes(id))S.opened.push(id);S.file=id}S.view='logs';S.inspector=null;S.selected=null;S.page=1;S.queryHits=null;closePop(false);runSearch();}
function locateLegacy(id){const r=recById(id);if(!r)return;S.view='logs';const rows=filtered(),at=rows.findIndex(x=>x.id===id);S.selected=id;if(at<0){S.inspector=id;S.inspectorTab='context';closePop(false);render();notify('此记录不在当前筛选中，已打开原始上下文。');return}S.page=Math.floor(at/S.pageSize)+1;S.folded.clear();closePop(false);render();const row=$('[data-record="'+id+'"]');row?.scrollIntoView({block:'center',behavior:'auto'});row?.focus({preventScroll:true});const node=$('[data-node="'+id+'"]');node?.scrollIntoView({block:'nearest',behavior:'auto'})}
function profile(){return S.profiles.find(p=>p.id===S.profile)||S.profiles[0]}
function saveProfileLegacy(){const p=profile();p.version=(Number(p.version||'1.0')+.1).toFixed(1);S.dirty=false;save('profiles',S.profiles);save('nodes',S.nodeRules);render();notify('规则草稿已保存到此浏览器。未发布到服务器。')}
function bindingDialogLegacy(ids){S.bindingIds=ids;dialog('指定解析规则',`<p class="hint">为 ${ids.length} 个文件指定个人规则。修改会提交到 /api/v2/bindings。</p><div class="field"><label class="label" for="bind-profile">解析规则</label><select class="select" id="bind-profile">${S.profiles.filter(p=>!p.deleted).map(p=>`<option>${E(p.name)}</option>`).join('')}</select></div><label class="check" style="margin-top:14px"><input type="checkbox" id="bind-inherit">恢复继承目录规则</label>`,B('取消','close-dialog',null,'')+B('应用到选中文件','apply-file-binding','check','primary'))}
function deleteFiles(ids){const fs=ids.map(id=>files.find(f=>f.id===id)).filter(Boolean);if(!fs.length)return;S.pendingDelete=fs.map(f=>f.id);dialog('移入回收站',`<p>将这 ${fs.length} 个文件移入回收站？</p><p class="hint">此操作可恢复，不会删除你电脑上的原始文件。</p><div class="mono" style="font-size:12px;line-height:1.9">${fs.slice(0,6).map(f=>E(f.name)).join('<br>')}</div>`,B('取消','close-dialog',null,'')+B('移入回收站','confirm-delete-files','trash','danger'))}
function runTestLegacy(){const text=$('#rule-test-input')?.value||'';S.testInput=text;if(text.length>200000){notify('试跑样本请控制在 200 KB 内。',true);return}const records=parseLocal(text,'test');const enabled=S.nodeRules.filter(r=>r.enabled);let worker;try{worker=newWorker()}catch(e){notify('无法启动试跑线程：'+e.message,true);return}const button=$('[data-act="run-test"]');button.disabled=true;button.innerHTML=I('clock')+'试跑中…';const timer=setTimeout(()=>{worker.terminate();notify('试跑超时，请简化节点表达式。',true);render()},1500);worker.onmessage=e=>{clearTimeout(timer);worker.terminate();if(e.data.error){notify('规则表达式无效：'+e.data.error,true);render();return}S.testResult={records:records.length,nodes:e.data.nodes,unparsed:records.filter(r=>r.level==='UNPARSED').length};render()};worker.postMessage({kind:'test',records,rules:enabled})}
function saveViewLegacy(){dialog('保存阅读视角',`<div class="field"><label class="label" for="view-name">视角名称</label><input id="view-name" class="input" value="${E(S.search||'自定义阅读视角')}" maxlength="40"></div><p class="hint">保存搜索、字段与时间筛选。日志正文不会写入本地设置。</p>`,B('取消','close-dialog')+B('保存视角','confirm-save-view','save','primary'))}
function aboutLegacy(){dialog('LogBench · 阅读与规则',`<p>紧凑阅读、可配置规则、全局时间筛选与多文件时间对比。</p><p class="hint">搜索、主题、时间选段、多行展开、本地导入与筛选导出在本机执行；服务器文件库、用户权限与共享规则来自 /api/v2/*。</p><p class="hint">日志中的错误与模块节点保留原文，不代表真实设备故障。视觉体系沿用 unify-design v1.0.1。</p>`)}
function helpLegacy(){dialog('操作帮助',`<div class="help-grid"><h3>定位与选段</h3><p>点击时间线节点的名称定位日志；点击时间可设为 A 起点、B 终点或 K 关键点。选择完成后点击“应用区间”。在时间线的“区间”页可折叠段落。</p><h3>阅读</h3><p>单击选择记录，双击或 Enter 打开详情。多行字段先显示摘要，完整值可在详情或原文中查看。</p><h3>快捷键</h3><p><kbd>Ctrl / ⌘ + F</kbd> 搜索　<kbd>Alt + T</kbd> 时间范围　<kbd>Esc</kbd> 关闭面板。记录聚焦时用 ↑ / ↓ 切换。</p><h3>来源与存储</h3><p>本机本机文件只在当前会话中读取；主题与时间收藏保存在此浏览器，服务器操作提交到 /api/v2/*。</p></div>`)}

// Delegation keeps controls consistent across the review surfaces.
document.addEventListener('click',async event=>{const el=event.target.closest('[data-act]');if(!el){if(!event.target.closest('#popover')&&!event.target.closest('#dialog'))closePop(false);return}if(el.disabled)return;const act=el.dataset.act,v=el.dataset.value,id=el.dataset.id,ix=Number(el.dataset.index);try{
 switch(act){
 case 'toggle-nav':if(innerWidth<=1000){$('#app').classList.toggle('nav-open')}else{S.sidebar=!S.sidebar;render()}break;
 case 'nav':S.view=v;closePop(false);render(false);break;
 case 'import':$('#file-input').click();break;
 case 'about':about();break;case 'help':help();break;
 case 'identity':dialog('当前身份',`<div class="row" style="gap:12px"><span class="avatar">研</span><div><strong>研发用户</strong><p class="hint mono">10.0.0.21 · 管理员</p></div></div><p class="hint">身份与权限由 /api/v2/me 提供；前端不代替鉴权。</p>`);break;
 case 'open-file':openFile(id);break;
 case 'close-file':S.opened=S.opened.filter(x=>x!==id);if(S.file===id)S.file=S.opened[0]||null;S.inspector=null;S.selected=null;S.queryHits=null;S.page=1;render();break;
 case 'scope':S.scope=v;S.view='logs';S.page=1;S.inspector=null;closePop(false);render(false);break;
 case 'save-view':saveView();break;
 case 'confirm-save-view':{const name=$('#view-name').value.trim();if(!name)return;const views=load('views',[]);views.push({name,search:S.search,flags:S.flags,rules:S.rules,mode:S.ruleMode,scope:S.scope,applied:S.applied});save('views',views.slice(-8));$('#dialog').close();render();notify('已保存个人阅读视角');break}
 case 'saved-view':{const x=load('views',[])[ix];if(x){S.search=x.search;S.flags=x.flags;S.rules=x.rules;S.ruleMode=x.mode;S.scope=x.scope;S.applied=x.applied;S.view='logs';runSearch()}break}
 case 'time-panel':showTime(el);break;case 'point':showPoint(id,el);break;
 case 'set-point':{const r=recById(id);if(!r?.key){notify('这条记录没有有效时间。',true);break}if(v==='key'){if(!S.draft.keys.includes(r.key))S.draft.keys.push(r.key)}else S.draft[v]=r.key;S.timeline=true;S.selected=id;closePop(false);render();if(innerWidth<=760){S.mobileTimeline=true;$('.timeline')?.classList.add('mobile-open')}notify(v==='key'?'已添加关键点，可在“区间”中折叠段落。':'已设置'+(v==='start'?'起点 A':'终点 B')+'，选择完成后应用区间。');break}
 case 'remove-point':S.draft[v]=null;render();break;
 case 'clear-record-point':{const r=recById(id);if(S.draft.start===r.key)S.draft.start=null;if(S.draft.end===r.key)S.draft.end=null;S.draft.keys=S.draft.keys.filter(t=>t!==r.key);closePop(false);render();break}
 case 'apply-time':applyRange(S.draft.start,S.draft.end);break;
 case 'apply-time-form':try{const a=timeFormValue('start'),b=timeFormValue('end');if(a>b)throw new Error('结束时间早于起始时间。');applyRange(a,b)}catch(e){$('#time-error').textContent=e.message}break;
 case 'clear-time':S.applied=null;S.draft={start:null,end:null,keys:[]};S.folded.clear();S.page=1;closePop(false);render();break;
 case 'time-preset':{let b=rangeBounds();if(v==='initial'||v==='loop'){const rs=baseRecords().filter(r=>r.phaseId===(v==='initial'?'boot':'loop'));if(rs.length)b={start:rs[0].key,end:rs.at(-1).key}}else if(v==='before-error'){const r=baseRecords().find(r=>issueOf(r));if(r){const d=new Date(r.ts.slice(0,19).replace(' ','T')+'Z');b={start:norm(new Date(+d-30000).toISOString().slice(0,19)),end:norm(new Date(+d+30000).toISOString().slice(0,19))}}}setTimeFields(b.start,b.end);break}
 case 'recent-tab':S.favoriteOnly=v==='favorites';$$('[data-act="recent-tab"]').forEach(b=>b.classList.toggle('active',(b.dataset.value==='favorites')===S.favoriteOnly));$('#range-recents').innerHTML=renderRecents();break;
 case 'favorite-range':{const r=S.recents.find(x=>x.id===id);if(r){r.favorite=!r.favorite;save('ranges',S.recents);$('#range-recents').innerHTML=renderRecents()}break}
 case 'apply-recent':{const r=S.recents.find(x=>x.id===id);if(r)applyRange(r.start,r.end);break}
 case 'filters':showFilters(el);break;
 case 'apply-filters':S.level=$('#filter-level').value;S.software=$('#filter-software').value;S.module=$('#filter-module').value;S.fn=$('#filter-fn').value.trim();S.page=1;closePop(false);render(false);break;
 case 'clear-filter':S.level='';S.software='';S.fn='';S.module='';S.page=1;closePop(false);render(false);break;
 case 'clear-level':S.level='';S.page=1;render();break;
 case 'clear-all-filters':clearAllFilters();break;
 case 'clear-search':S.search='';runSearch();break;
 case 'search-flag':S.flags[v]=!S.flags[v];runSearch();break;
 case 'multi':S.draftRuleMode=S.ruleMode;showMulti(el);break;
 case 'add-query':if(S.ruleDraft.length>=6){notify('最多同时配置 6 条筛选。',true);break}S.ruleDraft.push({query:'',name:'',field:'all',enabled:true,case:false,whole:false,regex:false});showMultiContent();break;
 case 'delete-query':S.ruleDraft.splice(ix,1);showMultiContent();break;
 case 'query-flag':S.ruleDraft[ix][v]=!S.ruleDraft[ix][v];showMultiContent();break;
 case 'draft-rule-mode':S.draftRuleMode=v;showMultiContent();break;
 case 'apply-rules':S.rules=S.ruleDraft.filter(r=>r.query.trim()).map(r=>({...r,name:r.query.trim()}));S.ruleMode=S.draftRuleMode||'or';S.ruleTab=Math.max(0,S.rules.findIndex(r=>r.enabled));closePop(false);runSearch();break;
 case 'clear-rules':S.rules=[];closePop(false);runSearch();break;
 case 'rule-mode':S.ruleMode=v;runSearch();break;
 case 'rule-tab':S.ruleTab=ix;if(S.ruleMode==='tabs')runSearch();else{S.ruleMode='tabs';runSearch()}break;
 case 'display':showDisplay(el);break;
 case 'theme':S.theme=v;save('theme',v);render();if(popKind==='display')showDisplay($('#display-btn'));break;
 case 'reset-colors':delete S.colorOverrides[S.theme];save('colors',S.colorOverrides);render();notify('已恢复本套默认色');break;
 case 'color-rules':S.view='rules';S.ruleSection='display';closePop(false);render(false);break;
 case 'toggle-timeline':if(innerWidth<=760){S.timeline=true;S.mobileTimeline=!S.mobileTimeline;render()}else{S.timeline=!S.timeline;render()}break;
 case 'timeline-tab':S.timelineTab=v;render();break;
 case 'timeline-issues':S.tlOnlyIssues=!S.tlOnlyIssues;render();break;
 case 'timeline-rules':S.view='rules';S.ruleSection='nodes';closePop(false);render(false);break;
 case 'fold-all':intervals().forEach(x=>S.folded.add(x.id));render();break;
 case 'unfold-all':S.folded.clear();render();break;
 case 'fold-interval':S.folded.has(id)?S.folded.delete(id):S.folded.add(id);render();break;
 case 'fold-blocks':S.expanded.clear();render();break;
 case 'select-record':S.selected=id;$$('.log-row').forEach(r=>{r.classList.toggle('selected',r.dataset.record===id);r.setAttribute('aria-selected',r.dataset.record===id?'true':'false')});$$('.tl-node').forEach(n=>n.classList.toggle('current',n.dataset.node===id));break;
 case 'expand-block':S.expanded.has(id)?S.expanded.delete(id):S.expanded.add(id);render();break;
 case 'body-mode':S.bodyModes[id]=S.bodyModes[id]==='raw'?'fields':'raw';render();break;
 case 'full-fields':S.fullFields.has(id)?S.fullFields.delete(id):S.fullFields.add(id);render();break;
 case 'inspect':S.inspector=id;S.selected=id;S.inspectorTab='fields';closePop(false);render();break;
 case 'close-inspector':S.inspector=null;render();break;
 case 'inspector-tab':S.inspectorTab=v;render();break;
 case 'inspect-prev':case 'inspect-next':{const rows=filtered(),i=rows.findIndex(x=>x.id===S.inspector),n=rows[i+(act==='inspect-next'?1:-1)];if(n){S.inspector=n.id;S.selected=n.id;render()}break}
 case 'locate':locate(id);break;
 case 'bookmark':S.bookmarks.has(id)?S.bookmarks.delete(id):S.bookmarks.add(id);render();break;
 case 'copy-record':copyRecord(id);break;
 case 'prev-page':S.page=Math.max(1,S.page-1);render(false);break;
 case 'next-page':S.page++;render(false);break;
 case 'export':{const rows=filtered();download('LogBench_filtered.log',rows.map(r=>r.raw.join('\n')).join('\n'));notify('已导出 '+rows.length+' 条完整记录；未受折叠影响。');break}
 case 'download-file':{const f=files.find(x=>x.id===id);if(f)download(f.name,f.text);break}
 case 'lib-source':S.libSource=v;S.folder='';S.libSelected.clear();render(false);break;
 case 'folder':S.folder=v;S.libSelected.clear();render(false);break;
 case 'toggle-device':S.collapsedDirs.has(v)?S.collapsedDirs.delete(v):S.collapsedDirs.add(v);render();break;
 case 'toggle-folder':S.folderOpen=!S.folderOpen;render();break;
 case 'clear-file-search':S.libQuery='';S.folder='';render();break;
 case 'clear-file-selection':S.libSelected.clear();render();break;
 case 'open-selected':{const ids=[...S.libSelected];ids.forEach(id=>{if(!S.opened.includes(id))S.opened.push(id)});S.libSelected.clear();openFile(ids.length>1?'merged':ids[0]);break}
 case 'favorite-file':{const f=files.find(x=>x.id===id);f.favorite=!f.favorite;render();break}
 case 'delete-file':deleteFiles([id]);break;
 case 'bulk-delete':deleteFiles([...S.libSelected]);break;
 case 'confirm-delete-files':S.pendingDelete.forEach(id=>{files.find(f=>f.id===id).deleted=true;S.opened=S.opened.filter(x=>x!==id)});if(!S.opened.includes(S.file))S.file=S.opened[0]||null;S.libSelected.clear();$('#dialog').close();render();notify('已移入回收站，可随时恢复');break;
 case 'restore-file':files.find(f=>f.id===id).deleted=false;render();notify('文件已恢复到文件库');break;
 case 'new-folder':dialog('新建文件夹',`<div class="field"><label class="label" for="folder-name">文件夹名称</label><input class="input" id="folder-name" placeholder="例如：设备 C"></div><p class="hint">空目录登记在当前工作区。</p>`,B('取消','close-dialog')+B('创建','confirm-folder','plus','primary'));break;
 case 'confirm-folder':{const name=$('#folder-name').value.trim();if(!name){notify('请填写文件夹名称。',true);break}if(/[\\/<>]/.test(name)){notify('名称不能包含路径分隔符。',true);break}S.extraFolders=S.extraFolders||[];const root=R6.roots.find(r=>r.id===R6.libraryRoot)||R6.roots.find(r=>r.id==='legacy');if(!root){notify('请先登记存储位置。',true);break;}R6.libraryRoot=root.id;S.libSource='server';S.folder=(S.folder||root.name+':')+(S.folder?'/':'')+name;S.extraFolders.push(S.folder);$('#dialog').close();render();notify('已创建目录');break}
 case 'bind-file':bindingDialog([id]);break;case 'bulk-bind':bindingDialog([...S.libSelected]);break;
 case 'apply-file-binding':{const inherited=$('#bind-inherit').checked,name=$('#bind-profile').value;S.bindingIds.forEach(id=>{const f=files.find(f=>f.id===id);f.profile=inherited?'CPQPSGX 标准':name;f.override=!inherited});$('#dialog').close();render();notify('规则归属已更新');break}
 case 'profile-trash':S.profileTrash=!S.profileTrash;const next=S.profiles.find(p=>!!p.deleted===S.profileTrash);if(next)S.profile=next.id;render(false);break;
 case 'choose-profile':S.profile=id;S.dirty=false;S.testResult=null;render(false);break;
 case 'new-profile':dialog('新建规则',`<div class="field"><label class="label" for="new-profile-name">规则名称</label><input class="input" id="new-profile-name" placeholder="例如：设备调试日志"></div><p class="hint">创建基于方括号格式的本地设计草稿。</p>`,B('取消','close-dialog')+B('创建规则','create-profile','plus','primary'));break;
 case 'create-profile':{const name=$('#new-profile-name').value.trim();if(!name){notify('请填写规则名称。',true);break}const p={id:'p'+Date.now(),name,version:'1.0',status:'个人草稿',desc:'自定义日志规则',format:'bracket',deleted:false};S.profiles.push(p);S.profile=p.id;S.profileTrash=false;S.ruleSection='parse';S.dirty=true;$('#dialog').close();render(false);break}
 case 'copy-profile':{const p={...profile(),id:'p'+Date.now(),name:profile().name+' · 副本',status:'个人草稿',version:'1.0',deleted:false};S.profiles.push(p);S.profile=p.id;S.dirty=true;render();notify('已复制为独立规则草稿');break}
 case 'delete-profile':dialog('删除规则',`<p>将“${E(profile().name)}”移入规则回收站？</p><p class="hint">可恢复。实际生产环境还需检查文件绑定引用。</p>`,B('取消','close-dialog')+B('移入回收站','confirm-delete-profile','trash','danger'));break;
 case 'confirm-delete-profile':profile().deleted=true;save('profiles',S.profiles);$('#dialog').close();S.profile=S.profiles.find(p=>!p.deleted)?.id||S.profile;render();notify('规则已移入回收站');break;
 case 'restore-profile':profile().deleted=false;S.profileTrash=false;save('profiles',S.profiles);render();notify('规则已恢复');break;
 case 'save-profile':saveProfile();break;
 case 'rule-section':S.ruleSection=v;render(false);break;
 case 'delete-node-rule':S.nodeRules.splice(ix,1);S.dirty=true;render();break;
 case 'add-node-rule':dialog('添加时间线节点',`<div class="field"><label class="label" for="node-name">节点名称</label><input class="input" id="node-name" placeholder="例如：开始采集"></div><div class="field" style="margin-top:12px"><label class="label" for="node-type">节点类型</label><select class="select" id="node-type"><option value="custom">自定义关键点</option><option value="start">阶段开始</option><option value="end">阶段结束</option><option value="error">错误</option><option value="warning">告警</option></select></div><div class="field" style="margin-top:12px"><label class="label" for="node-pattern">正文或等级匹配表达式</label><input class="input mono" id="node-pattern" placeholder="acquisition started"></div>`,B('取消','close-dialog')+B('添加节点','create-node-rule','plus','primary'));break;
 case 'create-node-rule':{const name=$('#node-name').value.trim(),pattern=$('#node-pattern').value.trim();if(!name||!pattern){notify('请填写节点名称与表达式。',true);break}try{new RegExp(pattern)}catch{notify('正则表达式无效。',true);break}S.nodeRules.push({name,pattern,type:$('#node-type').value,enabled:true});S.dirty=true;$('#dialog').close();render();break}
 case 'test-sample':S.testInput=files[0].records.slice(11,15).map(r=>r.raw.join('\n')).join('\n');render();break;
 case 'run-test':runTest();break;
 case 'connection':dialog('身份来源连接',`<div class="field"><label class="label" for="mc-directory">MCChecker 部署目录</label><input class="input mono" id="mc-directory" placeholder="/opt/MCChecker"></div><p class="hint">生产环境会只读预检人员信息与权限。此 HTML 没有服务器访问权限，不会读取目录。</p>`,B('关闭','close-dialog')+B('连接预检（需后端）','backend-required','shield','primary'));break;
 case 'backend-required':notify('无法预检目录，请检查服务连接。',true);break;
 case 'new-user':dialog('添加用户',`<div class="filter-grid"><div class="field"><label class="label" for="user-name">姓名</label><input class="input" id="user-name"></div><div class="field"><label class="label" for="user-ip">IP 地址</label><input class="input mono" id="user-ip" placeholder="10.0.0.23"></div></div><div class="field" style="margin-top:13px"><label class="label" for="user-role">权限</label><select class="select" id="user-role"><option>成员</option><option>管理员</option></select></div><p class="hint">保存后生效；权限由服务端校验。</p>`,B('取消','close-dialog')+B('添加','confirm-user','plus','primary'));break;
 case 'confirm-user':{const name=$('#user-name').value.trim(),ip=$('#user-ip').value.trim();if(!name||!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)||ip.split('.').some(n=>+n>255)){notify('请输入姓名和有效 IPv4 地址。',true);break}S.users.push({name,ip,role:$('#user-role').value});$('#dialog').close();render();break}
 case 'close-pop':closePop();break;case 'close-dialog':$('#dialog').close();break;
 }
 }catch(error){console.error(error);notify('操作未完成：'+error.message,true)}
});
document.addEventListener('input',e=>{const el=e.target;
 if(el.id==='main-search'){S.search=el.value;clearTimeout(debounce);debounce=setTimeout(()=>runSearch(),200)}
 if(el.id==='file-search'){S.libQuery=el.value;render()}
 if(el.dataset.query!==undefined){const r=S.ruleDraft[Number(el.dataset.query)];r.query=el.value;r.name=el.value}
 if(el.dataset.nodePattern!==undefined){S.nodeRules[Number(el.dataset.nodePattern)].pattern=el.value;S.dirty=true}
 if(el.id==='rule-test-input')S.testInput=el.value;
 if(el.id==='profile-name'){profile().name=el.value;S.dirty=true}
 if(el.id==='header-pattern'){profile().headerPattern=el.value;S.dirty=true}
 if(el.dataset.colorKey){S.colorOverrides[S.theme]=S.colorOverrides[S.theme]||{};S.colorOverrides[S.theme][el.dataset.colorKey]=el.value;save('colors',S.colorOverrides);applyColorOverrides();el.parentElement.querySelector('code').textContent=el.value}
});
document.addEventListener('change',e=>{const el=e.target;
 if(el.id==='file-input')importFiles(el.files);
 if(['precision','display-precision'].includes(el.id)){S.precision=el.value;save('precision',S.precision);render()}
 if(el.id==='density'){S.compact=el.value==='compact';save('compact',S.compact);render()}
 if(el.id==='wrap-checkbox'){S.wrap=el.checked;render()}
 if(el.id==='timeline-checkbox'){S.timeline=el.checked;render()}
 if(el.id==='page-size'){S.pageSize=+el.value;S.page=1;render(false)}
 if(el.id==='context-size'){S.contextSize=Math.max(0,Math.min(1000,+el.value||0));render()}
 if(el.id==='context-unit'){S.contextUnit=el.value;render()}
 if(el.dataset.queryEnable!==undefined)S.ruleDraft[+el.dataset.queryEnable].enabled=el.checked;
 if(el.dataset.queryField!==undefined)S.ruleDraft[+el.dataset.queryField].field=el.value;
 if(el.dataset.nodeCheck!==undefined){S.nodeRules[+el.dataset.nodeCheck].enabled=el.checked;S.dirty=true}
 if(el.dataset.fileCheck){el.checked?S.libSelected.add(el.dataset.fileCheck):S.libSelected.delete(el.dataset.fileCheck);render()}
 if(el.id==='select-all-files'){$$('[data-file-check]').forEach(c=>el.checked?S.libSelected.add(c.dataset.fileCheck):S.libSelected.delete(c.dataset.fileCheck));render()}
});
document.addEventListener('dblclick',e=>{const row=e.target.closest('[data-record]');if(row&&!e.target.closest('button')){S.inspector=row.dataset.record;S.inspectorTab='fields';render()}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(!$('#popover').classList.contains('hidden')){e.preventDefault();closePop()}else if(!$('#dialog').open&&S.inspector){S.inspector=null;render()}else{$('#app').classList.remove('nav-open');S.mobileTimeline=false;$('.timeline')?.classList.remove('mobile-open')}return}if($('#dialog').open)return;
 if((e.ctrlKey||e.metaKey)&&['f','k'].includes(e.key.toLowerCase())){e.preventDefault();if(S.view!=='logs'){S.view='logs';render()}$('#main-search').focus();$('#main-search').select()}
 if(e.altKey&&e.key.toLowerCase()==='t'){e.preventDefault();if(S.view!=='logs'){S.view='logs';render()}showTime($('#time-button'))}
 const row=e.target.closest('[data-record]');if(row&&e.target===row){if(e.key==='Enter'){e.preventDefault();S.inspector=row.dataset.record;S.inspectorTab='fields';render()}if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();const rows=filtered(),i=rows.findIndex(r=>r.id===row.dataset.record),n=rows[i+(e.key==='ArrowDown'?1:-1)];if(n)locate(n.id)}}
 if(e.key==='Enter'&&e.target.matches('div[data-act][tabindex]')){e.preventDefault();e.target.click()}
 if(e.key==='Tab'&&!$('#popover').classList.contains('hidden')){const list=$$('button:not(:disabled),input:not(:disabled),select,textarea,summary',$('#popover')),first=list[0],last=list.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}
});
let dragDepth=0;document.addEventListener('dragenter',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();dragDepth++;$('#drop-overlay').classList.remove('hidden')}});document.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault()});document.addEventListener('dragleave',()=>{dragDepth--;if(dragDepth<=0)$('#drop-overlay').classList.add('hidden')});document.addEventListener('drop',e=>{if(e.dataTransfer?.files.length){e.preventDefault();dragDepth=0;$('#drop-overlay').classList.add('hidden');importFiles(e.dataTransfer.files)}});
window.addEventListener('resize',()=>closePop(false));
// R4 interaction prototype extensions. Shared business permissions remain backend-only.
const Engine=LB4EngineFactory();
const clone=Engine.clone;
Object.assign(paths,{collapse:'M5 9l7-6 7 6M5 20l7-6 7 6',expand:'M5 4l7 6 7-6M5 15l7 6 7-6',compare:'M3 4h7v16H3zM14 4h7v16h-7zM10 9h4m-4 6h4',globe:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18',grid:'M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z'});
Object.assign(S,{pageSize:1000,rawMode:false,blocksDefault:true,blockClosed:new Set(),blockOpened:new Set(),globalSelection:null,picker:null,compare:{ids:[],layout:'columns',sync:true,top:0},timelinePage:0,dirBindings:load('dirBindings',{'server:data':'cpq'}),fileBindings:load('fileBindings',{}),activeView:null,viewDraft:null,ruleBusy:false});
S.profiles.forEach(p=>{const d=Engine.defaults();p.extractors=p.extractors||d.extractors;p.anomalies=p.anomalies||d.anomalies;p.nodes=p.nodes||clone(defaultNodes).map(q=>({...q,pattern:q.type==='start'?q.pattern+'|set recipe':q.type==='end'?q.pattern+'|Recipe applied':q.pattern}));p.nodes=p.nodes.map(Engine.normalizeNode);});
// A clearly labelled local fixture makes both branches of the picker reviewable.
const localDemo={id:'cache-sample',name:'local_review.sample.log',path:'本机缓存/调试样例',source:'local',sample:true,profile:'CPQPSGX 标准',records:makeDemo('cache-sample',2).slice(45,85),modified:'10-26 11:50',favorite:false};
localDemo.text=localDemo.records.map(r=>r.raw.join('\n')).join('\n');localDemo.size=new Blob([localDemo.text]).size;files.push(localDemo);
let recordIndex=new Map(),dataGeneration=0,mergedMemo=null,logVirtual=null,compareVirtuals=[],compareAxis=null,pendingAnchor=null;
function indexRecords(){recordIndex=new Map();files.forEach(f=>f.records.forEach(r=>recordIndex.set(r.id,r)));dataGeneration++;mergedMemo=null;compareAxis=null;}
function resolved(f,dirs=S.dirBindings,bindings=S.fileBindings,profiles=S.profiles){return Engine.resolveProfile(f,profiles,dirs,bindings);}
files.forEach(f=>{f.records.forEach(r=>r.originalEvent=clone(r.event));const b=resolved(f);f.profile=b.profile.name;f.ruleOrigin=b.origin;f.override=b.source==='文件单独指定'});indexRecords();
function selectedGlobalIds(selection=S.globalSelection){if(!selection)return[];const ids=new Set(selection.fileIds||[]);for(const f of files)if((selection.folders||[]).some(k=>k.startsWith(f.source+':')&&Engine.isWithin(f.path,k.slice(f.source.length+1))))ids.add(f.id);return[...ids].filter(id=>files.some(f=>f.id===id&&!f.deleted));}
function allBaseRecords(){if(S.file!=='merged'&&S.file!=='global')return currentFile()?.records||[];const ids=S.file==='global'?selectedGlobalIds():S.opened;const signature=dataGeneration+'|'+S.file+'|'+ids.join(',');if(mergedMemo?.signature===signature)return mergedMemo.rows;const rows=files.filter(f=>ids.includes(f.id)&&!f.deleted).flatMap(f=>f.records).sort((a,b)=>(a.key||'').localeCompare(b.key||'')||a.fileId.localeCompare(b.fileId)||a.line-b.line);mergedMemo={signature,rows};return rows;}
function blockOpen(r){return S.blocksDefault?!S.blockClosed.has(r.id):S.blockOpened.has(r.id);}
function liveDestroy(){logVirtual?.destroy();logVirtual=null;compareVirtuals.forEach(v=>v.destroy());compareVirtuals=[];}
function captureLogAnchor(){const sc=$('#log-scroll');if(!sc||!logVirtual)return null;let el=S.selected?sc.querySelector(`[data-record="${CSS.escape(S.selected)}"]`):null;const box=sc.getBoundingClientRect();if(!el||el.getBoundingClientRect().bottom<box.top+31||el.getBoundingClientRect().top>box.bottom)el=$$('[data-record]',sc).find(x=>x.getBoundingClientRect().bottom>box.top+32);return el?{id:el.dataset.record,y:el.getBoundingClientRect().top-box.top,left:sc.scrollLeft}:null;}
function renderR4(keep=true){const pos=preserve(),anchor=pendingAnchor;pendingAnchor=null;liveDestroy();document.body.dataset.theme=S.theme;const root=$('#app');root.className=`lb-app ${!S.sidebar?'nav-collapsed':''} ${S.compact?'compact':''} ${S.wrap?'wrap-lines':''} ${S.rawMode?'raw-mode':''}`;root.innerHTML=`${renderHeader()}<div class="mobile-scrim" data-act="toggle-nav"></div>${renderSidebar()}<main class="main">${S.view==='logs'?renderLogWorkspace():S.view==='files'?renderFiles():S.view==='rules'?renderRules():S.view==='compare'?renderCompare():renderAdmin()}<footer class="app-status"><span class="row">${I('lock','sm')}${LB.statusText()}</span><span class="status-right"><span class="extra">${S.view==='compare'?'共同时间轴 · 保留各文件原始时间':LB.dataFreshness()}</span>${LB.scenarioSelect()}<span>LogBench / R6</span></span></footer></main>`;applyColorOverrides();if(S.view==='logs')mountLogVirtual();if(S.view==='compare')mountCompare();restore(pos,keep);if(logVirtual)logVirtual.paint(true);if(anchor&&logVirtual){logVirtual.toId(anchor.id,anchor.y);$('#log-scroll').scrollLeft=anchor.left||0;requestAnimationFrame(()=>logVirtual?.toId(anchor.id,anchor.y));}if(S.view==='files')decorateBindings();LB.paintStatus();}
// File coverage is independent of search/time filters and preserves source precision.
const fileTimeCache=new WeakMap();
function fileTimeKey(value){const m=String(value||'').match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$/);if(!m)return null;const [y,mo,d,h,mi,se]=m.slice(1,7).map(Number);if(y<1000||mo<1||mo>12||d<1||h>23||mi>59||se>59)return null;const dt=new Date(Date.UTC(y,mo-1,d,h,mi,se));if(dt.getUTCFullYear()!==y||dt.getUTCMonth()!==mo-1||dt.getUTCDate()!==d)return null;return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}.${(m[7]||'').padEnd(9,'0')}`;}
function filenameStart(name){const m=String(name||'').match(/_(\d{8,23})\.log$/i);if(!m)return null;const s=m[1],n=s.length;if(![8,10,12].includes(n)&&n<14)return null;const stamp=`${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}`,clock=`${s.slice(8,10)||'00'}:${s.slice(10,12)||'00'}:${s.slice(12,14)||'00'}`,fraction=s.slice(14),key=fileTimeKey(stamp+' '+clock+(fraction?'.'+fraction:''));if(!key)return null;return {key,display:stamp+(n>8?' '+clock.slice(0,n===10?2:n===12?5:8):'')+(fraction?'.'+fraction:''),precision:n===8?'日':n===10?'小时':n===12?'分钟':n===14?'秒':'小数秒'};}
function fileTimeRange(f){const rows=f.records||[],epoch=typeof dataGeneration==='number'?dataGeneration:0,old=fileTimeCache.get(f);if(old&&old.rows===rows&&old.length===rows.length&&old.epoch===epoch&&old.name===f.name)return old.value;let start=null,end=null,count=0;for(const r of rows){const key=fileTimeKey(r.key||r.ts);if(!key)continue;count++;if(start===null||key<start)start=key;if(end===null||key>end)end=key;}const named=start?null:filenameStart(f.name),value={start:start||named?.key||null,end,origin:start?'records':named?'filename':'unknown',startDisplay:start?displayFileTime(start):named?.display||null,endDisplay:end?displayFileTime(end):null,precision:named?.precision||null,count};fileTimeCache.set(f,{rows,length:rows.length,epoch,name:f.name,value});return value;}
function displayFileTime(key){return key.replace(/\.0+$/,'').replace(/(\.\d*?[1-9])0+$/,'$1');}
function compactFileTimes(t,missing){const full=`起始 ${t.startDisplay||missing} → 结束 ${t.endDisplay||missing}${t.origin==='filename'?'（文件名推定）':''}`;if(!t.start)return `<div class="file-time-range" aria-label="${E(full)}"><span class="file-time-date">${E(missing)}</span></div>`;const date=t.start.slice(0,10),endDate=t.end?.slice(0,10),dates=date+(endDate&&endDate!==date?' → '+(date.slice(0,4)===endDate.slice(0,4)?endDate.slice(5):endDate):''),start=t.startDisplay.slice(11,19)||'—',end=t.endDisplay?.slice(11,19)||missing;return `<div class="file-time-range" title="${E(full)}" aria-label="${E(full)}"><span class="file-time-date">${E(dates)}${t.origin==='filename'?' · 文件名推定':''}</span><span class="file-time-span"><time aria-label="起始 ${E(t.startDisplay)}">${E(start)}</time><span aria-hidden="true">–</span><time aria-label="结束 ${E(t.endDisplay||missing)}">${E(end)}</time></span></div>`;}
function fileTimeCard(f){return compactFileTimes(fileTimeRange(f),f.lazy||f.loaded===false?'未解析':'无时间记录');}
function fileTimeIntervals(){return S.opened.map(id=>files.find(f=>f.id===id&&!f.deleted)).filter(Boolean).map(f=>({id:f.id,file:f,...fileTimeRange(f)})).sort((a,b)=>(a.start||'9999').localeCompare(b.start||'9999')||a.id.localeCompare(b.id));}
function fileTimeMillis(key){return Date.parse(key.slice(0,19).replace(' ','T')+'Z')+Number('0.'+key.slice(20))*1000;}
function renderFileIntervals(){const rows=fileTimeIntervals(),known=rows.filter(x=>x.end),lo=known.length?Math.min(...known.map(x=>fileTimeMillis(x.start))):0,hi=known.length?Math.max(...known.map(x=>fileTimeMillis(x.end))):0,span=hi-lo;return `<section class="file-intervals" aria-label="文件时间区间"><div class="file-interval-heading">文件区间 <span>${rows.length}</span></div>${rows.map(x=>{const left=x.end&&span?(fileTimeMillis(x.start)-lo)/span*100:0,width=x.end?(span?(fileTimeMillis(x.end)-fileTimeMillis(x.start))/span*100:100):0,duplicate=rows.some(y=>y.id!==x.id&&y.file.name===x.file.name);return `<button class="file-interval" data-act="file-time-range" data-id="${E(x.id)}" ${x.end?'':'disabled'} title="${E(x.file.path||x.file.name)}" aria-label="${E(x.file.name)}：${E(x.startDisplay||'起始未知')} 至 ${E(x.endDisplay||'结束未知')}"><strong>${E(x.file.name)}</strong>${duplicate?`<span class="file-interval-path">${E(x.file.path||'')}</span>`:''}${compactFileTimes(x,x.file.lazy||x.file.loaded===false?'未解析':'无时间记录')}<span class="file-interval-track" aria-hidden="true"><i style="margin-left:${left}%;width:${width}%"></i></span></button>`;}).join('')}</section>`;}
window.addEventListener('click',function(e){const el=e.target.closest('[data-act="file-time-range"]');if(!el)return;e.preventDefault();e.stopImmediatePropagation();const f=files.find(f=>f.id===el.dataset.id&&!f.deleted);if(!f)return;const t=fileTimeRange(f);if(t.origin==='records'&&t.end)applyRange(t.start,t.end);},true);
function renderSidebar(){const nav=[['logs','logs','日志浏览'],['files','folder','文件库'],['rules','sliders','规则管理'],['global','calendar','全局时间筛选'],['compare','compare','时间对比']];return`<aside class="sidebar"><nav class="feature-card" aria-label="主功能">${nav.map(([id,ic,n])=>`<button class="feature-button ${S.view===id?'active':''}" data-act="${id==='global'?'global-open':id==='compare'?'compare-open':'nav'}" data-value="${id}" ${S.view===id?'aria-current="page"':''}>${I(ic)}<span>${n}</span></button>`).join('')}</nav><div class="section-head opened-title">已打开<span class="number">${S.opened.length}</span><span class="spacer"></span>${B('','nav','folder','icon ghost sm','data-value="files" aria-label="管理文件"')}</div><div class="sidebar-files" data-scroll="sidefiles">${S.opened.map(id=>files.find(f=>f.id===id&&!f.deleted)).filter(Boolean).map(f=>`<div class="side-file ${S.file===f.id?'active':''}" data-act="open-file" data-id="${E(f.id)}" tabindex="0" role="button"><span>${I(f.source==='local'?'laptop':'file')}</span><div class="grow"><div class="fname ellipsis" title="${E(f.name)}">${E(f.name)}</div><div class="fmeta">${f.source==='local'?'本机缓存':'服务端'} · ${f.lazy||f.loaded===false?'未解析':fmt(f.records.length)+' 条'}</div></div>${fileTimeCard(f)}<button class="close-file btn icon ghost sm" data-act="close-file" data-id="${E(f.id)}" aria-label="关闭 ${E(f.name)}">${I('close','sm')}</button></div>`).join('')}</div><div class="sidefoot"><button class="nav-button" data-act="nav" data-value="admin">${I('settings')}管理后台</button><button class="nav-button" data-act="help">${I('book')}操作帮助</button></div></aside>`;}
function renderLogWorkspace(){const rows=filtered();return`${renderFileTabs()}<section class="workspace" aria-label="日志工作台"><div class="toolbar"><div class="search-box">${I('search')}<input id="main-search" type="search" value="${E(S.search)}" placeholder="搜索日志…" aria-label="搜索日志"><div class="search-flags">${[['case','Aa','区分大小写'],['whole','Ab','整词匹配'],['regex','.*','正则表达式']].map(([id,n,title])=>`<button class="flag ${id==='whole'?'whole':''}" data-act="search-flag" data-value="${id}" aria-label="${title}" title="${title}" aria-pressed="${S.flags[id]}">${n}</button>`).join('')}</div></div>${B('视角','views-menu','eye',S.activeView?'active':'','id="views-button" title="选择或保存阅读视角"')}${B('筛选','filters','filter',(S.level||S.fn||S.software)?'active':'','id="filter-btn"')}${B(S.applied?short(S.applied.start).slice(0,5)+'–'+short(S.applied.end).slice(0,5):'时间范围',S.file==='global'?'global-open':'time-panel','calendar',S.applied?'time-active':'','id="time-button"')}${B('多规则','multi','layers',S.rules.length?'active':'','id="multi-button"')}<span class="sep"></span>${B('','toggle-timeline','timeline',`icon ${S.timeline?'active':''}`,'id="timeline-toggle" aria-label="显示或隐藏时间线" title="显示或隐藏时间线" aria-pressed="'+S.timeline+'"')}${B('','toggle-blocks',S.blocksDefault?'collapse':'expand','icon','id="blocks-toggle" aria-label="'+(S.blocksDefault?'收起全部多行块':'展开全部多行块')+'" title="'+(S.blocksDefault?'收起全部多行块':'展开全部多行块')+'"')}${B(S.rawMode?'结构化':'原文','toggle-raw','code',S.rawMode?'active':'','id="raw-toggle" title="切换原始日志并保持焦点" aria-pressed="'+S.rawMode+'"')}${B('显示','display','display','','id="display-btn"')}${B('','export','download','icon ghost','aria-label="导出当前结果" title="导出当前结果"')}</div>${S.searchError?`<div class="inline-error" role="alert">${E(S.searchError)}</div>`:''}${renderRuleStrip()}${S.file==='global'?`<div class="global-resultbar"><span>${I('globe','sm')}全局时间筛选</span><span>${selectedGlobalIds().length} 个文件 · ${E(S.applied?.start.slice(0,19)||'')} — ${E(S.applied?.end.slice(0,19)||'')}</span><span class="spacer"></span><button class="link" data-act="global-open">调整范围</button><button class="link" data-act="save-view">保存视角</button><button class="btn icon ghost sm" data-act="clear-global" aria-label="清除全局时间筛选">${I('close','sm')}</button></div>`:''}<div class="workpanel ${!S.timeline?'hide-timeline':''} ${S.inspector?'inspecting':''}">${renderTimeline()}<div class="log-column"><div class="reader-scroll" id="log-scroll" data-scroll="logs">${renderLogTable(rows)}</div>${renderPager(rows)}</div>${S.inspector?renderInspector():''}</div></section>`;}
function renderFileTabs(){let html=renderFileTabsLegacy().replace(/<div class="file-tab-tools">[\s\S]*?<\/div>/,''); // Remove duplicate reading controls if present.
// The legacy bar ends in a date + timeline/fold controls; only retain file tabs and the date.
html=html.replace(/<button[^>]*data-act="(?:toggle-timeline|fold-blocks)"[\s\S]*?<\/button>/g,'');if(S.globalSelection)html=html.replace('<div class="file-tabs" role="tablist" aria-label="已打开的日志">',`<div class="file-tabs" role="tablist" aria-label="已打开的日志"><button class="file-tab ${S.file==='global'?'active':''}" role="tab" aria-selected="${S.file==='global'}" data-act="global-result">${I('globe')}全局筛选 <span class="badge">${selectedGlobalIds().length} 文件</span></button>`);return html;}
function renderTimelineR4(){const all=baseRecords(),ev=all.filter(r=>(r.event||pointLabel(r))&&(!S.tlOnlyIssues||pointLabel(r)||['warning','error'].includes(r.event?.type)));S.timelinePage=Math.min(S.timelinePage,Math.max(0,Math.ceil(ev.length/150)-1));const shown=ev.slice(S.timelinePage*150,(S.timelinePage+1)*150),groups=[...new Set(shown.map(r=>r.phaseId))];return`<aside class="timeline ${S.mobileTimeline?'mobile-open':''}" aria-label="时间线"><div class="timeline-head">${I('timeline')}<strong>时间线</strong><span class="badge">${fmt(ev.length)}</span><span class="spacer"></span>${B('','timeline-rules','sliders','icon ghost sm','aria-label="时间线节点规则"')}</div><div class="timeline-controls"><div class="segmented">${[['events','事件'],['segments','区间']].map(([id,n])=>`<button data-act="timeline-tab" data-value="${id}" class="${S.timelineTab===id?'active':''}">${n}</button>`).join('')}</div>${B('','timeline-issues','filter',`icon ghost sm ${S.tlOnlyIssues?'active':''}`,'aria-label="仅显示异常节点"')}</div><div class="timeline-date"><span>${all[0]?.ts.slice(0,10)||'未载入'}</span><span>事件序列</span></div><div class="timeline-scroll" data-scroll="timeline">${S.timelineTab==='segments'?renderSegments():groups.map(g=>{const es=shown.filter(r=>r.phaseId===g);return`<section class="phase"><div class="phase-header"><strong>${E(es[0]?.phase||'日志事件')}</strong><span class="phase-line"></span></div><div class="tl-nodes">${es.map(r=>{const p=pointLabel(r),event=r.event||{type:'custom',label:p==='K'?'自定义关键点':p==='A'?'选定起点':'选定终点'};return`<div class="tl-node ${E(event.type)} ${S.selected===r.id?'current':''} ${p?'marked':''}" data-node="${E(r.id)}"><div class="tl-mark">${p?`<span class="pin-icon ${p==='K'?'key':''}">${p}</span>`:`<span class="node-dot ${E(event.type)}"></span>`}</div><div class="grow"><div class="tl-time-row"><button class="tl-time" data-act="point" data-id="${E(r.id)}">${short(r.ts)}</button>${event.type==='error'?'<span class="tiny-state">错误</span>':''}</div><button class="tl-label" data-act="locate" data-id="${E(r.id)}">${E(event.label)}</button><button class="tl-source" data-act="locate" data-id="${E(r.id)}" title="定位此记录，不是打开脚本"><span class="tl-source-name">${E(r.src)}:${r.srcLine}</span><span>· L${r.line}</span>${I('arrow','sm')}</button></div></div>`}).join('')}</div></section>`}).join('')||'<div class="hint empty-small">没有符合规则的节点</div>'}</div>${ev.length>150&&S.timelineTab==='events'?`<div class="timeline-pager">${B('上一组','tl-prev',null,'ghost sm',S.timelinePage===0?'disabled':'')}<span>${S.timelinePage+1}/${Math.ceil(ev.length/150)}</span>${B('下一组','tl-next',null,'ghost sm',(S.timelinePage+1)*150>=ev.length?'disabled':'')}</div>`:''}${renderRangeDraft()}<div class="timeline-foot">${I('clock')}名称 / 来源定位 · 时间选点</div></aside>`;}
class VirtualList{
constructor(el,items,renderItem,estimate,{head=0,measure=true,onScroll=null}={}){this.el=el;this.items=items;this.renderItem=renderItem;this.head=head;this.measure=measure;this.onScroll=onScroll;this.tree=new Engine.Heights(items.map(estimate));this.byId=new Map(items.map((x,i)=>[x.id??x.key,i]));this.region=el.querySelector('.virtual-region');this.content=el.querySelector('.virtual-items');this.start=-1;this.end=-1;this.dead=false;this.extra=0;this.scrollHandler=()=>{this.paint();this.onScroll?.(el.scrollTop);};el.addEventListener('scroll',this.scrollHandler,{passive:true});if(measure)this.observer=new ResizeObserver(()=>{if(!this.raf)this.raf=requestAnimationFrame(()=>{this.raf=null;this.measureNow();});});this.paint(true);}
paint(force=false){if(this.dead||!this.items.length)return;const top=Math.max(0,this.el.scrollTop-this.head),start=Math.max(0,this.tree.indexAt(top)-6),end=Math.min(this.items.length,this.tree.indexAt(top+this.el.clientHeight)+9);this.region.style.height=(this.tree.total+this.extra)+'px';if(!force&&start===this.start&&end===this.end)return;this.start=start;this.end=end;this.observer?.disconnect();this.content.style.transform=`translateY(${this.tree.sum(start)}px)`;this.content.innerHTML=this.items.slice(start,end).map((x,j)=>`<div class="virtual-item" data-vi="${start+j}">${this.renderItem(x,start+j)}</div>`).join('');if(this.measure){this.content.querySelectorAll('[data-vi]').forEach(x=>this.observer.observe(x));this.measureNow();}}
measureNow(){if(this.dead||!this.measure)return;const first=this.tree.indexAt(Math.max(0,this.el.scrollTop-this.head)),before=this.tree.sum(first);let changed=false;for(const node of this.content.children){const i=Number(node.dataset.vi),h=node.getBoundingClientRect().height;if(h>0&&Math.abs(this.tree.values[i]-h)>.5){this.tree.set(i,h);changed=true;}}if(changed){const delta=this.tree.sum(first)-before;if(Math.abs(delta)>.5)this.el.scrollTop+=delta;this.region.style.height=(this.tree.total+this.extra)+'px';this.content.style.transform=`translateY(${this.tree.sum(this.start)}px)`;}}
toId(id,y=40){const i=this.byId.get(id);if(i===undefined)return;const target=Math.max(0,this.head+this.tree.sum(i)-y);this.extra=Math.max(0,target+this.el.clientHeight-(this.tree.total+this.head));this.region.style.height=(this.tree.total+this.extra)+'px';this.el.scrollTop=target;this.paint(true);this.measureNow();const item=this.content.querySelector(`[data-vi="${i}"]`);if(item){const actual=item.getBoundingClientRect().top-this.el.getBoundingClientRect().top;this.el.scrollTop+=actual-y;}}
destroy(){this.dead=true;this.el.removeEventListener('scroll',this.scrollHandler);this.observer?.disconnect();cancelAnimationFrame(this.raf);}}
let currentLogItems=[];
function logItems(rows){const pages=Math.max(1,Math.ceil(rows.length/S.pageSize));S.page=Math.min(pages,Math.max(1,S.page));const pageRows=rows.slice((S.page-1)*S.pageSize,S.page*S.pageSize);if(!S.folded.size)return pageRows;const ints=intervals().filter(it=>S.folded.has(it.id)),out=[],seen=new Set();for(const r of pageRows){const it=ints.find(i=>r.key>i.start&&r.key<i.end);if(!it)out.push(r);else if(!seen.has(it.id)){out.push({id:'fold-'+it.id,fold:it});seen.add(it.id)}}return out;}
function columnStyleR4(){const rows=baseRecords().slice(0,800);const measure=(f,min,max)=>Math.max(min,Math.min(max,rows.reduce((n,r)=>Math.max(n,String(f(r)).length),0)*7.1+12));return `--col-time:${S.precision==='raw'?157:S.precision==='ms'?99:73}px;--col-level:${measure(r=>r.level,55,73)}px;--col-app:${measure(r=>r.logger,57,88)}px;--col-src:${measure(r=>r.src+':'+r.srcLine,98,139)}px;--col-fn:${measure(r=>r.fn+'()',86,151)}px;`;}
function renderLogTableR4(rows){currentLogItems=logItems(rows);if(!rows.length)return`<div class="empty"><span class="empty-ico">${I('search','lg')}</span><strong>没有匹配的日志</strong><p>调整时间、文件或搜索条件，原始文件不会改变。</p>${B('清除筛选','clear-all-filters','refresh')}</div>`;return`<div class="log-virtual ${S.rawMode?'raw-log':''}" style="${columnStyle()}" role="table" aria-label="${S.rawMode?'日志原文':'日志记录'}" aria-rowcount="${rows.length+1}"><div class="virtual-head ${S.rawMode?'raw-head':'log-grid'}" role="row">${S.rawMode?'<span>行号</span><span>日志原文 · 完整精度</span>':['行号','时间','等级','软件','脚本 : 行号','函数','日志内容',''].map(n=>`<span role="columnheader">${n}</span>`).join('')}</div><div class="virtual-region"><div class="virtual-items"></div></div></div>`;}
function recordHtmlR4(r,i){if(r.fold)return`<div class="fold-placeholder"><button data-act="fold-interval" data-id="${E(r.fold.id)}">${I('chevron','sm')}${short(r.fold.start)}–${short(r.fold.end)} · ${fmt(r.fold.records.length)} 条已折叠 ${I('expand','sm')}</button></div>`;const issue=issueOf(r),severity=r.issueSeverity||(['WARN','WARNING'].includes(r.level)?'warning':'error');if(S.rawMode)return`<article class="record-item raw-record ${S.selected===r.id?'selected':''} ${issue?severity:''}" data-record="${E(r.id)}" data-act="select-record" data-id="${E(r.id)}" tabindex="0" role="row" aria-selected="${S.selected===r.id}" aria-rowindex="${i+2}">${r.raw.map((text,j)=>`<div class="raw-line"><button class="raw-line-number" data-act="point" data-id="${E(r.id)}" aria-label="原文 L${r.line+j} 时间点">${r.line+j}</button><code>${E(text)||' '}</code></div>`).join('')}</article>`;
const p=pointLabel(r),multi=r.body.length>0;return`<article class="record-item ${S.selected===r.id?'selected':''} ${issue?severity:''}" data-record="${E(r.id)}" data-act="select-record" data-id="${E(r.id)}" tabindex="0" role="row" aria-selected="${S.selected===r.id}" aria-rowindex="${i+2}"><div class="log-grid log-row"><span class="numcol">${r.line}</span><span><button class="log-time" data-act="point" data-id="${E(r.id)}" title="${E(r.ts)}">${short(r.ts,S.precision)}</button>${p?`<b class="time-bound-inline">${p}</b>`:''}</span><span><span class="level ${E(r.level)}">${E(r.level)}</span></span><span class="software" title="${E(r.logger)}">${E(r.logger)}</span><span class="source-code" title="${E(r.src)}:${r.srcLine}">${E(r.src)}<b>:${r.srcLine}</b></span><span class="function-cell" title="${E(r.fn)}()">${E(r.fn)}()</span><span class="message-cell"><span class="message-inner"><span class="message-text" title="${E(r.message)}">${highlighted(r)}</span>${issue&&!['ERROR','WARNING','WARN','FATAL'].includes(r.level)?`<span class="rule-alert ${severity}">规则${severity==='warning'?'告警':'异常'}</span>`:''}${multi?`<button class="body-counter" data-act="expand-block" data-id="${E(r.id)}" aria-expanded="${blockOpen(r)}" title="${blockOpen(r)?'折叠':'展开全部'}多行字段">${r.kind==='kv'?r.fields.length+' 字段':r.raw.length+' 行'}${I(blockOpen(r)?'down':'chevron','sm')}</button>`:''}${S.queryHits?.[r.id]?.matched?.length?`<span class="match-count">${S.queryHits[r.id].matched.map(i=>`<i class="c${i%3+1}">${i+1}</i>`).join('')}</span>`:''}${S.bookmarks.has(r.id)?I('bookmark','sm'):''}</span></span><button class="row-more" data-act="inspect" data-id="${E(r.id)}" aria-label="查看记录 L${r.line}">${I('more')}</button></div>${multi&&blockOpen(r)?renderBody(r):''}</article>`;}
function renderBodyR4(r){const raw=S.bodyModes[r.id]==='raw';return`<section class="inline-block dense-block" data-block="${E(r.id)}"><div class="block-minihead"><span>${r.kind==='kv'?'结构化字段':r.kind==='mask'?'索引掩码':'多行正文'}</span><span class="muted mono">L${r.line}–${r.lineEnd}</span><span class="spacer"></span><button class="reader-link" data-act="body-mode" data-id="${E(r.id)}">${raw?'字段表':'块原文'}</button><button class="reader-link" data-act="copy-record" data-id="${E(r.id)}" aria-label="复制多行块">${I('copy','sm')}</button></div>${raw?`<pre class="raw-block">${E(r.raw.join('\n'))}</pre>`:r.kind==='kv'?`<div class="kv-grid dense-kv">${r.fields.map(([k,v],i)=>`<div class="kv"><span class="kv-label" title="${E(k)} · ${E(r.fieldMeta?.[i]?.rule||'')} · L${r.fieldMeta?.[i]?.line||r.line}">${E(k)}</span><span class="kv-value ${/^(true|false)$/i.test(v)?'bool':''}" title="${E(v)}">${E(v)}${r.fieldMeta?.[i]?.unit?` <small>${E(r.fieldMeta[i].unit)}</small>`:''}</span></div>`).join('')}</div>`:`<pre class="raw-block">${E(r.body.join('\n'))}</pre>`}</section>`;}
function estimateRecordR4(r){if(r.fold)return 34;if(S.rawMode)return Math.max(30,r.raw.length*19+12);const row=S.compact?29:34;if(!r.body.length||!blockOpen(r))return row;const width=$('#log-scroll')?.clientWidth||900;return row+(S.bodyModes[r.id]==='raw'||r.kind!=='kv'?r.raw.length*19+42:Math.ceil(r.fields.length/Math.max(1,Math.floor((width-70)/300)))*25+38);}
function mountLogVirtual(){const el=$('#log-scroll');if(!currentLogItems.length||!el?.querySelector('.virtual-region'))return;logVirtual=new VirtualList(el,currentLogItems,recordHtml,estimateRecord,{head:30});}
function renderPager(rows){const pages=Math.max(1,Math.ceil(rows.length/S.pageSize));return`<div class="page-foot"><span>${fmt(rows.length)} 条记录</span><span class="pager-info">${S.selected?'已选 L'+(recById(S.selected)?.line||''):'按需渲染 · 保留完整记录'}</span><div class="paging"><select id="page-size" aria-label="每页记录数">${[1000,5000,10000,30000,50000].map(n=>`<option value="${n}" ${S.pageSize===n?'selected':''}>${fmt(n)} 条 / 页</option>`).join('')}</select><button data-act="prev-page" aria-label="上一页" ${S.page<=1?'disabled':''}>${I('left')}</button><span class="mono">${S.page} / ${pages}</span><button data-act="next-page" aria-label="下一页" ${S.page>=pages?'disabled':''}>${I('chevron')}</button></div></div>`;}
function renderInspectorR4(){if(S.inspectorTab==='context')S.inspectorTab='fields';return renderInspectorLegacy().replace(/<button data-act="inspector-tab" data-value="context"[\s\S]*?<\/button>/,'');}
function locate(id){const r=recById(id);if(!r)return;S.view='logs';const rows=filtered(),at=rows.findIndex(x=>x.id===id);if(at<0){dialog('记录在筛选范围外',`<p>${E(r.src)}:${r.srcLine} · 原文 L${r.line}</p><p class="hint">定位到原文件会清除当前搜索和时间限制；取消则保持当前阅读结果。</p>`,B('取消','close-dialog')+B('在原文件定位','locate-unfiltered','arrow','primary',`data-id="${E(id)}"`));return;}S.selected=id;S.page=Math.floor(at/S.pageSize)+1;S.folded.clear();closePop(false);render(false);logVirtual?.toId(id,Math.max(60,($('#log-scroll')?.clientHeight||600)*.32));const row=$(`[data-record="${CSS.escape(id)}"]`);row?.focus({preventScroll:true});$(`[data-node="${CSS.escape(id)}"]`)?.scrollIntoView({block:'nearest'});}
function showViews(anchor){const views=load('views',[]);pop(`${popHead('阅读视角')}<div class="menu"><div class="menu-label">内置视角</div>${[['all','logs','完整日志'],['issues','error','异常优先'],['multiline','layers','多行数据块'],['bookmarks','bookmark','我的书签']].map(([id,ic,n])=>`<button class="menu-item" data-act="scope" data-value="${id}">${I(ic)}${n}<span class="spacer"></span>${S.scope===id?I('check','sm'):''}</button>`).join('')}<div class="menu-sep"></div><div class="menu-label">自定义 · ${views.length}</div>${views.map((v,i)=>`<div class="saved-view-row"><button class="menu-item" data-act="saved-view" data-index="${i}">${I(v.kind==='compare'?'compare':v.global?'globe':'eye')}<span class="grow ellipsis">${E(v.name)}</span><span class="badge">${v.kind==='compare'?'对比':v.global?'全局':'日志'}</span></button><button class="btn icon ghost sm" data-act="delete-view" data-index="${i}" aria-label="删除视角 ${E(v.name)}">${I('trash','sm')}</button></div>`).join('')||'<p class="hint empty-small">可保存文件范围、时间与当前阅读设置。</p>'}<div class="menu-sep"></div><button class="menu-item" data-act="save-view">${I('plus')}保存当前视角</button></div>`,anchor,'views','views-pop');}
function viewSnapshotR4(){return{kind:S.view==='compare'?'compare':'logs',files:S.view==='compare'?[...S.compare.ids]:[...S.opened],file:S.file,global:clone(S.globalSelection),compare:clone(S.compare),search:S.search,flags:clone(S.flags),rules:clone(S.rules),mode:S.ruleMode,scope:S.scope,applied:clone(S.applied),draft:clone(S.draft),rawMode:S.rawMode,precision:S.precision,pageSize:S.pageSize,timeline:S.timeline,theme:S.theme,level:S.level,software:S.software,fn:S.fn,module:S.module};}
function saveView(){S.viewDraft=viewSnapshot();dialog('保存阅读视角',`<div class="field"><label for="view-name" class="label">视角名称</label><input class="input" id="view-name" maxlength="60" value="${E(S.view==='compare'?'多文件时间对比':S.file==='global'?'跨文件时间筛选':'自定义阅读视角')}"></div><p class="hint">保存文件范围、起止时间、搜索与显示布局。不会把日志正文写入阅读设置。</p>`,B('取消','close-dialog')+B('保存视角','confirm-save-view','save','primary'));}
function restoreViewR4(v){const missing=(v.files||[]).filter(id=>!files.some(f=>f.id===id&&!f.deleted));if(missing.length){notify('视角中的 '+missing.length+' 个文件已不可用，请重新选择文件。',true);return;}S.activeView=v.name;S.search=v.search||'';S.flags=clone(v.flags||{case:false,whole:false,regex:false});S.rules=clone(v.rules||[]);S.ruleMode=v.mode||'or';S.scope=v.scope||'all';S.applied=clone(v.applied||null);S.draft=clone(v.draft||{start:null,end:null,keys:[]});S.globalSelection=clone(v.global||null);S.rawMode=!!v.rawMode;S.precision=v.precision||'sec';S.pageSize=v.pageSize||1000;S.timeline=v.timeline!==false;S.theme=v.theme||'ink';for(const k of['level','software','fn','module'])S[k]=v[k]||'';S.page=1;S.folded.clear();S.inspector=null;closePop(false);if(v.kind==='compare'){S.compare={...clone(v.compare),top:0};S.view='compare';render(false)}else{S.opened=[...(v.files||S.opened)];S.file=v.file||S.opened[0];S.view='logs';runSearch();}}
S.appliedProfiles=clone(S.profiles);
function processJobs(jobs){return new Promise((resolve,reject)=>{let w;const url=URL.createObjectURL(new Blob(['const Engine=('+LB4EngineFactory.toString()+')();onmessage=e=>{try{postMessage({results:e.data.jobs.map(j=>({id:j.id,records:Engine.applyProfile(j.records,j.profile)}))})}catch(e){postMessage({error:e.message})}}'],{type:'text/javascript'}));try{w=new Worker(url)}catch(e){URL.revokeObjectURL(url);reject(e);return;}URL.revokeObjectURL(url);const t=setTimeout(()=>{w.terminate();reject(Error('规则执行超过 4 秒，已终止；请简化正则或缩小样本。'))},4000);w.onmessage=e=>{clearTimeout(t);w.terminate();e.data.error?reject(Error(e.data.error)):resolve(e.data.results)};w.onerror=e=>{clearTimeout(t);w.terminate();reject(Error(e.message||'规则线程失败'))};w.postMessage({jobs});});}
function profileOptions(value){return S.appliedProfiles.filter(p=>!p.deleted).map(p=>`<option value="${E(p.id)}" ${value===p.id?'selected':''}>${E(p.name)} · v${E(p.version)}</option>`).join('');}
function renderFiles(){let html=renderFilesLegacy();const root=R6.roots.find(r=>r.id===R6.libraryRoot);if(S.libSource==='server'&&root?.state==='offline')html=html.replace('<div class="file-table-wrap"','<div class="library-root-warning" role="status">存储位置离线，恢复挂载后重试。</div><div class="file-table-wrap"');const path=S.folder||(S.libSource==='local'?'本机缓存':((R6.roots.find(r=>r.id===R6.libraryRoot)?.name||'')+':'));if(S.libSource==='local'||(S.libSource==='server'&&R6.libraryRoot!=='all')){const source=S.libSource,b=Engine.resolveProfile({id:'__directory__',path,source},S.appliedProfiles,S.dirBindings,{});html=html.replace('<div class="library-breadcrumb">',`<div class="directory-rulebar"><span>${I('sliders','sm')}目录默认规则</span><button class="binding" data-act="directory-rule" data-path="${E(path)}" data-source="${source}">${E(b.profile.name)}${I('down','sm')}</button><span class="hint">${S.dirBindings[source+':'+path]?'本目录指定':'继承 '+E(b.origin)}</span><span class="spacer"></span><span class="hint">单文件覆盖优先</span></div><div class="library-breadcrumb">`);}return html;}
function decorateBindings(){$$('[data-act="bind-file"]').forEach(el=>{const f=files.find(f=>f.id===el.dataset.id);if(!f)return;const b=libraryBinding(f);el.innerHTML=I(f.override?'sliders':'link')+E(b.name);el.title=b.source+' · '+b.origin;const subtitle=el.parentElement.querySelector('.file-path');if(subtitle)subtitle.textContent=b.source;});}
function bindingDialog(ids){S.bindingTarget={kind:'files',ids};const f=files.find(f=>f.id===ids[0]),b=f?resolved(f,S.dirBindings,S.fileBindings,S.appliedProfiles):null;dialog('文件规则',`<p class="hint">为 ${ids.length} 个文件选择已保存的解析规则。单文件设置优先于目录继承。</p><div class="field"><label class="label" for="binding-rule">规则版本</label><select class="select" id="binding-rule">${profileOptions(b?.profile.id)}</select></div><label class="check"><input type="checkbox" id="binding-inherit">清除文件覆盖，恢复继承目录</label><div id="binding-error" class="form-error" role="alert"></div>`,B('取消','close-dialog')+B('应用并重新提取','commit-binding','check','primary'));}
function directoryDialog(path,source){S.bindingTarget={kind:'directory',path,source};const key=source+':'+path,count=files.filter(f=>!f.deleted&&f.source===source&&Engine.isWithin(f.path,path)&&!S.fileBindings[f.id]).length;const b=Engine.resolveProfile({id:'_',path,source},S.appliedProfiles,S.dirBindings,{});dialog('目录默认规则',`<p class="binding-path">${I('folder')}${E(path)}</p><div class="field"><label class="label" for="binding-rule">默认解析规则</label><select class="select" id="binding-rule">${profileOptions(S.dirBindings[key]||b.profile.id)}</select></div><label class="check"><input type="checkbox" id="binding-inherit">移除本目录设置，继承上级</label><p class="hint" style="margin-top:12px">作用于此目录及子目录；子目录单独设置和文件覆盖不受影响。当前范围内 ${count} 个文件没有单文件覆盖。</p><p class="hint">个人设置写入工作区，不修改共享配置。</p><div id="binding-error" class="form-error" role="alert"></div>`,B('取消','close-dialog')+B('应用并重新提取','commit-binding','check','primary'));}
async function commitBinding(button){const target=S.bindingTarget,value=$('#binding-rule').value,inherit=$('#binding-inherit').checked,dirs={...S.dirBindings},bindings={...S.fileBindings};if(target.kind==='directory'){const key=target.source+':'+target.path;if(inherit)delete dirs[key];else dirs[key]=value}else for(const id of target.ids){if(inherit)delete bindings[id];else bindings[id]=value;}const changed=files.filter(f=>!f.deleted&&Engine.resolveProfile(f,S.appliedProfiles,dirs,bindings).profile.id!==Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile.id);button.disabled=true;button.textContent='应用中…';try{const results=await processJobs(changed.map(f=>({id:f.id,records:f.records,profile:Engine.resolveProfile(f,S.appliedProfiles,dirs,bindings).profile})));S.dirBindings=dirs;S.fileBindings=bindings;save('dirBindings',dirs);save('fileBindings',bindings);for(const res of results){files.find(f=>f.id===res.id).records=res.records;}syncBindingLabels();indexRecords();$('#dialog').close();S.queryHits=null;render();notify('规则已应用，重新提取 '+results.length+' 个文件。原始日志未修改。');}catch(e){$('#binding-error').textContent=e.message;button.disabled=false;button.textContent='重新应用';}}
function syncBindingLabels(){for(const f of files){const b=Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings);f.profile=b.profile.name;f.ruleOrigin=b.origin;f.override=b.source==='文件单独指定';}}
function renderRules(){const ps=S.profiles.filter(p=>!!p.deleted===S.profileTrash),p=profile();p.extractors||=(Engine.defaults().extractors);p.anomalies||=(Engine.defaults().anomalies);p.nodes||=clone(defaultNodes);S.nodeRules=p.nodes;return`<div class="page-heading"><h1>规则管理</h1><span class="badge">${ps.length} 套</span><span class="spacer"></span><span class="hint">规则库</span>${B('回收站','profile-trash','trash','ghost')}${B('新建规则','new-profile','plus','primary')}</div><section class="rule-library-layout"><aside class="profiles"><div class="profiles-toolbar">${I('sliders')}规则配置<span class="spacer"></span></div><div class="profile-list" data-scroll="profiles">${ps.map(q=>`<button class="profile-item ${q.id===S.profile?'active':''}" data-act="choose-profile" data-id="${E(q.id)}"><div class="profile-title">${I('file','sm')}${E(q.name)}</div><div class="profile-meta">v${E(q.version)} · ${q.deleted?'已删除':'已保存版本'}</div><p class="profile-description">${E(q.desc||'自定义提取与判定')}</p></button>`).join('')}</div><div class="profiles-footer">字段提取 → 异常判定 → 时间线<br>修改先试跑，再保存应用。</div></aside><div class="rule-work"><div class="rule-work-head"><h2>${E(p.name)}</h2><span class="badge ${S.dirty?'amber':''}">${S.dirty?'未保存':'v'+E(p.version)}</span><span class="spacer"></span>${B('复制','copy-profile','copy','ghost sm')}${B('','delete-profile','trash','icon ghost sm','aria-label="删除规则"')}${p.deleted?B('恢复','restore-profile','restore','primary sm'):B(S.ruleBusy?'保存中…':'保存并应用','save-profile','save','primary sm',S.ruleBusy?'disabled':'')}</div><div class="rule-work-tabs">${[['parse','解析格式'],['fields','字段提取'],['errors','异常判定'],['nodes','时间线节点'],['display','显示配色']].map(([id,n])=>`<button data-act="rule-section" data-value="${id}" class="${S.ruleSection===id?'active':''}">${n}</button>`).join('')}</div><div class="rule-editor-split"><div class="rule-editor" data-scroll="rule-editor">${renderRuleEditor(p)}</div><aside class="test-pane"><div class="test-pane-head">${I('play','sm')}手动试跑<span class="spacer"></span><button class="link" data-act="test-sample">填入样例</button></div><label class="label" for="rule-test-input">原始日志</label><textarea class="textarea" id="rule-test-input" spellcheck="false" placeholder="粘贴包含头部与续行的日志…">${E(S.testInput??files[0].records.slice(11,15).map(r=>r.raw.join('\n')).join('\n'))}</textarea>${B(S.testBusy?'试跑中…':'试跑当前草稿','run-test','play','primary block',S.testBusy?'disabled':'')}<div id="test-output">${renderTestResult()}</div><p class="hint test-help">使用当前未保存草稿。试跑不改变已打开日志；保存后，绑定本规则的文件才重新提取。</p></aside></div></div></section>`;}
function renderRuleEditorR4(p){if(S.ruleSection==='fields'){return`<div class="editor-title"><h3>字段提取</h3>${B('新增提取规则','edit-extractor','plus','sm','data-index="-1"')}</div><p class="description">从消息正文、续行或完整记录中提取字段。原始文本始终保留；提取结果供异常判断、详情与后续视角使用。</p><details class="rule-guide"><summary>作用方式与规则写法</summary><div><p>依次执行已启用规则。先用正则定位，再用捕获组生成字段名和值。同名字段重复出现时保留每一项，不覆盖前值。</p><p>例：正文 <code>PPe: 4.5mJ</code>，表达式 <code>PPe:\\s*(?&lt;value&gt;[\\d.]+)</code>，固定字段名 <code>PPe</code>，取值组 <code>value</code>，单位 <code>mJ</code>，得到 <code>PPe = 4.5</code>。</p><p>动态字段使用 <code>(?&lt;key&gt;…)</code> 与 <code>(?&lt;value&gt;…)</code>。采用 JavaScript 正则，全局、逐行、不区分大小写匹配。类型只作标注，数值原串不转换或截断。</p></div></details><div class="editable-rules">${p.extractors.map((q,i)=>`<div class="editable-rule"><label class="check"><input type="checkbox" data-enable-extractor="${i}" ${q.enabled!==false?'checked':''} aria-label="启用 ${E(q.name)}"></label><div class="grow"><div class="rule-name">${E(q.name)} <span class="badge">${{message:'消息正文',body:'多行续行',both:'正文＋续行',raw:'完整原文'}[q.source]}</span></div><code class="rule-pattern">${E(q.pattern)}</code><div class="hint">输出：${q.key?E(q.key):'key 捕获组'} ← ${E(q.group||'value')} · ${E(q.type||'auto')}${q.unit?' · '+E(q.unit):''}</div></div>${B('编辑','edit-extractor','edit','ghost sm',`data-index="${i}"`)}${B('','delete-extractor','trash','icon ghost sm',`data-index="${i}" aria-label="删除 ${E(q.name)}"`)}</div>`).join('')||'<p class="hint empty-small">没有提取规则。原始记录仍可阅读。</p>'}</div>`;}
if(S.ruleSection==='errors')return`<div class="editor-title"><h3>异常判定</h3>${B('新增异常规则','edit-anomaly','plus','sm','data-index="-1"')}</div><p class="description">先提取字段，再判断条件。命中会添加派生异常及证据，不改写日志原始等级。</p><details class="rule-guide"><summary>条件、优先级与写法</summary><div><p><code>fields.err_code</code> → 非零，可排除 <code>0</code> 与 <code>0x0</code>；<code>fields.temp</code> → 大于 <code>80</code>，标记告警。</p><p>可匹配 <code>level</code>、<code>message</code>、<code>body</code>、<code>raw</code> 或 <code>fields.字段名</code>。多条规则同时命中时保留全部依据，错误展示优先于告警。字段不存在则不命中。</p><p>新增规则先在右侧输入正例与反例试跑，检查命中字段和值，再保存。正则失败或超时会中止，不覆盖当前日志结果。</p></div></details><div class="editable-rules">${p.anomalies.map((q,i)=>`<div class="editable-rule"><label class="check"><input type="checkbox" data-enable-anomaly="${i}" ${q.enabled!==false?'checked':''} aria-label="启用 ${E(q.name)}"></label><div class="grow"><div class="rule-name">${E(q.name)} <span class="badge ${q.severity==='error'?'red':'amber'}">${q.severity==='error'?'错误':'告警'}</span></div><code class="rule-pattern">${E(q.field)} ${E(opLabel(q.op))} ${E(q.value||'')}</code></div>${B('编辑','edit-anomaly','edit','ghost sm',`data-index="${i}"`)}${B('','delete-anomaly','trash','icon ghost sm',`data-index="${i}" aria-label="删除 ${E(q.name)}"`)}</div>`).join('')}</div>`;
if(S.ruleSection==='parse')return`<h3>记录边界</h3><p class="description">使用已验证的方括号格式。可在字段提取页扩展正文解析，不执行任意 Python 或 JavaScript 代码。</p><div class="field"><label class="label" for="profile-name">规则名称</label><input class="input" id="profile-name" value="${E(p.name)}"></div><div class="rule-examples">[时间] [等级] [软件] [PID] [TID]<br>[脚本:行号] [f7] [triple] function() &lt;TAG&gt; 正文</div><p class="hint">UTF-8；无头续行归属上一条记录，保留原文。新日志头部语法仍需后续解析适配器接入。</p>`;
return renderRuleEditorLegacy(p);}
function opLabel(op){return{equals:'等于',contains:'包含',regex:'匹配正则',nonzero:'非零',gt:'>',gte:'≥',lt:'<',lte:'≤',exists:'存在'}[op]||op;}
function editExtractor(i){const q=clone(profile().extractors[i]||{name:'',source:'message',pattern:'PPe:\\s*(?<value>[\\d.]+)',key:'PPe',group:'value',type:'number',unit:'mJ',enabled:true});S.editIndex=i;dialog(i<0?'新增字段提取规则':'编辑字段提取规则',`<div class="filter-grid"><div class="field"><label class="label" for="ex-name">规则名称</label><input class="input" id="ex-name" value="${E(q.name)}" placeholder="例如：提取脉冲能量"></div><div class="field"><label class="label" for="ex-source">提取来源</label><select class="select" id="ex-source">${[['message','消息正文'],['body','多行续行'],['both','正文＋续行'],['raw','完整原文']].map(([v,n])=>`<option value="${v}" ${v===q.source?'selected':''}>${n}</option>`).join('')}</select></div></div><div class="field"><label class="label" for="ex-pattern">匹配表达式</label><textarea class="code-input" id="ex-pattern" spellcheck="false">${E(q.pattern)}</textarea></div><div class="filter-grid"><div class="field"><label class="label" for="ex-key">固定字段名</label><input class="input mono" id="ex-key" value="${E(q.key)}" placeholder="留空使用 key 捕获组"></div><div class="field"><label class="label" for="ex-group">取值捕获组</label><input class="input mono" id="ex-group" value="${E(q.group||'value')}" placeholder="value 或数字组号"></div><div class="field"><label class="label" for="ex-type">类型标注</label><select class="select" id="ex-type">${['auto','string','number','boolean','hex'].map(t=>`<option ${q.type===t?'selected':''}>${t}</option>`).join('')}</select></div><div class="field"><label class="label" for="ex-unit">单位（可空）</label><input class="input" id="ex-unit" value="${E(q.unit)}"></div></div><p class="hint">保存到草稿后，可在右侧手动试跑；“保存并应用”才更新绑定文件。</p><div id="edit-rule-error" class="form-error" role="alert"></div>`,B('取消','close-dialog')+B('保存到草稿','commit-extractor','check','primary'),true);}
function editAnomaly(i){const q=clone(profile().anomalies[i]||{name:'',field:'fields.err_code',op:'nonzero',value:'',severity:'error',enabled:true});S.editIndex=i;dialog(i<0?'新增异常判断':'编辑异常判断',`<div class="field"><label class="label" for="an-name">规则名称</label><input class="input" id="an-name" value="${E(q.name)}" placeholder="例如：温度超限"></div><div class="filter-grid"><div class="field"><label class="label" for="an-field">判断字段</label><input class="input mono" id="an-field" list="an-field-options" value="${E(q.field)}"><datalist id="an-field-options">${['level','message','body','raw','fields.err_code','fields.rpc_success','fields.isValid'].map(x=>`<option value="${x}">`).join('')}</datalist></div><div class="field"><label class="label" for="an-op">条件</label><select class="select" id="an-op">${['equals','contains','regex','nonzero','gt','gte','lt','lte','exists'].map(x=>`<option value="${x}" ${q.op===x?'selected':''}>${opLabel(x)}</option>`).join('')}</select></div><div class="field"><label class="label" for="an-value">比较值 / 正则</label><input class="input mono" id="an-value" value="${E(q.value)}" placeholder="非零 / 存在条件无需填写"></div><div class="field"><label class="label" for="an-severity">派生严重度</label><select class="select" id="an-severity"><option value="error" ${q.severity==='error'?'selected':''}>错误</option><option value="warning" ${q.severity==='warning'?'selected':''}>告警</option></select></div></div><p class="hint">命中时保留“规则名、字段、原始值”作为依据。不会覆盖日志原始等级。</p><div id="edit-rule-error" class="form-error" role="alert"></div>`,B('取消','close-dialog')+B('保存到草稿','commit-anomaly','check','primary'),true);}
function renderTestResult(){if(S.testError)return`<div class="form-error" role="alert">${E(S.testError)}</div>`;if(!S.testResult?.results)return'';const rs=S.testResult.results;return`<div class="test-stats"><span><b>${rs.length}</b> 记录</span><span><b>${rs.reduce((n,r)=>n+r.fields.length,0)}</b> 字段</span><span><b>${rs.filter(r=>r.issue).length}</b> 异常</span></div><div class="test-result">${rs.map((r,i)=>`<details class="test-record" ${i===0?'open':''}><summary><code>L${r.line}</code><span>${E(r.fn)}</span>${r.issue?'<span class="badge '+(r.issueSeverity==='error'?'red':'amber')+'">'+E(r.issueSeverity==='error'?'错误':'告警')+'</span>':'<span class="badge">未命中异常</span>'}</summary><p class="test-message mono">${E(r.message)}</p>${r.fieldMeta.map(f=>`<div class="test-field"><code>${E(f.key)}</code><code>${E(f.value)}</code><small>${E(f.rule)} · ${f.source} L${f.line}</small></div>`).join('')}${r.issueMatches.map(m=>`<div class="test-evidence">${I('warning','sm')}<span>${E(m.name)}<br><code>${E(m.field)} = ${E(m.value)}</code></span></div>`).join('')}${r.event?`<small class="hint">时间线：${E(r.event.label)}</small>`:''}</details>`).join('')}</div>`;}
async function runTest(){if(S.testBusy)return;S.testInput=$('#rule-test-input')?.value||'';if(S.testInput.length>200000){notify('试跑样本最多 200 KB。',true);return;}S.testBusy=true;S.testError='';S.testResult=null;const records=parseLocal(S.testInput,'test'),p=clone(profile());render();try{const result=await processJobs([{id:'test',records,profile:p}]);S.testResult={results:result[0].records};}catch(e){S.testError=e.message;}finally{S.testBusy=false;render();}}
async function saveProfile(){if(S.ruleBusy)return;const p=clone(profile());p.nodes=clone(S.nodeRules);try{Engine.validate(p)}catch(e){notify(e.message,true);return;}p.version=(Number(p.version||'1.0')+.1).toFixed(1);const targets=files.filter(f=>!f.deleted&&resolved(f).profile.id===p.id);S.ruleBusy=true;render();try{const out=await processJobs(targets.map(f=>({id:f.id,records:f.records,profile:p})));for(const x of out)files.find(f=>f.id===x.id).records=x.records;S.profiles=S.profiles.map(q=>q.id===p.id?p:q);{const i=S.appliedProfiles.findIndex(q=>q.id===p.id);if(i<0)S.appliedProfiles.push(clone(p));else S.appliedProfiles[i]=clone(p);}save('profiles',S.appliedProfiles);S.dirty=false;S.dirtyProfiles?.delete(p.id);S.testResult=null;indexRecords();syncBindingLabels();S.queryHits=null;notify('规则已保存，并重新提取 '+targets.length+' 个绑定文件。');}catch(e){notify(e.message,true);}finally{S.ruleBusy=false;render();}}
function boundsOf(fs){let start=null,end=null;for(const f of fs)for(const r of f.records)if(r.key){if(!start||r.key<start)start=r.key;if(!end||r.key>end)end=r.key;}return{start:start||norm('2026-10-26 00:00:00'),end:end||norm('2026-10-26 23:59:59')};}
function openPicker(kind='global'){const existing=kind==='global'?S.globalSelection:null;const ids=kind==='compare'?(S.compare.ids.length?S.compare.ids:S.opened.slice(0,4)):existing?selectedGlobalIds():[...S.opened];const b=boundsOf(files.filter(f=>ids.includes(f.id)&&!f.deleted));const range=kind==='compare'?S.compare.range:existing?S.applied:null;S.picker={kind,ids:new Set(ids),folders:new Set(existing?.folders||[]),query:'',start:range?.start||b.start,end:range?.end||b.end};renderPickerDialog();}
function pickerTimes(){const p=S.picker,a=p.start,z=p.end;return`<div class="picker-time"><div class="picker-section-title">${I('calendar')}时间段</div><div class="field"><label class="label" for="global-start-date">起始时间</label><div class="date-time-row"><input class="input" id="global-start-date" type="date" value="${a.slice(0,10)}" aria-label="全局起始日期"><input class="input mono" id="global-start-time" type="time" step="1" value="${short(a)}" aria-label="全局起始时间"></div></div><div class="field"><label class="label" for="global-end-date">结束时间</label><div class="date-time-row"><input class="input" id="global-end-date" type="date" value="${z.slice(0,10)}" aria-label="全局结束日期"><input class="input mono" id="global-end-time" type="time" step="1" value="${short(z)}" aria-label="全局结束时间"></div></div><details class="fraction-details"><summary>小数秒（保留原始精度）</summary><label class="label" for="global-start-frac">起点小数秒</label><input class="input mono" id="global-start-frac" maxlength="9" value="${a.split('.')[1]||''}"><label class="label" for="global-end-frac">终点小数秒</label><input class="input mono" id="global-end-frac" maxlength="9" value="${z.split('.')[1]||''}"></details><div class="picker-actions">${B('所选文件全部时间','picker-full-time','clock','sm')}</div><div class="range-caption">${I('info','sm')}按日志原始时间，不自动转换时区。</div><div class="picker-section-title recent-title">最近使用 / 收藏</div><div class="picker-recents">${S.recents.slice(0,5).map((r,i)=>`<div class="recent-range"><button data-act="picker-recent" data-index="${i}">${r.start.slice(5,10)} ${short(r.start)}–${short(r.end)}</button><button class="btn icon ghost sm ${r.favorite?'starred':''}" data-act="picker-star" data-index="${i}" aria-label="收藏或取消收藏时间段">${I('star','sm')}</button></div>`).join('')||'<p class="hint">应用后的时间范围会记录在这里。</p>'}</div></div>`;}
function renderPickerDialog(){const p=S.picker;dialog(p.kind==='global'?'全局时间筛选':'选择时间对比文件',`<div class="global-picker">${pickerTimes()}<section class="picker-files"><div class="picker-section-title">${I('folder')}文件范围<span class="spacer"></span><span id="picker-count" class="badge">已选 ${p.ids.size} 个</span></div><div class="search-box picker-search">${I('search')}<input id="picker-search" placeholder="搜索文件名或目录…" aria-label="搜索候选日志"></div><div class="picker-selectbar"><span>${p.kind==='compare'?'选择 2–4 个文件':'勾选文件夹将包含所有子目录'}</span><span class="spacer"></span><button class="link" data-act="picker-clear">清空选择</button></div><div class="picker-tree" id="picker-tree">${renderPickTree()}</div><div class="hint picker-boundary">服务器日志库来自 /api/v2/library；本机缓存只读取当前浏览器数据。</div></section></div><div id="picker-error" class="form-error" role="alert"></div>${p.kind==='global'?'<label class="check keep-search"><input type="checkbox" id="picker-keep-search">沿用当前搜索与字段筛选</label>':''}`,B('取消','close-dialog')+B(p.kind==='compare'?'开始时间对比':'应用到日志浏览','apply-picker','check','primary'),true);$('#dialog').classList.add('scope-dialog');updatePickerChecks();}
function fileTree(){const roots=[];for(const source of['local','server']){const fs=files.filter(f=>!f.deleted&&f.source===source);const root={key:source+':',source,name:source==='local'?'本机缓存':'服务器日志库',path:'',children:new Map(),files:[]};for(const f of fs){const parts=f.path.split('/').filter(Boolean);if(source==='local'&&parts[0]==='本机缓存')parts.shift();let node=root;const prefix=source==='local'?['本机缓存']:[];for(const part of parts){prefix.push(part);if(!node.children.has(part))node.children.set(part,{key:source+':'+prefix.join('/'),source,name:part,path:prefix.join('/'),children:new Map(),files:[]});node=node.children.get(part);}node.files.push(f);}roots.push(root);}return roots;}
function descendants(n){return[...n.files,...[...n.children.values()].flatMap(descendants)];}
function renderPickTree(){const p=S.picker,q=p.query.toLowerCase();function draw(n,depth=0){const all=descendants(n),matches=all.filter(f=>!q||(f.name+' '+f.path).toLowerCase().includes(q));if(!matches.length)return'';return`<details class="pick-directory depth-${depth}" open><summary><span class="pick-chevron">${I('chevron','sm')}</span><input type="checkbox" data-pick-folder="${E(n.key)}" aria-label="选择目录 ${E(n.name)}"><span>${I(depth===0?(n.source==='local'?'laptop':'server'):'folder','sm')}</span><span class="grow">${E(n.name)}</span><span class="muted">${all.length}</span></summary><div>${[...n.children.values()].map(c=>draw(c,depth+1)).join('')}${n.files.filter(f=>!q||(f.name+' '+f.path).toLowerCase().includes(q)).map(f=>`<label class="pick-file"><input type="checkbox" data-pick-file="${E(f.id)}" ${p.ids.has(f.id)?'checked':''}>${I('file','sm')}<span class="grow"><span class="pick-filename">${E(f.name)}</span><small>${f.records[0]?.ts.slice(11,19)||'无有效时间'} – ${f.records.at(-1)?.ts.slice(11,19)||''} · ${fmt(f.records.length)} 条${f.sample?' · 样例':''}</small></span></label>`).join('')}</div></details>`;}return fileTree().map(n=>draw(n)).join('')||'<p class="hint empty-small">没有匹配文件。搜索不会清除已选范围。</p>';}
function pickerFolderFiles(key){const colon=key.indexOf(':'),source=key.slice(0,colon),path=key.slice(colon+1);return files.filter(f=>!f.deleted&&f.source===source&&(!path||Engine.isWithin(f.path,path)));}
function updatePickerChecks(){const p=S.picker;if(!p)return;$$('[data-pick-folder]').forEach(el=>{const fs=pickerFolderFiles(el.dataset.pickFolder),n=fs.filter(f=>p.ids.has(f.id)).length;el.checked=n===fs.length&&n>0;el.indeterminate=n>0&&n<fs.length;});$('#picker-count')?.replaceChildren(document.createTextNode('已选 '+p.ids.size+' 个'));const b=$('[data-act="apply-picker"]');if(b)b.disabled=p.kind==='compare'?(p.ids.size<2||p.ids.size>4):!p.ids.size;const err=$('#picker-error');if(err)err.textContent=p.kind==='compare'&&p.ids.size>4?'时间对比最多选择 4 个文件，请取消多余选择。':'';}
function refreshPickerTree(){const el=$('#picker-tree');if(!el)return;el.innerHTML=renderPickTree();updatePickerChecks();}
function updatePickerTime(a,b){for(const [prefix,key] of[['global-start',a],['global-end',b]]){$('#'+prefix+'-date').value=key.slice(0,10);$('#'+prefix+'-time').value=short(key);$('#'+prefix+'-frac').value=key.split('.')[1]||'';}S.picker.start=a;S.picker.end=b;}
function applyPicker(){const p=S.picker;let a,b;try{a=timeFormValue('global-start');b=timeFormValue('global-end');if(a>b)throw Error('结束时间不能早于起始时间。');if(!p.ids.size)throw Error('至少选择一个文件。');if(p.kind==='compare'&&(p.ids.size<2||p.ids.size>4))throw Error('请选择 2–4 个对比文件。');}catch(e){$('#picker-error').textContent=e.message;return;}addRecent(a,b);if(p.kind==='global'){const keep=$('#picker-keep-search')?.checked;if(!keep)clearAllFilters(false);S.globalSelection={fileIds:[...p.ids],folders:[...p.folders]};S.applied={start:a,end:b};S.draft={start:a,end:b,keys:[]};S.opened=[...new Set([...S.opened,...p.ids])];S.file='global';S.view='logs';S.page=1;S.inspector=null;S.queryHits=null;S.activeView=null;$('#dialog').close();runSearch();}else{S.compare={ids:[...p.ids],layout:p.ids.size===4?'grid':'columns',sync:true,top:0,range:{start:a,end:b}};S.view='compare';S.inspector=null;compareAxis=null;$('#dialog').close();render(false);}S.picker=null;}
function getCompareAxis(){const signature=dataGeneration+'|'+S.compare.ids.join(',')+'|'+JSON.stringify(S.compare.range);if(compareAxis?.signature===signature)return compareAxis.items;const fs=S.compare.ids.map(id=>files.find(f=>f.id===id&&!f.deleted)).filter(Boolean);const items=Engine.align(fs,S.compare.range?.start,S.compare.range?.end).map(x=>({...x,id:x.key,height:24+Math.max(1,...Object.values(x.cells).map(v=>v.length))*49}));compareAxis={signature,items};return items;}
function renderCompare(){const fs=S.compare.ids.map(id=>files.find(f=>f.id===id&&!f.deleted)).filter(Boolean);if(fs.length<2)return`<section class="empty compare-empty"><span class="empty-ico">${I('compare','lg')}</span><strong>选择 2–4 个文件进行时间对比</strong><p>以同一条时间轴对齐；某文件没有记录的时刻会留空，不把相邻时间误作同时发生。</p>${B('选择文件','compare-choose','folder','primary')}</section>`;const axis=getCompareAxis();return`<section class="compare-workspace"><div class="compare-toolbar"><strong>${I('compare')}时间对比</strong><span class="badge">${fs.length} 文件</span>${B('文件与时间','compare-choose','sliders','sm')}<div class="segmented compare-layout">${[['columns','列排'],['rows','横排'],...(fs.length===4?[['grid','2 × 2']]:[])].map(([id,n])=>`<button data-act="compare-layout" data-value="${id}" class="${S.compare.layout===id?'active':''}">${n}</button>`).join('')}</div><label class="check"><input type="checkbox" id="compare-sync" ${S.compare.sync?'checked':''}>同步滚动</label><span class="spacer"></span>${B('视角','views-menu','eye','sm')}${B('保存视角','save-view','save','sm')}</div><div class="compare-timebar"><span>共同时间轴 · 精确时刻对齐</span><span class="mono" id="compare-position">${E(axis[0]?.key.slice(0,26)||'无匹配记录')}</span><span class="spacer"></span><input id="compare-seek" class="input mono" type="time" step="1" aria-label="跳转到对比时间" value="${short(axis[0]?.key||'00:00:00')}">${B('跳转','compare-seek',null,'sm')}</div><div class="compare-grid layout-${S.compare.layout}">${fs.map((f,i)=>`<section class="compare-pane"><div class="compare-pane-head"><span class="compare-file-tag">${i+1}</span><div class="grow"><strong title="${E(f.path+'/'+f.name)}">${E(f.name)}</strong><small>${f.source==='local'?'本机缓存':'服务端'} · ${E(f.profile)}</small></div>${B('','compare-one-file','external','icon ghost sm',`data-id="${E(f.id)}" aria-label="单独阅读 ${E(f.name)}"`)}</div><div class="comparison-scroll" data-compare-file="${E(f.id)}" role="region" tabindex="0" aria-label="${E(f.name)} 时间对比">${axis.length?'<div class="virtual-region"><div class="virtual-items"></div></div>':'<div class="empty"><strong>时间范围内没有记录</strong><p>调整文件与时间范围后重试。</p></div>'}</div></section>`).join('')}</div><div class="compare-foot"><span>${fmt(axis.length)} 个共同时间点 · 无记录时留空</span><span class="spacer"></span><span>${E(S.compare.range?.start.slice(0,19)||'')} — ${E(S.compare.range?.end.slice(0,19)||'')}</span></div></section>`;}
function comparisonCell(slot,fileId){const rows=slot.cells[fileId]||[];return`<div class="comparison-slot" data-time-key="${E(slot.key)}" style="height:${slot.height}px"><div class="comparison-stamp">${E(short(slot.key,S.precision))}<span>${slot.key.slice(0,10)}</span></div>${rows.length?rows.map(r=>`<button class="comparison-record ${r.issueSeverity||''}" data-act="compare-record" data-id="${E(r.id)}"><span class="compare-record-meta"><b class="level ${E(r.level)}">${E(r.level)}</b><span>${E(r.src)}:${r.srcLine}</span><small>L${r.line}</small>${r.body.length?'<em>'+r.raw.length+' 行</em>':''}</span><span class="compare-message mono">${tokenText(r.message)}</span></button>`).join(''):'<div class="comparison-gap">该时刻无记录</div>'}</div>`;}
function mountCompare(){const axis=getCompareAxis();if(!axis.length)return;let syncing=false;const onScroll=(source,top)=>{S.compare.top=top;const idx=compareVirtuals[0]?.tree.indexAt(top)||0;const label=$('#compare-position');if(label)label.textContent=axis[idx]?.key||'';if(syncing||!S.compare.sync)return;syncing=true;for(const v of compareVirtuals)if(v.el!==source&&Math.abs(v.el.scrollTop-top)>.5){v.el.scrollTop=top;v.paint();}syncing=false;};$$('[data-compare-file]').forEach(el=>{const v=new VirtualList(el,axis,x=>comparisonCell(x,el.dataset.compareFile),x=>x.height,{measure:false,onScroll:top=>onScroll(el,top)});compareVirtuals.push(v);el.scrollTop=S.compare.top||0;v.paint(true);});}
function markDirty(){S.dirty=true;S.dirtyProfiles??=new Set();S.dirtyProfiles.add(S.profile);}
function fieldErrorText(e){const el=$('#edit-rule-error');if(el)el.textContent=e.message;else notify(e.message,true);}
document.addEventListener('click',async e=>{const el=e.target.closest('[data-act]');if(!el)return;const act=el.dataset.act,id=el.dataset.id,v=el.dataset.value,ix=Number(el.dataset.index);const owned=new Set(['global-open','compare-open','compare-choose','apply-picker','picker-clear','picker-full-time','picker-recent','picker-star','global-result','clear-global','views-menu','confirm-save-view','saved-view','delete-view','toggle-raw','toggle-timeline','toggle-blocks','expand-block','select-record','body-mode','locate-unfiltered','directory-rule','commit-binding','edit-extractor','commit-extractor','delete-extractor','edit-anomaly','commit-anomaly','delete-anomaly','compare-layout','compare-seek','compare-record','compare-one-file','choose-profile','copy-profile','confirm-delete-profile','restore-profile','clear-all-filters']);if(!owned.has(act))return;e.preventDefault();e.stopImmediatePropagation();try{
switch(act){
case'global-open':openPicker('global');break;
case'compare-open':if(S.compare.ids.length>=2){S.view='compare';render(false)}else openPicker('compare');break;
case'compare-choose':openPicker('compare');break;
case'apply-picker':applyPicker();break;
case'picker-clear':S.picker.ids.clear();S.picker.folders.clear();refreshPickerTree();break;
case'picker-full-time':{const b=boundsOf(files.filter(f=>S.picker.ids.has(f.id)&&!f.deleted));updatePickerTime(b.start,b.end);break;}
case'picker-recent':{const r=S.recents[ix];if(r)updatePickerTime(r.start,r.end);break;}
case'picker-star':{S.recents[ix].favorite=!S.recents[ix].favorite;save('ranges',S.recents);el.classList.toggle('starred',S.recents[ix].favorite);break;}
case'global-result':if(S.globalSelection){S.file='global';S.view='logs';S.page=1;S.queryHits=null;runSearch();}break;
case'clear-global':S.globalSelection=null;S.file=S.opened.length>1?'merged':S.opened[0];clearAllFilters();break;
case'views-menu':showViews(el);break;
case'confirm-save-view':{const name=$('#view-name').value.trim();if(!name){$('#view-name').focus();return;}const list=load('views',[]);list.push({...S.viewDraft,name,id:'view-'+Date.now()});save('views',list);S.activeView=name;$('#dialog').close();render();notify('已保存阅读视角：'+name);break;}
case'saved-view':{const item=load('views',[])[ix];if(item)restoreView(item);break;}
case'delete-view':{const list=load('views',[]);const removed=list.splice(ix,1)[0];save('views',list);showViews($('#views-button')||el);notify('已移除视角：'+(removed?.name||''));break;}
case'toggle-raw':pendingAnchor=captureLogAnchor();S.rawMode=!S.rawMode;render(false);break;
case'toggle-timeline':pendingAnchor=captureLogAnchor();S.timeline=!S.timeline;S.mobileTimeline=S.timeline&&innerWidth<=760;render();break;
case'toggle-blocks':pendingAnchor=captureLogAnchor();S.blocksDefault=!S.blocksDefault;S.blockClosed.clear();S.blockOpened.clear();render();break;
case'expand-block':pendingAnchor=captureLogAnchor();if(S.blocksDefault){S.blockClosed.has(id)?S.blockClosed.delete(id):S.blockClosed.add(id)}else{S.blockOpened.has(id)?S.blockOpened.delete(id):S.blockOpened.add(id)}render();break;
case'body-mode':pendingAnchor=captureLogAnchor();S.bodyModes[id]=S.bodyModes[id]==='raw'?'fields':'raw';render();break;
case'select-record':S.selected=id;$$('[data-record]').forEach(n=>{n.classList.toggle('selected',n.dataset.record===id);n.setAttribute('aria-selected',n.dataset.record===id)});break;
case'locate-unfiltered':$('#dialog').close();clearAllFilters(false);S.globalSelection=null;S.file=recById(id).fileId;if(!S.opened.includes(S.file))S.opened.push(S.file);S.page=1;locate(id);break;
case'directory-rule':directoryDialog(el.dataset.path,el.dataset.source);break;
case'commit-binding':await commitBinding(el);break;
case'edit-extractor':editExtractor(ix);break;
case'commit-extractor':{const q={id:'extract-'+Date.now(),enabled:true,name:$('#ex-name').value.trim(),source:$('#ex-source').value,pattern:$('#ex-pattern').value,key:$('#ex-key').value.trim(),group:$('#ex-group').value.trim()||'value',type:$('#ex-type').value,unit:$('#ex-unit').value.trim()};try{Engine.validate({extractors:[q]});if(!q.key&&!q.pattern.includes('(?<key>'))throw Error('未指定固定字段名时，表达式需要 key 命名捕获组。');if(!/^[\w]+$/.test(q.group))throw Error('取值组应为组名或数字。');if(S.editIndex<0)profile().extractors.push(q);else profile().extractors[S.editIndex]={...q,id:profile().extractors[S.editIndex].id||q.id,enabled:profile().extractors[S.editIndex].enabled};markDirty();S.testResult=null;$('#dialog').close();render();}catch(err){fieldErrorText(err)}break;}
case'delete-extractor':{const removed=profile().extractors.splice(ix,1)[0];markDirty();S.testResult=null;render();notify('已从草稿移除 '+removed.name+'，保存后才影响日志。');break;}
case'edit-anomaly':editAnomaly(ix);break;
case'commit-anomaly':{const q={id:'anomaly-'+Date.now(),enabled:true,name:$('#an-name').value.trim(),field:$('#an-field').value.trim(),op:$('#an-op').value,value:$('#an-value').value.trim(),severity:$('#an-severity').value};try{Engine.validate({anomalies:[q]});if(!['level','message','raw','body','fn','logger','src'].includes(q.field)&&!/^fields\.[A-Za-z_][\w.]*$/.test(q.field))throw Error('字段应为 level、message、body、raw 或 fields.字段名。');if(S.editIndex<0)profile().anomalies.push(q);else profile().anomalies[S.editIndex]={...q,id:profile().anomalies[S.editIndex].id||q.id,enabled:profile().anomalies[S.editIndex].enabled};markDirty();S.testResult=null;$('#dialog').close();render();}catch(err){fieldErrorText(err)}break;}
case'delete-anomaly':profile().anomalies.splice(ix,1);markDirty();S.testResult=null;render();break;
case'compare-layout':S.compare.layout=v;render(false);break;
case'compare-seek':{const t=$('#compare-seek').value;if(!t)return;const axis=getCompareAxis(),day=axis[0]?.key.slice(0,10);const target=norm(day+' '+(t.length===5?t+':00':t));let i=axis.findIndex(x=>x.key>=target);if(i<0)i=axis.length-1;for(const list of compareVirtuals){list.el.scrollTop=list.tree.sum(Math.max(i,0));list.paint();}break;}
case'compare-record':{const r=recById(id),f=files.find(f=>f.id===r.fileId);dialog('对比记录 · '+E(f.name),`<p class="mono">${E(r.ts)} · ${E(r.src)}:${r.srcLine} · L${r.line}</p><pre class="compare-detail-raw">${E(r.raw.join('\n'))}</pre>`,B('关闭','close-dialog')+B('复制原文','copy-record','copy','',`data-id="${E(id)}"`),true);break;}
case'compare-one-file':openFile(id);break;
case'choose-profile':if(S.dirty){S.dirtyProfiles??=new Set();S.dirtyProfiles.add(S.profile);}S.profile=id;S.dirty=!!S.dirtyProfiles?.has(id);S.testResult=null;S.testError='';render(false);break;
case'copy-profile':{const p=clone(profile());p.id='p'+Date.now();p.name+=' · 副本';p.version='1.0';p.deleted=false;S.profiles.push(p);S.appliedProfiles.push(clone(p));S.profile=p.id;S.profileTrash=false;S.dirty=false;save('profiles',S.appliedProfiles);render();break;}
case'confirm-delete-profile':{const p=profile();const used=Object.values(S.dirBindings).includes(p.id)||Object.values(S.fileBindings).includes(p.id);if(used){notify('此规则仍有目录或文件绑定，请先更换绑定后再删除。',true);$('#dialog').close();break;}p.deleted=true;const active=S.appliedProfiles.find(q=>q.id===p.id);if(active)active.deleted=true;save('profiles',S.appliedProfiles);$('#dialog').close();S.profile=S.profiles.find(q=>!q.deleted)?.id||p.id;render();break;}
case'restore-profile':{const p=profile();p.deleted=false;const applied=S.appliedProfiles.find(q=>q.id===p.id);if(applied)applied.deleted=false;else S.appliedProfiles.push(clone(p));S.profileTrash=false;save('profiles',S.appliedProfiles);render();notify('规则已恢复，可重新绑定文件或目录。');break;}
case'clear-all-filters':if(S.file==='global'){S.globalSelection=null;S.file=S.opened.length>1?'merged':S.opened[0];}S.activeView=null;clearAllFilters();break;
}
}catch(err){console.error(err);notify('操作未完成：'+err.message,true);}},true);
document.addEventListener('input',e=>{if(e.target.id==='picker-search'){S.picker.query=e.target.value;refreshPickerTree();}},true);
document.addEventListener('change',e=>{const el=e.target;if(el.dataset.pickFile){e.stopImmediatePropagation();const f=files.find(f=>f.id===el.dataset.pickFile);el.checked?S.picker.ids.add(f.id):S.picker.ids.delete(f.id);if(!el.checked)S.picker.folders=new Set([...S.picker.folders].filter(k=>!pickerFolderFiles(k).some(x=>x.id===f.id)));updatePickerChecks();}
if(el.dataset.pickFolder!==undefined){e.stopImmediatePropagation();const key=el.dataset.pickFolder,fs=pickerFolderFiles(key);for(const f of fs)el.checked?S.picker.ids.add(f.id):S.picker.ids.delete(f.id);if(el.checked){const path=key.slice(key.indexOf(':')+1);if(path)S.picker.folders.add(key);}else S.picker.folders=new Set([...S.picker.folders].filter(k=>!pickerFolderFiles(k).some(f=>fs.some(x=>x.id===f.id))));refreshPickerTree();}
if(el.dataset.enableExtractor!==undefined){e.stopImmediatePropagation();profile().extractors[Number(el.dataset.enableExtractor)].enabled=el.checked;markDirty();}
if(el.dataset.enableAnomaly!==undefined){e.stopImmediatePropagation();profile().anomalies[Number(el.dataset.enableAnomaly)].enabled=el.checked;markDirty();}
if(el.id==='compare-sync'){S.compare.sync=el.checked;if(el.checked&&compareVirtuals.length){const top=compareVirtuals[0].el.scrollTop;for(const v of compareVirtuals){v.el.scrollTop=top;v.paint();}}}
if(el.id==='page-size'){e.stopImmediatePropagation();const anchor=captureLogAnchor();S.pageSize=Number(el.value);const idx=anchor?filtered().findIndex(r=>r.id===anchor.id):-1;S.page=idx>=0?Math.floor(idx/S.pageSize)+1:1;pendingAnchor=anchor;render(false);}
if(['density','display-precision','precision','wrap-checkbox'].includes(el.id))pendingAnchor=captureLogAnchor();
},true);
// IndexedDB stores imported text only on this browser; it is not a backend identity boundary.
function cacheDBR4(){return new Promise((resolve,reject)=>{const req=indexedDB.open('logbench-r6-preview-r4-files',1);req.onupgradeneeded=()=>req.result.createObjectStore('files',{keyPath:'id'});req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});}
async function cachePutR4(f){const db=await cacheDB();try{await new Promise((res,rej)=>{const tx=db.transaction('files','readwrite');tx.objectStore('files').put({id:f.id,name:f.name,path:f.path,text:f.text,size:f.size,modified:f.modified,favorite:!!f.favorite,deleted:!!f.deleted});tx.oncomplete=res;tx.onerror=()=>rej(tx.error);});}finally{db.close();}}
async function cacheGetAllR4(){const db=await cacheDB();try{return await new Promise((res,rej)=>{const req=db.transaction('files','readonly').objectStore('files').getAll();req.onsuccess=()=>res(req.result);req.onerror=()=>rej(req.error);});}finally{db.close();}}
async function parseInWorker(text,id){const code=`const norm=${norm.toString()};const getFields=${getFields.toString()};const parseLocal=${parseLocal.toString()};onmessage=e=>{try{postMessage({records:parseLocal(e.data.text,e.data.id)})}catch(x){postMessage({error:x.message})}}`;const url=URL.createObjectURL(new Blob([code],{type:'text/javascript'}));return await new Promise((resolve,reject)=>{let w;try{w=new Worker(url)}catch(e){URL.revokeObjectURL(url);reject(e);return;}URL.revokeObjectURL(url);const timer=setTimeout(()=>{w.terminate();reject(Error('读取超时，请减少单次导入大小。'))},10000);w.onmessage=e=>{clearTimeout(timer);w.terminate();e.data.error?reject(Error(e.data.error)):resolve(e.data.records)};w.onerror=e=>{clearTimeout(timer);w.terminate();reject(Error(e.message))};w.postMessage({text,id});});}
async function importFilesR4(inputs){const incoming=[...inputs||[]];if(!incoming.length)return;let added=0,last=null,cached=0;for(const file of incoming){if(file.size>64*1024*1024){notify(file.name+' 超过单文件 64 MiB 限制。',true);continue;}try{const text=await file.text(),id='local-'+Date.now()+'-'+added;const f={id,name:file.name,path:'本机缓存/'+(file.webkitRelativePath?.split('/').slice(0,-1).join('/')||'导入文件'),source:'local',text,size:file.size,modified:new Date().toLocaleTimeString('zh-CN',{hour12:false}),favorite:false};const parsed=await parseInWorker(text,id),p=Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile;const result=await processJobs([{id,records:parsed,profile:p}]);f.records=result[0].records;f.profile=p.name;files.push(f);S.opened.push(id);last=id;added++;try{await cachePut(f);cached++;}catch{notify('浏览器未允许保存缓存，此文件仅在本次会话可用。',true);}}catch(e){notify('无法读取 '+file.name+'：'+e.message,true);}}
if(added){S.file=last;S.view='logs';S.globalSelection=null;clearAllFilters(false);S.selected=null;S.inspector=null;indexRecords();render(false);notify(`已读取 ${added} 个文件，${cached} 个已存本机缓存，未上传。`);}$('#file-input').value='';}
async function restoreCacheR4(){try{const rows=await cacheGetAll();let changed=false;for(const row of rows){if(files.some(f=>f.id===row.id))continue;const f={...row,source:'local',favorite:!!row.favorite};const parsed=await parseInWorker(f.text,f.id),p=Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile;f.records=(await processJobs([{id:f.id,records:parsed,profile:p}]))[0].records;f.profile=p.name;files.push(f);changed=true;}if(changed){indexRecords();render();}}catch{/* Browser storage blocked: local import still works in memory. */}}
document.addEventListener('click',e=>{const act=e.target.closest('[data-act]')?.dataset.act;if(['confirm-delete-files','restore-file','favorite-file'].includes(act))queueMicrotask(()=>{files.filter(f=>f.source==='local'&&!f.sample).forEach(f=>cachePut(f).catch(()=>{}));});},true);
function aboutR4(){dialog('LogBench · 阅读与规则',`<p>紧凑阅读、可配置规则、全局时间筛选与多文件时间对比。</p><p class="hint">导入、搜索、字段提取、异常规则、目录继承、时间筛选与同步对比在本机实际执行。服务器文件来自 /api/v2/library；权限由服务端校验。</p><p class="hint">本机导入可存 IndexedDB；阅读视角保存在浏览器。IP 身份、多用户隔离与缓存生命周期由 /api/v2/* 管理。</p>`);}
function help(){dialog('操作帮助',`<div class="help-grid"><h3>阅读与定位</h3><p>toolbar 选择视角、显示时间线、折叠多行或切换原文。原文切换以选中记录为锚点；选中记录离开屏幕时，以首个可见记录为锚点。</p><h3>时间筛选</h3><p>点击时间设 A / B / K；名称与脚本来源都可定位。全局时间筛选可勾选文件夹及文件，应用后进入合并结果。</p><h3>时间对比</h3><p>选择 2–4 个文件。各面板共用精确时间点；不同日志密度使用空槽对齐，滚动同步到同一时刻。4 文件默认 2×2。</p><h3>规则</h3><p>字段提取支持命名捕获组，异常判定支持字段比较。先手动试跑再保存；目录默认规则可在文件库直接设置。</p></div>`,null,true);}


/* R5 prototype: scoped enhancements; no production backend or user authentication. */
const COLUMNS=[['line','行号'],['time','时间'],['level','等级'],['logger','软件'],['src','脚本 : 行号'],['fn','函数'],['pid','进程 PID'],['tid','线程 TID']];
const DEFAULT_COLUMNS={line:true,time:true,level:true,logger:true,src:true,fn:true,pid:false,tid:false};
const SHAPE_NAMES={circle:'圆形',diamond:'菱形',square:'方形',triangle:'三角',star:'星形',flag:'旗帜',hexagon:'六边形',pin:'图钉',bolt:'闪电',cross:'十字'};
const COLOR_NAMES=['蓝','青','深青绿','绿','赭黄','橙','红','玫红','紫','靛蓝','蓝灰','石墨'];
Object.assign(S,{columns:{...DEFAULT_COLUMNS},timelinePageSize:50,pointStyles:{},traceViews:{},traceFolded:new Set(),focusRule:null,restoreReady:false});
const markerPath={circle:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z',diamond:'M12 2 22 12 12 22 2 12Z',square:'M4 4h16v16H4Z',triangle:'M12 3 22 21H2Z',star:'m12 2 3.1 6.5 7.1 1-5.1 5 .9 7.1-6-3.3-6.1 3.3 1.2-7.1-5.2-5 7.1-1Z',flag:'M5 3v19M5 4h14l-4 5 4 5H5Z',hexagon:'m7 3 10 0 5 9-5 9H7L2 12Z',pin:'M12 2a7 7 0 0 0-7 7c0 5 7 13 7 13s7-8 7-13a7 7 0 0 0-7-7Zm0 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z',bolt:'m14 2-9 12h6l-1 8 9-13h-6Z',cross:'M9 3h6v6h6v6h-6v6H9v-6H3V9h6Z'};
function marker(shape='circle',color='#2563eb',cls=''){const s=Engine.markerShapes.includes(shape)?shape:'circle',c=/^#[\da-f]{6}$/i.test(color)?color:'#2563eb';return`<svg class="node-symbol ${cls}" viewBox="0 0 24 24" style="color:${c}" aria-hidden="true"><path d="${markerPath[s]}" fill="${s==='flag'?'none':'currentColor'}" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round" fill-rule="evenodd"/></svg>`;}
function visibleColumns(){return COLUMNS.filter(([id])=>S.columns[id]!==false);}
function columnStyle(){const widths={line:48,time:S.precision==='raw'?157:S.precision==='ms'?100:76,level:73,logger:74,src:135,fn:143,pid:80,tid:80};const rs=baseRecords().slice(0,800);for(const key of['logger','src','fn']){const v=rs.reduce((a,r)=>Math.max(a,String(key==='src'?r.src+':'+r.srcLine:key==='fn'?r.fn+'()':r[key]).length),0);widths[key]=Math.max(key==='logger'?58:88,Math.min(widths[key],v*7+12));}const cols=visibleColumns().map(([id])=>widths[id]+'px');const min=visibleColumns().reduce((n,[id])=>n+widths[id],0)+268;return`--log-columns:${cols.join(' ')} minmax(240px,1fr) 28px;--log-min:${min}px;`;}
function renderLogTable(rows){currentLogItems=logItems(rows);if(!rows.length)return renderLogTableR4(rows);return`<div class="log-virtual ${S.rawMode?'raw-log':''}" style="${columnStyle()}" role="table" aria-label="${S.rawMode?'日志原文':'日志记录'}" aria-rowcount="${rows.length+1}"><div class="virtual-head ${S.rawMode?'raw-head':'log-grid'}" role="row">${S.rawMode?'<span>行号</span><span>日志原文 · 完整精度</span>':[...visibleColumns().map(x=>x[1]),'日志内容',''].map(n=>`<span role="columnheader">${n}</span>`).join('')}</div><div class="virtual-region"><div class="virtual-items"></div></div></div>`;}
function recordHtml(r,i){if(r.fold||S.rawMode)return recordHtmlR4(r,i);const p=pointLabel(r),issue=issueOf(r),severity=r.issueSeverity||(['WARNING','WARN'].includes(r.level)?'warning':'error'),multi=r.body.length>0||!!r.traceback;
const cells={line:`<span class="numcol">${r.line}</span>`,time:`<span><button class="log-time" data-act="point" data-id="${E(r.id)}" title="${E(r.ts)}">${short(r.ts,S.precision)}</button>${p?`<b class="time-bound-inline">${p}</b>`:''}</span>`,level:`<span><span class="level ${E(r.level)}">${E(r.level)}</span></span>`,logger:`<span class="software" title="${E(r.logger)}">${E(r.logger)}</span>`,src:`<span class="source-code" title="${E(r.src)}:${r.srcLine}">${E(r.src)}<b>:${r.srcLine}</b></span>`,fn:`<span class="function-cell" title="${E(r.fn)}()">${E(r.fn)}()</span>`,pid:`<span class="source-code">${E(r.pid)}</span>`,tid:`<span class="source-code">${E(r.tid)}</span>`};
const label=r.traceback?'Traceback · '+r.traceback.frames.length+' 栈帧':r.kind==='kv'?r.fields.length+' 字段':r.raw.length+' 行';
return`<article class="record-item ${S.selected===r.id?'selected':''} ${issue?severity:''}" data-record="${E(r.id)}" data-act="select-record" data-id="${E(r.id)}" tabindex="0" role="row" aria-selected="${S.selected===r.id}" aria-rowindex="${i+2}"><div class="log-grid log-row">${visibleColumns().map(([id])=>cells[id]).join('')}<span class="message-cell"><span class="message-inner"><span class="message-text" title="${E(r.message)}">${highlighted(r)}</span>${issue&&!['ERROR','WARNING','WARN','FATAL'].includes(r.level)?`<span class="rule-alert ${severity}">规则${severity==='warning'?'告警':'异常'}</span>`:''}${multi?`<button class="body-counter ${r.traceback?'trace-counter':''}" data-act="expand-block" data-id="${E(r.id)}" aria-expanded="${blockOpen(r)}">${label}${I(blockOpen(r)?'down':'chevron','sm')}</button>`:''}${S.queryHits?.[r.id]?.matched?.length?`<span class="match-count">${S.queryHits[r.id].matched.map(i=>`<i class="c${i%3+1}">${i+1}</i>`).join('')}</span>`:''}${S.bookmarks.has(r.id)?I('bookmark','sm'):''}</span></span><button class="row-more" data-act="inspect" data-id="${E(r.id)}" aria-label="查看记录 L${r.line}">${I('more')}</button></div>${multi&&blockOpen(r)?renderBody(r):''}</article>`;}
function renderBody(r){if(!r.traceback)return renderBodyR4(r);const t=r.traceback,raw=S.traceViews[r.id]==='raw',ex=t.exception,collapsed=S.traceFolded.has(r.id);return`<section class="trace-card" data-block="${E(r.id)}"><div class="trace-card-head">${I('code')}<strong>Traceback</strong><span class="trace-type">${E(ex?.type||'异常尚未完整输出')}</span><span class="spacer"></span><span class="trace-line-range">L${t.startLine}–${t.endLine}</span><button class="reader-link" data-act="r5-trace-mode" data-id="${E(r.id)}">${raw?'格式化':'堆栈原文'}</button><button class="reader-link" data-act="copy-record" data-id="${E(r.id)}" aria-label="复制完整异常记录">${I('copy','sm')}</button></div>${ex?`<div class="trace-error-message">${E(ex.message||ex.type)}</div>`:''}${t.incomplete?'<div class="trace-notice">未找到最终异常行，可能是截断日志；已保留全部收到的内容。</div>':''}${t.group?'<div class="trace-notice">ExceptionGroup：完整保留原文层级；栈帧计数仅供参考，不推断分支归属。</div>':''}${raw||t.group?`<pre class="trace-raw">${E(t.rawLines.join('\n'))}</pre>`:`<button class="trace-stack-toggle" data-act="r5-trace-fold" data-id="${E(r.id)}" aria-expanded="${!collapsed}">${I(collapsed?'chevron':'down','sm')}调用栈 · ${t.frames.length} 栈帧${t.sections.length>1?' · '+t.sections.length+' 个异常':''}<span class="spacer"></span><small>调用顺序 → 异常发生处</small></button>${collapsed?'':t.sections.map((section,j)=>`<div class="trace-section">${section.relation?`<div class="trace-chain">${I('link','sm')}${section.relation==='cause'?'上述异常是以下异常的直接原因':'处理上述异常时，又发生了以下异常'}</div>`:''}${section.frames.map((frame,k)=>`<div class="trace-frame ${k===section.frames.length-1?'fault':''}"><span class="trace-index">${k+1}</span><div class="trace-frame-main"><button class="trace-location" data-act="r5-source-line" data-id="${E(r.id)}" data-line="${frame.sourceLine}" title="查看日志原文中的栈帧行，不打开服务器源码"><span class="trace-path">${E(frame.path)}</span><b>:${frame.line}</b><span class="trace-fn">${E(frame.fn||'&lt;module&gt;')}</span>${I('arrow','sm')}</button>${frame.code.length?`<pre class="trace-code">${frame.code.map(c=>E(c.text)).join('\n')}</pre>`:''}</div></div>`).join('')}${section.exception?`<div class="trace-exception"><b>${E(section.exception.type)}</b><span>${E(section.exception.message)}</span></div>`:''}${section.extra.length?`<details class="trace-extra"><summary>其他原始内容 · ${section.extra.length} 行</summary><pre>${E(section.extra.map(x=>x.text).join('\n'))}</pre></details>`:''}</div>`).join('')}`}</section>`;}
function estimateRecord(r){if(r.traceback&&!S.rawMode&&blockOpen(r)){const t=r.traceback;return(S.compact?29:34)+90+(S.traceViews[r.id]==='raw'||t.group?t.rawLines.length*20:S.traceFolded.has(r.id)?0:t.frames.length*62+t.sections.length*38);}return estimateRecordR4(r);}
function showDisplay(anchor){showDisplayR4(anchor);const body=$('#popover .popover-body');body.insertAdjacentHTML('beforeend',`<div class="column-settings"><div class="row"><strong>显示列</strong><span class="spacer"></span><button class="link" data-act="r5-columns-reset">恢复默认</button></div><div class="column-options">${COLUMNS.map(([id,n])=>`<label class="check"><input type="checkbox" data-visible-column="${id}" ${S.columns[id]!==false?'checked':''}>${n}</label>`).join('')}</div><p class="hint">正文与记录操作保留。列设置自动保存，也会随阅读视角保存。</p></div><details class="column-settings"><summary>阅读偏好（写入工作区）</summary><div class="column-options"><label class="check"><input type="checkbox" data-lb-pref="wrap">自动换行</label><label class="check"><input type="checkbox" data-lb-pref="contrast">高对比</label><label class="check"><input type="checkbox" data-lb-pref="density">舒适密度</label></div><p class="hint">偏好写入 /api/v2/workspace；状态栏显示保存结果。</p></details>`);const panel=$('#popover');panel.style.maxHeight=Math.max(220,innerHeight-(parseFloat(panel.style.top)||panel.getBoundingClientRect().top)-12)+'px';}
function timelineData(){const all=baseRecords();const events=all.filter(r=>(r.event||pointLabel(r))&&(!S.tlOnlyIssues||pointLabel(r)||r.issue||['error','warning'].includes(r.event?.type)));const pages=Math.max(1,Math.ceil(events.length/S.timelinePageSize));S.timelinePage=Math.max(0,Math.min(pages-1,S.timelinePage));return{all,events,pages,shown:events.slice(S.timelinePage*S.timelinePageSize,(S.timelinePage+1)*S.timelinePageSize)};}
function renderTimeline(){const{all,events,pages,shown}=timelineData();let previous=null;const nodes=shown.map(r=>{const p=pointLabel(r),ev=r.event||{type:'custom',label:p==='K'?'自定义关键点':p==='A'?'选定起点':'选定终点'},n=Engine.normalizeNode({...ev,name:ev.label},0),custom=S.pointStyles[r.key],style=p==='K'?(custom||{shape:'star',color:'#7e22ce'}):n;const group=r.fileId+'|'+r.phaseId;const heading=group!==previous?`<div class="phase-header"><strong>${E(r.phase||'日志事件')}</strong><span class="phase-line"></span></div>`:'';previous=group;return`${heading}<div class="tl-node ${E(ev.type)} ${S.selected===r.id?'current':''} ${p?'marked':''}" data-node="${E(r.id)}"><div class="tl-mark">${p&&p!=='K'?`<span class="pin-icon">${p}</span>`:marker(style.shape,style.color)}</div><div class="grow"><div class="tl-time-row"><button class="tl-time" data-act="point" data-id="${E(r.id)}">${short(r.ts)}</button>${p==='K'?'<span class="point-tag">K</span>':''}${r.issueSeverity==='error'||ev.type==='error'?'<span class="tiny-state">错误</span>':r.issueSeverity==='warning'||ev.type==='warning'?'<span class="tiny-state warning">告警</span>':''}</div><button class="tl-label" data-act="locate" data-id="${E(r.id)}">${E(p==='K'&&custom?.name?custom.name:ev.label)}</button><button class="tl-source" data-act="locate" data-id="${E(r.id)}" title="定位此记录"><span class="tl-source-name">${E(r.src)}:${r.srcLine}</span><span>· L${r.line}</span>${I('arrow','sm')}</button>${(r.nodeMatches||[]).length>1?`<button class="tl-matches" data-act="inspect" data-id="${E(r.id)}">${r.nodeMatches.length} 条节点规则命中</button>`:''}</div></div>`;}).join('');return`<aside class="timeline ${S.mobileTimeline?'mobile-open':''}" aria-label="时间线"><div class="timeline-head">${I('timeline')}<strong>时间线</strong><span class="badge">${fmt(events.length)}</span><span class="spacer"></span>${B('','timeline-rules','sliders','icon ghost sm','aria-label="时间线节点规则"')}</div><div class="timeline-controls"><div class="segmented">${[['events','事件'],['segments','区间']].map(([id,n])=>`<button data-act="timeline-tab" data-value="${id}" class="${S.timelineTab===id?'active':''}">${n}</button>`).join('')}</div>${B('','timeline-issues','filter',`icon ghost sm ${S.tlOnlyIssues?'active':''}`,'aria-label="仅显示异常节点"')}</div><div class="timeline-date"><span>${all[0]?.ts.slice(0,10)||'未载入'}</span><button class="link timeline-explain" data-act="r5-timeline-help" aria-label="说明时间线分页与阶段">事件分页 ${I('info','sm')}</button></div><div class="timeline-scroll" data-scroll="timeline">${S.timelineTab==='segments'?renderSegments():`<div class="tl-nodes">${nodes||'<div class="hint empty-small">没有符合规则的节点</div>'}</div>`}</div>${S.timelineTab==='events'?`<div class="timeline-pager"><button class="btn icon ghost sm" data-act="tl-prev" aria-label="上一页节点" ${S.timelinePage===0?'disabled':''}>${I('left','sm')}</button><span class="tl-page-label">${S.timelinePage+1} / ${pages} 页</span><button class="btn icon ghost sm" data-act="tl-next" aria-label="下一页节点" ${S.timelinePage>=pages-1?'disabled':''}>${I('chevron','sm')}</button><select id="tl-page-size" aria-label="每页节点数">${[20,50,100,150].map(n=>`<option value="${n}" ${S.timelinePageSize===n?'selected':''}>${n} / 页</option>`).join('')}</select></div><div class="timeline-page-range">${events.length?`${S.timelinePage*S.timelinePageSize+1}–${Math.min(events.length,(S.timelinePage+1)*S.timelinePageSize)} / ${events.length} 个节点`:'0 个节点'}</div>`:''}${renderRangeDraft()}<div class="timeline-foot">${I('clock')}名称 / 来源定位 · 时间选点</div></aside>`;}
function renderInspector(){let html=renderInspectorR4();const r=recById(S.inspector);if(!r||S.inspectorTab!=='fields')return html;html=html.replace(/<div class="detail-rule">[\s\S]*?<\/div>/,'');html=html.replace(/<dt>生效规则<\/dt><dd>[\s\S]*?<\/dd>/,`<dt>生效配置</dt><dd>${E(r.appliedRule?.name||'默认配置')} <small>v${E(r.appliedRule?.version||'—')}</small><br><small class="muted">${E(resolved(files.find(f=>f.id===r.fileId)).source)}</small></dd>`);return html.replace('<div class="detail-section"><h4>消息正文</h4>',evidenceHtml(r)+'<div class="detail-section"><h4>消息正文</h4>');}
function evidenceHtml(r){const hits=[...(r.issueMatches||[]).map(q=>({...q,category:'errors'})),...(r.nodeMatches||[]).map(q=>({...q,category:'nodes'}))];const fields=new Map();for(const m of r.fieldMeta||[]){const key=m.ruleId||m.rule;if(!fields.has(key))fields.set(key,{id:key,name:m.rule,category:'fields',pattern:m.pattern,evidence:[]});fields.get(key).evidence.push(m);}hits.push(...fields.values());return`<section class="rule-evidence"><div class="evidence-title">实际命中规则 <span>${hits.length}</span></div>${hits.map(q=>`<details class="evidence-hit ${q.category}" ${q.category==='errors'?'open':''}><summary><span class="evidence-category">${{errors:'异常判断',nodes:'时间线',fields:'字段提取'}[q.category]}</span><strong>${E(q.name)}</strong></summary><div class="evidence-body"><code class="evidence-condition">${E(q.condition||q.pattern||(q.conditions||[]).map(Engine.conditionLabel).join(q.combine==='any'?' 或 ':' 且 '))}</code>${q.evidence.slice(0,8).map(ev=>`<div class="evidence-value"><span>${E(ev.field||ev.key||'匹配值')}</span><b>${E(ev.value)}</b><button data-act="r5-source-line" data-id="${E(r.id)}" data-line="${ev.line}">L${ev.line}</button></div>`).join('')}${q.evidence.length>8?`<span class="hint">其余 ${q.evidence.length-8} 项见提取字段。</span>`:''}<div class="evidence-origin"><span>v${E(r.appliedRule?.version)} · ${E(q.id)}</span><button class="link" data-act="r5-hit-rule" data-profile="${E(r.appliedRule?.id)}" data-category="${q.category}" data-rule="${E(q.id)}">查看规则</button></div></div></details>`).join('')||'<p class="hint">未命中已启用的异常、字段或节点规则。</p>'}<p class="evidence-footnote">条件与值来自本次解析快照，未保存草稿不会改变这里的判定。</p></section>`;}
function sourceLineDialog(r,line){const i=Math.max(0,line-r.line),start=Math.max(0,i-2),end=Math.min(r.raw.length,i+4);dialog('日志原文位置',`<p class="hint">${E(files.find(f=>f.id===r.fileId)?.name)} · L${line}（源代码位置仅作解析，不读取脚本文件）</p><div class="source-fragment">${r.raw.slice(start,end).map((text,j)=>`<div class="${j+start===i?'highlight':''}"><span>${r.line+start+j}</span><code>${E(text)}</code></div>`).join('')}</div>`,B('关闭','close-dialog')+B('复制完整记录','copy-record','copy','',`data-id="${E(r.id)}"`),true);}
function viewSnapshot(){return{...viewSnapshotR4(),columns:clone(S.columns),timelinePageSize:S.timelinePageSize,pointStyles:clone(S.pointStyles),compact:S.compact,wrap:S.wrap,blocksDefault:S.blocksDefault};}
function restoreView(v){S.columns={...DEFAULT_COLUMNS,...v.columns};S.timelinePageSize=v.timelinePageSize||50;S.pointStyles=clone(v.pointStyles||{});S.compact=!!v.compact;S.wrap=!!v.wrap;S.blocksDefault=v.blocksDefault!==false;restoreViewR4(v);}
function ruleConditionForm(q,i,prefix='nd'){const unary=['nonzero','exists','missing'].includes(q.op);return`<div class="node-condition" data-condition-index="${i}"><div class="condition-index">${i+1}</div><div class="condition-controls"><label>判断字段<input class="input mono" data-cond-field="${i}" list="node-fields" value="${E(q.field)}"></label><label>条件<select class="select" data-cond-op="${i}">${Engine.ops.map(op=>`<option value="${op}" ${q.op===op?'selected':''}>${E(Engine.conditionLabel({field:'',op,value:''}).trim())}</option>`).join('')}</select></label><label class="condition-value">比较值 / 正则<input class="input mono" data-cond-value="${i}" value="${E(q.value||'')}" ${unary?'disabled':''} placeholder="${unary?'无需填写':'例如 0x8001 或 ^ERROR$'}"></label><label class="check condition-case"><input type="checkbox" data-cond-case="${i}" ${q.caseSensitive?'checked':''}>区分大小写</label></div><button class="btn icon ghost sm" data-act="r5-node-remove-condition" data-index="${i}" aria-label="删除条件 ${i+1}" ${S.nodeDraft.conditions.length===1?'disabled':''}>${I('close','sm')}</button></div>`;}
function appearanceForm(q){return`<div class="marker-editor"><div class="marker-preview" id="marker-preview">${marker(q.shape,q.color)}<div><strong>${E(q.name||'自定义关键点')}</strong><span>11:39:15 · 节点预览</span></div></div><fieldset class="shape-options"><legend>点位形状</legend>${Engine.markerShapes.map(s=>`<button type="button" data-act="r5-shape" data-value="${s}" class="shape-choice ${q.shape===s?'active':''}" aria-pressed="${q.shape===s}" title="${SHAPE_NAMES[s]}">${marker(s,q.color)}<span>${SHAPE_NAMES[s]}</span></button>`).join('')}</fieldset><fieldset class="color-options"><legend>点位颜色</legend>${Engine.markerColors.map((c,i)=>`<button type="button" data-act="r5-marker-color" data-value="${c}" class="color-choice ${q.color===c?'active':''}" aria-label="${COLOR_NAMES[i]} ${c}" aria-pressed="${q.color===c}" style="--swatch:${c}">${q.color===c?I('check','sm'):''}</button>`).join('')}<label class="custom-marker-color">自定义<input type="color" id="node-custom-color" value="${E(q.color)}"></label></fieldset><p class="hint">图标与文字共同识别节点。自定义点位颜色不会改变原始日志等级。</p></div>`;}
function nodeEditor(i){S.nodeEditIndex=i;S.nodeDraft=Engine.normalizeNode(clone(profile().nodes?.[i]||{name:'',type:'custom',enabled:true,combine:'all',conditions:[{field:'message',op:'contains',value:''}],shape:'star',color:'#7e22ce'}),i<0?Date.now():i);S.nodeEditKind='rule';drawNodeEditor();}
function readNodeForm(){if(!S.nodeDraft)return;S.nodeDraft.name=$('#nd-name')?.value||'';if(S.nodeEditKind==='rule'){S.nodeDraft.type=$('#nd-type').value;S.nodeDraft.combine=$('#nd-combine').value;S.nodeDraft.enabled=$('#nd-enabled').checked;S.nodeDraft.conditions=S.nodeDraft.conditions.map((q,i)=>({...q,field:$(`[data-cond-field="${i}"]`).value.trim(),op:$(`[data-cond-op="${i}"]`).value,value:$(`[data-cond-value="${i}"]`).value,caseSensitive:$(`[data-cond-case="${i}"]`).checked}));}S.editingDraft={kind:S.nodeEditKind,index:S.nodeEditIndex,key:S.nodeEditKey,profile:S.profile,rule:clone(S.nodeDraft)};}
function drawNodeEditor(){const q=S.nodeDraft;S.editingDraft={kind:S.nodeEditKind,index:S.nodeEditIndex,key:S.nodeEditKey,profile:S.profile,rule:clone(q)};dialog(S.nodeEditKind==='point'?'关键点样式':S.nodeEditIndex<0?'新增时间线规则':'编辑时间线规则',`<div class="node-editor"><div class="node-match-editor"><div class="field"><label class="label" for="nd-name">${S.nodeEditKind==='point'?'关键点名称':'规则名称'}</label><input class="input" id="nd-name" value="${E(q.name)}" placeholder="例如：非零错误码节点" maxlength="80"></div>${S.nodeEditKind==='rule'?`<div class="filter-grid"><label class="field"><span class="label">节点类型</span><select class="select" id="nd-type">${[['custom','自定义关键点'],['start','模块开始'],['end','模块结束'],['error','错误节点'],['warning','告警节点']].map(([v,n])=>`<option value="${v}" ${q.type===v?'selected':''}>${n}</option>`).join('')}</select></label><label class="check"><input type="checkbox" id="nd-enabled" ${q.enabled!==false?'checked':''}>启用本规则</label></div><div class="condition-heading"><strong>匹配条件</strong><select class="select" id="nd-combine"><option value="all" ${q.combine==='all'?'selected':''}>全部满足 AND</option><option value="any" ${q.combine==='any'?'selected':''}>任一满足 OR</option></select></div><datalist id="node-fields">${[...Engine.fieldNames,'fields.err_code','fields.rpc_success','fields.isValid','fields.PPe'].map(x=>`<option value="${x}">`).join('')}</datalist><div class="conditions-list">${q.conditions.map(ruleConditionForm).join('')}</div>${B('添加条件','r5-node-add-condition','plus','sm',q.conditions.length>=12?'disabled':'')}<details class="rule-guide"><summary>匹配范围与规则写法</summary><div><p>先提取字段，再按记录评估条件。<code>fields.err_code 非零</code> 排除 <code>0</code>、<code>0x0</code>；搭配 <code>logger 等于 APP-A</code> 可限定软件。</p><p><code>raw</code> 匹配完整原文；<code>body</code> 匹配续行；<code>traceback.type</code> 匹配异常类型。字段不存在时仅“不存在”条件命中。正则采用 JavaScript 语法；默认不区分大小写。</p><p>多规则命中时全部保留依据，列表中最靠上的规则决定主图标，可使用上下移动调整优先级。原始错误等级和异常依据不被覆盖。</p></div></details>`:'<p class="hint">只改变此关键点的名称与标记，时间位置和筛选边界不变。</p>'}<div id="node-error" class="form-error" role="alert"></div></div>${appearanceForm(q)}</div>`,B('取消','r5-node-cancel')+B(S.nodeEditKind==='point'?'保存关键点':'保存到草稿','r5-node-commit','check','primary'),true);$('#dialog').classList.add('node-dialog');}
function renderRuleEditor(p){if(S.ruleSection!=='nodes')return renderRuleEditorR4(p);return`<div class="editor-title"><h3>时间线节点</h3>${B('新增节点规则','r5-edit-node','plus','sm','data-index="-1"')}</div><p class="description">复用异常判断的字段与条件，增加节点类型、图标和颜色。编辑先进入草稿，试跑后再保存应用。</p><details class="rule-guide"><summary>节点、阶段与分页的区别</summary><div><p>规则决定哪些记录成为节点。模块开始／结束是节点类型，不是分页边界。时间线每页显示指定数量的节点，“上一页／下一页”只翻页，不会切换日志文件或重新推断模块。</p><p>多个规则同时命中时保留所有依据；第一条决定主图标。原始等级、派生异常和手动 A / B / K 边界各自保留。</p></div></details>${S.editingDraft?.profile===p.id?`<div class="resume-draft">有未完成的节点编辑<button class="link" data-act="r5-resume-node">恢复编辑</button><button class="link" data-act="r5-discard-node">丢弃</button></div>`:''}<div class="editable-rules node-rule-list">${(p.nodes||[]).map((old,i)=>{const q=Engine.normalizeNode(old,i);return`<div class="editable-rule ${S.focusRule===q.id?'focused-rule':''}" data-node-rule-id="${E(q.id)}"><label class="check"><input type="checkbox" data-enable-node="${i}" ${q.enabled!==false?'checked':''} aria-label="启用 ${E(q.name)}"></label><span class="node-rule-symbol">${marker(q.shape,q.color)}</span><div class="grow"><div class="rule-name">${E(q.name)} <span class="badge">${{start:'模块开始',end:'模块结束',custom:'关键点',error:'错误',warning:'告警'}[q.type]||'节点'}</span></div><code class="rule-pattern">${q.conditions.map(c=>E(Engine.conditionLabel(c))).join(q.combine==='any'?'<br>或 ':'<br>且 ')}</code><span class="hint">${SHAPE_NAMES[q.shape]} · ${E(q.color)} · 优先级 ${i+1}</span></div><div class="node-rule-ops">${B('','r5-node-up','up','icon ghost sm',`data-index="${i}" aria-label="提高 ${E(q.name)} 优先级" ${i===0?'disabled':''}`)}${B('','r5-node-down','down','icon ghost sm',`data-index="${i}" aria-label="降低 ${E(q.name)} 优先级" ${i===p.nodes.length-1?'disabled':''}`)}${B('编辑','r5-edit-node','edit','ghost sm',`data-index="${i}"`)}${B('','r5-node-delete','trash','icon ghost sm',`data-index="${i}" aria-label="删除 ${E(q.name)}"`)}</div></div>`;}).join('')||'<div class="empty-small hint">暂无节点规则，点击新增规则。异常仍可在日志中识别。</div>'}</div>`;}
function showPoint(id,anchor){showPointR4(id,anchor);const r=recById(id);if(S.draft.keys.includes(r?.key))$('#popover .menu').insertAdjacentHTML('beforeend',`<div class="menu-sep"></div><button class="menu-item" data-act="r5-point-style" data-id="${E(id)}">${I('edit')}关键点名称与图标</button>`);}
function syncMarkerPreview(){if(!S.nodeDraft)return;const el=$('#marker-preview');if(el)el.innerHTML=marker(S.nodeDraft.shape,S.nodeDraft.color)+`<div><strong>${E($('#nd-name')?.value||'自定义关键点')}</strong><span>11:39:15 · 节点预览</span></div>`;}
// Capture only this extension's actions before R4's delegated handlers.
window.addEventListener('click',e=>{const el=e.target.closest('[data-act]');if(!el)return;const act=el.dataset.act,id=el.dataset.id,ix=Number(el.dataset.index),v=el.dataset.value;if(!act.startsWith('r5-')&&!['tl-prev','tl-next'].includes(act))return;e.preventDefault();e.stopImmediatePropagation();
try{switch(act){
case'tl-prev':case'tl-next':{const{pages}=timelineData();S.timelinePage=Math.max(0,Math.min(pages-1,S.timelinePage+(act==='tl-next'?1:-1)));render();const sc=$('[data-scroll="timeline"]');if(sc)sc.scrollTop=0;break;}
case'r5-timeline-help':dialog('时间线如何组织',`<p><strong>分页</strong>按节点数量切分，当前每页 ${S.timelinePageSize} 个；上一页和下一页只切换节点页。</p><p><strong>阶段标题</strong>来自记录已有的模块／阶段信息，与分页无关。导入日志没有阶段信息时显示“导入日志”，不凭空推断业务流程。</p><p class="hint">节点由已启用的时间线规则、异常判断和手动 A / B / K 标记产生。点击名称或来源定位，点击时间选点。</p>`);break;
case'r5-columns-reset':pendingAnchor=captureLogAnchor();S.columns={...DEFAULT_COLUMNS};render();showDisplay($('#display-btn'));break;
case'r5-trace-mode':pendingAnchor=captureLogAnchor();S.traceViews[id]=S.traceViews[id]==='raw'?'formatted':'raw';render();break;
case'r5-trace-fold':pendingAnchor=captureLogAnchor();S.traceFolded.has(id)?S.traceFolded.delete(id):S.traceFolded.add(id);render();break;
case'r5-source-line':{const r=recById(id);if(r)sourceLineDialog(r,Number(el.dataset.line));break;}
case'r5-hit-rule':{const p=S.profiles.find(p=>p.id===el.dataset.profile&&!p.deleted),section=el.dataset.category;const list=p?.[section==='errors'?'anomalies':section==='nodes'?'nodes':'extractors']||[];const found=list.find((q,i)=>(section==='nodes'?Engine.normalizeNode(q,i).id:q.id||q.name)===el.dataset.rule);if(!p||!found){notify('这条规则已被删除或替换，命中卡片仍保留当时的条件快照。',true);break;}S.view='rules';S.profile=p.id;S.ruleSection=section;S.focusRule=el.dataset.rule;render(false);requestAnimationFrame(()=>{const sel=section==='nodes'?`[data-node-rule-id="${CSS.escape(S.focusRule)}"]`:'.editable-rule';const entries=$$(sel);const row=section==='nodes'?entries[0]:entries.find(n=>n.textContent.includes(found.name));row?.classList.add('focused-rule');row?.scrollIntoView({block:'nearest'});});break;}
case'r5-edit-node':nodeEditor(ix);break;
case'r5-node-delete':{const p=profile();p.nodes.splice(ix,1);S.nodeRules=p.nodes;markDirty();S.testResult=null;render();notify('已从草稿移除，保存应用后生效。');break;}
case'r5-node-up':case'r5-node-down':{const a=profile().nodes,j=ix+(act==='r5-node-up'?-1:1);if(j>=0&&j<a.length)[a[ix],a[j]]=[a[j],a[ix]];S.nodeRules=a;markDirty();render();break;}
case'r5-node-add-condition':readNodeForm();if(S.nodeDraft.conditions.length<12)S.nodeDraft.conditions.push({field:'message',op:'contains',value:'',caseSensitive:false});drawNodeEditor();break;
case'r5-node-remove-condition':readNodeForm();if(S.nodeDraft.conditions.length>1)S.nodeDraft.conditions.splice(ix,1);drawNodeEditor();break;
case'r5-shape':readNodeForm();S.nodeDraft.shape=v;drawNodeEditor();break;
case'r5-marker-color':readNodeForm();S.nodeDraft.color=v;drawNodeEditor();break;
case'r5-node-cancel':S.editingDraft=null;S.nodeDraft=null;$('#dialog').close();render();break;
case'r5-node-commit':{readNodeForm();const q=clone(S.nodeDraft);if(!q.name.trim())throw Error('请填写名称。');if(S.nodeEditKind==='point'){S.pointStyles[S.nodeEditKey]={name:q.name,shape:q.shape,color:q.color};}else{Engine.validate({nodes:[q]});const p=profile();if(S.nodeEditIndex<0)p.nodes.push(q);else p.nodes[S.nodeEditIndex]=q;S.nodeRules=p.nodes;markDirty();S.testResult=null;}S.editingDraft=null;S.nodeDraft=null;$('#dialog').close();render();notify(S.nodeEditKind==='point'?'关键点已保存到本机。':'已保存到草稿，试跑后点击“保存并应用”。');break;}
case'r5-resume-node':{const d=S.editingDraft;if(d){S.nodeDraft=clone(d.rule);S.nodeEditIndex=d.index;S.nodeEditKind=d.kind;S.nodeEditKey=d.key;drawNodeEditor();}break;}
case'r5-discard-node':S.editingDraft=null;render();break;
case'r5-point-style':{const r=recById(id);if(!r)break;S.nodeEditKey=r.key;S.nodeEditKind='point';S.nodeEditIndex=-1;S.nodeDraft={name:'自定义关键点',shape:'star',color:'#7e22ce',...(S.pointStyles[r.key]||{})};drawNodeEditor();break;}
case'r5-storage':storagePanel();break;
case'r5-persist':requestPersistence();break;
case'r5-backup':exportWorkspace();break;
case'r5-import-backup':$('#workspace-input').click();break;
case'r5-confirm-backup':restoreBackup();break;
case'r5-retry-storage':retryStorage();break;
}}
catch(err){const box=$('#node-error');if(box&&$('#dialog').open)box.textContent=err.message;else notify(err.message,true);}finally{scheduleStateSave();}},true);
window.addEventListener('change',e=>{const el=e.target;
if(el.dataset.visibleColumn){e.stopImmediatePropagation();pendingAnchor=captureLogAnchor();S.columns[el.dataset.visibleColumn]=el.checked;render();}
if(el.id==='tl-page-size'){e.stopImmediatePropagation();const oldFirst=S.timelinePage*S.timelinePageSize;S.timelinePageSize=Number(el.value);S.timelinePage=Math.floor(oldFirst/S.timelinePageSize);render();const sc=$('[data-scroll="timeline"]');if(sc)sc.scrollTop=0;}
if(el.dataset.enableNode!==undefined){e.stopImmediatePropagation();profile().nodes[Number(el.dataset.enableNode)].enabled=el.checked;S.nodeRules=profile().nodes;markDirty();S.testResult=null;}
if(el.dataset.condOp!==undefined){readNodeForm();const i=Number(el.dataset.condOp);$(`[data-cond-value="${i}"]`).disabled=['nonzero','exists','missing'].includes(el.value);}
if(el.id==='node-custom-color'){S.nodeDraft.color=el.value;readNodeForm();syncMarkerPreview();}
if(el.id==='workspace-input'){previewBackup(el.files[0]);el.value='';}
queueMicrotask(scheduleStateSave);},true);
window.addEventListener('input',e=>{if(e.target.closest('.node-dialog')){readNodeForm();syncMarkerPreview();}queueMicrotask(scheduleStateSave);},true);
const STATE_KEYS=['view','file','opened','scope','search','flags','level','software','fn','module','rules','ruleMode','ruleTab','page','pageSize','precision','theme','compact','wrap','sidebar','timeline','timelineTab','tlOnlyIssues','timelinePage','timelinePageSize','selected','expanded','bodyModes','fullFields','bookmarks','draft','applied','folded','inspector','inspectorTab','contextSize','contextUnit','recents','favoriteOnly','libSource','folder','libQuery','libSelected','folderOpen','collapsedDirs','profiles','appliedProfiles','profile','ruleSection','profileTrash','dirty','dirtyProfiles','testInput','colorOverrides','rawMode','blocksDefault','blockClosed','blockOpened','globalSelection','compare','dirBindings','fileBindings','activeView','extraFolders','columns','pointStyles','traceViews','traceFolded','editingDraft','ruleDraft','draftRuleMode'];
const SET_KEYS=new Set(['expanded','fullFields','bookmarks','folded','libSelected','collapsedDirs','dirtyProfiles','blockClosed','blockOpened','traceFolded']);
const AUX_KEYS=['views','ranges','profiles','colors','dirBindings','fileBindings','theme','precision','compact'];
const storageState={ready:false,writing:false,importing:false,restoreBlocked:false,stateSavedAt:null,fileSavedAt:null,persistent:null,issue:'',unsaved:new Set(),quota:null,usage:null};
let stateTimer=null,idbTimer=null,dbPromise=null,lastSnapshot=null,bootSnapshot=null,backupCandidate=null,renderSaving=false;
function storageIssue(message){storageState.issue=message;updateStorageBadge();}
function updateStorageBadge(){const el=$('#cache-status');if(!el)return;const bad=storageState.issue||storageState.unsaved.size;el.classList.toggle('cache-warning',!!bad);el.innerHTML=I(bad?'warning':storageState.writing?'clock':'lock','sm')+`<span>${!storageState.ready?'正在恢复本机工作区':bad?'本机保存需处理':storageState.writing||storageState.importing?'正在保存':storageState.stateSavedAt?'已保存到本机':'本机存储'}</span>`;el.title=bad?String(storageState.issue||storageState.unsaved.size+' 个文件未落盘'):'查看本机存储、持久保存和备份';}
function cacheDB(){if(dbPromise)return dbPromise;dbPromise=new Promise((resolve,reject)=>{let req,finished=false;const fail=e=>{if(finished)return;finished=true;clearTimeout(timer);reject(e||Error('无法打开本机数据库'));};const timer=setTimeout(()=>fail(Error('本机数据库响应超时，请关闭旧页面后重试。')),3000);try{req=indexedDB.open('logbench-r6-preview-local',1);}catch(e){fail(e);return;}req.onupgradeneeded=()=>{const db=req.result;if(!db.objectStoreNames.contains('files'))db.createObjectStore('files',{keyPath:'id'});if(!db.objectStoreNames.contains('meta'))db.createObjectStore('meta',{keyPath:'id'});};req.onblocked=()=>{storageIssue('另一个 LogBench 页面阻止了存储升级，请关闭旧页面后重试。');};req.onsuccess=()=>{const db=req.result;if(finished){db.close();return;}finished=true;clearTimeout(timer);db.onversionchange=()=>{db.close();dbPromise=null;};resolve(db);};req.onerror=()=>fail(req.error);}).catch(e=>{dbPromise=null;throw e;});return dbPromise;}
function transactionDone(tx){return new Promise((resolve,reject)=>{tx.oncomplete=()=>resolve();tx.onabort=tx.onerror=()=>reject(tx.error||Error('本机存储事务失败'));});}
function cacheRow(f){return{id:f.id,name:f.name,path:f.path,text:f.text,size:f.size,modified:f.modified,favorite:!!f.favorite,deleted:!!f.deleted};}
async function cachePut(f){try{storageState.writing=true;updateStorageBadge();const db=await cacheDB();const tx=db.transaction('files','readwrite');tx.objectStore('files').put(cacheRow(f));await transactionDone(tx);storageState.fileSavedAt=Date.now();storageState.unsaved.delete(f.id);if(!storageState.unsaved.size&&storageState.issue.startsWith('文件'))storageState.issue='';}catch(e){storageState.unsaved.add(f.id);storageIssue('文件未保存到磁盘：'+(e?.name||'存储不可用')+'。请重试或导出备份，关闭页面可能丢失。');throw e;}finally{storageState.writing=false;updateStorageBadge();}}
async function readStore(store){const db=await cacheDB();return new Promise((resolve,reject)=>{const req=db.transaction(store,'readonly').objectStore(store).getAll();req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});}
async function cacheGetAll(){return readStore('files');}
function stateSnapshot(){const out={};for(const k of STATE_KEYS){if(S[k]!==undefined)out[k]=SET_KEYS.has(k)?[...S[k]]:clone(S[k]);}const anchor=S.view==='logs'?(captureLogAnchor()||S.savedAnchor):S.savedAnchor;const aux={views:clone(load('views',[])),ranges:clone(S.recents),profiles:clone(S.appliedProfiles),colors:clone(S.colorOverrides),dirBindings:clone(S.dirBindings),fileBindings:clone(S.fileBindings),theme:S.theme,precision:S.precision,compact:S.compact};return{schema:'logbench.workspace',version:1,savedAt:Date.now(),state:out,anchor:anchor||null,scroll:Object.fromEntries($$('[data-scroll]').map(el=>[el.dataset.scroll,[el.scrollTop,el.scrollLeft]])),fileMeta:files.map(f=>({id:f.id,favorite:!!f.favorite,deleted:!!f.deleted})),aux};}
function snapshotProblem(snapshot){
 if(!snapshot||snapshot.schema!=='logbench.workspace'||snapshot.version!==1||!snapshot.state||Array.isArray(snapshot.state))return'工作区格式或版本无效';const state=snapshot.state;
 for(const k of STATE_KEYS){if(state[k]===undefined)continue;const v=state[k],current=S[k];if(SET_KEYS.has(k)||Array.isArray(current)){if(!Array.isArray(v))return k+' 应为数组';}else if(current!==null&&current!==undefined&&typeof current!==typeof v)return k+' 类型不匹配';}
 for(const k of ['profiles','appliedProfiles']){const list=state[k];if(!Array.isArray(list)||!list.length||list.length>200)return'缺少有效规则配置';for(const profile of list){if(!profile||typeof profile.id!=='string'||!/^[\w.:-]+$/.test(profile.id)||typeof profile.name!=='string')return'规则配置标识无效';for(const field of['nodes','anomalies','extractors']){if(profile[field]!==undefined&&(!Array.isArray(profile[field])||profile[field].some(x=>!x||typeof x!=='object'||Array.isArray(x))))return'规则列表无效';}}}
 for(const k of ['compare','flags','draft','columns','pointStyles','traceViews','bodyModes','dirBindings','fileBindings','colorOverrides']){if(state[k]!==undefined&&(!state[k]||typeof state[k]!=='object'||Array.isArray(state[k])))return k+' 对象无效';}
 if(state.compare&&(!Array.isArray(state.compare.ids)||!state.compare.ids.every(x=>typeof x==='string')))return'对比文件列表无效';
 if(state.opened&&!state.opened.every(x=>typeof x==='string'))return'文件列表无效';
 if(state.columns&&Object.values(state.columns).some(x=>typeof x!=='boolean'))return'列设置无效';
 if(state.draft&&(!Array.isArray(state.draft.keys)||![state.draft.start,state.draft.end].every(x=>x===null||typeof x==='string')))return'时间草稿无效';
 if(snapshot.aux){for(const key of['views','ranges','profiles'])if(snapshot.aux[key]!==undefined&&!Array.isArray(snapshot.aux[key]))return'辅助设置 '+key+' 无效';}
 try{state.appliedProfiles.forEach(Engine.validate);}catch(e){return'已应用规则无效：'+e.message;}return'';
}
function scheduleStateSave(){if(!storageState.ready||storageState.restoreBlocked)return;clearTimeout(stateTimer);stateTimer=setTimeout(flushState,90);}
function flushState(){if(!storageState.ready||storageState.restoreBlocked)return;clearTimeout(stateTimer);const snapshot=stateSnapshot();lastSnapshot=snapshot;S.savedAnchor=snapshot.anchor;let localOK=false;try{localStorage.setItem('logbench-r6-workspace',JSON.stringify(snapshot));localOK=true;storageState.stateSavedAt=snapshot.savedAt;}catch(e){storageIssue('设置尚未确认写入磁盘：'+(e.name||'存储不可用')+'。请查看本机存储状态。');}clearTimeout(idbTimer);idbTimer=setTimeout(()=>writeSnapshot(snapshot,localOK),120);updateStorageBadge();return snapshot;}
async function writeSnapshot(snapshot,localOK=false){try{const db=await cacheDB(),tx=db.transaction('meta','readwrite');tx.objectStore('meta').put({id:'workspace',value:snapshot});await transactionDone(tx);storageState.stateSavedAt=snapshot.savedAt;if(!storageState.unsaved.size&&!storageState.issue.startsWith('恢复'))storageState.issue='';}catch(e){if(!localOK)storageIssue('本机存储不可用，当前设置仅在内存中。请导出备份；关闭页面可能丢失。');}updateStorageBadge();}
function hydrateSnapshot(snapshot){if(snapshotProblem(snapshot))return false;const state=snapshot.state;for(const k of STATE_KEYS){if(state[k]!==undefined){S[k]=SET_KEYS.has(k)?new Set(Array.isArray(state[k])?state[k]:[]):clone(state[k]);}}S.columns={...DEFAULT_COLUMNS,...(state.columns||{})};S.timelinePageSize=[20,50,100,150].includes(S.timelinePageSize)?S.timelinePageSize:50;S.pageSize=[1000,5000,10000,30000,50000].includes(S.pageSize)?S.pageSize:1000;S.appliedProfiles=Array.isArray(state.appliedProfiles)&&state.appliedProfiles.length?clone(state.appliedProfiles):clone(S.profiles);S.queryHits=null;S.searching=false;S.searchError='';S.ruleBusy=false;S.testBusy=false;S.testResult=null;S.profiles.forEach(p=>{p.nodes=(p.nodes||[]).map(Engine.normalizeNode)});S.appliedProfiles.forEach(p=>{p.nodes=(p.nodes||[]).map(Engine.normalizeNode)});S.nodeRules=profile()?.nodes||[];S.savedAnchor=snapshot.anchor;for(const [k,v]of Object.entries(snapshot.aux||{}))if(AUX_KEYS.includes(k))sessionStore.set(k,clone(v));for(const m of snapshot.fileMeta||[]){const f=files.find(f=>f.id===m.id);if(f){f.favorite=!!m.favorite;f.deleted=!!m.deleted;}}storageState.stateSavedAt=snapshot.savedAt;return true;}
async function migrateR4Files(){// Read old store only when explicitly present; never clear older data.
if(!indexedDB.databases)return;const list=await indexedDB.databases();if(!list.some(x=>x.name==='logbench-r6-preview-r4-files'))return;const old=await new Promise((res,rej)=>{const r=indexedDB.open('logbench-r6-preview-r4-files');r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});try{if(!old.objectStoreNames.contains('files'))return;const rows=await new Promise((res,rej)=>{const r=old.transaction('files').objectStore('files').getAll();r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});const current=new Set((await cacheGetAll()).map(x=>x.id));for(const row of rows)if(!current.has(row.id))await cachePut(row);}finally{old.close();}}
async function restoreCache(){storageState.ready=false;S.restoreReady=false;clearTimeout(stateTimer);clearTimeout(idbTimer);$('#app').inert=true;updateStorageBadge();let local=null;try{local=JSON.parse(localStorage.getItem('logbench-r6-workspace'));}catch{}let disk=null,rows=[];try{const meta=await readStore('meta');disk=meta.find(x=>x.id==='workspace')?.value;await migrateR4Files();rows=await cacheGetAll();}catch(e){storageIssue('本机数据库不可用；未清理已有数据。请检查浏览器存储权限，并导出重要工作区备份。');}
const candidates=[local,disk].filter(Boolean);const invalid=candidates.find(x=>snapshotProblem(x));if(invalid){storageState.restoreBlocked=true;storageIssue('恢复数据格式异常：'+snapshotProblem(invalid)+'。已有缓存未覆盖；请导出备份后处理。');}const snapshot=candidates.filter(x=>!snapshotProblem(x)).sort((a,b)=>(b.savedAt||0)-(a.savedAt||0))[0];bootSnapshot=snapshot||null;if(snapshot)hydrateSnapshot(snapshot);
for(const row of rows){if(files.some(f=>f.id===row.id))continue;const f={...row,source:'local',records:[]};try{const parsed=await parseInWorker(f.text,f.id),p=Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile;f.records=(await processJobs([{id:f.id,records:parsed,profile:p}]))[0].records;f.profile=p.name;}catch(e){f.parseError=e.message;storageIssue('恢复时有文件无法解析，正文仍在本机缓存；可下载或调整规则后重试。');}files.push(f);}
// Fixtures also use the last applied configuration, not an unsaved rule draft.
try{const fixtureFiles=files.filter(f=>f.source==='server'||f.sample);const jobs=fixtureFiles.map(f=>({id:f.id,records:f.records,profile:Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile}));const result=await processJobs(jobs);for(const item of result){const f=fixtureFiles.find(f=>f.id===item.id);f.records=item.records;f.profile=Engine.resolveProfile(f,S.appliedProfiles,S.dirBindings,S.fileBindings).profile.name;}}catch(e){storageIssue('恢复的规则无法执行，已保留原文件与草稿。'+e.message);}
if(snapshot){for(const m of snapshot.fileMeta||[]){const f=files.find(f=>f.id===m.id);if(f){f.favorite=!!m.favorite;f.deleted=!!m.deleted;}}const missing=S.opened.filter(id=>!files.some(f=>f.id===id&&!f.deleted));S.opened=S.opened.filter(id=>files.some(f=>f.id===id&&!f.deleted));if(!['global','merged'].includes(S.file)&&!files.some(f=>f.id===S.file&&!f.deleted))S.file=S.opened[0]||null;if(missing.length)storageIssue('恢复时有 '+missing.length+' 个文件不可用，未用其它文件替换。');}
indexRecords();storageState.ready=true;S.restoreReady=true;$('#app').inert=false;if(S.savedAnchor)pendingAnchor=S.savedAnchor;render(false);if(S.search||S.rules.some(r=>r.enabled&&r.query))runSearch(false);requestAnimationFrame(()=>{if(S.savedAnchor&&logVirtual)logVirtual.toId(S.savedAnchor.id,S.savedAnchor.y);const sc=snapshot?.scroll||{};for(const el of $$('[data-scroll]')){const p=sc[el.dataset.scroll];if(p&&el.dataset.scroll!=='logs'){el.scrollTop=p[0];el.scrollLeft=p[1];}}});refreshStorageInfo();}
async function importFiles(inputs){storageState.importing=true;updateStorageBadge();try{await importFilesR4(inputs);}finally{storageState.importing=false;flushState();updateStorageBadge();}}
function render(keep=true){if(S.view!=='logs'){const previous=captureLogAnchor();if(previous)S.savedAnchor=previous;}renderR4(keep);const foot=$('.app-status .row');if(foot)foot.insertAdjacentHTML('beforeend','<button id="cache-status" class="cache-status" data-act="r5-storage" aria-label="本机存储状态"></button>');$('.app-status .status-right')?.lastElementChild?.replaceChildren(document.createTextNode('LogBench / R6'));updateStorageBadge();scheduleStateSave();LB.paintStatus();}
async function refreshStorageInfo(){try{if(navigator.storage?.estimate){const n=await navigator.storage.estimate();storageState.quota=n.quota;storageState.usage=n.usage;}if(navigator.storage?.persisted)storageState.persistent=await navigator.storage.persisted();}catch{}updateStorageBadge();}
async function storagePanel(){await refreshStorageInfo();const local=files.filter(f=>f.source==='local'&&!f.sample);dialog('本机存储与备份',`<div class="storage-summary"><div><span>已缓存日志</span><strong>${local.length} 个</strong><small>${bytes(local.reduce((n,f)=>n+(f.size||0),0))} 原文（含回收站）</small></div><div><span>最近保存</span><strong>${storageState.stateSavedAt?new Date(storageState.stateSavedAt).toLocaleTimeString('zh-CN',{hour12:false}):'尚未确认'}</strong><small>设置变更自动写入</small></div></div><div class="storage-health ${storageState.issue?'bad':''}">${I(storageState.issue?'warning':'circlecheck')}<div><strong>${storageState.issue?'需要处理存储问题':'本机工作区已启用自动保存'}</strong><p>${E(storageState.issue||'文件原文、规则与草稿、列设置、视角、搜索、时间范围、书签和阅读位置均按当前浏览器保存。应用不设置过期时间。')}</p></div></div><div class="storage-persistence"><div><strong>${storageState.persistent===true?'已取得浏览器持久存储许可':storageState.persistent===false?'浏览器尚未授予持久存储':'持久存储状态不可查询'}</strong><p class="hint">${storageState.persistent===true?'浏览器不应因存储压力自动回收此站点数据。':'可向浏览器请求长期保留；请求可能被拒绝。保存数据与获得持久存储许可是两件事。'}</p></div>${B('请求持久保存','r5-persist','shield','sm',storageState.persistent===true||!navigator.storage?.persist?'disabled':'')}</div>${storageState.quota?`<p class="hint">站点存储用量 ${bytes(storageState.usage||0)} / 浏览器额度约 ${bytes(storageState.quota)}</p>`:''}<div class="storage-backup"><strong>工作区备份</strong><p>导出设置、规则草稿与全部本机日志原文。换浏览器、换部署地址或移动单文件 HTML 时，可从备份恢复。</p><div class="row">${B('导出完整备份','r5-backup','download','primary')}${B('导入备份','r5-import-backup','upload')}${storageState.issue?B('重试保存','r5-retry-storage','refresh'):''}</div></div><details class="storage-limits" open><summary>长期保存的边界</summary><p>请使用普通浏览窗口和固定的访问地址。无痕窗口、浏览器／系统清理、公司策略、配置损坏及更换浏览器档案都可能删除数据；网页不能阻止这些行为。没有持久许可时，存储压力下也可能回收数据。</p><p>协议、主机或端口变化会进入不同存储空间。单文件直接打开的存储行为取决于浏览器，建议通过固定的 localhost 或 HTTPS 地址使用。保存失败会提示，不会伪装成成功。</p></details>`,B('完成','close-dialog'),true);$('#dialog').classList.add('storage-dialog');}
async function requestPersistence(){try{if(!navigator.storage?.persist)throw Error('当前环境不支持持久存储请求。');storageState.persistent=await navigator.storage.persist();notify(storageState.persistent?'浏览器已授予持久存储许可。':'浏览器未授予持久存储；已保存的数据仍保留，建议导出备份。',!storageState.persistent);}catch(e){notify(e.message,true);}storagePanel();}
function workspaceBackup(){const snapshot=flushState()||stateSnapshot();return{format:'LogBench Workspace Backup',version:1,exportedAt:new Date().toISOString(),workspace:snapshot,files:files.filter(f=>f.source==='local'&&!f.sample).map(cacheRow)};}
function exportWorkspace(){const value=workspaceBackup();download('LogBench_workspace_'+new Date().toISOString().slice(0,10)+'.json',JSON.stringify(value), 'application/json;charset=utf-8');notify('已生成本机工作区备份，包含 '+value.files.length+' 个本机文件；请保管好文件。');}
function validateBackup(value){if(value?.format!=='LogBench Workspace Backup'||value.version!==1||value.workspace?.schema!=='logbench.workspace'||value.workspace.version!==1||!Array.isArray(value.files))throw Error('不支持的备份格式或版本，当前工作区未改变。');const problem=snapshotProblem(value.workspace);if(problem)throw Error(problem+'，当前工作区未改变。');const ids=new Set();let total=0;for(const f of value.files){if(typeof f.id!=='string'||!f.id.startsWith('local-')||ids.has(f.id)||typeof f.name!=='string'||typeof f.text!=='string'||typeof f.path!=='string')throw Error('文件条目无效或重复，当前工作区未改变。');ids.add(f.id);total+=f.text.length;if(f.text.length>64*1024*1024||total>128*1024*1024)throw Error('备份超过读取上限。');}const ps=value.workspace.state?.appliedProfiles;if(!Array.isArray(ps)||!ps.length||ps.length>200)throw Error('备份缺少有效规则配置。');ps.forEach(Engine.validate);return true;}
async function previewBackup(file){if(!file)return;try{if(file.size>160*1024*1024)throw Error('备份文件过大，请使用 LogBench 导出的备份。');const value=JSON.parse(await file.text());validateBackup(value);backupCandidate=value;dialog('恢复工作区备份',`<p>将恢复 <strong>${value.files.length} 个本机文件</strong>以及备份中的规则、草稿、阅读设置和视角。</p><p class="hint">此操作替换当前本机工作区。建议先导出当前备份；不会修改服务器文件。数据库替换在同一个事务内完成，失败不会留下半份文件库。</p>`,B('取消','close-dialog')+B('先备份当前','r5-backup','download')+B('替换并恢复','r5-confirm-backup','restore','primary'));}catch(e){backupCandidate=null;notify('无法读取备份：'+e.message,true);}}
async function restoreBackup(){if(!backupCandidate)return;const value=backupCandidate;validateBackup(value);try{const db=await cacheDB(),tx=db.transaction(['files','meta'],'readwrite');const fs=tx.objectStore('files');fs.clear();for(const f of value.files)fs.put(f);tx.objectStore('meta').put({id:'workspace',value:{...value.workspace,savedAt:Date.now()}});await transactionDone(tx);storageState.ready=false;clearTimeout(stateTimer);clearTimeout(idbTimer);files=files.filter(f=>f.source!=='local'||f.sample);try{localStorage.setItem('logbench-r6-workspace',JSON.stringify({...value.workspace,savedAt:Date.now()}));}catch{}for(const[k,v]of Object.entries(value.workspace.aux||{}))if(AUX_KEYS.includes(k))save(k,v);$('#dialog').close();storageState.issue='';storageState.restoreBlocked=false;storageState.unsaved.clear();await restoreCache();notify('备份中的本机文件与设置已恢复。');backupCandidate=null;}catch(e){storageIssue('备份恢复未完成：'+e.message);notify(storageState.issue,true);}}
async function retryStorage(){if(storageState.restoreBlocked){notify('已有缓存格式异常，已停止自动覆盖。请先导出备份，再导入一份有效备份恢复。',true);return;}storageState.issue='';try{for(const f of files.filter(f=>f.source==='local'&&!f.sample))await cachePut(f);flushState();await writeSnapshot(stateSnapshot());}catch{}storagePanel();}
// Every committed UI change is captured. Async operations trigger render() after completion.
window.addEventListener('click',()=>queueMicrotask(scheduleStateSave),true);
window.addEventListener('input',()=>queueMicrotask(scheduleStateSave),true);
window.addEventListener('change',()=>queueMicrotask(scheduleStateSave),true);
window.addEventListener('keydown',()=>queueMicrotask(scheduleStateSave),true);
document.addEventListener('scroll',e=>{if(e.target.matches?.('[data-scroll],.comparison-scroll'))scheduleStateSave();},true);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')flushState();});
window.addEventListener('pagehide',()=>{const s=flushState();if(s)writeSnapshot(s);});
window.addEventListener('beforeunload',e=>{flushState();if(storageState.unsaved.size||storageState.importing||storageState.writing){e.preventDefault();e.returnValue='';}});
const workspaceInput=document.createElement('input');workspaceInput.type='file';workspaceInput.id='workspace-input';workspaceInput.accept='.json';workspaceInput.hidden=true;document.body.append(workspaceInput);
function about(){dialog('LogBench · 阅读与规则',`<p>Traceback 卡片、节点分页、列显隐、本机工作区恢复、时间线条件与具体规则证据。</p><p class="hint">日志与规则在本机解析；服务器文件与身份来自 /api/v2/*。</p><p class="hint">本机存储不设过期时间，存储许可和失败状态可在底部查看，建议定期导出完整工作区备份。</p>`);}

/* R6 remote ingestion. Data supplied by the /api/v2 adapter (LB). */
const R6_KEY='logbench-r6-workspace';
const R6_SEED={
 roots:[{id:'main',name:'生产日志库',path:'/log/Logbench/data',state:'online',cache:'/log/Logbench/index',free:'2.6 TiB',defaultRule:'CPQPSGX 标准',type:'本地磁盘'},{id:'history',name:'历史归档库',path:'/data/Logbench/archive',state:'online',cache:'/log/Logbench/index',free:'1.1 TiB',defaultRule:'通用方括号',type:'本地磁盘'},{id:'cold',name:'维护日志库',path:'/srv/logbench/maintenance',state:'offline',cache:'/log/Logbench/index',free:'未挂载',defaultRule:'设备诊断',type:'独立挂载点'}],
 sources:[
 {id:'psg-a',name:'PSG-A · 主控日志',protocol:'sftp',endpoint:'psg-a.internal',port:22,credential:'只读采集密钥',mode:'live',status:'following',poll:2,scan:60,first:'all',verification:'append',readers:'管理员与研发组',count:86,raw:'924 MiB',indexed:'924 MiB',lag:'约 2 秒',coverage:100,maps:[{remote:'/log/psgtest9/debug/sw',root:'main',target:'psg-a/sw',rule:'CPQPSGX 标准'},{remote:'/log/psgtest9/debug/algorithm',root:'history',target:'psg-a/algorithm',rule:'CPQPSGX 标准'}]},
 {id:'psg-b',name:'PSG-B · 设备诊断',protocol:'smb',endpoint:'//psg-b.internal/logs',port:445,credential:'诊断共享只读账号',mode:'live',status:'recovering',poll:3,scan:60,first:'all',verification:'append',readers:'仅管理员',count:42,raw:'688 MiB',indexed:'640 MiB',lag:'落后 48 MiB',coverage:93,maps:[{remote:'/diagnostic',root:'main',target:'psg-b/diagnostic',rule:'设备诊断'}]},
 {id:'build',name:'构建中心 · 历史日志',protocol:'http',endpoint:'https://build.example.internal/logs/',port:443,credential:'构建服务 Token',mode:'schedule',status:'idle',poll:300,scan:300,first:'all',verification:'version',readers:'管理员与研发组',count:118,raw:'12.4 GiB',indexed:'12.4 GiB',lag:'下一次 14:40',coverage:100,maps:[{remote:'releases/',root:'history',target:'build/releases',rule:'通用方括号'}]},
 {id:'legacy',name:'旧机台 · SCP 兼容',protocol:'scp',endpoint:'legacy.internal',port:22,credential:'旧设备只读密钥',mode:'snapshot',status:'limited',poll:600,scan:600,first:'all',verification:'snapshot',readers:'仅管理员',count:17,raw:'3.8 GiB',indexed:'3.8 GiB',lag:'仅完整快照',coverage:100,maps:[{remote:'/var/log/device',root:'history',target:'legacy/device',rule:'设备诊断'}]}
 ],
 events:[{at:'14:36:04',source:'PSG-A',kind:'已提交',tone:'good',text:'新增字节已归档；索引提交到 924 MiB。当前文件继续追踪。'},{at:'14:35:59',source:'PSG-B',kind:'恢复采集',tone:'blue',text:'连接恢复，文件代次与已提交前缀通过校验。从 688 MiB 继续；解析队列独立追赶。'},{at:'14:35:42',source:'PSG-B',kind:'连接中断',tone:'warn',text:'保留最后确认偏移，不清空本地文件。自动重试采用退避。'},{at:'14:30:12',source:'构建中心',kind:'任务完成',tone:'good',text:'新归档 3 个稳定版本文件。其余缓存有效，未重复解析。'},{at:'14:10:00',source:'旧机台',kind:'能力降级',tone:'warn',text:'仅提供传统 SCP。禁止持续追踪，按完整快照计划归档。'}],
 tuning:{collect:4,perHost:2,parse:2,chunk:4,poll:2,memory:16,backfill:2},
 tab:'sources',libraryRoot:'all',selected:null,sourceFilter:'all',search:'',liveSource:null,liveFollow:false,newLines:0
};
let R6=clone(R6_SEED);try{const x=JSON.parse(localStorage.getItem(R6_KEY)||'null');if(x&&x.schema===1&&Array.isArray(x.state?.roots)&&Array.isArray(x.state?.sources))R6={...R6,...x.state,selected:null,search:'',liveSource:null,liveFollow:false};}catch{}
let r6Draft=null,r6Step=0,r6Probe=false,r6ProbeKind='ready',r6StorageWarning=false;
const r6Protocols={sftp:'SSH / SFTP',smb:'SMB',http:'HTTP(S)',scp:'SCP 兼容'};
const r6States={following:['持续追踪','good'],recovering:['续采 / 追赶','blue'],idle:['计划就绪',''],limited:['快照模式','warn'],paused:['已暂停',''],error:['需要处理','bad']};
const r6Btn=(label,action,icon,cls='',extra='')=>`<button class="btn ${cls}" data-r6="${action}" ${extra}>${icon?I(icon):''}<span>${E(label)}</span></button>`;
const r6Badge=(text,tone='')=>`<span class="r6-badge ${tone}"><i class="status-dot"></i>${E(text)}</span>`;
const r6Root=id=>R6.roots.find(x=>x.id===id);
const r6Source=id=>R6.sources.find(x=>x.id===id);
function r6Persist(){try{localStorage.setItem(R6_KEY,JSON.stringify({schema:1,state:R6}));}catch{if(!r6StorageWarning){r6StorageWarning=true;notify('设置仅保留在本次会话，浏览器拒绝本机保存。',true);}}}
function r6Event(source,kind,text,tone=''){R6.events.unshift({at:new Date().toLocaleTimeString('zh-CN',{hour12:false}),source,kind,text,tone});R6.events=R6.events.slice(0,100);r6Persist();}
function r6Capabilities(p,scenario='ready'){
 if(p==='scp')return{list:false,range:false,identity:false,live:false,kind:'快照归档',text:'传统 SCP 不提供可供本工具承诺的范围续传。保留完整快照；持续追踪不可用。'};
 if(p==='http')return{list:scenario!=='nolist',range:scenario!=='norange',identity:true,live:false,kind:scenario==='norange'?'完整快照':'版本续传',text:'HTTP 的 ETag 变化不能证明只发生追加。普通目录下载默认按稳定版本归档；持续追加需要额外的代次与不可变前缀约定。'};
 return{list:true,range:true,identity:true,live:true,kind:'增量归档',text:'逐字节续采以文件代次和已提交内容校验为前提。源端已覆盖或删除的字节不能凭偏移恢复。'};
}
function r6PathError(m){if(!m.remote.trim())return'请填写远程目录。';if(!m.target.trim())return'请填写存储子目录。';if(m.target.startsWith('/')||m.target.split(/[\\/]/).some(x=>x==='..'||x==='.'||!x)||/[\x00-\x1f]/.test(m.target))return'存储子目录必须为安全的相对路径，不能包含 ..、空路径段或控制字符。';if(m.depth!==undefined&&(!Number.isInteger(Number(m.depth))||Number(m.depth)<0||Number(m.depth)>32))return'目录扫描深度须为 0–32 的整数。';const root=r6Root(m.root);if(!root||root.state!=='online')return'选定存储位置离线，请先恢复挂载或改用在线位置。';return null;}
function r6Validate(d){if(!d.name?.trim())return'请输入日志源名称。';if(!d.endpoint?.trim())return'请输入服务器或地址。';if(!Number.isInteger(d.port)||d.port<1||d.port>65535)return'端口须为 1–65535 的整数。';if(!Number.isInteger(d.poll)||d.poll<1||!Number.isInteger(d.scan)||d.scan<1)return'检查周期必须是正整数。';if(!d.maps?.length)return'至少需要一条目录映射。';for(const m of d.maps){const e=r6PathError(m);if(e)return e;}const all=[...R6.sources.filter(x=>x.id!==d.id).flatMap(x=>x.maps.map(m=>({...m,owner:x.name}))),...d.maps.map(m=>({...m,owner:d.name}))];for(let i=0;i<all.length;i++)for(let j=i+1;j<all.length;j++)if(all[i].root===all[j].root&&(all[i].target===all[j].target||all[i].target.startsWith(all[j].target+'/')||all[j].target.startsWith(all[i].target+'/')))return'目标子目录重叠：'+all[i].owner+' 与 '+all[j].owner+'。请使用独立目录。';if(d.mode==='live'&&!r6Capabilities(d.protocol).live)return'此协议未确认持续增量能力，请改用定时 / 快照归档。';return null;}
function r6Status(x){const [label,tone]=r6States[x.status]||['未知','warn'];return r6Badge(label,tone);}
function r6Targets(x){return x.maps.map(m=>`<div class="r6-route">${I('folder','sm')}<span>${E(r6Root(m.root)?.name||'未配置')}<span class="muted"> / </span><code>${E(m.target)}</code></span></div>`).join('');}
function r6SourceRows(){const q=R6.search.toLowerCase();return R6.sources.filter(x=>(R6.sourceFilter==='all'||x.status===R6.sourceFilter)&&(!q||[x.name,x.endpoint,...x.maps.map(m=>m.remote+' '+m.target)].join(' ').toLowerCase().includes(q)));}
function r6Page(){const tabs=[['sources','远程日志源',R6.sources.length],['storage','存储位置',R6.roots.length],['cache','解析缓存',LB.state.cache?.items?.length??0],['events','运行记录',R6.events.length]];return `<section class="r6-content collection-center"><header class="collection-head"><div class="r6-tabs" role="tablist" aria-label="采集中心视图">${tabs.map(([id,name,count])=>`<button id="collection-tab-${id}" class="r6-tab ${R6.tab===id?'active':''}" role="tab" tabindex="${R6.tab===id?0:-1}" aria-controls="collection-panel" aria-selected="${R6.tab===id}" data-r6="tab" data-id="${id}">${name}<small>${count}</small></button>`).join('')}</div><div class="collection-actions">${r6Btn('资源设置','tuning','sliders','ghost')}${R6.tab==='sources'?r6Btn('添加日志源','new-source','plus','primary'):R6.tab==='storage'?r6Btn('添加存储位置','new-root','plus','primary'):''}</div></header><div id="collection-panel" class="collection-panel" role="tabpanel" aria-labelledby="collection-tab-${R6.tab}">${R6.tab==='sources'?r6Sources():R6.tab==='storage'?r6Storage():R6.tab==='cache'?r6Cache():r6Events()}</div></section>`;}
function r6Sources(){const rows=r6SourceRows();return `<div class="r6-toolbar"><div class="search-box">${I('search')}<input id="r6-source-search" value="${E(R6.search)}" placeholder="搜索名称、主机、目录…" aria-label="搜索远程日志源"></div><select class="select" id="r6-source-filter" aria-label="日志源状态">${[['all','全部状态'],...Object.entries(r6States).map(([id,v])=>[id,v[0]])].map(([id,t])=>`<option value="${id}" ${R6.sourceFilter===id?'selected':''}>${t}</option>`).join('')}</select><span class="spacer"></span><span class="collection-count">${rows.length} 项</span>${r6Btn('','refresh','refresh','icon ghost','aria-label="刷新日志源状态"')}</div><div class="r6-pane"><div class="r6-table-scroll" data-scroll="r6-sources">${rows.length?`<table class="r6-table collection-sources"><colgroup><col style="width:26%"><col style="width:25%"><col style="width:16%"><col style="width:11%"><col style="width:11%"><col style="width:11%"></colgroup><thead><tr><th>日志源</th><th>目录 / 存储位置</th><th>同步状态</th><th class="collection-number">归档量</th><th class="collection-number">索引覆盖</th><th>操作</th></tr></thead><tbody>${rows.map(x=>`<tr class="${R6.selected===x.id?'selected':''}"><td><div class="r6-source-title"><span class="collection-symbol">${I('server')}</span><div class="grow"><button class="r6-link" data-r6="source-detail" data-id="${E(x.id)}">${E(x.name)}</button><div class="r6-meta">${E(r6Protocols[x.protocol])} <span class="mono">${E(x.endpoint)}</span></div></div></div></td><td><div class="r6-path">${E(x.maps[0]?.remote||'未配置')}</div><div class="r6-meta">${E(r6Root(x.maps[0]?.root)?.name||'未配置')}${x.maps.length>1?'　+'+(x.maps.length-1)+' 个目录':''}</div></td><td>${r6Status(x)}<div class="r6-meta">${E(x.lag)}</div></td><td class="collection-number"><span class="mono">${E(x.raw)}</span><div class="r6-meta">${E(x.count)} 个文件</div></td><td class="collection-number">${x.count?`<span class="mono">${E(x.coverage)}%</span><div class="r6-progress" aria-label="索引覆盖 ${E(x.coverage)}%"><span style="width:${Math.max(0,Math.min(100,Number(x.coverage)||0))}%"></span></div>`:'<span class="r6-meta">未建立</span>'}</td><td><div class="r6-inline-actions">${r6Btn('详情','source-detail',null,'ghost sm',`data-id="${E(x.id)}"`)}${r6Btn('','pause',x.status==='paused'?'play':'pause','icon ghost sm',`data-id="${E(x.id)}" aria-label="${x.status==='paused'?'继续':'暂停'} ${E(x.name)}"`)}</div></td></tr>`).join('')}</tbody></table>`:`<div class="r6-empty">${I('search','lg')}<strong>${R6.search||R6.sourceFilter!=='all'?'没有匹配的日志源':'尚未添加日志源'}</strong>${R6.search||R6.sourceFilter!=='all'?r6Btn('清除筛选','clear-search','clear','sm'):''}</div>`}</div>${R6.selected&&r6Source(R6.selected)?r6Detail(r6Source(R6.selected)):''}</div>`;}
function r6Detail(x){const cap=r6Capabilities(x.protocol);return`<aside class="r6-drawer" aria-label="日志源详情"><div class="r6-drawer-head">${I('server')}<span class="grow">${E(x.name)}</span>${r6Btn('','close-detail','close','icon ghost sm','aria-label="关闭日志源详情"')}</div><div class="r6-drawer-body"><div class="row">${r6Status(x)}<span class="spacer"></span><span class="r6-sub">${r6Protocols[x.protocol]}</span></div><h3 class="r6-section-title">当前采集链路</h3><ol class="r6-steps"><li class="r6-step"><span class="r6-step-mark">${I('check','sm')}</span><div><strong>发现目录与文件</strong><p>扫描 ${x.maps.length} 个目录 · ${x.count} 个文件已登记</p></div></li><li class="r6-step"><span class="r6-step-mark">${I('check','sm')}</span><div><strong>${x.status==='recovering'?'已恢复连接，继续补采':'原文持久归档'}</strong><p>${x.raw} 已确认落盘 · 不重复写入已提交范围</p></div></li><li class="r6-step ${x.coverage<100?'warn':''}"><span class="r6-step-mark">${I(x.coverage<100?'clock':'check','sm')}</span><div><strong>${x.coverage<100?'增量索引追赶中':'索引已追上归档'}</strong><p>${x.indexed} 可查询 · ${x.coverage}% 当前覆盖</p></div></li></ol><div class="r6-watermark"><div><small>已归档检查点</small><b>${x.raw}</b></div><div><small>已索引检查点</small><b>${x.indexed}</b></div><div><small>配置版本</small><b class="mono">r${LB.state.revision}</b></div></div>${x.status==='recovering'?'<div class="r6-note warn">网络已恢复不等于解析已完成。原文与索引分别记录进度，缺失段会显式标记。</div>':''}<h3 class="r6-section-title">目录映射</h3>${x.maps.map(m=>`<div class="r6-map-line"><div class="r6-mapping-dir">${I('folder','sm')}<code>${E(m.remote)}</code></div><div class="r6-meta">${I('arrow','sm')} ${E(r6Root(m.root)?.path+'/'+m.target)}</div><div class="r6-meta">继承规则：${E(m.rule)}</div></div>`).join('')}<details><summary>连接能力与采集约束</summary><h3 class="r6-section-title">连接能力</h3><div class="r6-capabilities">${r6Badge(cap.list?'目录发现':'目录清单需配置',cap.list?'good':'warn')}${r6Badge(cap.range?'范围读取':'无范围续传',cap.range?'blue':'warn')}${r6Badge(cap.live?'追加追踪':'版本快照',cap.live?'good':'')}</div><p class="r6-mininote">${E(cap.text)}</p><h3 class="r6-section-title">归档与访问</h3><dl class="r6-kv"><dt>源文件删除</dt><dd>保留本地归档，不传播删除</dd><dt>文件轮转</dt><dd>按代次归档，旧代次继续补齐</dd><dt>首次采集</dt><dd>从头归档，历史补采与新日志分别排队</dd><dt>访问范围</dt><dd>${E(x.readers)}</dd><dt>采集身份</dt><dd>${E(x.credential)} · 仅引用，不显示密钥</dd><dt>数据版本</dt><dd class="mono">revision ${LB.state.revision} · ${E(LB.state.freshness)}</dd></dl></details></div><div class="r6-drawer-foot">${r6Btn('打开日志','read-source','logs','primary',`data-id="${x.id}"`)}${r6Btn('编辑','edit-source','edit','',`data-id="${x.id}"`)}${r6Btn(x.status==='paused'?'继续':'暂停','pause',x.status==='paused'?'play':'pause','',`data-id="${x.id}"`)}</div></aside>`;}
function r6Storage(){return`<div class="r6-toolbar"><details class="r6-sub"><summary>总库如何聚合</summary>逻辑总库聚合多个物理根目录；文件用存储位置 ID 区分，不靠同名路径合并。</details><span class="spacer"></span>${r6Btn('查看文件库','library','folder','sm')}</div><div class="r6-pane"><div class="r6-table-scroll"><table class="r6-table r6-root-table"><thead><tr><th>存储位置</th><th>物理目录</th><th>状态 / 可用空间</th><th>映射数</th><th>操作</th></tr></thead><tbody>${R6.roots.map(x=>`<tr><td><div class="r6-source-title"><div class="r6-source-symbol">${I('database')}</div><div class="r6-entity">${E(x.name)}<div class="r6-meta">${E(x.type)} · ${x.id}</div></div></div></td><td><div class="r6-path">${E(x.path)}</div><div class="r6-meta">索引：${E(x.cache)}</div></td><td>${r6Badge(x.state==='online'?'可读写':'离线 / 未挂载',x.state==='online'?'good':'bad')}<div class="r6-meta">${E(x.free)}${x.state==='online'?' 可用':''}</div></td><td>${R6.sources.flatMap(s=>s.maps).filter(m=>m.root===x.id).length}</td><td><div class="r6-inline-actions">${r6Btn('浏览','root-browse','folder','ghost sm',`data-id="${x.id}" ${x.state==='offline'?'disabled':''}`)}${r6Btn('设置','edit-root','settings','ghost sm',`data-id="${x.id}"`)}</div></td></tr>`).join('')}</tbody></table></div></div>`;}
function r6Cache(){const items=LB.state.cache?.items||[];return `<div class="r6-toolbar"><span class="collection-count">${items.length} 个文件</span><span class="spacer"></span>${items.length?r6Btn('重建规则结果','cache-rebuild','refresh','sm'):''}</div><div class="r6-pane"><div class="r6-table-scroll">${items.length?`<table class="r6-table"><thead><tr><th>文件 / 代次</th><th>归档原文</th><th>结构索引</th><th>规则结果</th><th>操作</th></tr></thead><tbody>${items.map(x=>`<tr><td><button class="r6-link" data-r6="read-source" data-id="${E('srv-'+x.fileId)}">${E(x.displayName||x.fileId)}</button><div class="r6-meta">${E(x.relPath||'')} · ${E(x.generation)}</div></td><td class="mono">${E(x.size)} B</td><td>${E(x.layers?.structure?.state||'未建立')}</td><td>${E(x.layers?.rules?.state||'未建立')}</td><td>${r6Btn('详情','cache-detail','info','ghost sm',`data-id="${E(x.fileId)}"`)}</td></tr>`).join('')}</tbody></table>`:`<div class="r6-empty">${I('database','lg')}<strong>暂无解析缓存</strong></div>`}</div></div>`;}
function r6Events(){return`<div class="r6-toolbar"><span class="spacer"></span>${r6Btn('导出运行记录','export-events','download','sm')}</div><div class="r6-pane"><div class="r6-table-scroll"><ol class="r6-log-list">${R6.events.map(e=>`<li><time>${E(e.at)}</time><span>${E(e.source)}</span><span>${r6Badge(e.kind,e.tone)}</span><div>${E(e.text)}</div></li>`).join('')}</ol></div></div>`;}
function r6NewSource(id){r6Draft=clone(id?r6Source(id):{id:null,name:'',protocol:'sftp',endpoint:'',port:22,credential:'只读采集密钥',mode:'live',poll:2,scan:60,first:'all',verification:'append',readers:'仅管理员',maps:[{remote:'/log/',root:'main',target:'new-source/logs',rule:'CPQPSGX 标准'}]});r6Step=0;r6Probe=false;r6ProbeKind='ready';r6Wizard();}
function r6CaptureDraft(){if(!r6Draft)return;for(const el of $$('[data-r6-field]'))r6Draft[el.dataset.r6Field]=el.type==='number'?Number(el.value):el.value;for(const el of $$('[data-r6-map]'))r6Draft.maps[Number(el.dataset.r6Index)][el.dataset.r6Map]=el.value;}
const r6Field=(label,key,value,options={})=>`<div class="field"><label class="label" for="r6-field-${key}">${label}</label><input class="input ${options.mono?'mono':''}" id="r6-field-${key}" data-r6-field="${key}" type="${options.type||'text'}" value="${E(value??'')}" ${options.type==='number'?'min="1" max="65535"':''} ${options.placeholder?`placeholder="${E(options.placeholder)}"`:''}><div class="hint">${options.hint||''}</div></div>`;
function r6StepConnection(){const d=r6Draft;return`<div class="r6-protocols" role="group" aria-label="传输协议">${Object.entries(r6Protocols).map(([p,l])=>`<button type="button" data-r6="protocol" data-id="${p}" class="${d.protocol===p?'active':''}" aria-pressed="${d.protocol===p}">${l}</button>`).join('')}</div>${r6Field('日志源名称','name',d.name,{placeholder:'例如：机台 A · 主控日志'})}${r6Field(d.protocol==='http'?'HTTP 基础地址':d.protocol==='smb'?'服务器 / 共享名称':'服务器地址','endpoint',d.endpoint,{mono:true,placeholder:d.protocol==='http'?'https://host.example/logs/':d.protocol==='smb'?'//host.example/logs':'host.example'})}<div class="r6-two">${r6Field('端口','port',d.port,{type:'number'})}<div class="field"><label class="label" for="r6-field-credential">凭据引用</label><select class="select" id="r6-field-credential" data-r6-field="credential">${['只读采集密钥','诊断共享只读账号','构建服务 Token','旧设备只读密钥'].map(x=>`<option ${x===d.credential?'selected':''}>${x}</option>`).join('')}</select></div></div><div class="r6-note ${d.protocol==='scp'||d.protocol==='http'?'warn':''}">${I('shield')}${E(r6Capabilities(d.protocol).text)}</div><label class="r6-checkrow"><input type="checkbox" data-r6-field="verifyHost" checked>${d.protocol==='sftp'||d.protocol==='scp'?'强制验证已登记的 SSH 主机密钥':d.protocol==='http'?'HTTPS 校验证书；HTTP 明文需明确允许':'使用 SMB 2/3；签名与加密能力在预检时确认'}</label>`;}
function r6StepMappings(){const d=r6Draft;return`${d.maps.map((m,i)=>`<div class="r6-map-editor"><div class="row"><strong style="font-size:12px">目录映射 ${i+1}</strong><span class="spacer"></span>${r6Btn('移除','remove-map','trash','ghost sm',`data-index="${i}" ${d.maps.length===1?'disabled':''}`)}</div><label class="label" for="r6-map-remote-${i}">远程目录</label><input class="input mono" id="r6-map-remote-${i}" data-r6-map="remote" data-r6-index="${i}" value="${E(m.remote)}"><div class="r6-two" style="margin-top:10px"><div class="field"><label class="label" for="r6-map-root-${i}">存储位置</label><select class="select" id="r6-map-root-${i}" data-r6-map="root" data-r6-index="${i}">${R6.roots.map(x=>`<option value="${x.id}" ${m.root===x.id?'selected':''}>${E(x.name)}${x.state==='offline'?' · 离线':''}</option>`).join('')}</select></div><div class="field"><label class="label" for="r6-map-target-${i}">存储子目录</label><input class="input mono" id="r6-map-target-${i}" data-r6-map="target" data-r6-index="${i}" value="${E(m.target)}"></div></div><div class="r6-path-preview" data-map-preview="${i}">${E((r6Root(m.root)?.path||'')+'/'+m.target)}</div><div class="r6-two" style="margin-top:12px"><div><label class="label" for="r6-map-rule-${i}">目录默认规则</label><select class="select" id="r6-map-rule-${i}" data-r6-map="rule" data-r6-index="${i}">${['CPQPSGX 标准','设备诊断','通用方括号'].map(x=>`<option ${x===m.rule?'selected':''}>${x}</option>`).join('')}</select></div><div><label class="label" for="r6-map-include-${i}">文件范围</label><input class="input mono" id="r6-map-include-${i}" data-r6-map="include" data-r6-index="${i}" value="${E(m.include||'*.log, *.log.*')}" aria-label="日志匹配模式"><div class="hint">逗号分隔；建议包含轮转文件。</div><details style="margin-top:8px"><summary>排除模式与扫描深度</summary><label class="label" for="r6-map-exclude-${i}">排除模式</label><input class="input mono" id="r6-map-exclude-${i}" data-r6-map="exclude" data-r6-index="${i}" value="${E(m.exclude||'.logbench-*')}"><label class="label" for="r6-map-depth-${i}">递归深度</label><input class="input" type="number" min="0" max="32" id="r6-map-depth-${i}" data-r6-map="depth" data-r6-index="${i}" value="${E(m.depth??8)}"></details></div></div></div>`).join('')}${r6Btn('增加一个远程目录','add-map','plus','sm')}`;}
function r6StepPolicy(){const d=r6Draft,cap=r6Capabilities(d.protocol);return`<div class="r6-two"><div class="field"><label class="label" for="r6-field-mode">同步方式</label><select class="select" id="r6-field-mode" data-r6-field="mode"><option value="live" ${d.mode==='live'?'selected':''} ${!cap.live?'disabled':''}>持续增量追踪</option><option value="schedule" ${d.mode==='schedule'?'selected':''}>定时同步</option><option value="snapshot" ${d.mode==='snapshot'?'selected':''}>稳定文件快照</option></select></div><div class="field"><label class="label" for="r6-field-first">首次接入</label><select class="select" id="r6-field-first" data-r6-field="first"><option value="all" ${d.first==='all'?'selected':''}>完整归档历史 + 继续追踪</option><option value="new" ${d.first==='new'?'selected':''}>仅接入后新增（保留缺口标记）</option></select></div></div><div class="r6-two">${r6Field('活动文件检查间隔（秒）','poll',d.poll,{type:'number',hint:'只是目标检查周期；可见延迟还包含远端刷盘、传输和索引排队。'})}${r6Field('目录发现间隔（秒）','scan',d.scan,{type:'number',hint:'只扫描元数据，不每次重读整份日志。'})}</div><div class="field"><label class="label" for="r6-field-readers">归档访问范围</label><select class="select" id="r6-field-readers" data-r6-field="readers"><option ${d.readers==='仅管理员'?'selected':''}>仅管理员</option><option ${d.readers==='管理员与研发组'?'selected':''}>管理员与研发组</option></select><div class="hint">新远程日志默认不向未登记访客开放。</div></div><h3 class="r6-section-title">可靠性策略</h3><label class="r6-checkrow"><input type="checkbox" data-r6-field="policy" checked>断线后从已确认检查点继续，并重新验证文件代次</label><label class="r6-checkrow"><input type="checkbox" data-r6-field="policy" checked>保留轮转与截断前的原文，不传播远端删除</label><label class="r6-checkrow"><input type="checkbox" data-r6-field="policy" checked>存储离线、权限失败或空间不足时暂停，明确告警</label><label class="r6-checkrow"><input type="checkbox" data-r6-field="policy" checked>保存服务器解析索引；没有新增字节时不重复解析</label><div class="r6-note warn" style="margin-top:16px">${I('info')}断线期间已在源端删除或覆盖的内容无法补回。系统标记缺口，不宣称“零丢失”。</div>`;}
function r6StepReview(){const d=r6Draft;return`<div class="r6-review-grid"><section><h3>连接与访问</h3><dl class="r6-kv"><dt>协议</dt><dd>${r6Protocols[d.protocol]}</dd><dt>服务器</dt><dd class="mono">${E(d.endpoint)}</dd><dt>访问范围</dt><dd>${E(d.readers)}</dd></dl></section><section><h3>采集方式</h3><dl class="r6-kv"><dt>模式</dt><dd>${{live:'持续增量',schedule:'定时同步',snapshot:'稳定文件快照'}[d.mode]}</dd><dt>目录映射</dt><dd>${d.maps.length} 个</dd><dt>历史范围</dt><dd>${d.first==='all'?'完整归档历史':'仅接入后新增，有历史缺口'}</dd></dl></section></div><h3 class="r6-section-title">预检与失败恢复</h3><div class="row">${r6Btn('运行预检','probe','shield','sm')}<span class="r6-sub">验证连接、主机密钥、目录发现、范围读取与目标子目录权限；不会写入远程文件。</span></div><div class="r6-probe" id="r6-probe-result">${LB.probeResult?LB.probeResult.checks.map(c=>`<div class="r6-probe-row ${c.ok?'':'bad'}">${I(c.ok?'check':'error')}<span>${E(c.label)} · ${E(c.detail)}</span></div>`).join(''):'<div class="r6-mininote">尚未运行预检。保存前必须通过全部检查。</div>'}</div>${LB.probeResult&&!LB.probeResult.ok?'<div class="r6-note bad" style="margin-top:12px">预检未通过，保存已停用。修正凭据、主机密钥或文件清单后重新预检；不会自动降级为匿名连接。</div>':''}<div class="r6-errorbox" id="r6-wizard-error" role="alert"></div>`;}
function r6Wizard(){const titles=['连接','目录映射','采集策略','确认与预检'];const side=[['连接只是能力入口','SSH 优先使用 SFTP；SMB 与 HTTP 在连接后检测能力，不能仅凭协议名称承诺实时续采。','凭据由管理员在服务端管理，浏览器只保存引用，不保存密码。'],['一个目录，一条映射','管理员先登记存储位置。每条映射选择物理根目录，再指定不重叠的子目录。','相同文件名通过源 ID、相对路径与代次区分；跨库查询不复制文件。'],['归档与索引各自推进','原文先持久化，再推进确认检查点。解析可以稍后追上，不阻塞已有日志阅读。','日志持续写入时，尾部未完成的多行记录标为待完成，不伪造完整 Traceback。'],['先验证，再启用','预检验证连接、主机密钥、目录发现、偏移读取、目标权限与协议降级。','能力不足不会伪装成成功；未通过时保存按钮停用，草稿原样保留。']][r6Step];dialog(r6Draft.id?'编辑远程日志源':'添加远程日志源',`<div class="r6-wizard-tabs" role="tablist" aria-label="日志源设置步骤">${titles.map((t,i)=>`<button data-r6="wizard-step" data-index="${i}" class="${i===r6Step?'active':''}" role="tab" aria-selected="${i===r6Step}"><span class="step-no">${i+1}</span>${t}</button>`).join('')}</div><div class="r6-wizard-layout"><div class="r6-form">${[r6StepConnection,r6StepMappings,r6StepPolicy,r6StepReview][r6Step]()}</div></div>`,r6Btn('取消','wizard-cancel',null)+`<span class="spacer"></span>`+(r6Step?r6Btn('上一步','wizard-back','left'):'')+(r6Step<3?r6Btn('下一步','wizard-next','arrow','primary'):r6Btn('保存日志源','wizard-save','check','primary',LB.probeResult&&LB.probeResult.ok?'':'disabled')),true);$('#dialog').classList.add('r6-dialog');}
function r6ShowFormError(msg){let p=$('#r6-wizard-error');if(!p){p=document.createElement('div');p.id='r6-wizard-error';p.className='r6-errorbox';p.setAttribute('role','alert');$('#dialog .r6-form').prepend(p);}p.textContent=msg;}
function r6SaveSource(){LB.saveSource();}
function r6RootDialog(id){const x=id?r6Root(id):{id:null,name:'',path:'/log/Logbench/data-new',cache:'/log/Logbench/index',defaultRule:'CPQPSGX 标准'};R6.editRoot=id;dialog(id?'存储位置设置':'添加存储位置',`<div class="field"><label class="label" for="r6-root-name">位置名称</label><input class="input" id="r6-root-name" value="${E(x.name)}"></div><div class="field"><label class="label" for="r6-root-path">服务器绝对路径</label><input class="input mono" id="r6-root-path" value="${E(x.path)}" ${id?'readonly':''}><p class="hint">修改已有位置的物理目录需要单独迁移，不在此处直接重定向。</p></div><div class="field"><label class="label" for="r6-root-cache">本地索引目录</label><input class="input mono" id="r6-root-cache" value="${E(x.cache)}"></div><div class="r6-note">保存后会调用 /api/v2/storage-roots 的预检，验证运行账号权限、磁盘挂载身份和可用空间。验证失败不会启用该位置。</div><div id="r6-root-error" class="r6-errorbox" role="alert"></div>`,r6Btn('取消','modal-close')+r6Btn('保存存储位置','save-root','save','primary'));}
function r6Tuning(){const t=R6.tuning;dialog('采集与解析资源',`<div class="r6-two">${[['全局传输并发','collect'],['每个远程主机并发','perHost'],['常驻解析进程','parse'],['历史补采并发','backfill'],['读写块大小（MiB）','chunk'],['进程组内存预算（GiB）','memory']].map(([l,k])=>`<div class="field"><label class="label" for="r6-tune-${k}">${l}</label><input id="r6-tune-${k}" class="input" type="number" min="1" max="64" value="${t[k]}"></div>`).join('')}</div><details><summary>高级预算（其余 7 项）与调优建议</summary><dl class="r6-kv">${[['query_slots','查询槽位'],['total_cpu','CPU 总上限'],['hot_cache_bytes','热页缓存'],['io_mb','磁盘 I/O 上限'],['index_queue','索引队列'],['upload_slots','上传槽位'],['retention_days','运行记录保留天数']].map(([k,l])=>`<dt>${l}</dt><dd class="mono">${t[k]??'—'}</dd>`).join('')}</dl><p class="r6-mininote">应在目标机比较 1 / 2 / 4 个解析进程，观察 CPU、磁盘等待、网络、索引积压和前台请求延迟。</p></details><div class="r6-errorbox" id="r6-tune-error" role="alert"></div>`,r6Btn('取消','modal-close')+r6Btn('保存资源设置','save-tuning','save','primary'),true);}
function r6CacheDetail(id){const x=(LB.state.cache?.items||[]).find(x=>x.fileId===id);if(!x)return;R6.cacheSelected=id;dialog('解析缓存 · '+(x.displayName||id),`<dl class="r6-kv"><dt>文件标识</dt><dd>${E(id)}</dd><dt>代次</dt><dd>${E(x.generation)}</dd><dt>原文</dt><dd>${E(x.size)} B</dd><dt>结构索引</dt><dd>${E(x.layers?.structure?.state||'未建立')}</dd><dt>规则结果</dt><dd>${E(x.layers?.rules?.state||'未建立')}</dd></dl><p class="hint">重建保留归档原文，完成前继续使用旧结果。</p>`,r6Btn('关闭','modal-close')+r6Btn('重建规则结果','cache-rebuild','refresh','primary'));}
function r6OpenReading(id){const x=r6Source(id);R6.liveSource=id;R6.newLines=0;R6.liveFollow=false;const f=files.find(f=>f.id==='f1')||files.find(f=>f.source==='server');if(f){openFile(f.id);notify('已打开 '+x.name+' 的归档索引；正文由本机 Worker 读取，不上传服务端。');}}
function r6Livebar(){const x=r6Source(R6.liveSource);if(!x)return'';return`<div class="r6-livebar">${I('database','sm')}<strong>服务器索引阅读</strong><span>${E(x.name)}</span><span class="r6-sub">归档 ${x.raw} · 索引 ${x.coverage}%</span><span class="spacer"></span>${R6.newLines?r6Btn('新增 '+R6.newLines+' 条','read-new','down','sm'):''}${r6Btn(R6.liveFollow?'暂停跟随':'跟随最新','follow',R6.liveFollow?'pause':'play',`sm ${R6.liveFollow?'active':''}`)}</div>`;}
paths.database='M4 6c0-4 16-4 16 0s-16 4-16 0v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0';paths.pause='M8 4v16M16 4v16';
const r6OldSidebar=renderSidebar,r6OldHeader=renderHeader,r6OldRender=render,r6OldLogs=renderLogWorkspace;
renderSidebar=function(){return r6OldSidebar().replace('</nav>',`<button class="feature-button ${S.view==='sync'?'active':''}" data-r6="nav-sync" ${S.view==='sync'?'aria-current="page"':''}>${I('refresh')}<span>采集中心</span></button></nav>`).replace(/<div class="sidefoot">.*?<\/div>/,'');};
renderHeader=function(){return r6OldHeader().replace('<span class="current">undefined</span>','<span class="current">采集中心</span>').replace('<div class="header-right">',`<div class="header-right"><details class="app-tools"><summary class="btn ghost">${I('sliders')}<span>工具</span>${I('down')}</summary><div class="app-tools-menu"><button data-r6="rev2-jobs">${I('layers')}本机批量任务</button><button data-r6="rev2-search">${I('search')}渐进检索</button><button data-r6="rev2-upload">${I('upload')}上传到私有区</button><div class="app-tools-divider"></div><button data-act="nav" data-value="admin">${I('settings')}管理后台</button><button data-act="help">${I('book')}操作帮助</button></div></details>`);};
renderLogWorkspace=function(){return r6OldLogs().replace('<section class="workspace"',r6Livebar()+'<section class="workspace"');};
render=function(keep=true){if(S.view!=='sync'){r6OldRender(keep);$('.app-status .status-right')?.lastElementChild?.replaceChildren(document.createTextNode('LogBench / R6'));return;}const pos=preserve();liveDestroy();document.body.dataset.theme=S.theme;const root=$('#app');root.className=`lb-app ${!S.sidebar?'nav-collapsed':''}`;root.innerHTML=`${renderHeader()}<div class="mobile-scrim" data-act="toggle-nav"></div>${renderSidebar()}<main class="main">${r6Page()}<footer class="app-status"><span class="row">${I('lock','sm')}${LB.statusText()}</span><span class="status-right"><span class="extra">${LB.dataFreshness()}</span>${LB.scenarioSelect()}<span>LogBench / R6</span></span></footer></main>`;restore(pos,keep);LB.paintStatus();};
document.addEventListener('click',function(e){const el=e.target.closest('[data-r6]');if(!el)return;e.preventDefault();e.stopImmediatePropagation();const a=el.dataset.r6,id=el.dataset.id;switch(a){
case'nav-sync':S.view='sync';render(false);break;
case'tab':R6.tab=id;R6.selected=null;r6Persist();render(false);break;
case'source-detail':R6.selected=id;render();break;
case'close-detail':R6.selected=null;render();break;
case'clear-search':R6.search='';R6.sourceFilter='all';render(false);break;
case'refresh':LB.refresh();break;
case'pause':LB.pause(id);break;
case'new-source':LB.probeResult=null;r6NewSource();break;case'edit-source':LB.probeResult=null;r6NewSource(id);break;
case'wizard-cancel':case'modal-close':$('#dialog').close();r6Draft=null;break;
case'protocol':LB.probeResult=null;r6CaptureDraft();r6Draft.protocol=id;r6Draft.port=id==='http'?443:id==='smb'?445:22;if(!r6Capabilities(id).live)r6Draft.mode=id==='scp'?'snapshot':'schedule';r6Probe=false;r6Wizard();break;
case'wizard-next':r6CaptureDraft();if(r6Step===0&&(!r6Draft.name.trim()||!r6Draft.endpoint.trim())){r6ShowFormError('请填写日志源名称和服务器地址。');break;}if(r6Step===1){const err=r6Validate(r6Draft);if(err){r6ShowFormError(err);break;}}r6Step=Math.min(3,r6Step+1);r6Wizard();break;
case'wizard-back':r6CaptureDraft();r6Step=Math.max(0,r6Step-1);r6Wizard();break;
case'wizard-step':r6CaptureDraft();r6Step=Number(el.dataset.index);r6Wizard();break;
case'add-map':LB.probeResult=null;r6CaptureDraft();r6Draft.maps.push({remote:'/log/other',root:'main',target:'new-source/logs-'+(r6Draft.maps.length+1),rule:'CPQPSGX 标准'});r6Probe=false;r6Wizard();break;
case'remove-map':LB.probeResult=null;r6CaptureDraft();r6Draft.maps.splice(Number(el.dataset.index),1);r6Probe=false;r6Wizard();break;
case'probe':LB.probe();break;
case'wizard-save':LB.saveSource();break;
case'new-root':r6RootDialog();break;case'edit-root':r6RootDialog(id);break;
case'save-root':LB.saveRoot();break;
case'library':R6.libraryRoot='all';S.libSource='server';S.folder='';S.libSelected.clear();S.view='files';render(false);break;
case'root-browse':R6.libraryRoot=id;S.libSource='server';S.folder='';S.libSelected.clear();S.view='files';render(false);break;
case'storage-tab':R6.tab='storage';S.view='sync';render(false);break;
case'tuning':r6Tuning();break;
case'save-tuning':LB.saveTuning();break;
case'cache-detail':r6CacheDetail(id);break;
case'cache-rebuild':LB.rebuildCache();break;
case'export-events':LB.exportEvents();break;
case'read-source':r6OpenReading(id);break;
case'follow':R6.liveFollow=!R6.liveFollow;R6.newLines=0;render();if(R6.liveFollow)$('#log-scroll')?.scrollTo({top:$('#log-scroll').scrollHeight});break;

case'read-new':R6.newLines=0;R6.liveFollow=true;render();$('#log-scroll')?.scrollTo({top:$('#log-scroll').scrollHeight});break;
case'datalink':LB.datalink();break;
case'backup':LB.backup();break;
case'backup-export':LB.exportBackup();break;
case'backup-restore':LB.restoreBackup();break;
case'connect':LB.connect();break;
case'retry':LB.retry();break;
case'clear-filters':R6.search='';R6.sourceFilter='all';LB.setScenario('ready');render(false);break;
case'saved-view':S.scope=id;S.view='logs';render(false);notify('已切换阅读视角：'+(id==='issues'?'异常优先':'我的书签'));break;
case'rev2-jobs':LB.refreshJobsPanel();break;case'rev2-jobs-refresh':LB.refreshJobsPanel();break;case'rev2-search':LB.searchPanel();break;case'rev2-query-start':LB.progressiveQuery(false);break;case'rev2-query-next':LB.progressiveQuery(true);break;case'rev2-upload':LB.uploadPanel();break;case'rev2-upload-start':LB.uploadPrivate();break;} },true);
document.addEventListener('input',e=>{if(e.target.id==='r6-source-search'){R6.search=e.target.value;render(true);}if(e.target.matches('[data-r6-map]')){r6CaptureDraft();LB.probeResult=null;r6Probe=false;const i=e.target.dataset.r6Index,p=$('[data-map-preview="'+i+'"]');if(p)p.textContent=(r6Root(r6Draft.maps[i].root)?.path||'')+'/'+r6Draft.maps[i].target;}if(e.target.matches('[data-r6-field]')){LB.probeResult=null;r6Probe=false;const b=$('#dialog [data-r6="wizard-save"]');if(b)b.disabled=true;}});
document.addEventListener('change',e=>{if(e.target.id==='lb-scenario'){LB.setScenario(e.target.value);}if(e.target.id==='r6-source-filter'){R6.sourceFilter=e.target.value;render(false);}if(e.target.matches('[data-r6-map]')){r6CaptureDraft();LB.probeResult=null;r6Probe=false;const i=e.target.dataset.r6Index,p=$('[data-map-preview="'+i+'"]');if(p)p.textContent=(r6Root(r6Draft.maps[i].root)?.path||'')+'/'+r6Draft.maps[i].target;}if(e.target.matches('[data-lb-pref]'))LB.savePref(e.target.dataset.lbPref,e.target.type==='checkbox'?e.target.checked:e.target.value);if(e.target.matches('[data-visible-column]'))LB.savePref('columns.'+e.target.dataset.visibleColumn,e.target.checked);});
window.LogBenchRemotePreview={state:R6,capabilities:r6Capabilities,validate:r6Validate,getDraft:()=>r6Draft,reset:()=>{localStorage.removeItem(R6_KEY);location.reload();}};

document.addEventListener('click',e=>{const x=e.target.closest('[data-act="about"]');if(!x)return;e.preventDefault();e.stopImmediatePropagation();LB.datalink();},true);
document.addEventListener('wheel',e=>{if(R6.liveFollow&&e.deltaY<0&&e.target.closest('#log-scroll')){R6.liveFollow=false;const b=document.querySelector('[data-r6="follow"]');if(b){b.classList.remove('active');b.innerHTML=I('play')+'<span>跟随最新</span>';}notify('已暂停自动跟随；后台采集不受阅读位置影响。');}}, {capture:true,passive:true});

window.LogBenchPrototype={state:S,getFiles:()=>files,filtered,baseRecords,parseLocal,applyRange,render,runSearch,engine:Engine,indexRecords,processJobs,saveProfile,runTest,openPicker,applyPicker,restoreView,viewSnapshot,importFiles,locate,blockOpen,getVirtual:()=>logVirtual,getCompareVirtuals:()=>compareVirtuals,getCompareAxis,timelineData,parseInWorker,storage:storageState,workspaceBackup,validateBackup,hydrateSnapshot,flushState,restoreCache,cachePut,cacheGetAll,viewSnapshot};
/* ===== R6 RESTORE ADAPTER · PRODUCTION ======================================
 * 由 tools/build_r6_frontend.py 插入原型 IIFE 内部，因此可直接访问
 * R6 / S / render / r6Btn / I / E / $ / dialog / notify / download / clone /
 * r6Event / r6CaptureDraft / r6Draft / r6Probe / r6Wizard / r6ShowFormError /
 * r6Validate / r6Source / r6Root / r6Persist / files 等同作用域成员。
 *
 * 与预览版 adapter.js 暴露完全相同的接口，故渲染层零改动：
 *   statusText / dataFreshness / scenarioSelect / paintStatus / stateBanner /
 *   state / prefs / probeResult / setScenario / savePref / datalink / backup /
 *   exportBackup / restoreBackup / connect / retry / refresh / pause / probe /
 *   saveSource / saveRoot / saveTuning / rebuildCache / exportEvents /
 *   ENDPOINTS / SCENARIOS / request / hydrate。
 *
 * 真实契约来源：server/app.py、server/collectors/*、server/storage/*、
 * server/indexing/*、server/runtime/*、server/query/*、server/uploads/*。
 * 设计层（DOM/CSS/视觉节拍）由原型 HTML 提供，本文件不定义任何 UI 结构。
 * 未定义的新状态只用原型既有组件形态表达（计划 §4.4）。
 */
const LB = (function () {
  'use strict';

  /* ---- 真实端点表 -------------------------------------------------------- */
  var ENDPOINTS = {
    me:                 ['GET',   '/api/v1/me'],
    health:             ['GET',   '/api/v1/health'],
    preferences:        ['GET',   '/api/v1/preferences'],
    putPreferences:     ['PUT',   '/api/v1/preferences'],
    capabilities:       ['GET',   '/api/v2/capabilities'],
    intake:             ['GET',   '/api/v2/intake'],
    sources:            ['GET',   '/api/v2/sources'],
    source:             ['GET',   '/api/v2/sources/{id}'],
    createSource:       ['POST',  '/api/v2/sources'],
    updateSource:       ['PATCH', '/api/v2/sources/{id}'],
    sourceState:        ['GET',   '/api/v2/sources/{id}/state'],
    mappings:           ['GET',   '/api/v2/sources/{id}/mappings'],
    probe:              ['POST',  '/api/v2/sources/{id}/probe'],
    actions:            ['POST',  '/api/v2/sources/{id}/actions'],
    credentials:        ['GET',   '/api/v2/credentials'],
    adapters:           ['GET',   '/api/v2/collectors/adapters'],
    roots:              ['GET',   '/api/v2/storage-roots'],
    createRoot:         ['POST',  '/api/v2/storage-roots'],
    updateRoot:         ['PATCH', '/api/v2/storage-roots/{id}'],
    probeRoot:          ['POST',  '/api/v2/storage-roots/{id}/probe'],
    cache:              ['GET',   '/api/v2/cache'],
    cacheUsage:         ['GET',   '/api/v2/cache/usage'],
    rebuild:            ['POST',  '/api/v2/cache/rebuild'],
    purge:              ['POST',  '/api/v2/cache/purge'],
    jobs:               ['GET',   '/api/v2/jobs'],
    job:                ['GET',   '/api/v2/jobs/{id}'],
    jobActions:         ['POST',  '/api/v2/jobs/{id}/actions'],
    indexJobs:          ['GET',   '/api/v2/index-jobs'],
    createIndexJob:     ['POST',  '/api/v2/index-jobs'],
    runtime:            ['GET',   '/api/v2/runtime'],
    budget:             ['GET',   '/api/v2/runtime/budget'],
    putBudget:          ['PUT',   '/api/v2/runtime/budget'],
    library:            ['GET',   '/api/v2/library'],
    libraryTree:        ['GET',   '/api/v2/library/tree'],
    librarySources:     ['GET',   '/api/v2/library/sources'],
    libraryFile:        ['GET',   '/api/v2/library/files/{id}'],
    fileContent:        ['GET',   '/api/v2/library/files/{id}/content'],
    fileRecords:        ['GET',   '/api/v2/files/{id}/records'],
    fileTimeline:       ['GET',   '/api/v2/files/{id}/timeline'],
    bindings:           ['GET',   '/api/v2/bindings'],
    rules:              ['GET',   '/api/v2/rules'],
    adminUsers:         ['GET',   '/api/v1/admin/users'],
    adminConfig:        ['GET',   '/api/v1/admin/config'],
    adminStats:         ['GET',   '/api/v1/admin/stats'],
    adminAudit:         ['GET',   '/api/v1/admin/audit'],
    queries:            ['POST',  '/api/v2/queries'],
    query:              ['GET',   '/api/v2/queries/{id}'],
    queryRecords:       ['GET',   '/api/v2/queries/{id}/records'],
    queryContinue:      ['POST',  '/api/v2/queries/{id}/continue'],
    queryExport:        ['GET',   '/api/v2/queries/{id}/export'],
    uploads:            ['POST',  '/api/v2/uploads'],
    upload:             ['GET',   '/api/v2/uploads/{id}'],
    uploadCommit:       ['POST',  '/api/v2/uploads/{id}/commit'],
    events:             ['GET',   '/api/v2/events'],
    pollEvents:         ['GET',   '/api/v2/events/poll']
  };

  var SCENARIOS = [
    ['ready', '正常'], ['loading', '加载中'], ['empty', '空库'],
    ['no-result', '无匹配'], ['error', '请求失败'], ['forbidden', '403 权限不足'],
    ['notfound', '404 资源不存在'], ['conflict', '409 版本冲突'],
    ['precondition', '前置条件未满足'],
    ['saving', '保存中'], ['saved', '已保存'], ['rebuilding', '重建进度'],
    ['offline', '服务离线']
  ];

  var state = {
    mode: 'live',
    scenario: 'loading',
    role: '读取中',
    freshness: '—',
    saved: '—',
    progress: 0,
    revision: 0,
    generatedAt: '',
    calls: [],
    me: null,
    capabilities: {},
    admin: false,
    qaMode: /(?:\?|&)qa=1(?:&|$)/.test(location.search)
  };

  // Remove every built-in fixture synchronously. The first paint happens in
  // L847 before boot() resolves, so leaving R6_SEED in place would briefly show
  // four fabricated sources / three roots / five events and let a user click a
  // source id that does not exist on the server (=> 404 on write).
  files.length = 0;
  R6.sources.length = 0;
  R6.roots.length = 0;
  R6.events.length = 0;
  R6.selected = null;
  R6.search = '';
  R6.sourceFilter = 'all';

  var prefs = { density: 'compact', contrast: 'normal', font_size: 13, wrap: false, time_precision: 'ms', sort: 'time' };
  var probeResult = null;
  var http = null;
  var eventsClient = null;
  var booted = false;
  var refreshTimer = null;

  /* ---- 传输层：优先复用 LBHttp（CSRF + epoch + 统一错误） ----------------- */
  function makeClient() {
    if (window.LBHttp && LBHttp.createClient) return LBHttp.createClient({});
    // Fallback: identical wire contract, used when /assets/r6.js is unavailable.
    var csrf = '';
    return {
      setCsrf: function (t) { csrf = t || ''; },
      reset: function () {}, close: function () {}, epoch: function () { return 0; },
      get: function (p) { return raw('GET', p, null); },
      post: function (p, b) { return raw('POST', p, b); },
      put: function (p, b) { return raw('PUT', p, b); },
      patch: function (p, b) { return raw('PATCH', p, b); },
      del: function (p, b) { return raw('DELETE', p, b); }
    };
    function raw(method, path, body) {
      var headers = {};
      if (body !== undefined && body !== null) headers['Content-Type'] = 'application/json';
      if (method !== 'GET' && method !== 'HEAD') headers['X-LogBench-CSRF'] = csrf;
      return fetch(path, {
        method: method, credentials: 'same-origin', headers: headers,
        body: body === undefined || body === null ? undefined : JSON.stringify(body)
      }).then(function (r) {
        return r.text().then(function (t) {
          var v = null;
          try { v = t ? JSON.parse(t) : {}; } catch (e) { v = {}; }
          if (!r.ok) {
            var d = (v && v.error) || {};
            var err = new Error(d.message || ('请求失败：' + r.status));
            err.code = d.code || ('http_' + r.status);
            err.details = d.details || {};
            err.status = r.status;
            throw err;
          }
          return v || {};
        });
      });
    }
  }

  function path(key, id, params) {
    var def = ENDPOINTS[key] || ['GET', key];
    var url = def[1].replace('/{id}', id ? '/' + encodeURIComponent(id) : '').replace('{id}', id ? encodeURIComponent(id) : '');
    if (params && window.LBHttp && LBHttp.query) url += LBHttp.query(params);
    return url;
  }

  /** Uniform result: {ok, status, data, error}. Never throws for HTTP status. */
  async function request(key, opts) {
    opts = opts || {};
    var def = ENDPOINTS[key] || ['GET', key];
    var method = opts.method || def[0];
    var url = path(key, opts.id, opts.params);
    state.calls.unshift({ at: now(), method: method, path: url });
    if (state.calls.length > 16) state.calls.length = 16;
    try {
      var fn = method === 'GET' ? 'get' : method === 'POST' ? 'post' : method === 'PUT' ? 'put' : method === 'PATCH' ? 'patch' : 'del';
      var data = await http[fn](url, method === 'GET' ? undefined : (opts.body || {}));
      return { ok: true, status: 200, data: data || {}, key: key };
    } catch (e) {
      var status = Number(e && e.status) || 0;
      var code = (e && e.code) || 'request_failed';
      var message = (e && e.message) || '请求失败';
      // Only the primary request owns the visible state. Eight parallel boot
      // requests must not let a secondary 404 overwrite the real outcome of
      // the action the user just took.
      if (opts.primary !== false) {
        state.lastError = message;
        state.lastCode = code;
        state.scenario = classifyFailure({ status: status, code: code });
      } else {
        state.secondaryErrors = (state.secondaryErrors || []).concat([
          { path: url, code: code, message: message }
        ]).slice(-8);
      }
      return { ok: false, status: status, code: code, error: message, data: {}, key: key };
    }
  }

  /** The backend uses 409 for both revision conflicts and state-machine
   *  preconditions (source_disabled / probe_required / target_overlap).
   *  They need different recovery guidance, so never collapse them. */
  function classifyFailure(r) {
    if (!r) return 'error';
    if (r.status === 403) return 'forbidden';
    if (r.status === 404) return 'notfound';
    if (r.status === 0) return 'offline';
    if (r.status === 409) {
      return ['revision_conflict', 'probe_stale', 'probe_expired'].indexOf(r.code) >= 0
        ? 'conflict' : 'precondition';
    }
    return 'error';
  }

  function actionId() {
    return 'ui-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  function now() {
    return new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }

  function humanBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    var u = ['KiB', 'MiB', 'GiB', 'TiB'], i = -1;
    do { n /= 1024; i += 1; } while (n >= 1024 && i < 3);
    return (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)) + ' ' + u[i];
  }

  function shortTime(v) {
    if (!v) return '—';
    var s = String(v);
    var m = s.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : s.slice(-8);
  }

  /* ---- 真实响应 → 原型视图模型 ------------------------------------------- */
  var STATUS_TONE = { queued: '', reading: 'blue', framing: 'blue', uploading: 'blue', uploaded: 'blue', indexing: 'blue', indexed: 'good', paused: '', failed: 'bad', cancelled: '' };

  function projectSources(intake, sources) {
    var byId = {};
    (sources.items || []).forEach(function (s) { byId[s.id] = s; });
    return (intake.items || []).map(function (it) {
      var full = byId[it.id] || {};
      var caps = it.capabilities || {};
      var policy = full.policy || {};
      var maps = (full.mappings || []).map(function (m) {
        return {
          remote: m.remoteDir || m.remote || '',
          root: m.rootId || m.root || 'main',
          target: m.targetSubdir || m.target || '',
          rule: m.ruleRevision || m.rule || '目录继承',
          include: m.include || '*.log, *.log.*',
          exclude: m.exclude || '.logbench-*',
          depth: m.recursive == null ? 8 : m.recursive
        };
      });
      if (!maps.length) maps = [{ remote: it.endpoint || '/', root: 'main', target: it.id || 'source', rule: '目录继承', include: '*.log', exclude: '', depth: 8 }];
      var status = it.paused ? 'paused' : !it.enabled ? 'idle'
        : it.lagBytes > 0 ? 'recovering'
        : (caps.rangeRead === 'supported' || caps.live !== false) ? 'following' : 'limited';
      return {
        id: it.id,
        name: it.label || full.label || it.id,
        protocol: (it.adapter || full.adapter || 'sftp').toLowerCase(),
        endpoint: it.endpoint || full.endpoint || '',
        port: Number(policy.port) || ((it.adapter === 'http') ? 443 : (it.adapter === 'smb') ? 445 : 22),
        credential: full.credentialRef || '服务端凭据引用',
        mode: policy.mode || (caps.rangeRead === 'supported' ? 'live' : 'schedule'),
        status: status,
        poll: Number(policy.poll) || 2,
        scan: Number(policy.scan) || 60,
        first: policy.first || 'all',
        verification: policy.verification || (caps.rangeRead === 'supported' ? 'append' : 'snapshot'),
        readers: full.acl === 'members' ? '管理员与研发组' : '仅管理员',
        count: Number(it.files) || 0,
        raw: humanBytes(it.archivedBytes),
        indexed: humanBytes(it.contiguousBytes),
        lag: it.lagBytes > 0 ? '落后 ' + humanBytes(it.lagBytes) : (it.paused ? '检查点与归档保留' : '已追上'),
        coverage: it.archivedBytes > 0 ? Math.min(100, Math.round(100 * (it.contiguousBytes || 0) / it.archivedBytes)) : 0,
        maps: maps,
        revision: Number(it.revision != null ? it.revision : full.revision) || 0,
        lastSeenAt: it.lastSeenAt,
        lastError: it.lastError,
        gaps: it.gaps || [],
        capabilities: caps
      };
    });
  }

  function projectRoots(payload) {
    return (payload.items || []).map(function (r) {
      return {
        id: r.id || r.root_id,
        name: r.label || r.id,
        path: r.path || '',
        cache: r.path ? r.path.replace(/\/$/, '') + '/.index' : '—',
        state: r.state === 'online' || r.enabled ? 'online' : 'offline',
        free: r.free_bytes == null ? '待预检' : humanBytes(r.free_bytes) + ' 可用',
        type: r.kind || (r.network_fs ? '网络挂载' : '本地磁盘'),
        defaultRule: r.defaultRule || '目录继承',
        revision: Number(r.revision) || 0,
        scope: r.scope,
        enabled: r.enabled,
        reason: r.reason
      };
    });
  }

  function projectJobs(payload) {
    return (payload.items || []).slice(0, 50).map(function (j) {
      return {
        id: j.id,
        at: shortTime(j.updated_at || j.created_at),
        source: (j.payload && (j.payload.source_id || j.payload.label)) || j.kind || '任务',
        kind: j.state || 'queued',
        tone: STATUS_TONE[j.state] || '',
        text: (j.progress != null ? '进度 ' + Math.round(Number(j.progress) * 100) + '% · ' : '') +
          (j.error || j.resume_reason || (j.kind + ' · ' + j.state)),
        state: j.state,
        revision: j.revision
      };
    });
  }

  // server/runtime/api.py Budget dataclass fields. base = administrator's
  // configured values; effective = pressure-shrunk values. The form shows base.
  function projectBudget(payload) {
    var b = (payload && (payload.base || payload.effective || payload.budget)) || {};
    var num = function (v, fallback) { return v == null ? fallback : Number(v); };
    return {
      collect: num(b.transfer_connections, 4),
      perHost: num(b.per_host_connections, 2),
      parse: num(b.parse_slots, 2),
      chunk: num(b.writer_slots, 4),
      poll: 2,
      memory: Math.round(num(b.mem_soft_bytes, 16 * 1073741824) / 1073741824),
      backfill: num(b.history_slots, 2),
      query_slots: num(b.query_slots, 2),
      total_cpu: num(b.total_cpu, 8),
      hot_cache_bytes: Math.round(num(b.hot_cache_bytes, 4 * 1073741824) / 1073741824),
      upload_slots: num(b.upload_slots, 4),
      per_principal_uploads: num(b.per_principal_uploads, 2),
      per_principal_queries: num(b.per_principal_queries, 1),
      interactive_reserve: num(b.interactive_reserve, 1),
      writer_slots: num(b.writer_slots, 4),
      revision: Number(payload && payload.revision) || 0
    };
  }

  function hydrate(d) {
    if (!d) return;
    if (d.sources) R6.sources = d.sources;
    if (d.roots) R6.roots = d.roots;
    if (d.events) R6.events = d.events;
    if (d.tuning) R6.tuning = Object.assign({}, R6.tuning, d.tuning);
    if (d.revision != null) state.revision = Number(d.revision) || state.revision;
  }

  /* ---- 启动：身份 → 并行首屏数据 → 渲染 → SSE ------------------------------ */
  async function boot() {
    http = makeClient();
    var me = await request('me');
    if (!me.ok) {
      state.scenario = classifyFailure(me);
      state.freshness = '—';
      state.saved = '未连接';
      render(false); paintStatus();
      return;
    }
    var m = me.data || {};
    state.me = m.user || null;
    state.csrfToken = m.csrfToken || '';
    state.capabilities = m.capabilities || {};
    state.admin = !!(state.capabilities && state.capabilities.admin);
    state.role = (state.me && (state.me.role === 'admin' ? '管理员' : state.me.name)) || '访客';
    if (http.setCsrf) http.setCsrf(m.csrfToken || '');

    // Preferences are v1, and carry their own revision for optimistic writes.
    var pref = await request('preferences');
    if (pref.ok && pref.data && pref.data.value) Object.assign(prefs, pref.data.value);
    state.prefRevision = (pref.data && pref.data.revision) || 0;
    if (state.admin) {
      var cred = await request('credentials', { primary: false });
      if (cred.ok) state.credentials = cred.data.items || [];
    }

    var secondary = { primary: false };
    var extra = state.admin ? await Promise.all([
      request('rules', secondary),
      request('adminUsers', secondary), request('adminConfig', secondary),
      request('adminStats', secondary),
      request('adminAudit', { primary: false, params: { limit: 12 } })
    ]) : [];
    var results = await Promise.all([
      request('intake'), request('sources', secondary), request('roots', secondary),
      request('jobs', secondary), request('budget', secondary), request('library', secondary),
      request('cache', secondary)
    ]);
    var intake = results[0], sources = results[1], roots = results[2],
        jobs = results[3], budget = results[4], library = results[5],
        cache = results[6];

    var okCount = results.filter(function (r) { return r.ok; }).length;
    if (!okCount) {
      state.scenario = 'offline';
      render(false); paintStatus();
      return;
    }
    // A viewer legitimately gets 403 on admin-only endpoints: keep the page
    // readable in read-only mode instead of replacing it with an error wall.
    var denied = results.some(function (r) { return r.status === 403; });
    if (denied && !state.admin) state.scenario = 'forbidden';

    var model = {
      sources: intake.ok ? projectSources(intake.data, sources.ok ? sources.data : { items: [] }) : R6.sources,
      roots: roots.ok ? projectRoots(roots.data) : R6.roots,
      events: jobs.ok ? projectJobs(jobs.data) : R6.events,
      tuning: budget.ok ? projectBudget(budget.data) : R6.tuning,
      revision: budget.ok ? Number(budget.data.revision) || 0 : state.revision
    };
    hydrate(model);
    state.library = library.ok ? library.data : { items: [] };
    projectLibraryToFiles(state.library.items);
    state.cache = cache.ok ? cache.data : { items: [] };
    if (extra.length) {
      state.adminData = {
        rules: extra[0].ok ? extra[0].data : { items: [] },
        users: extra[1].ok ? extra[1].data : { items: [] },
        config: extra[2].ok ? extra[2].data : {},
        stats: extra[3].ok ? extra[3].data : {},
        audit: extra[4].ok ? extra[4].data : { items: [] }
      };
      projectServerRules(state.adminData.rules.items);
    }
    // /api/v2/runtime closes the connection in this deployment (backend-side
    // status()); /api/v2/runtime/budget already carries base/effective/notes.
    state.runtime = budget.ok ? budget.data : {};
    state.generatedAt = (intake.data && intake.data.generatedAt) || now();
    state.freshness = shortTime(state.generatedAt) || now();
    state.saved = '已同步';
    if (state.scenario === 'loading' || state.scenario === 'offline') {
      state.scenario = R6.sources.length ? 'ready' : 'empty';
    }
    booted = true;
    render(false);
    paintStatus();
    startEvents();
  }

  /* ---- SSE：复用 LBEventClient；不可用时轮询 /api/v2/events/poll ----------- */
  function onLiveEvent(ev) {
    if (!ev || !ev.kind) return;
    var map = {
      archive_commit: '归档提交', index_commit: '索引提交', record_revision: '记录修订',
      rotation: '文件轮转', gap: '采集缺口', source_error: '采集异常', task_progress: '任务进度'
    };
    r6Event(ev.sourceId || ev.fileId || '采集服务', map[ev.kind] || ev.kind,
      ev.message || ev.note || (ev.kind + (ev.fileId ? ' · ' + ev.fileId : '')),
      ev.kind === 'source_error' || ev.kind === 'gap' ? 'warn' : 'good');
    if (S.view === 'sync' && R6.tab === 'events') render(true);
    scheduleRefresh();
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () { refresh(true); }, 1200);
  }

  function startEvents() {
    if (window.LBEventClient && LBEventClient.createClient) {
      try {
        // LBEventClient injects both transports. Without them start() silently
        // degrades to a polling mode that never issues a request.
        var eventOptions = {
          url: '/api/v2/events',
          pollUrl: '/api/v2/events/poll',
          fetchImpl: function (url) {
            return fetch(url, { credentials: 'same-origin' }).then(function (r) {
              if (!r.ok) throw new Error('事件轮询失败：' + r.status);
              return r.json();
            });
          },
          onEvent: onLiveEvent,
          onState: function (s) {
            var mode = s && s.mode;
            state.eventMode = mode || 'idle';
            if ((mode === 'offline' || mode === 'closed') && state.scenario === 'ready') {
              state.scenario = 'offline'; paintStatus();
            } else if ((mode === 'live' || mode === 'polling') && state.scenario === 'offline') {
              state.scenario = 'ready'; paintStatus();
            }
          },
          onResync: function () { refresh(true); }
        };
        if (typeof EventSource === 'function') {
          eventOptions.streamFactory = function (url) { return new EventSource(url); };
        }
        eventsClient = LBEventClient.createClient(eventOptions);
        LBEventClient.attachPageHooks(eventsClient, window);
        // createClient() only builds the client; without start() no EventSource
        // is opened and the real-time channel is silently dead.
        eventsClient.start();
        return;
      } catch (e) { /* fall through to polling */ }
    }
    var last = 0;
    setInterval(async function () {
      var r = await request('pollEvents', { params: { last: last || '' }, primary: false });
      if (!r.ok) return;
      (r.data.events || []).forEach(function (ev) { last = Math.max(last, Number(ev.id) || 0); onLiveEvent(ev); });
    }, 4000);
  }

  /* ---- 状态文本与状态件（与预览版同接口） -------------------------------- */
  function statusText() {
    if (state.scenario === 'loading') return '加载中 · 正在读取 /api/v2/intake';
    if (state.scenario === 'empty') return '空库 · 尚无远程日志源';
    if (state.scenario === 'no-result') return '无匹配 · 筛选条件已保留，采集任务不受影响';
    if (state.scenario === 'notfound') return '资源不存在（404） · 列表已刷新';
    if (state.scenario === 'error') return '请求失败 · ' + (state.lastError || '请重试');
    if (state.scenario === 'forbidden') return state.admin
      ? '权限不足（403） · 服务端拒绝本次请求'
      : '只读身份 · 采集管理操作需要管理员权限';
    if (state.scenario === 'conflict') return '版本冲突（409） · 输入已保留为草稿';
    if (state.scenario === 'precondition') return '前置条件未满足 · ' + (state.lastError || '请先完成预检与启用');
    if (state.scenario === 'saving') return '保存中 · 等待服务端回执';
    if (state.scenario === 'saved') return '已保存 · 服务端回执 revision ' + state.revision;
    if (state.scenario === 'rebuilding') return '重建中 ' + state.progress + '% · 旧索引继续可读';
    if (state.scenario === 'offline') return '服务未连接 · 写操作已停用';
    return (state.admin ? '服务已连接' : '服务已连接 · 只读') + ' · ' +
      R6.sources.length + ' 个日志源 · ' + state.role + ' · ' + state.saved;
  }

  function dataFreshness() {
    if (state.scenario === 'rebuilding') return '重建进度 ' + state.progress + '% · 旧索引可读';
    if (state.scenario === 'saving') return '提交中 · 请勿关闭页面';
    if (state.scenario === 'saved') return '已保存 · revision ' + state.revision;
    return '上次成功响应 ' + state.freshness + ' · revision ' + state.revision;
  }

  function scenarioSelect() {
    if (!state.qaMode) return '';
    return '<label for="lb-scenario" class="r6-sub" style="margin:0 6px">状态</label>' +
      '<select id="lb-scenario" class="select" aria-label="状态检视（QA）" style="height:18px;min-height:18px;font-size:11px;padding:0 4px;margin-right:8px">' +
      SCENARIOS.map(function (s) {
        return '<option value="' + s[0] + '"' + (state.scenario === s[0] ? ' selected' : '') + '>' + s[1] + '</option>';
      }).join('') + '</select>';
  }

  function stateBanner() {
    var s = state.scenario;
    if (s === 'ready') return '';
    if (s === 'saved') return '<div class="r6-note">' + I('check', 'sm') + '已保存 · 服务端已回执（revision ' + state.revision + '）。草稿已清除。</div>';
    if (s === 'loading') return '<div class="r6-note">' + I('clock', 'sm') + '加载中 · 正在读取采集状态，已保留上次成功快照，界面不跳动。</div><div class="r6-progress" aria-label="加载中"><span style="width:45%"></span></div>';
    if (s === 'saving') return '<div class="r6-note">' + I('clock', 'sm') + '保存中 · 正在提交到服务端；关闭页面不会丢失已填内容。</div><div class="r6-progress" aria-label="保存中"><span style="width:70%"></span></div>';
    if (s === 'rebuilding') return '<div class="r6-note">' + I('clock', 'sm') + '重建规则结果 · 旧索引继续可读，原文不重新下载。</div><div class="r6-progress" aria-label="重建进度 ' + state.progress + '%"><span style="width:' + state.progress + '%"></span></div>';
    if (s === 'empty') return '<div class="r6-empty">' + I('database', 'lg') + '<strong>还没有远程日志源</strong><p>添加第一个远程目录后开始采集。已登记的存储位置可以直接使用。</p>' + (state.admin ? r6Btn('添加日志源', 'new-source', 'plus', 'primary') : '') + '</div>';
    if (s === 'no-result') return '<div class="r6-empty">' + I('search', 'lg') + '<strong>没有匹配「' + E(R6.search || '') + '」的日志源</strong><p>这不是空库。调整名称、目录或状态条件；已有采集任务不受筛选影响。</p>' + r6Btn('清除筛选', 'clear-filters', 'clear', 'sm') + '</div>';
    if (s === 'notfound') return '<div class="r6-empty">' + I('warning', 'lg') + '<strong>日志源不存在或已被删除</strong><p>详情请求返回 404。列表已刷新为服务端最新状态，原文归档与索引不受影响。</p>' + r6Btn('返回日志源列表', 'clear-filters', 'left', 'sm') + '</div>';
    if (s === 'error') return '<div class="r6-errorbox" role="alert"><strong>无法读取采集状态</strong><p>' + E(state.lastError || '请求失败') + '。已保留上次成功快照，期间没有执行写操作。</p>' + r6Btn('重试', 'retry', 'refresh', 'sm') + '</div>';
    if (s === 'forbidden') return '<div class="r6-errorbox" role="alert"><strong>' + (state.admin ? '权限不足（403）' : '只读身份') + '</strong><p>' + (state.admin
      ? '服务端拒绝了本次请求。检查当前身份与服务配置后重试。'
      : '当前身份 <b>' + E(state.role) + '</b> 只有读取权限，无法暂停采集、保存配置或重建索引。页面保持只读，所有输入都已保留；联系管理员后可重试。') + '</p>' + r6Btn('重试', 'retry', 'refresh', 'sm') + '</div>';
    if (s === 'conflict') return '<div class="r6-note bad" role="alert"><strong>版本冲突（409）</strong><p>该对象已被其他人更新。你填写的内容已保留为草稿，未被覆盖，也不会自动合并。</p><div class="r6-inline-actions">' + r6Btn('查看数据链路', 'datalink', 'info', 'sm') + r6Btn('载入新版本后重试', 'retry', 'refresh', 'sm') + '</div></div>';
    if (s === 'precondition') {
      var preconditionHint = (state.lastCode === 'source_disabled' || state.lastCode === 'probe_required')
        ? '远程源需要先「运行预检 → 启用」，之后才能暂停或恢复；未通过预检不会悄悄降级为可用状态。'
        : '按提示补齐前置条件后重试；已填内容与已有索引均不会被删除。';
      return '<div class="r6-errorbox" role="alert"><strong>操作未执行 · 前置条件未满足</strong>' +
        '<p>服务端返回（<code class="mono">' + E(state.lastCode || '') + '</code>）：' + E(state.lastError || '') +
        ' ' + preconditionHint + '</p>' +
        '<div class="r6-inline-actions">' + r6Btn('添加/编辑日志源', 'new-source', 'plus', 'sm') + r6Btn('重试', 'retry', 'refresh', 'sm') + '</div></div>';
    }
    if (s === 'offline') return '<div class="r6-note warn">' + I('warning', 'sm') + '服务未连接 · 显示 ' + E(state.freshness) + ' 的上次成功快照。写操作已停用，恢复连接后可重试。</div>';
    return '';
  }

  var painting = false;
  function paintStatus() {
    if (painting) return;
    painting = true;
    try {
      var row = document.querySelector('.app-status .row');
      if (row) row.innerHTML = I('lock', 'sm') + statusText();
      var extra = document.querySelector('.app-status .status-right .extra');
      if (extra) extra.textContent = dataFreshness();
      var tag = document.querySelector('.lb-header .prototype-tag');
      if (tag) tag.textContent = state.scenario === 'offline' ? '服务未连接' : (state.admin ? '服务已连接' : '只读');
      var sel = document.getElementById('lb-scenario');
      if (sel && sel.value !== state.scenario) sel.value = state.scenario;
      // Read-only / permission state must be visible on the write controls too.
      var canWrite = state.admin && ['offline', 'error', 'forbidden'].indexOf(state.scenario) < 0;
      var primary = document.querySelector('.r6-head [data-r6="new-source"], .r6-head [data-r6="new-root"]');
      if (primary) {
        primary.disabled = !canWrite;
        primary.setAttribute('aria-label', canWrite ? '添加' : '需要管理员权限且服务已连接，当前不可添加');
      }

      var html = stateBanner();
      var host = document.getElementById('lb-state');
      var pane = document.querySelector('.r6-content > .r6-pane');
      var full = ['empty', 'no-result', 'notfound', 'loading'].indexOf(state.scenario) >= 0;
      if (html && pane) {
        if (!host) {
          host = document.createElement('div');
          host.id = 'lb-state';
          pane.parentNode.insertBefore(host, pane);
        }
        host.style.cssText = full
          ? 'flex:1;min-height:0;overflow:auto;padding:12px 18px;background:#fff'
          : 'flex-shrink:0;padding:8px 18px;border-bottom:1px solid var(--ud-line);background:#fff';
        if (host.innerHTML !== html) host.innerHTML = html;
        // .r6-pane{display:flex} outranks the UA [hidden] rule, so `hidden`
        // alone does nothing and the full-screen state banner gets squeezed
        // into a half-height row. Use an explicit inline display instead.
        pane.hidden = full;
        pane.style.display = full ? 'none' : '';
      } else if (host) {
        host.remove();
        if (pane) { pane.hidden = false; pane.style.display = ''; }
      } else if (pane) {
        pane.hidden = false; pane.style.display = '';
      }
    } finally { painting = false; }
  }

  function setScenario(v) { state.scenario = v; if (v === 'rebuilding') state.progress = 0; paintStatus(); }

  /* ---- 动作：全部走真实端点 ---------------------------------------------- */
  async function refresh(silent) {
    if (!booted) return boot();
    if (!silent) { state.scenario = 'loading'; paintStatus(); }
    var secondary = { primary: false };
    var results = await Promise.all([
      request('intake'), request('sources', secondary), request('roots', secondary),
      request('jobs', secondary), request('budget', secondary), request('library', secondary),
      request('cache', secondary)
    ]);
    var intake = results[0], sources = results[1];
    if (!intake.ok) {
      state.scenario = classifyFailure(intake);
      state.lastError = intake.error;
      state.lastCode = intake.code;
      paintStatus();
      return;
    }
    hydrate({
      sources: projectSources(intake.data, sources.ok ? sources.data : { items: [] }),
      roots: results[2].ok ? projectRoots(results[2].data) : R6.roots,
      events: results[3].ok ? projectJobs(results[3].data) : R6.events,
      tuning: results[4].ok ? projectBudget(results[4].data) : R6.tuning,
      revision: results[4].ok ? Number(results[4].data.revision) || 0 : state.revision
    });
    if (results[5].ok) state.library = results[5].data;
    if (results[6].ok) state.cache = results[6].data;
    if (results[4].ok) state.runtime = results[4].data;
    state.generatedAt = intake.data.generatedAt || now();
    state.freshness = shortTime(state.generatedAt) || now();
    if (state.scenario !== 'forbidden') state.scenario = R6.sources.length ? 'ready' : 'empty';
    render(false); paintStatus();
    if (!silent) notify('已从 /api/v2/intake 更新 ' + R6.sources.length + ' 个日志源');
  }

  function connect() { refresh(); }

  function retry() {
    if (state.scenario === 'conflict') {
      state.scenario = 'ready';
      paintStatus();
      notify('已重新载入服务端版本，草稿仍保留，可重新保存');
      return;
    }
    refresh();
  }

  function requireWrite() {
    if (!state.admin) {
      state.scenario = 'forbidden'; paintStatus();
      notify('当前身份只有读取权限，无法执行此操作。', true);
      return false;
    }
    return true;
  }

  async function pause(id) {
    if (!requireWrite()) return;
    var x = r6Source(id);
    if (!x) return;
    var action = x.status === 'paused' ? 'resume' : 'pause';
    state.scenario = 'saving'; paintStatus();
    var r = await request('actions', {
      id: id,
      body: { action: action, actionId: actionId(), expectedRevision: x.revision }
    });
    if (!r.ok) { paintStatus(); return; }
    state.revision = Number(x.revision) + 1;
    state.scenario = 'ready';
    state.saved = '已保存';
    r6Event(x.name, action === 'pause' ? '暂停采集' : '继续采集',
      action === 'pause' ? '暂停新的传输；不删除原文或解析缓存。' : '从已提交检查点继续，历史归档保留。',
      action === 'pause' ? '' : 'blue');
    await refresh(true);
  }

  async function probe() {
    if (!requireWrite()) return;
    r6CaptureDraft();
    var d = r6Draft || {};
    var host = document.getElementById('r6-probe-result');
    if (host) host.innerHTML = '<div class="r6-note">正在预检 ' + E(d.endpoint || '') + ' …</div>';
    // A probe can only run against a persisted source: save the draft first.
    var id = d.id;
    if (!id) {
      var created = await request('createSource', { body: draftToApi(d) });
      if (!created.ok) {
        state.scenario = classifyFailure(created);
        state.lastError = created.error; state.lastCode = created.code;
        if (host) host.innerHTML = '<div class="r6-errorbox" role="alert">预检前需要先保存草稿：' + E(created.error) + '</div>';
        paintStatus(); return;
      }
      id = created.data.item && created.data.item.id;
      r6Draft.id = id;
      if (created.data.item) r6Draft.revision = created.data.item.revision;
    }
    var r = await request('probe', { id: id, body: {} });
    var p = (r.data && r.data.probe) || {};
    var checks = (p.checks || []).map(function (c) {
      var ok = c.ok !== false && c.status !== 'failed' && c.state !== 'failed';
      return { key: c.key || c.name || 'check', label: c.label || c.name || c.key || '检查项', ok: ok, detail: c.detail || c.message || c.reason || (ok ? '通过' : '未通过') };
    });
    var ok = r.ok && p.ok !== false && checks.every(function (c) { return c.ok; });
    r6Probe = true;
    r6ProbeKind = ok ? 'ready' : 'auth';
    LB.probeResult = { ok: ok, checks: checks, fingerprint: (r.data && r.data.fingerprint) || p.fingerprint || '', revision: (r.data && r.data.revision) || p.configRevision || 0 };
    var save = document.querySelector('#dialog [data-r6="wizard-save"]');
    if (save) save.disabled = !ok;
    if (host) {
      host.innerHTML = (checks.length ? checks.map(function (c) {
        return '<div class="r6-probe-row ' + (c.ok ? '' : 'bad') + '">' + I(c.ok ? 'check' : 'error') + '<span>' + E(c.label) + ' · ' + E(c.detail) + '</span></div>';
      }).join('') : '<div class="r6-mininote">服务端未返回检查项；预检结果：' + (ok ? '通过' : '未通过') + '</div>') +
        (ok ? '' : '<div class="r6-note bad" style="margin-top:12px">预检未通过，保存已停用。修正凭据、主机密钥或文件清单后重新预检；不会自动降级为匿名连接。</div>');
    }
    r6Event(d.name || '新日志源', '预检完成',
      ok ? '连接、目录发现、范围读取与目标权限全部通过。' : '预检未通过，配置未提交。', ok ? 'good' : 'warn');
  }

  // Credentials live server-side as cred_* refs. A label that is not a
  // registered, ready ref must be sent as empty (=> None), never as prose.
  function resolveCredentialRef(label) {
    var list = state.credentials || [];
    for (var i = 0; i < list.length; i += 1) {
      var c = list[i];
      if (c.ref === label || c.label === label || c.id === label) return c.ref || c.id || '';
    }
    return '';
  }

  function splitPatterns(v) {
    return String(v || '').split(',').map(function (s) { return s.trim(); })
      .filter(Boolean).slice(0, 32);
  }

  // Wire contract: server/collectors/sources.py POLICY_KEYS, MODES,
  // _mapping_payload(). The prototype's live/schedule/snapshot vocabulary is
  // projected onto manual/scheduled/follow; intervals go into policy.
  function draftToApi(d) {
    var adapter = (d.protocol || 'sftp').toLowerCase();
    if (['sftp', 'smb', 'http', 'scp'].indexOf(adapter) < 0) adapter = 'sftp';
    var mode = d.mode === 'live' ? 'follow' : d.mode === 'snapshot' ? 'manual' : 'scheduled';
    var policy = {
      activeInterval: Math.max(1, Math.min(3600, Number(d.poll) || 2)),
      discoveryInterval: Math.max(1, Math.min(86400, Number(d.scan) || 60)),
      maxDepth: 8,
      allowLoopback: /^(127\.|localhost|\[?::1\])/.test(String(d.endpoint || '')),
      appendContract: d.verification !== 'snapshot'
    };
    if (adapter === 'http') {
      policy.directoryMode = mode === 'manual' ? 'manifest' : 'listing';
      policy.connectTimeout = 10;
      policy.readTimeout = 30;
    }
    return {
      adapter: adapter,
      label: d.name || '',
      endpoint: d.endpoint || '',
      credentialRef: resolveCredentialRef(d.credential),
      acl: d.readers === '管理员与研发组' ? 'members' : 'admin',
      policy: policy,
      mappings: (d.maps || []).map(function (m) {
        return {
          remoteDir: m.remote || '/',
          rootId: m.root || 'legacy',
          targetSubdir: m.target || '',
          include: splitPatterns(m.include || '*.log'),
          exclude: splitPatterns(m.exclude || ''),
          recursive: true,
          mode: mode,
          ruleRevision: m.rule || ''
        };
      })
    };
  }

  async function saveSource() {
    if (!requireWrite()) return;
    r6CaptureDraft();
    var error = r6Validate(r6Draft);
    if (error) { r6ShowFormError(error); return; }
    if (!LB.probeResult || !LB.probeResult.ok) { r6ShowFormError('请先运行预检并全部通过。'); return; }
    state.scenario = 'saving'; paintStatus();
    var d = clone(r6Draft);
    var r = d.id
      ? await request('updateSource', { id: d.id, body: Object.assign({}, draftToApi(d), { expectedRevision: LB.probeResult.revision || d.revision }) })
      : await request('createSource', { body: draftToApi(d) });
    if (!r.ok) {
      state.scenario = classifyFailure(r);
      state.lastError = r.error; state.lastCode = r.code;
      r6ShowFormError('保存失败：' + r.error + '。草稿已保留，可重试。');
      paintStatus(); return;
    }
    state.revision = Number((r.data.item && r.data.item.revision) || LB.probeResult.revision) || state.revision + 1;
    state.scenario = 'saved';
    state.saved = '已保存';
    r6Event(d.name, '配置保存', '目录映射已提交到服务端；采集任务按计划启动。', 'good');
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    LB.probeResult = null;
    await refresh(true);
    state.scenario = 'saved';
    paintStatus();
  }

  async function saveRoot() {
    if (!requireWrite()) return;
    var name = document.getElementById('r6-root-name').value.trim();
    var p = document.getElementById('r6-root-path').value.trim();
    var cache = document.getElementById('r6-root-cache').value.trim();
    var err = document.getElementById('r6-root-error');
    if (!name || !p.startsWith('/') || !cache.startsWith('/') || p.includes('..') || cache.includes('..')) {
      err.textContent = '请填写名称与不包含 .. 的绝对路径。'; return;
    }
    if (R6.roots.some(function (r) {
      return r.id !== R6.editRoot && (p === r.path || p.startsWith(r.path + '/') || r.path.startsWith(p + '/'));
    })) { err.textContent = '存储根目录不可重复或相互包含。'; return; }
    state.scenario = 'saving'; paintStatus();
    var r = R6.editRoot
      ? await request('updateRoot', { id: R6.editRoot, body: { label: name, expectedRevision: (r6Root(R6.editRoot) || {}).revision || 1 } })
      : await request('createRoot', { body: { label: name, path: p, kind: 'local' } });
    if (!r.ok) {
      state.scenario = classifyFailure(r);
      state.lastError = r.error; state.lastCode = r.code; paintStatus(); return;
    }
    var id = R6.editRoot || (r.data.item && (r.data.item.id || r.data.item.root_id));
    if (id) await request('probeRoot', { id: id, body: {} });
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    R6.tab = 'storage'; S.view = 'sync';
    await refresh(true);
    notify('存储位置已保存并完成权限预检');
  }

  async function saveTuning() {
    if (!requireWrite()) return;
    var ks = ['collect', 'perHost', 'parse', 'backfill', 'memory'];
    var v = {};
    ks.forEach(function (k) { v[k] = Number(document.getElementById('r6-tune-' + k).value); });
    if (ks.some(function (k) { return !Number.isInteger(v[k]) || v[k] < 1 || v[k] > 64; }) || v.perHost > v.collect) {
      document.getElementById('r6-tune-error').textContent = '请输入 1–64 的整数，且单主机并发不能超过总并发。';
      return;
    }
    state.scenario = 'saving'; paintStatus();
    // server/runtime/api.py:345 compares expectedRevision to its own revision,
    // which legitimately starts at 0. Never coerce 0 to a truthy fallback.
    var budgetRevision = state.runtime && state.runtime.revision != null
      ? Number(state.runtime.revision)
      : (R6.tuning.revision != null ? Number(R6.tuning.revision) : 0);
    var r = await request('putBudget', {
      body: {
        expectedRevision: Number.isFinite(budgetRevision) ? budgetRevision : 0,
        value: {
          transfer_connections: v.collect,
          per_host_connections: v.perHost,
          parse_slots: v.parse,
          history_slots: v.backfill,
          mem_soft_bytes: Math.round(v.memory * 1073741824)
        }
      }
    });
    if (!r.ok) {
      state.scenario = classifyFailure(r);
      state.saved = '未保存';
      state.lastError = r.error; state.lastCode = r.code;
      var box = document.getElementById('r6-tune-error');
      if (box) box.textContent = '保存失败：' + r.error + '。输入已保留；刷新页面取回最新版本号后重试。';
      paintStatus(); return;
    }
    hydrate({ tuning: Object.assign({}, v, { revision: r.data.revision }) });
    state.runtime = Object.assign({}, state.runtime, r.data || {});
    state.revision = Number(r.data.revision) || state.revision;
    state.scenario = 'saved'; state.saved = '已保存';
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    render(false); paintStatus();
    notify('资源预算已保存到 /api/v2/runtime/budget');
  }

  async function rebuildCache() {
    if (!requireWrite()) return;
    var fileId = ((state.cache.items || []).find(function (x) { return x.fileId === R6.cacheSelected; }) || (state.cache.items || [])[0] || {}).fileId || '';
    if (!fileId) {
      // 'empty' means "no remote sources" and would be the wrong recovery hint
      // here. Use a precondition with an explicit, actionable reason.
      state.lastCode = 'no_cache_entry';
      state.lastError = '当前缓存列表为空，没有可重建的索引。';
      state.scenario = 'precondition'; paintStatus();
      notify(state.lastError, true); return;
    }
    state.scenario = 'rebuilding'; state.progress = 8; paintStatus();
    var r = await request('rebuild', { body: { fileId: fileId, layers: ['rules', 'search'], actionId: actionId() } });
    if (!r.ok) {
      state.scenario = classifyFailure(r);
      state.lastError = r.error; state.lastCode = r.code; paintStatus(); return;
    }
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    var timer = setInterval(async function () {
      var jobs = await request('indexJobs', { params: { limit: 20 }, primary: false });
      var live = ((jobs.data && jobs.data.items) || []).filter(function (j) { return ['queued', 'reading', 'framing', 'indexing'].indexOf(j.state) >= 0; });
      state.progress = live.length ? Math.min(92, state.progress + 7) : 100;
      if (state.scenario === 'rebuilding') paintStatus();
      if (state.progress >= 100) {
        clearInterval(timer);
        state.scenario = 'saved'; state.saved = '已重建';
        r6Event('解析缓存', '重建完成', '新规则结果版本已验证并切换；原文未重新下载。', 'good');
        R6.tab = 'cache'; S.view = 'sync';
        await refresh(true);
        state.scenario = 'saved'; paintStatus();
      }
    }, 900);
  }

  function exportEvents() {
    download('logbench-r6-jobs.json', JSON.stringify({
      format: 'logbench.jobs/1', exported_at: new Date().toISOString(),
      revision: state.revision, jobs: R6.events
    }, null, 2), 'application/json');
  }

  async function savePref(key, value) {
    prefs[key] = value;
    var next = {};
    Object.keys(prefs).forEach(function (k) { next[k] = prefs[k]; });
    state.saved = '保存中'; paintStatus();
    var r = await request('putPreferences', { body: { value: next, expectedRevision: state.prefRevision || 0 } });
    if (!r.ok) {
      state.saved = '未保存'; state.lastError = r.error;
      state.scenario = r.status === 409 ? 'conflict' : 'error';
      paintStatus(); return;
    }
    state.prefRevision = r.data.revision || state.prefRevision;
    state.saved = '已保存'; paintStatus();
  }

  /* ---- 数据链路 / 备份 ---------------------------------------------------- */
  function datalink() {
    var rows = Object.keys(ENDPOINTS).map(function (k) {
      var e = ENDPOINTS[k];
      return '<tr><td class="mono">' + E(k) + '</td><td class="mono">' + E(e[0]) + ' ' + E(e[1]) + '</td></tr>';
    }).join('');
    var calls = state.calls.length ? state.calls.slice(0, 8).map(function (c) {
      return '<div class="r6-meta mono">' + E(c.at) + ' · ' + E(c.method) + ' ' + E(c.path) + '</div>';
    }).join('') : '<p class="r6-mininote">尚无请求记录。</p>';
    dialog('数据链路', '<div class="r6-form">' +
      '<div class="r6-note ' + (state.scenario === 'offline' ? 'warn' : '') + '">' + I('database') +
      (state.scenario === 'offline' ? '服务未连接，显示上次成功快照。' : '已连接 /api/v2 服务，界面数据来自真实回执。') + '</div>' +
      '<dl class="r6-kv"><dt>身份</dt><dd>' + E((state.me && state.me.name) || state.role) + ' · ' + E(state.role) + '</dd>' +
      '<dt>权限</dt><dd>' + (state.admin ? '管理员（可写）' : '只读') + '</dd>' +
      '<dt>数据新鲜度</dt><dd>' + E(state.freshness) + ' · ' + E(state.generatedAt || '—') + '</dd>' +
      '<dt>保存状态</dt><dd>' + E(state.saved) + ' · revision ' + state.revision + '</dd>' +
      '<dt>实时通道</dt><dd class="mono">/api/v2/events</dd>' +
      '<dt>轮询降级</dt><dd class="mono">/api/v2/events/poll</dd></dl>' +
      '<details><summary>端点映射（' + Object.keys(ENDPOINTS).length + ' 条）</summary><div class="r6-table-scroll"><table class="r6-table"><thead><tr><th>动作</th><th>方法与路径</th></tr></thead><tbody>' + rows + '</tbody></table></div></details>' +
      '<details><summary>最近调用</summary>' + calls + '</details>' +
      '</div>', r6Btn('关闭', 'modal-close', null) + r6Btn('重新连接', 'connect', 'refresh', 'primary'), true);
  }

  function backup() {
    dialog('备份与恢复', '<div class="r6-form">' +
      '<div class="r6-note warn">' + I('shield') + '<strong>恢复会替换当前工作区</strong>的阅读视角、书签与显示设置；已归档原文与解析索引不受影响。恢复失败不会留下半份文件库。</div>' +
      '<h3 class="r6-section-title">导出</h3><p class="r6-mininote">导出个人偏好与阅读视角（/api/v1/preferences）；服务端归档与索引请用运维备份工具。</p>' +
      r6Btn('导出工作区设置', 'backup-export', 'download', '') +
      '<h3 class="r6-section-title">恢复</h3><div class="field"><label class="label" for="lb-restore-file">选择备份文件</label><input id="lb-restore-file" class="input" type="file" accept=".json,application/json"></div>' +
      '<div class="r6-errorbox" id="lb-restore-error" role="alert"></div></div>',
      r6Btn('关闭', 'modal-close', null) + r6Btn('恢复并替换当前工作区', 'backup-restore', 'refresh', 'primary'), true);
  }

  function exportBackup() {
    download('logbench-workspace.json', JSON.stringify({
      format: 'LogBench Workspace Backup', version: 1,
      workspace: { schema: 'logbench.workspace', version: 1 },
      preferences: prefs, revision: state.prefRevision,
      exportedAt: new Date().toISOString()
    }, null, 2), 'application/json');
    notify('工作区设置已导出');
  }

  async function restoreBackup() {
    var input = document.getElementById('lb-restore-file');
    var err = document.getElementById('lb-restore-error');
    if (!input || !input.files || !input.files[0]) { err.textContent = '请先选择备份文件。'; return; }
    try {
      var value = JSON.parse(await input.files[0].text());
      if (value.format !== 'LogBench Workspace Backup' || value.version !== 1) throw new Error('格式或版本不受支持');
      var next = Object.assign({}, prefs, value.preferences || {});
      var r = await request('putPreferences', { body: { value: next, expectedRevision: state.prefRevision || 0 } });
      if (!r.ok) throw new Error(r.error || '服务端拒绝写入');
      Object.assign(prefs, next);
      state.prefRevision = r.data.revision || state.prefRevision;
      state.saved = '已恢复';
      var d = document.getElementById('dialog');
      if (d && d.open) d.close();
      render(false); paintStatus();
      notify('工作区设置已恢复，服务端归档数据未改变');
    } catch (e) {
      err.textContent = '恢复失败：' + e.message + '。当前工作区未改变，原文件保留。';
    }
  }

  /* ---- Rev2 补齐：本机批量任务 / 渐进搜索 / 私有上传 ----------------------- */
  // These three were added by Complete Design Rev2 and are absent from the R6
  // Remote Prototype. They use only prototype-native containers and controls.
  // Sidebar entries, not a persistent toolbar row: the project baseline
  // requires floating task panels that do not squeeze the reading area.
  function r6Rev2() {
    return '<div id="lb-rev2" class="section-head">本机任务与服务端加速</div>' +
      '<button class="nav-button" data-r6="rev2-jobs">' + I('layers') + '本机批量任务</button>' +
      '<button class="nav-button" data-r6="rev2-search">' + I('search') + '渐进检索</button>' +
      '<button class="nav-button" data-r6="rev2-upload">' + I('upload') + '上传到私有区</button>';
  }

  function jobsPanel() {
    var items = (state.jobsCache && state.jobsCache.items) || [];
    dialog('本机批量任务', '<div class="r6-form">' +
      '<p class="r6-mininote">本机导入默认只在浏览器内读取，不上传。下面的任务来自 /api/v2/index-jobs（服务端加速需显式选择）。</p>' +
      (items.length
        ? '<table class="r6-table"><thead><tr><th>任务</th><th>状态</th><th>进度</th></tr></thead><tbody>' +
          items.slice(0, 20).map(function (j) {
            return '<tr><td class="mono">' + E(j.id || j.file_id || '—') + '</td><td>' + r6Badge(E(j.state || 'queued'), j.state === 'indexed' ? 'good' : j.state === 'failed' ? 'bad' : 'blue') + '</td><td>' + (j.progress != null ? Math.round(Number(j.progress) * 100) + '%' : '—') + '</td></tr>';
          }).join('') + '</tbody></table>'
        : '<div class="r6-empty">' + I('layers', 'lg') + '<strong>当前没有任务</strong><p>打开本机日志后，任务会在这里显示；正文始终留在本浏览器。</p></div>') +
      '<div class="r6-errorbox" id="rev2-jobs-error" role="alert"></div></div>',
      r6Btn('关闭', 'modal-close', null) + r6Btn('刷新任务', 'rev2-jobs-refresh', 'refresh', ''), true);
  }

  async function refreshJobsPanel() {
    var r = await request('indexJobs', { params: { limit: 20 }, primary: false });
    if (r.ok) { state.jobsCache = r.data; jobsPanel(); }
    else {
      var e = document.getElementById('rev2-jobs-error');
      if (e) e.textContent = '读取任务失败：' + r.error;
    }
  }

  function libraryOptions() {
    var items = (state.library && state.library.items) || [];
    return items.filter(function (x) { return x.available !== false; }).slice(0, 50)
      .map(function (it) {
        return '<option value="' + E(it.fileId) + '">' + E(it.displayName || it.relPath || it.fileId) + '</option>';
      }).join('');
  }

  function searchPanel() {
    // server/query/api.py:151 requires file_id. An empty catalogue must be a
    // readable empty state, not a 400 sent to the API.
    var opts = libraryOptions();
    var fileField = opts
      ? '<div class="field"><label class="label" for="rev2-query-file">检索文件</label>' +
        '<select class="select" id="rev2-query-file">' + opts + '</select></div>'
      : '<div class="r6-empty">' + I('folder', 'lg') + '<strong>当前没有可检索的文件</strong>' +
        '<p>检索绑定到具体文件快照。先完成采集并落盘，或上传文件到私有区后重试。</p></div>';
    dialog('渐进检索', '<div class="r6-form">' +
      '<div class="field"><label class="label" for="rev2-query-text">检索文本</label><input class="input" id="rev2-query-text" placeholder="支持 Aa / 整词 / 正则与多条件"></div>' +
      fileField +
      '<p class="r6-mininote">查询在服务端按固定快照执行，结果按游标分批返回；未完成时可用「继续检索」推进，不阻塞界面。</p>' +
      '<div class="r6-progress" id="rev2-query-progress" aria-label="检索进度"><span style="width:0%"></span></div>' +
      '<div id="rev2-query-result" class="r6-mininote"></div>' +
      '<div class="r6-errorbox" id="rev2-query-error" role="alert"></div></div>',
      r6Btn('关闭', 'modal-close', null) +
      r6Btn('开始检索', 'rev2-query-start', 'search', 'primary', opts ? '' : 'disabled') +
      r6Btn('继续检索', 'rev2-query-next', 'arrow', ''), true);
  }

  async function progressiveQuery(cont) {
    var err = document.getElementById('rev2-query-error');
    var out = document.getElementById('rev2-query-result');
    var bar = document.querySelector('#rev2-query-progress span');
    if (err) err.textContent = '';
    try {
      var r;
      if (cont && state.queryId) {
        r = await request('queryContinue', { id: state.queryId, primary: false, body: { resume_token: state.queryToken || '', budget_ms: 800 } });
      } else {
        var text = (document.getElementById('rev2-query-text') || {}).value || '';
        var fileEl = document.getElementById('rev2-query-file');
        var fileId = fileEl ? fileEl.value : '';
        if (!text.trim()) { err.textContent = '请输入检索文本。'; return; }
        if (!fileId) { err.textContent = '请先选择一个可检索的文件；服务端查询必须绑定文件快照。'; return; }
        r = await request('queries', { primary: false, body: { text: text, terms: [text], file_id: fileId, limit: 200, budget_ms: 800 } });
      }
      if (!r.ok) { err.textContent = '检索失败：' + r.error; return; }
      var item = r.data.item || r.data;
      state.queryId = item.id || item.query_id || state.queryId;
      state.queryToken = item.resume_token || item.cursor || '';
      var rec = await request('queryRecords', { id: state.queryId, primary: false, params: { limit: 200, cursor: state.queryCursor || '' } });
      var rows = (rec.data && rec.data.items) || [];
      state.queryCursor = (rec.data && rec.data.next_cursor) || '';
      state.queryTotal = (state.queryTotal || 0) + rows.length;
      var st = item.state || (rec.data && rec.data.state) || 'running';
      if (out) out.textContent = '已返回 ' + state.queryTotal + ' 条 · 状态 ' + st + (state.queryCursor ? ' · 还有更多' : ' · 已完成');
      if (bar) bar.style.width = st === 'done' ? '100%' : String(Math.min(92, 18 + state.queryTotal / 2)) + '%';
    } catch (e) {
      if (err) err.textContent = '检索失败：' + (e && e.message);
    }
  }

  function uploadPanel() {
    dialog('上传到私有区', '<div class="r6-form">' +
      '<div class="r6-note warn">' + I('shield') + '上传会把文件正文发送到服务端，仅进入当前身份的私有区，不进入共享总库。不选择本入口时，本机导入始终留在本浏览器。</div>' +
      '<div class="field"><label class="label" for="rev2-upload-file">选择日志文件</label><input class="input" id="rev2-upload-file" type="file" accept=".log,.txt,.out"></div>' +
      '<p class="r6-mininote">分片上传支持断点恢复；每一片在服务端落盘确认后才计入进度。</p>' +
      '<div class="r6-progress" id="rev2-upload-progress" aria-label="上传进度"><span style="width:0%"></span></div>' +
      '<div id="rev2-upload-state" class="r6-mininote"></div>' +
      '<div class="r6-errorbox" id="rev2-upload-error" role="alert"></div></div>',
      r6Btn('取消', 'modal-close', null) + r6Btn('开始上传', 'rev2-upload-start', 'upload', 'primary'), true);
  }

  async function uploadPrivate() {
    var input = document.getElementById('rev2-upload-file');
    var err = document.getElementById('rev2-upload-error');
    var label = document.getElementById('rev2-upload-state');
    var bar = document.querySelector('#rev2-upload-progress span');
    var file = input && input.files && input.files[0];
    if (!file) { err.textContent = '请先选择文件。'; return; }
    var CHUNK = 4 * 1024 * 1024;
    var chunkSize = Math.min(CHUNK, Math.max(256 * 1024, file.size));
    try {
      var created = await request('uploads', { body: { name: file.name, size: file.size, chunkSize: chunkSize } });
      if (!created.ok) throw new Error(created.error);
      var session = created.data.item || created.data;
      var id = session.id || session.upload_id;
      var total = Math.ceil(file.size / chunkSize);
      for (var i = 0; i < total; i += 1) {
        var blob = file.slice(i * chunkSize, Math.min(file.size, (i + 1) * chunkSize));
        var buf = await blob.arrayBuffer();
        var digest = await digestHex(buf);
        var csrf = state.csrfToken || '';
        var res = await fetch('/api/v2/uploads/' + encodeURIComponent(id) + '/parts/' + i, {
          method: 'PUT', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/octet-stream', 'X-LogBench-Digest': digest, 'X-LogBench-CSRF': csrf },
          body: buf
        });
        if (!res.ok) {
          var body = await res.text();
          throw new Error('分片 ' + (i + 1) + '/' + total + ' 失败：' + (body || res.status));
        }
        if (bar) bar.style.width = Math.round(100 * (i + 1) / total) + '%';
        if (label) label.textContent = '已上传 ' + (i + 1) + ' / ' + total + ' 片';
      }
      var committed = await request('uploadCommit', { id: id, body: {} });
      if (!committed.ok) throw new Error(committed.error);
      if (label) label.textContent = '已封存到私有区，等待索引。';
      state.scenario = 'saved'; state.saved = '已上传';
      await refresh(true);
    } catch (e) {
      err.textContent = '上传失败：' + (e && e.message) + '。已上传分片保留在服务端会话中，可重试续传。';
    }
  }

  async function digestHex(buf) {
    if (!(window.crypto && crypto.subtle)) {
      var bytes = new Uint8Array(buf), h = 2166136261;
      for (var i = 0; i < bytes.length; i += 1) { h ^= bytes[i]; h = Math.imul(h, 16777619); }
      return ('00000000' + (h >>> 0).toString(16)).slice(-8);
    }
    var d = await crypto.subtle.digest('SHA-256', buf);
    return Array.prototype.map.call(new Uint8Array(d), function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }

  /* ---- 文件库 / 阅读器：真实 library 数据接入 ------------------------------
   * 原型 R5 层自带渲染与解析能力（parseInWorker / indexRecords / render），
   * 但它的 files 数组是内置 fixture。这里只替换数据源：R5 的列显隐、多行与
   * Traceback、规则证据、时间线、对比等阅读能力全部保留。
   */
  function projectLibraryToFiles(items) {
    // Keep files restored from the browser's own IndexedDB (local imports);
    // only replace the server-side catalogue projection.
    var previous = new Map(files.filter(f=>f.source==='server'&&f.fileId).map(f=>[f.fileId,f]));
    for (var i = files.length - 1; i >= 0; i -= 1) {
      if (files[i].source === 'server' && files[i].fileId) files.splice(i, 1);
    }
    (items || []).slice(0, 200).forEach(function (it) {
      if (it.available === false) return;
      var old=previous.get(it.fileId);
      var same=old&&old.generation===it.generation&&old.size===Number(it.size);
      files.push(Object.assign(old||{}, {
        id: 'srv-' + it.fileId,
        fileId: it.fileId,
        name: it.displayName || it.relPath || it.fileId,
        path: it.logicalPath || it.relPath || '',
        source: 'server',
        serverRule: it.rule, relPath: it.relPath || '',
        profile: libraryRuleName({source:'server',serverRule:it.rule}),
        records: same?old.records:[], text: same?old.text:'', size: Number(it.size) || 0,
        modified: shortTime(it.observedAt),
        favorite: old?old.favorite:false, deleted: old?old.deleted:false,
        rootId: it.rootId, rootLabel: it.rootLabel,
        coverage: it.coverage, generation: it.generation,
        lazy: same?old.lazy:true, loaded: same?old.loaded:false
      }));
    });
    if (typeof indexRecords === 'function') indexRecords();
  }

  async function loadRemoteFile(f, activate=true) {
    if (!f || f.loaded) { if(f&&activate)baseOpenFile(f.id);return !!f; }
    state.scenario = 'loading'; paintStatus();
    try {
      var res = await fetch('/api/v2/library/files/' + encodeURIComponent(f.fileId) + '/content',
        { credentials: 'same-origin' });
      if (!res.ok) {
        var body = await res.text();
        var detail = {};
        try { detail = (JSON.parse(body).error) || {}; } catch (e) { detail = {}; }
        throw new Error(detail.message || ('读取失败：' + res.status));
      }
      var text = await res.text();
      var records = await parseInWorker(text, f.id);
      f.text = text;
      f.records = records || [];
      // Preserve the server byte size (text.length counts UTF-16 units).
      f.loaded = true;
      f.lazy = false;
      if (typeof indexRecords === 'function') indexRecords();
      if(!activate){state.scenario='ready';return true;}
      S.opened = S.opened.filter(function (x) { return x !== f.id; });
      S.opened.unshift(f.id);
      S.file = f.id;
      S.view = 'logs';
      state.scenario = 'ready';
      render(false); paintStatus();
    } catch (e) {
      state.scenario = 'error';
      state.lastError = e.message;
      paintStatus();
      notify('打开失败：' + e.message, true);return false;
    }
  }

  // The R5 rule editor reads files[0].records for its sample textarea. With a
  // real (possibly empty) catalogue, seed testInput with '' so `??` short-
  // circuits; an empty rules library gets its own recoverable empty state.
  var baseRenderRules = renderRules;
  renderRules = function () {
    if (!S.profiles.length) {
      return '<div class="page-heading"><h1>规则管理</h1></div>' +
        '<section class="rule-library-layout"><div class="r6-empty">' + I('sliders', 'lg') +
        '<strong>服务端规则库为空</strong>' +
        '<p>规则定义来自 /api/v2/rules。管理员发布规则后，这里会列出可编辑的提取、异常与时间线配置。</p>' +
        '</div></section>';
    }
    if (!files.length && S.testInput == null) S.testInput = '';
    return baseRenderRules();
  };

  // Keep lazy remote reads behind the existing authenticated content endpoint.
  window.addEventListener('click',async function(e){const el=e.target.closest('[data-act]');if(!el)return;const action=el.dataset.act;if(action==='download-file'){const f=files.find(f=>f.id===el.dataset.id);if(!f?.fileId)return;e.preventDefault();e.stopImmediatePropagation();try{const r=await fetch('/api/v2/library/files/'+encodeURIComponent(f.fileId)+'/content',{credentials:'same-origin'});if(!r.ok)throw Error('HTTP '+r.status);download(f.name,await r.blob());}catch(err){notify('下载失败：'+err.message,true);}return;}if(action==='open-selected'){const ids=[...S.libSelected],selected=ids.map(id=>files.find(f=>f.id===id)).filter(Boolean);if(!selected.some(f=>f.fileId&&!f.loaded))return;e.preventDefault();e.stopImmediatePropagation();for(const f of selected){if(f.fileId&&!f.loaded&&!await loadRemoteFile(f,false)){render();return;}}selected.forEach(f=>{if(!S.opened.includes(f.id))S.opened.push(f.id);});S.libSelected.clear();baseOpenFile(ids.length>1?'merged':ids[0]);}},true);

  var baseOpenFile = openFile;
  openFile = function (id) {
    var f = files.find(function (x) { return x.id === id; });
    if (f && f.lazy && !f.loaded) { loadRemoteFile(f); return; }
    baseOpenFile(id);
  };

  r6OpenReading = function (id) {
    var f = files.find(function (x) { return x.id === id || x.fileId === id; });
    if (f) { loadRemoteFile(f); return; }
    state.scenario = 'notfound'; paintStatus();
  };


  // An empty job list must say why it is empty and where records will come
  // from; the prototype rendered a bare <ol> with no explanation.
  var baseR6Events = r6Events;
  r6Events = function () {
    if (!R6.events.length) {
      return '<div class="r6-toolbar">' +
        '' +
        '<span class="spacer"></span>' + r6Btn('导出运行记录', 'export-events', 'download', 'sm') + '</div>' +
        '<div class="r6-pane"><div class="r6-empty">' + I('clock', 'lg') +
        '<strong>还没有运行记录</strong>' +
        '' +
        '</div></div>';
    }
    return baseR6Events();
  };

  // The prototype's 6th budget field (read/write chunk size) has no counterpart
  // in server/runtime Budget. Keep the six-field layout, but make that row a
  // read-only status instead of an input that silently does nothing on save.
  r6Tuning = function () {
    var t = R6.tuning;
    var row = function (label, key, value) {
      return '<div class="field"><label class="label" for="r6-tune-' + key + '">' + label + '</label>' +
        '<input id="r6-tune-' + key + '" class="input" type="number" min="1" max="64" value="' + value + '"></div>';
    };
    var body = '' +
      '<div class="r6-two">' +
      row('全局传输并发', 'collect', t.collect) +
      row('每个远程主机并发', 'perHost', t.perHost) +
      row('常驻解析进程', 'parse', t.parse) +
      row('历史补采并发', 'backfill', t.backfill) +
      '<div class="field"><label class="label">读写块大小</label>' +
      '<div class="r6-meta" style="padding:8px 0">由服务端固定，不在本页面配置</div></div>' +
      row('进程组内存预算（GiB）', 'memory', t.memory) +
      '</div>' +
      '' +
      '<details><summary>其余预算 · 只读</summary><dl class="r6-kv">' +
      [['查询槽位', t.query_slots], ['CPU 总上限', t.total_cpu],
       ['热页缓存（GiB）', t.hot_cache_bytes], ['上传槽位', t.upload_slots],
       ['单身份上传', t.per_principal_uploads], ['单身份查询', t.per_principal_queries],
       ['交互保留位', t.interactive_reserve]]
        .map(function (p) { return '<dt>' + p[0] + '</dt><dd class="mono">' + E(p[1]) + '</dd>'; }).join('') +
      '</dl><p class="r6-mininote">字段来自 /api/v2/runtime/budget 的 base；effective 为压力收缩后的实际值。</p></details>' +
      '<div class="r6-errorbox" id="r6-tune-error" role="alert"></div>';
    dialog('采集与解析资源', body,
      r6Btn('取消', 'modal-close') + r6Btn('保存资源设置', 'save-tuning', 'save', 'primary'), true);
  };

  /* ---- rules / admin：接真实服务端定义 ------------------------------------ */
  // server/storage/rules_api.py returns item.profile with exactly the shape
  // R5's baseProfiles uses (extractors / anomalies / nodes / version / desc),
  // so the prototype editor can render real server rules unchanged.
  function projectServerRules(items) {
    var fallback = (typeof Engine !== 'undefined' && Engine.defaults)
      ? Engine.defaults() : { extractors: [], anomalies: [] };
    var nodes = (typeof defaultNodes !== 'undefined') ? clone(defaultNodes) : [];
    var server = (items || []).filter(function (it) { return !it.deletedAt; })
      .map(function (it) {
        var p = it.profile || {};
        return Object.assign({}, p, {
          id: it.ruleId,
          name: it.name || p.name || it.ruleId,
          desc: it.description || p.desc || '',
          version: String(it.revision || p.version || '1'),
          status: '服务端',
          deleted: false,
          extractors: p.extractors || fallback.extractors || [],
          anomalies: p.anomalies || fallback.anomalies || [],
          nodes: p.nodes || nodes
        });
      });
    var ids = {};
    server.forEach(function (p) { ids[p.id] = true; });
    // Keep locally authored drafts that do not exist on the server.
    var local = (S.profiles || []).filter(function (p) { return !ids[p.id]; });
    S.profiles = server.concat(local);
    if (!S.profiles.some(function (p) { return p.id === S.profile; })) {
      S.profile = S.profiles.length ? S.profiles[0].id : null;
    }
  }

  // Same containers/classes as the prototype admin page, real v1 admin data.
  renderAdmin = function () {
    var d = state.adminData;
    if (!d) {
      return '<div class="page-heading"><h1>管理后台</h1></div>' +
        '<section class="settings-body"><div class="r6-empty">' + I('shield', 'lg') +
        '<strong>需要管理员权限</strong>' +
        '<p>用户、服务端配置与审计记录由 /api/v1/admin/* 逐请求授权。当前身份为只读，页面不会用占位数据填充。</p>' +
        '</div></section>';
    }
    var users = (d.users && d.users.items) || [];
    var cfg = (d.config && d.config.config) || {};
    var rt = (d.config && d.config.runtime) || {};
    var stats = d.stats || {};
    var audit = (d.audit && d.audit.items) || [];
    var me = state.me || {};
    var role = me.role === 'admin' ? '管理员' : (me.name || '访客');
    var row = function (title, note, value) {
      return '<div class="settings-row"><div><strong>' + title + '</strong><small>' + E(note) +
        '</small></div><div class="mono">' + value + '</div></div>';
    };
    var userRows = users.length ? users.map(function (u) {
      return '<tr><td>' + E(u.name || u.ip) + '</td><td class="mono">' + E(u.ip) + '</td>' +
        '<td><span class="badge ' + (u.role === 'admin' ? 'blue' : '') + '">' +
        (u.role === 'admin' ? '管理员' : '成员') + '</span></td>' +
        '<td class="muted">' + (u.enabled ? '已启用' : '已停用') + '</td></tr>';
    }).join('') : '<tr><td colspan="4" class="muted">服务端未登记用户</td></tr>';
    var auditRows = audit.length ? audit.slice(0, 8).map(function (a) {
      return '<tr><td class="mono">' + E(shortTime(a.at)) + '</td><td>' + E(a.action) +
        '</td><td class="mono">' + E(a.target || '—') + '</td></tr>';
    }).join('') : '<tr><td colspan="3" class="muted">暂无审计记录</td></tr>';
    return '<div class="page-heading"><h1>管理后台</h1>' +
      '<span class="badge green">服务端已连接</span><span class="spacer"></span></div>' +
      '<section class="settings-body">' +
      '<div class="callout">' + I('info') + '<div>身份与权限由服务端逐请求校验；前端不代替鉴权。</div></div>' +
      '<h3 class="settings-title">身份与权限</h3>' +
      '<div class="settings-block">' +
      row('当前身份', E(me.name || me.ip || '—'), E(me.ip || '—') + ' · ' + E(role)) +
      row('身份模式', '由服务端配置决定', E(cfg.identityMode || 'local')) +
      row('服务端版本', 'GET /api/v1/health', E(stats.version || '—')) +
      '</div>' +
      '<div class="row" style="margin:22px 0 12px"><h3 class="settings-title" style="margin:0">用户列表</h3>' +
      '<span class="badge">' + users.length + '</span><span class="spacer"></span></div>' +
      '<div class="settings-block"><table class="settings-table"><thead><tr><th>用户</th><th>IP 地址</th><th>权限</th><th>状态</th></tr></thead><tbody>' +
      userRows + '</tbody></table></div>' +
      '<h3 class="settings-title" style="margin-top:23px">服务端配置与容量</h3>' +
      '<div class="settings-block">' +
      row('数据目录', '服务端绝对路径', E(rt.dataDirectory || '—')) +
      row('允许主机', 'Host 白名单', E((rt.allowedHosts || []).join(' / ') || '—')) +
      row('数据库大小', 'GET /api/v1/admin/stats', humanBytes(stats.databaseBytes)) +
      row('磁盘可用', 'GET /api/v1/admin/stats', humanBytes(stats.diskFreeBytes)) +
      row('规则 / 回收站', '服务端统计', E(String(stats.rules ?? 0)) + ' / ' + E(String(stats.trash ?? 0))) +
      '</div>' +
      '<h3 class="settings-title" style="margin-top:23px">最近审计</h3>' +
      '<div class="settings-block"><table class="settings-table"><thead><tr><th>时间</th><th>动作</th><th>对象</th></tr></thead><tbody>' +
      auditRows + '</tbody></table>' +
      '<p class="hint" style="padding:8px 12px">完整审计与诊断导出由 /api/v1/admin/audit 与运维工具提供。</p></div>' +
      '</section>';
  };

  /* ---- 暴露接口（与预览版一致） ------------------------------------------ */
  var api = {
    ENDPOINTS: ENDPOINTS, SCENARIOS: SCENARIOS, state: state, prefs: prefs,
    request: request, hydrate: hydrate,
    statusText: statusText, dataFreshness: dataFreshness, scenarioSelect: scenarioSelect,
    paintStatus: paintStatus, setScenario: setScenario, stateBanner: stateBanner,
    refresh: refresh, connect: connect, retry: retry, pause: pause, probe: probe,
    saveSource: saveSource, saveRoot: saveRoot, saveTuning: saveTuning,
    rebuildCache: rebuildCache, exportEvents: exportEvents, savePref: savePref,
    datalink: datalink, backup: backup, exportBackup: exportBackup, restoreBackup: restoreBackup,
    boot: boot, draftToApi: draftToApi,
    eventState: function () { return eventsClient ? eventsClient.state() : null; },
    jobsPanel: jobsPanel, refreshJobsPanel: refreshJobsPanel,
    searchPanel: searchPanel, progressiveQuery: progressiveQuery,
    uploadPanel: uploadPanel, uploadPrivate: uploadPrivate,
    r6Rev2: r6Rev2
  };
  return api;
})();
window.LB = LB;
LB.probeResult = null;

/* ---- 兼容层 --------------------------------------------------------------- */
window.LogBenchRemotePreview = Object.assign(window.LogBenchRemotePreview || {}, {
  adapter: LB, endpoints: LB.ENDPOINTS, state: R6
});
window.__r6 = Object.assign(window.__r6 || {}, {
  state: S, intake: R6, adapter: LB, api: LB.request,
  showView: function (v) { S.view = v; render(false); LB.paintStatus(); },
  applyEvent: function (e) { if (e && e.text) r6Event(e.source || 'system', e.kind || '事件', e.text, e.tone || ''); },
  scenario: LB.setScenario,
  follower: function () { return R6.liveFollow; }
});

window.addEventListener('click',function(e){const menu=document.querySelector('.app-tools[open]');if(menu&&(!menu.contains(e.target)||e.target.closest('.app-tools-menu button')))menu.open=false;},true);
document.addEventListener('keydown',function(e){const menu=document.querySelector('.app-tools[open]');if(menu&&e.key==='Escape'){menu.open=false;menu.querySelector('summary').focus();}});

/* 启动：先渲染骨架（loading 态），再拉取真实数据。 */
document.addEventListener('keydown',function(e){const tab=e.target.closest('.collection-head [role="tab"]');if(!tab||!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const tabs=Array.from(document.querySelectorAll('.collection-head [role="tab"]')),i=tabs.indexOf(tab),next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length;const id=tabs[next].dataset.id;tabs[next].click();document.getElementById('collection-tab-'+id)?.focus();});
LB.paintStatus();
LB.boot();
S.view="sync";render(false);window.LogBenchPrototype.ready=restoreCache().finally(()=>{S.view="sync";render(false);});
})();
