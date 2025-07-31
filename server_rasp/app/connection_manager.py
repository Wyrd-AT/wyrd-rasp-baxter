# connection_manager.py
import logging
logger = logging.getLogger(__name__)
from fastapi import WebSocket
from typing import List

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """Aceita uma nova conexão."""
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        """Remove uma conexão da lista."""
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        """Envia uma mensagem para TODAS as conexões ativas."""
        logger.info(f"[WebSocket] Transmitindo mensagem para {len(self.active_connections)} clientes: {message}")
        for connection in self.active_connections:
            await connection.send_text(message)

# Cria uma instância única que será usada em toda a aplicação
manager = ConnectionManager()