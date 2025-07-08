# config.py

# Endereço e porta do servidor TCP
IP = "10.0.0.149"
FINAL_IP = "10.0.0.126"
PORT = 9500
FINAL_PORT = 9501

# Rede usada nos scans
NETWORK_RANGE = "10.0.0.0/24"

NETWORK_PREFIX = "10.0.0."

# Historiador
HISTORY_RETENTION_DAYS = 7       # mantém apenas 7 dias de eventos
EVENT_PAGE_SIZE         = 50     # linhas por página em /events
CLEANUP_INTERVAL_SEC    = 3600   # a cada hora roda a limpeza

# MQTT
MQTT_BROKER_HOST = "10.0.0.149" # ou o IP do seu PC
MQTT_BROKER_PORT = 1883
MQTT_BED_LIST_TOPIC = "wyrd/baxter/beds/available"