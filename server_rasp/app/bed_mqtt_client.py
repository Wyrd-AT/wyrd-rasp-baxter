import paho.mqtt.client as mqtt
import json
import asyncio
import logging
from .config import settings

logger = logging.getLogger(__name__)

bed_state_queue = asyncio.Queue()

bed_client = mqtt.Client()

def on_bed_message(client, userdata, msg):
    try:
        payload_str = msg.payload.decode('utf-8')
        payload_json = json.loads(payload_str)
        
        # Verifica qual tópico mandou a mensagem
        if "location_update" in msg.topic:
            # É uma atualização de Mapa (IDs do Connecta)
            # Colocamos na fila com um tipo especial
            bed_state_queue.put_nowait({
                "type": "LOCATION_UPDATE",
                "data": payload_json
            })
            logger.info(f"[MQTT-CONNECTA] Recebida atualização de mapa/IDs (Versão: {payload_json.get('location_list_version')})")
        else:
            # É mensagem de estado de Cama normal
            # Adiciona o tipo para o processador saber diferenciar
            payload_json["type"] = "BED_STATE"
            bed_state_queue.put_nowait(payload_json)
            # logger.info(...) # Opcional: manter log de cama aqui se quiser
        
    except Exception as e:
        logger.error(f"[MQTT-CAMA] Erro ao processar mensagem: {e}")

def on_bed_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("[MQTT-CAMA] Conectado ao Broker!")
        
        # 1. Tópico das Camas
        client.subscribe("2.0/HIAE/hillrom/bed/+/json/state", qos=0)
        
        # 2. NOVO: Tópico de Atualização de Localização (Connecta)
        client.subscribe("2.0/HIAE/hillrom/gateway/connecta/json/location_update", qos=0)
        
        logger.info("[MQTT-CAMA] Subscrito aos tópicos de Cama e Location.")
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