# tcp_server.py

import asyncio
import json
from datetime import datetime, timezone

from .aggregator import enqueue_event
from .models import SessionLocal, ReceivedEvent

HOST = "0.0.0.0"
PORT = 9500

async def handle_client(reader, writer):
    peer_ip = writer.get_extra_info("peername")[0]
    print(f"[tcp_server] Conexão iniciada de {peer_ip}")
    buffer = b""

    try:
        while not reader.at_eof():
            data = await reader.read(1024)
            if not data:
                break
            buffer += data

            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    evt = json.loads(line.decode())
                    print(f"[tcp_server] JSON recebido de {peer_ip}: {evt}")

                    # Converte dataOn para UTC-aware
                    ds = evt.get("dataOn")
                    data_on = None
                    if ds:
                        dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
                        data_on = dt.astimezone(timezone.utc)

                    # Persiste no histórico usando data_on
                    db = SessionLocal()
                    rec = ReceivedEvent(
                        esp_id  = evt["esp_id"],
                        cama    = evt["cama"],
                        status  = evt["status"],
                        rssi    = int(evt.get("RSSI", 0)),
                        wifi    = int(evt.get("wifi", 0)),
                        data_on = data_on or datetime.now(timezone.utc),
                        raw     = evt
                    )
                    db.add(rec)
                    db.commit()
                    print(f"[tcp_server] Evento registrado id={rec.id}")

                    # Enfileira para o agregador
                    enqueue_event(evt)
                    print(f"[tcp_server] Evento enviado ao agregador")

                    # Responde ao cliente
                    writer.write(b"Evento recebido e processado\n")
                    await writer.drain()
                    print(f"[tcp_server] Resposta enviada a {peer_ip}")

                except json.JSONDecodeError:
                    print(f"[tcp_server] JSON inválido de {peer_ip}: {line.decode()}")
                except Exception as e:
                    print(f"[tcp_server] Erro ao processar dados de {peer_ip}: {e}")
    except (ConnectionResetError, asyncio.CancelledError) as e:
        print(f"[tcp_server] Conexão com {peer_ip} fechada ou cancelada: {e}")
    finally:
        writer.close()
        await writer.wait_closed()
        print(f"[tcp_server] Conexão com {peer_ip} finalizada")

async def start_server():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print(f"[tcp_server] Servidor TCP rodando em {HOST}:{PORT}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(start_server())
