# ==============================================================================
# ARQUIVO: app/dispatcher.py (VERSÃO HTTP)
# ==============================================================================
import requests
import json
import time
import logging
from .config import settings

logger = logging.getLogger(__name__)

def exponential_backoff(attempt):
    """Calcula tempo de espera: 2s, 4s, 8s, 16s, 30s (max)."""
    return min(2 ** attempt, 30)

def dispatch_event(evt: dict) -> bool:
    """
    Envia o evento via HTTP POST para o sistema externo usando
    o IP e Porta definidos no arquivo de configuração.
    """
    # 1. Carrega configurações
    ip = settings.get("final_ip")
    port = settings.get("final_port")
    
    if not ip or not port:
        logger.error("[DISPATCH-HTTP] Erro: 'final_ip' ou 'final_port' não configurados no config.ini")
        return False

    # 2. Monta a URL de destino
    # Ajuste o caminho '/integration/event' se o seu servidor esperar outro endpoint
    url = f"http://{ip}:{port}/rtls/" 

    logger.info(f"[DISPATCH-HTTP] Preparando envio para {url}. Payload: {evt}")

    attempt = 0
    max_attempts = 5

    while attempt < max_attempts:
        try:
            attempt += 1
            
            # 3. Envia o POST
            # O parâmetro json=evt faz a serialização automática e adiciona o header Content-Type: application/json
            response = requests.post(url, json=evt, timeout=5)
            
            # 4. Verifica Sucesso (Códigos 200 a 299)
            if 200 <= response.status_code < 300:
                logger.info(f"[DISPATCH-HTTP] Sucesso! Servidor respondeu: {response.status_code}")
                return True
            else:
                # Se o servidor responder erro (ex: 404, 500), loga e tenta de novo
                logger.warning(f"[DISPATCH-HTTP] Falha na tentativa {attempt}. Status: {response.status_code} - Corpo: {response.text}")
                # Força uma exceção para cair no bloco except e aguardar o backoff
                raise requests.exceptions.RequestException(f"Status HTTP inválido: {response.status_code}")

        except requests.exceptions.RequestException as e:
            wait = exponential_backoff(attempt)
            logger.info(f"[DISPATCH-HTTP] Erro de conexão na tentativa {attempt}: {e}. Aguardando {wait}s...")
            time.sleep(wait)
            
    # Se sair do loop, falhou todas as vezes
    logger.error(f"[DISPATCH-HTTP] FALHA FINAL após {attempt} tentativas. Evento descartado.")
    return False