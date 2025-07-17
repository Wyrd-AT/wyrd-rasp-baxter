# scan_imports.py
import os
from modulefinder import ModuleFinder
import sys

# --- Configuração ---
# O ponto de entrada principal do seu aplicativo
ENTRY_POINT_SCRIPT = 'run.py'

# Lista dos pacotes principais do seu requirements.txt para garantir que sejam incluídos
KNOWN_PACKAGES = [
    'uvicorn', 'fastapi', 'paho', 'requests',
    'sqlalchemy', 'sqladmin', 'scapy', 'starlette'
]

# --- Lógica do Scanner ---
print("--- Iniciando scanner de dependências ---")

# Pega o caminho da biblioteca padrão do Python para podermos ignorá-la
try:
    stdlib_path = os.path.dirname(os.__file__)
except:
    # Fallback para ambientes onde os.__file__ pode não estar definido
    from distutils.sysconfig import get_python_lib
    stdlib_path = get_python_lib(standard_lib=True)


finder = ModuleFinder(
    path=[os.getcwd()] + sys.path
)
finder.run_script(ENTRY_POINT_SCRIPT)

print(f"\n[INFO] Módulos encontrados a partir de '{ENTRY_POINT_SCRIPT}':")

all_top_level_modules = set()

for name, mod in finder.modules.items():
    # A heurística é: se não for da biblioteca padrão, nos interessa
    if mod.__file__ and not mod.__file__.startswith(stdlib_path):
        # Adiciona apenas o nome base do pacote (ex: "fastapi" em vez de "fastapi.routing")
        base_package = name.split('.')[0]
        if base_package and base_package not in ['run', 'scan_imports']: # Ignora os próprios scripts
             all_top_level_modules.add(base_package)

# Adiciona os pacotes conhecidos para garantir
for pkg in KNOWN_PACKAGES:
    all_top_level_modules.add(pkg)

print("\n--- LISTA DE BIBLIOTECAS PRINCIPAIS PARA O PYINSTALLER ---")
print("Use esta lista para os argumentos --collect-submodules e --hidden-import.")

for module_name in sorted(list(all_top_level_modules)):
    print(f" -> {module_name}")