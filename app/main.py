import os
import secrets
import csv
import io
import json
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .store import initialize, connect, rows
from .agent import graph
from .ops_agent import plan_command
from .browser_operator import execute as execute_browser, EXPORTS, SHOTS

@asynccontextmanager
async def lifespan(app):
    initialize()
    yield

app=FastAPI(title='ShopMate · 电商商家操作 Agent',version='2.0.0',lifespan=lifespan)
WEB=Path(__file__).resolve().parents[1]/'web'
app.mount('/static',StaticFiles(directory=WEB),name='static')
TOKENS={}
LOCK=threading.Lock()

class Login(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=128)

class Chat(BaseModel):
    message: str = Field(min_length=1,max_length=2000)
    session_id: str | None = None

class Refund(BaseModel):
    order_id: str = Field(max_length=32)
    reason: str = Field(min_length=3,max_length=500)

class Decision(BaseModel):
    decision: str
    note: str = Field(min_length=2,max_length=500)

class Document(BaseModel):
    title: str = Field(min_length=2,max_length=100)
    content: str = Field(min_length=10,max_length=10000)

class OpsCommand(BaseModel):
    command: str = Field(min_length=1,max_length=1000)

class OpsConfirm(BaseModel):
    task_id: str = Field(min_length=12,max_length=64)

class ConsoleAction(BaseModel):
    action: str
    sku: str | None = None
    stock: int | None = Field(default=None,ge=0,le=999999)
    price_cents: int | None = Field(default=None,ge=1,le=99999999)
    order_id: str | None = None
    waybill: str | None = Field(default=None,max_length=64)

def identity(request: Request):
    token=request.cookies.get('shopmate_session','')
    with LOCK:
        record=TOKENS.get(token)
    if not record or record['expires']<time.time():
        raise HTTPException(401,'请先登录')
    if request.method not in ['GET','HEAD','OPTIONS']:
        if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),record['csrf']):
            raise HTTPException(403,'请求校验失败，请刷新后重试')
    return record

def admin(user=Depends(identity)):
    if user['username']!='admin': raise HTTPException(403,'需要管理员权限')
    return user

def ops_operator(user=Depends(identity)):
    if user['username']!='operator': raise HTTPException(403,'需要商家运营账号')
    return user

@app.get('/')
def index(): return FileResponse(WEB/'index.html')

@app.get('/api/health')
def health(): return {'status':'ok','version':'2.0.0','model_configured':bool(os.getenv('MODEL_API_KEY'))}

@app.post('/api/login')
def login(data: Login,response: Response,request: Request):
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):
        raise HTTPException(403,'不允许跨站登录')
    passwords={'operator':os.getenv('OPERATOR_PASSWORD','operator123'),'buyer':os.getenv('BUYER_PASSWORD','buyer123'),'other':os.getenv('OTHER_PASSWORD','other123'),'admin':os.getenv('ADMIN_PASSWORD','admin123')}
    if data.username not in passwords or not secrets.compare_digest(data.password,passwords[data.username]):
        raise HTTPException(401,'用户名或密码不正确')
    token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24)
    record={'username':data.username,'csrf':csrf,'expires':time.time()+28800}
    with LOCK:
        for expired in [k for k,v in TOKENS.items() if v['expires']<time.time()]: TOKENS.pop(expired,None)
        TOKENS[token]=record
    response.set_cookie('shopmate_session',token,httponly=True,samesite='strict',max_age=28800,secure=os.getenv('COOKIE_SECURE')=='1')
    return {'username':data.username,'csrf':csrf}

@app.get('/api/me')
def me(user=Depends(identity)): return {'username':user['username'],'csrf':user['csrf']}

@app.post('/api/logout')
def logout(request:Request,response:Response,user=Depends(identity)):
    with LOCK: TOKENS.pop(request.cookies.get('shopmate_session'),None)
    response.delete_cookie('shopmate_session')
    return {'ok':True}

@app.get('/api/orders')
def orders(user=Depends(identity)):
    return rows('SELECT * FROM orders WHERE owner=?',(user['username'],))

@app.get('/api/sessions')
def sessions(user=Depends(identity)):
    return rows('SELECT s.id, (SELECT content FROM messages WHERE session_id=s.id ORDER BY id LIMIT 1) AS title FROM sessions s WHERE owner=? ORDER BY rowid DESC',(user['username'],))

@app.get('/api/sessions/{sid}')
def history(sid:str,user=Depends(identity)):
    if not rows('SELECT id FROM sessions WHERE id=? AND owner=?',(sid,user['username'])): raise HTTPException(404,'会话不存在')
    return rows('SELECT role,content FROM messages WHERE session_id=? ORDER BY id',(sid,))

@app.post('/api/chat')
def chat(data:Chat,user=Depends(identity)):
    if not data.message.strip(): raise HTTPException(422,'请输入问题')
    sid=data.session_id
    if sid:
        if not rows('SELECT id FROM sessions WHERE id=? AND owner=?',(sid,user['username'])): raise HTTPException(404,'会话不存在')
    else:
        sid=uuid.uuid4().hex
        with connect() as db: db.execute('INSERT INTO sessions(id,owner) VALUES(?,?)',(sid,user['username']))
    start=time.perf_counter()
    result=graph.invoke({'message':data.message,'owner':user['username'],'session':sid})
    with connect() as db:
        db.executemany('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)',[(sid,'user',data.message),(sid,'assistant',result['answer'])])
    return {**result,'session_id':sid,'latency_ms':round((time.perf_counter()-start)*1000)}

@app.post('/api/refunds')
def request_refund(data:Refund,user=Depends(identity)):
    if len(data.reason.strip())<3: raise HTTPException(422,'请填写至少三个字的退款原因')
    with connect() as db:
        order=db.execute('SELECT * FROM orders WHERE id=? AND owner=?',(data.order_id,user['username'])).fetchone()
        if not order: raise HTTPException(404,'订单不存在或无权访问')
        db.execute('INSERT OR IGNORE INTO refunds(order_id,owner,reason) VALUES(?,?,?)',(data.order_id,user['username'],data.reason.strip()))
        refund=dict(db.execute('SELECT * FROM refunds WHERE order_id=?',(data.order_id,)).fetchone())
    return refund

@app.get('/api/refunds')
def refunds(user=Depends(identity)):
    return rows('SELECT * FROM refunds WHERE owner=? ORDER BY id DESC',(user['username'],))

@app.get('/api/admin/overview')
def overview(user=Depends(admin)):
    return {'orders':rows('SELECT * FROM orders'),'refunds':rows('SELECT r.*,o.product,o.cents FROM refunds r JOIN orders o ON o.id=r.order_id ORDER BY r.id DESC'),'documents':rows('SELECT * FROM documents'),'audit':rows('SELECT * FROM audit ORDER BY id DESC LIMIT 50'),'messages':rows('SELECT COUNT(*) AS n FROM messages')[0]['n']}

@app.post('/api/admin/refunds/{rid}')
def decide(rid:int,data:Decision,user=Depends(admin)):
    if data.decision not in ['approved','rejected']: raise HTTPException(422,'无效审核决定')
    if len(data.note.strip())<2: raise HTTPException(422,'请填写有效的审核备注')
    with connect() as db:
        cur=db.execute("UPDATE refunds SET status=?,note=? WHERE id=? AND status='pending'",(data.decision,data.note,rid))
        if not cur.rowcount: raise HTTPException(409,'申请不存在或已审核')
        db.execute('INSERT INTO audit(actor,action,detail) VALUES(?,?,?)',(user['username'],'refund_'+data.decision,f'退款 #{rid}: {data.note}'))
    return {'ok':True}

@app.post('/api/admin/documents')
def save_document(data:Document,user=Depends(admin)):
    with connect() as db:
        db.execute('INSERT INTO documents(title,content) VALUES(?,?) ON CONFLICT(title) DO UPDATE SET content=excluded.content',(data.title,data.content))
        db.execute('INSERT INTO audit(actor,action,detail) VALUES(?,?,?)',(user['username'],'knowledge_update',data.title))
    return {'ok':True}

@app.get('/mock-admin')
def mock_admin(user=Depends(ops_operator)): return FileResponse(WEB/'mock-admin.html')

@app.get('/api/ops/console/metrics')
def console_metrics(user=Depends(ops_operator)):
    orders=rows('SELECT status,COUNT(*) AS n FROM ops_orders GROUP BY status')
    products=rows('SELECT COUNT(*) AS n, SUM(stock) AS stock FROM products')[0]
    after=rows("SELECT COUNT(*) AS n FROM aftersales WHERE status='待审核'")[0]['n']
    order_map={r['status']:r['n'] for r in orders}
    return [{'label':'今日订单', 'value':sum(order_map.values())},{'label':'待发货','value':order_map.get('待发货',0)},{'label':'在售 SKU','value':products['n']},{'label':'待审核售后','value':after}]

@app.get('/api/ops/console/orders')
def console_orders(q:str='',status:str='',user=Depends(ops_operator)):
    sql='SELECT o.*,p.name AS product FROM ops_orders o JOIN products p ON p.sku=o.product_sku WHERE 1=1';args=[]
    if status: sql+=' AND o.status=?';args.append(status)
    if q:
        sql+=' AND (o.id LIKE ? OR p.name LIKE ? OR o.buyer LIKE ?)';needle=f'%{q}%';args.extend([needle,needle,needle])
    sql+=' ORDER BY created DESC'
    return rows(sql,args)

@app.get('/api/ops/console/products')
def console_products(q:str='',user=Depends(ops_operator)):
    if q:return rows('SELECT * FROM products WHERE sku LIKE ? OR name LIKE ? ORDER BY sku',(f'%{q}%',f'%{q}%'))
    return rows('SELECT * FROM products ORDER BY sku')

@app.get('/api/ops/console/aftersales')
def console_aftersales(status:str='',user=Depends(ops_operator)):
    if status:return rows('SELECT * FROM aftersales WHERE status=? ORDER BY created DESC',(status,))
    return rows('SELECT * FROM aftersales ORDER BY created DESC')

@app.get('/api/ops/console/orders.csv')
def console_export(status:str='',user=Depends(ops_operator)):
    data=console_orders(status=status,user=user);buffer=io.StringIO();writer=csv.writer(buffer)
    writer.writerow(['订单号','商品','数量','实付金额','状态','收件人','收货地区','运单号','下单时间'])
    for o in data: writer.writerow([o['id'],o['product'],o['quantity'],f"{o['amount_cents']/100:.2f}",o['status'],o['buyer'],o['address'],o['tracking'],o['created']])
    return Response('\ufeff'+buffer.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="demo-orders.csv"'})

@app.post('/api/ops/console/actions')
def console_action(data:ConsoleAction,user=Depends(ops_operator)):
    if data.action in ('update_stock','update_price') and not data.sku:
        raise HTTPException(422,'请提供商品 SKU')
    if data.action=='update_stock' and data.stock is None:
        raise HTTPException(422,'请提供目标库存')
    if data.action=='update_price' and data.price_cents is None:
        raise HTTPException(422,'请提供目标售价')
    if data.action=='ship_order' and not data.order_id:
        raise HTTPException(422,'请提供订单号')
    with connect() as db:
        if data.action=='update_stock':
            cur=db.execute('UPDATE products SET stock=?, status=? WHERE sku=?',(data.stock,'在售' if data.stock else '库存不足',data.sku))
            detail=f'{data.sku} 库存更新为 {data.stock}'
        elif data.action=='update_price':
            cur=db.execute('UPDATE products SET price_cents=? WHERE sku=?',(data.price_cents,data.sku))
            detail=f'{data.sku} 售价更新为 ¥{data.price_cents/100:.2f}'
        elif data.action=='ship_order':
            if not data.waybill or len(data.waybill.strip())<5: raise HTTPException(422,'请填写有效运单号')
            cur=db.execute("UPDATE ops_orders SET status='已发货',tracking=? WHERE id=? AND status='待发货'",(data.waybill.strip(),data.order_id))
            detail=f'{data.order_id} 录入运单号 {data.waybill.strip()}'
        else: raise HTTPException(422,'不支持的演示操作')
        if not cur.rowcount: raise HTTPException(409,'目标记录不存在或状态已变化')
        db.execute('INSERT INTO audit(actor,action,detail) VALUES(?,?,?)',(user['username'],'browser_'+data.action,detail))
    return {'ok':True,'detail':detail}

def _save_run(run_id,owner,intent,plan,status,result,expires):
    with connect() as db: db.execute('INSERT OR REPLACE INTO op_runs(id,owner,intent,plan_json,status,result_json,expires) VALUES(?,?,?,?,?,?,?)',(run_id,owner,intent,json.dumps(plan,ensure_ascii=False),status,json.dumps(result,ensure_ascii=False),expires))

def _run_view(row):
    return {'task_id':row['id'],'intent':row['intent'],'plan':json.loads(row['plan_json']),'status':row['status'],'result':json.loads(row['result_json']),'created':row['created'],'expires':row['expires']}

@app.post('/api/ops/plan')
def ops_plan(data:OpsCommand,request:Request,user=Depends(ops_operator)):
    plan=plan_command(data.command)
    task_id=uuid.uuid4().hex;expires=time.time()+600
    if plan['questions']:
        result={'message':' '.join(plan['questions'])};_save_run(task_id,user['username'],plan['intent'],plan,'needs_info',result,expires)
    elif plan['intent']=='help':
        result={'message':plan['summary']};_save_run(task_id,user['username'],plan['intent'],plan,'needs_info',result,expires)
    elif plan['requires_confirmation']:
        result={'message':'请核对操作对象和变更内容。演示后台中的确认不会影响真实店铺。'};_save_run(task_id,user['username'],plan['intent'],plan,'pending_confirmation',result,expires)
    else:
        _save_run(task_id,user['username'],plan['intent'],plan,'executing',{},expires)
        try:
            result=execute_browser(plan,request.cookies.get('shopmate_session',''),str(request.base_url))
            _save_run(task_id,user['username'],plan['intent'],plan,'completed',result,expires)
        except Exception as exc:
            result={'message':f'演示后台操作未完成：{type(exc).__name__}。请检查本地 Chrome/Playwright 安装后重试。'}
            _save_run(task_id,user['username'],plan['intent'],plan,'failed',result,expires)
    row=rows('SELECT * FROM op_runs WHERE id=?',(task_id,))[0]
    return _run_view(row)

@app.post('/api/ops/confirm')
def ops_confirm(data:OpsConfirm,request:Request,user=Depends(ops_operator)):
    found=rows('SELECT * FROM op_runs WHERE id=? AND owner=?',(data.task_id,user['username']))
    if not found: raise HTTPException(404,'操作预览不存在')
    row=found[0]
    if row['status']!='pending_confirmation': raise HTTPException(409,'此操作不在待确认状态')
    if row['expires']<time.time(): raise HTTPException(410,'操作预览已过期，请重新生成')
    plan=json.loads(row['plan_json'])
    with connect() as db:
        cur=db.execute("UPDATE op_runs SET status='executing' WHERE id=? AND owner=? AND status='pending_confirmation'",(data.task_id,user['username']))
        if not cur.rowcount: raise HTTPException(409,'操作已被处理')
    try:
        result=execute_browser(plan,request.cookies.get('shopmate_session',''),str(request.base_url))
        status='completed'
    except Exception as exc:
        result={'message':f'演示后台操作未完成：{type(exc).__name__}。请检查本地 Chrome/Playwright 安装后重试。'};status='failed'
    _save_run(data.task_id,user['username'],row['intent'],plan,status,result,row['expires'])
    return _run_view(rows('SELECT * FROM op_runs WHERE id=?',(data.task_id,))[0])

@app.get('/api/ops/runs')
def ops_runs(user=Depends(ops_operator)):
    return [_run_view(r) for r in rows('SELECT * FROM op_runs WHERE owner=? ORDER BY created DESC LIMIT 30',(user['username'],))]

@app.get('/api/ops/screenshot/{name}')
def ops_screenshot(name:str,user=Depends(ops_operator)):
    if not re.fullmatch(r'operation-[0-9-]+\.png',name): raise HTTPException(404,'截图不存在')
    path=SHOTS/name
    if not path.is_file(): raise HTTPException(404,'截图不存在')
    return FileResponse(path,media_type='image/png')

@app.get('/api/ops/download/{name}')
def ops_download(name:str,user=Depends(ops_operator)):
    if not re.fullmatch(r'订单导出-[0-9-]+\.csv',name): raise HTTPException(404,'文件不存在')
    path=EXPORTS/name
    if not path.is_file(): raise HTTPException(404,'文件不存在')
    return FileResponse(path,filename=name,media_type='text/csv; charset=utf-8')
