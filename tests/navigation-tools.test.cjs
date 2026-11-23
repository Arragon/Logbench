const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const src=fs.readFileSync(path.join(__dirname,'../src/r6-app.js'),'utf8');
function context(){return vm.createContext({S:{view:'logs',opened:[]},I:()=>'',B:(s)=>s,files:[],E:String});}
test('sidebar retains main navigation and opened files, removes utility and saved-view sections',()=>{
 const c=context();vm.runInContext(src.match(/^function renderSidebar\(\).*$/m)[0],c);
 vm.runInContext('var r6OldSidebar=renderSidebar;',c);
 vm.runInContext(src.match(/^renderSidebar=function\(\).*$/m)[0],c);
 const html=vm.runInContext('renderSidebar()',c);
 assert.match(html,/已打开/);assert.match(html,/采集中心/);assert.match(html,/日志浏览/);
 for(const text of ['阅读视角','异常优先','我的书签','sidefoot','管理后台','操作帮助'])assert.ok(!html.includes(text),text);
});
test('header tools preserve every relocated action',()=>{
 const c=context();vm.runInContext(src.match(/^function renderHeader\(\).*$/m)[0],c);
 vm.runInContext('var r6OldHeader=renderHeader;',c);vm.runInContext(src.match(/^renderHeader=function\(\).*$/m)[0],c);
 const html=vm.runInContext('renderHeader()',c);
 for(const action of ['rev2-jobs','rev2-search','rev2-upload','data-value="admin"','data-act="help"'])assert.ok(html.includes(action),action);
 assert.match(html,/<details class="app-tools">/);assert.match(html,/<summary/);
});
