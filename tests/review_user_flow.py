import asyncio, json, tempfile
from pathlib import Path
from unittest.mock import patch
import httpx
from app import auth, db, jobs, main, pipeline

async def flow():
 with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', str(Path(tmp)/'review.db')):
  db.init()
  uid=db.run("INSERT INTO users (email) VALUES ('review@example.com')")
  db.run("INSERT INTO sessions (token,user_id) VALUES ('review',?)",uid)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test',cookies={auth.COOKIE:'review'}) as client:
   with patch.object(pipeline,'classify_topic',return_value={'ok':True,'name':'体验评审','focus':'','risk':'none','category':'通识'}):
    r=await client.post('/api/domains',json={'topic':'体验评审','goal':'系统了解'})
   assert r.status_code==200
   did=int(r.json()['url'].split('/')[-1])
   assert (await client.get(r.json()['url'])).status_code==200
   cids=[]
   for i in range(9):
    cid=db.run('INSERT INTO concepts (name,key) VALUES (?,?)',f'评审知识{i}',f'review{i}')
    db.run('INSERT INTO domain_nodes (domain_id,concept_id,stage,ord) VALUES (?,?,?,?)',did,cid,i//3,i)
    cids.append(cid)
   db.run("UPDATE domains SET status='ready' WHERE id=?",did)
   jobs.prefetch_first_pages(did)
   for label in pipeline.LABELS:
    assert (await client.post(f'/api/domains/{did}/level',json={'label':label})).status_code==200
   qs=[{'stage':i//3,'node':f'评审知识{i}','concept_id':cid,'q':f'评审题{i}','options':['正确','错误1','错误2','错误3'],'answer':0,'why':'测试解释'} for i,cid in enumerate(cids)]
   db.run("INSERT INTO diagnostics (domain_id,status,data) VALUES (?,'ready',?)",did,json.dumps({'questions':qs}))
   assert (await client.get(f'/d/{did}/diagnose')).status_code==200
   r=await client.post(f'/api/domains/{did}/diagnose',json={'answers':[0,0,0,-1,-1,-1,-1,-1,-1]})
   assert r.json()['tier']=='进阶' and r.json()['known']==3
   p=jobs.ensure_page(did,cids[3],'进阶',uid)
   data=json.loads((main.ROOT/'fixtures/递归.json').read_text())
   db.run("UPDATE pages SET status='ready',data=? WHERE id=?",json.dumps(data),p['id'])
   assert (await client.get(f"/p/{p['id']}")).status_code==200
   r=await client.post(f"/api/pages/{p['id']}/progress",json={'score':6})
   assert r.json()['next']['url']==f'/d/{did}/n/{cids[4]}'
   r=await client.post(f"/api/pages/{p['id']}/cards",json={'idx':'0-0','rating':'good'})
   assert r.status_code==200
   card=db.one('SELECT * FROM cards')
   db.run("UPDATE cards SET due_at='2000-01-01' WHERE id=?",card['id'])
   assert (await client.get('/review')).status_code==200
   assert (await client.post(f"/api/cards/{card['id']}/review",json={'rating':'good'})).json()['interval_days']>2
   with patch.object(main,'chat_json',return_value={'score':90,'feedback':'讲清楚了','points':[],'errors':[]}):
    assert (await client.post(f"/api/pages/{p['id']}/feynman",json={'text':'这是一次模拟用户对知识点的讲解，需要至少二十个汉字以验证流程。'})).status_code==200
   with patch.object(main,'chat',return_value='先说说你的理解？'):
    assert (await client.post(f"/api/pages/{p['id']}/tutor",json={'messages':[{'role':'user','content':'这个原理如何应用？'}]})).status_code==200
   assert (await client.post(f"/api/pages/{p['id']}/feedback",json={'text':'第一块的例子不清楚'})).status_code==200
   print('PASS 创建领域→等待→六档自选→九题诊断→学习→闯关→下一页→卡片→复习→费曼→问老师→反馈 (临时DB，模拟模型)')
asyncio.run(flow())
