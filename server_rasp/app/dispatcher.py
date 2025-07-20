# dispatcher.py

import socket
import json
import time
from .config import settings

# Função de backoff exponencial para reconexão
def exponential_backoff(attempt):
    return min(2 ** attempt, 30)  # Timeout máximo de 30 segundos

def dispatch_event(evt):
    #print(f"[dispatch_event] Recebido evt: {evt}")

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
            print(f"[dispatch_event] Tentativa {attempt} de conexão em {settings.get("final_ip")}:{settings.get("final_port")}...")
            with socket.create_connection((settings.get("final_ip"), settings.get("final_port")), timeout=5) as sock:
                sock.sendall(msg.encode())
                print(f"[dispatch_event] Payload enviado com sucesso na tentativa {attempt}.")
            break
        except (socket.timeout, socket.error) as e:
            wait = exponential_backoff(attempt)
            print(f"[dispatch_event] Erro ao enviar (tentativa {attempt}): {e!r}. Aguardando {wait}s para retry.")
            time.sleep(wait)
    else:
        print(f"[dispatch_event] Falha após {attempt} tentativas. Payload descartado.")
