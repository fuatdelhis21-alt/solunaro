#!/usr/bin/env bash
# Solunaro — Six Agent AI Trading Desk — VPS kurulumu (Ubuntu/Debian, root olarak çalıştır)
#
#   unzip solunaro.zip && cd solunaro
#   sudo bash install.sh            # kur + veri testi + testler (servisi BAŞLATMAZ)
#   sudo bash install.sh --start    # yukarıdakiler + systemd servisini başlat
#
# API anahtarları sohbete değil, buraya gizli girişle yazılır; sadece /opt/solunaro/.env
# dosyasına (chmod 600) kaydedilir. Tekrar çalıştırmak güvenlidir: mevcut .env ve veriler korunur.
set -euo pipefail

APP=/opt/solunaro
SVC=solunaro
START=0
[[ "${1:-}" == "--start" ]] && START=1

[[ $EUID -eq 0 ]] || { echo "Root olarak çalıştır: sudo bash install.sh"; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$SRC/run.py" && -d "$SRC/desk" ]] || { echo "install.sh proje klasörünün içinde olmalı"; exit 1; }

echo "==> Paketler"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sudo >/dev/null

echo "==> Kullanıcı ve dosyalar"
id solunaro &>/dev/null || useradd -r -m -s /usr/sbin/nologin solunaro
mkdir -p "$APP"
# Çalışan servis kod değişirken yarım dosya okumasın: güncelleme süresince durdur.
WAS_ACTIVE=0
if systemctl is-active --quiet "$SVC" 2>/dev/null; then
  WAS_ACTIVE=1
  echo "    servis güncelleme için durduruluyor"
  systemctl stop "$SVC"
fi
# kodu güncelle ama .env, data/ ve STOP dosyasına dokunma
rm -rf "$APP/desk" "$APP/tests" "$APP/tools"
tar -C "$SRC" --exclude="./.env" --exclude="./data" --exclude="./STOP" --exclude="./.venv" -cf - . \
  | tar -C "$APP" -xf -

echo "==> Python ortamı"
[[ -d "$APP/.venv" ]] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "==> Anahtarlar (.env)"
# Var olan .env ASLA üzerine yazılmaz (Telegram vb. ayarlar kaybolmasın). Anahtar zorunlu değil:
# testler, veri kontrolü ve backtest anahtarsız çalışır; sadece 'once'/'loop' LLM çağırır.
if [[ -f "$APP/.env" ]]; then
  echo "    .env zaten var, korunuyor. Değiştirmek için: sudo nano $APP/.env"
else
  read -rsp "    ANTHROPIC_API_KEY (sk-ant-..., boş geçebilirsin; ekranda görünmez): " AK; echo
  read -rp  "    TELEGRAM_BOT_TOKEN (boş geçebilirsin): " TT
  read -rp  "    TELEGRAM_CHAT_ID (boş geçebilirsin): " TC
  umask 077
  cat > "$APP/.env" <<EOF
ANTHROPIC_API_KEY=$AK
TELEGRAM_BOT_TOKEN=$TT
TELEGRAM_CHAT_ID=$TC
CRYPTOPANIC_TOKEN=
EOF
  unset AK TT TC
fi
if ! grep -q '^ANTHROPIC_API_KEY=sk-ant-' "$APP/.env"; then
  echo "    UYARI: ANTHROPIC_API_KEY 'sk-ant-' ile başlamıyor veya boş. LLM döngüsü çalışmaz."
  echo "    Düzelt: sudo nano $APP/.env   Kontrol: aşağıdaki 'keycheck' adımı"
fi
chmod 600 "$APP/.env"
chown -R solunaro:solunaro "$APP"

run_as_solunaro() { sudo -u solunaro bash -c "cd $APP && set -a && . ./.env && set +a && $*"; }

echo "==> Çevrimdışı testler"
TEST_LOG="$(mktemp)"
if ! run_as_solunaro ".venv/bin/python -W ignore -m unittest discover -s tests" > "$TEST_LOG" 2>&1; then
  tail -30 "$TEST_LOG"
  echo "TESTLER BAŞARISIZ. Kurulum durduruldu, servis başlatılmadı."
  exit 1
fi
tail -3 "$TEST_LOG"; rm -f "$TEST_LOG"

echo "==> Canlı veri kontrolü (LLM yok, ücretsiz)"
# Ağ kesintisi kurulumu durdurmaz, ama gizlenmez.
run_as_solunaro ".venv/bin/python run.py data" || echo "UYARI: veri kontrolü başarısız (ağ/kaynak). Çıktıyı incele."

echo "==> Servis dosyası"
sed "s#/usr/bin/python3 /opt/solunaro/run.py#$APP/.venv/bin/python $APP/run.py#" \
    "$APP/solunaro.service" > "/etc/systemd/system/$SVC.service"
systemctl daemon-reload

if [[ $START -eq 1 || $WAS_ACTIVE -eq 1 ]]; then
  systemctl enable --now "$SVC"
  sleep 3
  systemctl --no-pager status "$SVC" | head -12
  echo
  echo "Bot çalışıyor. Loglar: journalctl -u $SVC -f"
else
  cat <<EOF

Kurulum tamam, servis BAŞLATILMADI. Sıradaki adımlar:
  1) Yukarıdaki 'run.py data' çıktısında FAILED satırı var mı kontrol et.
  2) Baseline geçmiş testi (ücretsiz, birkaç dakika):
       sudo -u solunaro bash -c 'cd $APP && .venv/bin/python run.py backtest --days 180'
  3) API anahtarı kontrolü (ücretsiz, token harcamaz):
       sudo -u solunaro bash -c 'cd $APP && set -a && . ./.env && set +a && .venv/bin/python run.py keycheck'
     Tek bir tam döngü (6 LLM çağrısı, ücretli):
       sudo -u solunaro bash -c 'cd $APP && set -a && . ./.env && set +a && .venv/bin/python run.py once'
  4) Her şey yolundaysa sürekli çalıştır:
       sudo bash install.sh --start

Durdurmak: sudo systemctl stop $SVC   |   Acil durdurma: sudo -u solunaro touch $APP/STOP
Yedek:     sudo tar -C $APP -czf ~/solunaro-yedek.tgz data .env
EOF
fi
