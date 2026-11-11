"""智能 OCR：空间顺序候选文本、有限分块复检。保守匹配，避免跨区域误拼。"""
import re
import unicodedata
from PIL import Image, ImageOps
import numpy as np

MAX_CANDIDATES = 48

def normalize(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold()
    return re.sub(r'[\s\u200b-\u200f\ufeff\u2060\u00ad·•_\-]+', '', text)

def _items(result):
    items=[]
    for item in result or []:
        try:
            pts=np.asarray(item[0],dtype=float)
            if pts.shape != (4,2): continue
            word=str(item[1]).strip()
            if not word: continue
            x1,y1=pts.min(axis=0);x2,y2=pts.max(axis=0)
            if not np.isfinite([x1,y1,x2,y2]).all():continue
            items.append((float(x1),float(y1),float(x2),float(y2),word))
        except (TypeError,ValueError,IndexError): continue
    return items[:120]

def candidates(result):
    """输出 OCR 原文、行内拼接、列内拼接、相邻格子拼接（不做任意全排列）。"""
    boxes=_items(result)
    if not boxes:return []
    texts=[];seen=set()
    def add(s):
        s=str(s).strip()
        if s and s not in seen and len(texts)<MAX_CANDIDATES:
            seen.add(s);texts.append(s)
    for b in boxes:add(b[4])
    # 行排序：用框中心与高度分组，不把相距很远的词硬拼起来
    for vertical in (False,True):
        ordered=sorted(boxes,key=lambda b: ((b[0]+b[2])/2 if vertical else (b[1]+b[3])/2))
        groups=[]
        for b in ordered:
            coord=(b[0]+b[2])/2 if vertical else (b[1]+b[3])/2
            extent=max(1,(b[2]-b[0]) if vertical else (b[3]-b[1]))
            found=None
            for g in groups:
                if abs(coord-g[0]) <= max(extent,g[1])*0.65:
                    found=g;break
            if found is None:groups.append([coord,extent,[b]])
            else:found[2].append(b)
        for _,_,group in groups:
            group.sort(key=lambda b:b[1] if vertical else b[0])
            chain=[];last=None
            for b in group:
                gap=(b[1]-last[3]) if vertical and last else ((b[0]-last[2]) if last else 0)
                size=max(1,(b[2]-b[0]) if vertical else (b[3]-b[1]))
                if last and gap>size*2.5:
                    if len(chain)>1:add(''.join(v[4] for v in chain))
                    chain=[]
                chain.append(b);last=b
            if len(chain)>1:add(''.join(v[4] for v in chain))
    # 按阅读顺序组合连续行；最多 4 行，减少跨区域误判
    lines=sorted(boxes,key=lambda b:(round((b[1]+b[3])/2/35),b[0]))
    for n in (2,3,4):
        for i in range(max(0,len(lines)-n+1)):
            chunk=lines[i:i+n]
            xs=[(b[0]+b[2])/2 for b in chunk]
            ys=[(b[1]+b[3])/2 for b in chunk]
            widths=[max(1,b[2]-b[0]) for b in chunk]
            heights=[max(1,b[3]-b[1]) for b in chunk]
            # 仅拼接空间上相邻的识别框
            if all(abs(xs[j]-xs[j-1]) <= 3*max(widths[j],widths[j-1]) and abs(ys[j]-ys[j-1]) <= 3*max(heights[j],heights[j-1]) for j in range(1,n)):
                add(''.join(b[4] for b in chunk))
    return texts

def crops(image):
    """最多四个重叠象限；整图已识别则只在未命中时调用。"""
    w,h=image.size
    if min(w,h)<220:return []
    xmid=w//2;ymid=h//2
    overlap=int(min(w,h)*0.08)
    bounds=[(0,0,xmid+overlap,ymid+overlap),(xmid-overlap,0,w,ymid+overlap),(0,ymid-overlap,xmid+overlap,h),(xmid-overlap,ymid-overlap,w,h)]
    return [image.crop(b) for b in bounds]
