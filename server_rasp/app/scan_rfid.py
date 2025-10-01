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

async def rfid_scan_task(websocket: WebSocket, datacenter_id: int) -> List[str]:
    """
    Tarefa de fundo que abre a porta serial, lê as tags e as envia via WebSocket.
    Retorna a lista final de tags lidas.
    """
    # IMPORTANTE: Verifique se esta porta está correta para o seu sistema!
    PORTA_SERIAL = '/dev/rfcomm0' # Para Linux. No Windows, seria 'COM3', 'COM4', etc.
    tags_lidas = set()

    writer = None  # Definido aqui para estar acessível no 'finally'
    try:
        # Abre a conexão com a porta serial de forma assíncrona
        reader, writer = await serial_asyncio.open_serial_connection(url=PORTA_SERIAL, baudrate=115200, timeout=1)

        # Envia o comando de inventário para a pistola
        writer.write(b'.iv\r\n')
        await websocket.send_text(json.dumps({"type": "scan_status", "message": "Scan iniciado..."}))
        
        # Loop para ler as respostas da pistola
        while True:
            linha_bytes = await reader.readline()
            linha = linha_bytes.decode('ascii').strip()

            if linha.startswith('EP:'):
                tag_id = linha[4:]
                if tag_id not in tags_lidas:
                    tags_lidas.add(tag_id)
                    # Envia a nova tag para o frontend em tempo real
                    await websocket.send_text(json.dumps({"type": "tag_scanned", "tag_id": tag_id}))
            
            if linha.startswith('OK:') or linha.startswith('ER:'):
                break # Termina se o leitor parar por conta própria
    
    except serial.SerialException:
        await websocket.send_text(json.dumps({"type": "scan_error", "message": f"Erro: Não foi possível abrir a porta {PORTA_SERIAL}."}))
    except asyncio.CancelledError:
        # Esta exceção é levantada quando o usuário clica em "Parar Scan"
        logger.info("Tarefa de scan foi cancelada pelo cliente.")
        await websocket.send_text(json.dumps({"type": "scan_status", "message": "Scan cancelado."}))
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "scan_error", "message": f"Erro inesperado: {e}"}))
    finally:
        # Garante que a pistola pare de escanear ao finalizar ou cancelar
        if writer and not writer.is_closing():
            writer.write(b'.ab\r\n') # Comando de abortar
            writer.close()
        
        await websocket.send_text(json.dumps({"type": "scan_status", "message": "Scan finalizado."}))
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