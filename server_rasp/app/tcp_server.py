# tcp_server.py

import asyncio
import json
from datetime import datetime, timezone

from .aggregator import enqueue_event
from .models import SessionLocal, ReceivedEvent
from .config import IP, PORT

HOST = IP

async def handle_client(reader, writer):
    peer_ip = writer.get_extra_info("peername")[0]
    #print(f"[tcp_server] Conexão iniciada de {peer_ip}")
    buffer = b""

    try:
        while not reader.at_eof():
            data = await reader.read(1024)
            if not data:
                break
            buffer += data

            while b"\n" in buffer:
                # ... (o resto do seu código de processamento de JSON fica igual)
                line, buffer = buffer.split(b"\n", 1)
                try:
                    evt = json.loads(line.decode())
                    print(f"[tcp_server] JSON recebido de {peer_ip}: {evt}")

                    # Adiciona ao histórico (código existente)
                    db = SessionLocal()
                    # ... (código para salvar no ReceivedEvent)
                    db.close()

                    # Enfileira para o agregador
                    enqueue_event(evt)
                    #print(f"[tcp_server] Evento enviado ao agregador")

                    # Responde ao cliente
                    writer.write(b"Evento recebido e processado\n")
                    await writer.drain()
                    print(f"[tcp_server] Resposta enviada a {peer_ip}")

                except json.JSONDecodeError:
                    print(f"[tcp_server] JSON inválido de {peer_ip}: {line.decode()}")
                except Exception as e:
                    print(f"[tcp_server] Erro ao processar dados de {peer_ip}: {e}")
    
    # --- INÍCIO DA CORREÇÃO ---
    except (ConnectionResetError, asyncio.CancelledError, ConnectionAbortedError) as e:
        # Apenas regista que o cliente desconectou de forma inesperada.
        print(f"[tcp_server] Conexão com {peer_ip} fechada abruptamente: {type(e).__name__}")
    finally:
        # Tenta fechar o writer de forma segura
        if not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, ConnectionAbortedError):
                pass # Ignora erros que possam acontecer aqui também
        #print(f"[tcp_server] Conexão com {peer_ip} finalizada")

async def start_server():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print(f"[tcp_server] Servidor TCP rodando em {HOST}:{PORT}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(start_server())
