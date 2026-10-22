"""Task planning for merchant operations. Plans are deterministic and writes need confirmation."""
import json
import re
import uuid
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from .store import rows

class OpsState(TypedDict, total=False):
    command: str
    intent: str
    plan: dict
    questions: list[str]

def understand(s):
    q=s['command'].strip()
    has_order_id=bool(re.search(r'(?<![A-Z0-9])DD\d{8,14}(?![A-Z0-9])',q.upper()))
    if '发货' in q and (has_order_id or any(x in q for x in ['给订单','为订单','填写单号','录入单号','确认发货','发快递'])):
        return {'intent':'ship_order'}
    if any(x in q for x in ['待发货','已发货','已签收']) and any(x in q for x in ['查询','查','导出','订单','筛选']):
        return {'intent':'export_orders' if any(x in q for x in ['导出','下载']) else 'order_search'}
    intents=[
        ('export_orders',['导出订单','订单导出','导出待发货','导出已发货']),
        ('stock_update',['库存改','库存调整','库存设为','库存设置','补库存','增加库存','减少库存']),
        ('price_update',['改价','价格改','售价改','价格调整','改价格','调价','售价调','价格调到','价格设为']),
        ('ship_order',['填写单号','录入单号','发快递']),
        ('aftersales_search',['售后','退货','退款申请','查看退']),
        ('product_search',['商品库存','查库存','库存多少','查商品','商品信息','商品列表']),
        ('order_search',['订单','待发货','已发货','已签收','查单']),
    ]
    intent='help'
    for candidate, words in intents:
        if any(word in q for word in words):
            intent=candidate
            break
    return {'intent':intent}

def build_plan(s):
    q=s['command'].strip()
    plan={'intent':s['intent'],'command':q,'requires_confirmation':False,'target':{},'summary':'','questions':[]}
    if s['intent'] in ('order_search','export_orders'):
        m=re.search(r'(?<![A-Z0-9])DD\d{8,14}(?![A-Z0-9])',q.upper())
        status=next((x for x in ['待发货','已发货','已签收','售后处理中'] if x in q), '')
        plan['target']={'order_id':m.group() if m else '', 'status':status}
        plan['summary']=('导出' if s['intent']=='export_orders' else '查询') + (f'订单 {m.group()}' if m else f'{status}订单' if status else '全部订单')
        if s['intent']=='export_orders': plan['requires_confirmation']=False
    elif s['intent']=='product_search':
        product=_match_product(q)
        plan['target']={'sku':product['sku'] if product else '', 'name':product['name'] if product else _product_hint(q)}
        plan['summary']=f"查询商品 {product['name']}" if product else '查询商品与库存'
        if not product and plan['target']['name']:
            matches=_matching_products(plan['target']['name'])
            if len(matches)==1:
                product=matches[0];plan['target']={'sku':product['sku'],'name':product['name']};plan['summary']=f"查询商品 {product['name']}"
    elif s['intent'] in ('stock_update','price_update'):
        product=_match_product(q)
        numbers=re.findall(r'(?<![A-Z0-9])-?\d+(?:\.\d{1,2})?(?![\d.])',re.sub(r'SKU\d+','',q,flags=re.I))
        number=numbers[-1] if numbers else None
        if not product: plan['questions'].append('请说明要修改的商品名称或 SKU。')
        if not number: plan['questions'].append('请说明目标库存数量或售价。')
        if number and s['intent']=='stock_update' and ('.' in number or int(number)>999999 or int(number)<0):
            plan['questions'].append('库存必须是 0 到 999999 之间的整数。')
        if number and s['intent']=='price_update' and (float(number)<=0 or float(number)>999999.99):
            plan['questions'].append('售价必须大于 0 元且不超过 999999.99 元。')
        if product and number:
            if s['intent']=='stock_update':
                if '.' not in number and 0<=int(number)<=999999:
                    value=int(number)
                    plan['target']={'sku':product['sku'],'name':product['name'],'stock':value,'before':product['stock']}
                    plan['summary']=f"将 {product['name']}（{product['sku']}）库存从 {product['stock']} 件改为 {value} 件"
            else:
                if 0<float(number)<=999999.99:
                    cents=int(round(float(number)*100))
                    plan['target']={'sku':product['sku'],'name':product['name'],'price_cents':cents,'before':product['price_cents']}
                    plan['summary']=f"将 {product['name']}（{product['sku']}）售价从 ¥{product['price_cents']/100:.2f} 改为 ¥{cents/100:.2f}"
            plan['requires_confirmation']=bool(plan['target'])
    elif s['intent']=='ship_order':
        match=re.search(r'(?<![A-Z0-9])DD\d{8,14}(?![A-Z0-9])',q.upper())
        waybill=re.search(r'(?<![A-Z0-9])(?:SF|YT|ZTO|STO|YTO|JD|DEMO)[-]?[A-Z0-9]{5,24}(?![A-Z0-9])',q.upper())
        if not match: plan['questions'].append('请提供待发货订单号，例如 DD20261021001。')
        if match:
            order=rows('SELECT o.*,p.name AS product FROM ops_orders o JOIN products p ON p.sku=o.product_sku WHERE o.id=?',(match.group(),))
            if not order: plan['questions'].append(f'没有找到订单 {match.group()}。')
            elif order[0]['status']!='待发货': plan['questions'].append(f"订单 {match.group()} 当前状态为“{order[0]['status']}”，不能按待发货流程处理。")
            else:
                number=waybill.group() if waybill else 'DEMO-'+uuid.uuid4().hex[:8].upper()
                plan['target']={'order_id':match.group(),'waybill':number,'synthetic_waybill':not bool(waybill),'product':order[0]['product']}
                plan['summary']=f"为订单 {match.group()}（{order[0]['product']}）录入{ '演示' if not waybill else ''}运单号 {number}"
                plan['requires_confirmation']=True
    elif s['intent']=='aftersales_search':
        status=next((x for x in ['待审核','待补充材料','已完成'] if x in q), '')
        plan['target']={'status':status}
        plan['summary']=f'查询{status}售后记录' if status else '查询全部售后记录'
    else:
        plan['intent']='help';plan['summary']='我可以帮你查订单/商品库存、导出订单、预览库存或售价修改、预览发货，以及查询售后记录。'
    return {'plan':plan,'questions':plan['questions']}

def _matching_products(text):
    norm=re.sub(r'[\s\W_]+','',text.lower())
    products=rows('SELECT * FROM products')
    out=[]
    for p in products:
        if p['sku'].lower() in text.lower() or re.sub(r'[\s\W_]+','',p['name'].lower()) in norm:
            out.append(p)
        elif len(norm)>=3 and norm in re.sub(r'[\s\W_]+','',p['name'].lower()):
            out.append(p)
    return out

def _match_product(text):
    matches=_matching_products(text)
    return matches[0] if len(matches)==1 else None

def _product_hint(q):
    q=re.sub(r'(把|将|帮我|请|商品|库存|数量|改成|改为|调整到|设为|增加到|减少到|售价|价格|改价|修改|查询|查一下|查|是多少|设成|到|元|件|个|只)', ' ', q)
    q=re.sub(r'\d+(?:\.\d+)?|SKU\d+',' ',q,flags=re.I)
    return ' '.join(q.split()).strip()

builder=StateGraph(OpsState)
builder.add_node('understand',understand)
builder.add_node('build_plan',build_plan)
builder.add_edge(START,'understand')
builder.add_edge('understand','build_plan')
builder.add_edge('build_plan',END)
graph=builder.compile()

def plan_command(command):
    return graph.invoke({'command':command})['plan']
