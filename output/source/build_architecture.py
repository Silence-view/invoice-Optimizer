from pathlib import Path
from html import escape
import json, math, re, zipfile
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'output'
PDF = OUT / 'pdf' / 'invoice-automation-one-page.pdf'
DIAG = OUT / 'diagrams'
W, H = 1600, 1000
FONT_DIR = Path('/System/Library/Fonts/Supplemental')
pdfmetrics.registerFont(TTFont('Arial', str(FONT_DIR/'Arial.ttf')))
pdfmetrics.registerFont(TTFont('Arial-Bold', str(FONT_DIR/'Arial Bold.ttf')))
C = dict(ink='#162A43', muted='#52647A', blue='#2466AD', bluebg='#EDF4FC',
         teal='#13776E', tealbg='#EAF7F3', amber='#9C6109', amberbg='#FFF5DF',
         red='#AC3F49', redbg='#FCEEF0', border='#CBD6E2', bg='#F6F8FB',
         white='#FFFFFF', line='#6A7E96', purple='#7251A2', purplebg='#F4EFFB')
c = canvas.Canvas(str(PDF), pagesize=(W,H), pageCompression=1)
c.setTitle('Invoice Automation | Technical Architecture and Operational Diagrams')
c.setAuthor('Architecture proposal prepared for the project owner')
svg, pages, checks = [], [], []
page_num = 0

def color(v): return HexColor(C.get(v,v))
def rect(x,y,w,h,fill='white',stroke=None,r=8,sw=1):
    c.setFillColor(color(fill)); c.setStrokeColor(color(stroke or fill)); c.setLineWidth(sw)
    c.roundRect(x,H-y-h,w,h,r,fill=1,stroke=bool(stroke))
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{C.get(fill,fill)}" stroke="{C.get(stroke,stroke) if stroke else "none"}" stroke-width="{sw}"/>')

def line(points,col='line',sw=1.7,dash=False):
    c.setStrokeColor(color(col)); c.setLineWidth(sw); c.setDash([5,4] if dash else [])
    p=c.beginPath(); p.moveTo(points[0][0],H-points[0][1])
    for x,y in points[1:]: p.lineTo(x,H-y)
    c.drawPath(p); c.setDash([])
    pts=' '.join(f'{x},{y}' for x,y in points)
    svg.append(f'<polyline points="{pts}" fill="none" stroke="{C.get(col,col)}" stroke-width="{sw}"'+(' stroke-dasharray="5 4"' if dash else '')+'/>')

def poly(points,fill='white',stroke='line',sw=1.7):
    c.setFillColor(color(fill)); c.setStrokeColor(color(stroke)); c.setLineWidth(sw)
    p=c.beginPath();p.moveTo(points[0][0],H-points[0][1])
    for x,y in points[1:]:p.lineTo(x,H-y)
    p.close();c.drawPath(p,fill=1,stroke=1)
    pts=' '.join(f'{x},{y}' for x,y in points)
    svg.append(f'<polygon points="{pts}" fill="{C.get(fill,fill)}" stroke="{C.get(stroke,stroke)}" stroke-width="{sw}"/>')

def text(x,y,s,size=16,col='ink',bold=False,anchor='start'):
    s=str(s);font='Arial-Bold' if bold else 'Arial'
    c.setFont(font,size);c.setFillColor(color(col))
    if anchor=='middle': c.drawCentredString(x,H-y-size*.82,s)
    elif anchor=='end': c.drawRightString(x,H-y-size*.82,s)
    else:c.drawString(x,H-y-size*.82,s)
    svg.append(f'<text x="{x}" y="{y+size*.82}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" text-anchor="{anchor}" fill="{C.get(col,col)}">{escape(s)}</text>')
    tw=pdfmetrics.stringWidth(s,font,size)
    left=x-(tw/2 if anchor=='middle' else tw if anchor=='end' else 0)
    if left < -0.5 or left+tw > W+0.5 or y<0 or y+size>H:
        checks.append(dict(page=page_num,issue='out_of_page',text=s))

def wrap(s,width,size=16,bold=False):
    font='Arial-Bold' if bold else 'Arial';out=[]
    for para in s.split('\n'):
        if not para: out.append('');continue
        cur=''
        for word in para.split():
            test=(cur+' '+word).strip()
            if cur and pdfmetrics.stringWidth(test,font,size)>width:out.append(cur);cur=word
            else:cur=test
        out.append(cur)
    return out

def para(x,y,s,width,size=16,col='ink',bold=False,leading=None,maxh=None):
    leading=leading or size*1.28;ls=wrap(s,width,size,bold)
    for i,t in enumerate(ls):text(x,y+i*leading,t,size,col,bold)
    h=len(ls)*leading
    if maxh is not None and h>maxh+0.1:checks.append(dict(page=page_num,issue='text_overflow',text=s,height=h,available=maxh))
    return y+h

def tag(x,y,s,col='blue',size=12):
    tw=pdfmetrics.stringWidth(s,'Arial-Bold',size)+18
    rect(x,y,tw,22,C.get(col+'bg','bluebg'),None,5)
    text(x+9,y+5,s,size,col,True)
    return tw

def label(x,y,s,col='muted',size=12):
    tw=pdfmetrics.stringWidth(s,'Arial',size)
    rect(x-tw/2-5,y-2,tw+10,size+6,'white',None,3)
    text(x,y,s,size,col,anchor='middle')

def arrow(points,label_text=None,label_pos=None,col='line',dash=False,head=True):
    line(points,col,1.8,dash)
    if head:
        x,y=points[-1];px,py=points[-2];a=math.atan2(y-py,x-px);L=8
        poly([(x,y),(x-L*math.cos(a-.45),y-L*math.sin(a-.45)),(x-L*math.cos(a+.45),y-L*math.sin(a+.45))],col,col,1)
    if label_text:
        label(*(label_pos or ((points[0][0]+points[-1][0])/2,(points[0][1]+points[-1][1])/2-17)),label_text,col)

def node(x,y,w,h,title,tech,body,col='blue',size=15):
    rect(x,y,w,h,'white','border',9)
    rect(x,y,5,h,col,None,2)
    yy=para(x+17,y+15,title,w-34,19,'ink',True,22)
    yy=para(x+17,yy+7,tech,w-34,12,col,True,15)
    para(x+17,yy+10,body,w-34,size,'muted',leading=size*1.25,maxh=y+h-yy-22)

def notes(items,title='OPERATION NOTES'):
    x,y,w=1200,157,348
    rect(x,y,w,765,'bg',None,12)
    text(x+22,y+22,title,14,'blue',True)
    yy=y+55
    for name,body in items:
        yy=para(x+22,yy,name,w-44,16,'ink',True,20)
        yy=para(x+22,yy+7,body,w-44,14.5,'muted',leading=19)+23
    if yy>912:checks.append(dict(page=page_num,issue='notes_overflow',height=yy))

def start(title,subtitle,slug):
    global svg,page_num
    page_num+=1;svg=[]
    rect(0,0,W,H,'white',None,0)
    rect(0,0,W,7,'blue',None,0)
    text(52,32,f'INVOICE AUTOMATION  /  {page_num:02d}',13,'blue',True)
    text(52,62,title,34,'ink',True)
    para(52,109,subtitle,1480,16,'muted',leading=20,maxh=42)
    line([(52,944),(1548,944)],'border',1)
    text(52,962,'PROPOSED DESIGN | 27 OCT 2026 | UK | ~200 customers | largest customer ~2M movements/month; period assumed',11,'muted')
    text(1548,962,'01 / 01',12,'blue',True,anchor='end')
    pages.append(dict(number=page_num,slug=slug,title=title))

def end():
    p=pages[-1]
    doc=f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img"><title>{escape(p["title"])}</title><desc>Proposed technical architecture. Not an implemented system or a measured production benchmark.</desc>'+''.join(svg)+'</svg>'
    (DIAG/'invoice-automation-one-page.svg').write_text(doc)
    c.showPage()

def architecture():
    start('System architecture and technology boundaries','Two operational loops share one versioned system of record: continuous control and monthly invoicing.','system-architecture')
    arrow([(292,248),(340,248)],'incremental input',(317,158))
    arrow([(640,248),(780,248)],'upload + verify',(710,223))
    arrow([(490,320),(490,385)],'publish manifest + job',(584,349))
    arrow([(340,441),(302,441)],'read',(321,419))
    arrow([(302,493),(340,493)],'write',(321,501))
    arrow([(760,460),(800,460)])
    arrow([(740,555),(740,590),(1140,590),(1140,672),(1120,672)],'alert jobs / delivery evidence',(934,571),col='teal')
    arrow([(1120,672),(1140,672),(1140,590),(740,590),(740,555)],col='teal')
    arrow([(177,555),(177,630)],'cases / worklists',(177,588))
    arrow([(52,720),(28,720),(28,460),(52,460)],col='amber',dash=True)
    arrow([(490,555),(490,630)],'freeze close scope',(490,588))
    arrow([(640,712),(680,712)])
    arrow([(880,712),(920,712)])
    arrow([(177,800),(177,831),(1020,831),(1020,800)],'authenticated approval of exact delivery package',(595,812),col='amber',dash=True)
    node(52,180,240,140,'01  Source A / B','SOURCE CONNECTORS','Schema and cadence TBD. File, API or CDC connector.')
    node(340,180,300,140,'02  Intake service','PYTHON + BULK SQL','Publish manifest + intake job; then observations + ready assessment jobs + checkpoint.',size=14)
    node(780,180,340,140,'03  Immutable storage','OBJECT STORAGE','Raw files, PDF/TeX artifacts and versioned Parquet exports. Checksums + manifests.','teal')
    node(52,385,250,170,'04  Control workers','SQL + PYTHON / SKLEARN','Schema checks; matching; decimal reconciliation; frozen logistic inference. Cases + alert outbox.','blue',14.5)
    node(340,385,420,170,'05  Operational system of record','POSTGRESQL','Movement versions, evidence links, cases, corrections, customer / contract versions, close snapshots, approvals, dispatch jobs and audit events.','teal',16)
    node(800,385,320,170,'06  Analytics / training','PARQUET + DUCKDB + PYTHON','Versioned exports. Point-in-time features, reviewed labels, temporal evaluation. Promote a frozen model only after validation.','purple',14.5)
    node(52,630,250,170,'07  Human review','EXCEL + AUTHENTICATED UI','Case: evidence / old -> new\nReason + expected version\nInvoice: PDF / To / message\n[Submit] [Approve package]','amber',13.5)
    node(340,630,300,170,'08  Close / issue controls','PYTHON + SQL','Completeness; decimal charges; frozen snapshot. Unique billing allocations at issue. Issuance follows agreed policy.','blue',14)
    node(680,630,200,170,'09  Documents','JSON -> TEX -> PDF','Freeze number/date and content before approval. Isolated render; verify artifacts.','blue',14)
    node(920,630,200,170,'10  Dispatch','PYTHON + EMAIL API','Separate alert / invoice queues. Invoice approval recheck. Exact payload; attempts and events.','blue',14)
    rect(52,857,1120,65,'bg',None,7)
    text(68,868,'DISPATCH',10.5,'blue',True)
    text(68,889,'Queued / reserved / accepted / unknown / failed',12,'ink')
    text(445,868,'DELIVERY EVIDENCE',10.5,'teal',True)
    text(445,889,'None / delivered to server / deferred / bounced',12,'ink')
    text(835,868,'CUSTOMER ACKNOWLEDGEMENT',10.5,'amber',True)
    text(835,889,'None / explicitly confirmed',12,'ink')
    notes([
      ('One authority','PostgreSQL owns operational state. Parquet is a versioned analytical projection; an Excel export is a worklist.'),
      ('Cross-store publication','Upload and verify an object before publishing its DB reference. Object storage and SQL do not share an atomic transaction.'),
      ('Human and send controls','A correction appends a version. Approval binds the PDF, sender, recipients and message. Revocation and reservation share a lock; no guaranteed recall after submission.'),
      ('Model and evidence','A score cannot override failed accounting controls. Training is offline. Missing scores stay unknown. Email acceptance does not prove delivery or reading.'),
      ('Unresolved decisions','Source schemas, peak/total volumes, billing basis, VAT/document rules and SLAs are unknown. No production benchmark. Unknown send outcomes require reconciliation before retry.')])
    end()

architecture()
c.save()
(OUT/'source'/'diagram_manifest.json').write_text(json.dumps({'title':'Invoice automation technical architecture','date':'2026-10-27','status':'proposed','pages':pages,'layout_checks':checks},indent=2))
print(json.dumps({'pdf':str(PDF),'pages':len(pages),'layout_checks':checks},indent=2))
if checks:raise SystemExit(2)
