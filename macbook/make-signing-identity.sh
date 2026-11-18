#!/usr/bin/env bash
# Создаёт устойчивую self-signed identity для подписи «Whisper Dictate Lite».
#
# Зачем: ad-hoc подпись (codesign --sign -) даёт новый отпечаток при каждой сборке, поэтому
# macOS считает обновлённое приложение «другим» и сбрасывает разрешения «Универсальный доступ»
# и «Микрофон». Подпись постоянным сертификатом даёт стабильный designated requirement —
# разрешения переживают переустановку. build.sh подхватывает эту identity автоматически.
#
# Запускать ОДИН раз на самой машине, в обычной сессии (не по SSH): шаг set-key-partition-list
# требует пароль от входа (login keychain) и в headless-сессии зависнет на GUI-запросе.
# После этого все пересборки/обновления подписываются без вопросов.
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

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Создаю сертификат для подписи кода…"
openssl genrsa -out "$work/key.pem" 2048 >/dev/null 2>&1
openssl req -x509 -new -key "$work/key.pem" -out "$work/cert.pem" -days 3650 \
  -subj "/CN=$NAME" \
  -addext "basicConstraints=critical,CA:false" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning" >/dev/null 2>&1
openssl pkcs12 -export -out "$work/id.p12" -inkey "$work/key.pem" -in "$work/cert.pem" \
  -name "$NAME" -passout pass:wdl >/dev/null 2>&1

echo "Импортирую в связку ключей входа…"
security import "$work/id.p12" -k "$KEYCHAIN" -P wdl -T /usr/bin/codesign -T /usr/bin/security >/dev/null

echo
echo "Чтобы codesign не спрашивал доступ к ключу каждый раз, введите пароль от входа в Mac"
echo "(тот же, что при входе в систему; ввод скрыт). Можно пропустить (Enter) — тогда при"
echo "первой сборке macOS один раз спросит «Разрешить» в окне."
read -rs -p "Пароль входа: " LOGIN_PW || true
echo
if [ -n "${LOGIN_PW:-}" ]; then
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$LOGIN_PW" "$KEYCHAIN" >/dev/null 2>&1 \
    && echo "Доступ к ключу настроен — подпись пойдёт без вопросов." \
    || echo "Не удалось настроить доступ (неверный пароль?) — при первой сборке macOS спросит «Разрешить»."
fi

echo
if security find-identity -v -p codesigning | grep -q "$NAME"; then
  echo "Готово. Теперь build.sh/install.sh подписывают «$NAME» — разрешения переживут обновление."
else
  echo "Что-то пошло не так: identity не появилась. Останется ad-hoc подпись." >&2
  exit 1
fi
