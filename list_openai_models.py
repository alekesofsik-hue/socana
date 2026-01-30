#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_dotenv(dotenv_path: Path) -> dict[str, str]:
    """
    Minimal .env parser:
    - KEY=VALUE
    - ignores empty lines and comments (# ...)
    - supports quoted values: KEY="value" or KEY='value'
    """
    if not dotenv_path.exists():
        raise FileNotFoundError(f".env not found рядом со скриптом: {dotenv_path}")

    env: dict[str, str] = {}
    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        if (len(v) >= 2) and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        env[k] = v
    return env


def fetch_models(api_key: str, base_url: str = "https://api.openai.com") -> list[str]:
    url = base_url.rstrip("/") + "/v1/models"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(f"HTTP {e.code} при запросе моделей: {body}".strip()) from e
    except Exception as e:
        raise RuntimeError(f"Ошибка запроса моделей: {e}") from e

    try:
        data = json.loads(payload)
    except Exception as e:
        raise RuntimeError(f"Не смог распарсить JSON ответа /v1/models: {e}\nRaw: {payload[:3000]}") from e

    items = data.get("data") or []
    ids = []
    for m in items:
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            ids.append(mid)
    return sorted(set(ids))


def main() -> int:
    p = argparse.ArgumentParser(description="Print available OpenAI model ids (reads .env next to this script).")
    p.add_argument("--prefix", default="", help="Filter model ids by prefix (e.g. gpt-).")
    p.add_argument("--json", action="store_true", help="Output as JSON array.")
    p.add_argument(
        "--base-url",
        default=None,
        help="Optional API base URL (default: https://api.openai.com). You may also set OPENAI_BASE_URL in .env.",
    )
    args = p.parse_args()

    dotenv_path = Path(__file__).resolve().parent / ".env"
    env = load_dotenv(dotenv_path)

    api_key = env.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: OPENAI_API_KEY is missing or empty in .env", file=sys.stderr)
        return 2

    base_url = (args.base_url or env.get("OPENAI_BASE_URL") or "https://api.openai.com").strip()

    ids = fetch_models(api_key=api_key, base_url=base_url)
    if args.prefix:
        ids = [i for i in ids if i.startswith(args.prefix)]

    if args.json:
        print(json.dumps(ids, ensure_ascii=False, indent=2))
    else:
        for i in ids:
            print(i)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

