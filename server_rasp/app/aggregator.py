# aggregator.py (versão final, orientada a eventos, sem EXPIRY)

import asyncio
from collections import defaultdict
from .presence import check_presence
from .dispatcher import dispatch_event
from .models import SessionLocal, Embarcado, Bed

# --- Configurações ---
# Frequência para tentar novamente a verificação de MAC (em segundos)
RETRY_PRESENCE_FREQUENCY_SEC = 60 

# --- Estruturas de Dados em Memória ---
_buffer = []
_beds_in_process = set()
# Dicionário para rastrear tarefas de retry de MAC pendentes {cama_nome: asyncio.Task}
_pending_mac_checks = {} 

def enqueue_event(evt):
    """ Coloca um novo evento no buffer. """
    # Não precisamos de timestamp aqui, já que não há mais expiração
    _buffer.append(evt)
    print(f"[aggregator] enqueue: {evt}")


async def retry_mac_check(cama_nome: str, event_data: dict):
    """
    Tarefa de longa duração que verifica a presença de um MAC em baixa frequência.
    Esta tarefa só termina se encontrar o MAC ou for cancelada.
    """
    print(f"[aggregator-retry] Iniciada tarefa de verificação para '{cama_nome}'. Frequência: {RETRY_PRESENCE_FREQUENCY_SEC}s.")
    
    while True:
        await asyncio.sleep(RETRY_PRESENCE_FREQUENCY_SEC)
        
        db = SessionLocal()
        try:
            bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()
            if not bed:
                print(f"[aggregator-retry] Cama '{cama_nome}' não encontrada na DB. Cancelando retry.")
                break

            print(f"[aggregator-retry] Tentando verificar presença de '{cama_nome}' (MAC: {bed.mac_address}).")
            if check_presence(bed.mac_address):
                print(f"[aggregator-retry] SUCESSO! Cama '{cama_nome}' encontrada na rede.")
                # Cama apareceu! Realiza a lógica de associação.
                emb = db.query(Embarcado).filter(Embarcado.id_esp == event_data["esp_id"]).first()
                if emb and bed.quarto is None:
                    print(f"[aggregator-retry] Associando cama '{cama_nome}' via retry ao quarto '{emb.quarto}'.")
                    bed.quarto = emb.quarto
                    db.commit()
                    # Despacha o evento original
                    dispatch_payload = event_data.copy()
                    dispatch_payload.update({"quarto": bed.quarto, "status": "GET", "mac_address": bed.mac_address})
                    dispatch_event(dispatch_payload)
                break # Termina a tarefa de retry com sucesso
        finally:
            db.close()

    if cama_nome in _pending_mac_checks:
        del _pending_mac_checks[cama_nome]
    print(f"[aggregator-retry] Finalizada tarefa de verificação para '{cama_nome}'.")


async def process_bed_events(cama_nome: str):
    """
    Função 'worker' que processa os eventos de uma cama.
    """
    _beds_in_process.add(cama_nome)
    
    db = SessionLocal()
    try:
        events_for_bed = [e for e in _buffer if e.get("cama") == cama_nome]
        if not events_for_bed: return

        # --- LÓGICA DE 'OUT' EXPLÍCITO ---
        if any(e.get("status") == "OUT" for e in events_for_bed):
            if cama_nome in _pending_mac_checks:
                _pending_mac_checks[cama_nome].cancel()
                del _pending_mac_checks[cama_nome]
                print(f"[aggregator] Verificação de presença para '{cama_nome}' cancelada devido a evento 'OUT'.")
            
            evt_out = next((e for e in events_for_bed if e.get("status") == "OUT"), events_for_bed[0])
            bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()
            if bed and bed.quarto is not None:
                print(f"[aggregator] Recebido 'OUT' para '{cama_nome}'. Removendo do quarto '{bed.quarto}'.")
                bed.quarto = None; db.commit()
                dispatch_payload = evt_out.copy(); dispatch_payload.update({"quarto": None, "status": "OUT", "mac_address": bed.mac_address})
                dispatch_event(dispatch_payload)
            
            # Remove todos os eventos desta cama, pois seu estado é definitivo.
            for ev in list(_buffer):
                if ev.get("cama") == cama_nome: _buffer.remove(ev)
            return

        # --- LÓGICA DE 'GET' ---
        best_event = min(events_for_bed, key=lambda e: e.get("RSSI", 1000))
        print(f"[aggregator] FILTRO PARA '{cama_nome}': {len(events_for_bed)} eventos na disputa. "
              f"Vencedor: ESP '{best_event['esp_id']}' com RSSI {best_event['RSSI']}.")
        
        esp_id = best_event["esp_id"]
        emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
        bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()

        if not bed or not emb:
            print(f"[aggregator] Cama ou ESP não cadastrado para {best_event}. Removendo.")
            for ev in list(_buffer):
                if ev.get("cama") == cama_nome: _buffer.remove(ev)
            return

        if check_presence(bed.mac_address):
            if cama_nome in _pending_mac_checks:
                _pending_mac_checks[cama_nome].cancel()
                del _pending_mac_checks[cama_nome]
                print(f"[aggregator] Cama '{cama_nome}' encontrada. Cancelando tarefa de retry pendente.")

            dispatch_payload = best_event.copy()
            if bed.quarto is None:
                print(f"[aggregator] Associando '{cama_nome}' ao quarto '{emb.quarto}'.")
                bed.quarto = emb.quarto; db.commit()
                dispatch_payload.update({"quarto": bed.quarto, "status": "GET", "mac_address": bed.mac_address})
                dispatch_event(dispatch_payload)
            elif bed.quarto != emb.quarto:
                print(f"[aggregator] Conflito Ignorado: '{cama_nome}' já está em '{bed.quarto}', mas foi detectada em '{emb.quarto}'.")
            else:
                print(f"[aggregator] Confirmação de '{cama_nome}' no quarto '{bed.quarto}'.")
            
            # Limpa o buffer para esta cama, pois o estado foi consolidado.
            for ev in list(_buffer):
                if ev.get("cama") == cama_nome: _buffer.remove(ev)
        else:
            print(f"[aggregator] Presença de '{cama_nome}' não detectada. Iniciando monitorização em segundo plano.")
            if cama_nome not in _pending_mac_checks:
                task = asyncio.create_task(retry_mac_check(cama_nome, best_event))
                _pending_mac_checks[cama_nome] = task
            
            # Limpa o buffer, transferindo a responsabilidade para a tarefa de retry.
            for ev in list(_buffer):
                if ev.get("cama") == cama_nome: _buffer.remove(ev)
            
    finally:
        db.close()
        # Não removemos mais de _beds_in_process aqui, pois a tarefa é curta.
        if cama_nome in _beds_in_process:
            _beds_in_process.remove(cama_nome)


async def main_aggregator_loop():
    """ O loop principal que orquestra as tarefas. """
    print("[aggregator] Agregador Orientado a Eventos iniciado.")
    while True:
        await asyncio.sleep(1)
        
        pending_events_by_bed = defaultdict(list)
        for evt in _buffer:
            pending_events_by_bed[evt["cama"]].append(evt)
            
        for cama_nome, events in pending_events_by_bed.items():
            if cama_nome not in _beds_in_process:
                asyncio.create_task(process_bed_events(cama_nome))

def start_aggregator():
    pass