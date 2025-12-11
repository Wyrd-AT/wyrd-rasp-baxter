import paho.mqtt.client as mqtt
import json
import asyncio
import logging
from .config import settings

logger = logging.getLogger(__name__)

# --- CORREÇÃO: A fila deve ser definida aqui no topo ---
bed_state_queue = asyncio.Queue()
# -----------------------------------------------------

bed_client = mqtt.Client()

def on_bed_message(client, userdata, msg):
    try:
        payload_str = msg.payload.decode('utf-8')
        payload_json = json.loads(payload_str)
        
        # Coloca na fila para o main.py processar
        bed_state_queue.put_nowait(payload_json)
        
        # Log Informativo para debug
        logger.info(f"[MQTT-CAMA] Recebido: ID='{payload_json.get('id')}' Modelo='{payload_json.get('model')}'")
        
    except Exception as e:
        logger.error(f"[MQTT-CAMA] Erro ao processar mensagem: {e}")

def on_bed_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("[MQTT-CAMA] Conectado ao Broker de Camas!")
        # Subscreve ao tópico genérico para pegar todas as camas
        topic = "2.0/HIAE/hillrom/bed/+/json/state"
        client.subscribe(topic, qos=0)
    else:
        logger.error(f"[MQTT-CAMA] Falha conexão código: {rc}")

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
        logger.error(f"[MQTT-CAMA] Erro fatal na conexão: {e}")