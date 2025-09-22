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

async def send_challenge_and_get_response(payload: dict) -> bool:
    """
    Abre uma conexão TCP, envia um desafio JSON, aguarda uma resposta JSON,
    e retorna True se a resposta for bem-sucedida, senão False.
    """
    challenge_payload = {**payload, "status": "SERVER"}
    
    try:
        logger.debug(f"[TCP-CLIENT] Conectando a {FINAL_IP}:{FINAL_PORT}...")
        reader, writer = await asyncio.open_connection(FINAL_IP, FINAL_PORT)

        # Prepara a mensagem de desafio
        message_to_send = json.dumps(challenge_payload) + '\n'
        logger.info(f"[TCP-CLIENT] Enviando desafio: {message_to_send.strip()}")
        
        # Envia a mensagem
        writer.write(message_to_send.encode())
        await writer.drain()

        # Aguarda pela resposta com um timeout de 5 segundos
        try:
            response_data = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if response_data:
                response_text = response_data.decode().strip()
                logger.info(f"[TCP-CLIENT] Resposta recebida: {response_text}")
                
                response_json = json.loads(response_text)
                # Verifica se a resposta é a confirmação que esperamos
                if response_json.get("status") == "TRUE":
                    return True # SUCESSO!

            return False # Resposta vazia ou não confirmada

        except asyncio.TimeoutError:
            logger.warning(f"[TCP-CLIENT] Timeout: Nenhuma resposta recebida de {FINAL_IP}:{FINAL_PORT} em 5 segundos.")
            return False
        except json.JSONDecodeError:
            logger.warning(f"[TCP-CLIENT] Resposta não era um JSON válido.")
            return False

    except ConnectionRefusedError:
        logger.error(f"[TCP-CLIENT] Conexão recusada por {FINAL_IP}:{FINAL_PORT}.")
        return False
    except Exception as e:
        logger.error(f"[TCP-CLIENT] Erro inesperado na comunicação TCP: {e}")
        return False
    finally:
        # Garante que a conexão seja sempre fechada
        if 'writer' in locals() and not writer.is_closing():
            writer.close()
            await writer.wait_closed()