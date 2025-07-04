# presence

from .nmap_scan import get_connected_macs
from .config import NETWORK_RANGE

def check_presence(mac):
    """
    Verifica se o MAC está presente na rede, via Nmap (ou fallback ARP).
    """
    connected_macs = get_connected_macs(NETWORK_RANGE)
    presente = mac.lower() in connected_macs
    print(f"[presence] MAC {mac} {'está' if presente else 'não está'} conectado.")
    #print(f"Dispositivos: {connected_macs}")
    return presente
