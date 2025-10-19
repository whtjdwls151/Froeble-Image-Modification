import os
import base64
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types
from PIL import Image
from io import BytesIO

# ✅ dotenv 로드
from dotenv import load_dotenv
load_dotenv()  # .env 파일을 환경변수로 로드

# OpenAI 공식 SDK
from openai import OpenAI

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# ✅ .env 로부터 주입된 환경변수 사용
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("환경변수 OPENAI_API_KEY가 설정되지 않았습니다. .env를 확인하세요.")

client = OpenAI(api_key=api_key)
gemini_client = genai.Client()  # GEMINI_API_KEY를 자동 인식
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"

# 업로드 임시 디렉토리
UPLOAD_DIR = "./uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ===== 라우팅: 정적 파일 (프론트) =====
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ===== Text Chat (대화) =====
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    raw_messages = data.get("messages", [])

    # ✅ content[].type을 Responses API에 맞게 정규화
    def normalize_messages(msgs):
        fixed = []
        for m in msgs:
            role = m.get("role", "user")
            content = m.get("content", [])
            new_content = []
            for c in content:
                ctype = c.get("type")
                if ctype == "text":            # ← 프론트에서 넘어오는 타입
                    new_content.append({
                        "type": "input_text",  # ← Responses API가 요구
                        "text": c.get("text", "")
                    })
                elif ctype == "image_url":
                    # URL/데이터 URI 등을 써서 이미지 넣을 때 이렇게 매핑
                    new_content.append({
                        "type": "input_image",
                        "image_url": c.get("url") or c.get("image_url")
                    })
                else:
                    # 이미 올바른 타입이거나(예: input_text) 그 외는 그대로
                    new_content.append(c)
            fixed.append({"role": role, "content": new_content})
        return fixed

    messages = normalize_messages(raw_messages)

    try:
        resp = client.responses.create(
            model="gpt-5",
            input=messages,    # ← 정규화된 messages
        )
        text = getattr(resp, "output_text", "") or ""
        return jsonify({"ok": True, "type": "text", "content": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# ===== Image Generate/Edit =====
@app.route("/api/image", methods=["POST"])
def image():
    """
    멀티파트 폼:
      - prompt: str (필수)
      - image: file (선택) → 있으면 '편집', 없으면 '생성'
      - mask: file (선택, PNG 알파 사용) → 지금은 nano-banana가 네이티브 마스크 파라미터를 제공하지 않으므로
        서버에서 부분 합성(옵션)으로 처리 가능. 우선은 프롬프트 기반 편집으로 동작시킴.
      - size: str (선택) → Gemini는 기본적으로 입력 이미지 크기를 따르거나 1:1을 반환.
    """
    prompt = request.form.get("prompt", "").strip()
    size = request.form.get("size", "1024x1024")  # 생성 모드에서만 힌트로 사용
    image_file = request.files.get("image")
    # mask_file = request.files.get("mask")  # 필요시 로컬 합성 로직로 확장

    if not prompt and not image_file:
        return jsonify({"ok": False, "error": "prompt 또는 image가 필요합니다."}), 400

    try:
        # === 편집: 이미지 + 텍스트 → 이미지 ===
        if image_file:
            # Gemini는 편집 시 그냥 [이미지, 프롬프트]를 contents로 보내면 됩니다. :contentReference[oaicite:2]{index=2}
            img = Image.open(image_file.stream).convert("RGBA")

            resp = gemini_client.models.generate_content(
                model=GEMINI_IMAGE_MODEL,
                contents=[img, prompt],
                # 필요시 응답 모달리티/가로세로비 설정 가능:
                # config=types.GenerateContentConfig(response_modalities=[types.Modality.IMAGE])
            )

            # 응답에서 이미지 바이트 꺼내기
            data_url = None
            for part in resp.candidates[0].content.parts:
                if getattr(part, "inline_data", None):
                    raw = part.inline_data.data  # bytes
                    b64 = base64.b64encode(raw).decode("utf-8")
                    data_url = f"data:image/png;base64,{b64}"
                    break
            if not data_url:
                # 텍스트만 왔을 수도 있으므로 메시지 반환
                text = getattr(resp, "text", "") or "이미지 결과가 없습니다."
                return jsonify({"ok": True, "type": "text", "content": text})

            return jsonify({"ok": True, "type": "image", "content": data_url})

        # === 생성: 텍스트 → 이미지 ===
        else:
            # 기본은 정사각(1:1). 특정 종횡비를 원하면 config.image_config.aspect_ratio 사용. :contentReference[oaicite:3]{index=3}
            resp = gemini_client.models.generate_content(
                model=GEMINI_IMAGE_MODEL,
                contents=[prompt],
                # 예) 정사각 유지: 생략
                # 예) 와이드 16:9:
                # config=types.GenerateContentConfig(
                #     image_config=types.ImageConfig(aspect_ratio="16:9"),
                #     response_modalities=[types.Modality.IMAGE],
                # )
            )
            data_url = None
            for part in resp.candidates[0].content.parts:
                if getattr(part, "inline_data", None):
                    raw = part.inline_data.data
                    b64 = base64.b64encode(raw).decode("utf-8")
                    data_url = f"data:image/png;base64,{b64}"
                    break
            if not data_url:
                text = getattr(resp, "text", "") or "이미지 결과가 없습니다."
                return jsonify({"ok": True, "type": "text", "content": text})

            return jsonify({"ok": True, "type": "image", "content": data_url})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400



if __name__ == "__main__":
    # 개발용 실행
    app.run(host="0.0.0.0", port=8000, debug=True)
