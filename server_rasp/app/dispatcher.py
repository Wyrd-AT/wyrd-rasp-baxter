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
def dispatch_event(evt):
    # 1. Montagem do Payload:
    # Filtra e organiza os dados do evento em um dicionário que será
    # convertido para JSON. Um caractere de nova linha (\n) é adicionado
    # ao final, um requisito comum para delimitadores de mensagem em sockets.
    payload = {
        "quarto": evt.get("quarto"),
        "cama":   evt.get("cama"),
        "status": evt.get("status"),
        "dataOn": evt.get("dataOn"),
        "wifi":   evt.get("wifi")
    }
    msg = json.dumps(payload) + "\n"
    print(f"[dispatch_event] Payload montado: {payload}")

    # 2. Loop de Tentativas de Envio:
    # Tenta enviar a mensagem até 5 vezes. Se a conexão falhar,
    # ele usa a função 'exponential_backoff' para esperar antes de tentar
    # novamente. Se todas as 5 tentativas falharem, a mensagem é descartada
    # e um log de falha é registrado.
    attempt = 0
    while attempt < 5:
        try:
            attempt += 1
            print(f"[dispatch_event] Tentativa {attempt} de conexão em {settings.get('final_ip')}:{settings.get('final_port')}...")
            # Tenta criar uma conexão de socket com o IP e Porta definidos no config.ini.
            with socket.create_connection((settings.get("final_ip"), settings.get("final_port")), timeout=5) as sock:
                # Se a conexão for bem-sucedida, envia a mensagem (codificada em bytes).
                sock.sendall(msg.encode())
                print(f"[dispatch_event] Payload enviado com sucesso na tentativa {attempt}.")
            break # Sai do loop se o envio for bem-sucedido.
        except (socket.timeout, socket.error) as e:
            # Se a conexão falhar (timeout ou outro erro de socket).
            wait = exponential_backoff(attempt)
            print(f"[dispatch_event] Erro ao enviar (tentativa {attempt}): {e!r}. Aguardando {wait}s para retry.")
            time.sleep(wait) # Espera o tempo calculado antes da próxima tentativa.
    else:
        # Este 'else' pertence ao 'while'. Ele só é executado se o loop terminar
        # sem um 'break', ou seja, se todas as tentativas falharem.
        print(f"[dispatch_event] Falha após {attempt} tentativas. Payload descartado.")