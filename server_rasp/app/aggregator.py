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

    # 1) Trata eventos expirados: dispara WARNING antes de remover
    for e in list(_buffer):
        if now > e["expire_at"]:
            esp_id    = e["esp_id"]
            cama_nome = e["cama"]
            db  = SessionLocal()
            emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
            quarto = emb.quarto if emb else None

            # monta payload de WARNING
            warning_payload = {
                "esp_id":      esp_id,
                "cama":        cama_nome,
                "quarto":      quarto,
                "status":      "WARNING",
                "dataOn":      e.get("dataOn"),
                "wifi":        e.get("wifi"),
                "mac_address": None
            }
            bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()
            if bed:
                warning_payload["mac_address"] = bed.mac_address

            print(f"[aggregator] expirado: dispatch WARNING {warning_payload}")
            dispatch_event(warning_payload)

            _buffer.remove(e)

    # 2) filtra só os que estão prontos para tentar
    due = [e for e in _buffer if now >= e["next_attempt"]]
    if not due:
        return

    # 3) agrupa por cama, pegando o de maior RSSI
    best_by_cama = {}
    for e in due:
        cama = e["cama"]
        rssi = e.get("RSSI", 0)
        if cama not in best_by_cama or rssi > best_by_cama[cama]["RSSI"]:
            best_by_cama[cama] = e

    # 4) agrupa por ESP, novamente maior RSSI
    best_by_esp = {}
    for e in best_by_cama.values():
        esp = e["esp_id"]
        rssi = e.get("RSSI", 0)
        if esp not in best_by_esp or rssi > best_by_esp[esp]["RSSI"]:
            best_by_esp[esp] = e

    # 5) escolhe um para processar: o mais antigo (FIFO)
    candidatos = list(best_by_esp.values())
    candidatos.sort(key=lambda ev: ev["received_at"])
    evt = candidatos[0]

    # 6) tenta processar
    esp_id    = evt["esp_id"]
    cama_nome = evt["cama"]

    db  = SessionLocal()
    emb = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
    if not emb:
        print(f"[aggregator] ESP não cadastrada ({esp_id}), removendo.")
        _buffer.remove(evt)
        return

    bed = db.query(Bed).filter(Bed.nome_cama == cama_nome).first()
    mac = bed.mac_address if bed else None

    if mac and check_presence(mac):
        payload = {
            "esp_id":      esp_id,
            "cama":        cama_nome,
            "quarto":      emb.quarto,
            "status":      evt.get("status"),
            "dataOn":      evt.get("dataOn"),
            "wifi":        evt.get("wifi"),
            "mac_address": mac
        }
        print(f"[aggregator] dispatching: {payload}")
        dispatch_event(payload)
        _buffer.remove(evt)
    else:
        # ausência de presença → agenda retry e move pro fim da fila
        evt["next_attempt"] = now + RETRY_INTERVAL
        evt["received_at"]  = now
        print(f"[aggregator] ausência de {cama_nome}, retry em {RETRY_INTERVAL}s e movido ao fim da fila")

def start_aggregator():
    print(f"[aggregator] iniciado (EXPIRY={EXPIRY}s, RETRY={RETRY_INTERVAL}s)")
    def loop():
        while True:
            _process_next()
            time.sleep(1)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
