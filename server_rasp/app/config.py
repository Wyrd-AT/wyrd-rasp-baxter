# config.py
import configparser
import os
import logging
logger = logging.getLogger(__name__)

# Define o nome do arquivo de configuração que ficará ao lado do .exe
CONFIG_FILE = 'config.ini'

def load_configuration():
    """
    Lê o arquivo config.ini e retorna um dicionário com as configurações.
    Se o arquivo não existir, cria um com valores padrão.
    """
    config = configparser.ConfigParser()
    
    if not os.path.exists(CONFIG_FILE):
        logger.info(f"Arquivo '{CONFIG_FILE}' não encontrado. Criando com valores padrão.")
        config['Network'] = {
            'IP': '0.0.0.0',
            'PORT': '8080'
        }
        config['MQTT'] = {
            'MQTT_BROKER_HOST': '127.0.0.1',
            'MQTT_BROKER_PORT': '1883',
            'MQTT_ASSET_LIST_TOPIC': 'wyrd/rtls/assets/available',
            'MQTT_ESP_COMMAND_TOPIC': 'wyrd/rtls/esp/all/command'
        }
        # --- NOVA SEÇÃO ADICIONADA ---
        config['Aggregator'] = {
            'process_interval_sec': '2.0',
            'reading_timeout_sec': '10',
            'disappearance_tolerance_cycles': '10' # Novo parâmetro para a lógica de saída
        }

        config['RFID'] = {
            'SERIAL_PORT': 'COM3'  # Define um padrão razoável para Windows
        }

        config['UI_FEATURES'] = {
            'show_rtls_historico': 'true',
            'show_rtls_embarcados': 'true',
            'show_rtls_assets': 'true',
            'show_rtls_quartos': 'true',
            'show_rtls_planta': 'true',
            'show_rfid_inventario': 'true',
            'show_rfid_cadastro': 'true',
            'show_rfid_catalogo': 'true'
        }
        
        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
    
    config.read(CONFIG_FILE)
    
    # Junta todas as configurações de todas as seções em um único dicionário
    settings = {}
    for section in config.sections():
        settings.update(config[section])
        
    return settings

# Carrega as configurações para serem importadas por outros módulos
settings = load_configuration()