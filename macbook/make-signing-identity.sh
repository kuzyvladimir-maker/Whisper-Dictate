#!/usr/bin/env bash
# Создаёт устойчивую self-signed identity для подписи «Whisper Dictate Lite».
#
# Зачем: ad-hoc подпись (codesign --sign -) даёт новый отпечаток при каждой сборке, поэтому
# macOS считает обновлённое приложение «другим» и сбрасывает разрешения «Универсальный доступ»
# и «Микрофон». Подпись постоянным сертификатом даёт стабильный designated requirement —
# разрешения переживают переустановку. build.sh подхватывает эту identity автоматически.
#
# Запускать ОДИН раз на самой машине, в обычной сессии (не по SSH): доступ к ключу
# может потребовать подтверждения в графической сессии.
# Разрешения после обновления нужно проверить на целевом Mac.
#
#   bash macbook/make-signing-identity.sh
#
# Удалить потом: security delete-identity -c "Whisper Dictate Lite Self-Signed" login.keychain-db

set -euo pipefail

NAME="Whisper Dictate Lite Self-Signed"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if ! command -v openssl >/dev/null 2>&1; then
  echo "Нужен openssl (есть в macOS по умолчанию)." >&2
  exit 1
fi

if security find-identity -v -p codesigning 2>/dev/null | grep -q "$NAME"; then
  echo "Identity «$NAME» уже есть — ничего не делаю."
  exit 0
fi

umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Создаю сертификат для подписи кода…"
openssl genrsa -out "$work/key.pem" 2048 >/dev/null 2>&1
cat > "$work/cert.cnf" <<'CONF'
[req]
distinguished_name = dn
x509_extensions = code_signing
prompt = no
[dn]
CN = Whisper Dictate Lite Self-Signed
[code_signing]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CONF
openssl req -x509 -new -key "$work/key.pem" -out "$work/cert.pem" -days 3650 \
  -config "$work/cert.cnf" >/dev/null 2>&1
openssl pkcs12 -export -out "$work/id.p12" -inkey "$work/key.pem" -in "$work/cert.pem" \
  -name "$NAME" -passout pass:wdl >/dev/null 2>&1

echo "Импортирую в связку ключей входа…"
security import "$work/id.p12" -k "$KEYCHAIN" -P wdl -T /usr/bin/codesign -T /usr/bin/security >/dev/null

echo
echo "При первой подписи macOS может запросить доступ codesign к этому ключу."
echo "Подтвердите его в локальной графической сессии. Пароль скрипт не запрашивает."

echo
if security find-identity -v -p codesigning | grep -q "$NAME"; then
  echo "Готово. Теперь build.sh/install.sh подписывают «$NAME» — проверьте разрешения после обновления."
else
  echo "Что-то пошло не так: identity не появилась. Останется ad-hoc подпись." >&2
  exit 1
fi
