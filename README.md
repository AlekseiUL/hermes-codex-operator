<p align="center">
  <img src="docs/assets/hermes-codex-operator-hero.png" alt="An idea flows through Hermes, a structured work contract, Codex, verification, and back to the user" width="100%">
</p>

<h1 align="center">Hermes Codex Operator</h1>

<p align="center">
  <a href="https://github.com/AlekseiUL/hermes-codex-operator/actions/workflows/ci.yml"><img src="https://github.com/AlekseiUL/hermes-codex-operator/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

<p align="center"><strong>Talk to Hermes. Ship with Codex.</strong></p>

<p align="center">
  A small, local-first bridge between Nous Research's Hermes Agent and OpenAI
  Codex CLI/App Server. It turns plain-language ideas into tracked, verified
  Codex work—planned by Hermes, executed by Codex, and explained back to you.
</p>

<p align="center">
  <a href="#english">English</a> · <a href="#русский">Русский</a>
</p>

<a id="english"></a>

## English

## The idea

You should not need to be a programmer—or an expert prompt writer—to get high
quality work from Codex.

Tell Hermes what you want in ordinary language. Hermes acts as the technical
partner: it clarifies only what matters, proposes architecture and trade-offs,
decomposes the work, and creates a precise engineering brief. Hermes Codex
Operator then owns the fragile middle of the journey: dispatching one exact
Codex turn, keeping its identity, following it to completion, reading the
authoritative result, checking the workspace, and returning evidence.

Codex does the implementation. Hermes stays in the conversation. You get the
result—and a clear explanation of what happened and what to do next.

```mermaid
flowchart LR
    U["You<br/>Telegram · CLI · chat"] --> H["Hermes<br/>technical partner"]
    X["Research · specs · team artifacts<br/>selected by you"] -.->|optional context| H
    H --> C["WorkContract v1<br/>scope · checks · identity"]
    C --> O["Hermes Codex Operator<br/>dispatch · follow · recover"]
    O --> A["Codex App Server<br/>thread · turn · events"]
    A --> W["Project workspace<br/>implementation"]
    W --> V["Scope + verification<br/>evidence, not promises"]
    V --> R["Result + receipt<br/>exact turn, exact checks"]
    R --> H
    H --> U
```

## Why this is useful

- **A technical partner before coding.** The bundled Hermes skill turns a rough
  idea into architecture, options, a decomposition, acceptance criteria, and a
  Codex-ready task.
- **One task stays one task.** Stable task, operation, thread, and turn IDs keep
  follow-ups in the right Codex context.
- **No silent double-submit.** If dispatch becomes ambiguous, the operation is
  marked `UNKNOWN`; the prompt is not sent again automatically.
- **Completion is checked.** Codex's final message is read from the exact turn,
  then declared commands and changed-file scope are verified independently.
- **Restarts do not erase ownership.** A private SQLite journal plus the exact
  retained WorkContract support status, recovery, collection, and continuation.
- **The answer comes back in human language.** Hermes can explain the result,
  blockers, evidence, and best next step through the channel you already use.

The project is deliberately two small pieces, not another giant orchestrator:

1. `hermes-codex`, a dependency-free Python lifecycle CLI for Codex App Server;
2. `skills/hermes-codex-operator`, a lightweight Hermes skill that teaches the
   agent how to plan, dispatch, monitor, verify, and report.

The wheel intentionally contains the core CLI only. The repository and source
distribution contain both pieces; until a dedicated skill installer exists,
use a source checkout for the complete Hermes integration shown below.

## A normal conversation

> **You:** I have a product idea, but I do not know how to structure the code
> or write a good Codex prompt.
>
> **Hermes:** I will turn the idea into a small architecture, point out the
> important choices, split the work into a verifiable first milestone, and
> send that milestone to Codex.
>
> **Later, in Telegram:** The Codex task is complete. It changed two files, all
> declared checks passed, and the result was read back from the exact task. Here
> is what was built, what it means in plain language, and the best next step.

When Hermes owns the Telegram conversation, no Telegram code is needed in this
repository: the foreground operator returns a structured result and Hermes
sends the final message through its existing channel. Completion events for an exact manually started turn are stored durably in a
transport-neutral local outbox. Delivery remains the host application's job.

## What is available today

| Capability | v0.1.0 status |
|---|---|
| Clarify an idea; propose architecture, options, decomposition, and acceptance criteria | Included in the Hermes skill |
| Start a new Codex task or continue the same task context | Implemented in the core |
| Wait for the exact turn, read it authoritatively, and return the result | Implemented in the core |
| Verify changed-file scope and argv-only checks | Implemented in the core |
| Durable status, conservative recovery, immutable receipts | Implemented in the core |
| Explain the result and send it through Telegram or another Hermes channel | Provided by the host Hermes agent |
| Add selected research or team artifacts to a brief | Supported as a Hermes workflow; automatic cross-agent crawling is not included |
| Catalog threads and watch an exact manually started turn | Implemented read-only with durable completion events |
| Create visible Codex Desktop projects and tasks | Roadmap: optional, version-gated desktop adapter |
| Background daemon, Kanban, general multi-agent routing, or multi-harness execution | Intentionally not included |

This table is a product contract: the README does not advertise roadmap items
as finished features.

## Quick start

Requirements:

- Python 3.11 through 3.14;
- an installed and authenticated Codex CLI with App Server support;
- Hermes Agent only if you want the conversational agent workflow.

Clone the repository and install the core CLI:

```bash
git clone https://github.com/AlekseiUL/hermes-codex-operator.git
cd hermes-codex-operator
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
hermes-codex doctor --json
```

`doctor` is a shallow local preflight: it checks the Python runtime and that
the `codex` executable reports a version. It does not prove authentication,
configuration validity, or a successful App Server turn; use the explicit live
smoke in [COMPATIBILITY.md](docs/COMPATIBILITY.md) for that.

Create a `WorkContract` with an absolute workspace path. The included
[synthetic example](examples/work-contract.json) is safe to copy and edit:

```json
{
  "schema_version": "1",
  "task_id": "widget-health",
  "operation_id": "widget-health-001",
  "workspace": "/absolute/path/to/widget",
  "prompt": "Add a health endpoint, cover it with tests, and report the changed files.",
  "allowed_change_globs": ["src/**", "tests/**"],
  "verification": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
  "timeout_seconds": 900
}
```

The contract is private operational input: it contains the raw prompt and an
absolute workspace path. Keep the exact file outside Git and outside the target
workspace, set mode `0600` on POSIX, and retain it beside the private journal
until completion. Losing or editing it prevents safe recovery by design because
the journal stores only its digest, not the raw prompt.

Submit it once from an allowlisted parent directory:

```bash
hermes-codex submit \
  --contract work-contract.json \
  --allow-root /absolute/path/to/projects \
  --json
```

Inspect, reconcile, or collect the same operation without creating another
turn:

```bash
hermes-codex status widget-health-001 --json
hermes-codex recover --contract work-contract.json --allow-root /absolute/path/to/projects --json
hermes-codex collect --contract work-contract.json --allow-root /absolute/path/to/projects --json
```

Read-only catalog and exact-ID watch commands never resume or start Codex work:

```bash
hermes-codex catalog --json
hermes-codex catalog read THREAD_ID --json
hermes-codex catalog read THREAD_ID --include-result --json
hermes-codex adopt THREAD_ID TURN_ID --json
hermes-codex watch WATCH_ID --json
hermes-codex events list --json
hermes-codex events ack EVENT_ID --json
```

`catalog` is bounded to 20 pages and 1,000 threads. `catalog read` performs an
exact read-only `thread/read` and exposes turn IDs plus result digests; raw
result text requires explicit `--include-result`. `adopt` requires both exact
IDs and verifies them through `thread/read`. Watches survive process restart;
one terminal watch creates exactly one event containing the result digest, not
the raw result. Missing, duplicate, or conflicting identity becomes `UNKNOWN`
without dispatch. Event acknowledgement is idempotent. There is no daemon or
delivery transport in the package, so callers poll `watch` and consume the
outbox themselves.

For a follow-up, keep `task_id` and use a new `operation_id`. That resumes the
same Codex thread. Never reuse an operation ID with different instructions.

For lifecycle commands, JSON `ok` means the operation is `VERIFIED`. `status`
still exits `0` when it successfully reads a non-verified record, so automation
must inspect both `status` and `ok`. Invocation/configuration errors exit `2`;
a lifecycle command that finishes without verification exits `3`.

## Add the Hermes skill

Hermes discovers skills from `$HERMES_HOME/skills/` (normally
`~/.hermes/skills/`; profiles use their own Hermes home). From the checkout:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills"
cp -R skills/hermes-codex-operator "$HERMES_HOME/skills/"
hermes skills list
```

Start a new Hermes session after installing the skill, then speak normally or
invoke `/hermes-codex-operator`. The skill stays compact; the detailed role and
configuration recipes live in the
[agent playbook](docs/AGENT_PLAYBOOK.md).

## Reliability model

One `operation_id` represents at most one logical Codex turn.

```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> STARTING_THREAD
    STARTING_THREAD --> READY
    READY --> DISPATCHING
    DISPATCHING --> RUNNING
    RUNNING --> TURN_COMPLETED
    TURN_COMPLETED --> VERIFYING
    VERIFYING --> VERIFIED
    VERIFYING --> VERIFICATION_FAILED
    VERIFYING --> SCOPE_VIOLATION
    DISPATCHING --> UNKNOWN: acceptance cannot be proven
    RUNNING --> TIMED_OUT: exact turn remains bound
    UNKNOWN --> RUNNING: recover exact bound turn
    UNKNOWN --> TURN_COMPLETED: recover exact terminal turn
    TIMED_OUT --> RUNNING: recover still-running turn
    TIMED_OUT --> TURN_COMPLETED: recover exact terminal turn
    UNKNOWN --> FAILED: explicit abandon if identity is missing
    UNKNOWN --> FAILED: recover exact failed turn
    TIMED_OUT --> FAILED: recover exact failed turn
    TIMED_OUT --> CANCELLED: recover interrupted turn
    RUNNING --> FAILED
    RUNNING --> CANCELLED
    VERIFIED --> [*]
    VERIFICATION_FAILED --> [*]
    SCOPE_VIOLATION --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

`TURN_COMPLETED` is not the same as `VERIFIED`. A verified receipt binds the
prompt digest, workspace digest, exact Codex thread and turn, authoritative
result digest, changed paths, and verification evidence.

The operator is evidence enforcement, not an operating-system sandbox. Codex's
sandbox remains its execution boundary. Declared verification argv is trusted
caller-selected code and runs as the operator's OS user, outside the Codex
sandbox; `shell=false`, time/output bounds, and post-hoc workspace scope checks
do not prevent that command from writing elsewhere. Use only reviewable,
project-local checks you would run yourself.

## Privacy by construction

- no telemetry;
- no raw prompt or model response in the journal or receipt by default;
- no private Hermes profile, Telegram token, chat ID, team name, or local
  project copied into the repository;
- synthetic fixtures only;
- repository tree, staged index, Git history, and release archive privacy
  gates;
- deterministic sdist normalization removes local account names from tar owner
  metadata before publication;
- local runtime state is ignored and stored with private permissions.

Read [SECURITY.md](SECURITY.md), the
[threat model](docs/THREAT_MODEL.md), and the
[privacy release procedure](docs/PRIVACY_RELEASE.md) before deployment or
publication.

## Project status

`v0.1.0` is a pre-alpha clean-room implementation. Its protocol and failure
behavior have a comprehensive scripted suite, while live compatibility is
qualified separately for each Codex CLI, Hermes, Python, and OS combination.
Unqualified combinations are targets, not support claims. See
[COMPATIBILITY.md](docs/COMPATIBILITY.md) and [ROADMAP.md](docs/ROADMAP.md).

The integration uses the official
[Codex App Server protocol](https://developers.openai.com/codex/app-server),
which provides threads, turns, streamed lifecycle events, and authoritative
thread reads. Hermes skills follow the current
[Hermes Agent skills system](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md).

## Development

```bash
PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning \
  python3 -m unittest discover -s tests
python3 scripts/privacy_gate.py . --mode tree
```

Architecture and protocol details are in [RFC-0001](docs/RFC-0001.md).
Contributor setup is in [CONTRIBUTING.md](CONTRIBUTING.md). Independent review
findings are recorded in [REVIEWS](docs/REVIEWS.md).

## Creator and useful links

- [Aleksei Ulianov on GitHub](https://github.com/AlekseiUL)
- [Aleksei Ulianov on YouTube](https://youtube.com/@alekseiulianov)
- [Sprut AI Telegram channel](https://t.me/Sprut_AI)
- [Sprut AI Telegram community chat](https://t.me/+eH-qNIDmud8zNDZi)
- [AI Операционка](https://t.me/tribute/app?startapp=sJyg)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [OpenAI Codex](https://github.com/openai/codex)

## License and trademarks

MIT. This is an independent community project. It is not affiliated
with or endorsed by OpenAI or Nous Research. Hermes Agent and Codex are names
of their respective projects and products.

---

<a id="русский"></a>

## Русский

### Что это

Hermes Codex Operator - локальная прослойка между
[Hermes Agent](https://github.com/NousResearch/hermes-agent) и Codex CLI / App
Server. Вы формулируете задачу обычным языком. Hermes помогает уточнить цель,
границы и критерии приёмки. Codex пишет код. Operator сохраняет точную связь с
потоком и запуском Codex, ждёт завершения, перечитывает итог и проверяет
рабочую папку.

Проблема, которую решает проект, простая: сообщения Codex о завершении мало.
Нужно знать, какой именно запуск выполнился, что он изменил, какие проверки
реально прошли и не отправилась ли одна задача дважды.

### Что уже работает в v0.1.0

- новый запуск Codex и продолжение того же контекста;
- постоянные идентификаторы задачи, операции, потока и запуска;
- защита от автоматической повторной отправки при неоднозначном результате;
- точное чтение финального ответа через `thread/read`;
- проверка изменённых файлов по разрешённым шаблонам;
- команды проверки в виде массива аргументов, без запуска через shell;
- приватный SQLite-журнал для статуса и восстановления;
- неизменяемый receipt с хешами и результатами проверок;
- отдельный Hermes skill для подготовки, запуска, контроля и объяснения
  результата.
- ограниченный каталог потоков, read-only подключение по точным `threadId` и
  `turnId`, постоянные watches и локальный outbox событий завершения;

Operator не является фоновым диспетчером, полноценной песочницей или заменой
человеческой приёмки. Он может наблюдать только точный уже известный поток и
запуск через App Server; Desktop Projects и управление интерфейсом не входят в
пакет.

### Быстрый старт

Требования:

- Python 3.11-3.14;
- установленный и авторизованный Codex CLI с App Server;
- Hermes Agent, если нужен разговорный сценарий.

```bash
git clone https://github.com/AlekseiUL/hermes-codex-operator.git
cd hermes-codex-operator
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
hermes-codex doctor --json
```

Создайте приватный `WorkContract`. Синтетический пример лежит в
[`examples/work-contract.json`](examples/work-contract.json). В контракте
задаются рабочая папка, промпт, разрешённые файлы, команды проверки и тайм-аут.

```bash
hermes-codex submit \
  --contract work-contract.json \
  --allow-root /absolute/path/to/projects \
  --json
```

Для той же операции используйте `status`, `recover` или `collect`. Не создавайте
новый запуск, если состояние стало `UNKNOWN` или `TIMED_OUT`: Operator специально
сохраняет неоднозначность и требует сверить точный поток и запуск.

### Подключение skill к Hermes

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills"
cp -R skills/hermes-codex-operator "$HERMES_HOME/skills/"
hermes skills list
```

После установки начните новую сессию Hermes и говорите с агентом обычным
языком или вызовите `/hermes-codex-operator`.

### Модель надёжности

Один `operation_id` соответствует максимум одному логическому запуску Codex.
Если Operator не может доказать, что запрос был принят или не принят, операция
получает статус `UNKNOWN`. Повторная отправка автоматически не выполняется.

`TURN_COMPLETED` ещё не означает `VERIFIED`. Для подтверждённого результата
Operator связывает хеш промпта, рабочую папку, точный поток и запуск Codex,
авторитетно перечитанный ответ, список изменений и результаты проверок.

Команды проверки выбирает пользователь. Они запускаются от имени текущего
пользователя операционной системы и могут иметь побочные эффекты. Используйте
только понятные проектные команды, которые вы готовы запустить вручную.

### Приватность

- телеметрии нет;
- исходный промпт и ответ модели по умолчанию не попадают в журнал и receipt;
- тесты используют только синтетические данные и временные папки;
- privacy gate проверяет дерево, Git index, достижимую историю, sdist и wheel;
- метаданные sdist нормализуются перед публикацией, чтобы локальное имя
  пользователя не попало в архив.

Подробности: [политика безопасности](SECURITY.md),
[модель угроз](docs/THREAT_MODEL.md),
[процедура публикации](docs/PRIVACY_RELEASE.md) и
[совместимость](docs/COMPATIBILITY.md).

### Автор и ресурсы

- [Алексей Ульянов на GitHub](https://github.com/AlekseiUL)
- [Алексей Ульянов на YouTube](https://youtube.com/@alekseiulianov)
- [Telegram-канал Sprut AI](https://t.me/Sprut_AI)
- [Telegram-чат сообщества](https://t.me/+eH-qNIDmud8zNDZi)
- [AI Операционка](https://t.me/tribute/app?startapp=sJyg)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [OpenAI Codex](https://github.com/openai/codex)

Проект распространяется по лицензии MIT. Это независимый проект
сообщества, не связанный официально с OpenAI или Nous Research и не одобренный
ими.
