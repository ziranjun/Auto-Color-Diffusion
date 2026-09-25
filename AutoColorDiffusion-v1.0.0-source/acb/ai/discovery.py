# -*- coding: utf-8 -*-
"""从服务商的 /models 接口拉取"该 API 真实可调用的模型列表"。

【为什么需要】
    模型 id 是服务商说了算的（而且会随版本变化），写死在配置里就是"占位"。
    保存密钥后调一次 `GET {base_url}/models`，拿到真实 id 填进下拉，
    用户切换模型时用的就一定是这个 API 真实接受的模型名。
"""
from __future__ import annotations

import requests

from ..config import ProviderSpec
from ..logging_setup import get_logger

log = get_logger("ai.discovery")

# 连接 / 读取超时。拉模型列表是很轻的请求，给 10s 连接 + 30s 读取足够，
# 但要避免网络不通时让界面后台线程干等 60s（对话请求才需要那么久）。
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30


def discover_models(provider: ProviderSpec, api_key: str) -> list[str]:
    """调用 `GET {base_url}{models_endpoint}`，返回模型 id 列表。

    兼容两种常见返回形态：
      1. OpenAI 风格：{"object": "list", "data": [{"id": "...", ...}, ...]}
      2. 直接就是数组：[{"id": "...", ...}, ...]

    失败（网络、401、无 data 等）抛异常，由调用方决定如何兜底。
    """
    url = provider.models_url
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider.extra_headers:
        headers.update(provider.extra_headers)

    log.info("正在拉取模型列表：%s", url)
    resp = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
    resp.raise_for_status()
    payload = resp.json()

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("data")
    else:
        items = None

    if not isinstance(items, list):
        raise ValueError(f"服务商返回里没有模型列表（{url}）")

    ids: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    if not ids:
        raise ValueError(f"服务商返回的模型列表为空（{url}）")

    # 去重保序：有些中转站会把同一个模型列两次。
    unique: list[str] = []
    for model_id in ids:
        if model_id not in unique:
            unique.append(model_id)
    log.info("拉到 %d 个模型：%s", len(unique), ", ".join(unique[:20]))
    return unique
