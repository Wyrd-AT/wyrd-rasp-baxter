# ARQUIVO: app/bed_mqtt_client.py

import paho.mqtt.client as mqtt
import json
import asyncio
import logging
import time
import uuid
from .config import settings

logger = logging.getLogger(__name__)

bed_state_queue = asyncio.Queue()
bed_client = mqtt.Client()

# 1. Configuração Dinâmica
FACILITY_ID = settings.get("facility_id", "HBAX")
BASE_TOPIC = f"2.0/{FACILITY_ID}/hillrom"

# Tópicos de Escuta (Subscribe)
TOPIC_STATE       = f"{BASE_TOPIC}/bed/+/json/state"
TOPIC_WIFI_SIGNAL = f"{BASE_TOPIC}/bed/+/json/wifi/signal_strength"
TOPIC_GRAVITY     = f"{BASE_TOPIC}/bed/+/json/center_of_gravity"
TOPIC_LOC_UPDATE  = f"{BASE_TOPIC}/gateway/connecta/json/location_update"
TOPIC_CMD_RESP    = f"{BASE_TOPIC}/gateway/connecta/command/get_locations/response"

# Tópicos de Envio (Publish)
TOPIC_CMD_REQ     = f"{BASE_TOPIC}/gateway/connecta/command/get_locations/request"
TOPIC_GATEWAY_REQ = f"{BASE_TOPIC}/gateway/connecta/command/get_gateway_state/request"

def on_bed_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload_str = msg.payload.decode('utf-8')
        try:
            payload_json = json.loads(payload_str)
        except json.JSONDecodeError:
            return 

        # Extrai ID do tópico: .../bed/Modelo-ID/...
        parts = topic.split('/')
        full_id = "unknown"
        if "bed" in parts:
            try:
                idx = parts.index("bed")
                if len(parts) > idx + 1:
                    full_id = parts[idx + 1] # Ex: Accella-HRP001...
            except: pass
        
        # A. HEARTBEATS (Sinal Wifi ou Centro de Gravidade)
        if "wifi/signal_strength" in topic or "center_of_gravity" in topic:
            bed_state_queue.put_nowait({
                "type": "HEARTBEAT",
                "id": full_id,
                "timestamp": time.time()
            })

        # B. ESTADO TÉCNICO (Dados da Cama: IP, Mac Wifi, FW)
        elif "json/state" in topic:
            payload_json["type"] = "BED_STATE"
            payload_json["full_id_from_topic"] = full_id
            bed_state_queue.put_nowait(payload_json)

        # C. LOCATION UPDATE (Push passivo)
        elif "location_update" in topic:
            bed_state_queue.put_nowait({"type": "LOCATION_UPDATE", "data": payload_json})

        # D. RESPOSTA DE GET LOCATIONS (Pull ativo)
        elif "get_locations/response" in topic:
            mqtt_resp = payload_json.get("mqttResponse", {})
            data_inner = mqtt_resp.get("data", {})
            if data_inner and "locations" in data_inner:
                bed_state_queue.put_nowait({
                    "type": "LOCATION_UPDATE", 
                    "data": {"locations": data_inner["locations"]}
                })

    except Exception as e:
        logger.error(f"[MQTT-CAMA] Erro: {e}")

def on_bed_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info(f"[MQTT-CAMA] Conectado! Facility: {FACILITY_ID}")
        client.subscribe(TOPIC_STATE, qos=0)
        client.subscribe(TOPIC_WIFI_SIGNAL, qos=0)
        client.subscribe(TOPIC_GRAVITY, qos=0)
        client.subscribe(TOPIC_LOC_UPDATE, qos=0)
        client.subscribe(TOPIC_CMD_RESP, qos=0)
        logger.info("[MQTT-CAMA] Subscrito em todos os tópicos.")
    else:
        logger.error(f"[MQTT-CAMA] Falha conexão código: {rc}")

# --- COMANDOS ---

def send_get_locations_command():
    """Pede a árvore de locais periodicamente."""
    if not bed_client.is_connected(): return
    payload = {
        "command_id": "get_locations",
        "data": None,
        "reply_to": TOPIC_CMD_RESP,
        "transaction_id": str(uuid.uuid4())
    }
    bed_client.publish(TOPIC_CMD_REQ, json.dumps(payload), qos=1)

def send_gateway_check_command(bed_full_id):
    """Verifica se a cama achou o servidor (Keep-Alive)."""
    if not bed_client.is_connected(): return
    
    # Tópico específico de resposta para ESSA cama
    reply_topic = f"2.0/{FACILITY_ID}/hillrom/bed/{bed_full_id}/command/get_gateway_state/response"
    
    payload = {
        "command_id": "get_gateway_state",
        "data": None,
        "reply_to": reply_topic,
        "transaction_id": str(uuid.uuid4())
    }
    bed_client.publish(TOPIC_GATEWAY_REQ, json.dumps(payload), qos=1)
    logger.info(f"[GATEWAY-CHECK] Enviado para {bed_full_id}")

def start_bed_client():
    broker_host = settings.get("bed_mqtt_host", "127.0.0.1")
    broker_port = int(settings.get("bed_mqtt_port", 1883))
    username = settings.get("bed_mqtt_user", None)
    password = settings.get("bed_mqtt_pass", None)

    if username and password:
        bed_client.username_pw_set(username, password)

    bed_client.on_connect = on_bed_connect
    bed_client.on_message = on_bed_message

    try:
        bed_client.connect(broker_host, broker_port, 60)
        bed_client.loop_start()
    except Exception as e:
        logger.error(f"[MQTT-CAMA] Erro fatal: {e}")