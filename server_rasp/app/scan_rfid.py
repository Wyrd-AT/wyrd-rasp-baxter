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

async def rfid_scan_task(websocket: WebSocket, datacenter_id: int, mode: str) -> List[str]:
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