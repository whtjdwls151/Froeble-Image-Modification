// ======================== State / Utils ========================
const $ = s => document.querySelector(s);
const app = $("#app");

let state = {
  view: "projects",   // "projects" | "editor"
  projects: [],
  current: null,      // { slug, meta, illustrations: [...] }
  currentLabel: null, // "A", ...
  editBaseVersion: null, // "__ORIGINAL__" | "A-2.png" | null
  generating: false,
  detailImage: null,
  chatCache: {},       // { "<slug>|<label>": [{baseUrl,prompt,outUrl,outFile}] }
  chatInitTs: {}
};

async function jget(url){ const r=await fetch(url); return r.json(); }
async function jpost(url, body){
  const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  return r.json();
}
async function jpostForm(url, form){ const r=await fetch(url,{method:"POST",body:form}); return r.json(); }
async function jdel(url){ const r=await fetch(url,{method:"DELETE"}); return r.json(); }

function escapeHtml(s){ return (s||"").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;","&gt;":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

function scrollChatMsgToTop(msgEl){
  const chat = $("#chat"); if(!chat || !msgEl) return;
  const targetTop = msgEl.offsetTop - chat.offsetTop;
  chat.scrollTop = targetTop;
}
function scrollChatToBottom(){
  const chat = $("#chat"); if(chat) chat.scrollTop = chat.scrollHeight;
}

// ======================== Projects View ========================
async function loadProjects(){
  const res = await jget("/api/projects");
  if(res.ok) state.projects = res.projects;
  render();
}

function render(){
  if(state.view==="projects") renderProjects();
  else openEditor(state.current?.slug || state.projects[0]?.slug);
}

function renderProjects(){
  app.innerHTML = `
    <div class="container">
      <div class="section">
        <h2>프로젝트 만들기</h2>
        <div style="display:flex;gap:8px;align-items:center;">
          <input id="new-name" type="text" placeholder="새 프로젝트 이름" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"/>
          <button class="btn primary" id="btn-create">생성</button>
          <button class="btn" id="btn-reload">새로고침</button>
        </div>
      </div>
      <div class="section">
        <h2>프로젝트 리스트</h2>
        <div class="project-list" id="project-list"></div>
      </div>
    </div>

    <!-- 📌 공용 모달: 프로젝트 화면에도 포함 -->
    <div class="modal hidden" id="modal">
      <div class="modal__backdrop"></div>
      <div class="modal__panel">
        <button id="modal-close" class="modal__close">✕</button>
        <img id="modal-img" src="" alt="detail"/>
        <div class="modal__footer">
          <button class="btn" id="modal-back">돌아가기</button>
          <a class="btn primary" id="modal-download" href="#" download>다운로드</a>
        </div>
      </div>
    </div>
  `;

  $("#btn-create").onclick = async ()=>{
    const name = $("#new-name").value.trim();
    if(!name) return alert("이름을 입력하세요.");
    const r = await jpost("/api/projects", {name});
    if(!r.ok) return alert(r.error||"에러");
    await loadProjects();
  };
  $("#btn-reload").onclick = loadProjects;

  const list = $("#project-list");
  list.innerHTML = "";
  state.projects.forEach(p=>{
    const div = document.createElement("div");
    div.className="project-card";
    div.innerHTML = `
      <div class="project-head">
        <div>
          <div style="font-weight:700">${escapeHtml(p.name)}</div>
          <div class="project-meta">생성: ${p.created_at||"-"} · 수정: ${p.updated_at||"-"} · 삽화: ${p.illustration_count}</div>
        </div>
        <div style="display:flex;gap:6px;">
          <button class="btn danger" data-del="${p.slug}">삭제</button>
          <button class="btn" data-rename="${p.slug}">이름변경</button>
          <button class="btn" data-zip="${p.slug}">전체 삽화 다운로드</button>
          <button class="btn primary" data-open="${p.slug}">열기</button>
        </div>
      </div>
      <div class="preview-row">
        ${p.previews.map(u=>`<div class="preview"><img data-detail="${u}" src="${u}"/></div>`).join("")}
      </div>
    `;
    list.appendChild(div);
  });

  list.querySelectorAll("[data-open]").forEach(b=>b.onclick=()=>openEditor(b.dataset.open));
  list.querySelectorAll("[data-rename]").forEach(b=>b.onclick=async()=>{
    const name = prompt("새 이름을 입력하세요:"); if(!name) return;
    const r = await jpost(`/api/projects/${b.dataset.rename}/rename`,{name});
    if(!r.ok) return alert(r.error||"에러");
    await loadProjects();
  });
  list.querySelectorAll("[data-del]").forEach(b=>b.onclick=async()=>{
    if(!confirm("정말 삭제할까요?")) return;
    const r = await jdel(`/api/projects/${b.dataset.del}`);
    if(!r.ok) return alert(r.error||"에러");
    await loadProjects();
  });
  list.querySelectorAll("[data-zip]").forEach(b=>{
    b.onclick = ()=>{ window.location.href = `/api/projects/${b.dataset.zip}/download_selected_numbered`; };
  });

  // 👉 미리보기 이미지 클릭 = 상세보기
  list.querySelectorAll("[data-detail]").forEach(img=>img.onclick=()=>openDetail({url:img.dataset.detail,name:"preview.png"}));

  // 👉 프로젝트 화면에서도 모달 닫기 동작 바인딩
  $("#modal-close").onclick = closeDetail;
  $("#modal-back").onclick = closeDetail;
  $("#modal").querySelector(".modal__backdrop").onclick = closeDetail;

  // 👉 프로젝트 화면에서도 이미지 클릭/채팅점프 델리게이션 활성화
  document.addEventListener("click", delegatedClicks, true);
}

// ======================== Editor View ========================
async function openEditor(slug){
  const res = await jget(`/api/projects/${slug}`);
  if(!res.ok) return alert(res.error||"에러");
  state.current = { slug, meta: res.meta, illustrations: res.illustrations };
  state.currentLabel = res.illustrations[0]?.label || null;
  state.view="editor"; state.editBaseVersion=null; state.generating=false;

  app.innerHTML = `
    <div class="editor-full">
      <div class="left-col">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div>
            <div style="font-weight:700" id="proj-name"></div>
            <div class="project-meta" id="proj-meta"></div>
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn" id="btn-back">← 목록</button>
            <button class="btn primary" id="btn-download">전체 삽화 다운로드</button>
          </div>
        </div>

        <div style="font-weight:700;margin-bottom:8px;font-size:20px">채팅</div>
        <div id="chat" class="chat-list"></div>

        <div class="composer">
          <div id="base-thumb-wrap" class="composer-thumb"></div>
          <div class="composer-input">
            <textarea id="prompt" placeholder="수정 사항을 적어주세요. (Enter=전송, Shift+Enter=줄바꿈)"></textarea>
          </div>
          <button class="btn primary" id="btn-send">수정 요청</button>
        </div>

        <div class="gen-overlay" id="gen-overlay" style="display:none"><div class="spinner"></div> 이미지 생성 중...</div>
      </div>

      <div class="right-col">
        <div class="illus-strip">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <div style="font-weight:700;font-size:20px">삽화 선택</div>
            <div style="display:flex;gap:8px">
              <label class="btn">삽화 추가
                <input id="add-files" type="file" multiple accept="image/*" style="display:none"/>
              </label>
              <button class="btn danger" id="btn-del-illus">삽화 삭제</button>
            </div>
          </div>
          <div class="illus-row" id="illus-row"></div>
        </div>

        <div class="history-grid">
          <div style="font-weight:700;margin-bottom:8px;font-size:20px">수정 기록 (<span id="hist-count">0</span>)</div>
          <div class="grid" id="history-grid"></div>
        </div>
      </div>
    </div>

    <!-- 📌 에디터 화면에도 동일 모달 포함 -->
    <div class="modal hidden" id="modal">
      <div class="modal__backdrop"></div>
      <div class="modal__panel">
        <button id="modal-close" class="modal__close">✕</button>
        <img id="modal-img" src="" alt="detail"/>
        <div class="modal__footer">
          <button class="btn" id="modal-back">돌아가기</button>
          <a class="btn primary" id="modal-download" href="#" download>다운로드</a>
        </div>
      </div>
    </div>
  `;

  $("#btn-back").onclick = ()=>{ state.view="projects"; loadProjects(); };
 // numbered 라우트 사용 권장
  $("#btn-download").onclick = ()=>{
    window.location.href = `/api/projects/${state.current.slug}/download_selected_numbered`;
  };

  $("#add-files").onchange = onAddFiles;
  $("#btn-del-illus").onclick = onDeleteIllustration;
  $("#prompt").addEventListener("keydown", e=>{
    if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); $("#btn-send").click(); }
  });
  $("#btn-send").onclick = onSendPrompt;

  // 모달 닫기
  $("#modal-close").onclick = closeDetail;
  $("#modal-back").onclick = closeDetail;
  $("#modal").querySelector(".modal__backdrop").onclick = closeDetail;

  // 델리게이션
  document.addEventListener("click", delegatedClicks, true);

  paintProjectHeader();
  paintComposerBase();
  await refreshChatDataForCurrent();
  await paintPanelsForCurrent();
  scrollChatToBottom();
}

function paintProjectHeader(){
  $("#proj-name").textContent = state.current.meta.name;
  $("#proj-meta").textContent = `생성: ${state.current.meta.created_at} · 수정: ${state.current.meta.updated_at} · 현재: ${state.currentLabel||"-"}`;
}

// ======================== Data (chat log) ========================
async function refreshChatDataForCurrent(){
  const proj = state.current;
  const ill = proj.illustrations.find(i=>i.label===state.currentLabel);
  if(!ill) return;
  const key = `${proj.slug}|${ill.label}`;
  if(ill.chat_log_url){
    try{
      const txt = await (await fetch(ill.chat_log_url)).text();
      const { items, initTs } = parseChatLog(txt, proj.slug, ill.label);
      state.chatCache[key]  = items;
      state.chatInitTs[key] = initTs || null;
    }catch(e){ state.chatCache[key] = []; state.chatInitTs[key] = null; }
  }else{
    state.chatCache[key] = [];
    state.chatInitTs[key] = null;
  }
}

function formatTs(ts){
  if(!ts) return "";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts; // 파싱 실패 시 원문
  const pad = n => String(n).padStart(2,"0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function parseChatLog(text, slug, label){
  const lines = text.split(/\r?\n/);
  const items = [];
  let cur = null;
  let initTs = null;

  const headerRE = /^\[([^\]]+)\]\s\[(USER|MODEL|INIT|SELECT|MODEL:TEXT)\]/;
  const isHeader = (s) => headerRE.test(s || "");

  for (let i = 0; i < lines.length; i++){
    const line = lines[i];
    if (!line) continue;

    const h = line.match(headerRE);
    const ts = h ? h[1] : null;
    const kind = h ? h[2] : null;

    if (kind === "INIT"){
      // 최초 업로드 시각 저장 (여러 번 있을 수 있지만 첫 번째를 사용)
      if (!initTs) initTs = ts || null;
      continue;
    }

    if (kind === "USER"){
      const base = line.match(/base=([^\s]+)\s/);
      let promptPart = "";
      const idx = line.indexOf("prompt=");
      if (idx >= 0) promptPart = line.slice(idx + "prompt=".length);

      // 다음 헤더 전까지 멀티라인 프롬프트 이어붙이기
      let j = i + 1;
      while (j < lines.length && !isHeader(lines[j])) {
        promptPart += (promptPart ? "\n" : "") + lines[j];
        j++;
      }
      i = j - 1;

      cur = {
        userTs: ts || null,
        modelTs: null,
        baseUrl: base ? `/files${base[1]}` : "",
        prompt: (promptPart || "").trim(),
        outUrl: "",
        outFile: ""
      };
      continue;
    }

    if (kind === "MODEL" && line.includes("out=") && cur){
      const out = line.match(/out=([^\s]+)\s*$/);
      if (out) {
        cur.outUrl  = `/files${out[1]}`;
        cur.outFile = cur.outUrl.split("/").pop() || "";
        cur.modelTs = ts || null;
        items.push(cur);
        cur = null;
      }
      continue;
    }
  }

  return { items, initTs };
}



function deriveVersionLabelFromBase(baseUrl, label){
  if(!baseUrl) return `${label}-0`;
  if(baseUrl.endsWith("original.png")) return `${label}-0`;
  const f = baseUrl.split("/").pop() || "";
  return f.replace(".png","");
}

// ======================== Paint (Chat / History / Strip) ========================
async function paintPanelsForCurrent(){
  paintChat();
  paintHistory();
  paintIllusStrip();
  paintComposerBase();
}

function currentIllustration(){
  return state.current.illustrations.find(i=>i.label===state.currentLabel);
}

function buildChatHTML(ill, slug){
  const key = `${state.current.slug}|${ill.label}`;
  const items = state.chatCache[key] || [];
  const initTs = state.chatInitTs[key] || null;
  let html = "";

  // --- Original (A-0) ---
  if(ill.original_url){
    const id = `${ill.label}-0`;
    const isSel = !ill.selected || ill.selected==="__ORIGINAL__";
    html += `
      <div class="chat-msg assistant" id="msg-${id}">
        <div class="bubble">
          <div class="msg-img ${isSel?'is-selected':''}" data-version="${id}">
            <img data-detail="${ill.original_url}" data-name="${id}.png" src="${ill.original_url}" alt="original"/>
            <div class="badge">${id}</div>
            <div class="badge heart">♥</div>
            <div class="action-heart" data-select="__ORIGINAL__" data-select-label="${ill.label}">♥</div>
            <div class="action-bar">
              <span class="action-pill" data-edit-base="__ORIGINAL__">이 버전 수정</span>
            </div>
          </div>
          <div class="meta-stamp">${formatTs(initTs)}</div>
        </div>
      </div>
    `;
  }

  // --- Generated sequences ---
  items.forEach(it=>{
    const file = it.outFile;
    const name = file.replace(".png","");
    const isSel = (file===ill.selected);
    const usedBase = deriveVersionLabelFromBase(it.baseUrl, ill.label);

    // [USER] base thumbnail (128px)
    html += `
      <div class="chat-msg user" id="msg-${name}-req-base">
        <div class="bubble">
          <div class="mini-img" style="position:relative;display:inline-block;">
            <img class="base-thumb" src="${it.baseUrl}" alt="${usedBase}" data-chat-jump="${usedBase}"/>
            <div class="badge" style="position:absolute;top:8px;left:8px">${usedBase}</div>
          </div>
          <div class="meta-stamp">${formatTs(it.userTs)}</div>
        </div>
      </div>
    `;

    // [USER] prompt
    html += `
      <div class="chat-msg user" id="msg-${name}-req">
        <div class="bubble">
          <div style="white-space:pre-wrap">${escapeHtml(it.prompt||"")}</div>
          <div class="meta-stamp">${formatTs(it.userTs)}</div>
        </div>
      </div>
    `;

    // [ASSISTANT] generated image
    html += `
      <div class="chat-msg assistant" id="msg-${name}">
        <div class="bubble">
          <div class="msg-img ${isSel?'is-selected':''}" data-version="${name}">
            <img data-detail="${it.outUrl}" data-name="${file}" src="${it.outUrl}"/>
            <div class="badge">${name}</div>
            <div class="badge heart">♥</div>
            <div class="action-heart" data-select="${file}" data-select-label="${ill.label}">♥</div>
            <div class="action-bar">
              <span class="action-pill" data-edit-base="${file}">이 버전 수정</span>
            </div>
          </div>
          <div class="meta-stamp">${formatTs(it.modelTs)}</div>
        </div>
      </div>
    `;
  });

  return html || `<div class="chat-msg assistant"><div class="bubble">아직 생성된 이미지가 없습니다. 프롬프트를 입력해보세요.</div></div>`;
}


function paintChat(){
  const ill = currentIllustration(); if(!ill) return;
  $("#chat").innerHTML = buildChatHTML(ill, state.current.slug);

  // 이미지 클릭 == 상세보기
  $("#chat").querySelectorAll("[data-detail]").forEach(el=> el.onclick = ()=> openDetail({url:el.dataset.detail,name:el.dataset.name||"image.png"}));
  // 최종선택 하트
  $("#chat").querySelectorAll("[data-select]").forEach(el=> attachSelectHandler(el, state.current.slug));
  // 이 버전 수정
  $("#chat").querySelectorAll("[data-edit-base]").forEach(el=>{
    el.onclick = ()=>{
      state.editBaseVersion = el.dataset.editBase || null;
      paintComposerBase();
      $("#prompt").focus();
    };
  });
  // 베이스 썸네일 클릭 -> 해당 채팅으로 점프
  $("#chat").querySelectorAll("[data-chat-jump]").forEach(el=>{
    el.onclick = ()=>{
      const id = el.dataset.chatJump;
      const target = document.getElementById(`msg-${id}`);
      if(target){ scrollChatMsgToTop(target); target.style.outline="2px solid #10a37f"; setTimeout(()=>target.style.outline="",1200); }
    };
  });
}

function historyCount(ill){ return (ill ? (ill.original_url?1:0) + (ill.version_count||0) : 0); }

function buildHistoryHTML(ill, slug){
  let cells = "";
  const cellTmpl = (label, name, url, isSel) => `
    <div class="cell ${isSel?'is-selected':''}" data-version="${name}">
      <img data-detail="${url}" data-name="${name}.png" src="${url}"/>
      <div class="heart">♥</div>
      <div class="heart-btn" data-select="${name.endsWith('-0')?'__ORIGINAL__':name+'.png'}" data-select-label="${label}">♥</div>
      <div class="overlay">
        <span class="action-pill" data-chat-jump="${name}">채팅 이동</span>
      </div>
      <div class="label">${name}</div>
    </div>
  `;

  if(ill.original_url){
    const name = `${ill.label}-0`;
    const isSel = !ill.selected || ill.selected==="__ORIGINAL__";
    cells += cellTmpl(ill.label, name, ill.original_url, isSel);
  }

  (ill.version_files||[]).forEach(v=>{
    const url = `/files/projects/${slug}/illustrations/${ill.label}/versions/${v}`;
    const isSel = (v===ill.selected);
    const name = v.replace(".png","");
    cells += cellTmpl(ill.label, name, url, isSel);
  });

  return cells;
}

function paintHistory(){
  const ill = currentIllustration(); if(!ill) return;
  $("#hist-count").textContent = historyCount(ill);
  $("#history-grid").innerHTML = buildHistoryHTML(ill, state.current.slug);

  // 이미지 클릭 == 상세보기
  $("#history-grid").querySelectorAll("[data-detail]").forEach(el=> el.onclick = ()=> openDetail({url:el.dataset.detail,name:el.dataset.name||"image.png"}));
  // 최종선택 하트
  $("#history-grid").querySelectorAll("[data-select]").forEach(el=> attachSelectHandler(el, state.current.slug));
  // 채팅 이동
  $("#history-grid").querySelectorAll("[data-chat-jump]").forEach(el=>{
    el.onclick = ()=>{
      const id = el.dataset.chatJump;
      const target = document.getElementById(`msg-${id}`);
      if(target){ scrollChatMsgToTop(target); target.style.outline="2px solid #10a37f"; setTimeout(()=>target.style.outline="",1200); }
    };
  });
}

function paintIllusStrip(){
  const row = $("#illus-row");
  row.innerHTML = state.current.illustrations.map(it=>{
    const thumb = it.selected_url || it.original_url || "";
    const count = (it.original_url?1:0)+(it.version_count||0);
    return `
      <div class="illus-card ${it.label===state.currentLabel?"active":""}">
        <img data-pick-illus="${it.label}" src="${thumb}"/>
        <div class="label">${it.label}</div>
        <div class="count">${count}</div>
      </div>
    `;
  }).join("");

  row.querySelectorAll("[data-pick-illus]").forEach(el=>{
    el.onclick = async ()=>{
      state.currentLabel = el.dataset.pickIllus;
      state.editBaseVersion = null;
      await refreshChatDataForCurrent();
      paintProjectHeader();
      paintChat();
      paintHistory();
      paintIllusStrip();
      paintComposerBase();
      scrollChatToBottom();
    };
  });
}

function getCurrentBaseSrc(ill, slug){
  // 1) "이 버전 수정"을 눌러 1회성으로 지정된 경우
  if (state.editBaseVersion) {
    return state.editBaseVersion === "__ORIGINAL__"
      ? (ill.original_url || "")
      : `/files/projects/${slug}/illustrations/${ill.label}/versions/${state.editBaseVersion}`;
  }

  // 2) 그렇지 않으면 최신 생성본(가장 마지막 버전 파일명) → 없으면 original
  const list = Array.isArray(ill.version_files) ? ill.version_files.slice() : [];
  // server가 정렬해주지만, 방어적으로 숫자 기준 정렬
  list.sort((a,b)=>{
    const na = parseInt((a.match(/-(\d+)\.png$/)||[])[1]||"0",10);
    const nb = parseInt((b.match(/-(\d+)\.png$/)||[])[1]||"0",10);
    return na-nb;
  });
  if (list.length > 0) {
    const last = list[list.length - 1]; // ex) "A-4.png"
    return `/files/projects/${slug}/illustrations/${ill.label}/versions/${last}`;
  }
  return ill.original_url || "";
}

function paintComposerBase(){
  const ill = currentIllustration(); if(!ill) return;
  const wrap = document.getElementById("base-thumb-wrap");
  if(!wrap) return;

  // 어떤 이미지를 "원본(베이스)"로 사용할지
  let src = getCurrentBaseSrc(ill, state.current.slug);
  if(!src || src === "") {
    // 폴백: 원본이 있으면 원본으로
    src = ill.original_url || "";
  }
  if(!src){
    wrap.innerHTML = ""; // 보여줄 것이 없음
    return;
  }

  // 라벨(A-4 등)
  let labelText = "";
  if(state.editBaseVersion){
    labelText = (state.editBaseVersion==="__ORIGINAL__")
      ? `${ill.label}-0`
      : state.editBaseVersion.replace(".png","");
  }else{
    const n = Number(ill.version_count||0);
    labelText = n>0 ? `${ill.label}-${n}` : `${ill.label}-0`;
  }

  wrap.innerHTML = `
    <div style="position:relative;display:inline-block;">
      <img src="${src}" alt="base-preview"/>
      <div class="badge">${labelText}</div>
    </div>
  `;
}

// ======================== Actions ========================
function showGenerating(on){
  state.generating = !!on;
  const ov = $("#gen-overlay"); if(ov) ov.style.display = on ? "flex" : "none";
  const ta = $("#prompt"); if(ta) ta.disabled = !!on;
  const btn = $("#btn-send"); if(btn) btn.disabled = !!on;
}

async function onAddFiles(e){
  const files = Array.from(e.target.files||[]);
  if(!files.length) return;
  const form = new FormData();
  files.forEach(f=>form.append("images", f));
  const r = await jpostForm(`/api/projects/${state.current.slug}/illustrations`, form);
  if(!r.ok) return alert(r.error||"에러");

  const res = await jget(`/api/projects/${state.current.slug}`);
  if(res.ok){ state.current.illustrations = res.illustrations; }
  if(!state.currentLabel && res.illustrations[0]) state.currentLabel = res.illustrations[0].label;

  await refreshChatDataForCurrent();
  paintPanelsForCurrent();
}

async function onDeleteIllustration(){
  const ill = currentIllustration(); if(!ill) return;
  if(!confirm("이 삽화의 모든 기록/이미지가 삭제됩니다. 계속할까요?")) return;
  const r = await jdel(`/api/projects/${state.current.slug}/illustrations/${ill.label}`);
  if(!r.ok) return alert(r.error||"에러");

  const res = await jget(`/api/projects/${state.current.slug}`);
  if(res.ok){ state.current.illustrations = res.illustrations; }
  state.currentLabel = state.current.illustrations[0]?.label || null;

  await refreshChatDataForCurrent();
  paintPanelsForCurrent();
}

async function onSendPrompt(){
  const ill = currentIllustration(); if(!ill) return alert("삽화를 먼저 추가하세요.");
  const text = $("#prompt").value.trim(); if(!text) return;
  if(state.generating) return;

  $("#prompt").value = ""; // 입력 즉시 비우기

  const form = new FormData();
  form.append("label", ill.label);
  form.append("prompt", text);
  if(state.editBaseVersion!==null && state.editBaseVersion!=="") {
    form.append("base_version", state.editBaseVersion); // ← 1회성 기반 버전
  }

  showGenerating(true);
  const r = await jpostForm(`/api/projects/${state.current.slug}/edit`, form);
  showGenerating(false);

  if(!r.ok){ alert(r.error||"에러"); return; }

  // ✅ 여기서 바로 1회성 리셋 (이후부터는 최신본 기준)
  state.editBaseVersion = null;

  // 최신 프로젝트/삽화 메타 동기화
  const res = await jget(`/api/projects/${state.current.slug}`);
  if(res.ok){
    state.current.meta = res.meta;
    const updated = res.illustrations.find(x=>x.label===ill.label);
    const arr = state.current.illustrations;
    const idx = arr.findIndex(x=>x.label===ill.label);
    if(idx>=0 && updated) arr[idx]=updated;
  }

  // 채팅/기록 재로딩 & UI 업데이트
  await refreshChatDataForCurrent();
  paintProjectHeader();
  paintChat();
  paintHistory();
  paintIllusStrip();
  paintComposerBase();   // ← 이미 editBaseVersion이 null이므로 “최신본 기준”으로 표시

  scrollChatToBottom();  // 새 결과 보이도록
}

// ---- 선택(♥) — 깜빡임 없이 클래스만 토글 ----
function attachSelectHandler(el, slug){
  el.onclick = async (e)=>{
    e.preventDefault(); e.stopPropagation();
    const label   = el.getAttribute("data-select-label");
    const version = el.getAttribute("data-select"); // "__ORIGINAL__" | "A-2.png"

    const ill = state.current.illustrations.find(i=>i.label===label);
    const prevId = (ill && ill.selected)
      ? (ill.selected==="__ORIGINAL__" ? `${label}-0` : ill.selected.replace(".png",""))
      : `${label}-0`;
    const nextId = (version==="__ORIGINAL__") ? `${label}-0` : version.replace(".png","");

    const r = await jpost(`/api/projects/${slug}/select`, { label, version });
    if(!r.ok){ alert(r.error||"에러"); return; }

    if(ill){
      ill.selected = version;
      ill.selected_url = (version==="__ORIGINAL__") ? ill.original_url
        : `/files/projects/${slug}/illustrations/${label}/versions/${version}`;
    }

    updateSelectionUINoFlicker(label, prevId, nextId);
  };
}

function updateSelectionUINoFlicker(label, prevId, nextId){
  // History
  const grid = $("#history-grid");
  if(grid){
    const prevCell = grid.querySelector(`.cell[data-version="${CSS.escape(prevId)}"]`);
    const nextCell = grid.querySelector(`.cell[data-version="${CSS.escape(nextId)}"]`);
    if(prevCell) prevCell.classList.remove("is-selected");
    if(nextCell) nextCell.classList.add("is-selected");
  }

  // Chat
  const chat = $("#chat");
  if(chat){
    const prevBox = chat.querySelector(`.msg-img[data-version="${CSS.escape(prevId)}"]`);
    const nextBox = chat.querySelector(`.msg-img[data-version="${CSS.escape(nextId)}"]`);
    if(prevBox) prevBox.classList.remove("is-selected");
    if(nextBox) nextBox.classList.add("is-selected");
  }

  // Top strip thumb
  const ill = state.current.illustrations.find(i=>i.label===label);
  if(ill && ill.selected_url){
    const th = document.querySelector(`.illus-row img[data-pick-illus="${CSS.escape(label)}"]`);
    if(th) th.src = ill.selected_url;
  }
}

// ======================== Detail Modal & Delegation ========================
function openDetail({url, name}){
  state.detailImage = {url, name};
  const modal = $("#modal");
  $("#modal-img").src = url;
  $("#modal-download").href = url;
  $("#modal-download").setAttribute("download", name||"image.png");
  modal.classList.remove("hidden");
}
function closeDetail(){
  const modal = $("#modal");
  if(modal) modal.classList.add("hidden");
  state.detailImage=null;
}

// 이미지 클릭/채팅 점프 공통 델리게이션(프로젝트/에디터 공통)
function delegatedClicks(e){
  const t = e.target;
  const d = t.closest?.("[data-detail]");
  if(d && !d._handled){ d._handled=true; openDetail({url:d.dataset.detail,name:d.dataset.name||"image.png"}); return; }
  const j = t.closest?.("[data-chat-jump]");
  if(j && !j._handled){
    j._handled=true;
    const id=j.dataset.chatJump; const el=document.getElementById(`msg-${id}`);
    if(el){ scrollChatMsgToTop(el); el.style.outline="2px solid #10a37f"; setTimeout(()=>el.style.outline="",1200); }
  }
}

// ======================== Boot ========================
loadProjects();
