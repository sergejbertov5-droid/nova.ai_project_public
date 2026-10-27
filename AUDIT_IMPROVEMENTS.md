# Аудит NOVA AI v3.0 — Стабильность, Интеллект, UX

*Создано: 2026-10-10*

---

## 1. Обзор архитектуры

NOVA AI v3.0 — это модульный голосовой AI-ассистент с расширенными когнитивными функциями:

```
┌─────────────────────────────────────────────────────────────┐
│                        main.py                              │
│  (SingleInstanceGuard, VPN, Health, TTS, Assistant,        │
│   Reminders, Routine, SelfLearning, VPNMonitor, Telegram)  │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│  AgentRuntime │    │ CommandRouter │    │   AIBrain     │
│  (Planner +   │    │ (ToolRegistry,│    │ (ProviderChain,│
│   Executor)   │    │  Permissions) │    │  HyperGen,    │
│               │    │               │    │   Memory)     │
└───────────────┘    └───────────────┘    └───────────────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              ▼
              ┌───────────────────────────────┐
              │   Core Services Layer         │
              │  Memory | Profile | Routine   │
              │  VPN    | Learning | Context  │
              └───────────────────────────────┘
```

---

## 2. Аудит стабильности (Critical)

### 2.1 Найденные проблемы

| Проблема | Файл | Строка | Риск | Описание |
|----------|------|--------|------|----------|
| **Race condition в MemoryStore** | `core/memory.py` | 127-132 | 🔴 High | `RLock` защищает только внутренние методы, но `_read()`/`_write()` вызываются извне без блокировки |
| **Отсутствие graceful shutdown для VPN** | `core/vpn_service.py` | ~440 | 🟡 Medium | `VPNMonitor.stop()` не восстанавливает оригинальные proxy env vars |
| **Thread leak в DaemonRunner** | `core/daemon_threads.py` | - | 🟡 Medium | `abandon()` оставляет потоки жить вечно; нет hard limit на общее число |
| **Blocking I/O в voice loop** | `main.py` | 762-1060 | 🟡 Medium | `run_voice_loop` делает сетевые вызовы (STT) в главном цикле |
| **No circuit breaker для AI providers** | `core/ai_providers.py` | - | 🟡 Medium | После множественных сбоев провайдер продолжает опрашиваться |
| **Файл состояния AI провайдеров не атомарен при чтении** | `core/ai_providers.py` | 304-335 | 🟢 Low | `_load_failures` может вернуть частично прочитанный dict при конкурентном доступе |

### 2.2 Исправления стабильности (Plan)

1. **MemoryStore**: Добавить `@property` для thread-safe доступа, сделать все публичные методы атомарными
2. **VPN Monitor**: Добавить `restore_original_proxy_env` в `stop()`
3. **DaemonRunner**: Добавить `max_total_threads` и периодическую уборку abandoned
4. **Voice Loop**: Вынести STT в отдельный поток с очередью
5. **Circuit Breaker**: Добавить в `ai_providers.py` автоматическое отключение после N последовательных сбоев
6. **State File**: Использовать file locking (fcntl/portalocker) для `ai_provider_state.json`

---

## 3. Аудит когнитивных функций (Intelligence)

### 3.1 Текущие возможности

| Функция | Реализация | Оценка |
|---------|------------|--------|
| **Multi-provider AI** | Auto-failover цепочка (Groq→Gemini→Cerebras→OpenRouter→NIM→Ollama) | ⭐⭐⭐⭐⭐ |
| **Hypergeneration** | 2-3 кандидата, выбор по `_candidate_value` | ⭐⭐⭐⭐ |
| **Memory 2.0** | Категории, confidence, sources, importance | ⭐⭐⭐⭐⭐ |
| **Planner + Executor** | Структурированные планы с зависимостями, бюджетом, ретраями, репланированием | ⭐⭐⭐⭐⭐ |
| **Self-learning** | Веб-поиск → AI анализ → сохранение в `self_learning.json` | ⭐⭐⭐⭐ |
| **Routine Monitor** | Time-based nudges (overwork, entertainment) | ⭐⭐⭐ |
| **Screen Awareness** | Метаданные активного окна, Chrome tab | ⭐⭐⭐ |
| **User Profile** | Work tools, interests, startup suggestions | ⭐⭐⭐ |

### 3.2 Пробелы в интеллекте

| Пробел | Приоритет | Предлагаемое решение |
|--------|-----------|---------------------|
| **Нет эмоционального контекста** | 🔴 High | Добавить sentiment analysis пользователя, адаптацию персоны |
| **Нет долгосрочного планирования** | 🟡 Medium | Добавить `goal_decomposition` + `task_scheduler` для multi-day целей |
| **Нет cross-session learning** | 🟡 Medium | Персистентный векторный индекс (FAISS/Chroma) для semantic memory |
| **Ограниченная рефлексия** | 🟡 Medium | Добавить `post_task_reflection` — анализ своих ошибок после задачи |
| **Нет theory of mind** | 🟢 Low | Модель намерений пользователя (belief/desire tracking) |
| **Нет proactive suggestions** | 🟢 Low | Предсказательные предложения на основе паттернов |

### 3.3 Улучшения когнитивных функций (Plan)

1. **Emotional Intelligence Module** — анализ тона пользователя, адаптация персоны
2. **Semantic Memory (Vector DB)** — FAISS/Chroma для долгосрочного семантического поиска
3. **Goal Decomposition Engine** — разбивка больших целей на подзадачи с дедлайнами
4. **Reflection Loop** — пост-таск анализ: что пошло не так, как улучшить
5. **Proactive Assistant** — предиктивные предложения на основе usage patterns
6. **Multi-modal Reasoning** — интеграция скриншотов + текст для контекста

---

## 4. Аудит UX / UI / "Красота" (User Experience)

### 4.1 Текущее состояние

| Компонент | Состояние | Проблемы |
|-----------|-----------|----------|
| **Control Panel (NOVA AI Control.app)** | Tkinter-based | Устаревший визуал, нет тем, плохая типографика |
| **Hologram (NOVA AI Hologram.app)** | OpenGL/GLFW | Красиво, но изолировано от основного UI |
| **Logging/Monitoring** | JSONL logs + console | Нет визуального дашборда, нет real-time метрик |
| **Startup Experience** | Текстовый greeting + readiness table | Нет onboarding, нет прогресс-бара инициализации |
| **Error Handling** | Говорит об ошибках голосом | Нет graceful degradation UI, нет actionable suggestions |

### 4.2 Улучшения UX/UI (Plan)

1. **Modern Control Panel** — переписать на **CustomTkinter** или **PySide6** с:
   - System theme (light/dark/auto)
   - Real-time metrics dashboard (CPU, RAM, AI latency, task queue)
   - Visual task timeline (Gantt-style)
   - Settings с live validation
   - Log viewer с фильтрами и поиском

2. **Hologram Integration** — объединить с панелью:
   - Hologram как overlay на панели
   - Voice visualization (waveform/spectrum)
   - Status indicators (listening, thinking, speaking)

3. **Onboarding Flow** — первый запуск:
   - Welcome wizard (API keys, permissions, persona)
   - Microphone calibration
   - Test commands

4. **Rich Notifications** — macOS native notifications + in-app toast system

5. **Accessibility** — VoiceOver support, keyboard navigation, high contrast mode

---

## 5. План реализации (Phased)

### Phase 1: Stability Foundation (Week 1-2)
- [ ] Fix MemoryStore race conditions
- [ ] Add VPN graceful shutdown
- [ ] Implement Circuit Breaker for AI providers
- [ ] Add file locking for state files
- [ ] Move STT to background thread with queue

### Phase 2: Cognitive Enhancement (Week 2-3)
- [ ] Add Emotional Intelligence module
- [ ] Integrate FAISS/Chroma for semantic memory
- [ ] Implement Goal Decomposition Engine
- [ ] Add Reflection Loop (post-task analysis)
- [ ] Enhance SelfLearning with vector search

### Phase 3: UX/UI Modernization (Week 3-4)
- [ ] Rewrite Control Panel on CustomTkinter/PySide6
- [ ] Integrate Hologram as overlay
- [ ] Add real-time metrics dashboard
- [ ] Implement onboarding wizard
- [ ] Add rich notification system

### Phase 4: Polish & Integration (Week 4-5)
- [ ] Cross-component integration testing
- [ ] Performance profiling & optimization
- [ ] Documentation update
- [ ] Release preparation

---

## 6. Метрики успеха

| Метрика | Текущее | Цель |
|---------|---------|------|
| **Uptime** | ~95% | 99.9% |
| **AI Response Latency (p95)** | ~3-5s | <2s |
| **Memory Search Latency** | ~50ms | <10ms |
| **Task Success Rate** | ~85% | >95% |
| **User Satisfaction (implicit)** | Unknown | >90% positive interactions |
| **Startup Time** | ~15s | <5s |
| **Zero Data Loss Incidents** | Occasional | 0 |

---

## 7. Технический долг (Technical Debt)

| Область | Описание | Effort |
|---------|----------|--------|
| **Type Hints** | Частичное покрытие, добавить полную типизацию | Medium |
| **Test Coverage** | Только planner tests, нужны integration tests | High |
| **Documentation** | Docstrings есть, но нет архитектурной docs | Medium |
| **Config Validation** | Нет схемы валидации .env (pydantic-settings) | Low |
| **Plugin System** | Нет механизма плагинов для инструментов | High |

---

## 8. Риски и митигация

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| **Breaking changes в AI API** | High | High | Абстракция `ai_providers`, версионирование промптов |
| **macOS permission changes** | Medium | High | Fallback к текстовым режимам, graceful degradation |
| **Voice recognition degradation** | Medium | Medium | Несколько STT engines, confidence thresholds |
| **Memory bloat** | Low | Medium | MAX_FACTS limit, periodic cleanup, vector DB pagination |
| **Dependency conflicts** | Medium | Medium | Locked requirements, venv isolation, vendoring critical deps |

---

## 9. Приоритет следующих шагов

1. **СРОЧНО**: MemoryStore thread safety + VPN shutdown fix
2. **ВАЖНО**: Circuit breaker + STT background thread
3. **СТРАТЕГИЧЕСКИ**: Semantic memory + Emotional intelligence
4. **UX**: Modern Control Panel + Hologram integration
5. **НАКОПИТЕЛЬНО**: Tests, docs, plugin system

---

*Документ будет обновляться по мере выполнения фаз.*