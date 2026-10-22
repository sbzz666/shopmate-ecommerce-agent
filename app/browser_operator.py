"""Playwright executor restricted to ShopMate's local simulated merchant console."""
import os
import re
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'/'operations'
EXPORTS=DATA/'exports'
SHOTS=DATA/'screenshots'

def execute(plan, token, base_url):
    if not token or base_url.rstrip('/') not in ('http://127.0.0.1:8765','http://localhost:8765'):
        raise ValueError('浏览器执行器只允许访问本机演示后台。')
    base_url=base_url.rstrip('/')
    DATA.mkdir(parents=True,exist_ok=True);EXPORTS.mkdir(parents=True,exist_ok=True);SHOTS.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    shot_name=f'operation-{stamp}.png'
    result={'summary':plan['summary'],'intent':plan['intent'],'steps':[],'screenshot':f'/api/ops/screenshot/{shot_name}'}
    with sync_playwright() as p:
        path=os.getenv('SHOPMATE_CHROME_PATH',r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        browser=p.chromium.launch(headless=True, executable_path=path)
        context=browser.new_context(accept_downloads=True,viewport={'width':1440,'height':900})
        context.add_cookies([{'name':'shopmate_session','value':token,'url':base_url,'sameSite':'Strict','httpOnly':True}])
        page=context.new_page()
        page.goto(base_url+'/mock-admin',wait_until='domcontentloaded')
        page.get_by_text('演示店铺',exact=True).wait_for()
        page.wait_for_function('window.shopmateReady === true')
        result['steps'].append('打开本地模拟商家后台')
        intent=plan['intent'];target=plan['target']
        if intent in ('order_search','export_orders'):
            page.get_by_role('button',name='订单管理').click()
            if target.get('order_id'): page.get_by_label('搜索订单').fill(target['order_id'])
            if target.get('status'): page.get_by_label('订单状态').select_option(label=target['status'])
            if intent=='export_orders':
                with page.expect_download() as download_info:
                    page.get_by_role('button',name='导出当前订单').click()
                download=download_info.value
                file_name=f'订单导出-{stamp}.csv'
                download.save_as(EXPORTS/file_name)
                result.update({'download_file':file_name,'download_url':f'/api/ops/download/{file_name}'})
                result['steps'].append('在后台选择筛选条件并点击导出')
                result['rows']=(page.locator('#order-rows tr').count())
            else:
                page.get_by_role('button',name='查询订单').click()
                result['steps'].append('填写订单条件并点击查询')
                result['rows']=page.locator('#order-rows tr').all_inner_texts()
        elif intent in ('product_search','stock_update','price_update'):
            page.get_by_role('button',name='商品库存').click()
            if target.get('sku'):
                page.get_by_label('搜索商品').fill(target['sku'])
            page.get_by_role('button',name='查询商品').click()
            result['steps'].append('打开商品库存页并查询目标 SKU')
            if intent=='product_search': result['rows']=page.locator('#product-rows tr').all_inner_texts()
            else:
                row=page.locator(f'tr[data-sku="{target["sku"]}"]')
                if intent=='stock_update':
                    row.get_by_label('库存-'+target['sku']).fill(str(target['stock']))
                    row.get_by_role('button',name='保存库存').click()
                    result['steps'].append('填写新库存并点击保存库存')
                    page.wait_for_function("args => {const e=document.querySelector(args.selector);return e && e.getAttribute('data-current-stock')===args.expected}",{'selector':f'tr[data-sku="{target["sku"]}"]','expected':str(target['stock'])},timeout=10000)
                    actual=row.get_by_label('库存-'+target['sku']).input_value()
                    if actual!=str(target['stock']): raise RuntimeError('库存结果校验不一致')
                    result['rows']={'商品':row.inner_text(),'库存':actual}
                else:
                    row.get_by_label('售价-'+target['sku']).fill(f'{target["price_cents"]/100:.2f}')
                    row.get_by_role('button',name='保存售价').click()
                    result['steps'].append('填写新售价并点击保存售价')
                    page.wait_for_function("args => {const e=document.querySelector(args.selector);return e && e.getAttribute('data-current-price-cents')===args.expected}",{'selector':f'tr[data-sku="{target["sku"]}"]','expected':str(target['price_cents'])},timeout=10000)
                    actual=row.get_by_label('售价-'+target['sku']).input_value()
                    expected=f'{target["price_cents"]/100:.2f}'
                    if actual!=expected: raise RuntimeError('售价结果校验不一致')
                    result['rows']={'商品':row.inner_text(),'售价':actual}
        elif intent=='ship_order':
            page.get_by_role('button',name='订单管理').click()
            page.get_by_label('搜索订单').fill(target['order_id'])
            page.get_by_role('button',name='查询订单').click()
            row=page.locator(f'tr[data-order-id="{target["order_id"]}"]')
            row.get_by_label('快递单号').fill(target['waybill'])
            row.get_by_role('button',name='确认发货').click()
            result['steps'] += ['查找目标订单','填写运单号并点击确认发货']
            row.get_by_text('已发货',exact=True).wait_for(state='visible',timeout=10000)
            result['rows']=row.inner_text()
            if '已发货' not in result['rows']: raise RuntimeError('发货状态结果校验不一致')
        elif intent=='aftersales_search':
            page.get_by_role('button',name='售后管理').click()
            if target.get('status'): page.get_by_label('售后状态').select_option(label=target['status'])
            page.get_by_role('button',name='查询售后').click()
            result['steps'].append('选择售后状态并点击查询')
            result['rows']=page.locator('#after-rows tr').all_inner_texts()
        else: raise ValueError('当前任务类型暂不支持浏览器执行。')
        page.screenshot(path=str(SHOTS/shot_name),full_page=True)
        result['steps'].append('核对页面操作结果并保存执行截图')
        context.close();browser.close()
    return result
