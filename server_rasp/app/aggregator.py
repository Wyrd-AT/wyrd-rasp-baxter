# aggregator.py (Versão Final com Média Móvel Exponencial)
import asyncio
import time
import json
from .presence import check_presence
from .models import SessionLocal, Asset, Embarcado, Quarto, GlobalSetting
from .services import batch_update_asset_assignments
from .mqtt_client import scan_data_queue
import logging

logger = logging.getLogger(__name__)
signal_logger = logging.getLogger('signals')

# --- CACHE EM MEMÓRIA ---
_esp_map = {}
_asset_map = {}
_config_needs_reload = asyncio.Event()
_asset_realtime_state = {}

# --- Configurações Padrão ---
_config = {
    "process_interval_sec": 2.0,
    "reading_timeout_sec": 10,
    "default_rssi_threshold": -75,
    "conflict_margin_db": 5,
    "inertia_entrada_ms": 3000,
    "inertia_saida_ms": 10000,
    "ema_alpha": 0.4 
}

class AssetState:
    def __init__(self, mac):
        self.mac = mac
        self.readings = {}
        self.last_known_ema = {}
        self.last_strongest_signal = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000}
        self.candidate_quarto_id = None
        self.candidate_since = None
        self.disappeared_since = None
        self.weak_signal_since = None
        self.pending_wifi_check_since = None  
        self.pending_quarto_id = None   
        self.pending_event_details = {} 

    def update_reading(self, esp_id, rssi, timestamp):
        """
        Atualiza a leitura de uma ESP e calcula a Média Móvel Exponencial (EMA).
        """
        # Se for a primeira leitura desta ESP, a EMA inicial é o próprio RSSI.
        old_ema = self.readings.get(esp_id, {}).get("ema_rssi", rssi)
        
        # Fórmula da Média Móvel Exponencial
        alpha = _config["ema_alpha"]
        new_ema = (rssi * alpha) + (old_ema * (1 - alpha))

        # Guarda o valor bruto (rssi) e o valor suavizado (ema_rssi)
        self.readings[esp_id] = {"rssi": rssi, "timestamp": timestamp, "ema_rssi": new_ema}
        
        self.last_known_ema[esp_id] = new_ema

        self.disappeared_since = None

    def cleanup_old_readings(self):
        now = time.time()
        self.readings = {
            esp_id: data for esp_id, data in self.readings.items()
            if now - data["timestamp"] < _config["reading_timeout_sec"]
        }
        return bool(self.readings)

def clear_asset_candidate_state(mac_beacon_to_clear: str):
    """
    Função externa para limpar o estado de candidato OU pendente de um ativo,
    cancelando uma operação em andamento.
    """
    if mac_beacon_to_clear in _asset_realtime_state:
        state = _asset_realtime_state[mac_beacon_to_clear]
        
        state.candidate_quarto_id = None
        state.candidate_since = None
        state.pending_wifi_check_since = None
        state.pending_quarto_id = None
        state.pending_event_details = {}
        
        logger.info(f"[aggregator-cancel] Estado de candidato/pendente para o ativo {mac_beacon_to_clear} foi limpo via API.")
        return True
    return False

async def _consume_scan_data_queue():
    while not scan_data_queue.empty():
        item = await scan_data_queue.get()
        signal_logger.info(json.dumps(item))
        esp_id, payload = item.get("esp_id"), item.get("payload", {})
        beacons, timestamp = payload.get("beacons", []), payload.get("timestamp", time.time())
        for beacon in beacons:
            mac = beacon.get("mac", "").lower()
            if not mac: continue
            if mac not in _asset_realtime_state:
                _asset_realtime_state[mac] = AssetState(mac)
            _asset_realtime_state[mac].update_reading(esp_id, beacon.get("rssi"), timestamp)

async def _processar_localizacoes():
    if not _esp_map or not _asset_map: return

    changes_to_commit = []
    now = time.time() # <--- NOVA ADIÇÃO: 'now' definido no início para ser usado em todo o ciclo
    db = SessionLocal()
    try:
        # <--- INÍCIO DA NOVA SECÇÃO: Processamento de Verificações de Wi-Fi Pendentes ---
        # Este bloco é executado primeiro para verificar o status dos ativos que já estão à espera da confirmação de Wi-Fi.
        for mac, state in list(_asset_realtime_state.items()):
            if state.pending_wifi_check_since is not None:
                asset_info = _asset_map.get(mac)
                if not asset_info: continue
                
                wifi_mac_address = asset_info.get("wifi_mac")

                # SUCESSO: O Wi-Fi foi encontrado
                if wifi_mac_address and await check_presence(wifi_mac_address):
                    logger.info(f"EVENTO CONFIRMADO (Wi-Fi OK): Ativo {mac} confirmado no Quarto {state.pending_quarto_id}.")
                    
                    change = state.pending_event_details
                    change["new_quarto_id"] = state.pending_quarto_id
                    change["details"] = change["details"].replace("Aguardando Wi-Fi", "Wi-Fi confirmado.")
                    changes_to_commit.append(change)

                    if mac in _asset_map:
                        _asset_map[mac]["quarto_id"] = state.pending_quarto_id
                    
                    # Limpa o estado pendente para finalizar a operação
                    state.pending_wifi_check_since = None
                    state.pending_quarto_id = None
                    state.pending_event_details = {}
                    
                # TIMEOUT: A verificação está a demorar demasiado (ex: 3 minutos)
                elif (now - state.pending_wifi_check_since) > 180:
                    logger.warning(f"EVENTO CANCELADO (TIMEOUT): Verificação de Wi-Fi para o ativo {mac} excedeu o tempo limite. A abortar a mudança de quarto.")
                    
                    # Apenas limpa o estado pendente, revertendo a operação
                    state.pending_wifi_check_since = None
                    state.pending_quarto_id = None
                    state.pending_event_details = {}
        # <--- FIM DA NOVA SECÇÃO ---

        for mac, state in list(_asset_realtime_state.items()):
            # <--- MUDANÇA: Ignora ativos que ainda estão pendentes para não processá-los duas vezes no mesmo ciclo
            if state.pending_wifi_check_since is not None:
                continue

            asset_info = _asset_map.get(mac)
            if not asset_info:
                continue 

            asset_id = asset_info.get("id")
            quarto_id_atual = asset_info.get("quarto_id")
            wifi_mac_address = asset_info.get("wifi_mac")

            if not asset_id: continue

            # --- Lógica de SAÍDA POR TIMEOUT (sem alterações) ---
            if not state.cleanup_old_readings():
                if quarto_id_atual is not None:
                    if state.disappeared_since is None: state.disappeared_since = time.time()
                    if (time.time() - state.disappeared_since) * 1000 > _config["inertia_saida_ms"]:
                        logger.info(f"EVENTO OUT (TIMEOUT): Ativo {mac} marcado para remoção do Quarto {quarto_id_atual}.")
                        changes_to_commit.append({
                            "asset_id": asset_id, "new_quarto_id": None,
                            "source_esp_id": state.last_strongest_signal.get("esp_id") or "server_timeout",
                            "rssi": state.last_strongest_signal.get("rssi", -1000),
                            "details": f"Ativo desapareceu (Inércia Saída: {_config['inertia_saida_ms']}ms)"
                        })
                        if mac in _asset_map: _asset_map[mac]["quarto_id"] = None
                        del _asset_realtime_state[mac]
                else:
                    del _asset_realtime_state[mac]
                continue
            
            # --- Lógica de encontrar o candidato mais forte (sem alterações) ---
            strongest_candidate = {"esp_id": None, "rssi": -1000, "ema_rssi": -1000, "quarto_id": None}
            for esp_id, reading in state.readings.items():
                if esp_id not in _esp_map: continue
                q_id, q_rssi = _esp_map[esp_id]
                is_occupied = False
                for other_mac, other_asset_info in _asset_map.items():
                    if other_mac != mac and other_asset_info.get("quarto_id") == q_id:
                        is_occupied = True
                        break
                
                if is_occupied:
                    logger.debug(f"QUARTO OCUPADO: Quarto {q_id} já tem um ativo. Ignorando sinal da ESP {esp_id} para o ativo {mac}.")
                    continue
                threshold = q_rssi if q_rssi is not None else _config["default_rssi_threshold"]
                if reading["ema_rssi"] > threshold and reading["ema_rssi"] > strongest_candidate["ema_rssi"]:
                    strongest_candidate = {
                        "esp_id": esp_id, 
                        "rssi": reading["rssi"],
                        "ema_rssi": reading["ema_rssi"],
                        "quarto_id": q_id
                    }
            
            top_esp, top_read = max(state.readings.items(), key=lambda i: i[1]['ema_rssi'])
            state.last_strongest_signal = {"esp_id": top_esp, "rssi": top_read['rssi'], "ema_rssi": top_read['ema_rssi']}
            candidate_quarto_id = strongest_candidate["quarto_id"]

            # --- Lógica de SAÍDA POR SINAL FRACO (sem alterações) ---
            if quarto_id_atual is not None and not candidate_quarto_id:
                if state.weak_signal_since is None: state.weak_signal_since = time.time()
                elif (time.time() - state.weak_signal_since) * 1000 > _config["inertia_saida_ms"]:
                    logger.info(f"EVENTO OUT (SINAL FRACO): Ativo {mac} marcado para remoção do Quarto {quarto_id_atual}.")
                    changes_to_commit.append({
                        "asset_id": asset_id, "new_quarto_id": None,
                        "source_esp_id": state.last_strongest_signal['esp_id'],
                        "rssi": state.last_strongest_signal['rssi'],
                        "details": f"Sinal (EMA) permaneceu fraco por {_config['inertia_saida_ms']}ms"
                    })
                    if mac in _asset_map: _asset_map[mac]["quarto_id"] = None
                    state.weak_signal_since = None
                    continue
            elif state.weak_signal_since is not None: state.weak_signal_since = None

            # --- Lógica de gestão do candidato (sem alterações) ---
            if candidate_quarto_id == quarto_id_atual:
                state.candidate_quarto_id = quarto_id_atual
                state.candidate_since = None
                continue

            if candidate_quarto_id != state.candidate_quarto_id:
                if quarto_id_atual is not None and candidate_quarto_id is not None:
                    esps_no_quarto_atual = [esp for esp, (q_id, _) in _esp_map.items() if q_id == quarto_id_atual]
                    leituras_ao_vivo = [r["ema_rssi"] for e, r in state.readings.items() if e in esps_no_quarto_atual]
                    if leituras_ao_vivo:
                        rssi_para_comparacao = max(leituras_ao_vivo)
                    else:
                        emas_conhecidos = [ema for esp, ema in state.last_known_ema.items() if esp in esps_no_quarto_atual]
                        rssi_para_comparacao = max(emas_conhecidos) if emas_conhecidos else -1000
                    
                    if strongest_candidate["ema_rssi"] < (rssi_para_comparacao + _config["conflict_margin_db"]):
                        continue
                
                state.candidate_quarto_id = candidate_quarto_id
                state.candidate_since = time.time()

            # <--- INÍCIO DA SECÇÃO MODIFICADA: Lógica de Entrada e Transição para Pendente ---
            # Este bloco agora não confirma a entrada diretamente. Em vez disso, ele inicia o estado de verificação pendente.
            if state.candidate_since and state.candidate_quarto_id is not None:
                # 1. Condição de Inércia (sem alteração)
                #    Verifica se o ativo permaneceu como candidato estável pelo tempo necessário.
                if (now - state.candidate_since) * 1000 > _config["inertia_entrada_ms"]:
                    # 2. Condição de Validade do Candidato (sem alteração)
                    #    Confirma que o candidato forte ainda é o mesmo que iniciou o processo e que é para um novo quarto.
                    if state.candidate_quarto_id == candidate_quarto_id and state.candidate_quarto_id != quarto_id_atual:
                        

                        # REGRA 1: Sem Trocas Diretas. O ativo deve estar livre (quarto_id_atual deve ser None).
                        if quarto_id_atual is not None:
                            logger.info(f"[WH-CONNECT] Atribuição para {mac} BLOQUEADA. O ativo já está no quarto {quarto_id_atual} e precisa de um evento 'OUT' primeiro.")
                            state.candidate_since = None # Reseta o candidato para evitar loops de verificação
                        
                        # REGRA 2: Limite de Ocupação. O quarto de destino deve ter vaga.
                        else:
                            ativos_no_quarto_candidato = [
                                asset for asset in _asset_map.values() if asset.get("quarto_id") == state.candidate_quarto_id
                            ]
                            if len(ativos_no_quarto_candidato) >= _config["max_assets_per_room"]:
                                logger.warning(f"[WH-CONNECT] Atribuição para {mac} BLOQUEADA. O quarto {state.candidate_quarto_id} já atingiu o seu limite de {_config['max_assets_per_room']} ativo(s).")
                                state.candidate_since = None # Reseta o candidato
                            
                            # <--- FIM DAS NOVAS REGRAS DE NEGÓCIO ---

                            # 3. Se todas as regras passaram, inicia o estado PENDENTE (lógica original movida para dentro deste 'else')
                            else:
                                # Caso de exceção: Se o ativo não tem Wi-Fi, confirma diretamente.
                                if not wifi_mac_address:
                                    logger.warning(f"Ativo {mac} não tem um MAC de Wi-Fi. Confirmando entrada no Quarto {state.candidate_quarto_id} diretamente.")
                                    changes_to_commit.append({
                                        "asset_id": asset_id, "new_quarto_id": state.candidate_quarto_id,
                                        "source_esp_id": strongest_candidate['esp_id'], "rssi": strongest_candidate['rssi'],
                                        "details": f"Localizado via {strongest_candidate['esp_id']} (Sem verificação de Wi-Fi)."
                                    })
                                    if mac in _asset_map: _asset_map[mac]["quarto_id"] = state.candidate_quarto_id
                                    state.candidate_since = None
                                
                                # Caso principal: Se tem Wi-Fi, inicia o estado pendente.
                                elif state.pending_wifi_check_since is None:
                                    logger.info(f"EVENTO PENDENTE: Ativo {mac} está estável e passou nas regras para o Quarto {state.candidate_quarto_id}. INICIANDO verificação de Wi-Fi.")
                                    
                                    state.pending_quarto_id = state.candidate_quarto_id
                                    state.pending_wifi_check_since = now
                                    state.pending_event_details = {
                                        "asset_id": asset_id, "source_esp_id": strongest_candidate['esp_id'],
                                        "rssi": strongest_candidate['rssi'],
                                        "details": f"Localizado via {strongest_candidate['esp_id']} com RSSI Bruto {strongest_candidate['rssi']} (EMA {strongest_candidate['ema_rssi']:.1f}). Aguardando Wi-Fi."
                                    }
                                    
                                    # Limpa o estado de candidato para não re-acionar este bloco
                                    state.candidate_since = None
                                    state.candidate_quarto_id = None

        if changes_to_commit:
            await batch_update_asset_assignments(db, changes_to_commit)
    finally:
        db.close()

def _load_maps_from_db():
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
                "wifi_mac": a.mac_address  # Carregamos o novo campo aqui
            } for a in assets
        }
        settings_from_db = {s.key: s.value for s in db.query(GlobalSetting).all()}
        
        new_config = _config.copy()
        new_config["default_rssi_threshold"] = int(settings_from_db.get("rssi_threshold", _config["default_rssi_threshold"]))
        new_config["conflict_margin_db"] = int(settings_from_db.get("conflict_margin_db", _config["conflict_margin_db"]))
        new_config["inertia_entrada_ms"] = int(settings_from_db.get("inercia_entrada", _config["inertia_entrada_ms"]))
        new_config["inertia_saida_ms"] = int(settings_from_db.get("inercia_saida", _config["inertia_saida_ms"]))
        new_config["max_assets_per_room"] = int(settings_from_db.get("max_assets_per_room", 1))
        # Carrega o novo parâmetro também, se existir no DB
        new_config["ema_alpha"] = float(settings_from_db.get("ema_alpha", _config["ema_alpha"]))
        _config = new_config
        
        logger.info(f"[RTLS] Cache (re)carregado: {len(_esp_map)} ESPs, {len(_asset_map)} Ativos. Configs: {str(_config)}")
    finally:
        db.close()

def flag_for_reload():
    _config_needs_reload.set()

async def main_aggregator_loop():
    logger.info("[RTLS] Motor de localização (v. com EMA) iniciado.")
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