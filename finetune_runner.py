from bfl_finetune import request_finetuning, finetune_progress, finetune_inference, get_inference
import os
import time
from dotenv import load_dotenv
import requests
from PIL import Image
from io import BytesIO

# 환경 변수 로드
load_dotenv()

# zip_path = "/home/younghyeon/My_project/Froebel/finetuning_data/finetuning_images.zip"
# finetune_comment = "My Finetune V1"

# response = request_finetuning(zip_path, finetune_comment, trigger_word="keeez", mode="style", api_key= os.environ.get("BFL_API_KEY"), iterations=150, learning_rate=0.00001, captioning=True, priority="quality", finetune_type="full", lora_rank=32)

# print("파인튜닝 요청 완료!")
# print("🔍 API 응답 내용:", response) 
# print("finetune_id", response["finetune_id"])

flux_api = os.environ.get("BFL_API_KEY")
finetune_id = os.environ.get("BFL_FINETUNE_ID")
status = finetune_progress(finetune_id, flux_api)
print("📊 현재 상태:", status)  # "Pending", "Training", "Ready" 중 하나

# prompt = "A boy play the piano, keeez"
# fine_inference = finetune_inference(
#     finetune_id=finetune_id,
#     finetune_strength=1.2,
#     endpoint="flux-pro-1.1-ultra-finetuned",
#     api_key=flux_api,
#     prompt=prompt,
#     width=1024,
#     height=768
# )

# print(fine_inference)

# id = fine_inference["id"]
# image_load = get_inference(id, flux_api)

# print(image_load)
# inference_id = "9dc01ca1-727d-49e3-87c1-a0a3700f59d2"  # polling_url에서 추출
# max_attempts = 60
# attempt = 0
# image_url = None

# while attempt < max_attempts:
#     time.sleep(1)
#     result = get_inference(inference_id, api_key=flux_api)
#     status = result.get("status")
#     print(f"⏳ [{attempt+1}] 상태: {status}")

#     if status == "Ready":
#         image_url = result["result"]["sample"]
#         print("✅ 이미지 준비 완료! URL:", image_url)
#         break
#     elif status == "Request Moderated":
#         print("❌ 콘텐츠 필터링에 의해 차단됨.")
#         break
#     elif status == "Failed":
#         print("❌ 이미지 생성 실패.")
#         break

#     attempt += 1

# if image_url:
#     image_data = requests.get(image_url).content
#     image = Image.open(BytesIO(image_data))
#     image.save("output_image.png")
#     print("📁 이미지 저장 완료: output_image.png")

