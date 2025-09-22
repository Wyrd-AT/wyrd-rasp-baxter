# Arquivo: app/handshake_tcp_client.py

import asyncio
import json
import logging
from .config import settings

logger = logging.getLogger(__name__)

# Usaremos as mesmas configurações de IP e Porta do dispatcher
FINAL_IP = settings.get("final_ip")
# Para o seu teste, vamos usar a porta 9502 como pediu
FINAL_PORT = settings.get("handshake_tcp_port", 9502) 

async def send_challenge(payload: dict):
    """
    Abre uma conexão TCP e envia um desafio "fire-and-forget".
    Não espera por uma resposta nesta conexão.
    """
    challenge_payload = {**payload, "status": "SERVER"}
    writer = None  # Define writer como None inicialmente
    try:
        logger.debug(f"[TCP-CHALLENGE] Conectando a {FINAL_IP}:{FINAL_PORT} para enviar desafio...")
        # Abre a conexão com um timeout para a própria conexão
        _, writer = await asyncio.wait_for(asyncio.open_connection(FINAL_IP, FINAL_PORT), timeout=5.0)

        message_to_send = json.dumps(challenge_payload) + '\n'
        logger.info(f"[TCP-CHALLENGE] Enviando desafio: {message_to_send.strip()}")
        
        writer.write(message_to_send.encode())
        await writer.drain()
        
    except Exception as e:
        logger.error(f"[TCP-CHALLENGE] Falha ao enviar desafio: {e}")
    finally:
        # Garante que a conexão seja sempre fechada após o envio
        if writer and not writer.is_closing():
            writer.close()
            await writer.wait_closed()
            logger.debug(f"[TCP-CHALLENGE] Conexão de envio fechada.")