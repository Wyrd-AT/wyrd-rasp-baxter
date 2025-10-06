import logging
import json
import asyncio
import serial_asyncio
from typing import List
from .config import settings

from fastapi import WebSocket
from sqlalchemy.orm import Session
import serial # Adicionado para tratar a exceção corretamente

# --- MUDANÇA 1: Importações Atualizadas ---
# Removemos 'ProductType'
from .models import InventorySnapshot, InventoryItem, Product

logger = logging.getLogger(__name__)

async def rfid_scan_task(websocket: WebSocket, datacenter_id: int) -> List[str]:
    """
    Tarefa de fundo que coloca a pistola em modo de gatilho e ouve as tags lidas.
    """
    PORTA_SERIAL = settings.get('serial_port', 'COM3')
    tags_lidas = set()
    reader, writer = None, None

    try:
        reader, writer = await asyncio.wait_for(
            serial_asyncio.open_serial_connection(url=PORTA_SERIAL, baudrate=115200),
            timeout=5.0
        )

        writer.write(b'.sa -s inv\r\n')
        await writer.drain()
        await websocket.send_text(json.dumps({"type": "scan_status", "message": "Modo de gatilho ativado. Pressione o gatilho para ler."}))
        
        while True:
            linha_bytes = await asyncio.wait_for(reader.readline(), timeout=300.0)
            linha = linha_bytes.decode('ascii').strip()

            if linha.startswith('EP:'):
                tag_id = linha[4:]
                if tag_id not in tags_lidas:
                    tags_lidas.add(tag_id)
                    await websocket.send_text(json.dumps({"type": "tag_scanned", "tag_id": tag_id}))
    
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
            writer.write(b'.sa -s off\r\n')
            await writer.drain()
            writer.close()
            logger.info(f"Porta serial {PORTA_SERIAL} fechada.")
        
        await websocket.send_text(json.dumps({"type": "scan_status", "message": "Sessão de leitura finalizada."}))
        return list(tags_lidas)

def save_tags_as_inventory(db: Session, datacenter_id: int, tags: List[str]):
    """Pega uma lista de tags e salva como um novo snapshot de inventário."""
    if not tags:
        logger.info("Nenhuma tag para salvar no inventário.")
        return False
    
    try:
        produtos_processados = []
        
        # --- MUDANÇA 2: Lógica de 'default_tipo' Removida ---
        # Não precisamos mais nos preocupar com o tipo do produto aqui.

        for codigo_rfid in tags:
            produto = db.query(Product).filter(Product.codigo_rfid == codigo_rfid).first()
            if not produto:
                # --- MUDANÇA 3: Criação do Produto Simplificada ---
                # Apenas criamos o produto com o código. A associação com um equipamento
                # será feita posteriormente na página de cadastro.
                produto = Product(codigo_rfid=codigo_rfid)
                db.add(produto)
            produtos_processados.append(produto)
        db.flush()

        novo_snapshot = InventorySnapshot(datacenter_id=datacenter_id)
        db.add(novo_snapshot)
        db.flush()

        itens_para_salvar = [InventoryItem(snapshot_id=novo_snapshot.id, product_id=p.id) for p in set(produtos_processados)]
        db.add_all(itens_para_salvar)
        db.commit()
        logger.info(f"{len(tags)} tags salvas no novo snapshot de inventário {novo_snapshot.id}")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao salvar inventário do scan RFID: {e}")
        db.rollback()
        return False