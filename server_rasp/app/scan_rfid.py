import logging
import json
import asyncio
import serial_asyncio
from typing import List

from fastapi import WebSocket
from sqlalchemy.orm import Session

# Importa os modelos necessários do seu arquivo de modelos
from .models import InventorySnapshot, InventoryItem, Product, ProductType

logger = logging.getLogger(__name__)

async def rfid_scan_task(websocket: WebSocket, datacenter_id: int, scan_mode: str) -> List[str]:
    """
    Tarefa de fundo que lê tags de RFID, suportando modo 'automatico' ou 'gatilho'.
    """
    PORTA_SERIAL = 'COM22' # Ou 'COM3' no Windows
    tags_lidas = set()
    reader, writer = None, None

    try:
        reader, writer = await asyncio.wait_for(
            serial_asyncio.open_serial_connection(url=PORTA_SERIAL, baudrate=115200),
            timeout=5.0
        )

        # --- LÓGICA CONDICIONAL BASEADA NO MODO ---
        if scan_mode == "automatico":
            # Modo 1: Leitura contínua automática
            writer.write(b'.iv\r\n')
            await websocket.send_text(json.dumps({"type": "scan_status", "message": "Scan automático iniciado..."}))
        elif scan_mode == "gatilho":
            # Modo 2: Ativa o gatilho da pistola
            writer.write(b'.sa -s inv\r\n')
            await writer.drain()
            await websocket.send_text(json.dumps({"type": "scan_status", "message": "Modo de gatilho ativado. Pressione o gatilho para ler."}))
        
        # O loop de leitura é o mesmo para ambos os modos
        while True:
            # O timeout encerra a sessão se não houver atividade
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
        await websocket.send_text(json.dumps({"type": "scan_error", "message": f"Erro: Porta serial '{PORTA_SERIAL}' indisponível."}))
    except asyncio.CancelledError:
        logger.info("Tarefa de scan foi cancelada pelo usuário.")
        raise
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "scan_error", "message": f"Erro inesperado: {e}"}))
    finally:
        logger.info("Finalizando tarefa de scan e limpando recursos.")
        if writer and not writer.is_closing():
            # Limpeza condicional: desliga o gatilho ou aborta o scan
            if scan_mode == "gatilho":
                writer.write(b'.sa -s off\r\n')
            else:
                writer.write(b'.ab\r\n')
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
        # Para simplificar, vamos associar todas as novas tags ao primeiro tipo de produto que encontrarmos
        default_tipo = db.query(ProductType).first()
        if not default_tipo:
            raise Exception("Nenhum tipo de produto encontrado no banco de dados para associar as tags.")
        default_tipo_id = default_tipo.id

        for codigo_rfid in tags:
            produto = db.query(Product).filter(Product.codigo_rfid == codigo_rfid).first()
            if not produto:
                produto = Product(codigo_rfid=codigo_rfid, product_type_id=default_tipo_id)
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