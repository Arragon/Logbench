"""File time coverage for opened cards and the merged timeline."""
import re
from pathlib import Path
BASE=Path(__file__).parent

def apply(text):
    helpers=(BASE/'r6_file_times.js').read_text().strip()
    anchor='function renderSidebar(){'
    if text.count(anchor)!=1:raise ValueError('Expected one sidebar renderer')
    text=text.replace(anchor,helpers+'\n'+anchor)
    match=re.search(r'^function renderSidebar\(\).*$',text,re.M)
    line=match[0];line=line.replace("${fmt(f.records.length)} 条", "${f.lazy||f.loaded===false?'未解析':fmt(f.records.length)+' 条'}")
    line=line.replace('</div></div><button class="close-file', '</div></div>${fileTimeCard(f)}<button class="close-file')
    text=text[:match.start()]+line+text[match.end():]
    match=re.search(r'^function renderSegments\(\).*$',text,re.M)
    line=match[0].replace('return`','const manual=`',1)
    assert line.endswith('`}')
    line=line[:-2]+'''`;return S.file==='merged'?renderFileIntervals()+`<details class="manual-intervals" ${S.draft.keys.length?'open':''}><summary>关键点区间</summary>${manual}</details>`:manual;}'''
    return text[:match.start()]+line+text[match.end():]
