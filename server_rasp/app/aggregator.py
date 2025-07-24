# aggregator.py - Versão Final com Janela de Disputa e Veredito Explícito

import asyncio
from datetime import datetime, timezone

# Importa as funções e modelos necessários
from .dispatcher import dispatch_event_to_rtls
from .models import SessionLocal, Asset, Embarcado, ReceivedEvent
from .mqtt_client import publish_available_assets, publish_verdict # Importa a nova função de veredito

# --- Configurações ---
DISPUTE_WINDOW_SEC = 3  # Janela de 5 segundos para a disputa

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
        print(f"[aggregator-update] ERRO ao atualizar evento {event_id}: {e}")
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

    print(f"\n[aggregator] Janela para '{beacon_mac}' FECHADA. Resolvendo com {len(events)} eventos.")

    # 1. Elege o melhor evento baseado no RSSI mais forte
    best_event = max(events, key=lambda e: e.get("RSSI", -1000))
    winner_esp_id = best_event.get("esp_id")
    print(f"[aggregator] Vencedor da disputa: ESP '{winner_esp_id}' com RSSI {best_event.get('RSSI')}.")

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
        asset = db.query(Asset).filter(Asset.mac_beacon == beacon_mac).first()
        emb = db.query(Embarcado).filter(Embarcado.id_esp == winner_esp_id).first()

        if asset and emb and asset.quarto != emb.quarto:
            quarto_anterior = asset.quarto
            asset.quarto = emb.quarto
            db.commit()
            publish_available_assets() # Atualiza a lista geral para todos
            
            # Prepara e despacha o evento para a Rtls
            event_data = {
                "ativo": asset.mac_beacon, "quarto": emb.quarto.nome,
                "data_evento": best_event.get("data_on"), "tipo_evento": "wyrd.ENTRADA"
            }
            success = await dispatch_event_to_rtls("wyrd.ENTRADA", event_data)
            if success:
                detail = f"Ativo associado ao quarto '{emb.quarto.nome}' e evento de entrada enviado com sucesso."
                _update_event_status(best_event.get("event_id"), "OK", detail)
            else:
                detail = f"Ativo associado ao quarto '{emb.quarto.nome}', mas a notificação para a Rtls falhou."
                _update_event_status(best_event.get("event_id"), "Erro", detail)

        elif asset and emb and asset.quarto == emb.quarto:
             _update_event_status(best_event.get("event_id"), "Confirmado", f"Ativo já estava no quarto '{emb.quarto}'.")
        
        else: # Caso asset ou embarcado não sejam encontrados
            detail = f"Componente não cadastrado: {'Ativo' if not asset else 'ESP'}."
            _update_event_status(best_event.get("event_id"), "Erro", detail)

    finally:
        db.close()


async def enqueue_event(evt: dict):
    """ Coloca um evento na fila de disputa ou o processa imediatamente se for 'OUT'. """
    print(f"[aggregator] Evento recebido: {evt}")
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
                publish_available_assets() # Notifica todos sobre a disponibilidade
                
                # Despacha o evento de SAÍDA
                event_data = {
                    "ativo": beacon_mac, "quarto": quarto_anterior.nome,
                    "data_evento": evt.get("data_on"), "tipo_evento": "wyrd.SAIDA"
                }
                success = await dispatch_event_to_rtls("wyrd.SAIDA", event_data)
                if success:
                    _update_event_status(event_id, "OK", f"Ativo desassociado e evento de saída enviado com sucesso para o quarto '{quarto_anterior}'.")
                else:
                    _update_event_status(event_id, "Erro", f"O ativo foi desassociado, mas a notificação para a Rtls falhou.")
                
            elif asset:
                 _update_event_status(event_id, "Confirmado", "Ativo já estava desassociado.")
            else:
                 _update_event_status(event_id, "Erro", f"Ativo com beacon '{beacon_mac}' não cadastrado.")
        finally:
            db.close()
        return # Encerra a função

    # --- Cenário de ENTRADA: Abre ou entra em uma janela de disputa ---
    if evt.get("status") == "GET":
        async with _lock:
            if beacon_mac not in _dispute_windows:
                # Primeiro evento para este ativo: abre a janela
                print(f"[aggregator] Nova janela de disputa de {DISPUTE_WINDOW_SEC}s para o ativo '{beacon_mac}'.")
                _dispute_windows[beacon_mac] = []
                # Agenda a resolução da disputa para daqui a X segundos
                loop = asyncio.get_running_loop()
                loop.call_later(
                    DISPUTE_WINDOW_SEC,
                    lambda: asyncio.create_task(_resolve_dispute(beacon_mac))
                )

            # Adiciona o evento à disputa em andamento
            _dispute_windows[beacon_mac].append(evt)
            print(f"[aggregator] Evento da ESP '{evt.get('esp_id')}' adicionado à disputa por '{beacon_mac}'.")


# O loop principal agora apenas precisa existir, o trabalho é feito pelos eventos.
async def main_aggregator_loop():
    """ O loop principal agora apenas mantém o programa rodando. """
    print(f"[aggregator] Agregador orientado a eventos iniciado. Janela de disputa: {DISPUTE_WINDOW_SEC}s.")
    while True:
        # O loop pode dormir por mais tempo, já que a lógica agora é reativa
        await asyncio.sleep(3600) # Dorme por uma hora, apenas para manter a task viva.

# Esta função não é mais necessária, mas a mantemos para não quebrar nenhuma importação antiga.
def cancel_pending_task(wifi_mac: str) -> bool:
    print(f"[aggregator-cancel] A função de cancelamento não é mais aplicável na nova arquitetura.")
    return False