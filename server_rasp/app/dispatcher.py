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

import logging
logger = logging.getLogger(__name__)

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
def dispatch_event(evt: dict) -> bool: # O parâmetro 'evt' é o dicionário completo
    """
    Envia um evento pré-formatado para o sistema final.
    """
    # --- INÍCIO DA CORREÇÃO ---
    # Remove a recriação do payload. Agora, 'evt' já é o payload final.
    payload = evt
    # --- FIM DA CORREÇÃO ---

    msg = json.dumps(payload) + "\n"
    logger.info(f"[dispatch_event] Payload montado: {payload}")

    attempt = 0
    while attempt < 5:
        try:
            attempt += 1
            logger.info(f"[dispatch_event] Tentativa {attempt} de conexão...")
            with socket.create_connection((settings.get("final_ip"), int(settings.get("final_port"))), timeout=5) as sock:
                sock.sendall(msg.encode())
                logger.info(f"[dispatch_event] Payload enviado com sucesso.")
                return True
        except (socket.timeout, socket.error) as e:
            wait = exponential_backoff(attempt)
            logger.info(f"[dispatch_event] Erro ao enviar (tentativa {attempt}): {e!r}. Aguardando {wait}s.")
            time.sleep(wait)
    else:
        logger.info(f"[dispatch_event] FALHA FINAL após {attempt} tentativas. Payload descartado.")
        return False
    

# import requests
# import json
# import time
# import logging
# from .config import settings

# logger = logging.getLogger(__name__)

# def exponential_backoff(attempt):
#     return min(2 ** attempt, 30)

# def dispatch_event(evt: dict) -> bool:
#     """
#     Envia o evento via HTTP POST para o sistema externo.
#     """
#     # Monta a URL (Assumindo http, você pode ajustar para https se tiver certificado)
#     # Você pode adicionar um path específico no config se precisar, ex: /api/v1/eventos
#     ip = settings.get("final_ip")
#     port = settings.get("final_port")
    
#     # URL de destino (Ex: http://192.168.0.50:9500/receber_evento)
#     # Se não tiver endpoint específico, deixe apenas a raiz ou ajuste aqui:
#     url = f"http://{ip}:{port}/integration/event" 

#     logger.info(f"[DISPATCH-HTTP] Preparando envio para {url}. Payload: {evt}")

#     attempt = 0
#     while attempt < 5:
#         try:
#             attempt += 1
#             # Envia o POST. O parâmetro 'json=' já faz o dumps e põe o header application/json
#             response = requests.post(url, json=evt, timeout=5)
            
#             # Verifica se o servidor respondeu com sucesso (200-299)
#             if response.status_code >= 200 and response.status_code < 300:
#                 logger.info(f"[DISPATCH-HTTP] Sucesso! Resposta: {response.status_code}")
#                 return True
#             else:
#                 logger.warning(f"[DISPATCH-HTTP] Falha. Servidor respondeu: {response.status_code} - {response.text}")
#                 # Dependendo da lógica, você pode querer tentar de novo ou desistir aqui
#                 # Vamos contar como erro para tentar de novo no loop
#                 raise requests.exceptions.RequestException(f"Status ruim: {response.status_code}")

#         except requests.exceptions.RequestException as e:
#             wait = exponential_backoff(attempt)
#             logger.info(f"[DISPATCH-HTTP] Erro na tentativa {attempt}: {e}. Aguardando {wait}s.")
#             time.sleep(wait)
            
#     else:
#         logger.error(f"[DISPATCH-HTTP] FALHA FINAL após {attempt} tentativas. Evento descartado.")
#         return False