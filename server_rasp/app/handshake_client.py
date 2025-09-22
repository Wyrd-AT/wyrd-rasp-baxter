# Arquivo: app/handshake_ws_client.py

import asyncio
import websockets
import json
import logging
from .config import settings
from . import aggregator

logger = logging.getLogger(__name__)

# Fila para enviar desafios do Guardião para este cliente
outbound_queue = asyncio.Queue()

async def send_challenge(payload: dict):
    """Função chamada pelo Guardião para enfileirar um desafio a ser enviado."""
    await outbound_queue.put(payload)

async def run_client():
    """
    Tarefa principal que roda em background, mantendo a conexão WebSocket
    e gerenciando o envio e recebimento de mensagens.
    """
    ws_url = settings.get('handshake_ws_url', 'ws://127.0.0.1:9502')
    
    while True:
        try:
            async with websockets.connect(ws_url) as websocket:
                logger.info(f"[WS-CLIENT] Conectado com sucesso ao servidor de handshake em {ws_url}")

                # Tarefas para enviar e receber mensagens em paralelo
                sender_task = asyncio.create_task(sender(websocket))
                receiver_task = asyncio.create_task(receiver(websocket))

                done, pending = await asyncio.wait(
                    [sender_task, receiver_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
        except Exception as e:
            logger.error(f"[WS-CLIENT] Erro na conexão WebSocket: {e}. Tentando reconectar em 10 segundos...")
            await asyncio.sleep(10)

async def sender(websocket):
    """Pega mensagens da fila e as envia para o servidor final."""
    while True:
        payload = await outbound_queue.get()
        challenge_payload = {**payload, "status": "SERVER"}
        logger.info(f"[WS-CLIENT] Enviando desafio: {challenge_payload}")
        await websocket.send(json.dumps(challenge_payload))

async def receiver(websocket):
    """Ouve por respostas do servidor final e confirma a presença."""
    async for message in websocket:
        try:
            data = json.loads(message)
            logger.info(f"[WS-CLIENT] Resposta recebida: {data}")
            
            nome_cama = data.get("cama")
            status_confirmado = data.get("status")

            if nome_cama and status_confirmado == "TRUE":
                # Precisamos encontrar o MAC da cama a partir do nome
                # Esta é uma operação que acessa o DB, então precisa ser feita com cuidado.
                # Para simplificar, vamos chamar uma função no agregador que já tem o mapa.
                # (Precisaremos adicionar essa função no aggregator.py)
                
                # Vamos simplificar por agora e chamar direto a confirmação no agregador
                # (O ideal seria ter um mapa nome_cama -> mac_beacon no agregador)
                
                from .models import SessionLocal, Asset
                db = SessionLocal()
                try:
                    asset = db.query(Asset).filter(Asset.nome_ativo == nome_cama).first()
                    if asset:
                        aggregator.confirm_asset_by_handshake(asset.mac_beacon)
                    else:
                        logger.warning(f"[WS-CLIENT] Confirmação recebida para a cama '{nome_cama}', mas ela não foi encontrada no DB.")
                finally:
                    db.close()

        except json.JSONDecodeError:
            logger.warning(f"[WS-CLIENT] Mensagem JSON inválida recebida: {message}")
        except Exception as e:
            logger.error(f"[WS-CLIENT] Erro ao processar resposta: {e}")