"""
config.py — GitHub Actions용 경량 설정 스텁.
원본 프로젝트(D:\\Claude\\files)의 main.py 대신 이 파일을 사용한다.
DART/Gemini 등 다른 키는 이 워크플로우에서 쓰지 않으므로 포함하지 않는다.
"""

import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
