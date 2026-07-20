import os
import re
from openai import OpenAI

def get_openrouter_client():
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("APIKEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OpenRouter API Key (OPENROUTER_API_KEY or APIKEY) is not set in environment variables.")
    
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://github.com/SBCBot",
            "X-Title": "SBCBot Network"
        }
    )

def generate_llm_reply(
    system_instruction: str = None,
    user_prompt: str = None,
    history: list = None,
    image_parts: list = None,  # 互換性のために引数を残していますが画像は処理しません
    model: str = None
) -> str:
    """
    OpenRouter APIを使用してテキストのみでLLM応答を生成する。
    """
    if model is None:
        model = os.getenv("OPENROUTER_MODEL", "tencent/hy3:free")

    client = get_openrouter_client()
    messages = []

    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})

    if history:
        for msg in history:
            role = msg.get("role", "user")
            if role == "model":
                role = "assistant"
            content = msg.get("content", "")
            messages.append({"role": role, "content": content})

    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})
    elif not history:
        messages.append({"role": "user", "content": "こんにちは"})

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
        )
        raw_text = response.choices[0].message.content or ""
        safe_text = re.sub(r"@[\w\-\.]+(?:@[\w\-\.]+)?", "", raw_text).strip()
        return safe_text
    except Exception as e:
        print(f"OpenRouter API error with model '{model}': {e}")
        return f"エラーが発生しちゃった… (エラー内容: {e})"
