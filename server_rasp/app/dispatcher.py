# ==============================================================================
# ARQUIVO: dispatcher.py
# ==============================================================================
"""
Propósito do Arquivo:
Envia o resultado final de um evento para o sistema externo (Connecta).

Funções Chave no Fluxo:
- `dispatch_event(evt)`: Recebe os dados de um evento resolvido (ex: "Cama X
  no Quarto Y"), formata em JSON e envia via socket TCP, com tentativas
  automáticas em caso de falha.
"""

import socket
import json
import time
from .config import settings

# --- Seção: Estratégia de Nova Tentativa (Exponential Backoff) ---
# Esta função auxiliar implementa uma estratégia de "backoff exponencial".
# A cada nova tentativa de conexão falha, ela calcula um tempo de espera
# que aumenta exponencialmente (2^1, 2^2, 2^3...), até um limite máximo.
# Isso evita sobrecarregar o serviço de destino com tentativas muito rápidas.
def exponential_backoff(attempt):
    # O tempo de espera dobra a cada tentativa, mas não passa de 30 segundos.
    return min(2 ** attempt, 30)

# --- Seção: Função Principal de Despacho ---
# A função 'dispatch_event' é o coração deste módulo.
# Ela recebe um evento, monta o payload JSON no formato esperado pelo
# sistema de destino, e tenta enviá-lo via socket TCP.
def dispatch_event(evt) -> bool: # Adicionamos a anotação de retorno -> bool
    """
    Envia um evento para o sistema final e retorna True em caso de sucesso
    ou False em caso de falha final após todas as tentativas.
    """
    # ... (a montagem do payload continua igual)
    payload = {
        "quarto": evt.get("quarto"),
        "cama":   evt.get("cama"),
        "status": evt.get("status"),
        "dataOn": evt.get("dataOn"),
        "wifi":   evt.get("wifi")
    }
    msg = json.dumps(payload) + "\n"
    print(f"[dispatch_event] Payload montado: {payload}")

    attempt = 0
    while attempt < 5:
        try:
            attempt += 1
            print(f"[dispatch_event] Tentativa {attempt} de conexão...")
            with socket.create_connection((settings.get("final_ip"), int(settings.get("final_port"))), timeout=5) as sock:
                sock.sendall(msg.encode())
                print(f"[dispatch_event] Payload enviado com sucesso.")
                return True # SUCESSO: Retorna True
        except (socket.timeout, socket.error) as e:
            wait = exponential_backoff(attempt)
            print(f"[dispatch_event] Erro ao enviar (tentativa {attempt}): {e!r}. Aguardando {wait}s.")
            time.sleep(wait)
    else:
        print(f"[dispatch_event] FALHA FINAL após {attempt} tentativas. Payload descartado.")
        return False # FALHA FINAL: Retorna False