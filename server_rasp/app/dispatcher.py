# dispatcher.py

import socket
import json
import time
from .config import FINAL_IP, PORT

# Função de backoff exponencial para reconexão
def exponential_backoff(attempt):
    return min(2 ** attempt, 30)  # Timeout máximo de 30 segundos

def dispatch_event(evt):
    print(f"[dispatch_event] Recebido evt: {evt}")
    
    # — Removido: verificação de presença aqui, pois já é feita no agregador —
    # cama = evt.get("cama")
    # mac  = evt.get("mac_address")
    # print(f"[dispatch_event] Verificando presença da cama {cama} (MAC {mac})...")
    # if not check_presence(mac):
    #     print(f"[dispatch_event] Cama {cama} não está presente. Abortando dispatch.")
    #     return
    # print(f"[dispatch_event] Cama {cama} presente. Preparando payload...")

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
            print(f"[dispatch_event] Tentativa {attempt} de conexão em {FINAL_IP}:{PORT}...")
            with socket.create_connection((FINAL_IP, PORT), timeout=5) as sock:
                sock.sendall(msg.encode())
                print(f"[dispatch_event] Payload enviado com sucesso na tentativa {attempt}.")
            break
        except (socket.timeout, socket.error) as e:
            wait = exponential_backoff(attempt)
            print(f"[dispatch_event] Erro ao enviar (tentativa {attempt}): {e!r}. Aguardando {wait}s para retry.")
            time.sleep(wait)
    else:
        print(f"[dispatch_event] Falha após {attempt} tentativas. Payload descartado.")
