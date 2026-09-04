# Clippy Assistant

Персональный Telegram-ассистент на Python с интеграцией OpenAI, Google Calendar и Google Tasks.

Это очищенная публичная версия реально работающего production-проекта.
Credentials, OAuth-токены, пользовательские данные, рабочие базы, логи и приватный system prompt в репозиторий не входят.

## Возможности

- Telegram-интерфейс;
- OpenAI API и tool calling;
- планирование дня и недели;
- чтение и изменение Google Calendar;
- работа с Google Tasks;
- проекты и next actions;
- локальная память на SQLite;
- отдельная база знаний;
- распознавание голосовых сообщений;
- генерация голосовых ответов;
- погода через Open-Meteo;
- HTTP gateway для внешних интеграций;
- подтверждение чувствительных действий;
- очереди и аудит сообщений.

## Архитектура

Telegram → bot.py → ai_agent.py

AI-агент взаимодействует с:

- OpenAI API;
- Google Calendar;
- Google Tasks;
- SQLite memory;
- project planning;
- creative knowledge;
- voice tools;
- Clippy Gateway.

## Стек

- Python 3.12+
- aiogram 3
- OpenAI API
- Google Calendar API
- Google Tasks API
- Google OAuth
- SQLite
- aiohttp
- Linux / systemd

## Основные модули

- `bot.py` — Telegram-интерфейс и фоновые процессы.
- `ai_agent.py` — AI-агент, tools и маршрутизация.
- `calendar_tools.py` — Google Calendar.
- `google_tasks_tools.py` — Google Tasks.
- `memory_store.py` — долговременная память.
- `creative_knowledge.py` — локальная база знаний.
- `project_next_actions.py` — проекты и next actions.
- `voice_tools.py` — speech-to-text и text-to-speech.
- `clippy_gateway.py` — HTTP gateway.
- `bot_tools.py` — интеграция с отдельным клиентским Telegram-ботом.

## Безопасность публичной версии

В репозитории отсутствуют:

- Telegram tokens;
- OpenAI API keys;
- Google credentials и OAuth tokens;
- реальные calendar IDs;
- production IP и абсолютные server paths;
- пользовательские SQLite databases;
- сообщения клиентов;
- runtime state;
- приватный production system prompt.

Runtime-файлы хранятся в `data/` и исключены через `.gitignore`.

## Установка

1. Создать Python 3.12 virtual environment.
2. Установить `requirements.txt`.
3. Скопировать `.env.example` в собственную конфигурацию.
4. Указать свои API credentials и IDs.
5. Запустить `python bot.py`.

Код не загружает `.env` автоматически: переменные окружения должны быть экспортированы shell или переданы process manager.

## Public vs production

Production-версия работает под systemd с отдельными Unix-пользователями, закрытыми credentials, runtime databases и reverse proxy.

Эта версия специально обезличена для демонстрации архитектуры, интеграций и подхода к разработке.

## Разработка

Проект создавался как собственная рабочая система автоматизации.

AI-инструменты использовались для помощи при написании, рефакторинге и ревью кода.
Архитектура, постановка задач, тестирование, диагностика и внедрение production-изменений контролировались владельцем проекта.
