const $=s=>document.querySelector(s);
let localUrls=[];

$('#videos').onchange=()=>{
 localUrls.forEach(URL.revokeObjectURL);localUrls=[];
 const files=[...$('#videos').files];
 if(!files.length){$('#results').innerHTML='<div class="empty">휴대폰에서 영상 파일을 선택하세요.</div>';return}
 $('#results').innerHTML=files.map((f,i)=>{const u=URL.createObjectURL(f);localUrls.push(u);return `<article class="scene"><div class="sentence">${i+1}. ${esc(f.name)}</div><video src="${u}" controls playsinline preload="metadata" style="width:100%;max-height:260px;background:#000;border-radius:10px"></video></article>`}).join('');
 $('#status').textContent=`원본 영상 ${files.length}개 선택됨`;
};

$('#auto').onclick=async()=>{
 const b=$('#auto'),script=$('#script').value.trim(),files=[...$('#videos').files];
 if(!script)return alert('대본을 입력하세요.');if(!files.length)return alert('원본 영상을 한 개 이상 선택하세요.');
 b.disabled=true;$('#download').style.display='none';$('#preview').style.display='none';$('#previewEmpty').style.display='block';$('#bar').style.width='3%';$('#status').textContent=`원본 영상 ${files.length}개 업로드 중`;
 try{
  const form=new FormData();files.forEach(f=>form.append('files',f));
  const ur=await fetch('/api/upload',{method:'POST',body:form});const ud=await ur.json();if(!ur.ok)throw Error(ud.detail||'영상 업로드 실패');
  $('#bar').style.width='12%';$('#status').textContent='대본 장면에 영상을 자동 배치하는 중';
  const rr=await fetch('/api/render-uploaded',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({script,video_tokens:ud.files.map(f=>f.token),voice:$('#voice').value})});
  const rd=await rr.json();if(!rr.ok)throw Error(rd.detail||'렌더링 시작 실패');poll(rd.job_id);
 }catch(e){$('#status').textContent=e.message;b.disabled=false}
};

async function poll(id){try{const r=await fetch('/api/jobs/'+id),d=await r.json();$('#bar').style.width=(d.progress||0)+'%';$('#status').textContent=d.message;if(d.status==='done'){const a=$('#download'),p=$('#preview');a.href=d.download;a.style.display='block';p.src=d.download;p.style.display='block';$('#previewEmpty').style.display='none';p.load();$('#auto').disabled=false;return}if(d.status==='error'){$('#auto').disabled=false;return}setTimeout(()=>poll(id),1500)}catch(e){$('#status').textContent=e.message;$('#auto').disabled=false}}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
