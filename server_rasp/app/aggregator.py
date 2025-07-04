# aggregator.py (versão concorrente com asyncio)

import asyncio
from collections import defaultdict
from .presence import check_presence
from .dispatcher import dispatch_event
from .models import SessionLocal, Embarcado, Bed

# --- Configurações ---
RETRY_INTERVAL = 5
EXPIRY = 20

# --- Estruturas de Dados em Memória ---
# Buffer de eventos recebidos
_buffer = []
# Conjunto para rastrear quais camas já estão a ser processadas
_beds_in_process = set()

def enqueue_event(evt):
    """ Coloca um novo evento no buffer. É thread-safe. """
    evt["received_at"] = asyncio.get_event_loop().time()
    _buffer.append(evt)
    #print(f"[aggregator] enqueue: {evt}")

async def process_bed_events(cama_nome: str):
    """
    Função 'worker' que processa todos os eventos de UMA ÚNICA cama.
    Esta função é executada como uma tarefa independente para cada cama.
    """
    print(f"[aggregator] Iniciando processamento para a cama: {cama_nome}")
    _beds_in_process.add(cama_nome)
    
    db = SessionLocal()
    try:
        now = asyncio.get_event_loop().time()
        
        # Filtra apenas os eventos relevantes para esta cama
        events_for_bed = [e for e in _buffer if e.get("cama") == cama_nome]
        if not events_for_bed:
            return

        # --- LÓGICA DE 'OUT' EXPLÍCITO ---
        eventos_out = [e for e in events_for_bed if e.get("status") == "OUT"]
        if eventos_out:
            # Pega o primeiro evento de OUT como referência para o payload
            evt_out = eventos_out[0]
            bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()
            if bed and bed.quarto is not None:
                print(f"[aggregator] Recebido 'OUT' para '{cama_nome}'. Removendo do quarto '{bed.quarto}'.")
                bed.quarto = None
                db.commit()

                # --- CORREÇÃO AQUI ---
                # Monta o payload completo usando os dados do evento 'OUT'
                dispatch_payload = evt_out.copy()
                dispatch_payload.update({
                    "quarto": None,
                    "status": "OUT",
                    "mac_address": bed.mac_address
                })
                dispatch_event(dispatch_payload)

            # Remove todos os eventos (GET e OUT) desta cama do buffer
            for ev in events_for_bed: _buffer.remove(ev)
            return

        # --- LÓGICA DE 'GET' ---
        best_event = min(events_for_bed, key=lambda e: e.get("RSSI", 1000))
        print(f"[aggregator] FILTRO PARA '{cama_nome}': {len(events_for_bed)} eventos na disputa. "
              f"Vencedor: ESP '{best_event['esp_id']}' com RSSI {best_event['RSSI']}.")


        if now > best_event.get("received_at", 0) + EXPIRY:
            print(f"[aggregator] Evento principal para '{cama_nome}' expirou. Despachando WARNING.")
            
            # --- CORREÇÃO AQUI ---
            # Usa o 'best_event' como base para o payload de WARNING
            dispatch_payload = best_event.copy()
            dispatch_payload.update({"status": "WARNING"})
            dispatch_event(dispatch_payload)
            
            bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()
            if bed and bed.quarto is not None:
                print(f"[aggregator] '{cama_nome}' expirou. Removendo do quarto '{bed.quarto}'.")
                bed.quarto = None
                db.commit()
            
            for ev in events_for_bed: _buffer.remove(ev)
            return

        esp_id = best_event["esp_id"]
        emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
        bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()

        if not bed or not emb:
            print(f"[aggregator] Cama ou ESP não cadastrado para {best_event}. Removendo eventos.")
            for ev in events_for_bed: _buffer.remove(ev)
            return

        if check_presence(bed.mac_address):
            # --- CORREÇÃO AQUI ---
            # Prepara o payload final usando o 'best_event' como base
            dispatch_payload = best_event.copy()

            if bed.quarto is None:
                print(f"[aggregator] Associando '{cama_nome}' ao quarto '{emb.quarto}'.")
                bed.quarto = emb.quarto
                db.commit()
                # Atualiza o payload com o novo estado e despacha
                dispatch_payload.update({
                    "quarto": bed.quarto,
                    "status": "GET", # Muda o status para 'GET' para indicar a entrada
                    "mac_address": bed.mac_address
                })
                dispatch_event(dispatch_payload)
            elif bed.quarto != emb.quarto:
                print(f"[aggregator] Conflito Ignorado: '{cama_nome}' já em '{bed.quarto}', detectada em '{emb.quarto}'.")
            else:
                print(f"[aggregator] Confirmação de '{cama_nome}' no quarto '{bed.quarto}'.")
            
            for ev in events_for_bed: _buffer.remove(ev)
        else:
            print(f"[aggregator] Presença de '{cama_nome}' não detectada. Nenhum estado será alterado.")
            
    finally:
        db.close()
        _beds_in_process.remove(cama_nome)
        print(f"[aggregator] Finalizado processamento para a cama: {cama_nome}")

async def main_aggregator_loop():
    """ O loop principal que orquestra as tarefas. """
    print("[aggregator] Agregador Concorrente iniciado.")
    while True:
        await asyncio.sleep(1) # Intervalo do ciclo
        
        # Agrupa todos os eventos pendentes por nome da cama
        pending_events_by_bed = defaultdict(list)
        for evt in _buffer:
            pending_events_by_bed[evt["cama"]].append(evt)
            
        # Para cada cama com eventos, lança uma tarefa se não estiver a ser processada
        for cama_nome, events in pending_events_by_bed.items():
            if cama_nome not in _beds_in_process:
                asyncio.create_task(process_bed_events(cama_nome))

def start_aggregator():
    """ Função chamada pelo main.py para iniciar o agregador. """
    # No novo modelo, o main.py (que já tem um loop asyncio) vai gerir a tarefa
    pass