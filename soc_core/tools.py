from __future__ import annotations

import requests


class WebTools:
    def __init__(self, serper_api_key: str | None = None, tavily_api_key: str | None = None):
        self.serper_api_key = serper_api_key
        self.tavily_api_key = tavily_api_key

    def serper_search(self, query: str, num: int = 5) -> list[dict]:
        if not self.serper_api_key:
            return []
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": self.serper_api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("organic", []) or []

    def tavily_search(self, query: str, max_results: int = 5) -> list[dict]:
        if not self.tavily_api_key:
            return []
        r = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": self.tavily_api_key, "query": query, "max_results": max_results},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("results", []) or []

