const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../src/r6-app.js'),'utf8');
function render(rows,search=''){
 const c={R6:{search,sourceFilter:'all',selected:null},r6SourceRows:()=>rows,r6States:{},E:s=>String(s??'').replaceAll('<','&lt;').replaceAll('"','&quot;'),I:()=>'',r6Btn:()=>'',r6Protocols:{sftp:'SFTP'},r6Root:()=>null,r6Status:()=>'',r6Source:()=>null};
 vm.createContext(c);vm.runInContext(source.match(/^function r6Sources\(\).*$/m)[0],c);return vm.runInContext('r6Sources()',c);
}
test('never-collected source has no fabricated coverage, and empty mappings are safe',()=>{
 const html=render([{id:'x',name:'<unsafe>',protocol:'sftp',endpoint:'host',maps:[],raw:'0 B',count:0,coverage:0,status:'ready',lag:'未开始'}]);
 assert.ok(html.includes('未建立'));assert.ok(html.includes('&lt;unsafe>'));assert.ok(!html.includes('width:0%'));assert.ok(html.includes('归档量'));
});
test('empty collection and filtered empty list have distinct recovery',()=>{
 assert.ok(render([]).includes('尚未添加日志源'));assert.ok(render([],'absent').includes('没有匹配的日志源'));
});
