"""Structural validation for R5 profiles. JS regexes are executed only in a browser Worker."""
import json
import re

OPS = {'equals','notEquals','contains','notContains','startsWith','endsWith','regex','nonzero','gt','gte','lt','lte','exists','missing'}
SHAPES = {'circle','diamond','square','triangle','star','flag','hexagon','pin','bolt','cross'}
FIELDS = {'level','logger','message','body','raw','fn','src','pid','tid','phase','traceback.type'}

def validate(profile):
    if not isinstance(profile, dict) or profile.get('format','bracket') != 'bracket':
        raise ValueError('目前支持 bracket 日志格式。')
    if len(json.dumps(profile, ensure_ascii=False).encode()) > 1024*1024:
        raise ValueError('规则配置超过 1 MiB。')
    def string(value, label, max_length=2000, required=False):
        if not isinstance(value,str) or len(value)>max_length or (required and not value.strip()):
            raise ValueError(label+'内容无效或过长。')
    def condition(q):
        if not isinstance(q,dict) or q.get('op') not in OPS:
            raise ValueError('不支持的匹配条件。')
        field=q.get('field','');string(field,'字段',150,True)
        if field not in FIELDS and not re.fullmatch(r'fields\.[A-Za-z_][\w.]*',field):
            raise ValueError('字段名无效。')
        string(str(q.get('value','')),'比较值',2000)
        if q['op']=='regex' and not q.get('value'): raise ValueError('正则不能为空。')
        if q['op'] in {'gt','gte','lt','lte'}:
            import math
            if not math.isfinite(float(q.get('value',''))): raise ValueError('阈值必须为有限数字。')
    for category in ('extractors','anomalies','nodes'):
        rows=profile.get(category)
        if not isinstance(rows,list) or len(rows)>200:raise ValueError(category+'必须为最多 200 项的数组。')
        ids=set()
        for q in rows:
            if not isinstance(q,dict):raise ValueError('每条规则必须为对象。')
            string(q.get('name'),'规则名',100,True)
            if 'enabled' in q and not isinstance(q['enabled'],bool): raise ValueError('启停状态无效。')
            if q.get('id'):
                string(q['id'],'规则 ID',100,True)
                if q['id'] in ids:raise ValueError('同类规则的 ID 不可重复。')
                ids.add(q['id'])
            if category=='extractors':
                string(q.get('pattern'),'提取正则',2000,True)
                if q.get('source') not in {'message','body','both','raw'}:raise ValueError('提取来源无效。')
                if q.get('type','auto') not in {'auto','string','number','boolean','hex'}:raise ValueError('类型标注无效。')
                for key in ('key','group','unit'):string(str(q.get(key,'')),key,100)
            elif category=='anomalies':
                condition(q)
                if q.get('severity') not in {'error','warning'}:raise ValueError('严重度无效。')
            else:
                if q.get('type') not in {'start','end','error','warning','custom'}:raise ValueError('节点类型无效。')
                if q.get('shape') and q['shape'] not in SHAPES:raise ValueError('图标形状无效。')
                if q.get('color') and not re.fullmatch(r'#[0-9a-fA-F]{6}',q['color']):raise ValueError('颜色必须为六位十六进制色值。')
                if 'conditions' in q:
                    cs=q['conditions']
                    if not isinstance(cs,list) or not 1<=len(cs)<=12:raise ValueError('节点需要 1–12 条条件。')
                    if q.get('combine','all') not in {'all','any'}:raise ValueError('条件关系无效。')
                    for c in cs:condition(c)
                else:string(q.get('pattern'),'节点正则',2000,True)
    return profile
