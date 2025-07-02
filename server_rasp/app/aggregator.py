# aggregator

import time
import threading
from .presence import check_presence
from .dispatcher import dispatch_event
from .models import SessionLocal, Embarcado, Bed

# quantos segundos esperar entre tentativas do mesmo evento
RETRY_INTERVAL = 5

# quando expira todo evento (em segundos)
EXPIRY = 15

# buffer de eventos com metadados
_buffer = []

def enqueue_event(evt):
    now = time.time()
    evt["received_at"]  = now
    evt["expire_at"]    = now + EXPIRY
    evt["next_attempt"] = now
    _buffer.append(evt)
    print(f"[aggregator] enqueue: {evt}")

def _process_next():
    now = time.time()
    db = SessionLocal()

    try:
        # --- TRATAMENTO DE EVENTOS 'OUT' (Casos 5 e 6) ---
        # Estes são tratados primeiro porque são explícitos e não precisam de 'check_presence'.
        eventos_out = [e for e in list(_buffer) if e.get("status") == "OUT"]
        for evt in eventos_out:
            cama_nome = evt["cama"]
            bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()

            if bed and bed.quarto is not None:
                # --- Caso 5: Chega OUT e a cama está num quarto ---
                print(f"[aggregator] Recebido 'OUT' para a cama '{cama_nome}' que estava no quarto '{bed.quarto}'.")
                bed.quarto = None
                db.commit()
                # Encaminha o evento 'OUT' para o servidor final
                dispatch_event({
                    "cama": cama_nome,
                    "quarto": None,
                    "status": "OUT",
                    "mac_address": bed.mac_address
                })
            else:
                # --- Caso 6: Chega OUT e a cama já não está num quarto ---
                print(f"[aggregator] Recebido 'OUT' para a cama '{cama_nome}' que já estava sem quarto. Evento ignorado.")
            
            _buffer.remove(evt) # Remove o evento 'OUT' após o tratamento

        # --- TRATAMENTO DE EVENTOS EXPIRADOS (Fallback para o 'OUT') ---
        eventos_expirados = [e for e in list(_buffer) if now > e["expire_at"]]
        for e in eventos_expirados:
            cama_nome = e["cama"]
            print(f"[aggregator] Evento para a cama '{cama_nome}' expirou (tentativas de 'GET' falharam).")
            dispatch_event({"cama": cama_nome, "status": "WARNING", "esp_id": e["esp_id"]})
            _buffer.remove(e)

            # Lógica 'OUT' por expiração: se este era o último evento para a cama
            if not any(evt for evt in _buffer if evt["cama"] == cama_nome):
                bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()
                if bed and bed.quarto is not None:
                    print(f"[aggregator] Todos os eventos para '{cama_nome}' expiraram. Removendo do quarto '{bed.quarto}'.")
                    bed.quarto = None
                    db.commit()

        # --- TRATAMENTO DE EVENTOS 'GET' (Casos 1, 2, 3 e 4) ---
        due = [e for e in _buffer if now >= e["next_attempt"] and e.get("status") == "GET"]
        if not due:
            return

        # A lógica de seleção do melhor candidato continua
        best_by_cama, best_by_esp = {}, {}
        for e in due:
            cama = e["cama"]
            rssi = e.get("RSSI", 0)
            if cama not in best_by_cama or rssi > best_by_cama[cama].get("RSSI", 0): best_by_cama[cama] = e
        for e in best_by_cama.values():
            esp = e["esp_id"]
            rssi = e.get("RSSI", 0)
            if esp not in best_by_esp or rssi > best_by_esp[esp].get("RSSI", 0): best_by_esp[esp] = e
        
        if not best_by_esp: return
        
        candidatos = list(best_by_esp.values())
        candidatos.sort(key=lambda ev: ev["received_at"])
        evt = candidatos[0]

        # Processa o melhor candidato 'GET'
        esp_id = evt["esp_id"]
        cama_nome = evt["cama"]
        emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
        bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()
        
        if not bed or not emb:
            print(f"[aggregator] Cama ou ESP não cadastrado para o evento {evt}. Removendo.")
            _buffer.remove(evt)
            return

        # Verifica a presença na rede
        mac_presente = check_presence(bed.mac_address)

        if mac_presente: # Cama está no Wi-Fi
            if bed.quarto is None:
                # --- Caso 1: Chega GET, cama no wifi, sem quarto ---
                print(f"[aggregator] Associando cama '{cama_nome}' ao quarto '{emb.quarto}'.")
                bed.quarto = emb.quarto
                db.commit()
                dispatch_event({
                    "cama": cama_nome, "quarto": bed.quarto, "status": "IN", 
                    "mac_address": bed.mac_address, "esp_id": esp_id
                })
                _buffer.remove(evt)
            else:
                # --- Caso 2: Chega GET, cama no wifi, já com quarto (conflito ou confirmação) ---
                if bed.quarto == emb.quarto:
                    print(f"[aggregator] Confirmação de '{cama_nome}' no quarto '{bed.quarto}'. Evento processado.")
                else:
                    print(f"[aggregator] Conflito Ignorado: '{cama_nome}' já está em '{bed.quarto}', detectada em '{emb.quarto}'.")
                _buffer.remove(evt) # Remove o evento para não reprocessar

        else: # Cama NÃO está no Wi-Fi
            if bed.quarto is not None:
                # --- Caso 4: Chega GET, cama não está no wifi, mas tem quarto ---
                print(f"[aggregator] Deteção de '{cama_nome}' (offline) por '{esp_id}' ignorada, pois já tem quarto '{bed.quarto}'.")
                _buffer.remove(evt)
            else:
                # --- Caso 3: Chega GET, cama não está no wifi e não tem quarto ---
                print(f"[aggregator] Cama '{cama_nome}' não encontrada na rede. Agendando retry.")
                evt["next_attempt"] = now + RETRY_INTERVAL
    
    finally:
        db.close()
        
def start_aggregator():
    print(f"[aggregator] iniciado (EXPIRY={EXPIRY}s, RETRY={RETRY_INTERVAL}s)")
    def loop():
        while True:
            _process_next()
            time.sleep(1)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
