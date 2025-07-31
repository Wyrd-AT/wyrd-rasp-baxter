# aggregator.py - Versão Final com Janela de Disputa e Veredito Explícito
import logging
logger = logging.getLogger(__name__)
import asyncio
from datetime import datetime, timezone
from .connection_manager import manager
from .services import update_asset_assignment 
from sqlalchemy.orm import joinedload


# Importa as funções e modelos necessários
# from .dispatcher import dispatch_event_to_rtls
from .models import SessionLocal, Asset, Embarcado, ReceivedEvent
from .mqtt_client import publish_available_assets, publish_verdict # Importa a nova função de veredito

# --- Configurações ---
DISPUTE_WINDOW_SEC = 2  # Janela de 5 segundos para a disputa

# --- Estruturas de Dados em Memória ---
_dispute_windows = {}
_lock = asyncio.Lock()


def _update_event_status(event_id: int, status: str, detail: str):
    """ Helper para atualizar o status de um evento no banco de dados. """
    db = SessionLocal()
    try:
        event_to_update = db.query(ReceivedEvent).filter(ReceivedEvent.id == event_id).first()
        if event_to_update:
            event_to_update.status = status
            event_to_update.status_detail = detail
            db.commit()
    except Exception as e:
        logger.error(f"[aggregator-update] ERRO ao atualizar evento {event_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def _resolve_dispute(beacon_mac: str):
    """
    Função chamada após a janela de disputa fechar.
    Ela elege o vencedor, atualiza o estado e envia os vereditos via MQTT.
    """
    async with _lock:
        events = _dispute_windows.pop(beacon_mac, [])
        if not events:
            return

    logger.info(f"\n[aggregator] Janela para '{beacon_mac}' FECHADA. Resolvendo com {len(events)} eventos.")

    # 1. Elege o melhor evento baseado no RSSI mais forte
    best_event = max(events, key=lambda e: e.get("RSSI", -1000))
    winner_esp_id = best_event.get("esp_id")
    logger.info(f"[aggregator] Vencedor da disputa: ESP '{winner_esp_id}' com RSSI {best_event.get('RSSI')}.")

    # 2. Envia o veredito ("WIN" ou "LOSE") para cada participante da disputa
    transacao_id = best_event.get("transacao_id")

    for evt in events:
        esp_id = evt.get("esp_id")
        if esp_id == winner_esp_id:
            # --- ALTERAÇÃO AQUI: Passamos o transacao_id ---
            publish_verdict(esp_id, "WIN", beacon_mac, transacao_id)
        else:
            # --- ALTERAÇÃO AQUI: Passamos o transacao_id ---
            publish_verdict(esp_id, "LOSE", beacon_mac, transacao_id)
            detail = f"Sinal mais fraco (RSSI: {evt.get('RSSI', 'N/A')}). Perdeu disputa para ESP '{winner_esp_id}'."
            _update_event_status(evt.get("event_id"), "Ignorado", detail)
    
    # 3. Processa a lógica de associação para o vencedor
    db = SessionLocal()
    try:
        asset = db.query(Asset).options(joinedload(Asset.quarto)).filter(Asset.mac_beacon == beacon_mac).first()
        emb = db.query(Embarcado).options(joinedload(Embarcado.quarto)).filter(Embarcado.id_esp == winner_esp_id).first()

        # Verifica se os componentes existem antes de prosseguir
        if not asset:
            detail = "Ativo com este MAC não está cadastrado no sistema."
            _update_event_status(best_event.get("event_id"), "Erro", detail)
            return
        
        if not emb:
            detail = f"ESP vencedora '{winner_esp_id}' não está cadastrada no sistema."
            _update_event_status(best_event.get("event_id"), "Erro", detail)
            return
        
        # Chamamos o serviço para fazer a associação
        await update_asset_assignment(db=db, asset_id=asset.id, new_quarto_id=emb.quarto_id)
        
        # A função de serviço já trata da notificação do quarto anterior e da atualização
        # da lista de ativos via MQTT, além de notificar o frontend via WebSocket.

        # Apenas atualizamos o status do evento vencedor
        if asset.quarto_id == emb.quarto_id:
             _update_event_status(best_event.get("event_id"), "OK", f"Ativo associado com sucesso ao quarto '{emb.quarto.nome}'.")
        else:
             _update_event_status(best_event.get("event_id"), "Confirmado", f"Ativo já estava no quarto '{emb.quarto.nome}'.")

    finally:
        db.close()


async def enqueue_event(evt: dict):
    """ Coloca um evento na fila de disputa ou o processa imediatamente se for 'OUT'. """
    logger.info(f"[aggregator] Evento recebido: {evt}")
    event_id = evt.get("event_id")
    beacon_mac = evt.get("ativo")

    # --- Cenário de SAÍDA: Processamento imediato, tem prioridade sobre disputas "GET" ---
    if evt.get("status") == "OUT":
        db = SessionLocal()
        try:
            asset = db.query(Asset).filter(Asset.mac_beacon == beacon_mac).first()
            if asset and asset.quarto is not None:
                quarto_anterior = asset.quarto
                asset.quarto = None
                db.commit()
                await manager.broadcast("ATUALIZAR_ESTADO") # <-- ADICIONE ESTA LINHA
                publish_available_assets() # Notifica todos sobre a disponibilidade
                
                # Despacha o evento de SAÍDA
                event_data = {
                    "ativo": beacon_mac, "quarto": quarto_anterior.nome,
                    "data_evento": evt.get("data_on"), "tipo_evento": "wyrd.SAIDA"
                }
                # success = await dispatch_event_to_rtls("wyrd.SAIDA", event_data)
                # if success:
                #     _update_event_status(event_id, "OK", f"Ativo desassociado e evento de saída enviado com sucesso para o quarto '{quarto_anterior}'.")
                # else:
                #     _update_event_status(event_id, "Erro", f"O ativo foi desassociado, mas a notificação para a Rtls falhou.")
                _update_event_status(event_id, "OK", f"Ativo desassociado com sucesso Do quarto '{quarto_anterior}'.")
            elif asset:
                 _update_event_status(event_id, "Confirmado", "Ativo já estava desassociado.")
            else:
                 _update_event_status(event_id, "Erro", f"Ativo com beacon '{beacon_mac}' não cadastrado.")
        finally:
            db.close()
        return # Encerra a função

    # --- Cenário de ENTRADA (com a nova verificação de segurança) ---
    if evt.get("status") == "GET":
        async with _lock:
            # Se já existe uma disputa em andamento, apenas adiciona o evento a ela.
            if beacon_mac in _dispute_windows:
                _dispute_windows[beacon_mac].append(evt)
                logger.info(f"[aggregator] Evento da ESP '{evt.get('esp_id')}' adicionado à disputa existente por '{beacon_mac}'.")
                return

            # --- VERIFICAÇÃO DE SEGURANÇA CRÍTICA ---
            # Antes de criar uma NOVA disputa, verifica na base de dados se o ativo já foi alocado.
            db = SessionLocal()
            try:
                asset_ja_alocado = db.query(Asset).filter(
                    Asset.mac_beacon == beacon_mac,
                    Asset.quarto_id.isnot(None)
                ).first()

                if asset_ja_alocado:
                    # Se o ativo já tem um quarto, este é um pedido atrasado.
                    logger.info(f"[aggregator] Pedido GET para '{beacon_mac}' IGNORADO. Ativo já pertence ao quarto '{asset_ja_alocado.quarto.nome}'.")
                    _update_event_status(event_id, "Ignorado", f"Ativo já alocado ao quarto {asset_ja_alocado.quarto.nome}.")
                    return # Impede o "roubo".
            finally:
                db.close()
            # --- FIM DA VERIFICAÇÃO ---
            
            # Se chegámos aqui, o ativo está livre e não há disputa. Podemos iniciar uma.
            logger.info(f"[aggregator] Nova janela de disputa de {DISPUTE_WINDOW_SEC}s para o ativo '{beacon_mac}'.")
            _dispute_windows[beacon_mac] = [evt] # Adiciona o evento atual como o primeiro
            
            loop = asyncio.get_running_loop()
            loop.call_later(
                DISPUTE_WINDOW_SEC,
                lambda: asyncio.create_task(_resolve_dispute(beacon_mac))
            )

# O loop principal agora apenas precisa existir, o trabalho é feito pelos eventos.
async def main_aggregator_loop():
    """ O loop principal agora apenas mantém o programa rodando. """
    logger.info(f"[aggregator] Agregador orientado a eventos iniciado. Janela de disputa: {DISPUTE_WINDOW_SEC}s.")
    while True:
        # O loop pode dormir por mais tempo, já que a lógica agora é reativa
        await asyncio.sleep(3600) # Dorme por uma hora, apenas para manter a task viva.

# Esta função não é mais necessária, mas a mantemos para não quebrar nenhuma importação antiga.
def cancel_pending_task(wifi_mac: str) -> bool:
    logger.info(f"[aggregator-cancel] A função de cancelamento não é mais aplicável na nova arquitetura.")
    return False