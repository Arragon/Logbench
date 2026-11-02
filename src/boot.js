/* Identity is resolved before any per-user browser state is read. No anonymous fallback. */
(async()=>{
  'use strict';
  try {
    const response=await fetch('/api/v1/me',{cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(15000)});
    const value=await response.json();
    if(!response.ok)throw Error(value.error?.message||'身份服务不可用');
    if(!/^[a-f0-9]{24}$/.test(value.user?.id||''))throw Error('服务返回了无效的用户标识');
    window.LB_SESSION=value;
    const script=document.createElement('script');script.src='/assets/workbench.js';
    script.onerror=()=>showError('页面资源加载失败，请检查服务状态后重试。');
    document.head.append(script);
  }catch(error){showError(error.message);}
  function showError(message){
    const box=document.createElement('section');box.className='service-boot';
    const title=document.createElement('h1');title.textContent='暂时无法进入 LogBench';
    const description=document.createElement('p');description.textContent=message;
    const hint=document.createElement('p');hint.textContent='已有本机缓存未清理。管理员可检查 IP 权限、Host 配置和身份来源。';
    const retry=document.createElement('button');retry.className='btn primary';retry.textContent='重新连接';retry.onclick=()=>location.reload();
    box.append(title,description,hint,retry);document.getElementById('app').replaceChildren(box);
  }
})();
