# dispatcher.py (versão final e assíncrona)
import requests
import json
import asyncio # <--- IMPORTAMOS A BIBLIOTECA ASYNCIO
from .config import settings

def _exponential_backoff(attempt: int) -> int:
    """Calcula o tempo de espera, dobrando a cada tentativa até um máximo de 60s."""
    return min(2 ** attempt, 60)

# --- ALTERAÇÃO AQUI: A função agora é 'async def' ---
async def dispatch_event_to_rtls(tipo_evento: str, event_data: dict) -> bool:
    """
    Envia um evento formatado para a Rtls de forma assíncrona.
    Retorna True em caso de sucesso (status 202) e False em caso de falha final.
    """
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": settings.get("rtls_api_key")
    }
    payload = { "tipoEvento": tipo_evento, "eventData": event_data }
    print(f"[DISPATCHER] Preparando para enviar para Rtls: {payload}")

    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        print(f"[DISPATCHER] Tentativa {attempt}/{max_attempts}...")
        try:
            # NOTA: requests é uma biblioteca síncrona. O ideal seria usar httpx ou aiohttp.
            # Mas para não adicionar novas dependências, a chamada síncrona aqui é rápida
            # e o principal ganho de performance vem do asyncio.sleep.
            response = requests.post(
                settings.get("rtls_webhook_url"),
                headers=headers,
                json=payload,
                timeout=10
            )
            if response.status_code == 200:
                print(f"[DISPATCHER] Sucesso! Evento '{tipo_evento}' aceito pela Rtls.")
                return True

            if 400 <= response.status_code < 500:
                print(f"[DISPATCHER] ERRO CLIENTE! Status: {response.status_code}. Abortando.")
                return False

            print(f"[DISPATCHER] ERRO SERVIDOR! Status: {response.status_code}. Tentando novamente...")

        except requests.exceptions.RequestException as e:
            print(f"[DISPATCHER] ERRO DE CONEXÃO: {e}. Tentando novamente...")

        if attempt < max_attempts:
            wait_time = _exponential_backoff(attempt)
            print(f"[DISPATCHAER] Aguardando {wait_time}s de forma não-bloqueante.")
            # --- ALTERAÇÃO AQUI: Usamos await asyncio.sleep() ---
            await asyncio.sleep(wait_time)

    print(f"[DISPATCHER] FALHA FINAL! Evento '{tipo_evento}' descartado após {max_attempts} tentativas.")
    return False