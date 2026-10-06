import os
import time
from dotenv import load_dotenv
from ollama import Client

load_dotenv(override=True)

OLLAMA_HOST = os.getenv("OLLAMA_HOST")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")

client = Client(
    host=OLLAMA_HOST,
    headers={
        "Authorization": f"Bearer {OLLAMA_API_KEY}"
    }
)


def list_models():
    response = client.list()
    return [m.model for m in response.models]


def llm_with_meta(prompt, model=None, json_mode=False, temperature=0):
    """Call the model and also return latency and token counts (used for evaluation metrics)."""
    model = model or OLLAMA_MODEL
    start = time.time()
    response = client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        format="json" if json_mode else None,
        options={"temperature": temperature},
    )
    return {
        "text": response["message"]["content"],
        "model": model,
        "latency_ms": int((time.time() - start) * 1000),
        "prompt_tokens": response.get("prompt_eval_count") or 0,
        "output_tokens": response.get("eval_count") or 0,
    }


def llm(prompt, model=None, json_mode=False, temperature=0):
    """Simple version: return only the model's text reply."""
    return llm_with_meta(prompt, model, json_mode, temperature)["text"]