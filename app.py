import os, re, io, json, zipfile, shutil, base64, datetime, unicodedata, string
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from flask import Flask, request, jsonify, send_from_directory, send_file, abort
from flask_cors import CORS
from dotenv import load_dotenv
from PIL import Image
from google import genai
from google.genai import types

load_dotenv()

# ---- 기본 경로/클라이언트 설정 ----
DATA_DIR = os.getenv("DATA_DIR", "./data")
PROJECTS_DIR = os.path.join(DATA_DIR, "projects")
os.makedirs(PROJECTS_DIR, exist_ok=True)

# Gemini (nano-banana)
gemini_client = genai.Client()  # GEMINI_API_KEY 자동 인식
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"  # nano-banana (이미지)

# Flask
app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# ---------- 유틸 ----------
SAFE_CHARS = "-_.() %s%s" % (string.ascii_letters, string.digits)
def slugify(val: str) -> str:
    val = unicodedata.normalize("NFKD", val).encode("ascii", "ignore").decode("ascii")
    val = "".join(c for c in val if c in SAFE_CHARS).strip().lower()
    val = re.sub(r"[-\s]+", "-", val)
    return val or "project"

def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")

def ensure_dir(p):
    os.makedirs(p, exist_ok=True)

def read_json(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(p, obj):
    ensure_dir(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def read_text(p):
    if not os.path.exists(p): return ""
    with open(p, "r", encoding="utf-8") as f:
        return f.read()

def write_text(p, text):
    ensure_dir(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)

def append_text(p, line):
    ensure_dir(os.path.dirname(p))
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def list_projects():
    items = []
    for slug in sorted(os.listdir(PROJECTS_DIR)):
        proj_path = os.path.join(PROJECTS_DIR, slug)
        if not os.path.isdir(proj_path): continue
        meta = read_json(os.path.join(proj_path, "project.json"), {})
        illustrations_dir = os.path.join(proj_path, "illustrations")
        count = 0
        previews = []
        if os.path.isdir(illustrations_dir):
            for label in sorted(os.listdir(illustrations_dir)):
                lab_dir = os.path.join(illustrations_dir, label)
                if not os.path.isdir(lab_dir): continue
                count += 1
                sel = read_text(os.path.join(lab_dir, "selected.txt")).strip()
                # ★ "__ORIGINAL__" 처리
                if sel == "__ORIGINAL__":
                    if os.path.exists(os.path.join(lab_dir, "original.png")):
                        previews.append(f"/files/projects/{slug}/illustrations/{label}/original.png")
                elif sel:
                    previews.append(f"/files/projects/{slug}/illustrations/{label}/versions/{sel}")
                else:
                    # 아직 선택 없으면 original
                    if os.path.exists(os.path.join(lab_dir, "original.png")):
                        previews.append(f"/files/projects/{slug}/illustrations/{label}/original.png")
        items.append({
            "slug": slug,
            "name": meta.get("name", slug),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "illustration_count": count,
            "previews": previews,
        })
    return items

def project_path(slug):
    return os.path.join(PROJECTS_DIR, slug)

def illustrations_path(slug):
    return os.path.join(project_path(slug), "illustrations")

def chatlogs_path(slug):
    return os.path.join(project_path(slug), "chat_logs")

def next_label(existing: List[str]) -> str:
    # A, B, C... (이미 존재하는 레이블 다음)
    letters = [chr(i) for i in range(ord('A'), ord('Z')+1)]
    for L in letters:
        if L not in existing:
            return L
    # Z 넘어가면 AA, AB... 간단 구현
    idx = 1
    while True:
        for L in letters:
            cand = letters[idx-1] + L
            if cand not in existing:
                return cand
        idx += 1

def latest_version_num(label_dir: str) -> int:
    ver_dir = os.path.join(label_dir, "versions")
    if not os.path.isdir(ver_dir): return 0
    maxn = 0
    for fn in os.listdir(ver_dir):
        m = re.match(rf"([A-Z]+)-(\d+)\.png$", fn)
        if m:
            n = int(m.group(2))
            if n > maxn: maxn = n
    return maxn

def save_pil(img: Image.Image, path: str):
    ensure_dir(os.path.dirname(path))
    img.save(path, format="PNG")

def load_pil(path: str) -> Image.Image:
    return Image.open(path).convert("RGBA")

def force_same_size(output_img: Image.Image, ref_path: str) -> Image.Image:
    """출력 이미지를 base 이미지와 동일한 크기로 강제 보정."""
    try:
        with Image.open(ref_path) as ref:
            if output_img.size != ref.size:
                output_img = output_img.resize(ref.size, Image.LANCZOS)
    except Exception:
        pass
    return output_img

# ---------- 파일 서빙 ----------
@app.route("/files/<path:subpath>")
def files(subpath):
    # /files/projects/<slug>/...
    safe_root = os.path.abspath(DATA_DIR)
    full = os.path.abspath(os.path.join(DATA_DIR, subpath))
    if not full.startswith(safe_root):
        abort(403)
    if not os.path.exists(full):
        abort(404)
    if os.path.isdir(full):
        abort(404)
    return send_file(full)

# ---------- 프로젝트 CRUD ----------
@app.route("/api/projects", methods=["GET"])
def api_list_projects():
    return jsonify({"ok": True, "projects": list_projects()})

@app.route("/api/projects", methods=["POST"])
def api_create_project():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "프로젝트 이름이 필요합니다."}), 400

    slug = slugify(name)
    base = project_path(slug)

    # 1) 같은 slug(=이름 충돌) 폴더가 이미 있으면 거부
    if os.path.isdir(base):
        return jsonify({"ok": False, "error": "동일한 이름의 프로젝트가 이미 존재합니다."}), 409

    # 2) 메타 이름이 같은 프로젝트가 있는지도(대소문자 무시) 거부
    for existing in os.listdir(PROJECTS_DIR):
        pdir = os.path.join(PROJECTS_DIR, existing)
        if not os.path.isdir(pdir): 
            continue
        meta = read_json(os.path.join(pdir, "project.json"), {})
        if (meta.get("name","").strip().lower() == name.lower()):
            return jsonify({"ok": False, "error": "동일한 이름의 프로젝트가 이미 존재합니다."}), 409

    # 생성
    ensure_dir(base)
    meta = {
        "name": name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    write_json(os.path.join(base, "project.json"), meta)
    ensure_dir(illustrations_path(slug))
    ensure_dir(chatlogs_path(slug))
    return jsonify({"ok": True, "slug": slug})


@app.route("/api/projects/<slug>/rename", methods=["POST"])
def api_rename_project(slug):
    data = request.get_json(force=True)
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "이름이 필요합니다."}), 400

    base = project_path(slug)
    if not os.path.isdir(base):
        return jsonify({"ok": False, "error": "프로젝트가 없습니다."}), 404

    # 다른 프로젝트와 이름 중복 체크
    for existing in os.listdir(PROJECTS_DIR):
        if existing == slug:
            continue
        pdir = os.path.join(PROJECTS_DIR, existing)
        if not os.path.isdir(pdir):
            continue
        meta2 = read_json(os.path.join(pdir, "project.json"), {})
        if meta2.get("name","").strip().lower() == new_name.lower():
            return jsonify({"ok": False, "error": "동일한 이름의 프로젝트가 이미 존재합니다."}), 409

    meta_p = os.path.join(base, "project.json")
    meta = read_json(meta_p, {})
    meta["name"] = new_name
    meta["updated_at"] = now_iso()
    write_json(meta_p, meta)
    return jsonify({"ok": True})


@app.route("/api/projects/<slug>", methods=["DELETE"])
def api_delete_project(slug):
    base = project_path(slug)
    if not os.path.isdir(base):
        return jsonify({"ok": False, "error": "프로젝트가 없습니다."}), 404
    shutil.rmtree(base)
    return jsonify({"ok": True})

@app.route("/api/projects/<slug>", methods=["GET"])
def api_project_detail(slug):
    base = project_path(slug)
    if not os.path.isdir(base):
        return jsonify({"ok": False, "error": "프로젝트가 없습니다."}), 404
    meta = read_json(os.path.join(base, "project.json"), {})
    illus_dir = illustrations_path(slug)
    items = []
    if os.path.isdir(illus_dir):
        for label in sorted(os.listdir(illus_dir)):
            Ldir = os.path.join(illus_dir, label)
            if not os.path.isdir(Ldir): continue
            sel = read_text(os.path.join(Ldir, "selected.txt")).strip()
            versions = []
            ver_dir = os.path.join(Ldir, "versions")
            if os.path.isdir(ver_dir):
                for fn in sorted(os.listdir(ver_dir)):
                    if re.match(rf"{label}-\d+\.png$", fn):
                        versions.append(fn)

            # ★ selected, selected_url 일관 처리
            original_url = f"/files/projects/{slug}/illustrations/{label}/original.png" if os.path.exists(os.path.join(Ldir,"original.png")) else ""
            if not sel:
                selected = "__ORIGINAL__"
                selected_url = original_url
            elif sel == "__ORIGINAL__":
                selected = "__ORIGINAL__"
                selected_url = original_url
            else:
                selected = sel
                selected_url = f"/files/projects/{slug}/illustrations/{label}/versions/{sel}"

            items.append({
                "label": label,
                "original_url": original_url,
                "selected": selected,
                "selected_url": selected_url,
                "version_files": versions,
                "version_count": len(versions),
                "chat_log_url": f"/files/projects/{slug}/chat_logs/{label}.txt" if os.path.exists(os.path.join(chatlogs_path(slug), f"{label}.txt")) else ""
            })
    return jsonify({"ok": True, "meta": meta, "illustrations": items})

# ---------- 삽화 업로드/삭제/다운로드 ----------
@app.route("/api/projects/<slug>/illustrations", methods=["POST"])
def api_add_illustrations(slug):
    base = project_path(slug)
    if not os.path.isdir(base):
        return jsonify({"ok": False, "error": "프로젝트가 없습니다."}), 404
    files = request.files.getlist("images")
    if not files:
        return jsonify({"ok": False, "error": "이미지 파일이 없습니다."}), 400

    illus_dir = illustrations_path(slug)
    existing = sorted([d for d in os.listdir(illus_dir) if os.path.isdir(os.path.join(illus_dir,d))])
    created = []
    label_cursor = existing[:]  # 복사
    for f in files:
        # 새 레이블 결정
        L = next_label(label_cursor)
        label_cursor.append(L)
        Ldir = os.path.join(illus_dir, L)
        ensure_dir(Ldir)
        # 원본 저장 (png로 통일)
        img = Image.open(f.stream).convert("RGBA")
        img.save(os.path.join(Ldir, "original.png"), format="PNG")
        # 버전 폴더
        ensure_dir(os.path.join(Ldir, "versions"))
        # ★ 기본 최종 선택 = 원본
        write_text(os.path.join(Ldir, "selected.txt"), "__ORIGINAL__")
        # 채팅 로그 파일 생성
        ensure_dir(chatlogs_path(slug))
        append_text(os.path.join(chatlogs_path(slug), f"{L}.txt"),
                    f"[{now_iso()}] [INIT] Uploaded original for {L}")
        created.append(L)

    # updated_at
    meta_p = os.path.join(base, "project.json")
    meta = read_json(meta_p, {})
    meta["updated_at"] = now_iso()
    write_json(meta_p, meta)

    return jsonify({"ok": True, "labels": created})

@app.route("/api/projects/<slug>/illustrations/<label>", methods=["DELETE"])
def api_delete_illustration(slug, label):
    Ldir = os.path.join(illustrations_path(slug), label)
    if not os.path.isdir(Ldir):
        return jsonify({"ok": False, "error": "삽화가 없습니다."}), 404
    # 경고/확인은 프론트에서 alert 처리. 서버는 바로 삭제
    shutil.rmtree(Ldir)
    # 로그도 삭제
    log_p = os.path.join(chatlogs_path(slug), f"{label}.txt")
    if os.path.exists(log_p):
        os.remove(log_p)
    # updated_at
    meta_p = os.path.join(project_path(slug), "project.json")
    meta = read_json(meta_p, {})
    meta["updated_at"] = now_iso()
    write_json(meta_p, meta)
    return jsonify({"ok": True})

@app.route("/api/projects/<slug>/download_selected", methods=["GET"])
def api_download_selected(slug):
    base = project_path(slug)
    if not os.path.isdir(base):
        return jsonify({"ok": False, "error": "프로젝트가 없습니다."}), 404

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        illus_dir = illustrations_path(slug)
        if os.path.isdir(illus_dir):
            for label in sorted(os.listdir(illus_dir)):
                Ldir = os.path.join(illus_dir, label)
                if not os.path.isdir(Ldir): continue
                sel = read_text(os.path.join(Ldir, "selected.txt")).strip()
                if not sel or sel == "__ORIGINAL__":
                    src = os.path.join(Ldir, "original.png")
                    arcname = f"{label}.png"
                else:
                    src = os.path.join(Ldir, "versions", sel)
                    arcname = sel  # 또는 f"{label}.png"로 통일하려면 변경
                if os.path.exists(src):
                    zf.write(src, arcname=arcname)
    mem.seek(0)
    return send_file(mem, mimetype="application/zip",
                     as_attachment=True, download_name=f"{slug}_selected.zip")

# ---------- 최종 선택(♥) ----------
@app.route("/api/projects/<slug>/select", methods=["POST"])
def api_select_version(slug):
    data = request.get_json(force=True)
    label = (data.get("label") or "").strip()
    version = (data.get("version") or "").strip()  # "__ORIGINAL__" | "A-2.png"
    if not label or not version:
        return jsonify({"ok": False, "error": "label, version 필요"}), 400
    Ldir = os.path.join(illustrations_path(slug), label)
    if not os.path.isdir(Ldir):
        return jsonify({"ok": False, "error": "삽화가 없습니다."}), 404

    if version == "__ORIGINAL__":
        # ★ 원본을 최종선택
        if not os.path.exists(os.path.join(Ldir, "original.png")):
            return jsonify({"ok": False, "error": "원본이 없습니다."}), 404
        write_text(os.path.join(Ldir, "selected.txt"), "__ORIGINAL__")
        append_text(os.path.join(chatlogs_path(slug), f"{label}.txt"),
                    f"[{now_iso()}] [SELECT] __ORIGINAL__ set as selected")
    else:
        vpath = os.path.join(Ldir, "versions", version)
        if not os.path.exists(vpath):
            return jsonify({"ok": False, "error": "버전이 없습니다."}), 404
        write_text(os.path.join(Ldir, "selected.txt"), version)
        append_text(os.path.join(chatlogs_path(slug), f"{label}.txt"),
                    f"[{now_iso()}] [SELECT] {version} set as selected")

    # updated_at
    meta_p = os.path.join(project_path(slug), "project.json")
    meta = read_json(meta_p, {})
    meta["updated_at"] = now_iso()
    write_json(meta_p, meta)
    return jsonify({"ok": True})

# ---------- 편집(나노 바나나) ----------
@app.route("/api/projects/<slug>/edit", methods=["POST"])
def api_edit(slug):
    """
    form-data:
      - label: "A"
      - prompt: str
      - base_version: "A-2.png" | "__ORIGINAL__" (optional)
    모든 생성물은 versions/A-n.png 로 저장.
    새 이미지가 생성되면 해당 삽화의 최종 선택으로 갱신.
    채팅 로그(chat_logs/A.txt)에는
      - [USER] base(썸네일 경로) + prompt 기록
      - [MODEL] output 버전 파일명 기록
    """
    label = (request.form.get("label") or "").strip()
    prompt = (request.form.get("prompt") or "").strip()
    base_version = (request.form.get("base_version") or "").strip()

    if not label or not prompt:
        return jsonify({"ok": False, "error": "label, prompt가 필요합니다."}), 400

    Ldir = os.path.join(illustrations_path(slug), label)
    if not os.path.isdir(Ldir):
        return jsonify({"ok": False, "error": "삽화가 없습니다."}), 404

    # 베이스 이미지 결정
    versions_dir = os.path.join(Ldir, "versions")
    ensure_dir(versions_dir)

    if base_version == "__ORIGINAL__":
        base_img_path = os.path.join(Ldir, "original.png")
        if not os.path.exists(base_img_path):
            return jsonify({"ok": False, "error": "원본 이미지가 없습니다."}), 404
    elif base_version:
        cand = os.path.join(versions_dir, base_version)
        if os.path.exists(cand):
            base_img_path = cand
        else:
            return jsonify({"ok": False, "error": "base_version 파일이 없습니다."}), 404
    else:
        # 최신 버전 or original
        n = latest_version_num(Ldir)
        if n > 0:
            base_img_path = os.path.join(versions_dir, f"{label}-{n}.png")
        else:
            base_img_path = os.path.join(Ldir, "original.png")
            if not os.path.exists(base_img_path):
                return jsonify({"ok": False, "error": "원본 이미지가 없습니다."}), 404

    # Gemini 호출 (이미지+프롬프트 → 이미지)
    try:
        src_img = Image.open(base_img_path).convert("RGBA")
        resp = gemini_client.models.generate_content(
            model=GEMINI_IMAGE_MODEL,
            contents=[src_img, prompt],
        )
        # 응답에서 이미지 바이트 추출
        out_bytes = None
        if resp and getattr(resp, "candidates", None):
            for part in resp.candidates[0].content.parts:
                if getattr(part, "inline_data", None):
                    out_bytes = part.inline_data.data
                    break
        if not out_bytes:
            # 텍스트만 온 경우
            text = getattr(resp, "text", "") or "이미지 결과가 없습니다."
            # 로그만 남기고 에러로 반환
            append_text(os.path.join(chatlogs_path(slug), f"{label}.txt"),
                        f"[{now_iso()}] [MODEL:TEXT] {text}")
            return jsonify({"ok": False, "error": text}), 400

        out_img = Image.open(io.BytesIO(out_bytes)).convert("RGBA")
        # ★ 출력 크기 = 베이스 크기 강제
        out_img = force_same_size(out_img, base_img_path)

        # 새 버전 번호
        new_n = latest_version_num(Ldir) + 1
        out_name = f"{label}-{new_n}.png"
        out_path = os.path.join(versions_dir, out_name)
        save_pil(out_img, out_path)

        # 로그 기록 (채팅 메시지처럼)
        # 사용자: 베이스/프롬프트
        base_rel = base_img_path.replace(DATA_DIR, "").replace("\\", "/")
        append_text(os.path.join(chatlogs_path(slug), f"{label}.txt"),
                    f"[{now_iso()}] [USER] base={base_rel} prompt={prompt}")
        # 모델: 생성 파일
        out_rel = out_path.replace(DATA_DIR, "").replace("\\", "/")
        append_text(os.path.join(chatlogs_path(slug), f"{label}.txt"),
                    f"[{now_iso()}] [MODEL] out={out_rel}")

        # 프로젝트 갱신
        meta_p = os.path.join(project_path(slug), "project.json")
        meta = read_json(meta_p, {})
        meta["updated_at"] = now_iso()
        write_json(meta_p, meta)

        return jsonify({
            "ok": True,
            "version": out_name,
            "image_url": f"/files{out_rel}"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    
@app.route("/api/projects/<slug>/download_selected_numbered", methods=["GET"])
def api_download_selected_numbered(slug):
    base = project_path(slug)
    if not os.path.isdir(base):
        return jsonify({"ok": False, "error": "프로젝트가 없습니다."}), 404

    meta = read_json(os.path.join(base, "project.json"), {})
    proj_name = meta.get("name") or slug
    zip_name = f"{proj_name}.zip"

    files_to_zip = []
    illus_dir = illustrations_path(slug)
    if os.path.isdir(illus_dir):
        for label in sorted(os.listdir(illus_dir)):
            Ldir = os.path.join(illus_dir, label)
            if not os.path.isdir(Ldir):
                continue
            sel = read_text(os.path.join(Ldir, "selected.txt")).strip()
            if sel:
                if sel == "__ORIGINAL__":
                    src = os.path.join(Ldir, "original.png")
                else:
                    src = os.path.join(Ldir, "versions", sel)
            else:
                src = os.path.join(Ldir, "original.png")
            if os.path.exists(src):
                files_to_zip.append(src)

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, src in enumerate(files_to_zip, start=1):
            zf.write(src, arcname=f"{idx}.png")
    mem.seek(0)
    return send_file(mem, mimetype="application/zip",
                     as_attachment=True, download_name=zip_name)


# ---------- 정적 진입 ----------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
