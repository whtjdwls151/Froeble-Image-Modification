const chatEl = document.getElementById("chat");
const inputEl = document.getElementById("user-input");
const sendBtn = document.getElementById("send-btn");
const attachBtn = document.getElementById("attach-btn");
const imageInput = document.getElementById("image-input");
const maskInput = document.getElementById("mask-input");
const sizeEl = document.getElementById("image-size");
const chipsEl = document.getElementById("file-chips");

// 대화 상태 (백엔드 Responses API로 전달될 포맷 유지)
let messages = [];
let queuedFiles = []; // 이미지 첨부(여러 개)
let pasteListenerBound = false;

// --------- UI Helpers ----------
function el(tag, cls, html){
  const d = document.createElement(tag);
  if(cls) d.className = cls;
  if(html !== undefined) d.innerHTML = html;
  return d;
}
function appendMsg(role, html){
  const row = el("div", `msg msg--${role}`);
  if(role === "assistant"){
    row.appendChild(el("div","avatar","🤖"));
  }
  const bubble = el("div","bubble");
  bubble.innerHTML = html;
  row.appendChild(bubble);
  chatEl.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
  return bubble;
}
function loadingDots(){
  return `<span class="dots"><span></span><span></span><span></span></span>`;
}
function escapeHtml(s){
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function autoResizeTextarea(){
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + 'px';
}

// --------- File chips ----------
function refreshChips(){
  chipsEl.innerHTML = "";
  queuedFiles.forEach((f, i) => {
    const chip = el("div", "chip");
    const img = el("img");
    chip.appendChild(img);
    const reader = new FileReader();
    reader.onload = e => { img.src = e.target.result; };
    reader.readAsDataURL(f);

    chip.appendChild(el("span","", escapeHtml(f.name)));
    const x = el("button","", "✕");
    x.addEventListener("click", () => {
      queuedFiles.splice(i,1);
      refreshChips();
    });
    chip.appendChild(x);
    chipsEl.appendChild(chip);
  });
  sendBtn.disabled = !inputEl.value.trim() && queuedFiles.length === 0;
}

// --------- Input wiring ----------
attachBtn.addEventListener("click", ()=> imageInput.click());
imageInput.addEventListener("change", ()=>{
  for(const f of imageInput.files){
    if(f.type.startsWith("image/")) queuedFiles.push(f);
  }
  imageInput.value = "";
  refreshChips();
});

if(!pasteListenerBound){
  window.addEventListener("paste", (e)=>{
    const items = e.clipboardData?.items || [];
    for(const it of items){
      if(it.kind === "file" && it.type.startsWith("image/")){
        const f = it.getAsFile();
        queuedFiles.push(f);
      }
    }
    refreshChips();
  });
  pasteListenerBound = true;
}

["dragenter","dragover"].forEach(ev=>{
  window.addEventListener(ev, e=>{
    e.preventDefault(); document.body.classList.add("dragover");
  });
});
["dragleave","drop"].forEach(ev=>{
  window.addEventListener(ev, e=>{
    e.preventDefault(); document.body.classList.remove("dragover");
  });
});
window.addEventListener("drop", (e)=>{
  const files = e.dataTransfer?.files || [];
  for(const f of files){
    if(f.type.startsWith("image/")) queuedFiles.push(f);
  }
  refreshChips();
});

// textarea behaviors
inputEl.addEventListener("input", ()=>{
  autoResizeTextarea();
  sendBtn.disabled = !inputEl.value.trim() && queuedFiles.length === 0;
});
inputEl.addEventListener("keydown", (e)=>{
  if(e.key === "Enter" && !e.shiftKey){
    e.preventDefault();
    onSend();
  }
});
sendBtn.addEventListener("click", onSend);

// --------- Networking ----------
async function onSend(){
  const text = inputEl.value.trim();
  if(!text && queuedFiles.length === 0) return;

  // 유저 메시지 표시
  let userHTML = "";
  if(text) userHTML += `<div>${escapeHtml(text)}</div>`;
  if(queuedFiles.length){
    const thumbs = queuedFiles.map(()=>`<div style="width:64px;height:64px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:8px;"></div>`).join("");
    userHTML += `<div style="display:flex; gap:8px; margin-top:8px;">${thumbs}</div>`;
  }
  appendMsg("user", userHTML);

  // 입력 초기화
  inputEl.value = ""; autoResizeTextarea();
  chipsEl.innerHTML = "";
  const filesToSend = queuedFiles.slice();
  queuedFiles = [];
  sendBtn.disabled = true;

  // 로딩 표시
  const loaderBubble = appendMsg("assistant", loadingDots());

  try{
    let res, data;
    if(filesToSend.length){ // 이미지 생성/편집
      const form = new FormData();
      form.append("prompt", text || "");
      form.append("size", sizeEl.value);
      // 편집 시 첫 번째 이미지를 이미지로 보냄 (필요하면 마스크 확장 가능)
      form.append("image", filesToSend[0]);
      // form.append("mask", somePngMaskFile);
      res = await fetch("/api/image", { method: "POST", body: form });
      data = await res.json();
      if(!data.ok) throw new Error(data.error || "이미지 처리 실패");
      loaderBubble.innerHTML = `<img src="${data.content}" alt="result" />`;
      // history(옵션)
      messages.push({ role:"user", content:[{type:"text", text:text}]});
      messages.push({ role:"assistant", content:[{type:"text", text:"[이미지 응답]"}]});
    }else{ // 텍스트 대화
      messages.push({ role:"user", content:[{type:"text", text:text}]});
      res = await fetch("/api/chat", {
        method:"POST",
        headers:{ "Content-Type": "application/json" },
        body: JSON.stringify({ messages })
      });
      data = await res.json();
      if(!data.ok) throw new Error(data.error || "대화 실패");
      loaderBubble.textContent = data.content || "";
      messages.push({ role:"assistant", content:[{type:"text", text:data.content || ""}]});
    }
  }catch(err){
    loaderBubble.innerHTML = `에러: ${escapeHtml(String(err.message || err))}`;
  }finally{
    sendBtn.disabled = false;
  }
}
