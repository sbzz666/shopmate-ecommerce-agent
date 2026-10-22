from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from app import store, agent
from app.main import app, TOKENS

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DB',tmp_path/'test.db')
    monkeypatch.delenv('MODEL_API_KEY',raising=False)
    for key in ['ADMIN_PASSWORD','BUYER_PASSWORD','OTHER_PASSWORD']: monkeypatch.delenv(key,raising=False)
    TOKENS.clear()
    with TestClient(app) as c: yield c

def login(c,who='buyer'):
    r=c.post('/api/login',json={'username':who,'password':who+'123'})
    assert r.status_code==200
    c.headers['X-CSRF-Token']=r.json()['csrf']

def chat(c,q,sid=None):
    r=c.post('/api/chat',json={'message':q,'session_id':sid})
    assert r.status_code==200,r.text
    return r.json()

def test_auth_and_csrf(client):
    assert client.get('/api/orders').status_code==401
    assert client.post('/api/login',json={'username':'buyer','password':'bad'}).status_code==401
    login(client)
    assert len(client.get('/api/orders').json())==3
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/chat',json={'message':'你好'}).status_code==403

def test_order_and_session_isolation(client):
    login(client)
    r=chat(client,'查询订单 SM1001')
    assert '上海' in r['answer']
    assert '无权' not in r['answer']
    assert '有权访问' in chat(client,'查询订单 SM2001')['answer']
    assert client.get('/api/admin/overview').status_code==403
    login(client,'other')
    assert client.get('/api/sessions/'+r['session_id']).status_code==404
    assert client.post('/api/chat',json={'message':'继续','session_id':r['session_id']}).status_code==404
    assert client.post('/api/refunds',json={'order_id':'SM1001','reason':'不想要了'}).status_code==404

def test_refund_complete_flow(client):
    login(client)
    r=chat(client,'我要退款')
    assert '请提供' in r['answer']
    r=chat(client,'SM1002',r['session_id'])
    assert r['intent']=='refund'
    assert '确认提交' in r['answer']
    assert client.get('/api/refunds').json()==[]
    payload={'order_id':'SM1002','reason':'下错颜色了'}
    first=client.post('/api/refunds',json=payload).json()
    second=client.post('/api/refunds',json=payload).json()
    assert first['id']==second['id']
    assert '待人工审核' in chat(client,'退款进度',r['session_id'])['answer']
    assert client.post('/api/admin/refunds/'+str(first['id']),json={'decision':'approved','note':'批准'}).status_code==403
    login(client,'admin')
    decision={'decision':'approved','note':'资料完整，批准模拟退款'}
    assert client.post('/api/admin/refunds/'+str(first['id']),json=decision).status_code==200
    assert client.post('/api/admin/refunds/'+str(first['id']),json=decision).status_code==409
    login(client)
    assert '审核通过' in chat(client,'SM1002 退款进度')['answer']

def test_rag_and_unknown(client):
    login(client)
    r=chat(client,'云感耳机续航是多少？')
    assert r['sources'][0]['title'].startswith('云感')
    assert '32 小时' in r['answer']
    assert '[1]' in r['answer']
    r=chat(client,'它能防水吗？',r['session_id'])
    assert 'IPX4' in r['answer']
    assert not chat(client,'量子引力方程怎么推导')['sources']
    assert chat(client,'退换货政策是什么')['intent']=='knowledge'

def test_knowledge_update_and_handoff(client):
    login(client,'admin')
    assert client.post('/api/admin/documents',json={'title':'星轨投影仪','content':'星轨投影仪支持自动对焦，分辨率为 1080P，售价 999 元。'}).status_code==200
    assert client.post('/api/admin/documents',json={'title':'星轨投影仪','content':'星轨投影仪支持自动对焦，分辨率为 1080P，售价 899 元。'}).status_code==200
    login(client)
    r=chat(client,'星轨投影仪多少钱')
    assert '899' in r['answer']
    assert '服务记录' in chat(client,'转人工')['answer']

def test_model_fallback_and_generation(client,monkeypatch):
    login(client)
    monkeypatch.setattr(agent,'model',lambda messages:None)
    assert '32 小时' in chat(client,'云感耳机续航')['answer']
    monkeypatch.setattr(agent,'model',lambda messages:'耳机搭配充电盒约 32 小时。[1]')
    r=chat(client,'云感耳机续航')
    assert r['mode']=='在线模型 + 检索'
    assert r['answer']=='耳机搭配充电盒约 32 小时。[1]'

def test_rejection_and_input_validation(client):
    login(client)
    assert client.post('/api/chat',json={'message':'   '}).status_code==422
    assert client.post('/api/refunds',json={'order_id':'SM1001','reason':'   '}).status_code==422
    refund=client.post('/api/refunds',json={'order_id':'SM1001','reason':'重复购买了'}).json()
    login(client,'admin')
    assert client.post('/api/admin/refunds/'+str(refund['id']),json={'decision':'unknown','note':'测试'}).status_code==422
    assert client.post('/api/admin/refunds/'+str(refund['id']),json={'decision':'rejected','note':'请补充退货凭证'}).status_code==200
    login(client)
    assert '已驳回' in chat(client,'SM1001 退款状态')['answer']

def test_parallel_refund_idempotency(client):
    login(client)
    def submit(_):
        return client.post('/api/refunds',json={'order_id':'SM1003','reason':'买错规格了'}).json()['id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids=list(pool.map(submit,range(8)))
    assert len(set(ids))==1
    assert len(client.get('/api/refunds').json())==1

def test_cross_origin_login_and_logout(client):
    assert client.post('/api/login',json={'username':'buyer','password':'buyer123'},headers={'Origin':'https://evil.example'}).status_code==403
    login(client)
    assert client.post('/api/logout',json={}).status_code==200
    assert client.get('/api/me').status_code==401

def test_natural_order_number_and_return_request(client):
    login(client)
    assert '上海' in chat(client,'我的订单SM1001到哪了')['answer']
    assert chat(client,'我要退货SM1002')['intent']=='refund'
    assert '请提供订单号' in chat(client,'查询订单 SM1001999999999999')['answer']
