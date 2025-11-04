# app/bed_mqtt_client.py (NOVO ARQUIVO)

import paho.mqtt.client as mqtt
import json
import asyncio
from .config import settings
import logging

logger = logging.getLogger(__name__)

# Fila dedicada para dados de status da cama (conectado/desconectado)
bed_state_queue = asyncio.Queue()

# Cria um objeto cliente SEPARADO
bed_client = mqtt.Client()

def on_bed_message(client, userdata, msg):
    """Callback focado APENAS nas Camas."""
    topic = msg.topic
    
    try:
        payload_str = msg.payload.decode('utf-8')
        payload_json = json.loads(payload_str)
        # Coloca a mensagem na fila para o main.py processar
        bed_state_queue.put_nowait(payload_json) 
        logger.debug(f"[MQTT-CAMA] Mensagem de status da cama recebida e enfileirada: {payload_json.get('id')}")
    except json.JSONDecodeError:
        logger.warning(f"[MQTT-CAMA] JSON inválido recebido no tópico de cama: {topic}")
    except Exception as e:
        logger.error(f"[MQTT-CAMA] Erro ao processar mensagem de cama: {e}")

def on_bed_connect(client, userdata, flags, rc):
    """Callback focado APENAS nas Camas."""
    if rc == 0:
        logger.info("[MQTT-CAMA] Conectado ao Broker de Camas (Hillrom)!")
        topic = "2.0/HIAE/hillrom/bed/+/json/state"
        client.subscribe(topic, qos=0)
        logger.info(f"[MQTT-CAMA] Subscrito ao tópico: '{topic}'")
    else:
        logger.error(f"[MQTT-CAMA] Falha ao conectar ao broker de camas, código: {rc}")

def start_bed_client():
    """Inicializa o cliente MQTT dedicado para as Camas."""
    logger.info("[MQTT-CAMA] Iniciando cliente das Camas...")
    
    # Busca as credenciais específicas no config.ini
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
        logger.error(f"[MQTT-CAMA] Não foi possível conectar ao broker de camas: {e}")