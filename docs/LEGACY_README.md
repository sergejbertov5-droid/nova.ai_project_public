> Historical project documentation supplied in the archive. Commands, feature claims and old test counts have not been independently verified for this snapshot. Use the root README and SECURITY.md for presentation guidance.

# NOVA AI — AI Assistant (Enhanced)

## Quick Start

### One-click launch (Desktop)
```bash
# Make executable and run
chmod +x launch_nova_ai.sh stop_nova_ai.sh
./launch_nova_ai.sh          # Background with auto-restart
./launch_nova_ai.sh --foreground  # Foreground (for debugging)
./stop_nova_ai.sh            # Stop background instance
```

### macOS App Bundle
Double-click `NOVA AI.app` in Finder or drag to Dock/Applications.

### Manual run
```bash
source .venv/bin/activate
python main.py
```

**Важно:** семантическая память использует тяжелые ML‑зависимости (`numpy`, `faiss‑cpu`, `sentence‑transformers` и др.). В базовом `.venv` их нет — в этом режиме NOVA AI работает в "text‑only" режиме без семантического поиска. Если нужна полная функциональность и запуск всех тестов, активируйте окружение `.venv2` (в нём уже установлены необходимые пакеты) или установите их в `.venv` вручную:
```bash
source .venv/bin/activate
pip install numpy faiss‑cpu sentence‑transformers
```

---

## Демонстрация (Demo)

### Подготовка за 2 минуты
1. **AI-ключ Groq** (главное): [console.groq.com/keys](https://console.groq.com/keys) →
   *Create API Key* → вставьте в `.env` в `AI_API_KEY` (бесплатно). Без ключа NOVA AI
   работает на эвристике: краткие выводы остаются, но AI-«рассуждения» отключены.
2. **Микрофон** (для голосового ввода): Системные настройки → Конфиденциальность
   и безопасность → Микрофон → отметьте Terminal и/или NOVA AI.app. Если микрофон
   недоступен — NOVA AI **сам переключится на текстовый ввод**.
3. **Геолокация** (для «Где я» и погоды без города): Конфиденциальность →
   Службы геолокации → Terminal/NOVA AI.app.
4. **Telegram**: креды уже в `.env`; при первом «проверь телеграм» в терминал
   придёт код подтверждения — введите его один раз.

### Проверка готовности
При запуске NOVA AI печатает блок **«ГОТОВНОСТЬ К ДЕМО»**: AI / Голос / Микрофон /
Telegram / Chrome — всё, что с пометкой ✗, чинится по подсказке в той же строке.

### Сценарий демо (~3 минуты)
| Вы пишете / говорите | Ответ NOVA AI |
|---|---|
| «Который час» / «Какая дата» | время и дата |
| «Батарея» / «Место на диске» | состояние системы |
| «Погода в Москве» | погода с прогнозом (город приоритетнее геолокации) |
| «Проверь почту» | краткий вывод по важным письмам |
| «Открой браузер» / «Открой YouTube» | Chrome открывает нужный сайт |
| «Напомни, что ...» / «Что ты обо мне помнишь» | постоянная память |
| «Маршрут до ...» | маршрут в Картах |
| «Статус» / «Отчёт» | метрики задач и отчёт |
| «Джарвис, который час» (голос) | wake-word + команда |
| «Выход» | корректное завершение |

### Текстовый режим (без микрофона)
В `.env` поставьте `JARVIS_INPUT_MODE=text`, или NOVA AI переключится сам,
если микрофон недоступен.

### Панель управления и настройки (иконка на рабочем столе)
Клик по **NOVA AI Control** на рабочем столе открывает панель управления:
включить/выключить NOVA AI, открыть логи и **«Настройки»** — четыре вкладки:

- **Безопасность** — мастер-пароль на личные данные: создание, смена,
  удаление. Пока пароль не введён, графы с ключами во вкладке
  «Доступы и данные» **заблокированы** (доступ только у владельца).
  Пароль хранится только PBKDF2-хэшем (`data/vault.json`) и нигде не
  записывается открытым текстом. Здесь же кнопка **«Удалить все личные
  ключи»** — нажимайте её **перед передачей проекта** другому человеку,
  чтобы ключи не утекли. NOVA AI вернётся в режим эвристики до вставки
  новых ключей.
- **Доступы и данные** — подпункты доступа к Telegram и графы подключения
  личных данных (все блокируются мастер-паролем):
  - **Telegram-бот** — доступ к боту (вкл/выкл), поле токена `@BotFather`,
    права бота (читать сообщения / писать от имени владельца / отвечать
    автоматически), параметры ответов (ваш user id, разрешённые чаты,
    приветствие) и кнопка **«Отвязать бота»** (удаляет токен, сессию и
    историю ответов).
  - **Telegram-аккаунт** — `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`
    (my.telegram.org) и их удаление.
  - **AI (Groq)** — `AI_API_KEY`, эндпоинт, модель, `AI_JSON_MODE`.
  - **Озвучка (Fish.audio NOVA AI → ElevenLabs)** — голос NOVA AI из фильма, ключи.
- **Разрешения** — права NOVA AI на действия в компьютере (открытие
  приложений, браузер, файлы, терминал, почта) со степенью риска.
  Сохраняются в `data/permissions.json`.
- **Общие** — режим ввода (голос/текст) и персона (Sol/Terra/Luna).

Изменения ключей и разрешений применяются после перезапуска NOVA AI.

## Автономный запуск при включении Mac (LaunchAgent)

NOVA AI работает как **LaunchAgent** и стартует **сам**, при входе в
пользовательскую сессию macOS — без панели управления, терминала и любой
другой программы:

- Конфиг: `~/Library/LaunchAgents/com.nova_ai.assistant.plist`
- `RunAtLoad` — запускается сразу при логине;
- `KeepAlive` — автоматически перезапускается при падении
  (`SuccessfulExit: false`), но **не** перезапускается после чистого
  остановa кнопкой «Выключить»;
- `ThrottleInterval: 30` — защита от «штормящего» рестарта;
- Статус агента в launchd:
  ```bash
  launchctl list | grep nova_ai
  ```
  (загружен и работает → строка вида `19702  0  com.nova_ai.assistant`).

Управление:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nova_ai.assistant.plist   # запустить
launchctl bootout  gui/$(id -u)/com.nova_ai.assistant                                 # остановить
launchctl print-disabled gui/$(id -u) | grep nova_ai   # должно быть "=> enabled"
```

Если «disabled» — вернуть автозапуск: `launchctl enable gui/$(id -u)/com.nova_ai.assistant`.

> Логика «самого NOVA AI» запускается из-под супервизора
> `launch_nova_ai_daemon.sh`, который поднимает бандл `NOVA AI Daemon.app`
> через LaunchServices, а тот запускает `run_nova_ai_terminal.command`
> (цикл с авторестартом). Поэтому в панели управления кнопка
> «Включить/Выключить» работает именно с LaunchAgent-ом, а статус
> смотрится через launchd.

### Микрофон: как запускается и почему больше не «пищит»

На macOS доступ к микрофону — системное разрешение (TCC), привязанное к
«ответственному» приложению. NOVA AI запускается через бандл
**NOVA AI Daemon.app** (LSUIElement, зарегистрирован в LaunchServices), и цикл
живёт его дочерним процессом:

`launchd` → супервизор `launch_nova_ai_daemon.sh` → `open -g -j "NOVA AI Daemon.app"`
(скрыто, без окна) → `Contents/Resources/daemon.sh` →
`/bin/bash run_nova_ai_terminal.command` (цикл) → `python main.py`.

Почему именно бандл, а не «просто скрипт»: launchd признаёт «ответственным»
только приложение, зарегистрированное в LaunchServices. Прямой запуск
`python main.py` из launchd (равно как через `nohup` с закрытым окном) TCC не
признаёт — NOVA AI глухой: «Сэр, я вас не слышу», при формально исправном
микрофоне и живом индикаторе в Системных настройках.

Поэтому цикл запускается **дочерним** процессом, а не через `exec`: `exec`
заменил бы образ процесса, бандл перестал бы существовать как бандл, TCC
потерял бы его в цепочке вверх и подставил тихий поток микрофона. Бандл
остаётся живым и остаётся ответственным. Супервизор всегда видит актуальный
PID цикла (`logs/nova_ai-term.pid`) и поднимает бандл заново, если он упал.

Подробности — в комментариях `run_nova_ai_terminal.command`,
`launch_nova_ai_daemon.sh` и `NOVA AI Daemon.app/Contents/Resources/daemon.sh`.

### Почему раньше было «Сэр, я вас не слышу» каждые 64 секунды

Это была не поломка микрофона, а ошибка самодиагностики: в тихой комнате
(микрофон исправен, просто никто не говорит) счётчик «пустых сессий» набирал
8 × 8 с ≈ 64 с и NOVA AI вслух «пищал» предупреждение **каждую минуту**. Теперь:

- после долгого молчания NOVA AI вежливо спрашивает «Сэр, вы ещё здесь?»
  (не «жалуется» на микрофон — вопрос о присутствии) — не чаще раза в
  30 минут; остальные циклы молчания только пишут в лог;
- STT-запрос к Google ограничен таймаутом **15 с** (`operation_timeout`) —
  раньше без лимита мёртвый канал к Google «глушил» NOVA AI навсегда;
- диагностика на старте в логе: `Микрофон OK ... RMS=...` либо
  `Микрофон даёт тишину ... RMS=...`. Проверка звука, индекс, язык и
  слово-будильник — `.env`: `STT_MIC_INDEX`, `STT_LANGUAGE`, `STT_WAKE_WORD`.

### Перебивание NOVA AI голосом (barge-in)

Пока NOVA AI говорит, он продолжает кратко слушать микрофон. Скажите
«**Стоп**», «**Замолчи**», «**Хватит**» — NOVA AI немедленно замолкает,
а следующую вашу фразу принимает уже **без** слова-будильника.

---

## Экран, профиль пользователя и распорядок дня (безопасно)

### Безопасное наблюдение за экраном
NOVA AI умеет вежливо «видеть», что происходит на экране — **без слежки**:

- Видит **только метаданные**: какое приложение на переднем плане, какие
  программы открыты, активную вкладку Chrome (для Chrome вы уже разрешили
  чтение вкладок — `BROWSER_READ`).
- **Не читает** содержимое окон, переписку, пароли; клавиши не записывает.
- Вся статистика хранится **локально** в `data/usage_log.json` и никуда
  не отправляется.
- Скриншоты **выключены по умолчанию** (`JARVIS_SCREEN_CAPTURE=false`).
  Включаются только явно и сохраняются локально в `data/screenshots`.

Спросите: «**Что на экране?**», «**Чем я занимаюсь?**»,
«**Сколько я работаю?**».

### Профиль пользователя (что ему интересно)
NOVA AI запоминает, с чем вы работаете и что вам интересно
(файл `data/user_profile.json`, локально):

- «**Я работаю с OpenCode и VS Code**» — запомнит и будет предлагать
  удобные действия (для данного компьютера это OpenCode и Visual Studio Code).
- «**Мой профиль**» / «**Что ты обо мне знаешь?**» — покажет, что запомнил.
- «**Начнём работу**» — откроет рабочие программы из профиля (только
  разрешённые приложения).

### Распорядок дня по времени
NOVA AI сам следит за временем и предлагает полезное в нужный момент:

- Засиделись **в развлечениях** дольше нормы (по умолчанию 45 минут) —
  вежливо предложит вернуться к работе: «…Не хотите ли вернуться к работе —
  могу открыть OpenCode?»
- Отработали **больше нормы** за день (по умолчанию 8 часов) — предложит
  отдохнуть и **задаст вопрос**: «Продолжим работу или завершим день?»
- Подсказки — всегда **вопросы и предложения**, выбор за вами. Можно
  сказать «**Выключи подсказки**» (тихий режим на 3 часа) или «**Включи
  подсказки**».
- Не беспокоит первые минуты после запуска, во время своей речи и не чаще
  кулдауна (настройки `JARVIS_ROUTINE_*` в `.env`).

---

## Живая речь и «гипергенерация» (без рутины)

Чтобы NOVA AI не звучал как заезженная запись, генерация речи построена
так, чтобы каждый ответ был живым и естественным:

- **Температура 0.8** вместо прежних 0.2 (0.2 — самая «предсказуемая», она и
  давала одинаковые рутинные фразы) + лёгкий **джиттер** между запросами.
- **`top_p=0.95` и штрафы за повторы слов** (`frequency_penalty`/
  `presence_penalty`) — модель реже зацикливается на одних и тех же словах.
- **Гипергенерация**: NOVA AI «думает по-человечески» — на обычные ответы
  генерирует **несколько вариантов** и выбирает лучший по оценке: живость,
  вежливость, отсутствие штампов («всё готово», «конечно», «отлично») и
  **неповторение недавних ответов** (помнит последние фразы в сессии).
- **Живой стиль на каждый запрос**: в промпт подмешивается случайное
  указание стиля + текущее время суток, день недели — формулировки меняются.
- Приветствие при старте тоже стало разнообразным (несколько вариантов на
  каждое время суток).

Настройки: `AI_TEMPERATURE`, `AI_TOP_P`, `AI_PENALTIES`, `AI_HYPERGEN*` в
`.env` (подробности — в `.env.example`). Гипергенерацию можно отключить
(`AI_HYPERGEN=false`), если нужно отвечать быстрее.

---

## Веб-поиск и самообучение (безопасно, всё локально)

NOVA AI умеет искать в интернете и **учиться** — и автоматически улучшать
свою работу, не мешая вам:

### Реальный веб-поиск по команде
- «**Найди в интернете X**», «**Загугли X**», «**Что такое X?**» — вернёт
  реальные результаты (заголовок + описание + источник) голосом, без
  открытия Chrome.
- «**Прочитай сайт …**» (`web.fetch`) — прочитает содержимое страницы.
- Поиск идёт по цепочке бесплатных источников: **DuckDuckGo → Wikipedia API
  → Google News RSS**. Если один недоступен — автоматически берётся следующий,
  без ключей и без лимитов.

### Самообучение из интернета
- «**Изучи тему нейросети**» — найдёт материалы, проанализирует AI'ем,
  запомнит факты и **будет использовать их в разговорах** (подмешиваются в
  контекст решений).
- «**Что ты знаешь о …?**» — расскажет изученное; «**Что ты изучил?**» —
  список тем.

### Автоулучшение (фоновое, фоновый поток SelfImprover)
- Периодически (раз в сутки, `JARVIS_LEARN_IMPROVE_HOURS`) NOVA AI сам ищет в
  интернете идеи, как работать лучше, анализирует их и сохраняет локально
  (`data/self_learning.json`).
- Раз в день вежливо спрашивает: «Не хотите ли послушать, что я нашёл?» —
  выбор всегда за вами. Ничего в системе не меняет без спроса.
- «**Покажи улучшения**» — расскажет накопленные идеи.

Настройки: `JARVIS_LEARN_*` в `.env`.

### Что уходит в сеть, а что остаётся на компьютере

Полностью офлайн NOVA AI не работает: ему нужен AI, голос и, по желанию,
Telegram. Ниже — честный список, чтобы не было сюрпризов.

**Остаётся на компьютере** (файлы в `data/`, само никуда не уходит):
- история разговоров и память;
- профиль пользователя, задачи, заметки;
- настройки и разрешения;
- распознавание речи по умолчанию — **локально, на движке Vosk**
  (`models/vosk*`), аудио никуда не отправляется;
- скриншоты (если вы их включили) — только в `data/screenshots`.

**Уходит в сеть** — по каждому пункту это осознанное решение:

| Куда | Что уходит | Как отключить |
|---|---|---|
| AI-провайдер (Gemini / Groq / Ollama) | текст ваших сообщений, контекст разговора, факты из памяти | убрать ключ в `.env` — останется режим эвристики |
| ElevenLabs / Fish.audio | текст, который NOVA AI произносит | `TTS_PROVIDER=elevenlabs` с пустым ключом, либо голос macOS `say` |
| Поиск (DuckDuckGo, Wikipedia, Google News) | сам поисковый запрос | не использовать поиск |
| SelfImprover | тема и найденные материалы — на анализ AI | `JARVIS_LEARN_ENABLED=false` |
| Telegram | ваши сообщения и ответы бота, токен бота | `TG_BOT_ENABLED=false` |
| Почта, браузер, файлы | только то, к чему вы явно обратились | ограничения в панели разрешений |
| VPN `geo-auto` | проверочный запрос к случайным публичным прокси | `JARVIS_VPN_GEO_AUTO=false` |

> **Про `geo-auto` стоит знать:** по умолчанию он **включён**
> (`JARVIS_VPN_GEO_AUTO=true`). NOVA AI сам ищет доступный канал среди
> бесплатных публичных прокси, чтобы открыть доступ к AI из регионов, где
> он закрыт. Туннель действует только на процесс NOVA AI — систему и другие
> программы он не меняет, sudo не нужен, сертификаты не ставятся, ключи API
> идут к провайдеру по TLS (прокси видит только `CONNECT`). Если приватность
> для вас важнее работоспособности AI — выключите одной строкой выше.

Ключи хранятся только в `.env` и не попадают в логи. Телеметрии, аналитики и
сбора статистики в NOVA AI нет вообще.

---

## Собственный VPN-сервис NOVA AI (только для NOVA AI)

Чтобы NOVA AI работал «сразу и без проблем», ещё **до приветствия** он сам
проверяет сеть, выбирает лучший канал-«сервер» и подключается:

- **Каналы (серверы)**, перебираются живой пробой: прямое подключение →
  системный прокси macOS (`scutil`) → свои прокси из `data/vpn_servers.json`,
  `JARVIS_VPN_PROXY` и `JARVIS_VPN_SERVERS` (.env).
- **Проверка до приветствия**: `ensure_connectivity()` в `main.py` идёт раньше
  TTS и AI. Не успел/нет сети за `JARVIS_VPN_CONNECT_TIMEOUT` — честно говорит
  «работаю в локальном режиме» (без паники, AI-fallback на Ollama).
- **Фоновый монитор** (`VPNMonitor`): автофейловер при потере сети и возврат к
  прямому подключению, когда сеть опять есть. Ничего не говорит, только логирует.
- **Команды**: «Как интернет?», «Есть ли интернет?», «Твой VPN»,
  «Какие у тебя сервера?».

**Важно: VPN действует только на NOVA AI.** Туннель применяется через переменные
окружения *внутри собственного процесса* NOVA AI (их честно читает `requests` во
всех сетевых вызовах: AI-провайдеры, поиск, погода, почта). Система и другие
приложения не видят туннель, их трафик идёт как раньше. Ничего не устанавливает,
не требует sudo, не создаёт системные сетевые интерфейсы (utun), порты не
открывает. При отключении канала исходные прокси пользователя (если были)
восстанавливаются.

Настройки: `JARVIS_VPN_*` в `.env`; свои серверы — в `data/vpn_servers.json`.

---

## Telegram-бот (автоответы, когда вы заняты)

NOVA AI может отвечать на ваши сообщения в Telegram **от вашего имени**,
когда вы заняты — особенно на «не особо важные» сообщения:

1. Создайте бота в **@BotFather** (`/newbot`), вставьте токен в настройках
   (`Настройки → Доступы и данные → Telegram-бот`) и включите доступ.
2. Укажите свой user id (телеграм-канал **@userinfobot**) — бот не будет
   отвечать на ваши собственные сообщения.
3. Включите переключатели прав: читать сообщения, писать от вашего имени,
   отвечать автоматически.
4. Перезапустите NOVA AI — бот запустится в фоне.

Как работает: первое сообщение в новом чате бот встречает приветствием
*(«Привет! Я Jar'vis, AI-ассистент Сергей. Сергей сейчас занят, так что
буду отвечать за него…»)*, дальше реплики генерирует сам AI (Groq).
Важные темы (деньги, документы, встречи, срочное) бот не трогает — они
остаются вам через обычный мониторинг входящих.

Все ключи, сессия и история ответов бота удаляются кнопкой
**«Отвязать бота»** в настройках или кнопкой «Удалить все личные ключи».

---

## Voice Improvements

### Fish.audio TTS (Primary — голос NOVA AI из фильма)
- **Голос NOVA AI из «Железного человека»** через Fish.audio (`FISH_VOICE_ID`)
- Бесплатный тир (`s2.1-pro-free`) — экономия кредитов ElevenLabs
- Streaming playback (низкая задержка), авто-фолбэк на ElevenLabs и macOS `say`
- Настройка в `.env`: `FISH_API_KEY`, `FISH_VOICE_ID`, `FISH_MODEL`, `TTS_PROVIDER`

### ElevenLabs TTS (Fallback)
- **High-quality neural voices** via ElevenLabs API
- Streaming playback (low latency)
- Configurable: stability, similarity, style, speaker boost
- Settings in `.env`: `ELEVENLABS_*`
- Подключается автоматически, если Fish.audio недоступен

### macOS Say Fallback
- Automatic fallback if API unavailable
- Uses `Milena` voice at 175 WPM

### Wake Word Detection
- Say "Джарвис", "NOVA AI", "Эй Джарвис", "Hey NOVA AI"
- 10-second active window after wake word
- Configurable in `SpeechRecognitionSTT`

---

## Intelligence Improvements

### Few-Shot Examples (26 cases)
- Covers all 13 tools with proper JSON format
- Memory commands, browser actions, system tools
- Located in `prompts/few_shot.json`

### Pydantic Validation
- All 13 tools have typed argument schemas
- Auto-validation after AI response
- Schemas exposed in tool catalog for AI awareness

### Persona System
- **Sol** (default): Quick, confident, proactive
- **Terra**: Systematic engineer, step-by-step
- **Luna**: Attentive conversationalist
- Switch: "Переключись на режим Terra/Luna/Sol"

---

## Task Monitoring & Regulations

### TaskMonitor (`core/task_monitor.py`)
Records every task execution with:
- Unique task ID, goal, persona, priority
- Step-by-step tool calls with timing
- Success/failure status, errors
- JSONL logs in `logs/tasks/`

### Regulations Enforced
| Rule | Limit |
|------|-------|
| Max steps per task | 30 записей шагов (≈10 итераций агента) |
| Max task duration | 120 seconds |
| Max step duration | 30 seconds |
| Max retries per step | 2 |
| Checkpoint interval | 3 steps |
| Duplicate prevention | 5-second window |

### Metrics & Reports
```python
monitor = get_task_monitor()
monitor.get_metrics()          # Success rate, avg duration, etc.
monitor.get_history(10)        # Last 10 tasks
monitor.export_report()        # Full JSON report
```

### Text Mode Commands
- `статус` / `metrics` — Show metrics
- `история` / `history` — Show recent tasks
- `отчет` / `report` — Export detailed report

---

## Performance Optimizations

| Area | Improvement |
|------|-------------|
| TTS | Streaming Fish.audio → ElevenLabs → afplay (no temp files) |
| STT | AVFoundation native (no PyAudio), VAD tuned |
| AI | Cached prompt templates, escaped JSON |
| Logging | Rotating JSONL (10MB), separate error log |
| Task Loop | Early exit for local commands, no AI call |

---

## Configuration (`.env`)

```env
# Голос NOVA AI (Fish.audio) — необязательно, нужен свой голос
# Свой ID: fish.audio → Reference Models. Без него — фолбэк на ElevenLabs.
TTS_PROVIDER=auto                 # auto | fish | elevenlabs
FISH_API_KEY=ваш_ключ_fish_audio
FISH_VOICE_ID=                     # ← свой ID с fish.audio
FISH_MODEL=s2.1-pro-free
# ElevenLabs TTS (фолбэк)
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_VOICE_ID=pNInz6obpgDQGcFmaJgB
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
ELEVENLABS_STABILITY=0.35
ELEVENLABS_SIMILARITY=0.80
ELEVENLABS_STYLE=0.35
ELEVENLABS_SPEAKER_BOOST=true

# AI (OpenAI-совместимый) — цепочка провайдеров «неограниченно и онлайн»
# FreeLLM API — **ОСНОВНОЙ ПРОВАЙДЕР** (приоритетный, локальный, безлимитен).
# Запуск: ~/freellmapi → docker compose up -d
# Ключ генерируется автоматически в ~/freellmapi/.env
FREELLM_API_KEY=                   # вставьте ключ из ~/freellmapi/.env
FREELLM_BASE_URL=http://localhost:3001
FREELLM_MODEL=auto
# Остальные провайдеры — резервные (авто-фейловер при ошибках FreeLLM):
AI_PROVIDER=auto               # auto | groq | gemini | cerebras | openrouter | nim | ollama | custom
GROQ_API_KEY=                  # https://console.groq.com/keys
GEMINI_API_KEY=                # https://aistudio.google.com/apikey
CEREBRAS_API_KEY=              # https://cloud.cerebras.ai (~1 млн токенов/день)
OPENROUTER_API_KEY=            # https://openrouter.ai/keys
NIM_API_KEY=                   # https://build.nvidia.com
OLLAMA_BASE_URL=http://127.0.0.1:11434   # локально, безлимитно, без ключа
# Legacy: общий ключ (Groq) либо свой эндпоинт (AI_BASE_URL+AI_MODEL+AI_API_KEY = custom)
AI_API_KEY=вставьте_сюда_ключ
AI_BASE_URL=https://api.groq.com/openai/v1
AI_MODEL=llama-3.3-70b-versatile
AI_JSON_MODE=true                      # Groq/Cerebras: true; Gemini/OpenRouter/NIM/Ollama: false

> **AI «неограниченно и онлайн».** FreeLLM API — **приоритетный провайдер**,
> локальный OpenAI-совместимый роутер (`localhost:3001/v1`), запускаемый
> через Colima + Docker (`~/freellmapi`). Он не требует внешнего интернета,
> не имеет ограничений по запросам и предоставляет авто-маршрутизацию к
> лучшим доступным моделям (auto/auto/fusion/...).
> 
> **Установка FreeLLM API:**
> ```bash
> # 1. Установить Colima (Docker на macOS)
> brew install colima docker docker-compose
> colima start
> 
> # 2. Создать папку и запустить FreeLLM
> mkdir -p ~/freellmapi
> cd ~/freellmapi
> cat > docker-compose.yml <<'EOF'
> services:
>   freellmapi:
>     image: ghcr.io/tashfeenahmed/freellmapi:latest
>     env_file:
>       - .env
>     environment:
>       NODE_ENV: production
>       PORT: 3001
>     ports:
>       - "127.0.0.1:3001:3001"
>     volumes:
>       - freellmapi-data:/app/server/data
>     restart: unless-stopped
>     healthcheck:
>       test: ["CMD", "node", "-e", "fetch('http://127.0.0.1:3001/api/ping').then((res) => { if (!res.ok) process.exit(1); }).catch(() => process.exit(1));"]
>       interval: 30s
>       timeout: 5s
>       start_period: 15s
>       retries: 3
> 
> volumes:
>   freellmapi-data:
> EOF
> 
> # 3. Запустить (сгенерирует ключ в .env)
> docker compose up -d
> 
> # 4. Ключ будет в ~/freellmapi/.env — скопируйте его в FREELLM_API_KEY
> cat ~/freellmapi/.env | grep ENCRYPTION_KEY
> ```
> 
> При недоступности FreeLLM NOVA AI автоматически переключается на
> резервные провайдеры в цепочке (`core/ai_providers.py`): Gemini →
> Groq → Cerebras → OpenRouter → NIM → Ollama. Бесплатные лимиты
> провайдеров складываются, а локальная **Ollama** вообще не имеет
> лимитов и работает даже без интернета.
> Ничего из старых настроек не ломается: `AI_API_KEY` + `AI_BASE_URL` +
> `AI_MODEL` продолжают работать как раньше (провайдер Groq / свой эндпоинт).
> Без ключей система переходит в режим эвристики: краткие выводы остаются,
> «рассуждения» AI отключаются — о чём NOVA AI предупреждает при старте.

# Telegram (аккаунт — мониторинг входящих)
# Свой аккаунт: my.telegram.org → API development tools. Номер и API ID
# принадлежат лично вам, в готовой конфигурации их быть не должно.
TELEGRAM_API_ID=0
TELEGRAM_API_HASH=

# Telegram-бот (автоответы, когда владелец занят)
TG_BOT_TOKEN=            # @BotFather → /newbot (секрет)
TG_BOT_ENABLED=false     # разрешить боту запускаться
TG_BOT_READ=true         # право: читать входящие
TG_BOT_WRITE=true        # право: писать от имени владельца
TG_BOT_AUTOREPLY=true    # право: отвечать автоматически
TG_BOT_OWNER_ID=0        # ваш user id (@userinfobot) — бот игнорирует вас
TG_BOT_OWNER_NAME=Сергей
TG_BOT_DIALOGS=          # разрешённые чаты (id через запятую); пусто = все личные
TG_BOT_GREETING=Привет! Я Jar'vis, AI-ассистент {owner}. {owner} сейчас занят, так что буду отвечать за него. Чем могу помочь?

# Мастер-пароль хранится только PBKDF2-хэшем в data/vault.json (не в .env)

# Mode
JARVIS_INPUT_MODE=voice
JARVIS_PERSONA=sol
```

---

## FreeLLM API Integration

FreeLLM API — **приоритетный AI-провайдер** проекта. Это локальный
OpenAI-совместимый роутер (`http://localhost:3001/v1`), запускаемый через
Colima + Docker (`~/freellmapi`).

**Особенности:**
- Работает без внешнего интернета (после первоначальной загрузки контейнера).
- Нет ограничений по количеству запросов.
- Автоматически маршрутизирует запросы к лучшим доступным моделям
  (`auto`, `fusion`, `gemini-3.8-flash`, `claude-opus-4-5` и др.).
- Поддерживает потоковую генерацию (`stream: true`).
- Поддерживает вызовы инструментов (`tools`, `tool_choice`).
- Ошибки авторизации (неверный ключ) возвращают HTTP 401.

**Переменные окружения:**
```
FREELLM_API_KEY=           # ключ из ~/freellmapi/.env
FREELLM_BASE_URL=http://localhost:3001
FREELLM_MODEL=auto
```

**Порядок фолбэка (auto-фейловер):**
1. **FreeLLM** — если доступен и ключ валиден.
2. **Gemini** — Google Gemini (бесплатный уровень AI Studio).
3. **Groq** — Llama 3.3 70B (~30 req/мин).
4. **Cerebras** — GPT-OSS 120B (~1М токенов/день).
5. **OpenRouter** — бесплатные модели (~50 req/день).
6. **NIM** — NVIDIA Nemotron (~40 req/мин).
7. **Ollama** — локально, безлимитно, без ключа.

Если все провайдеры недоступны — NOVA AI переходит в **режим эвристики**
(краткие локальные ответы без AI).

**Известные ограничения:**
- Семантическая память требует `sentence-transformers` + `faiss-cpu`.
  На Python 3.11 + Intel Mac совместимые версии:
  `numpy<2`, `torch==2.2.2`, `sentence-transformers<5`,
  `transformers<4.40`. Без них память работает в **текстовом режиме**
  (добавление/поиск по тексту без эмбеддингов).
- Зависимости PyTorch/NumPy могут конфликтовать — см. раздел
  "Performance Optimizations" и `dependencies_diagnostics.txt`.

---
## Architecture

```
main.py                 # Entry point, loops
├── core/
│   ├── agent_runtime.py      # Enhanced with TaskMonitor
│   ├── agent_runtime_base.py # Base logic
│   ├── assistant.py          # Speaker + Router
│   ├── brain.py              # AI decisions, Pydantic validation, streaming
│   ├── commands.py           # Local commands + AI routing
│   ├── task_monitor.py       # Task observation & regulations
│   ├── tool_registry.py      # ToolSpec with schemas
│   ├── tool_schemas.py       # Pydantic models for all tools
│   ├── briefing.py           # Startup briefing
│   ├── health.py             # Startup checks
│   ├── confirmations.py      # Dangerous action confirmation
│   ├── vault.py              # Master password (PBKDF2) for personal data
│   ├── memory.py             # Persistent memory
│   ├── ai_providers.py       # Provider chain with auto-failover (FreeLLM → Gemini → ...)
│   ├── semantic_memory.py    # Semantic memory with FAISS embeddings
│   ├── planner.py            # Task planning
│   ├── execution_plan.py     # Plan structure and execution
│   ├── executor.py           # Plan executor with retries
│   ├── emotional_intelligence.py  # Emotion tracking
│   ├── self_learning.py      # Self-learning from web
│   └── vpn_service.py        # NOVA AI VPN (network routing)
├── voice/
│   ├── text_to_speech.py     # ElevenLabs + macOS fallback
│   ├── speech_to_text.py     # AVFoundation + Google STT
│   └── state.py              # Speaking state
├── tools/                    # 13 tools (browser, system, mail, etc.)
│   ├── telegram_monitor.py   # Unread inbox monitoring (user account)
│   └── telegram_bot.py       # Auto-replies bot (NOVA AI answers while busy)
└── prompts/
    ├── decide.md             # Main decision prompt
    ├── decide_next.md        # Next step prompt
    ├── few_shot.json         # 26 examples
    └── system_*.md           # Personas
```

---

## Голограмма NOVA AI (интерфейс 2026)

### «Живая» голограмма за всеми окнами — `jarvis_hologram.py`

Проекция NOVA AI исполняется Core Animation (одно прозрачное окно, без кастомного
рендера на CPU). Вид v4: точный светящийся контур головы, извлечённый из силуэта
иконки (сглаживание Chaikin), стеклянная заливка, внутренние пунктирные оболочки
со спицами-связями, восходящие частицы-данные, свип-скан «подсветки», вращающиеся
овалы-кольца со спутниками, ореол и проектор-база с риплом.

**Композиция (снизу вверх):**

1. неоновая циановая **аура** позади силуэта + **ореол-кольцо**;
2. **луч проектора** от базы к голове;
3. **кор голограммы** — контур головы по альфе иконки (бело-циановый, с сильным
   свечением), стеклянный градиент силуэта, 2 пунктирные оболочки, спицы-связи,
   восходящий «дождь» частиц и свип-скан, при котором лицо подсвечивается;
4. два классических **овала-кольца**, вращающиеся вокруг головы (панорамный
   переворот), с бегущими точками-спутниками;
5. HUD: **скобки по углам** и боковые штрих-линии данных;
6. **проектор-база**: платформа, светящаяся линза и расходящееся кольцо-рипл.

Анимации: бут-секвенция, «дыхание», парение, пульс-кольцо, вдох частиц.

**Поведение окна** — строго ПОЗАДИ всех окон и приложений:

- уровень окна `-2147483618` — между обоями (Dock, `-2147483624`) и иконками
  рабочего стола (Finder, `-2147483603`), ниже всех окон (0);
- прозрачное, безрамочное, без тени; игнорирует мышь (клики проходят насквозь);
- отсутствует в Cmd-Tab, Dock и Mission Control.

Запуск:

```bash
.venv/bin/python3 jarvis_hologram.py          # позади всех окон
JARVIS_HOLO_LEVEL=0 .venv/bin/python3 jarvis_hologram.py   # поверх (для отладки)
```

Диагностика без глаз: `JARVIS_HOLO_TEST=1` + offscreen `renderInContext_`
даёт ASCII-предпросмотр композиции.

### Панель управления «2026» — `control_gui.py`

Современный тёмный UI: образ NOVA AI в циановом кольце, статус-чипы
(AI / голос / Telegram-бот), дышащий индикатор Состояния, карточки.
Кнопки: **Включить/Выключить NOVA AI**, **Настройки**, **Логи**,
**Образ** (вкл/выкл голограммы независимо).

### Скрытый запуск без окна Terminal

- `start_nova_ai_hidden.sh` — ставит LaunchAgent (`com.nova_ai.assistant`) и
  открывает голограмму скрыто. Никакое окно не появляется.
- LaunchAgent запускает супервизор `launch_nova_ai_daemon.sh`, который, если
  NOVA AI упал, поднимает его через `open -g -j "NOVA AI Daemon.app"` — бандл
  имеет `LSUIElement`, поэтому ни окна, ни Dock-иконки. Цикл NOVA AI живёт
  дочерним процессом этого бандла, поэтому **право на микрофон сохраняется**
  (TCC признаёт ответственным только зарегистрированный bundle) и супервизор
  всегда видит актуальный PID цикла (`logs/nova_ai-term.pid`).
- `install_launchagent.sh` — тот же агент, но только ставит, без загрузки.
  Собирает plist из того же шаблона `launchd/com.nova_ai.assistant.plist`.
- `NOVA AI.app` (LSUIElement) — двойной клик: скрытый старт NOVA AI + голограмма +
  панель управления. Работает без Dock-иконки и без Cmd-Tab.
- `NOVA AI Hologram.app` — отдельный скрытый запуск голограммы.

> Если NOVA AI не стартует при входе в систему, проверьте, что агент указывает
> на существующую папку проекта: `launchctl print gui/$(id -u)/com.nova_ai.assistant | grep -A2 ProgramArguments`.
> Переустановить агент с правильным путём — `./start_nova_ai_hidden.sh`.

---

## Testing

```bash
# Запуск всех тестов (venv должен быть активирован)
python -m pytest tests/ -q

# Результаты (последний прогон с FreeLLM интеграцией):
# 938 passed, 15 errors (semantic search quality tests — need sentence-transformers),
# 35 failed (pre-existing integration e2e issues, not related to FreeLLM).
# 1 test fixed: test_decide_clarification.py::RuntimeClarificationTest

# Запуск только тестов провайдеров и семантической памяти
python -m pytest tests/test_provider_cooldown.py tests/test_quota_cooldown.py \
    tests/test_semantic_memory.py tests/test_semantic_memory_crud.py \
    tests/test_semantic_performance_recovery.py -q
```

**Зависимости для семантической памяти:**
```bash
# Совместимые версии для Python 3.11 на Intel Mac
pip install "numpy<2" "torch==2.2.2" "faiss-cpu==1.12.0" \
    "sentence-transformers<5" "transformers<4.40"
```

---

## Logs

- `logs/nova_ai.jsonl` — Structured JSON logs (rotating)
- `logs/nova_ai-errors.jsonl` — Errors only
- `logs/tasks/tasks-YYYY-MM-DD.jsonl` — Task execution records
- `logs/nova_ai-app.log` — App bundle output