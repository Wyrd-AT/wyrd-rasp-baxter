# presence.py

from .nmap_scan import get_connected_macs
from .config import settings

def check_presence(mac: str):
    """
    Verifica se o MAC está presente na rede, via Nmap.
    """
    print(f"[presence] Verificando presença do MAC: {mac}")
    
    # CORREÇÃO: A função get_connected_macs() é chamada sem argumentos.
    # Ela é autossuficiente e busca as configurações necessárias sozinha.
    connected_macs = get_connected_macs()

    presente = mac.lower() in connected_macs
    print(f"[presence] MAC {mac} {'está' if presente else 'não está'} conectado.")
    return presente