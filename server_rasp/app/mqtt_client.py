# mqtt_client.py (versão corrigida com a função que faltava)
import paho.mqtt.client as mqtt
import json
from .models import SessionLocal, Badge
from .config import settings

# --- ESTRUTURA ADICIONADA ---
_publish_queue = []

# --- Cliente MQTT (sem alteração) ---
client = mqtt.Client()

def get_available_badges_macs():
    """Busca no banco de dados os MACs de BEACONS dos crachás disponíveis."""
    db = SessionLocal()
    try:
        available_badges = db.query(Badge.mac_beacon).filter(Badge.quarto_id.is_(None)).all()
        mac_list = [mac for mac, in available_badges if mac]
        return mac_list
    finally:
        db.close()

def publish_available_badges():
    """Publica a lista de crachás disponíveis. Se offline, enfileira a publicação."""
    mac_list = get_available_badges_macs()
    payload = json.dumps(mac_list)
    topic = settings.get('mqtt_badge_list_topic')

    if not client.is_connected():
        print(f"[MQTT] Cliente não conectado. Enfileirando publicação para o tópico '{topic}'.")
        _publish_queue.append({'topic': topic, 'payload': payload, 'qos': 1, 'retain': True})
        return

    print(f"[MQTT] Publicando lista de CRACHÁS disponíveis no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1, retain=True)

def publish_verdict(esp_id: str, status: str, beacon_mac: str, transacao_id: int):
    """Publica o resultado de uma disputa para uma ESP específica, incluindo o ID da transação."""
    if not client.is_connected():
        print(f"[MQTT] Cliente não conectado. Abortando envio de veredito para {esp_id}.")
        return

    verdict_topic = f"wyrd/eritel/esp/{esp_id}/verdict"
    payload = json.dumps({
        "status": status,
        "cracha": beacon_mac,
        "transacao_id": transacao_id
    })
    
    print(f"[MQTT] Enviando veredito '{status}' (ID: {transacao_id}) para a ESP '{esp_id}' no tópico '{verdict_topic}'")
    client.publish(verdict_topic, payload, qos=2)

# --- FUNÇÃO QUE ESTAVA FALTANDO ---
def publish_command_to_esp(esp_id: str, command: dict):
    """Publica um comando específico para o canal individual de uma ESP."""
    if not client.is_connected():
        print(f"[MQTT] Cliente não conectado. Abortando envio de comando para {esp_id}.")
        return

    # Este tópico deve ser compatível com o que o ESP espera
    command_topic = f"wyrd/eritel/esp/{esp_id}/command"
    payload = json.dumps(command)

    print(f"[MQTT] Enviando comando {payload} para a ESP '{esp_id}' no tópico '{command_topic}'")
    client.publish(command_topic, payload, qos=2)
# --- FIM DA FUNÇÃO QUE ESTAVA FALTANDO ---

def on_connect(client, userdata, flags, rc):
    """Callback executado quando a conexão com o broker é (re)estabelecida."""
    if rc == 0:
        print("[MQTT] Conectado com sucesso ao Broker MQTT!")
        publish_available_badges()

        if _publish_queue:
            print(f"[MQTT] Enviando {_publish_queue.__len__()} mensagens da fila de espera...")
            for msg in list(_publish_queue):
                client.publish(
                    topic=msg['topic'],
                    payload=msg['payload'],
                    qos=msg.get('qos', 1),
                    retain=msg.get('retain', False)
                )
                _publish_queue.remove(msg)
            print("[MQTT] Fila de mensagens processada.")
    else:
        print(f"[MQTT] Falha ao conectar, código de retorno: {rc}\n")

def connect_mqtt():
    """Inicia a conexão com o broker MQTT."""
    client.on_connect = on_connect
    try:
        broker_port = int(settings.get("mqtt_broker_port"))
        client.connect(settings.get("mqtt_broker_host"), broker_port, 60)
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Não foi possível conectar ao broker: {e}")