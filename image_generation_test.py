from dotenv import load_dotenv
import os
from PIL import Image
import requests
import time
from io import BytesIO
from bfl_finetune import finetune_inference, get_inference

# .env 파일에서 API 키와 파인튜닝 ID 불러오기
load_dotenv()

def generate_image(prompt, idx):
    try:
        print(f"🎨 이미지 생성 시작: {prompt[:50]}...")

        flux_api = os.environ.get("BFL_API_KEY")
        finetune_id = os.environ.get("BFL_FINETUNE_ID")

        response = finetune_inference(
            finetune_id=finetune_id,
            api_key=flux_api,
            endpoint="flux-pro-1.1-ultra-finetuned",  # 또는 사용 중인 다른 endpoint
            prompt=prompt,
            width=1024,
            height=768,
        )

        request_id = response.get("id")
        if not request_id:
            print("❌ 요청 ID를 받지 못했습니다.")
            return None

        for attempt in range(60):
            time.sleep(0.5)
            result = get_inference(id=request_id, api_key=flux_api)
            status = result.get("status")

            if status == "Ready":
                image_url = result["result"]["sample"]
                print(f"✅ 이미지 생성 완료! URL: {image_url}")

                image_response = requests.get(image_url)
                image = Image.open(BytesIO(image_response.content))

                os.makedirs("test_images", exist_ok=True)
                image_path = f"test_images/image_{idx}.png"
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


# 🖼️ 프롬프트 입력
prompt = "The figure in the painting is depicted standing in a bright space. The hair is rendered in a warm brown, and the eyes are a deep gray, giving off a calm impression. The outfit and shoes are uniformly black, creating a sophisticated look. In their hand, they hold a dark brown magic wand, adding a touch of mystery to the scene."
existing_images = os.listdir("test_images") if os.path.exists("test_images") else []
idx = len([f for f in existing_images if f.endswith(".png")]) + 1

# ✅ 이미지 생성 실행
generate_image(prompt, idx)
