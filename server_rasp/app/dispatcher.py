# dispatcher.py
import requests
import json
from .config import ERITEL_WEBHOOK_URL, ERITEL_API_KEY

def dispatch_event_to_eritel(tipo_evento: str, event_data: dict):
    """
    Envia um evento formatado para o Webhook Dispatcher da Eritel.
    """
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": ERITEL_API_KEY
    }

    payload = {
        "tipoEvento": tipo_evento,
        "eventData": event_data
    }

    print(f"[DISPATCHER] Enviando para Eritel: {payload}")

    try:
        response = requests.post(
            ERITEL_WEBHOOK_URL,
            headers=headers,
            json=payload,
            timeout=10 # Timeout de 10 segundos
        )

        # A Eritel espera um 202 Accepted. Qualquer outra coisa é um problema.
        if response.status_code == 202:
            print(f"[DISPATCHER] Sucesso! Evento '{tipo_evento}' aceito pela Eritel.")
        else:
            print(f"[DISPATCHER] ERRO! Status: {response.status_code}, Resposta: {response.text}")

    except requests.exceptions.RequestException as e:
        print(f"[DISPATCHER] ERRO DE CONEXÃO: Falha ao enviar evento para Eritel. Erro: {e}")