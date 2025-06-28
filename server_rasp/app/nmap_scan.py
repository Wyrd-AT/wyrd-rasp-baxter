import subprocess
import re
import platform
from datetime import datetime
from .config import FINAL_IP, PORT, NETWORK_RANGE, NETWORK_PREFIX

# Se estivermos no Linux/RaspPi, importe o Scapy
if platform.system() != "Windows":
    from scapy.all import ARP, Ether, srp

from .models import SessionLocal, Bed

# -----------------------------------------------------------------------------
# Fallback Windows: ping sweep paralelo + arp -a
# -----------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor

def _ping_ip(ip):
    # -n 1: um ping; -w 50: timeout 50 ms
    subprocess.run(f"ping -n 1 -w 50 {ip}",
                   shell=True,
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

def get_macs_via_arp_parallel(network_prefix=NETWORK_PREFIX):
    """
    Faz um ping sweep em paralelo (50 threads) e depois lê arp -a
    para capturar todos os MACs na cache ARP.
    """
    print("[nmap_scan] Fallback Windows: ping sweep paralelo + arp -a…")
    ips = [f"{network_prefix}{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=50) as pool:
        pool.map(_ping_ip, ips)

    result = subprocess.run("arp -a",
                            shell=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True)
    output = result.stdout
    print(f"[nmap_scan] Saída do arp -a:\n{output}")

    macs = re.findall(r"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})", output)
    # Converte '-' para ':' e lower
    macs = [m.replace("-", ":").lower() for m in macs]
    print(f"[nmap_scan] MACs encontrados (ARP): {macs}")
    return macs

# -----------------------------------------------------------------------------
# ARP‐scan rápido via Scapy (Linux/RaspPi)
# -----------------------------------------------------------------------------
def arp_scan_scapy(network=NETWORK_RANGE):
    """
    Dispara um broadcast ARP via Scapy e captura todas as respostas.
    Funciona em ~1–2 segundos num Linux/RaspPi.
    """
    print(f"[nmap_scan] ARP‐scan via Scapy em {network}…")
    pkt = Ether(dst="ff:ff:ff:ff:ff:ff")/ARP(pdst=network)
    ans, _ = srp(pkt, timeout=2, verbose=False)
    macs = [rcv[ARP].hwsrc.lower() for _, rcv in ans]
    print(f"[nmap_scan] MACs encontrados (Scapy): {macs}")
    return macs

# -----------------------------------------------------------------------------
# Interface unificada
# -----------------------------------------------------------------------------
def get_connected_macs(network=NETWORK_RANGE):
    """
    Retorna todos os MACs ativos na rede.
    - No Windows: ping sweep paralelo + arp -a.
    - No Linux: ARP‐scan via Scapy; se falhar, tenta nmap.
    """
    system = platform.system()
    if system == "Windows":
        prefix = network.rsplit(".", 1)[0] + "."
        return get_macs_via_arp_parallel(network_prefix=prefix)

    # Linux/RaspPi: tenta Scapy
    try:
        return arp_scan_scapy(network)
    except Exception as e:
        print(f"[nmap_scan] Scapy falhou ({e}), tentando nmap…")

    # Fallback para nmap
    cmd = f"nmap -sn {network}"
    result = subprocess.run(cmd, shell=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.stderr:
        print(f"[nmap_scan] Erro no nmap: {result.stderr.strip()}")
    output = result.stdout
    print(f"[nmap_scan] Saída do nmap:\n{output}")

    mac_addresses = re.findall(r"MAC Address: ([\\w:]+)", output)
    print(f"[nmap_scan] MACs encontrados (nmap): {mac_addresses}")
    return mac_addresses

# -----------------------------------------------------------------------------
# Se você quiser persistir no banco, pode implementar aqui:
#    from .models import SessionLocal, Bed
#    def save_macs(macs): ...
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Teste rápido
    print(get_connected_macs())
