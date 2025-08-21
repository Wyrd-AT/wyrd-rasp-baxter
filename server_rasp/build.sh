#!/bin/bash

# --- build.sh (Versão Corrigida) ---
# Script para compilar a aplicação FastAPI em um único executável para Linux.

# Nome do executável final
APP_NAME="Servidor_WH_RTLS"

echo "--- Iniciando o processo de build para $APP_NAME ---"

# 1. ATIVAÇÃO DO AMBIENTE VIRTUAL
# Garante que estamos usando as dependências corretas.
echo "[PASSO 1/4] Ativando ambiente virtual..."
source server_rasp/venv/bin/activate # <-- CORREÇÃO APLICADA AQUI

# 2. ENCONTRAR DEPENDÊNCIAS OCULTAS
# Roda o seu script scan_imports.py e formata a saída para o PyInstaller.
echo "[PASSO 2/4] Buscando por dependências ocultas..."
HIDDEN_IMPORTS=$(python scan_imports.py | grep " -> " | sed 's/ -> /--hidden-import /' | tr '\n' ' ')
echo "Dependências encontradas: $HIDDEN_IMPORTS"

# 3. ENCONTRAR CAMINHOS IMPORTANTES
# Encontra automaticamente o caminho para os templates/estáticos do SQLAdmin.
echo "[PASSO 3/4] Localizando pacotes de site..."
SITE_PACKAGES_PATH=$(python -c "import site; print(site.getsitepackages()[0])")
SQLADMIN_PATH="$SITE_PACKAGES_PATH/sqladmin"
echo "SQLAdmin encontrado em: $SQLADMIN_PATH"

# 4. EXECUTAR O PYINSTALLER
# Monta e executa o comando final com todos os dados e dependências.
echo "[PASSO 4/4] Executando o PyInstaller..."
pyinstaller --name "$APP_NAME" \
    --onefile \
    --noconsole \
    --add-data "config.ini:." \
    --add-data "base_wh.db:." \
    --add-data "alembic.ini:." \
    --add-data "alembic:alembic" \
    --add-data "app/web/templates:web/templates" \
    --add-data "app/web/static:web/static" \
    --add-data "$SQLADMIN_PATH/templates:sqladmin/templates" \
    --add-data "$SQLADMIN_PATH/statics:sqladmin/statics" \
    $HIDDEN_IMPORTS \
    run.py

echo "--- Build Concluído! ---"
echo "Seu executável está em: dist/$APP_NAME"