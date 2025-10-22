# app/dispatcher.py (VERSÃO FINAL UNIFICADA)

import socket
import json
import time
from .config import settings
import logging

logger = logging.getLogger(__name__)

def _exponential_backoff(attempt):
    """Calcula o tempo de espera, aumentando a cada tentativa até um máximo de 30s."""
    return min(2 ** attempt, 30)

def dispatch_event(evt: dict) -> bool:
    """
    Recebe um dicionário de evento, o converte para JSON e o envia para o 
    sistema final via socket TCP, com múltiplas tentativas em caso de falha.
    Retorna True em caso de sucesso e False em caso de falha final.
    """
    # Converte o dicionário para uma string JSON e adiciona uma quebra de linha,
    # que geralmente é usada como um delimitador de mensagem em sockets.
    payload_json = json.dumps(evt) + "\n"
    logger.info(f"[DISPATCHER] Preparando para enviar payload: {evt}")

    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            logger.info(f"[DISPATCHER] Tentativa {attempt + 1}/{max_attempts} de conexão com {settings.get('final_ip')}:{settings.get('final_port')}...")
            
            # Cria uma conexão de socket, envia os dados e fecha.
            # O 'with' garante que o socket seja fechado mesmo se ocorrer um erro.
            with socket.create_connection((settings.get("final_ip"), int(settings.get("final_port"))), timeout=10) as sock:
                sock.sendall(payload_json.encode('utf-8'))
                logger.info("[DISPATCHER] Payload enviado com sucesso.")
                return True # Sucesso, sai da função.

        except (socket.timeout, socket.error) as e:
            wait_time = _exponential_backoff(attempt)
            logger.warning(f"[DISPATCHER] Erro na tentativa {attempt + 1}: {e}. Aguardando {wait_time}s para tentar novamente.")
            time.sleep(wait_time)
            
    # Se o loop terminar sem sucesso após todas as tentativas.
    logger.error(f"[DISPATCHER] FALHA FINAL após {max_attempts} tentativas. Payload descartado: {evt}")
    return False