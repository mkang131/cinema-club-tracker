# Cinema Club Tracker

[![CI](https://github.com/mkang131/cinema-club-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/mkang131/cinema-club-tracker/actions/workflows/ci.yml)

Бэкенд на FastAPI для одного закрытого клуба кинолюбителей. Участники ищут фильмы, сохраняют их в каталог, ставят оценки и оставляют отзывы. Клубный рейтинг можно сравнить с рейтингами Кинопоиска и IMDb. Интерфейс для ручной работы — Swagger.

## Установка

Нужны Python 3.12 и Git. В PowerShell:

```powershell
git clone https://github.com/mkang131/cinema-club-tracker.git
cd cinema-club-tracker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

В Linux/macOS используй `python3.12 -m venv .venv`, активацию `source .venv/bin/activate` и копирование `cp .env.example .env`. Остальные команды одинаковые. Дальше выполняй их из папки проекта с активированным окружением.

Один раз после копирования `.env.example` сгенерируй приватный `JWT_SECRET`. Команда записывает его в `.env` без вывода в терминал:

```powershell
python -c "from pathlib import Path; import secrets; p=Path('.env'); p.write_text(p.read_text(encoding='utf-8').replace('JWT_SECRET=', 'JWT_SECRET=' + secrets.token_hex(32), 1), encoding='utf-8')"
```

При повторном запуске используй существующий `.env`. Он, базы, письма, логи и виртуальное окружение исключены из Git. Не добавляй их и реальные секреты в репозиторий.

## Настройки

Параметры читаются из окружения и локального `.env`; образец — [.env.example](.env.example). Окружение процесса имеет приоритет. После изменения настроек перезапусти сервер.

| Переменная | Назначение и значение по умолчанию |
|---|---|
| `APP_ENV` | `development`, `test` или `production`; по умолчанию `development` |
| `APP_VERSION` | Версия в `/version`, `0.1.0` |
| `DATABASE_URL` | SQLite: `sqlite:///./data/cinema.db` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`; по умолчанию `INFO` |
| `JWT_SECRET` | Обязательный случайный секрет, минимум 32 символа |
| `ACCESS_TOKEN_TTL_MINUTES` | Срок токена входа, 30 минут |
| `RATING_WEIGHTS` | JSON с шестью положительными весами; пример в `.env.example`, все веса равны 1 |
| `KINOPOISK_API_KEY` | Ключ [Kinopoisk API Unofficial](https://kinopoiskapiunofficial.tech/): регистрация на сайте → личный кабинет |
| `EXTERNAL_CACHE_TTL_SECONDS` | Актуальность сохранённых внешних данных, 86400 секунд |
| `SMTP_HOST`, `SMTP_PORT` | SMTP-сервер; в локальном примере `localhost`, `1025` |
| `SMTP_USER`, `SMTP_PASSWORD` | SMTP-учётные данные; в локальном примере пустые |
| `SMTP_FROM`, `SMTP_TLS` | Адрес отправителя и STARTTLS; локально `cinema-club@example.org`, `false` |

Ключ кино API и SMTP не нужны для запуска, регистрации и работы с уже сохранёнными фильмами. Без ключа внешние поиск и импорт возвращают `503 / external_service_not_configured`. При сбое обновления устаревшая карточка возвращается с `external_data_stale: true`; сохранённый каталог остаётся доступен. Условия и квоты внешнего сервиса проверяй у провайдера.

## Запуск

```powershell
python -m alembic upgrade head
python -m uvicorn app_final:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Миграции создают таблицы; папка `data` создаётся автоматически. Фильмы и записи участников сохраняются в SQLite после перезапуска. Сервер останавливается через `Ctrl+C`.

- [Swagger](http://127.0.0.1:8000/docs) — схемы и отправка запросов.
- [Health](http://127.0.0.1:8000/health) — `200`, `{"status":"ok","database":"ok"}` при доступной БД.
- [Version](http://127.0.0.1:8000/version) — `{"version":"0.1.0"}` при настройках по умолчанию.

Проверка из PowerShell: `Invoke-RestMethod http://127.0.0.1:8000/health`; в Linux: `curl http://127.0.0.1:8000/health`.

## Первый участник и фильм

В другом терминале активируй то же окружение. Создай приглашение для участника:

```powershell
python -m app.cli create-invite --email member@example.org
```

Код выводится один раз, действует 7 дней и подходит только указанному email. Передай его участнику приватно.

В Swagger нажимай **Try it out → Execute**. Значения в угловых скобках замени своими.

1. `POST /api/v1/auth/register`:

   ```json
   {
     "email": "member@example.org",
     "password": "<свой пароль от 12 до 128 символов>",
     "invitation_code": "<код из CLI>"
   }
   ```

2. В `POST /api/v1/auth/login` передай JSON с теми же `email` и `password`. Скопируй `access_token` из ответа. Нажми **Authorize** и вставь только токен — Swagger добавит `Bearer`. Проверить вход можно через `GET /api/v1/users/me`.
3. Найди фильм: `GET /api/v1/catalog/films?query=Матрица`. Для подбора доступны фильтры и сортировки в Swagger; жанры и страны — в `GET /api/v1/catalog/filters`. Эти запросы требуют ключа кино API.
4. Добавь фильм: `POST /api/v1/movies` с `{"kinopoisk_id":301}`; используй идентификатор из поиска. Запомни внутренний `id` из ответа: он используется вместо внешнего `kinopoisk_id` в следующих запросах. Повторное добавление возвращает существующий фильм.
5. `PUT /api/v1/movies/{movie_id}/feedback/me`:

   ```json
   {
     "scores": {
       "idea": 9,
       "plot": 8,
       "screenplay": 9,
       "acting": 8,
       "sound": 10,
       "visuals": 9
     },
     "review": "Понравились атмосфера и музыка."
   }
   ```

6. `GET /api/v1/movies/{movie_id}` вернёт карточку, внешние и клубные оценки. `GET /api/v1/movies` — каталог клуба, `GET /api/v1/movies/{movie_id}/reviews` — отзывы. Для списков используй `page` и `page_size`: по умолчанию 1 и 20, максимум 100 записей на страницу.

Каталог и отзывы доступны после входа. В списке отзывов нет имени или ID автора; владелец базы всё равно видит связь записи с участником. Своя запись доступна по `/api/v1/movies/{movie_id}/feedback/me`: `GET` — просмотр, `PUT` — замена, `DELETE` — удаление. Чужую запись изменить нельзя.

Один участник имеет одну запись на фильм. `PUT` полностью заменяет её и не добавляет голос. Оба поля обязательны: `scores: null` оставляет только текст, `review: null` — только оценки. Хотя бы одна часть должна быть заполнена.

Оценки: идея, сюжет, сценарий, игра актёров, звук и визуал. Все шесть — целые числа 1–10. Общую `overall` считает сервер: при равных весах сумма делится на 6. Клубная общая — среднее общих оценок участников без промежуточного округления. `ratings_count` считает участников с оценками, `reviews_count` — с текстом; отзыв без оценок не добавляет голос. Без голосов средние равны `null`. Изменение `RATING_WEIGHTS` и перезапуск пересчитывают результаты из исходных оценок.

## Восстановление пароля

Для локальной почты можно запустить [Mailpit](https://mailpit.axllent.org/docs/install/docker/) через Docker:

```powershell
docker run -d --name cinema-mailpit -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 axllent/mailpit
```

Настройки `.env.example` уже подходят: `localhost:1025`, пустые SMTP-учётные данные, `SMTP_TLS=false`. Письма видны в [Mailpit](http://127.0.0.1:8025), в настоящий ящик они не отправляются. Для настоящей почты укажи параметры SMTP-провайдера; `SMTP_TLS=true` включает STARTTLS.

1. Вызови `POST /api/v1/auth/forgot-password` с `{"email":"member@example.org"}`. Ответ `202` нейтральный и не подтверждает наличие аккаунта или доставку.
2. Открой письмо и вызови `POST /api/v1/auth/reset-password` с `{"token":"<код из письма>","new_password":"<новый пароль от 12 до 128 символов>"}`.
3. После `204` войди заново. Старые токены входа больше не работают.

Код восстановления одноразовый, действует 30 минут; новый код заменяет предыдущий. Лимиты: вход — 10 попыток за 10 минут на IP, восстановление — 5 запросов в час на IP, не более одного письма в минуту на email.

## Проверки и CI

```powershell
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -v
```

В `test_app.py` — не менее трёх проверок API, в `tests/` — остальные сценарии. Тесты используют временные SQLite и подменяют кино API и SMTP: реальные ключи, интернет и отправка писем не требуются. Версии зависимостей закреплены в `requirements.lock`.

[CI](https://github.com/mkang131/cinema-club-tracker/actions/workflows/ci.yml) проверяет PR в `main` и повторно запускается при `push` в `main`. Обычный push в ветку без PR его не запускает. Шаги: установка зависимостей разработки, Ruff, миграции новой SQLite и pytest. Ошибка любого шага делает `checks` неуспешным.

После успеха исходники доступны **7 дней**: **Actions → нужный запуск → Artifacts → cinema-club-tracker-source**. Архив содержит код, миграции, тесты и конфигурацию без секретов и локальных данных. После распаковки выполни установку и настройку `.env` выше. Ошибки CI смотри в **Actions → запуск → checks → упавший шаг**.

## Диагностика

Логи идут в терминал сервера: запуск, версия, метод и путь запроса, статус, длительность и `request_id`. По `X-Request-ID` из ответа можно найти запрос. Пароли, токены, тексты отзывов и писем не логируются. `LOG_LEVEL=WARNING` сокращает вывод после перезапуска.

| Проблема | Что сделать |
|---|---|
| Ошибка настроек при запуске | Проверь `JWT_SECRET` от 32 символов и JSON `RATING_WEIGHTS` в `.env` |
| PowerShell блокирует активацию | Используй `.\.venv\Scripts\python.exe` вместо `python` без активации |
| Порт 8000 занят | Останови предыдущий сервер или укажи `--port 8001` |
| `no such table` | Выполни миграции с тем же `DATABASE_URL`; `/health` проверяет соединение, а не таблицы |
| `/health` возвращает `503` | Проверь путь к SQLite, права на файл и диск, затем логи |
| `401` | Войди и обнови токен в Authorize; старый мог истечь или быть отменён |
| `422` | Сверь запрос со схемой Swagger и `error.details`; при регистрации проверь приглашение и email |
| Внешний каталог возвращает `503` | Проверь API-ключ, квоту и доступность источника; сохранённый каталог работает |
| Не приходит письмо | Проверь SMTP/Mailpit, лимиты и `mail_delivery_failed` в логах |
| `429` | Дождись окончания лимита входа или восстановления |

Запускай один процесс Uvicorn: кэш и лимиты хранятся в памяти и сбрасываются при перезапуске. Проект рассчитан на небольшой каталог; локальные фильтры выполняются в Python. Для доступа реальных участников через интернет нужны HTTPS и резервные копии SQLite.

## Файлы проекта

```text
app_final.py                         # Точка входа
requirements.txt, requirements-dev.txt # Зависимости запуска и проверок
README.md, .gitignore, .env.example    # Инструкции и локальные настройки
.github/workflows/ci.yml              # CI и артефакт исходников
test_app.py                           # Базовые тесты API
app/                                 # Реализация сервиса
migrations/, alembic.ini              # Миграции базы
requirements.lock                    # Точные версии зависимостей
tests/, conftest.py, pyproject.toml    # Остальные тесты и настройки проверок
```

`app_final.py` экспортирует фабрику из `app/`; для запуска нужны оба, а также миграции и конфигурация. Полный контракт API и критерии приёмки — в [ТЗ](TECHNICAL_SPECIFICATION.md), правила изменений и PR — в [AGENTS.md](https://github.com/mkang131/cinema-club-tracker/blob/main/AGENTS.md).
