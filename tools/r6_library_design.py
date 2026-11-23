"""Unify the R6 file library without replacing its file-operation handlers."""
import re
from pathlib import Path
BASE=Path(__file__).parent

def apply(text):
    helper=(BASE/'r6_library_design.js').read_text().strip()
    # The input is the existing prototype + real adapter. Rewrite the owning
    # functions, rather than adding another render override.
    match=re.search(r'^function renderFilesLegacy\(\).*$',text,re.M)
    if not match: raise ValueError('Missing canonical file library')
    original=match[0]
    original=original.replace("if(S.folder)list=list.filter", "if(S.libSource==='server'&&R6.libraryRoot!=='all')list=list.filter(f=>f.rootId===R6.libraryRoot);if(S.folder)list=list.filter")
    original=original.replace("f.name+' '+f.path+' '+f.profile", "f.name+' '+f.path+' '+libraryRuleName(f)")
    start=original.index('<aside class="folder-pane">');end=original.index('</aside>',start)+len('</aside>')
    original=original[:start]+'<aside class="folder-pane">${libraryFolders(list)}</aside>'+original[end:]
    original=original.replace('${E(f.profile)}','${E(libraryRuleName(f))}').replace('${f.records.length} 条记录','${libraryRecordCount(f)}')
    original=original.replace("server:'data'", "server:(R6.roots.find(r=>r.id===R6.libraryRoot)?.name||'所有存储位置')")
    original=original.replace("${E(S.folder.replace(/^data\\//,''))}", "${E(S.folder.split(':').slice(1).join(':')||S.folder)}")
    text=text[:match.start()]+helper+'\n'+original+text[match.end():]
    # Drop both alternate server-list implementations and the page switch.
    text=re.sub(r'^function r6Library\(\).*\n','',text,flags=re.M)
    text=re.sub(r'  // Same containers/classes as the prototype file library, real catalogue rows\.\n  r6Library = function \(\) \{.*?\n  };\n','',text,flags=re.S)
    text=text.replace(',r6OldFiles=renderFiles','')
    text=re.sub(r'^renderFiles=function\(\).*\n','',text,flags=re.M)
    text=re.sub(r"^case'local-library':.*\n",'',text,flags=re.M)
    text=text.replace("R6.localLibrary=false;R6.libraryRoot='all';", "R6.libraryRoot='all';S.libSource='server';S.folder='';S.libSelected.clear();")
    text=text.replace("R6.localLibrary=false;R6.libraryRoot=id;", "R6.libraryRoot=id;S.libSource='server';S.folder='';S.libSelected.clear();")
    # Binding names/origins must come from the same resolved value.
    text=re.sub(r'^function decorateBindings\(\).*$',lambda _:'''function decorateBindings(){$$('[data-act="bind-file"]').forEach(el=>{const f=files.find(f=>f.id===el.dataset.id);if(!f)return;const b=libraryBinding(f);el.innerHTML=I(f.override?'sliders':'link')+E(b.name);el.title=b.source+' · '+b.origin;const subtitle=el.parentElement.querySelector('.file-path');if(subtitle)subtitle.textContent=b.source;});}''',text,flags=re.M)
    # Keep directory rule controls, now scoped to the selected root/directory.
    text=text.replace("const path=S.folder||(S.libSource==='local'?'本机缓存':'data');", "const path=S.folder||(S.libSource==='local'?'本机缓存':((R6.roots.find(r=>r.id===R6.libraryRoot)?.name||'')+':'));")
    text=text.replace("if(['server','local'].includes(S.libSource)){const source=", "if(S.libSource==='local'||(S.libSource==='server'&&R6.libraryRoot!=='all')){const source=")
    # Retain parsed data and personal file metadata across catalogue refreshes.
    text=text.replace("    for (var i = files.length - 1; i >= 0; i -= 1) {", "    var previous = new Map(files.filter(f=>f.source==='server'&&f.fileId).map(f=>[f.fileId,f]));\n    for (var i = files.length - 1; i >= 0; i -= 1) {")
    text=text.replace("      files.push({\n        id: 'srv-'", "      var old=previous.get(it.fileId);\n      var same=old&&old.generation===it.generation&&old.size===Number(it.size);\n      files.push(Object.assign(old||{}, {\n        id: 'srv-'")
    text=text.replace("profile: it.rule || '目录继承',", "serverRule: it.rule, relPath: it.relPath || '',\n        profile: libraryRuleName({source:'server',serverRule:it.rule}),")
    text=text.replace("records: [], text: '', size: Number(it.size) || 0,", "records: same?old.records:[], text: same?old.text:'', size: Number(it.size) || 0,")
    text=text.replace("favorite: false, deleted: false,\n        rootId:", "favorite: old?old.favorite:false, deleted: old?old.deleted:false,\n        rootId:")
    text=text.replace("lazy: true, loaded: false\n      });", "lazy: same?old.lazy:true, loaded: same?old.loaded:false\n      }));")
    text=text.replace('      f.size = text.length;','      // Preserve the server byte size (text.length counts UTF-16 units).')
    text=text.replace("async function loadRemoteFile(f) {", "async function loadRemoteFile(f, activate=true) {")
    text=text.replace("if (!f || f.loaded) { if (f) baseOpenFile(f.id); return; }", "if (!f || f.loaded) { if(f&&activate)baseOpenFile(f.id);return !!f; }")
    text=text.replace("      S.opened = S.opened.filter(function (x) { return x !== f.id; });", "      if(!activate){state.scenario='ready';return true;}\n      S.opened = S.opened.filter(function (x) { return x !== f.id; });")
    text=text.replace("notify('打开失败：' + e.message, true);", "notify('打开失败：' + e.message, true);return false;")
    handlers = """  // Keep lazy remote reads behind the existing authenticated content endpoint.
  window.addEventListener('click',async function(e){const el=e.target.closest('[data-act]');if(!el)return;const action=el.dataset.act;if(action==='download-file'){const f=files.find(f=>f.id===el.dataset.id);if(!f?.fileId)return;e.preventDefault();e.stopImmediatePropagation();try{const r=await fetch('/api/v2/library/files/'+encodeURIComponent(f.fileId)+'/content',{credentials:'same-origin'});if(!r.ok)throw Error('HTTP '+r.status);download(f.name,await r.blob());}catch(err){notify('下载失败：'+err.message,true);}return;}if(action==='open-selected'){const ids=[...S.libSelected],selected=ids.map(id=>files.find(f=>f.id===id)).filter(Boolean);if(!selected.some(f=>f.fileId&&!f.loaded))return;e.preventDefault();e.stopImmediatePropagation();for(const f of selected){if(f.fileId&&!f.loaded&&!await loadRemoteFile(f,false)){render();return;}}selected.forEach(f=>{if(!S.opened.includes(f.id))S.opened.push(f.id);});S.libSelected.clear();baseOpenFile(ids.length>1?'merged':ids[0]);}},true);
"""
    text=text.replace('  var baseOpenFile = openFile;',handlers+'\n  var baseOpenFile = openFile;')
    text=re.sub(r'^function isWithin\(.*$',lambda _:"function isWithin(path,parent){return path===parent||(parent.endsWith(':')?path.startsWith(parent):path.startsWith(parent.replace(/\\/$/,'')+'/'));}",text,flags=re.M)
    text=text.replace("S.extraFolders.push('data/'+name+'/新日志');S.folder='data/'+name;", "const root=R6.roots.find(r=>r.id===R6.libraryRoot)||R6.roots.find(r=>r.id==='legacy');if(!root){notify('请先登记存储位置。',true);break;}R6.libraryRoot=root.id;S.libSource='server';S.folder=(S.folder||root.name+':')+(S.folder?'/':'')+name;S.extraFolders.push(S.folder);")
    text=text.replace('目录登记会保存到服务端。','空目录登记在当前工作区。')
    text=text.replace('let html=renderFilesLegacy();const path=', "let html=renderFilesLegacy();const root=R6.roots.find(r=>r.id===R6.libraryRoot);if(S.libSource==='server'&&root?.state==='offline')html=html.replace('<div class=\"file-table-wrap\"','<div class=\"library-root-warning\" role=\"status\">存储位置离线，恢复挂载后重试。</div><div class=\"file-table-wrap\"');const path=")
    return text
