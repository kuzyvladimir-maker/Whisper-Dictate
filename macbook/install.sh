#!/usr/bin/env bash
# Whisper Dictate Lite — установка одной командой (Intel и Apple Silicon, macOS 11+).
#
#   curl -fsSL https://raw.githubusercontent.com/kuzyvladimir-maker/Whisper-Dictate/main/macbook/install.sh | bash
#
# или из клона репозитория:   bash macbook/install.sh [--with-local]
#
#   --with-local   ещё и локальный фолбэк: whisper.cpp + модель small (~470 МБ)
#
# Что делает: собирает приложение из исходников (нужны Xcode Command Line Tools),
# кладёт его в ~/Applications, включает автозапуск, создаёт ~/.config/whisper-dictate/.
# Ключи Gemini сам не трогает: их кладут в ~/.config/whisper-dictate/env.

set -euo pipefail

main() {
  local repo="kuzyvladimir-maker/Whisper-Dictate"
  local branch="${WDL_BRANCH:-main}"
  local app_name="Whisper Dictate Lite"
  local bundle_id="com.whisperdictate.lite"
  local install_dir="$HOME/Applications"
  local app="$install_dir/$app_name.app"
  local conf_dir="$HOME/.config/whisper-dictate"
  local local_dir="$HOME/.local/share/whisper-dictate"
  local plist="$HOME/Library/LaunchAgents/$bundle_id.plist"
  local with_local=0
  local arg
  for arg in "$@"; do
    case "$arg" in
      --with-local) with_local=1 ;;
      *) echo "Неизвестный параметр: $arg" >&2; exit 2 ;;
    esac
  done

  say "Whisper Dictate Lite: установка на $(sw_vers -productName) $(sw_vers -productVersion), $(uname -m)"

  # 1. Компилятор Swift из Command Line Tools.
  if ! xcrun --find swiftc >/dev/null 2>&1; then
    say "Нужны Xcode Command Line Tools. Сейчас откроется окно установки."
    say "Когда установка закончится, запустите эту команду ещё раз."
    xcode-select --install >/dev/null 2>&1 || true
    exit 1
  fi

  # 2. Исходники: рядом со скриптом (клон) или свежий архив с GitHub.
  local src="" tmp=""
  local self="${BASH_SOURCE[0]:-}"
  if [ -n "$self" ] && [ -f "$(dirname "$self")/build.sh" ] && [ -d "$(dirname "$self")/Sources" ]; then
    src="$(cd "$(dirname "$self")" && pwd)"
  else
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" EXIT
    say "Скачиваю исходники ($repo@$branch)..."
    curl -fsSL "https://github.com/$repo/archive/refs/heads/$branch.tar.gz" | tar -xz -C "$tmp"
    src="$(find "$tmp" -maxdepth 2 -type d -name macbook | head -1)"
    [ -n "$src" ] || { echo "В архиве нет папки macbook/" >&2; exit 1; }
  fi

  # 3. Сборка под эту машину.
  local build_out
  build_out="$(mktemp -d)/$app_name.app"
  bash "$src/build.sh" "$build_out"

  # 4. Замена установленной версии.
  local upgrading=0
  [ -d "$app" ] && upgrading=1
  launchctl bootout "gui/$(id -u)/$bundle_id" >/dev/null 2>&1 || true
  pkill -x WhisperDictateLite >/dev/null 2>&1 || true
  mkdir -p "$install_dir"
  rm -rf "$app"
  ditto "$build_out" "$app"
  rm -rf "$(dirname "$build_out")"
  if [ "$upgrading" = 1 ]; then
    # Новая подпись: старое разрешение «Универсальный доступ» больше не действует, сбрасываем,
    # чтобы macOS спросила заново (иначе галочка стоит, но не работает).
    tccutil reset Accessibility "$bundle_id" >/dev/null 2>&1 || true
    tccutil reset Microphone "$bundle_id" >/dev/null 2>&1 || true
  fi

  # 5. Настройки и словарь (существующие не трогаем).
  mkdir -p "$conf_dir"
  chmod 700 "$conf_dir"
  if [ ! -f "$conf_dir/env" ]; then
    cat > "$conf_dir/env" <<'ENV'
# Ключи Gemini для Whisper Dictate Lite. Файл не коммитить и никому не пересылать.
# Несколько бесплатных ключей — через запятую: при лимите (429) приложение переключится на следующий.
GEMINI_API_KEY=
ENV
  fi
  chmod 600 "$conf_dir/env"
  if [ ! -f "$conf_dir/dictionary.txt" ]; then
    cp "$src/dictionary.example.txt" "$conf_dir/dictionary.txt"
  fi

  # 6. Локальный фолбэк (по желанию).
  if [ "$with_local" = 1 ]; then
    install_local "$local_dir"
  fi

  # 7. Fn — клавиша диктовки: убираем системное действие Fn/🌐 (смена раскладки, эмодзи),
  #    иначе оно срабатывает на каждое отпускание. Вернуть: Настройки → Клавиатура.
  local fn_note=""
  if [ "$(defaults read com.apple.HIToolbox AppleFnUsageType 2>/dev/null || echo unset)" != "0" ]; then
    defaults write com.apple.HIToolbox AppleFnUsageType -int 0
    fn_note="Системное действие клавиши Fn/🌐 выключено («Ничего не делать»), чтобы Fn работала для диктовки."
  fi

  # 8. Автозапуск при входе.
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$bundle_id</string>
  <key>ProgramArguments</key>
  <array><string>$app/Contents/MacOS/WhisperDictateLite</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/WhisperDictateLite.stderr.log</string>
</dict>
</plist>
PLIST
  if [ -z "${WDL_NO_LAUNCH:-}" ]; then
    launchctl bootstrap "gui/$(id -u)" "$plist"
  fi

  # 9. Итог.
  local keys_state="НЕТ — впишите ключ (см. ниже)"
  if grep -Eq '^[[:space:]]*(export[[:space:]]+)?GEMINI_API_KEYS?=[^[:space:]]' "$conf_dir/env"; then
    keys_state="есть"
  fi
  say ""
  say "Готово. Приложение: $app (значок микрофона в строке меню)."
  say "Ключ Gemini: $keys_state  →  $conf_dir/env"
  say ""
  say "Первый запуск:"
  say "  1. macOS спросит «Универсальный доступ» → Открыть Системные настройки → включите Whisper Dictate Lite."
  say "     (Нужен для горячей клавиши и вставки текста.)"
  say "  2. macOS спросит доступ к микрофону → Разрешить."
  say "  3. Ключ: откройте $conf_dir/env и впишите GEMINI_API_KEY=ключ1,ключ2"
  say "     затем меню значка → «Перечитать ключи и словарь»."
  say "  4. Держите Fn и говорите, отпустите — текст вставится."
  say "     Короткое нажатие — запись до следующего нажатия. Esc — отмена."
  say "     Другая клавиша (правый ⌘ или правый ⌥) — в меню значка."
  if [ -n "$fn_note" ]; then say ""; say "$fn_note"; fi
  say ""
  say "Проверка: \"$app/Contents/MacOS/WhisperDictateLite\" --selftest"
}

install_local() {
  local dir="$1"
  local model="$dir/ggml-small.bin"
  local sha1="55356645c2b361a969dfd0ef2c5a50d530afd8d5"
  mkdir -p "$dir/bin"
  if [ -x "$dir/bin/whisper-cli" ]; then
    say "whisper.cpp уже установлен."
  elif command -v whisper-cli >/dev/null 2>&1; then
    ln -sf "$(command -v whisper-cli)" "$dir/bin/whisper-cli"
    say "whisper.cpp найден: $(command -v whisper-cli)"
  elif command -v brew >/dev/null 2>&1; then
    say "Ставлю whisper.cpp через Homebrew..."
    brew install whisper-cpp
    ln -sf "$(command -v whisper-cli)" "$dir/bin/whisper-cli"
  elif command -v cmake >/dev/null 2>&1; then
    say "Собираю whisper.cpp из исходников..."
    local wtmp
    wtmp="$(mktemp -d)"
    local metal=ON
    [ "$(uname -m)" = "x86_64" ] && metal=OFF   # Metal в ggml на Intel-видеокартах ненадёжен
    git clone --depth 1 --branch v1.8.7 https://github.com/ggml-org/whisper.cpp.git "$wtmp/whisper.cpp"
    cmake -S "$wtmp/whisper.cpp" -B "$wtmp/build" -DCMAKE_BUILD_TYPE=Release -DGGML_METAL="$metal" \
      -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF \
      -DCMAKE_OSX_DEPLOYMENT_TARGET="$(sw_vers -productVersion | cut -d. -f1).0"
    cmake --build "$wtmp/build" --config Release -j"$(sysctl -n hw.ncpu)" --target whisper-cli
    cp "$wtmp/build/bin/whisper-cli" "$dir/bin/whisper-cli"
    rm -rf "$wtmp"
  else
    say "Для локального фолбэка нужен Homebrew (https://brew.sh) или cmake. Пропускаю."
    return 0
  fi
  if [ ! -f "$model" ]; then
    say "Скачиваю модель whisper small (~470 МБ)..."
    curl -fL --progress-bar "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin" -o "$model.part"
    if [ "$(shasum -a 1 "$model.part" | awk '{print $1}')" != "$sha1" ]; then
      rm -f "$model.part"
      say "Контрольная сумма модели не совпала, локальный фолбэк не установлен."
      return 0
    fi
    mv "$model.part" "$model"
  fi
  say "Локальный фолбэк готов: whisper.cpp + small."
}

say() { printf '%s\n' "$1"; }

main "$@"
