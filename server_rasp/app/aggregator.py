# ==============================================================================
# ARQUIVO: aggregator.py
# FUNÇÃO:  Cérebro do sistema RTLS. Processa dados brutos de sinal, aplica
#          regras de negócio e determina a localização final dos ativos.
# ==============================================================================

# ===== SEÇÃO 1: IMPORTS E LOGGING =============================================
import asyncio
import time
import json
import logging
from datetime import datetime, timezone, timedelta 

FUSO_HORARIO_BRASIL = timezone(timedelta(hours=-3))

from .presence import check_presence
from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting, ReceivedEvent
from .services import batch_update_asset_assignments
from .mqtt_client import scan_data_queue
from .dispatcher import dispatch_event
from .config import settings

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# ===== SEÇÃO 2: MÓDULOS DE CACHE E CONFIGURAÇÃO GLOBAL ========================
_esp_map = {}
_asset_map = {}
_asset_realtime_state = {}
_config_needs_reload = asyncio.Event()

# --- Configurações padrão, carregadas na inicialização e atualizadas via DB ---
_config = {
    "process_interval_sec": float(settings.get('process_interval_sec', 2.0)),
    "reading_timeout_sec": int(settings.get('reading_timeout_sec', 10)),
    "default_rssi_threshold": -75, 
    "conflict_margin_db": 5,
    "inertia_entrada_ms": 3000,   
    "inertia_saida_ms": 10000,    
    "ema_alpha": float(settings.get('ema_alpha', 0.4)),
    "max_assets_per_room": 1       
}

PENDING_WIFI_CHECK_INTERVAL_SEC = int(settings.get('pending_wifi_check_interval_sec', 10))
PENDING_WARNING_TIMEOUT_SEC = int(settings.get('pending_warning_timeout_sec', 300))
PENDING_EXPIRATION_TIMEOUT_SEC = int(settings.get('pending_expiration_timeout_sec', 600))
DISAPPEARANCE_TOLERANCE_CYCLES = int(settings.get('disappearance_tolerance_cycles', 10))

# ===== SEÇÃO 3: CLASSE DE ESTADO DO ATIVO (AssetState) ========================
class AssetState:
    def __init__(self, mac):
        self.mac = mac
        self.readings = {}
        self.last_known_ema = {}
        self.last_strongest_signal = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000}
        self.candidate_quarto_id = None
        self.candidate_since = None
        self.disappeared_since = None
        self.disappearance_count = 0
        self.weak_signal_since = None
        self.pending_wifi_check_since = None  
        self.pending_quarto_id = None   
        self.pending_event_details = {} 
        self.pending_event_db_id = None 
        self.last_wifi_check_at = 0
        self.warning_issued = False

    def update_reading(self, esp_id, rssi, timestamp, wifi_signal=None):
        old_ema = self.readings.get(esp_id, {}).get("ema_rssi", rssi)
        alpha = _config["ema_alpha"]
        new_ema = (rssi * alpha) + (old_ema * (1 - alpha))
        self.readings[esp_id] = {"rssi": rssi, "timestamp": timestamp, "ema_rssi": new_ema, "wifi_signal": wifi_signal}
        self.last_known_ema[esp_id] = new_ema
        self.disappearance_count = 0

    def cleanup_old_readings(self):
        now = time.time()
        self.readings = {k: v for k, v in self.readings.items() if now - v["timestamp"] < _config["reading_timeout_sec"]}
        return bool(self.readings)

# ===== SEÇÃO 4: FUNÇÕES DE INTERFACE E CONTROLE ===============================
def clear_asset_candidate_state(mac_beacon_to_clear: str):
    """Função de controle para limpar o estado de um ativo, chamada externamente (cancelamento manual) ou internamente (cancelamento automático)."""
    if mac_beacon_to_clear in _asset_realtime_state:
        state = _asset_realtime_state[mac_beacon_to_clear]
        event_id_to_cancel = state.pending_event_db_id

        # Limpeza completa do estado em memória
        state.candidate_quarto_id = None
        state.candidate_since = None
        state.pending_wifi_check_since = None
        state.pending_quarto_id = None
        state.pending_event_details = {}
        state.pending_event_db_id = None
        state.warning_issued = False
        state.last_wifi_check_at = 0
        
        # Atualiza o evento no DB se ele existia
        if event_id_to_cancel:
            db = SessionLocal()
            try:
                event = db.query(ReceivedEvent).get(event_id_to_cancel)
                if event and event.status == "Pendente":
                    event.status = "Cancelado"
                    event.status_detail = "Cancelado pelo operador ou por nova detecção."
                    db.commit()
            finally:
                db.close()
        
        logger.info(f"Estado de memória para o ativo {mac_beacon_to_clear} foi limpo.")
        return True
    return False

def update_asset_cache(mac_beacon: str, new_quarto_id: int | None):
    """
    (NOVA FUNÇÃO) Atualiza o quarto de um ativo no cache em memória do agregador.
    Garante a consistência entre o DB e o estado em tempo real.
    """
    if mac_beacon in _asset_map:
        _asset_map[mac_beacon]["quarto_id"] = new_quarto_id
        logger.info(f"Cache do agregador atualizado para o ativo {mac_beacon}: novo quarto_id é {new_quarto_id}.")

def flag_for_reload():
    """Sinaliza ao loop principal que o cache de mapas precisa ser recarregado."""
    _config_needs_reload.set()

# ===== SEÇÃO 5: FUNÇÕES INTERNAS DO MOTOR RTLS ================================
async def _consume_scan_data_queue():
    """Consome os dados brutos da fila do MQTT e atualiza o estado dos ativos."""
    db = None
    try:
        while not scan_data_queue.empty():
            item = await scan_data_queue.get()
            signal_logger.info(json.dumps(item))
            esp_id, payload = item.get("esp_id"), item.get("payload", {})
            beacons = payload.get("beacons", [])
            wifi_signal = payload.get("wifi_signal")
            for beacon in beacons:
                mac = beacon.get("mac", "").lower()
                if not mac or mac not in _asset_map: continue

                asset_info = _asset_map.get(mac)
                
                if asset_info.get("status") == 'Offline':
                    logger.info(f"Ativo '{asset_info.get('nome_ativo')}' ({mac}) está de volta. Marcando como Online.")
                    if db is None: db = SessionLocal() 
                    asset_db = db.query(Asset).get(asset_info.get("id"))
                    if asset_db:
                        asset_db.status = 'Online'
                    _asset_map[mac]['status'] = 'Online'
                
                if mac not in _asset_realtime_state:
                    _asset_realtime_state[mac] = AssetState(mac)
                _asset_realtime_state[mac].update_reading(esp_id, beacon.get("rssi"), time.time(), wifi_signal)
    finally:
        if db:
            db.commit()
            db.close()

async def _processar_localizacoes():
    """O coração da lógica de localização, executado a cada ciclo do loop principal."""
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    now = time.time()
    db = SessionLocal()
    try:
        for mac, state in list(_asset_realtime_state.items()):
            
            # --- ESTÁGIO 1: PROCESSAR ATIVOS EM ESTADO PENDENTE ---
            if state.pending_wifi_check_since is not None:
                if state.pending_wifi_check_since is None: continue # Proteção contra race condition

                if (now - state.last_wifi_check_at) > PENDING_WIFI_CHECK_INTERVAL_SEC:
                    state.last_wifi_check_at = now
                    asset_info = _asset_map.get(mac, {})
                    wifi_mac_address = asset_info.get("wifi_mac")

                    if wifi_mac_address and await check_presence(wifi_mac_address):
                        logger.info(f"EVENTO CONFIRMADO (Wi-Fi OK): Ativo {mac} confirmado no Quarto {state.pending_quarto_id}.")
                        changes_to_commit.append({**state.pending_event_details, "new_quarto_id": state.pending_quarto_id, "event_to_update_id": state.pending_event_db_id})
                        if mac in _asset_map: _asset_map[mac]["quarto_id"] = state.pending_quarto_id
                        state.pending_wifi_check_since = None
                        state.pending_quarto_id = None
                        state.pending_event_details = {}
                        state.pending_event_db_id = None
                        state.warning_issued = False
                        state.last_wifi_check_at = 0

                if state.pending_wifi_check_since and (now - state.pending_wifi_check_since) > PENDING_WARNING_TIMEOUT_SEC and not state.warning_issued:
                    logger.warning(f"EVENTO ALERTA (PENDENTE): Wi-Fi para {mac} ausente por mais de {PENDING_WARNING_TIMEOUT_SEC}s.")
                    asset_info = _asset_map.get(mac, {})
                    quarto_pendente = db.query(Quarto).get(state.pending_quarto_id)
                    warning_event = ReceivedEvent(esp_id=state.pending_event_details.get("source_esp_id", "aggregator"), ativo=mac, quarto_nome=quarto_pendente.nome if quarto_pendente else "N/A", action="ALERTA", status="OK", status_detail=f"Ativo '{asset_info.get('nome_ativo')}' detectado, mas Wi-Fi ausente por mais de {PENDING_WARNING_TIMEOUT_SEC}s.", rssi=state.pending_event_details.get("rssi"), data_on=datetime.now(timezone.utc), raw={"reason": "Pending Wi-Fi check delay"})
                    db.add(warning_event)
                    state.warning_issued = True
                    dispatch_payload = {"quarto": quarto_pendente.nome if quarto_pendente else "N/A", "cama": asset_info.get("nome_ativo"), "status": "ALERTA", "dataOn": datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')}
                    logger.info(f"A despachar ALERTA para o servidor final: {dispatch_payload}")
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, dispatch_event, dispatch_payload)

                elif state.pending_wifi_check_since and (now - state.pending_wifi_check_since) > PENDING_EXPIRATION_TIMEOUT_SEC:
                    logger.error(f"EVENTO VENCIDO (TIMEOUT): Verificação de Wi-Fi para {mac} excedeu o tempo limite de {PENDING_EXPIRATION_TIMEOUT_SEC}s.")
                    event = db.query(ReceivedEvent).get(state.pending_event_db_id)
                    if event and event.status == "Pendente":
                        event.status = "Vencido"
                        event.status_detail = f"Verificação de Wi-Fi excedeu o tempo limite de {PENDING_EXPIRATION_TIMEOUT_SEC} segundos."
                    clear_asset_candidate_state(mac)
                continue

            # --- ESTÁGIO 2: PROCESSAR SAÍDAS (Ativos que não estão pendentes) ---
            if not state.cleanup_old_readings():
                state.disappearance_count += 1
                
                logger.debug(f"Ativo {mac} sem sinal. Contagem de desaparecimento: {state.disappearance_count}/{DISAPPEARANCE_TOLERANCE_CYCLES}.")

                if state.disappearance_count >= DISAPPEARANCE_TOLERANCE_CYCLES:
                    asset_info = _asset_map.get(mac, {})
                    
                    if asset_info.get("status") == 'Online':
                        logger.warning(f"Ativo '{asset_info.get('nome_ativo')}' ({mac}) desapareceu consistentemente. Marcando como Offline.")
                        
                        if asset_info.get("quarto_id") is not None:
                            changes_to_commit.append({
                                "asset_id": asset_info.get("id"), "new_quarto_id": None,
                                "source_esp_id": state.last_strongest_signal.get("esp_id") or "server_disappearance",
                                "rssi": state.last_strongest_signal.get("rssi", -1000),
                                "details": f"Ativo desapareceu do radar BLE consistentemente."
                            })
                        
                        asset_db = db.query(Asset).get(asset_info.get("id"))
                        if asset_db:
                            asset_db.status = 'Offline'
                        if mac in _asset_map:
                            _asset_map[mac]['status'] = 'Offline'

                    # Remove da monitorização em tempo real após a ação ser tomada
                    del _asset_realtime_state[mac]

                continue # Pula para o próximo ativo

            # --- ESTÁGIO 3: PROCESSAR ENTRADAS E CANDIDATOS ---
            asset_info = _asset_map.get(mac, {})
            asset_id = asset_info.get("id")
            quarto_id_atual = asset_info.get("quarto_id")
            wifi_mac_address = asset_info.get("wifi_mac")

            # 3.1: Encontrar o Sinal Mais Forte (Candidato)
            strongest_candidate = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000, "quarto_id": None, "wifi_signal": None}
            for esp_id, reading in state.readings.items():
                if esp_id not in _esp_map: continue
                q_id, q_rssi = _esp_map[esp_id]
                if any(om != mac and oa.get("quarto_id") == q_id for om, oa in _asset_map.items()): continue
                threshold = q_rssi if q_rssi is not None else _config["default_rssi_threshold"]
                if reading["ema_rssi"] > threshold and reading["ema_rssi"] > strongest_candidate["ema_rssi"]:
                    strongest_candidate.update({"esp_id": esp_id, "rssi": reading["rssi"], "ema_rssi": reading["ema_rssi"], "quarto_id": q_id, "wifi_signal": reading.get("wifi_signal")})
            
            if state.readings:
                top_esp, top_read = max(state.readings.items(), key=lambda i: i[1]['ema_rssi'])
                state.last_strongest_signal = {"esp_id": top_esp, "rssi": top_read['rssi'], "ema_rssi": top_read['ema_rssi']}
            candidate_quarto_id = strongest_candidate["quarto_id"]

            # 3.2: Lógica de Saída Unificada (Sinal Fraco ou Desaparecimento)
            if quarto_id_atual is not None:
                # CONDIÇÃO A: O ativo ainda é um candidato válido para seu quarto?
                if candidate_quarto_id == quarto_id_atual:
                    # Se sim, e se um timer de saída estava a correr, ele é cancelado.
                    if state.disappeared_since is not None:
                        logger.info(f"Sinal para o ativo {mac} no quarto atual ({quarto_id_atual}) restabelecido. Cancelando inércia de saída.")
                        state.disappeared_since = None
                
                # CONDIÇÃO B: O ativo NÃO é mais um candidato válido.
                else:
                    # Se o timer de saída ainda não foi iniciado, inicia-o AGORA.
                    if state.disappeared_since is None:
                        logger.info(f"Ativo {mac} não é mais candidato para o quarto atual ({quarto_id_atual}). Iniciando inércia de saída.")
                        state.disappeared_since = now
                    
                    # Se o timer JÁ estava a correr, verifica se o tempo expirou.
                    elif (now - state.disappeared_since) * 1000 > _config["inertia_saida_ms"]:
                        clear_asset_candidate_state(mac) # Cancela qualquer pendência antes de sair
                        logger.info(f"EVENTO OUT (INÉRCIA): Ativo {mac} removido do Quarto {quarto_id_atual} após inércia de saída.")
                        changes_to_commit.append({
                            "asset_id": asset_id, 
                            "new_quarto_id": None,
                            "source_esp_id": state.last_strongest_signal.get('esp_id') or "server_inertia_out",
                            "rssi": state.last_strongest_signal.get('rssi', -1000),
                            "details": f"Sinal permaneceu fraco ou ausente por mais de {_config['inertia_saida_ms']}ms."
                        })
                        if mac in _asset_realtime_state: del _asset_realtime_state[mac]
                        continue

            # 3.3: Lógica de Gestão de Candidato para ENTRADA (Apenas para ativos livres)
            if quarto_id_atual is None:
                if candidate_quarto_id != state.candidate_quarto_id:
                    state.candidate_quarto_id = candidate_quarto_id
                    state.candidate_since = now

            # 3.4: Transição para o Estado PENDENTE
            if state.candidate_since and state.candidate_quarto_id is not None and (now - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                if state.candidate_quarto_id == candidate_quarto_id and state.candidate_quarto_id != quarto_id_atual:
                    
                    # 1ª Verificação: O ativo está livre?
                    if quarto_id_atual is not None:
                        logger.info(f"[RTLS] Atribuição para {mac} BLOQUEADA. Ativo já está no quarto {quarto_id_atual} e precisa de um evento 'OUT' primeiro.")
                        state.candidate_since = None
                        continue 

                    
                    # 2ª Verificação: O quarto de destino tem vaga?
                    ativos_no_quarto = [a for a in _asset_map.values() if a.get("quarto_id") == state.candidate_quarto_id]
                    if len(ativos_no_quarto) >= _config.get("max_assets_per_room", 1):
                        logger.warning(f"[RTLS] Atribuição para {mac} BLOQUEADA. Quarto {state.candidate_quarto_id} já atingiu o limite de ocupação.")
                        state.candidate_since = None
                        continue
                        
                    # 3ª Verificação (Meritocracia): Existe um concorrente pendente mais forte?
                    strongest_competitor_state = None
                    strongest_competitor_signal = -1000
                    for other_mac, other_state in _asset_realtime_state.items():
                        if other_mac != mac and other_state.pending_quarto_id == state.candidate_quarto_id:
                            competitor_signal = other_state.last_strongest_signal.get('ema_rssi', -1000)
                            if competitor_signal > strongest_competitor_signal:
                                strongest_competitor_signal = competitor_signal
                                strongest_competitor_state = other_state

                    if strongest_competitor_state and strongest_candidate['ema_rssi'] <= strongest_competitor_signal:
                        logger.info(f"[RTLS] Atribuição para {mac} BLOQUEADA. Concorrente pendente '{strongest_competitor_state.mac}' tem sinal mais forte ou igual ({strongest_competitor_signal:.1f}dBm vs {strongest_candidate['ema_rssi']:.1f}dBm).")
                        state.candidate_since = None
                        continue
                                        
                    # Cancela os concorrentes mais fracos, se houver
                    if strongest_competitor_state:
                        logger.warning(f"[RTLS] Ativo {mac} tem o sinal mais forte. Cancelando outros concorrentes pendentes para o Quarto {state.candidate_quarto_id}.")
                        for other_mac, other_state in list(_asset_realtime_state.items()):
                            if other_mac != mac and other_state.pending_quarto_id == state.candidate_quarto_id:
                                clear_asset_candidate_state(other_mac)

                    if not wifi_mac_address:
                        logger.warning(f"Ativo {mac} não tem MAC de Wi-Fi. Confirmando entrada...")
                        changes_to_commit.append({
                            "asset_id": asset_id, 
                            "new_quarto_id": state.candidate_quarto_id, 
                            "source_esp_id": strongest_candidate['esp_id'], 
                            "rssi": strongest_candidate['rssi'], 
                            "wifi_signal": strongest_candidate['wifi_signal'],
                            "details": f"Localizado via {strongest_candidate['esp_id']} (Sem verificação de Wi-Fi)."
                        })
                        if mac in _asset_map: _asset_map[mac]["quarto_id"] = state.candidate_quarto_id
                        state.candidate_since = None
                    elif state.pending_wifi_check_since is None:
                        if state.pending_event_db_id is not None:
                            logger.info(f"Ativo {mac} tem novo candidato. Cancelando evento pendente anterior (ID: {state.pending_event_db_id}).")
                            event_antigo = db.query(ReceivedEvent).get(state.pending_event_db_id)
                            if event_antigo and event_antigo.status == "Pendente":
                                event_antigo.status = "Cancelado"
                                event_antigo.status_detail = "Cancelado automaticamente por nova detecção mais estável."

                        logger.info(f"EVENTO PENDENTE: Ativo {mac} -> Quarto {state.candidate_quarto_id}. Criando novo evento e iniciando verificação.")
                        quarto_pendente = db.query(Quarto).get(state.candidate_quarto_id)
                        pending_event = ReceivedEvent(esp_id=strongest_candidate['esp_id'], ativo=mac, quarto_nome=quarto_pendente.nome if quarto_pendente else "N/A", action="GET", status="Pendente", status_detail=f"Aguardando Wi-Fi ({wifi_mac_address}).", rssi=strongest_candidate['rssi'], data_on=datetime.now(timezone.utc), raw=strongest_candidate)
                        db.add(pending_event)
                        db.commit()
                        db.refresh(pending_event)
                        
                        state.pending_event_db_id = pending_event.id
                        state.pending_quarto_id = state.candidate_quarto_id
                        state.pending_wifi_check_since = now
                        state.last_wifi_check_at = 0
                        state.warning_issued = False
                        state.pending_event_details = {
                                        "asset_id": asset_id, 
                                        "source_esp_id": strongest_candidate['esp_id'], 
                                        "rssi": strongest_candidate['rssi'], 
                                        "wifi_signal": strongest_candidate['wifi_signal'], 
                                        "details": f"Wi-Fi confirmado via {strongest_candidate['esp_id']} (EMA {strongest_candidate['ema_rssi']:.1f})."
                                    }                        
                        state.candidate_since = None
                        state.candidate_quarto_id = None
        
        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
        db.commit()
    finally:
        db.close()

# ===== SEÇÃO 6: LOOP PRINCIPAL E CARREGAMENTO DE DADOS ========================
def _load_maps_from_db():
    """Carrega/recarrega os mapas de ESPs, Ativos e Configurações do banco de dados para o cache em memória."""
    global _esp_map, _asset_map, _config
    db = SessionLocal()
    try:
        esps = db.query(Embarcado).all()
        _esp_map = {e.id_esp: (e.quarto_id, e.rssi_threshold) for e in esps}
        
        assets = db.query(Asset).all()
        _asset_map = {
            a.mac_beacon: {
                "id": a.id, 
                "nome_ativo": a.nome_ativo, 
                "quarto_id": a.quarto_id, 
                "wifi_mac": a.mac_address,
                "status": a.status 
            } 
            for a in assets
        }
        
        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        
        _config["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", _config["default_rssi_threshold"]))
        _config["inertia_entrada_ms"] = int(settings_from_db.get("inercia_entrada", _config["inertia_entrada_ms"]))
        _config["inertia_saida_ms"] = int(settings_from_db.get("inertia_saida", _config["inertia_saida_ms"]))
        _config["max_assets_per_room"] = int(settings_from_db.get("max_assets_per_room", _config["max_assets_per_room"]))
        _config["ema_alpha"] = float(settings_from_db.get("ema_alpha", _config["ema_alpha"]))
        
        logger.info(f"[RTLS] Cache (re)carregado: {len(_esp_map)} ESPs, {len(_asset_map)} Ativos.")
    finally:
        db.close()

async def main_aggregator_loop():
    """Ponto de entrada principal. Roda para sempre, orquestrando o motor RTLS."""
    logger.info("[RTLS] Motor de localização iniciado.")
    _load_maps_from_db()
    while True:
        try:
            if _config_needs_reload.is_set():
                _load_maps_from_db()
                _config_needs_reload.clear()
            await _consume_scan_data_queue()
            await _processar_localizacoes()
        except Exception as e:
            logger.error(f"[RTLS] Erro crítico no loop principal: {e}", exc_info=True)
        await asyncio.sleep(_config["process_interval_sec"])