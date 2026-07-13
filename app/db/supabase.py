from __future__ import annotations
from threading import local

from supabase import Client, create_client
from app.core.config import settings

_thread_state = local()


def get_supabase() -> Client:
    # supabase-pyの同期httpx/http2 clientは複数スレッドで共有しない。
    # FastAPIのthread poolとバックグラウンド処理ごとに接続プールを分離する。
    client: Client | None = getattr(_thread_state, "client", None)
    if client is None:
        client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
        _thread_state.client = client
    return client
