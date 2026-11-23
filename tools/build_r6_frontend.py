#!/usr/bin/env python3
"""Build the R6 front end from the approved design baseline.

The design layer is the Linear R6 prototype HTML verbatim: every <style> rule,
class name, shell DOM node, view and control is preserved. Only three kinds of
change are applied (docs/R6/r6-frontend-restore-plan.md §4.1):

  1. Copywriting: remove prototype self-declarations, slogans, repeated badges,
     .r6-foot disclaimers. Concept help moves into <details>.
  2. Data layer: replace the synthetic entries with an adapter inserted inside
     the prototype IIFE (so it reaches R6 / render / r6Btn / dialog directly).
  3. Missing components and states, expressed with prototype-native components.

Two modes, one transformation. The generated files are plain html/css/js
served by the existing app.py static routes; there is no runtime build step.

  python3 tools/build_r6_frontend.py            # preview (offline samples)
  python3 tools/build_r6_frontend.py --prod     # production /r6 + real API

--prod writes src/r6.html, src/r6.css and src/r6-app.js. Roll back with:
  git checkout eff4edc -- src/r6.html src/r6.css src/r6-app.js
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROD = '--prod' in sys.argv
SRC = ROOT / 'LogBench_R6_Remote_Prototype.html'
DST = ROOT / 'docs' / 'R6' / 'preview' / 'r6-restored.html'
PREVIEW_ADAPTER = ROOT / 'docs' / 'R6' / 'preview' / 'adapter.js'
PROD_ADAPTER = ROOT / 'docs' / 'R6' / 'preview' / 'adapter.prod.js'
ADAPTER = PROD_ADAPTER if PROD else PREVIEW_ADAPTER

text = SRC.read_text('utf-8')
lines = text.split('\n')
warnings = []


def sub(old, new, expect=1):
    """Exact replacement; report (but do not fail) when a phrase is absent."""
    global text
    n = text.count(old)
    if n == 0:
        warnings.append('MISSING  ' + old[:78])
        return
    if expect and n != expect:
        warnings.append('COUNT %d!=%d  %s' % (n, expect, old[:68]))
    text = text.replace(old, new)


def line(n, new):
    """Replace a whole source line (1-based). Prototype keeps one function/line."""
    assert 1 <= n <= len(lines), n
    lines[n - 1] = new


# ---------------------------------------------------------------------------
# 1. Whole-line replacements of R6 render functions.
#    Every container, class, tab, table column and control is kept from the
#    prototype. Only wording, data bindings and demo-only controls change.
# ---------------------------------------------------------------------------

line(776, r"""function r6Page(){return`<div class="r6-head"><h1>采集中心</h1><details class="r6-sub"><summary>采集链路与暂停影响</summary><span>远程目录 → 原文归档 → 可复用索引。同步不删除远端文件；暂停保留归档与检查点。</span></details><span class="spacer"></span>${r6Btn('资源设置','tuning','sliders','ghost sm')}${R6.tab==='sources'?r6Btn('添加日志源','new-source','plus','primary'):R6.tab==='storage'?r6Btn('添加存储位置','new-root','plus','primary'):''}</div><section class="r6-content"><div class="r6-tabs" role="tablist" aria-label="采集中心视图">${[['sources','远程日志源',R6.sources.length],['storage','存储位置',R6.roots.length],['cache','解析缓存',R6.sources.length],['events','运行记录',R6.events.length]].map(([id,name,count])=>`<button class="r6-tab ${R6.tab===id?'active':''}" role="tab" aria-selected="${R6.tab===id}" data-r6="tab" data-id="${id}">${name}<small>${count}</small></button>`).join('')}</div>${R6.tab==='sources'?r6Sources():R6.tab==='storage'?r6Storage():R6.tab==='cache'?r6Cache():r6Events()}</section>`;}""")

line(777, r"""function r6Sources(){const rows=r6SourceRows();return`<div class="r6-toolbar"><div class="search-box">${I('search')}<input id="r6-source-search" value="${E(R6.search)}" placeholder="搜索日志源、目录或存储位置" aria-label="搜索远程日志源"></div><select class="select" id="r6-source-filter" aria-label="日志源状态">${[['all','全部状态'],['following','持续追踪'],['recovering','续采 / 追赶'],['paused','已暂停'],['limited','快照模式']].map(([id,t])=>`<option value="${id}" ${R6.sourceFilter===id?'selected':''}>${t}</option>`).join('')}</select><span class="spacer"></span><div class="r6-inline-stats"><span><b>${R6.sources.reduce((n,x)=>n+x.maps.length,0)}</b>目录映射</span><span><b>${R6.sources.filter(x=>x.status==='following').length}</b>追踪中</span><span><b>${R6.sources.filter(x=>x.status==='recovering').length}</b>续采中</span></div>${r6Btn('','refresh','refresh','icon ghost','aria-label="刷新日志源状态"')}</div><div class="r6-pane"><div class="r6-table-scroll" data-scroll="r6-sources">${rows.length?`<table class="r6-table"><thead><tr><th>日志源 / 连接</th><th>目录 → 存储位置</th><th>同步状态</th><th class="r6-detail-col">归档 / 索引覆盖</th><th>操作</th></tr></thead><tbody>${rows.map(x=>`<tr class="${R6.selected===x.id?'selected':''}"><td><div class="r6-source-title"><div class="r6-source-symbol">${I(x.protocol==='http'?'cloud':'server')}</div><div><button class="r6-link" data-r6="source-detail" data-id="${x.id}">${E(x.name)}</button><div class="r6-meta">${E(r6Protocols[x.protocol])} <span class="muted">·</span> <span class="mono">${E(x.endpoint)}</span></div></div></div></td><td><div class="r6-path">${E(x.maps[0].remote)}${x.maps.length>1?`<details class="r6-sub"><summary>+${x.maps.length-1} 个目录</summary>${x.maps.slice(1).map(m=>`<div class="mono">${E(m.remote)}</div>`).join('')}</details>`:''}</div>${r6Targets(x)}</td><td>${r6Status(x)}<div class="r6-meta">${E(x.lag)}</div></td><td class="r6-detail-col"><div class="mono">${x.raw} <span class="muted">/ ${x.coverage}%</span></div><div class="r6-progress" aria-label="索引覆盖 ${x.coverage}%"><span style="width:${x.coverage}%"></span></div><div class="r6-meta">${x.count} 个文件</div></td><td><div class="r6-inline-actions">${r6Btn('查看','source-detail',null,'ghost sm',`data-id="${x.id}"`)}${r6Btn('','pause',x.status==='paused'?'play':'pause','icon ghost sm',`data-id="${x.id}" aria-label="${x.status==='paused'?'继续':'暂停'} ${E(x.name)}"`)}</div></td></tr>`).join('')}</tbody></table>`:`<div class="r6-empty">${I('search','lg')}<strong>没有匹配的日志源</strong><p>调整名称、目录或状态条件；已有采集任务不受筛选影响。</p>${r6Btn('清除筛选','clear-search','clear','sm')}</div>`}</div>${R6.selected&&r6Source(R6.selected)?r6Detail(r6Source(R6.selected)):''}</div>`;}""")

line(778, r"""function r6Detail(x){const cap=r6Capabilities(x.protocol);return`<aside class="r6-drawer" aria-label="日志源详情"><div class="r6-drawer-head">${I('server')}<span class="grow">${E(x.name)}</span>${r6Btn('','close-detail','close','icon ghost sm','aria-label="关闭日志源详情"')}</div><div class="r6-drawer-body"><div class="row">${r6Status(x)}<span class="spacer"></span><span class="r6-sub">${r6Protocols[x.protocol]}</span></div><h3 class="r6-section-title">当前采集链路</h3><ol class="r6-steps"><li class="r6-step"><span class="r6-step-mark">${I('check','sm')}</span><div><strong>发现目录与文件</strong><p>扫描 ${x.maps.length} 个目录 · ${x.count} 个文件已登记</p></div></li><li class="r6-step"><span class="r6-step-mark">${I('check','sm')}</span><div><strong>${x.status==='recovering'?'已恢复连接，继续补采':'原文持久归档'}</strong><p>${x.raw} 已确认落盘 · 不重复写入已提交范围</p></div></li><li class="r6-step ${x.coverage<100?'warn':''}"><span class="r6-step-mark">${I(x.coverage<100?'clock':'check','sm')}</span><div><strong>${x.coverage<100?'增量索引追赶中':'索引已追上归档'}</strong><p>${x.indexed} 可查询 · ${x.coverage}% 当前覆盖</p></div></li></ol><div class="r6-watermark"><div><small>已归档检查点</small><b>${x.raw}</b></div><div><small>已索引检查点</small><b>${x.indexed}</b></div><div><small>配置版本</small><b class="mono">r${LB.state.revision}</b></div></div>${x.status==='recovering'?'<div class="r6-note warn">网络已恢复不等于解析已完成。原文与索引分别记录进度，缺失段会显式标记。</div>':''}<h3 class="r6-section-title">目录映射</h3>${x.maps.map(m=>`<div class="r6-map-line"><div class="r6-mapping-dir">${I('folder','sm')}<code>${E(m.remote)}</code></div><div class="r6-meta">${I('arrow','sm')} ${E(r6Root(m.root)?.path+'/'+m.target)}</div><div class="r6-meta">继承规则：${E(m.rule)}</div></div>`).join('')}<details><summary>连接能力与采集约束</summary><h3 class="r6-section-title">连接能力</h3><div class="r6-capabilities">${r6Badge(cap.list?'目录发现':'目录清单需配置',cap.list?'good':'warn')}${r6Badge(cap.range?'范围读取':'无范围续传',cap.range?'blue':'warn')}${r6Badge(cap.live?'追加追踪':'版本快照',cap.live?'good':'')}</div><p class="r6-mininote">${E(cap.text)}</p><h3 class="r6-section-title">归档与访问</h3><dl class="r6-kv"><dt>源文件删除</dt><dd>保留本地归档，不传播删除</dd><dt>文件轮转</dt><dd>按代次归档，旧代次继续补齐</dd><dt>首次采集</dt><dd>从头归档，历史补采与新日志分别排队</dd><dt>访问范围</dt><dd>${E(x.readers)}</dd><dt>采集身份</dt><dd>${E(x.credential)} · 仅引用，不显示密钥</dd><dt>数据版本</dt><dd class="mono">revision ${LB.state.revision} · ${E(LB.state.freshness)}</dd></dl></details></div><div class="r6-drawer-foot">${r6Btn('打开日志','read-source','logs','primary',`data-id="${x.id}"`)}${r6Btn('编辑','edit-source','edit','',`data-id="${x.id}"`)}${r6Btn(x.status==='paused'?'继续':'暂停','pause',x.status==='paused'?'play':'pause','',`data-id="${x.id}"`)}</div></aside>`;}""")

line(779, r"""function r6Storage(){return`<div class="r6-toolbar"><details class="r6-sub"><summary>总库如何聚合</summary>逻辑总库聚合多个物理根目录；文件用存储位置 ID 区分，不靠同名路径合并。</details><span class="spacer"></span>${r6Btn('查看文件库','library','folder','sm')}</div><div class="r6-pane"><div class="r6-table-scroll"><table class="r6-table r6-root-table"><thead><tr><th>存储位置</th><th>物理目录</th><th>状态 / 可用空间</th><th>映射数</th><th>操作</th></tr></thead><tbody>${R6.roots.map(x=>`<tr><td><div class="r6-source-title"><div class="r6-source-symbol">${I('database')}</div><div class="r6-entity">${E(x.name)}<div class="r6-meta">${E(x.type)} · ${x.id}</div></div></div></td><td><div class="r6-path">${E(x.path)}</div><div class="r6-meta">索引：${E(x.cache)}</div></td><td>${r6Badge(x.state==='online'?'可读写':'离线 / 未挂载',x.state==='online'?'good':'bad')}<div class="r6-meta">${E(x.free)}${x.state==='online'?' 可用':''}</div></td><td>${R6.sources.flatMap(s=>s.maps).filter(m=>m.root===x.id).length}</td><td><div class="r6-inline-actions">${r6Btn('浏览','root-browse','folder','ghost sm',`data-id="${x.id}" ${x.state==='offline'?'disabled':''}`)}${r6Btn('设置','edit-root','settings','ghost sm',`data-id="${x.id}"`)}</div></td></tr>`).join('')}</tbody></table><div style="padding:20px 18px;max-width:900px"><div class="r6-note warn">${I('shield')}目录离线时暂停该位置的写入，不会自动退回部署目录，也不会把“未挂载”当作空目录重新创建。</div><details><summary>存储位置与采集目录分开管理</summary><dl class="r6-kv"><dt>管理员登记</dt><dd>先注册绝对路径并验证服务账号的读写权限、挂载身份和剩余空间。</dd><dt>每个远程目录</dt><dd>选择存储位置和独立子目录。相对路径保留，重叠目标在保存前阻止。</dd><dt>统一检索</dt><dd>文件、时间范围和多文件对比使用同一总库目录，不要求物理目录相邻。</dd><dt>解析索引</dt><dd>索引留在服务器本地磁盘；不把 SQLite WAL 数据库放在 SMB / NFS 共享盘。</dd></dl></details></div></div></div>`;}""")

line(780, r"""function r6Cache(){return`<div class="r6-resource"><div><strong>按文件代次复用，不因重新打开而重解析</strong><small>原文、结构索引、规则结果分层保存；颜色与列设置不触发重建。</small></div><span class="spacer"></span><span class="r6-healthmark">2 个解析进程 · 运行中</span>${r6Btn('资源设置','tuning','sliders','sm')}</div><div class="r6-toolbar"><details class="r6-sub"><summary>查询与重建的边界</summary>查询只使用已提交索引；重建时继续提供旧版，并标明规则版本。检查点与原文归档属于持久数据，不作为缓存清理。</details><span class="spacer"></span>${r6Btn('重建规则结果','cache-rebuild','refresh','sm')}</div><div class="r6-pane"><div class="r6-table-scroll"><table class="r6-table"><thead><tr><th>文件 / 代次</th><th>归档原文</th><th>结构索引</th><th>规则结果</th><th>覆盖 / 状态</th><th>操作</th></tr></thead><tbody>${R6.sources.map((x,i)=>`<tr><td><button class="r6-link" data-r6="read-source" data-id="${x.id}">${['cpqpsgx.log','device_io.log','build_20261031.log','legacy_10.log'][i]||'session.log'}</button><div class="r6-meta">${E(x.name)} · g00${7+i}</div></td><td class="mono">${x.raw}</td><td><span class="r6-badge ${x.count?'good':''}">${I(x.count?'check':'clock','sm')}${x.count?'可复用':'待建立'}</span><div class="r6-meta">分帧 / 时间 / 原文位置</div></td><td>${r6Badge(!x.count?'未开始':R6.rebuilding&&i===0?'重算中 · 旧版可读':'版本有效',!x.count?'':R6.rebuilding&&i===0?'blue':'good')}<div class="r6-meta">${i===0?'CPQPSGX 1.2':x.maps[0].rule}</div></td><td><span class="mono">${x.coverage}%</span><div class="r6-meta">${x.indexed} 已提交</div></td><td>${r6Btn('缓存详情','cache-detail','info','ghost sm',`data-id="${x.id}"`)}</td></tr>`).join('')}</tbody></table><div style="padding:20px 18px;max-width:900px"><details><summary>什么时候需要重算？</summary><div class="r6-review-grid"><section><h3>仅追加新日志</h3><p class="r6-mininote">从最后提交的字节与记录边界继续，尾部未结束的 Traceback 保留为待完成记录。</p></section><section><h3>修改提取 / 异常 / 节点规则</h3><p class="r6-mininote">只重算受影响的规则结果层；规则草稿与已应用版本完全分开。</p></section><section><h3>修改配色或点位形状</h3><p class="r6-mininote">直接更新呈现。有效的结构索引、字段与命中证据无需重建。</p></section><section><h3>原文被替换、截断或规则引擎升级</h3><p class="r6-mininote">创建新代次或新索引版本；校验后切换，不让旧索引悄悄对应新文件。</p></section></div></details></div></div></div>`;}""")

line(781, r"""function r6Events(){return`<div class="r6-toolbar"><details class="r6-sub"><summary>记录范围</summary>采集、断线重连、归档、索引和权限事件均保留来源与时间；来自 /api/v2/jobs 与 SSE 增量。</details><span class="spacer"></span>${r6Btn('导出运行记录','export-events','download','sm')}</div><div class="r6-pane"><div class="r6-table-scroll"><ol class="r6-log-list">${R6.events.map(e=>`<li><time>${E(e.at)}</time><span>${E(e.source)}</span><span>${r6Badge(e.kind,e.tone)}</span><div>${E(e.text)}</div></li>`).join('')}</ol></div></div>`;}""")

line(788, r"""function r6StepReview(){const d=r6Draft;return`<div class="r6-review-grid"><section><h3>连接与访问</h3><dl class="r6-kv"><dt>协议</dt><dd>${r6Protocols[d.protocol]}</dd><dt>服务器</dt><dd class="mono">${E(d.endpoint)}</dd><dt>访问范围</dt><dd>${E(d.readers)}</dd></dl></section><section><h3>采集方式</h3><dl class="r6-kv"><dt>模式</dt><dd>${{live:'持续增量',schedule:'定时同步',snapshot:'稳定文件快照'}[d.mode]}</dd><dt>目录映射</dt><dd>${d.maps.length} 个</dd><dt>历史范围</dt><dd>${d.first==='all'?'完整归档历史':'仅接入后新增，有历史缺口'}</dd></dl></section></div><h3 class="r6-section-title">预检与失败恢复</h3><div class="row">${r6Btn('运行预检','probe','shield','sm')}<span class="r6-sub">验证连接、主机密钥、目录发现、范围读取与目标子目录权限；不会写入远程文件。</span></div><div class="r6-probe" id="r6-probe-result">${LB.probeResult?LB.probeResult.checks.map(c=>`<div class="r6-probe-row ${c.ok?'':'bad'}">${I(c.ok?'check':'error')}<span>${E(c.label)} · ${E(c.detail)}</span></div>`).join(''):'<div class="r6-mininote">尚未运行预检。保存前必须通过全部检查。</div>'}</div>${LB.probeResult&&!LB.probeResult.ok?'<div class="r6-note bad" style="margin-top:12px">预检未通过，保存已停用。修正凭据、主机密钥或文件清单后重新预检；不会自动降级为匿名连接。</div>':''}<div class="r6-errorbox" id="r6-wizard-error" role="alert"></div>`;}""")

line(789, r"""function r6Wizard(){const titles=['连接','目录映射','采集策略','确认与预检'];const side=[['连接只是能力入口','SSH 优先使用 SFTP；SMB 与 HTTP 在连接后检测能力，不能仅凭协议名称承诺实时续采。','凭据由管理员在服务端管理，浏览器只保存引用，不保存密码。'],['一个目录，一条映射','管理员先登记存储位置。每条映射选择物理根目录，再指定不重叠的子目录。','相同文件名通过源 ID、相对路径与代次区分；跨库查询不复制文件。'],['归档与索引各自推进','原文先持久化，再推进确认检查点。解析可以稍后追上，不阻塞已有日志阅读。','日志持续写入时，尾部未完成的多行记录标为待完成，不伪造完整 Traceback。'],['先验证，再启用','预检验证连接、主机密钥、目录发现、偏移读取、目标权限与协议降级。','能力不足不会伪装成成功；未通过时保存按钮停用，草稿原样保留。']][r6Step];dialog(r6Draft.id?'编辑远程日志源':'添加远程日志源',`<div class="r6-wizard-tabs" role="tablist" aria-label="日志源设置步骤">${titles.map((t,i)=>`<button data-r6="wizard-step" data-index="${i}" class="${i===r6Step?'active':''}" role="tab" aria-selected="${i===r6Step}"><span class="step-no">${i+1}</span>${t}</button>`).join('')}</div><div class="r6-wizard-layout"><div class="r6-form">${[r6StepConnection,r6StepMappings,r6StepPolicy,r6StepReview][r6Step]()}</div><aside class="r6-form-side"><strong>${side[0]}</strong><p>${side[1]}</p><details><summary>约束与恢复</summary><p>${side[2]}</p><p>写入使用 expected_revision；版本冲突（409）时保留草稿，不覆盖服务端数据。</p></details></aside></div>`,r6Btn('取消','wizard-cancel',null)+`<span class="spacer"></span>`+(r6Step?r6Btn('上一步','wizard-back','left'):'')+(r6Step<3?r6Btn('下一步','wizard-next','arrow','primary'):r6Btn('保存日志源','wizard-save','check','primary',LB.probeResult&&LB.probeResult.ok?'':'disabled')),true);$('#dialog').classList.add('r6-dialog');}""")

line(791, r"""function r6SaveSource(){LB.saveSource();}""")

line(792, r"""function r6RootDialog(id){const x=id?r6Root(id):{id:null,name:'',path:'/log/Logbench/data-new',cache:'/log/Logbench/index',defaultRule:'CPQPSGX 标准'};R6.editRoot=id;dialog(id?'存储位置设置':'添加存储位置',`<div class="field"><label class="label" for="r6-root-name">位置名称</label><input class="input" id="r6-root-name" value="${E(x.name)}"></div><div class="field"><label class="label" for="r6-root-path">服务器绝对路径</label><input class="input mono" id="r6-root-path" value="${E(x.path)}" ${id?'readonly':''}><p class="hint">修改已有位置的物理目录需要单独迁移，不在此处直接重定向。</p></div><div class="field"><label class="label" for="r6-root-cache">本地索引目录</label><input class="input mono" id="r6-root-cache" value="${E(x.cache)}"></div><div class="r6-note">保存后会调用 /api/v2/storage-roots 的预检，验证运行账号权限、磁盘挂载身份和可用空间。验证失败不会启用该位置。</div><div id="r6-root-error" class="r6-errorbox" role="alert"></div>`,r6Btn('取消','modal-close')+r6Btn('保存存储位置','save-root','save','primary'));}""")

line(793, r"""function r6Tuning(){const t=R6.tuning;dialog('采集与解析资源',`<p class="hint">资源上限与实时、历史任务分开设置；保存后立即生效，状态栏显示服务端回执。</p><div class="r6-two">${[['全局传输并发','collect'],['每个远程主机并发','perHost'],['常驻解析进程','parse'],['历史补采并发','backfill'],['读写块大小（MiB）','chunk'],['进程组内存预算（GiB）','memory']].map(([l,k])=>`<div class="field"><label class="label" for="r6-tune-${k}">${l}</label><input id="r6-tune-${k}" class="input" type="number" min="1" max="64" value="${t[k]}"></div>`).join('')}</div><div class="r6-note">250 GB RAM 优先留给文件系统页缓存，不将整份 1 GB 日志展开为浏览器或 Python 对象。首次建索引与已索引阅读分开限流。</div><details><summary>高级预算（其余 7 项）与调优建议</summary><dl class="r6-kv">${[['query_slots','查询槽位'],['total_cpu','CPU 总上限'],['hot_cache_bytes','热页缓存'],['io_mb','磁盘 I/O 上限'],['index_queue','索引队列'],['upload_slots','上传槽位'],['retention_days','运行记录保留天数']].map(([k,l])=>`<dt>${l}</dt><dd class="mono">${t[k]??'—'}</dd>`).join('')}</dl><p class="r6-mininote">应在目标机比较 1 / 2 / 4 个解析进程，观察 CPU、磁盘等待、网络、索引积压和前台请求延迟。</p></details><div class="r6-errorbox" id="r6-tune-error" role="alert"></div>`,r6Btn('取消','modal-close')+r6Btn('保存资源设置','save-tuning','save','primary'),true);}""")

line(794, r"""function r6CacheDetail(id){const x=r6Source(id);dialog('解析缓存 · '+x.name,`<dl class="r6-kv"><dt>文件代次</dt><dd class="mono">${E(id)} / g0007</dd><dt>原文检查点</dt><dd>${x.raw}，已确认落盘</dd><dt>解析检查点</dt><dd>${x.indexed}，${x.coverage}% 当前覆盖</dd><dt>结构索引键</dt><dd class="mono">file_id + generation + parser_build + encoding</dd><dt>规则结果键</dt><dd class="mono">structure_id + semantic_rule_hash + access_scope</dd><dt>未完成尾部</dt><dd>保存原始字节与多行状态；下次追加后修订同一记录</dd><dt>删除缓存</dt><dd>仅删除可重建索引；不删除原文、采集检查点或审计</dd></dl><details><summary>索引键与代次如何对应</summary><p class="r6-mininote">容量与代次来自 /api/v2/cache 的服务端回执；重建会创建新规则结果版本，校验通过后才切换，旧版本在切换前持续可读。</p></details>`,r6Btn('关闭','modal-close')+r6Btn('重建规则结果','cache-rebuild','refresh','primary'));}""")

line(795, r"""function r6Library(){const roots=R6.libraryRoot==='all'?R6.roots:R6.roots.filter(x=>x.id===R6.libraryRoot);const entries=R6.sources.flatMap((s,i)=>s.maps.filter(m=>roots.some(r=>r.id===m.root)).map((m,j)=>({s,m,name:j?'algorithm.log':['cpqpsgx.log','device_io.log','build_20261031.log','legacy_10.log'][i]||'session.log'})));return`<div class="r6-head"><h1>文件库</h1><span class="r6-badge">${entries.length} 个文件</span><span class="spacer"></span>${r6Btn('存储位置','storage-tab','database','sm')}</div><section class="r6-content"><div class="r6-toolbar">${r6Btn('本机临时文件','local-library','laptop','ghost sm')}<span class="spacer"></span><span class="r6-sub">本机文件只保留在浏览器</span></div><div class="library-shell" style="flex:1;min-height:0"><aside class="folder-pane"><div class="folder-title">存储位置</div><button class="folder-item ${R6.libraryRoot==='all'?'selected':''}" data-r6="root-browse" data-id="all">${I('layers')}所有位置</button>${R6.roots.map(r=>`<button class="folder-item ${R6.libraryRoot===r.id?'selected':''}" data-r6="root-browse" data-id="${r.id}">${I(r.state==='online'?'folder':'warning')}<span>${E(r.name)}</span></button>${R6.sources.flatMap(s=>s.maps).filter(m=>m.root===r.id).map(m=>`<div class="r6-meta" style="padding-left:33px;margin:7px 0">${E(m.target)}</div>`).join('')}`).join('')}</aside><div class="library-main"><div class="r6-filebar">${I('folder')}<strong>${R6.libraryRoot==='all'?'全部存储位置':E(r6Root(R6.libraryRoot)?.name)}</strong><span class="spacer"></span><details class="r6-sub"><summary>默认规则</summary>共享目录默认规则保持继承；单文件覆盖见「规则绑定」。</details></div><div class="r6-table-scroll">${entries.length?`<table class="r6-table"><thead><tr><th>日志 / 相对目录</th><th>存储位置</th><th>原文 / 索引</th><th>规则 / 来源</th><th>操作</th></tr></thead><tbody>${entries.map(({s,m,name})=>`<tr><td><button class="r6-link" data-r6="read-source" data-id="${s.id}">${E(name)}</button><div class="r6-meta mono">${E(m.target)}</div></td><td><span>${E(r6Root(m.root)?.name)}</span><div class="r6-meta">${E(s.name)}</div></td><td><span class="mono">${s.raw}</span><div class="r6-meta">${s.coverage}% 可查询</div></td><td><span>${E(m.rule)}</span><div class="r6-meta">目录继承 · 规则版本冻结</div></td><td>${r6Btn('打开','read-source','logs','ghost sm',`data-id="${s.id}"`)}</td></tr>`).join('')}</tbody></table>`:`<div class="r6-empty">${I('warning','lg')}<strong>存储位置不可用</strong><p>保留目录登记，不把离线位置当成空库。恢复挂载后重新验证。</p>${r6Btn('查看存储位置','storage-tab','database','sm')}</div>`}</div></div></div></section>`;}""")

line(796, r"""function r6OpenReading(id){const x=r6Source(id);R6.liveSource=id;R6.newLines=0;R6.liveFollow=false;const f=files.find(f=>f.id==='f1')||files.find(f=>f.source==='server');if(f){openFile(f.id);notify('已打开 '+x.name+' 的归档索引；正文由本机 Worker 读取，不上传服务端。');}}""")

line(797, r"""function r6Livebar(){const x=r6Source(R6.liveSource);if(!x)return'';return`<div class="r6-livebar">${I('database','sm')}<strong>服务器索引阅读</strong><span>${E(x.name)}</span><span class="r6-sub">归档 ${x.raw} · 索引 ${x.coverage}%</span><span class="spacer"></span>${R6.newLines?r6Btn('新增 '+R6.newLines+' 条','read-new','down','sm'):''}${r6Btn(R6.liveFollow?'暂停跟随':'跟随最新','follow',R6.liveFollow?'pause':'play',`sm ${R6.liveFollow?'active':''}`)}</div>`;}""")

# 动作分派：演示态动作换成 /api/v2/* 适配层，新增补齐组件的动作。
line(811, r"""case'refresh':LB.refresh();break;""")
line(812, r"""case'pause':LB.pause(id);break;""")
line(813, r"""case'new-source':LB.probeResult=null;r6NewSource();break;case'edit-source':LB.probeResult=null;r6NewSource(id);break;""")
line(821, r"""case'probe':LB.probe();break;""")
line(822, r"""case'wizard-save':LB.saveSource();break;""")
line(824, r"""case'save-root':LB.saveRoot();break;""")
line(830, r"""case'save-tuning':LB.saveTuning();break;""")
line(832, r"""case'cache-rebuild':LB.rebuildCache();break;""")
line(833, r"""case'export-events':LB.exportEvents();break;""")
line(836, r"""""")  # append-demo removed entirely

REV2_CASES = "" if not PROD else (
    "case'rev2-jobs':LB.refreshJobsPanel();break;"
    "case'rev2-jobs-refresh':LB.refreshJobsPanel();break;"
    "case'rev2-search':LB.searchPanel();break;"
    "case'rev2-query-start':LB.progressiveQuery(false);break;"
    "case'rev2-query-next':LB.progressiveQuery(true);break;"
    "case'rev2-upload':LB.uploadPanel();break;"
    "case'rev2-upload-start':LB.uploadPrivate();break;"
)

line(838, r"""case'datalink':LB.datalink();break;
case'backup':LB.backup();break;
case'backup-export':LB.exportBackup();break;
case'backup-restore':LB.restoreBackup();break;
case'connect':LB.connect();break;
case'retry':LB.retry();break;
case'clear-filters':R6.search='';R6.sourceFilter='all';LB.setScenario('ready');render(false);break;
case'saved-view':S.scope=id;S.view='logs';render(false);notify('已切换阅读视角：'+(id==='issues'?'异常优先':'我的书签'));break;
""" + REV2_CASES + r"""} },true);""")

# 输入 / 变化委托：加状态检视器、阅读偏好、列显隐写入；删除演示情形下拉。
line(840, r"""document.addEventListener('change',e=>{if(e.target.id==='lb-scenario'){LB.setScenario(e.target.value);}if(e.target.id==='r6-source-filter'){R6.sourceFilter=e.target.value;render(false);}if(e.target.matches('[data-r6-map]')){r6CaptureDraft();r6Probe=false;const i=e.target.dataset.r6Index,p=$('[data-map-preview="'+i+'"]');if(p)p.textContent=(r6Root(r6Draft.maps[i].root)?.path||'')+'/'+r6Draft.maps[i].target;}if(e.target.matches('[data-lb-pref]'))LB.savePref(e.target.dataset.lbPref,e.target.type==='checkbox'?e.target.checked:e.target.value);if(e.target.matches('[data-visible-column]'))LB.savePref('columns.'+e.target.dataset.visibleColumn,e.target.checked);});""")

# about 覆盖层：演示声明改为数据链路入口。
line(843, r"""document.addEventListener('click',e=>{const x=e.target.closest('[data-act="about"]');if(!x)return;e.preventDefault();e.stopImmediatePropagation();LB.datalink();},true);""")

# 侧栏：补阅读视角 / 书签（原型已有的 .section-head + .side-view 形态）。
line(800, r"""renderSidebar=function(){return r6OldSidebar().replace('</nav>',`<button class="feature-button ${S.view==='sync'?'active':''}" data-r6="nav-sync" ${S.view==='sync'?'aria-current="page"':''}>${I('refresh')}<span>采集中心</span></button></nav>`).replace(/<div class="sidefoot">.*?<\/div>/,'');};""")

# 顶栏覆盖层不再做字符串补丁替换。
line(801, r"""renderHeader=function(){return r6OldHeader().replace('<span class="current">undefined</span>','<span class="current">采集中心</span>').replace('<div class="header-right">',`<div class="header-right"><details class="app-tools"><summary class="btn ghost">${I('sliders')}<span>工具</span>${I('down')}</summary><div class="app-tools-menu"><button data-r6="rev2-jobs">${I('layers')}本机批量任务</button><button data-r6="rev2-search">${I('search')}渐进检索</button><button data-r6="rev2-upload">${I('upload')}上传到私有区</button><div class="app-tools-divider"></div><button data-act="nav" data-value="admin">${I('settings')}管理后台</button><button data-act="help">${I('book')}操作帮助</button></div></details>`);};""")

# 把适配层插进 IIFE 内部（启动 render 之前），保持同作用域访问。
adapter = ADAPTER.read_text('utf-8').rstrip('\n')
line(846, lines[845] + '\n' + adapter)

text = '\n'.join(lines)

# ---------------------------------------------------------------------------
# 2. Global copy / shell replacements
# ---------------------------------------------------------------------------
sub('<title>LogBench R6 · 远程采集与索引设计原型</title>',
    '<title>LogBench R6 · 远程采集与索引</title>')

# Only added CSS in the whole preview: narrow-viewport overflow guard.
# Prototype @media(max-width:650px) leaves the 22px status bar and 39px tabs
# wrapping/overflowing at 375px. Every desktop value stays byte-identical.
NARROW_CSS = """/* Preview-only: 375px overflow guard. Desktop geometry untouched. */
@media(max-width:650px){
.lb-header .privacy-note{display:none}
.lb-header .header-right .btn{white-space:nowrap;font-size:11px;padding:0 7px}
.lb-header .prototype-tag{max-width:88px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.r6-head{padding:0 10px}
.r6-head details{display:none}
.r6-tabs{gap:12px;padding:0 10px;overflow-x:auto}
.r6-tab{white-space:nowrap;font-size:11px}
.r6-pane .r6-table-scroll{min-width:0}
.app-status .row{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.app-status .status-right .extra{display:none}
.app-status .status-right>span:last-child{display:none}
.app-status #lb-scenario{max-width:96px}
}
/* Linear R6 project baseline: detail/task panels float over the reading
   surface and must not shrink it. The prototype only applied this below
   1000px; the desktop drawer was a flex item that took 380px from the table.
   Same class, same width and colours - only the positioning changes. */
@media(min-width:1001px){.r6-drawer{position:absolute;right:0;top:0;bottom:0;z-index:15;box-shadow:-8px 0 24px #182c4b18}}
"""
sub('</style></head>', NARROW_CSS + '</style></head>')
sub('/* Review-only extension to R5. No network calls, credentials, or real sync jobs. */',
    '/* R6 remote ingestion. Data supplied by the /api/v2 adapter (LB). */')
sub("'logbench-r6-review-state-v1'", "'logbench-r6-workspace'")
sub('logbench-r6-preview-local-', 'logbench-r6-', expect=0)

# 顶栏：状态入口 + 备份入口 + 隐私说明
sub('<button class="prototype-tag" data-act="about">交互原型</button><span class="privacy-note">${I(\'lock\',\'sm\')} 本机处理</span>${B(\'打开日志\',\'import\',\'upload\',\'primary\',\'id="import-button"\')}',
    '<button class="prototype-tag" data-r6="datalink">未连接服务</button><span class="privacy-note">${I(\'lock\',\'sm\')} 本机文件不上传</span><button class="btn icon ghost" data-r6="backup" aria-label="备份与恢复">${I(\'save\')}</button>${B(\'打开日志\',\'import\',\'upload\',\'primary\',\'id="import-button"\')}')

# R3 死代码里的状态栏文案（避免残留）
sub("'合成演示数据 · 无服务端连接'", "'服务未连接'")
sub("'操作仅作用于当前原型'", "'本机文件不上传'")
sub('LogBench / R3', 'LogBench / R6')
sub("'LogBench / R5'", "'LogBench / R6'")

# R5 状态栏（5 个非 sync 视图）
sub("<span class=\"row\">${I('lock','sm')}交互原型 · 服务器数据为样例</span><span class=\"status-right\"><span class=\"extra\">${S.view==='compare'?'共同时间轴 · 保留各文件原始时间':'文件不上传 · 原始时间不转换时区'}</span><span>LogBench / R4</span></span>",
    "<span class=\"row\">${I('lock','sm')}${LB.statusText()}</span><span class=\"status-right\"><span class=\"extra\">${S.view==='compare'?'共同时间轴 · 保留各文件原始时间':LB.dataFreshness()}</span>${LB.scenarioSelect()}<span>LogBench / R6</span></span>")
sub('decorateBindings();}', 'decorateBindings();LB.paintStatus();}', expect=0)
sub('updateStorageBadge();scheduleStateSave();}',
    'updateStorageBadge();scheduleStateSave();LB.paintStatus();}')

# R6 sync 状态栏
sub("${I('lock','sm')}设计原型 · 合成数据 · 不连接远程服务", "${I('lock','sm')}${LB.statusText()}")
sub('<span class="status-right"><span>LogBench / R6</span></span></footer></main>`;restore(pos,keep);};',
    '<span class="status-right"><span class="extra">${LB.dataFreshness()}</span>${LB.scenarioSelect()}<span>LogBench / R6</span></span></footer></main>`;restore(pos,keep);LB.paintStatus();};')
sub("'LogBench / R6 设计'", "'LogBench / R6'")

# 显示设置浮层追加阅读偏好高级区（复用 .column-settings / .column-options / .check）
sub('<p class="hint">正文与记录操作保留。列设置自动保存，也会随阅读视角保存。</p></div>`);const panel=',
    '<p class="hint">正文与记录操作保留。列设置自动保存，也会随阅读视角保存。</p></div><details class="column-settings"><summary>阅读偏好（写入工作区）</summary><div class="column-options"><label class="check"><input type="checkbox" data-lb-pref="wrap">自动换行</label><label class="check"><input type="checkbox" data-lb-pref="contrast">高对比</label><label class="check"><input type="checkbox" data-lb-pref="density">舒适密度</label></div><p class="hint">偏好写入 /api/v2/workspace；状态栏显示保存结果。</p></details>`);const panel=')

# 侧栏 / 文件标签 / 视图内的演示标记
sub('<span class="badge" style="margin-left:auto">演示</span>', '')
sub('<span class="badge" style="margin-left:auto;font-size:10px">演示</span>', '')
sub('<span class="badge" style="font-size:10px">演示</span>', '')
sub("${f.source==='local'?'本机':'示例'}", "${f.source==='local'?'本机':'服务端'}")
sub("'服务器样例'", "'服务端'")
sub("'服务器示例'", "'服务端'")
sub("'新建示例文件夹'", "'新建文件夹'")
sub('新建示例文件夹', '新建文件夹')
sub('已保存到原型', '已保存')
sub('服务器目录为演示数据，本机文件不上传', '本机文件不上传')
sub('原型导入使用内置方括号解析器；此处编辑为设计演示，不执行任意解析代码。',
    '使用内置方括号解析器；不执行任意解析代码。')
sub('这是交互设计原型。身份、服务器路径与权限变更仅作界面演示，不写入实际服务配置。',
    '身份与权限由服务端提供；前端不代替鉴权。')
sub('演示用户', '当前用户')
sub('本原型保存在当前浏览器会话', '保存在当前浏览器会话')
sub("超过原型的单文件 10 MB 限制。", "超过单文件 10 MB 限制。")
sub('关闭此原型后需重新打开原始文件。', '关闭页面后需重新打开原始文件。')
sub('本次修改仅模拟规则归属，不重新实现后端解析。', '修改会提交到 /api/v2/bindings。')
sub('从当前原型的文件库移入回收站？', '移入回收站？')
sub('原型不识别访问 IP。实际身份和权限需由后端提供，不能由前端显示代替鉴权。',
    '身份与权限由 /api/v2/me 提供；前端不代替鉴权。')
sub('此原型最多同时配置 6 条筛选。', '最多同时配置 6 条筛选。')
sub('已在原型中移入回收站，可随时恢复', '已移入回收站，可随时恢复')
sub('文件已恢复到原型文件库', '文件已恢复到文件库')
sub('已更新原型中的规则归属，未修改服务器', '规则归属已更新')
sub('原型未接入后端，无法预检服务器目录。', '无法预检目录，请检查服务连接。')
sub('添加演示用户', '添加用户')
sub('添加当前用户', '添加用户', expect=0)
sub('仅演示，不授予实际权限。', '保存后生效；权限由服务端校验。')
sub('本原型不会创建实际服务器目录。', '目录登记会保存到服务端。')
sub('已创建空的示例目录', '已创建目录')
sub('本 HTML 仅保存个人原型设置，不修改服务器共享配置。', '个人设置写入工作区，不修改共享配置。')
sub('本机原型规则库', '规则库')
sub('当前原型使用已验证的方括号格式。', '使用已验证的方括号格式。')
sub('服务器日志库为合成样例；本机缓存仅读取当前浏览器数据。',
    '服务器日志库来自 /api/v2/library；本机缓存只读取当前浏览器数据。')
sub('超过原型单文件 64 MiB 限制。', '超过单文件 64 MiB 限制。')
sub('服务器文件为合成样例，服务器权限与共享写入没有接入。',
    '服务器文件来自 /api/v2/library；权限由服务端校验。')
sub('备份超过原型的读取上限。', '备份超过读取上限。')
sub('备份文件过大，请使用原型生成的备份。', '备份文件过大，请使用 LogBench 导出的备份。')
sub("未用演示文件替换。", "未用其它文件替换。")
sub('原型设置仅保留在本次会话，浏览器拒绝本机保存。', '设置仅保留在本次会话，浏览器拒绝本机保存。')
sub('后台采集不受阅读位置影响（示例）。', '后台采集不受阅读位置影响。')

# 帮助 / 关于弹层里的长段演示声明
sub('用于评审紧凑阅读、可配置规则、全局时间筛选与多文件时间对比。',
    '紧凑阅读、可配置规则、全局时间筛选与多文件时间对比。')
sub('样本中的错误与模块节点均为设计演示，不代表真实设备故障。',
    '日志中的错误与模块节点保留原文，不代表真实设备故障。')
sub('文件仅在当前会话中读取。主题和时间收藏保存在此浏览器；服务器相关操作仅为界面演示。',
    '本机文件只在当前会话中读取；主题与时间收藏保存在此浏览器，服务器操作提交到 /api/v2/*。')
sub('全局风格沿用 unify-design v1.0.1。', '视觉体系沿用 unify-design v1.0.1。')
sub('R4 视觉体系上的交互完善：Traceback 卡片、节点分页、列显隐、本机工作区恢复、时间线条件与图标、具体规则证据。',
    'Traceback 卡片、节点分页、列显隐、本机工作区恢复、时间线条件与具体规则证据。')
# 二级弹层 / 管理视图 / 帮助文案的剩余演示标记
sub('打开本地日志，或从文件库选择示例文件。', '打开本地日志，或从文件库选择文件。')
sub('共享日志库 · 本原型仅模拟管理操作', '共享日志库 · 来自 /api/v2/library')
sub('<span class="badge amber">未连接后端</span>', '<span class="badge green">本地预览</span>')
sub('<span class="hint">此页展示管理布局与操作边界</span>', '')
sub('<span class="badge">仅演示</span>', '')
sub('<td class="muted">演示配置</td>', '<td class="muted">已启用</td>')
sub("dialog('LogBench · 交互设计原型'", "dialog('LogBench · 阅读与规则'")
sub('本原型用于评审页面布局、信息层级与关键操作，不连接服务器。',
    '紧凑阅读、可配置规则、全局时间筛选与多文件时间对比。')
sub('搜索、主题、时间选段、多行展开、本地导入和筛选导出可实际操作；服务器文件库、用户权限、共享规则发布为合成数据演示。并非生产版本。',
    '搜索、主题、时间选段、多行展开、本地导入与筛选导出在本机执行；服务器文件库、用户权限与共享规则来自 /api/v2/*。')
sub('<span class="badge amber">演示</span>', '')
sub('<summary>条件、优先级与示例</summary>', '<summary>条件、优先级与写法</summary>')
sub("dialog('LogBench R4 · 交互原型'", "dialog('LogBench · 阅读与规则'")
sub('导入、搜索、字段提取、异常规则、目录继承、时间筛选与同步对比在本机实际执行。服务器文件为合成样例，服务器权限与共享写入没有接入。',
    '导入、搜索、字段提取、异常规则、目录继承、时间筛选与同步对比在本机执行；服务器文件与权限来自 /api/v2/*。')
sub('IP 身份、多用户隔离和生产缓存生命周期仍由后端实现。',
    'IP 身份、多用户隔离与缓存生命周期由 /api/v2/* 管理。')
sub("dialog('LogBench R5 · 交互原型'", "dialog('LogBench · 阅读与规则'")
sub('日志与规则在本机解析；服务器文件和身份仍为合成演示。没有接入生产权限或共享写入。',
    '日志与规则在本机解析；服务器文件与身份来自 /api/v2/*。')
sub('原型导入使用内置方括号解析器', '使用内置方括号解析器', expect=0)

# 表单 / 协议 / 目录映射变更后让预检结果失效，避免拿旧检查保存新配置。
sub("if(e.target.matches('[data-r6-field]')){r6Probe=false;",
    "if(e.target.matches('[data-r6-field]')){LB.probeResult=null;r6Probe=false;")
sub("if(e.target.matches('[data-r6-map]')){r6CaptureDraft();r6Probe=false;",
    "if(e.target.matches('[data-r6-map]')){r6CaptureDraft();LB.probeResult=null;r6Probe=false;", expect=0)
sub("case'protocol':r6CaptureDraft();", "case'protocol':LB.probeResult=null;r6CaptureDraft();")
sub("case'add-map':r6CaptureDraft();", "case'add-map':LB.probeResult=null;r6CaptureDraft();")
sub("case'remove-map':r6CaptureDraft();", "case'remove-map':LB.probeResult=null;r6CaptureDraft();")

# 向导里的 checked disabled 假策略：改为真实可配置字段 / 只读状态行
sub('<label class="check"><input type="checkbox" checked disabled>保留未匹配行与空行来源</label>',
    '<label class="check"><input type="checkbox" checked data-lb-pref="keepUnmatched">保留未匹配行与空行来源</label>')
sub('<label class="r6-checkrow"><input type="checkbox" checked disabled>${d.protocol===\'sftp\'||d.protocol===\'scp\'?\'强制验证已登记的 SSH 主机密钥\':d.protocol===\'http\'?\'HTTPS 校验证书；HTTP 明文需明确允许\':\'使用 SMB 2/3；签名与加密能力在预检时确认\'}</label>',
    '<label class="r6-checkrow"><input type="checkbox" data-r6-field="verifyHost" checked>${d.protocol===\'sftp\'||d.protocol===\'scp\'?\'强制验证已登记的 SSH 主机密钥\':d.protocol===\'http\'?\'HTTPS 校验证书；HTTP 明文需明确允许\':\'使用 SMB 2/3；签名与加密能力在预检时确认\'}</label>')
for label in [
    '断线后从已确认检查点继续，并重新验证文件代次',
    '保留轮转与截断前的原文，不传播远端删除',
    '存储离线、权限失败或空间不足时暂停，明确告警',
    '保存服务器解析索引；没有新增字节时不重复解析',
]:
    sub('<label class="r6-checkrow"><input checked disabled type="checkbox">' + label + '</label>',
        '<label class="r6-checkrow"><input type="checkbox" data-r6-field="policy" checked>' + label + '</label>')

# ---------------------------------------------------------------------------
# 3. Write + report
# ---------------------------------------------------------------------------
# Keep all six navigation tiles on the same icon-above-label layout.
sub("${id==='logs'?'wide':''} ", '')

from r6_collection_design import apply as apply_collection_design, css as collection_css
text = apply_collection_design(text)
from r6_library_design import apply as apply_library_design
text = apply_library_design(text)
from r6_file_times import apply as apply_file_times
text = apply_file_times(text)

if PROD:
    # Split the single-file baseline into the three files that app.py already
    # serves: /r6 -> src/r6.html, /assets/r6/r6.css -> src/r6.css,
    # /assets/r6/app.js -> src/r6-app.js. /assets/r6.js (the 43-module
    # engine layer) is still assembled by server/frontend.py at request time.
    css = re.search(r'<style>(.*?)</style></head>', text, re.S).group(1)
    js = re.search(r'<script>(.*)</script></body>', text, re.S).group(1)
    body = re.search(r'(<body[^>]*>)(.*?)<script>', text, re.S)
    title = re.search(r'<title>(.*?)</title>', text, re.S).group(1)
    html = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        '<title>' + title + '</title>'
        '<link rel="stylesheet" href="/assets/r6/r6.css">'
        '</head>' + body.group(1) + body.group(2) +
        '<script src="/assets/r6.js"></script>\n'
        '<script src="/assets/r6/app.js"></script>\n'
        '</body></html>\n'
    )
    targets = [
        (ROOT / 'src' / 'r6.html', html, 'text/html'),
        (ROOT / 'src' / 'r6.css', css.lstrip('\n') + '\n' + collection_css() + '\n' + (ROOT / 'tools' / 'r6_shell_tools.css').read_text('utf-8') + '\n' + (ROOT / 'tools' / 'r6_library_design.css').read_text('utf-8') + '\n' + (ROOT / 'tools' / 'r6_file_times.css').read_text('utf-8'), 'text/css'),
        (ROOT / 'src' / 'r6-app.js', js.lstrip('\n'), 'text/javascript'),
    ]
    for target, content, _kind in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, 'utf-8')
        print('写入    :', target.relative_to(ROOT), target.stat().st_size, 'bytes')
    print('模式    : PRODUCTION（真实 /api/v2/*）')
else:
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(text, 'utf-8')
    print('输出    :', DST.relative_to(ROOT), DST.stat().st_size, 'bytes')
    print('模式    : PREVIEW（离线响应形状快照）')
print('源文件  :', SRC.relative_to(ROOT))
print('适配层  :', ADAPTER.relative_to(ROOT))
print('行数    :', len(text.split('\n')))
for w in warnings:
    print('  !', w)

# 残留扫描：主界面与所有可达文案不应再出现演示态自我声明
import re
bad = []
for i, l in enumerate(text.split('\n'), 1):
    for k in ['合成', '示例', '演示', '交互原型', '设计原型', 'synthetic:true', '原型未接入', '原型不', '本原型', '未连接后端', '不连接远程服务']:
        if k in l:
            bad.append((i, k, l[max(0, l.find(k) - 30):l.find(k) + 46]))
print('演示态残留:', len(bad))
for b in bad[:30]:
    print('  L%-4d [%s] …%s…' % b)
