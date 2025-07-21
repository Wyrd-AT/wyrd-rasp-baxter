# config.py
import configparser
import os

CONFIG_FILE = 'config.ini'

def load_configuration():
    """
    Lê o arquivo config.ini e retorna um dicionário com as configurações.
    Se o arquivo não existir, cria um com TODAS as configurações padrão.
    """
    config = configparser.ConfigParser()
    
    if not os.path.exists(CONFIG_FILE):
        print(f"Arquivo '{CONFIG_FILE}' não encontrado. Criando com valores padrão completos.")
        
        config['Network'] = {
            'IP': '10.0.0.149',
            'Port': '8000',
            'Network_Range_Scan': '10.0.0.0/24' # ATENÇÃO: Ajuste para a sua rede
        }
        
        config['Dispatcher'] = {
            'Final_IP': '10.0.0.126',
            'Final_Port': '9500'
        }

        config['MQTT'] = {
            'Broker_Host': '10.0.0.149',
            'Broker_Port': '1883',
            'Bed_List_Topic': 'wyrd/baxter/beds/available',
            'Command_Topic': 'wyrd/baxter/esp/all/command',
            'Individual_Command_Topic': 'wyrd/baxter/esp/{esp_id}/command'
        }

        config['Application'] = {
            'History_Retention_Days': '7',
            'Event_Page_Size': '20',
            'Cleanup_Interval_Sec': '3600',
            'Warning_Delay_Minutes': '5',
        }

        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
    
    config.read(CONFIG_FILE)
    
    settings = {}
    for section in config.sections():
        settings.update(config[section])
        
    return settings

settings = load_configuration()