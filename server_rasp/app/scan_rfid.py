import logging
import json
import asyncio
import serial_asyncio
from typing import List, Optional
from .config import settings

from fastapi import WebSocket
from sqlalchemy.orm import Session
import serial # Adicionado para tratar a exceção corretamente

# --- MUDANÇA 1: Importações Atualizadas ---
# Removemos 'ProductType'
from .models import InventorySnapshot, InventorySnapshotItem, Item

logger = logging.getLogger(__name__)

async def rfid_scan_task(websocket: WebSocket, mode: str, espaco_id: Optional[int] = None) -> List[str]:
    """
    Tarefa de fundo que ouve as tags lidas.
    Suporta dois modos:
    - 'multiple': Configura o gatilho para leitura contínua.
    - 'single': Executa um único inventário e retorna a tag mais forte.
    """
    PORTA_SERIAL = settings.get('serial_port', 'COM3') 
    tags_lidas = set()
    reader, writer = None, None

    try:
        # A conexão é a mesma para ambos os modos
        reader, writer = await asyncio.wait_for(
            serial_asyncio.open_serial_connection(url=PORTA_SERIAL, baudrate=115200),
            timeout=5.0
        ) 

        # --- A LÓGICA AGORA SE DIVIDE BASEADO NO MODO ---

        if mode == 'single':
            # --- MODO DE LEITURA ÚNICA ---
            await websocket.send_text(json.dumps({"type": "scan_status", "message": "Executando leitura única..."}))
            
            # Comando .iv -fs on: Inventory, Find Strongest only
            # Executa o inventário uma vez e retorna apenas a tag com o sinal mais forte.
            writer.write(b'.iv -fs on\r\n')
            await writer.drain()

            # Loop para ler a resposta do comando, que é finita
            while True:
                linha_bytes = await asyncio.wait_for(reader.readline(), timeout=10.0) # Timeout de 10s para a resposta
                linha = linha_bytes.decode('ascii').strip()

                if linha.startswith('EP:'): # Encontrou a tag
                    tag_id = linha[4:]
                    if tag_id not in tags_lidas:
                        tags_lidas.add(tag_id)
                        await websocket.send_text(json.dumps({"type": "tag_scanned", "tag_id": tag_id})) 
                
                # O comando finalizou, podemos sair do loop
                if linha.startswith('OK:') or linha.startswith('ER:'):
                    break
            
            # A tarefa termina aqui para o modo 'single'

        elif mode == 'multiple':
            # --- MODO DE LEITURA MÚLTIPLA (O CÓDIGO QUE JÁ TÍNHAMOS) ---
            # Comando .sa -s inv: Switch Action, Simple press, Inventory
            # Configura o gatilho físico para iniciar/parar o inventário
            writer.write(b'.sa -s inv\r\n') 
            await writer.drain() 
            await websocket.send_text(json.dumps({"type": "scan_status", "message": "Modo de gatilho ativado. Pressione para ler."})) 
            
            # Loop infinito que só é interrompido pelo cancelamento (botão 'parar' ou 'cancelar')
            while True:
                linha_bytes = await asyncio.wait_for(reader.readline(), timeout=300.0) 
                linha = linha_bytes.decode('ascii').strip()

                if linha.startswith('EP:'): 
                    tag_id = linha[4:]
                    if tag_id not in tags_lidas:
                        tags_lidas.add(tag_id)
                        await websocket.send_text(json.dumps({"type": "tag_scanned", "tag_id": tag_id})) 
        
        else:
            await websocket.send_text(json.dumps({"type": "scan_error", "message": f"Erro: Modo de scan '{mode}' desconhecido."}))

    except asyncio.TimeoutError:
        await websocket.send_text(json.dumps({"type": "scan_status", "message": "Sessão finalizada por inatividade."})) 
    except (serial.SerialException, FileNotFoundError):
        await websocket.send_text(json.dumps({"type": "hardware_error", "message": f"ERRO DE HARDWARE: Verifique se a pistola RFID está conectada na porta '{PORTA_SERIAL}'."})) 
    except asyncio.CancelledError:
        logger.info("Tarefa de scan foi cancelada pelo usuário.") 
        raise
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "scan_error", "message": f"Erro inesperado: {e}"})) 
    finally:
        logger.info("Finalizando tarefa de scan e limpando recursos.") 
        if writer and not writer.is_closing():
            # Envia o comando Abort para garantir que qualquer operação pare
            writer.write(b'.ab\r\n')
            await writer.drain()
            writer.close()
            logger.info(f"Porta serial {PORTA_SERIAL} fechada.")
        
        await websocket.send_text(json.dumps({"type": "scan_status", "message": "Sessão de leitura finalizada."})) 
        return list(tags_lidas) 

async def check_reader_health(websocket: WebSocket):
    """
    Tenta se comunicar com o leitor RFID para verificar se ele está conectado e respondendo.
    """
    PORTA_SERIAL = settings.get('serial_port', 'COM3')
    reader, writer = None, None
    try:
        await websocket.send_text(json.dumps({"type": "reader_status", "status": "CHECKING", "message": "Verificando conexão com o leitor..."}))
        
        # 1. Tenta abrir a porta serial
        reader, writer = await asyncio.wait_for(
            serial_asyncio.open_serial_connection(url=PORTA_SERIAL, baudrate=115200),
            timeout=3.0 # Timeout curto de 3 segundos
        )

        # 2. Envia um comando simples que espera uma resposta
        # NOTA: '.vr' é um comando comum para pedir a versão do firmware.
        #       Consulte o manual da sua pistola para o comando correto de status/versão.
        writer.write(b'.vr\r\n')
        await writer.drain()

        # 3. Aguarda uma resposta
        # Se o leitor responder qualquer coisa, consideramos que ele está funcionando.
        await asyncio.wait_for(reader.readline(), timeout=3.0)

        # 4. Se tudo deu certo, envia a mensagem de sucesso
        await websocket.send_text(json.dumps({"type": "reader_status", "status": "OK", "message": f"Leitor conectado e respondendo na porta {PORTA_SERIAL}."}))
        logger.info(f"Verificação de saúde do leitor na porta {PORTA_SERIAL} bem-sucedida.")

    except (serial.SerialException, FileNotFoundError):
        msg = f"FALHA: Leitor não encontrado na porta '{PORTA_SERIAL}'. Verifique a conexão e o arquivo config.ini."
        await websocket.send_text(json.dumps({"type": "reader_status", "status": "ERROR", "message": msg}))
    except asyncio.TimeoutError:
        msg = f"FALHA: O leitor na porta '{PORTA_SERIAL}' foi encontrado, mas não está respondendo. Verifique se está ligado."
        await websocket.send_text(json.dumps({"type": "reader_status", "status": "ERROR", "message": msg}))
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "reader_status", "status": "ERROR", "message": f"Erro inesperado: {e}"}))
    finally:
        if writer and not writer.is_closing():
            writer.close()

def save_tags_as_inventory(db: Session, espaco_id: int, tags: List[str]):
    """Pega uma lista de tags e salva como um novo snapshot de inventário no espaço informado."""
    if not tags:
        logger.info("Nenhuma tag para salvar no inventário.")
        return False
    
    try:
        snapshot = InventorySnapshot(espaco_id=espaco_id)
        db.add(snapshot)
        db.flush()

        tag_list = []
        for codigo in tags:
            code = codigo.strip().upper()
            if not code:
                continue
            tag_list.append(code)

        items_map = {
            it.codigo_rfid: it
            for it in db.query(Item).filter(Item.codigo_rfid.in_(tag_list)).all()
        }

        entradas = []
        for code in tag_list:
            entradas.append(
                InventorySnapshotItem(
                    snapshot_id=snapshot.id,
                    codigo_rfid=code,
                    item_id=items_map.get(code).id if code in items_map else None,
                )
            )

        db.add_all(entradas)
        db.commit()
        logger.info(f"{len(entradas)} tags salvas no snapshot {snapshot.id}")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao salvar inventário do scan RFID: {e}")
        db.rollback()
        return False
