/* Declarative LogBench rule engine. Values stay lossless strings. No eval. */
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
function isWithin(path,parent){return path===parent||path.startsWith(parent.replace(/\/$/,'')+'/');}
function resolveProfile(f,profiles,folders={},bindings={}){const valid=id=>profiles.find(p=>p.id===id&&!p.deleted);let p=valid(bindings[f.id]);if(p)return{profile:p,source:'文件单独指定',origin:f.name};const entries=Object.entries(folders).filter(([k,id])=>k.startsWith(f.source+':')&&isWithin(f.path,k.slice(f.source.length+1))&&valid(id)).sort((a,b)=>b[0].length-a[0].length);if(entries.length)return{profile:valid(entries[0][1]),source:'继承目录',origin:entries[0][0].slice(f.source.length+1)};return{profile:profiles.find(p=>!p.deleted)||profiles[0],source:'工作区默认',origin:'默认规则'};}
function filterTime(rows,a,b){return rows.filter(r=>r.key&&(!a||r.key>=a)&&(!b||r.key<=b));}
function align(fs,a,b){const map=new Map();for(const f of fs)for(const r of filterTime(f.records,a,b)){if(!map.has(r.key))map.set(r.key,{key:r.key,cells:Object.fromEntries(fs.map(x=>[x.id,[]]))});map.get(r.key).cells[f.id].push(r);}return[...map.values()].sort((x,y)=>x.key.localeCompare(y.key));}
class Heights{constructor(vals){this.values=Float64Array.from(vals);this.tree=new Float64Array(vals.length+1);for(let i=0;i<vals.length;i++){let j=i+1;this.tree[j]+=vals[i];const p=j+(j&-j);if(p<this.tree.length)this.tree[p]+=this.tree[j];}}sum(n){let s=0;for(let i=Math.min(n,this.values.length);i>0;i-=i&-i)s+=this.tree[i];return s;}get total(){return this.sum(this.values.length);}set(i,v){const d=v-this.values[i];this.values[i]=v;for(let j=i+1;j<this.tree.length;j+=j&-j)this.tree[j]+=d;}indexAt(offset){let i=0,s=0,bit=1;while(bit<this.tree.length)bit*=2;for(bit>>=1;bit;bit>>=1){let j=i+bit;if(j<this.tree.length&&s+this.tree[j]<=offset){i=j;s+=this.tree[j];}}return Math.min(i,Math.max(0,this.values.length-1));}}
return{parseTraceback,normalizeNode,markerShapes,markerColors,conditionLabel,testCondition,fieldNames,ops,defaults,validate,applyProfile,resolveProfile,isWithin,filterTime,align,Heights,clone};}
if(typeof module!=='undefined')module.exports=LB4EngineFactory();
