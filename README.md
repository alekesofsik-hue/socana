# SOCANA

**SOCANA** — гибридный сервис (Rules + AI) для чтения алертов Kaspersky из IMAP, анализа с учетом контекста инфраструктуры и отправки отчетов в Telegram.

## Возможности

- IMAP polling (SSL) + фильтрация писем по `FROM cloud_noreply@kaspersky.com`
- Детерминированный парсер писем (HTML/Text → key-value → нормализованный `KasperskyEvent`)
- Контекст активов (SQLite): `UNCLASSIFIED` / `SERVER` / `WORKSTATION`
- Дедупликация/анти-спам: окно `ANTI_SPAM_WINDOW_SECONDS` + порог `ANTI_SPAM_REPEAT_THRESHOLD`
- Telegram (aiogram 3.x):
  - `/assets` — список хостов + классификация (SERVER/WORKSTATION) + удаление
  - кнопка **«Подробности»** → вывод raw-текста письма из БД
- CrewAI (опционально): 3 агента (Analyst / Researcher / Dispatcher) + фоллбек на правила при `ENABLE_LLM=false`

## Быстрый старт

1) Создай `.env` по примеру `env.example` (в этом окружении создание dot-файлов может быть ограничено).

2) Установи зависимости:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3) Запуск:

```bash
python main.py run
```

Дополнительно:

```bash
python main.py run-once
python main.py get-updates
python main.py imap-debug --sample 10
python main.py reset-db --yes
```

## Установка как системный сервис (systemd) — Вариант 1

Ниже — рекомендуемый способ, чтобы SOCANA **работал постоянно** и **автозапускался после перезагрузки**.

### 1) Убедись, что проект запускается вручную

```bash
cd /home/ezovskikh_a/apps/socana
source .venv/bin/activate
python main.py run
```

Останови `Ctrl+C`.

### 2) Установи unit-файл systemd

В репозитории есть готовый unit: `deploy/systemd/socana.service`.

Скопируй его в systemd и перечитай конфиг:

```bash
sudo cp /home/ezovskikh_a/apps/socana/deploy/systemd/socana.service /etc/systemd/system/socana.service
sudo systemctl daemon-reload
```

### 3) Включи автозапуск и запусти сервис

```bash
sudo systemctl enable --now socana
```

### 4) Проверка статуса и логи

```bash
sudo systemctl status socana --no-pager
sudo journalctl -u socana -f
```

### 5) Управление

```bash
sudo systemctl restart socana
sudo systemctl stop socana
```

Примечания:
- SOCANA читает `.env` из рабочей директории (см. `WorkingDirectory=/home/ezovskikh_a/apps/socana`). Убедись, что `.env` лежит рядом с `main.py`.
- Если меняешь `.env` — делай `sudo systemctl restart socana`.
- Если ты используешь `EnvironmentFile=` в unit-файле: учти, что systemd может некорректно прочитать значения с “особыми” символами (например `#`) и в итоге получится `AUTHENTICATIONFAILED` для IMAP. Рекомендуется **не** подгружать `.env` через `EnvironmentFile` и дать SOCANA читать `.env` самостоятельно.

## Чистый старт (с чистого листа)

Если хочешь полностью обнулить SOCANA (БД событий/дедуп/ассетов) и заново прогнать письма:

- **1) Останови сервис**, если он запущен (`Ctrl+C` в терминале с `python main.py run`).
- **2) Сбрось БД SOCANA**:

```bash
python main.py reset-db --yes
```

- **3) В почтовом клиенте отметь нужные письма как “Непрочитанные”** (UNSEEN), чтобы SOCANA увидел их при polling.
- **4) Проверь глазами, что IMAP реально видит UNSEEN**:

```bash
python main.py imap-debug --sample 5
```

Убедись, что `UNSEEN_FROM > 0`.

- **5) Первый прогон**:

```bash
python main.py run-once
```

- **6) Боевой режим**:

```bash
python main.py run
```

## Конфигурация (.env)

Все ключи перечислены в `env.example`. Ниже — подробное описание каждого.

### IMAP

- **IMAP_HOST**: IMAP сервер. Для Timeweb: `imap.timeweb.ru`.
- **IMAP_PORT**: порт IMAP over SSL. Обычно `993`.
- **IMAP_USERNAME**: логин IMAP (обычно полный email или имя ящика у провайдера).
- **IMAP_PASSWORD**: пароль IMAP (или app-password, если включена 2FA).
- **IMAP_FROM_FILTER**: фильтр отправителя. Для Kaspersky Cloud: `cloud_noreply@kaspersky.com`.
- **IMAP_MAILBOX**: папка для чтения. Обычно `INBOX`. Можно указать другую, если алерты попадают в отдельную папку.
- **IMAP_MARK_SEEN**: если `true`, SOCANA после обработки письма ставит флаг `\\Seen` (ack), чтобы UNSEEN polling не гонял одни и те же письма бесконечно.
  - Рекомендуется `true` для прод/теста, иначе сервис будет каждый цикл видеть те же UNSEEN (и будет расти дедуп-счетчик).
- **IMAP_POLL_INTERVAL_SECONDS**: интервал проверки IMAP (polling) в секундах.
  - По умолчанию: `60` (в коде есть минимум `5`, чтобы случайно не поставить 0/1 и не “ддосить” ящик).
  - Пример: `IMAP_POLL_INTERVAL_SECONDS=120` (проверка раз в 2 минуты).

### Telegram

- **TELEGRAM_BOT_TOKEN**: токен бота от BotFather (формат `123456:ABCDEF...`).
- **TELEGRAM_CHAT_ID**: числовой `chat_id`, куда SOCANA шлет отчеты.
  - Если пусто — сервис **не будет отправлять уведомления**, но продолжит парсить и писать в БД.
  - Как получить: запусти `python main.py get-updates` после того, как отправишь боту любое сообщение.
- **TELEGRAM_ADMIN_USER_IDS**: список `user_id` Telegram, которые считаются **админами** SOCANA:
  - могут управлять `/assets` и делать привязку владельца к хосту (через меню `/assets` или `/bind <HOST> <USER_ID>`).
  - Пример: `TELEGRAM_ADMIN_USER_IDS=123456789,987654321`

#### Рекомендуемый режим “просто и безопасно” (по умолчанию)

- **1 чат для админов**: заполни только `TELEGRAM_CHAT_ID`.
- **Админы по user_id**: заполни `TELEGRAM_ADMIN_USER_IDS`.
- **Онбординг владельцев**:
  - пользователь пишет боту `/start` и сразу видит свой `user_id`
  - админ привязывает `user_id` к хосту в `/assets` → кнопка **Bind owner**
- **Важно про безопасность**:
  - “raw email / Подробности” доступны **только админам**
  - владельцам хостов алерты отправляются **без** кнопки “Подробности” (чтобы не раскрывать raw‑письма)

#### Advanced / optional

- **TELEGRAM_ADMIN_CHAT_IDS**: дополнительные `chat_id` (через запятую), куда SOCANA будет отправлять алерты **всегда** (вместе с `TELEGRAM_CHAT_ID`, если он задан).
  - Пример: `TELEGRAM_ADMIN_CHAT_IDS=-1001234567890,123456789`
- **TELEGRAM_ALLOWED_USER_IDS**: старый глобальный allow-list (может мешать онбордингу пользователей). Обычно оставляй пустым.

### Database

- **SQLITE_PATH**: путь к SQLite файлу.
  - Рекомендуется **абсолютный путь** (пример в `env.example`).
  - Файл будет создан автоматически (таблицы — тоже) при первом запуске.

### Dedup / Anti-spam (строки 17–19 в `env.example`)

SOCANA считает fingerprint (SHA256) от связки полей события:  
`device + event_type + detection_name + object_path + result`  
и использует его, чтобы не спамить в Telegram повторяющимися письмами.

- **ANTI_SPAM_WINDOW_SECONDS**: “окно” дедупликации в секундах (по умолчанию `600` = 10 минут).
  - Если в течение окна приходит событие с тем же fingerprint, оно считается повтором и **не отправляется** в Telegram.
  - Если окно истекло, следующее такое событие считается “новым” и может быть отправлено.
  - Практика:
    - 300–900 сек — типично для SOC, чтобы не забивать канал.
    - 0/малое значение приведет к частым уведомлениям.

- **ANTI_SPAM_REPEAT_THRESHOLD**: порог “эскалации по частоте” (по умолчанию `3`).
  - SOCANA все равно увеличивает счетчик повторов в БД, даже если не отправляет уведомление.
  - Когда количество повторов достигает этого порога, SOCANA может отправить “burst”-сообщение (и далее ограничивает частоту этим же окном), чтобы ты увидел массовость.
  - Рекомендации:
    - `3` — хорошо для ловли “зацикленных” повторов.
    - `10+` — если у вас очень шумные источники.

#### Как подбирать `ANTI_SPAM_WINDOW_SECONDS` и `ANTI_SPAM_REPEAT_THRESHOLD`

Эти два параметра отвечают за “сколько шума вы готовы видеть” в Telegram при повторяющихся алертах одного типа на одном хосте.

- **ANTI_SPAM_WINDOW_SECONDS (окно)** — это минимальная “паузa” между уведомлениями про одинаковый fingerprint.
  - Выбирай окно, равное **типичному интервалу повторов**, который не несет новой информации.
  - Практика:
    - Если алерты повторяются каждые 10–30 секунд (шумная волна) → ставь 600–1800 сек.
    - Если алерты редкие, но важные (раз в 10–30 минут) → 300–600 сек.

- **ANTI_SPAM_REPEAT_THRESHOLD (порог)** — это “когда шум становится сигналом” и стоит отправить burst-уведомление о массовости.
  - Подбирается от того, сколько повторов в окне ты считаешь уже значимым.
  - Практика:
    - 3–5: быстро покажет “что-то зациклилось/массово пошло”.
    - 10–20: подходит при очень шумной среде, где 3 повтора — норма.

- **Быстрый способ настроить по частоте**:
  - Пусть \(R\) — ожидаемое число повторов в минуту для “типичного шума” одного fingerprint.
  - Тогда:
    - **окно** ≈ 5–15 минут (чтобы “схлопывать” волну)
    - **порог** ≈ \(R \times \text{окно\_в\_минутах}\) / 2 (чтобы burst был только при реально заметной волне)
  - Пример: 2 повтора/мин и окно 10 минут → ожидаемо 20 повторов. Порог 10 даст burst только при длительной серии.

#### Готовые профили (рекомендуемые пресеты)

Скопируй нужные значения в `.env`.

- **prod (сбалансировано)**:
  - `ANTI_SPAM_WINDOW_SECONDS=600`
  - `ANTI_SPAM_REPEAT_THRESHOLD=3`
  - `ENABLE_LLM=false`
  - `LOG_LEVEL=INFO`

- **noisy (очень шумная среда / много одинаковых алертов)**:
  - `ANTI_SPAM_WINDOW_SECONDS=1800`
  - `ANTI_SPAM_REPEAT_THRESHOLD=10`
  - `ENABLE_LLM=false` (включать позже, иначе LLM будет “жечь” лимиты на шум)
  - `LOG_LEVEL=INFO`

- **test (отладка/проверка, увидеть почти всё)**:
  - `ANTI_SPAM_WINDOW_SECONDS=60`
  - `ANTI_SPAM_REPEAT_THRESHOLD=2`
  - `ENABLE_LLM=false` (или `true`, если тестируешь CrewAI)
  - `LOG_LEVEL=DEBUG`

### AI (строки 21–24 в `env.example`)

- **ENABLE_LLM**: включить CrewAI-обработку.
  - `false` (рекомендуется для начала): **rules-first** — сервис формирует базовый summary без LLM.
  - `true`: после rules/enrich SOCANA прогоняет событие через CrewAI (Analyst/Researcher/Dispatcher).
  - Важно: если `true`, но LLM недоступен/ошибка сети — система не должна падать (см. retry/exception handling в `soc_core/app.py`).

- **OPENAI_API_KEY**: ключ LLM-провайдера (используется CrewAI через выбранную конфигурацию LLM).
  - Если `ENABLE_LLM=false`, ключ не требуется.
  - Если `ENABLE_LLM=true` и ключ пустой — CrewAI, скорее всего, не сможет работать.

- **OPENAI_MODEL**: модель для LLM (пример: `gpt-4o-mini`).
  - Используется как строковый идентификатор при создании CrewAI агентов.
  - Рекомендация для стабильного теста/прода: `gpt-5-mini-2025-08-07` (быстрее/дешевле, чем pro, и обычно надежнее по коннектам).

- **PROMPTS_PATH**: (опционально) путь к YAML-файлу с промптами/инструкциями CrewAI.
  - Если не задан — используется дефолтный файл `soc_core/prompts.yaml`.
  - Удобно, если ты хочешь редактировать промпты без правок кода (например, хранить их рядом с `.env`).

### Файл промптов CrewAI (`soc_core/prompts.yaml`)

В этом файле лежат все “инструкции” для агентов (role/goal/backstory) и суффиксы задач (task prompts).
Ты можешь безопасно редактировать этот YAML — сервис подхватит изменения при следующей обработке события (перезапуск не обязателен, но рекомендуется).

Минимальная структура:

```yaml
analyst:
  role: "Security Analyst (SOC Expert)"
  goal: "Assess real risk and detect patterns across events"
  backstory: "..."
researcher:
  role: "Threat Researcher (Intelligence)"
  goal: "Provide MITRE ATT&CK technique and Kaspersky references for HIGH/CRITICAL"
  backstory: "..."
dispatcher:
  role: "Telegram Dispatcher"
  goal: "Format concise Telegram report"
  backstory: "..."
tasks:
  soc_analysis_suffix: "SOC analysis:"
  threat_research_suffix: "Threat research (only if HIGH/CRITICAL):"
  telegram_report_suffix: "Telegram report:"
```

### Web research tools (строки 26–28 в `env.example`)

Эти ключи нужны Threat Researcher агенту (по желанию), чтобы подтягивать ссылки/описания из внешних источников.
Если ключи пустые — сервис продолжит работать, просто без web-enrichment.

- **SERPER_API_KEY**: ключ Serper (`google.serper.dev`) для поиска.
- **TAVILY_API_KEY**: ключ Tavily (`api.tavily.com`) для поиска/сводок.

#### Где взять ключи Serper / Tavily (для тестового запуска)

Эти ключи **не обязательны** для теста SOCANA. Если оставить пустыми — сервис будет работать без web-enrichment.
Но если ты хочешь, чтобы агент Threat Researcher прикладывал ссылки/контекст из веба, ключи нужны.

- **SERPER_API_KEY (Serper)**:
  - **Где зарегистрироваться**: `https://serper.dev/`
  - **Где взять ключ**: после входа в кабинет открой Dashboard/Account → API Key (названия разделов могут отличаться) и создай/скопируй ключ.
  - **Как проверить**: Serper показывает usage/лимиты в дашборде. В SOCANA запросы идут на `https://google.serper.dev/search` (см. `soc_core/tools.py`).
  - **Куда вставить**: в `.env` → `SERPER_API_KEY=...`

- **TAVILY_API_KEY (Tavily)**:
  - **Где зарегистрироваться**: `https://tavily.com/`
  - **Где взять ключ**: в кабинете найди API Keys и создай/скопируй ключ.
  - **Как проверить**: в кабинете Tavily обычно есть usage/квоты. В SOCANA запросы идут на `https://api.tavily.com/search` (см. `soc_core/tools.py`).
  - **Куда вставить**: в `.env` → `TAVILY_API_KEY=...`

#### Мини-чеклист тестового запуска

- **1) Настрой `.env`** (на базе `env.example`):
  - **IMAP_\***: логин/пароль ящика, куда падают письма Kaspersky
  - **TELEGRAM_BOT_TOKEN**: токен бота
  - **SQLITE_PATH**: абсолютный путь (можно оставить как в примере)
  - Для теста рекомендуемый пресет:
    - `ANTI_SPAM_WINDOW_SECONDS=60`
    - `ANTI_SPAM_REPEAT_THRESHOLD=2`
    - `LOG_LEVEL=DEBUG`

- **2) Получи `TELEGRAM_CHAT_ID`**:
  - Напиши боту любое сообщение (в личку или в группе, где он состоит)
  - Запусти: `python main.py get-updates`
  - Скопируй `chat_id=...` в `.env`

- **3) Запусти тест**:
  - Разовый прогон: `python main.py run-once`
  - Полный режим (bot + polling): `python main.py run`
  - Если “в webmail письма непрочитанные, а SOCANA не видит UNSEEN” или наоборот — проверь реальную картину через IMAP:
    - `python main.py imap-debug --sample 10`

### Logging (строки 30–31 в `env.example`)

- **LOG_LEVEL**: уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
  - `INFO` — дефолт для прод-подобного режима.
  - `DEBUG` — удобно при отладке парсинга/IMAP.

## Архитектура

Пакет `soc_core/`:

- `app.py` — асинхронный loop (polling)
- `config.py` — конфигурация (env → settings)
- `imap_client.py` — IMAP клиент (fetch писем)
- `parser.py` — детерминированный парсер Kaspersky (BeautifulSoup + Regex)
- `database.py` — SQLite (SQLAlchemy async) + репозитории/дедуп
- `agents.py` — CrewAI-агенты
- `tasks.py` — оркестрация AI/Rules pipeline
- `tools.py` — web tools (Serper/Tavily) для Threat Researcher
- `bot.py` — aiogram-бот (команды + callbacks)
- `models.py` — Pydantic модели домена/DTO

