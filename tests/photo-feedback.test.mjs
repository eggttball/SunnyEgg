import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import http from 'node:http';
import { createGalleryServer } from '../server.mjs';

async function fixture(t, photoFeedbackRunner) {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-feedback-http-'));
  await mkdir(join(root, 'data/media/local'), { recursive: true });
  await writeFile(join(root, 'data/media/local/photo.jpg'), Buffer.from([255,216,255,1,2,3]));
  await writeFile(join(root, 'data/manifest.json'), JSON.stringify({version:1,posts:[{
    id:'post-1',kind:'photos',assets:[{id:'asset-1',type:'image',path:'media/local/photo.jpg'}],
  }]}));
  const server=createGalleryServer({root,photoFeedbackRunner});
  server.listen(0,'127.0.0.1');await once(server,'listening');
  t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await rm(root,{recursive:true,force:true});});
  const base=`http://127.0.0.1:${server.address().port}`;
  const request=(path,options={})=>new Promise((resolve,reject)=>{
    const req=http.request(base+path,{method:options.method||'GET',headers:options.headers},res=>{
      const chunks=[];res.on('data',chunk=>chunks.push(chunk));res.on('end',()=>resolve({status:res.statusCode,body:Buffer.concat(chunks).toString()}));
    });req.on('error',reject);req.end(options.body);
  });
  const post=(input,headers={})=>request('/api/photo-feedback',{method:'POST',headers:{Origin:base,'Content-Type':'application/json',...headers},body:typeof input==='string'?input:JSON.stringify(input)});
  return {root,request,post,base};
}

test('photo feedback persists through the real local helper and is reversible',async t=>{
  const {root,request,post}=await fixture(t);
  const input={postId:'post-1',assetId:'asset-1',excluded:true};
  const response=await post(input);
  assert.equal(response.status,200);
  const result=JSON.parse(response.body);
  assert.deepEqual({postId:result.postId,assetId:result.assetId,excluded:result.excluded},input);
  const gallery=JSON.parse((await request('/api/gallery')).body);
  assert.equal(gallery.posts[0].assets[0].userExcluded,true);
  assert.equal((await post({...input,excluded:false})).status,200);
  const restored=JSON.parse((await request('/api/gallery')).body);
  assert.equal(restored.posts[0].assets[0].userExcluded,false);
  assert.equal((await readFile(join(root,'data/media/local/photo.jpg'))).length,6);
  assert.equal((await request('/data/feedback.json')).status,404);
  const feedback=JSON.parse(await readFile(join(root,'data/feedback.json'),'utf8'));
  assert.equal((await request('/'+feedback.examples['asset-1'].path)).status,404);
  assert.doesNotMatch(response.body,/imageHash|negative-examples|features|vectors|\/Users\//);
});

test('feedback rejects cross-origin and malformed requests before invoking the runner',async t=>{
  let calls=0;
  const {post}=await fixture(t,async input=>{calls++;return input;});
  const input={postId:'post-1',assetId:'asset-1',excluded:true};
  assert.equal((await post(input,{Origin:'https://evil.example'})).status,403);
  assert.equal((await post(input,{'Content-Type':'text/plain'})).status,415);
  assert.equal((await post({...input,assetId:'../secret'})).status,400);
  assert.equal((await post({...input,excluded:1})).status,400);
  assert.equal((await post({...input,path:'/private/secret'})).status,400);
  assert.equal((await post('{ invalid')).status,400);
  assert.equal((await post(' '.repeat(8193))).status,413);
  assert.equal(calls,0);
});

test('feedback cannot exclude an unrelated asset and never exposes raw runner errors',async t=>{
  const {post}=await fixture(t);
  assert.equal((await post({postId:'post-1',assetId:'unknown',excluded:true})).status,404);
  const broken=await fixture(t,async()=>{throw new Error('private-secret /Users/private/profile');});
  const response=await broken.post({postId:'post-1',assetId:'asset-1',excluded:true});
  assert.equal(response.status,503);
  assert.doesNotMatch(response.body,/private-secret|\/Users\//);
});
