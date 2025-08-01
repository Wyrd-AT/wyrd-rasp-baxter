# app/connection_manager.py
import uuid
from typing import Dict
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        # Usamos um dicionário para guardar {client_id: websocket}
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket) -> str:
        """Aceita uma nova conexão, gera um ID e guarda-a."""
        await websocket.accept()
        client_id = str(uuid.uuid4()) # Gera um ID único
        self.active_connections[client_id] = websocket
        return client_id

    def disconnect(self, client_id: str):
        """Remove uma conexão pelo seu ID."""
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_to_client(self, client_id: str, message: str):
        """Envia uma mensagem para um cliente específico."""
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(message)

    async def broadcast(self, message: str):
        """Envia uma mensagem para TODOS os clientes."""
        for connection in self.active_connections.values():
            await connection.send_text(message)

manager = ConnectionManager()