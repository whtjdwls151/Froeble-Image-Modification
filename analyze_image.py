import argparse
import base64
import json
import mimetypes
import os
import sys
from urllib.parse import urlparse

from PIL import Image
import numpy as np
from dotenv import load_dotenv
from bfl_finetune import finetune_inference, get_inference
import time
import requests
from io import BytesIO
from openai import OpenAI


# =========================
# OpenAI SDK
# =========================
try:
    from openai import OpenAI
except ImportError:
    print("`pip install openai` 로 설치해주세요.")
    sys.exit(1)

HUMAN_LABELS = {"girl","boy","woman","man","person","child","character"}

def _object_phrase(o):
    label = _clean_label(o.get("label"))
    if not label:
        return None
    attr = o.get("attributes", {}) or {}
    other = (attr.get("other") or "").lower()
    material = (attr.get("material") or "").lower()
    pattern  = (attr.get("pattern")  or "").lower()
    color_adj = None
    if isinstance(o.get("dominant_color_hex"), str):
        color_adj = _nearest_color_name(o["dominant_color_hex"])

    # 사람: 색은 '사람'이 아니라 '의상'에 붙임, fabric 생략
    if label in HUMAN_LABELS:
        phrase = f"a {label}"
        if color_adj:
            phrase += f" wearing {color_adj} clothes"
        # 머리 표현만 선택적으로
        hair = None
        low = other
        if "curly" in low: hair = "curly hair"
        elif "straight" in low: hair = "straight hair"
        if hair:
            phrase += f" with {hair}"
        return phrase

    # 나무: leafy + 색 + tree, wood 생략
    if label == "tree":
        leafy = ("leafy" in other) or ("leafy" in pattern)
        base_color = color_adj or "brown"
        prefix = "leafy " if leafy else ""
        return f"a {prefix}{base_color} tree".strip()

    # 일반 객체(예: dog 등)
    phrase = f"a {color_adj} {label}" if color_adj else f"a {label}"
    extras = []
    if material == "fur" or "fur" in other:
        extras.append("fur")
    # 친근/표정류
    if "friendly" in other:
        extras.append("friendly expression")
    elif other and other not in ("solid",):
        extras.append(other)
    if extras:
        phrase += " with " + " and ".join(extras)
    return phrase

# =========================
# 파일/경로 유틸
# =========================
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def save_json(obj: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def list_reference_images(ref_dir: str):
    out = []
    if not os.path.isdir(ref_dir):
        return out
    with os.scandir(ref_dir) as it:
        for e in it:
            if not e.is_file():
                continue
            _, ext = os.path.splitext(e.name.lower())
            if ext in IMG_EXTS:
                try:
                    stat = e.stat()
                    out.append((e.path, e.name, stat.st_mtime))
                except Exception:
                    continue
    return out

def sort_images(items, mode="newest"):
    if mode == "newest":
        return sorted(items, key=lambda x: x[2], reverse=True)
    if mode == "oldest":
        return sorted(items, key=lambda x: x[2])
    return sorted(items, key=lambda x: x[1].lower())

# =========================
# 이미지 로드/인코딩
# =========================
def load_image_as_data_url(image_path_or_url: str) -> str:
    parsed = urlparse(image_path_or_url)
    if parsed.scheme in ("http", "https"):
        return image_path_or_url
    if not os.path.exists(image_path_or_url):
        raise FileNotFoundError(f"이미지를 찾을 수 없습니다: {image_path_or_url}")
    mime, _ = mimetypes.guess_type(image_path_or_url)
    if mime is None:
        mime = "image/jpeg"
    with open(image_path_or_url, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"

def approx_dominant_hex(image_path_or_url: str) -> str | None:
    parsed = urlparse(image_path_or_url)
    if parsed.scheme in ("http", "https"):
        return None
    try:
        img = Image.open(image_path_or_url).convert("RGB")
        img_small = img.resize((64, 64))
        arr = np.array(img_small).reshape(-1, 3)
        mean = arr.mean(axis=0).astype(int)
        return "#{:02x}{:02x}{:02x}".format(mean[0], mean[1], mean[2])
    except Exception:
        return None

# =========================
# 비전 분석 프롬프트(모델 지침)
# =========================
SYSTEM_PROMPT = """You are a precise vision-to-JSON extractor.
Only return valid JSON that strictly matches the provided schema.
If unsure, estimate and reflect uncertainty in descriptions (not the schema).
All bounding boxes are normalized floats in [0,1] with top-left origin.
"""

JSON_SCHEMA_TEXT = """
Return a JSON object like this (keys must match exactly):

{
  "image_info": {
    "width_px": null,
    "height_px": null,
    "inferred_scene": "",
    "notes": ""
  },
  "style": {
    "genre": "",
    "rendering": "",
    "line_style": "",
    "color_palette": "",
    "lighting": "",
    "tone_mood": "",
    "shading": "",
    "texture": "",
    "composition": "",
    "references": ""
  },
  "style_tags": ["tag1","tag2","tag3"],
  "objects": [
    {
      "id": "obj_1",
      "label": "",
      "confidence": 0.0,
      "bbox_norm": { "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0 },
      "dominant_color_hex": "#000000",
      "attributes": {
        "material": "",
        "pattern": "",
        "shape": "",
        "size": "",
        "other": ""
      }
    }
  ]
}
Rules:
- style_tags: 5~10 short, model-friendly tags; lowercase; hyphen for multi-word.
- color_palette: brief overall description, not hex list.
"""

USER_INSTRUCTIONS = """Extract:
1) Objects with normalized bounding boxes, dominant color hex, and attributes.
2) Overall image style (genre, rendering, line style, color palette, lighting, tone/mood, shading, texture, composition, references).
3) 5~10 concise style tags (e.g., ["flat-2d","cartoon","soft-lighting","warm-palette","bold-outline"]).
Return strictly valid JSON matching the schema.
"""

# =========================
# OpenAI 호출
# =========================
def call_gpt_vision(image_ref: str, api_key: str, model: str = "gpt-4o-mini") -> dict:
    client = OpenAI(api_key=api_key)
    completion = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": JSON_SCHEMA_TEXT},
                    {"type": "text", "text": USER_INSTRUCTIONS},
                    {"type": "image_url", "image_url": {"url": image_ref}},
                ],
            },
        ],
        temperature=0.1,
    )
    text = completion.choices[0].message.content
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        text_fixed = text.strip().split("```")[-1]
        text_fixed = text_fixed.replace("json\n", "").replace("json", "")
        return json.loads(text_fixed)

# =========================
# 위치/색상 유틸
# =========================
def _bbox_center(b):
    cx = float(b.get("x", 0.0)) + float(b.get("w", 0.0)) / 2.0
    cy = float(b.get("y", 0.0)) + float(b.get("h", 0.0)) / 2.0
    return cx, cy

def _region_phrase(cx, cy, tol=0.1):
    if cx < 0.5 - tol: horiz = "left"
    elif cx > 0.5 + tol: horiz = "right"
    else: horiz = "center"
    if cy < 0.5 - tol: vert = "upper"
    elif cy > 0.5 + tol: vert = "lower"
    else: vert = "middle"
    if horiz == "center" and vert == "middle":
        return "the center"
    if vert in ("upper","lower"):
        return f"the {vert} {horiz}"
    return f"the {horiz}"

def _relative_phrase(a_label, a_bbox, b_label, b_bbox, near_thresh=0.12, overlap_thresh=0.25):
    ax, ay = _bbox_center(a_bbox); bx, by = _bbox_center(b_bbox)
    dx, dy = ax - bx, ay - by
    dist = (dx**2 + dy**2) ** 0.5
    ax0, ax1 = a_bbox["x"], a_bbox["x"] + a_bbox["w"]
    bx0, bx1 = b_bbox["x"], b_bbox["x"] + b_bbox["w"]
    overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    union = max(ax1, bx1) - min(ax0, bx0)
    overlap_ratio = (overlap / union) if union > 0 else 0.0
    if dx < -0.08:
        return f"The {a_label} is to the left of the {b_label}."
    if dx >  0.08:
        return f"The {a_label} is to the right of the {b_label}."
    if overlap_ratio >= overlap_thresh:
        if dy > 0.08:
            return f"The {a_label} is below the {b_label}."
        if dy < -0.08:
            return f"The {a_label} is above the {b_label}."
    if dist <= near_thresh:
        return f"The {a_label} is near the {b_label}."
    return None

def _clean_label(label: str) -> str:
    return (label or "").lower().strip()

def _hex_to_rgb(hexstr: str):
    hexstr = hexstr.lstrip("#")
    if len(hexstr) != 6:
        return (128,128,128)
    return tuple(int(hexstr[i:i+2], 16) for i in (0,2,4))

_BASIC_COLORS = {
    "white": (255,255,255), "black": (0,0,0), "red": (255,0,0),
    "green": (0,170,0), "blue": (0,102,255), "yellow": (255,212,0),
    "gray": (136,136,136), "brown": (139,69,19), "pink": (255,192,203),
    "purple": (128,0,128), "orange": (255,165,0), "beige": (245,222,179),
    "silver": (192,192,192), "gold": (212,175,55), "cyan": (0,255,255),
    "magenta": (255,0,255),
}

def _nearest_color_name(hexstr: str) -> str | None:
    try:
        r,g,b = _hex_to_rgb(hexstr)
    except Exception:
        return None
    best, bestd = None, 1e9
    for name,(R,G,B) in _BASIC_COLORS.items():
        d = (r-R)**2 + (g-G)**2 + (b-B)**2
        if d < bestd:
            best, bestd = name, d
    return best

# =========================
# 프롬프트 생성(EN → KO)
# =========================
def build_english_prompt(analysis: dict) -> str:
    info = analysis.get("image_info", {}) or {}
    style = analysis.get("style", {}) or {}
    objs = analysis.get("objects", []) or []

    scene = (info.get("inferred_scene") or "").strip()
    notes = (info.get("notes") or "").strip()

    lines = []
    if scene:
        lines.append(scene.rstrip(".") + ".")

    # 객체(자연스러운 문구로 합성)
    phrases = []
    for o in objs:
        phrase = _object_phrase(o)
        if phrase:
            phrases.append(phrase)


        if phrases:
            if len(phrases) == 1:
                lines.append(f"The scene includes {phrases[0]}.")
            elif len(phrases) == 2:
                lines.append(f"The scene includes {phrases[0]} and {phrases[1]}.")
            else:
                lines.append(f"The scene includes {', '.join(phrases[:-1])}, and {phrases[-1]}.")

    # 절대 위치
    pos_sents = []
    for o in objs:
        label = _clean_label(o.get("label"))
        bbox = (o.get("bbox_norm") or {})
        if not label or not {"x","y","w","h"} <= set(bbox.keys()):
            continue
        cx, cy = _bbox_center(bbox)
        region = _region_phrase(cx, cy, tol=0.1)
        verb = "stands" if label in ("girl","boy","person","man","woman","character","child") else "is"
        pos_sents.append(f"The {label} {verb} in {region}.")

    # 상대 위치(상위 3개까지만)
    objs_for_rel = objs[:3]
    for i in range(len(objs_for_rel)):
        for j in range(i+1, len(objs_for_rel)):
            a = objs_for_rel[i]; b = objs_for_rel[j]
            a_label = _clean_label(a.get("label")); b_label = _clean_label(b.get("label"))
            a_bbox = a.get("bbox_norm") or {}; b_bbox = b.get("bbox_norm") or {}
            if not a_label or not b_label: continue
            if not ({"x","y","w","h"} <= set(a_bbox.keys()) and {"x","y","w","h"} <= set(b_bbox.keys())):
                continue
            rel = _relative_phrase(a_label, a_bbox, b_label, b_bbox)
            if rel: pos_sents.append(rel)

    if pos_sents:
        seen, uniq = set(), []
        for s in pos_sents:
            if s not in seen:
                uniq.append(s); seen.add(s)
        lines.append(" ".join(uniq))

    # 스타일/톤
    style_bits = []
    for key in ["genre","rendering","line_style","color_palette","lighting","tone_mood","shading","texture"]:
        val = (style.get(key) or "").strip()
        if val:
            style_bits.append(val)
    # 'illustration' 단어 중복 제거
    filtered = [b for b in style_bits if b.lower() not in {"illustration","an illustration","a illustration"}]
    if filtered:
        # 관사 오류 방지: 그냥 명사구 나열
        lines.append("The illustration is rendered in " + ", ".join(filtered) + " style.")

    if style.get("composition"):
        lines.append(f"The composition is {style['composition']}.")
    if notes:
        lines.append(notes.rstrip(".") + ".")
    lines.append("Child-friendly picture-book illustration, flat 2D digital illustration, high quality.")

    out = " ".join(" ".join(l.split()) for l in lines).strip()
    return out

# def english_to_korean_prompt(en_prompt: str, api_key: str, model: str = "gpt-4o-mini") -> str:
#     client = OpenAI(api_key=api_key)
#     sys_msg = (
#         "You are a professional Korean copywriter for children's picture-book prompts. "
#         "Rewrite the given English prompt into fluent Korean natural sentences for an image generation model. "
#         "Keep spatial relations and composition details, and DO NOT omit any concrete nouns. "
#         "Translate spatial words explicitly: left, right, upper, lower, center, middle. "
#         "Include the phrase '플랫 2D 디지털 일러스트' verbatim. "
#         "Do NOT use colons, bullets, or list-like structures. 2-4 sentences. No code blocks."
#     )
#     user_msg = f"English prompt:\n{en_prompt}\n\nRewrite in Korean as natural sentences."
#     resp = client.chat.completions.create(
#         model=model,
#         temperature=0.2,
#         messages=[
#             {"role": "system", "content": sys_msg},
#             {"role": "user", "content": user_msg},
#         ],
#     )
#     ko = resp.choices[0].message.content.strip()
#     return ko.replace("```", "").strip()
from openai import OpenAI

def english_to_korean_prompt_strict(en_prompt: str, api_key: str, model: str = "gpt-4o-mini") -> str:
    """
    영어 프롬프트를 한국어로 '충실하게' 번역:
    - 모든 사실(주체, 색상, 재질, 위치/관계, 스타일/무드, 구성)을 1개도 빠뜨리지 않음
    - 자연스러운 문장 2~4개, 콜론/리스트/코드블록 금지
    - 핵심 용어 고정 번역(예: upper left, bold outlines, flat 2D digital illustration 등)
    """
    client = OpenAI(api_key=api_key)
    sys_msg = (
        "You are a FAITHFUL translator. Translate the English prompt into fluent Korean sentences "
        "(2–4 sentences). DO NOT add or omit any factual detail: subjects, colors (e.g., blue), "
        "materials, absolute/relative positions, style descriptors, mood, and composition. "
        "No lists, no colons, no quotes, no code blocks. Keep everything factual."
    )
    # 핵심 용어 고정 매핑을 힌트로 제공
    mapping = """
Use these fixed Korean mappings:
- upper left → 왼쪽 상단
- lower left → 왼쪽 하단
- upper right → 오른쪽 상단
- lower right → 오른쪽 하단
- to the right of → 오른쪽에
- to the left of → 왼쪽에
- center / centered → 중앙 / 중앙에
- flat, 2D digital illustration → 플랫 2D 디지털 일러스트
- bold outlines → 굵은 윤곽선
- soft, muted colors → 부드럽고 차분한 색조
- playful demeanor → 장난스러운/유쾌한 분위기
"""
    resp = client.chat.completions.create(
        model=model,
        temperature=0.0,  # ✅ 창의성 끔: 누락/변형 방지
        messages=[
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": mapping.strip() + "\n\n" + en_prompt},
        ],
    )
    return resp.choices[0].message.content.strip().replace("```", "").strip()

# =========================
# 편집(한국어 → 영어 edits → 적용 → 영어 정규화)
# =========================
_EDIT_SCHEMA = """
Return JSON with the exact shape:
{
  "edits": [
    {
      "object_match": { "id": "", "label": "" },  // at least one key present
      "set": {
        "label": "",
        "dominant_color_hex": "#ffffff",
        "bbox_norm": { "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0 },
        "attributes": {
          "material": "",
          "pattern": "",
          "shape": "",
          "size": "",
          "other": ""
        }
      },
      "why": ""
    }
  ]
}
Rules:
- All values inside `set` MUST be in ENGLISH only (concise nouns/adjectives).
- Only include fields you intend to change in the `set` object.
- For colors, always set a valid 7-char HEX in `dominant_color_hex`.
- Use existing labels/ids from the provided JSON when possible.
- If the instruction is ambiguous, choose the most likely single target.
"""

# 한국어 색상 → HEX 힌트(모델 가이드용)
_COLOR_HINT = """
Korean color words to HEX mapping (use when user instruction mentions colors):
흰색/하얀색:white=#FFFFFF, 검정/검은색:black=#000000, 빨강/빨간색:red=#FF0000, 파랑/파란색:blue=#0066FF,
초록/초록색:green=#00AA00, 노랑/노란색:yellow=#FFD400, 주황/주황색:orange=#FFA500, 갈색:brown=#8B4513,
회색:gray=#888888, 분홍/분홍색:pink=#FFC0CB, 보라/보라색:purple=#800080, 베이지:beige=#F5DEB3,
은색:silver=#C0C0C0, 금색:gold=#D4AF37, 청록:cyan=#00FFFF, 자홍:magenta=#FF00FF
"""

def propose_edits_via_gpt(current_json: dict, user_instruction_ko: str, api_key: str, model: str = "gpt-4o-mini") -> dict:
    client = OpenAI(api_key=api_key)
    sys_msg = (
        "You convert Korean user edit requests into structured JSON edits to modify an existing analysis JSON. "
        "All output string values inside `set` must be ENGLISH ONLY. Be minimal and precise. Always return valid JSON."
    )
    payload = json.dumps(current_json, ensure_ascii=False)
    user_msg = (
        f"Current analysis JSON:\n{payload}\n\n"
        f"User edit request (Korean): {user_instruction_ko}\n\n"
        f"Use this color mapping when needed:\n{_COLOR_HINT}\n\n"
        f"Return JSON following this schema:\n{_EDIT_SCHEMA}"
    )
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ],
    )
    return json.loads(resp.choices[0].message.content)

def _deep_merge_set(dst: dict, patch: dict):
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge_set(dst[k], v)
        else:
            dst[k] = v

def apply_edits_to_analysis(analysis: dict, edits: dict) -> dict:
    objects = analysis.get("objects", [])
    for e in (edits.get("edits") or []):
        match = (e.get("object_match") or {})
        setter = (e.get("set") or {})
        if not setter:
            continue
        # 대상: id 우선, 없으면 label
        target = None
        if match.get("id"):
            for o in objects:
                if o.get("id") == match["id"]:
                    target = o; break
        if target is None and match.get("label"):
            for o in objects:
                if (o.get("label") or "").lower() == match["label"].lower():
                    target = o; break
        if target is None:
            continue
        if "attributes" in setter and not isinstance(target.get("attributes"), dict):
            target["attributes"] = {}
        _deep_merge_set(target, setter)
    return analysis

def normalize_analysis_to_english(analysis: dict, api_key: str, model: str = "gpt-4o-mini") -> dict:
    """
    안전망: 만약 편집 결과에 한국어가 섞여도 전체 JSON 내 문자열을 영어로 정규화.
    키/숫자/구조는 변경 금지.
    """
    client = OpenAI(api_key=api_key)
    sys_msg = (
        "You will receive a JSON. Return the same JSON but with ALL string values translated to ENGLISH ONLY. "
        "Do not change keys, structure, or any numeric values. Keep arrays and objects intact."
    )
    payload = json.dumps(analysis, ensure_ascii=False)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": payload},
        ],
    )
    return json.loads(resp.choices[0].message.content)

def polish_english_prompt(en_prompt: str, api_key: str, model: str = "gpt-4o-mini") -> str:
    """
    EN 프롬프트를 FLUX 친화적으로 다듬음:
    - 한 문단, 40~80 단어
    - 문법/표현 어색함 수정 (e.g., 'a illustration'→'an illustration', 'in the left'→'on the left')
    - 객체/색/위치/구도/스타일/품질 정보는 추가/삭제하지 않음
    - 콜론/리스트/따옴표/코드블록 금지
    """
    client = OpenAI(api_key=api_key)
    sys_msg = (
        "You are a careful prompt editor for FLUX image generation. "
        "Rewrite the given English prompt into one fluent paragraph (about 40–80 words). "
        "Fix grammar and awkward phrasing; prefer 'on the left/right' not 'in the left/right'; "
        "use 'an illustration' not 'a illustration'. "
        "Keep ALL entities, colors, spatial relations, composition, and style descriptors; "
        "do NOT add or remove facts. No lists, no colons, no quotes, no code blocks."
    )
    resp = client.chat.completions.create(
        model=model, temperature=0.2,
        messages=[{"role": "system", "content": sys_msg},
                  {"role": "user", "content": en_prompt}]
    )
    return resp.choices[0].message.content.strip().replace("```", "").strip()

# def english_to_korean_prompt(en_prompt: str, api_key: str, model: str = "gpt-4o-mini") -> str:
#     """
#     영어 프롬프트를 자연스러운 한국어 문장으로 변환(콜론/리스트 금지, 2~4문장).
#     """
#     client = OpenAI(api_key=api_key)
#     sys_msg = (
#         "You are a professional Korean copywriter for children's picture-book prompts. "
#         "Rewrite the given English prompt into fluent Korean natural sentences for an image generation model. "
#         "Keep spatial relations and composition details. No colons, bullets, or code blocks. 2–4 sentences."
#     )
#     resp = client.chat.completions.create(
#         model=model, temperature=0.2,
#         messages=[{"role": "system", "content": sys_msg},
#                   {"role": "user", "content": en_prompt}]
#     )
#     return resp.choices[0].message.content.strip().replace("```", "").strip()

# 🔧 이미지 생성 함수 (질문에서 주신 그대로)
def generate_image(prompt, idx):
    try:
        print(f"🎨 파인튜닝된 모델로 이미지 생성 시작: {prompt[:50]}...")

        flux_api = os.environ.get("BFL_API_KEY")
        finetune_id = os.environ.get("BFL_FINETUNE_ID")

        response = finetune_inference(
            finetune_id=finetune_id,
            api_key=flux_api,
            endpoint="flux-pro-1.1-ultra-finetuned",
            prompt=prompt,
            width=1024,
            height=768,
        )

        request_id = response.get("id")
        if not request_id:
            print("❌ 요청 ID를 받지 못했습니다.")
            return None

        # 상태 확인 루프
        for attempt in range(60):
            time.sleep(0.5)
            result = get_inference(id=request_id, api_key=flux_api)
            status = result.get("status")

            if status == "Ready":
                image_url = result["result"]["sample"]
                print(f"✅ 이미지 생성 완료! URL: {image_url}")

                image_response = requests.get(image_url)
                image = Image.open(BytesIO(image_response.content))

                os.makedirs("fairy_tale_pictures", exist_ok=True)
                image_path = f"fairy_tale_pictures/image_{idx}.png"
                image.save(image_path)

                print(f"🎉 이미지 저장 완료: {image_path}")
                return image_path

            elif status == "Request Moderated":
                print("🚨 요청이 콘텐츠 필터링에 의해 차단됨.")
                return None
            elif status == "Failed":
                print("❌ 이미지 생성 실패.")
                return None

        print("⏳ 최대 시도 횟수 도달")
        return None

    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        return None

# =========================
# 인터랙티브 실행(단일 이미지)
# =========================
def interactive_session(api_key: str, image_path: str, out_dir="analysis_results",
                        vision_model="gpt-4o-mini", lang_model="gpt-4o-mini", edit_model="gpt-4o-mini",
                        show_en=False):
    ensure_dir(out_dir)
    stem = os.path.splitext(os.path.basename(image_path))[0]

    # 1) 분석
    image_ref = load_image_as_data_url(image_path)
    analysis = call_gpt_vision(image_ref=image_ref, api_key=api_key, model=vision_model)

    # 보정: 누락 색상 백업
    fallback_hex = approx_dominant_hex(image_path)
    if isinstance(analysis, dict) and "objects" in analysis and isinstance(analysis["objects"], list):
        for obj in analysis["objects"]:
            hexv = obj.get("dominant_color_hex")
            if not (isinstance(hexv, str) and hexv.startswith("#") and len(hexv) == 7):
                if fallback_hex:
                    obj["dominant_color_hex"] = fallback_hex

    # 저장(원본 v1, 영어 상태)
    v = 1
    path_json_v = os.path.join(out_dir, f"{stem}_analysis_v{v}.json")
    save_json(analysis, path_json_v)

    # 2) 원본 프롬프트
    en = build_english_prompt(analysis)
    en_polished = polish_english_prompt(en, api_key=api_key, model=lang_model)

    if show_en:
        print(f"\n=== [{os.path.basename(image_path)}] EN prompt (original, polished) ===")
        print(en_polished); print("="*60)

    ko = english_to_korean_prompt_strict(en_polished, api_key=api_key, model=lang_model)
    print(f"\n=== [{os.path.basename(image_path)}] 한국어 프롬프트 (원본) ===")
    print(ko); print("="*60 + "\n")


    # 3) 인터랙티브 편집 루프
    print("수정 지시문을 입력하세요. 예) 강아지는 흰색으로 바꿔줘.  (그만하려면 엔터 또는 'q')")
    current = analysis
    while True:
        try:
            instr = input("> ")
        except KeyboardInterrupt:
            print("\n종료합니다.")
            break

        if not instr or instr.strip().lower() in ("q", "quit", "exit"):
            print("편집 세션 종료.")
            break

        # 3-1) 편집안 생성(한글→영어 edits) → 적용
        try:
            edits = propose_edits_via_gpt(current, instr.strip(), api_key=api_key, model=edit_model)
            current = apply_edits_to_analysis(current, edits)
            # 3-1.5) 영어 정규화(혹시 한국어가 섞였을 경우 대비)
            current = normalize_analysis_to_english(current, api_key=api_key, model=edit_model)
        except Exception as e:
            print(f"[경고] 편집 적용 중 오류: {e}")
            continue

        # 3-2) 버전 업 저장(영어 JSON 보장)
        v += 1
        path_json_v = os.path.join(out_dir, f"{stem}_analysis_v{v}.json")
        save_json(current, path_json_v)
        print(f"[저장] Updated object JSON (EN) → {path_json_v}")

        # 3-3) 수정본: EN → Polish → KO (먼저 출력) → 이미지 생성
        en2 = build_english_prompt(current)
        en2_polished = polish_english_prompt(en2, api_key=api_key, model=lang_model)

        print("\n=== English Prompt (edited, polished) ===")
        print(en2_polished)
        print("=" * 60)

        ko2 = english_to_korean_prompt_strict(en2_polished, api_key=api_key, model=lang_model)
        print("\n=== 한국어 프롬프트 (수정본) ===")
        print(ko2)
        print("=" * 60 + "\n")

        # 그 다음 이미지 생성
        generate_image(en2_polished, v)


        print("추가 수정 지시문을 계속 입력하거나, 엔터로 종료하세요.")


# =========================
# 배치 처리(여러 이미지)
# =========================
def interactive_session_batch(api_key: str, image_paths: list, out_dir="analysis_results",
                              vision_model="gpt-4o-mini", lang_model="gpt-4o-mini", edit_model="gpt-4o-mini",
                              show_en=False, pause_between=True):
    for idx, path in enumerate(image_paths, 1):
        print(f"\n>>> [{idx}/{len(image_paths)}] 자동 선택된 이미지: {os.path.basename(path)}")
        interactive_session(api_key, path, out_dir, vision_model, lang_model, edit_model, show_en=show_en)
        if pause_between and idx < len(image_paths):
            print("\n다음 이미지로 넘어갑니다...\n")

# =========================
# 메인
# =========================
def main():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("환경변수 OPENAI_API_KEY가 설정되지 않았습니다.")

    parser = argparse.ArgumentParser(description="Auto-pick from reference_image, interactive KO edits → EN JSON versioning → EN prompt → KO prompt.")
    parser.add_argument("--image", help="특정 파일만 처리 (reference_image/ 안의 파일명)")
    parser.add_argument("--all", action="store_true", help="폴더 내 모든 이미지를 순차 처리")
    parser.add_argument("--pick", choices=["newest", "oldest", "alpha"], default="newest", help="자동 선택 기준")
    parser.add_argument("--out-dir", default="analysis_results")
    parser.add_argument("--vision-model", default="gpt-4o-mini")
    parser.add_argument("--lang-model", default="gpt-4o-mini")
    parser.add_argument("--edit-model", default="gpt-4o-mini")
    parser.add_argument("--show-en", action="store_true", help="EN prompt도 함께 출력")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    ref_dir = os.path.join(base_dir, "reference_image")

    items = list_reference_images(ref_dir)
    if args.image:
        target_path = os.path.join(ref_dir, args.image)
        if not os.path.exists(target_path):
            raise FileNotFoundError(f"reference_image/{args.image} 을(를) 찾을 수 없습니다.")
        image_paths = [target_path]
        print(f"지정된 이미지 1장을 처리합니다: {args.image}")
    else:
        if not items:
            raise FileNotFoundError("reference_image 폴더에 처리할 이미지가 없습니다.")
        items_sorted = sort_images(items, args.pick)
        if args.all:
            image_paths = [p for (p, n, t) in items_sorted]
            names = ", ".join(os.path.basename(p) for p in image_paths)
            print(f"reference_image에서 {len(image_paths)}장 자동 선택: {names}")
        else:
            image_paths = [items_sorted[0][0]]
            print(f"reference_image에서 자동 선택(기준: {args.pick}): {os.path.basename(image_paths[0])}")

    if len(image_paths) == 1:
        interactive_session(
            api_key=api_key,
            image_path=image_paths[0],
            out_dir=args.out_dir,
            vision_model=args.vision_model,
            lang_model=args.lang_model,
            edit_model=args.edit_model,
            show_en=args.show_en,
        )
    else:
        interactive_session_batch(
            api_key=api_key,
            image_paths=image_paths,
            out_dir=args.out_dir,
            vision_model=args.vision_model,
            lang_model=args.lang_model,
            edit_model=args.edit_model,
            show_en=args.show_en,
        )

if __name__ == "__main__":
    main()
