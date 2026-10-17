"""
Синхронизация лотов автовыдачи (StockSync) — плагин для FunPay Cardinal v0.1.17+

🔄 Держит поле «Наличие» лотов на FunPay в соответствии с реальным количеством
товаров в товарных файлах автовыдачи (storage/products/*.txt).

Зачем: «Наличие» на FunPay и товарный файл живут отдельной жизнью — после ручного
пополнения товара, частичных возвратов или сбоев цифры расходятся. Покупатели
видят несуществующие остатки (или «0», хотя товар есть). Плагин синхронизирует их.

Возможности:
 • Автоматическая синхронизация лота сразу ПОСЛЕ каждой автовыдачи
   (событие POST_DELIVERY) — товар выдан, «Наличие» тут же уменьшено.
 • Периодическая полная синхронизация всех лотов автовыдачи (по умолчанию
   каждые 10 минут) — ловит ручные пополнения файлов и любые расхождения.
 • Ручная кнопка «🔄 Синхронизировать сейчас» с подробным отчетом в чат.
 • Уведомления только о реальных изменениях («Лот: 100 → 7») — без спама.
 • 🩺 Диагностика: какие секции конфига автовыдачи НЕ соответствуют ни одному
   активному лоту (лот переименовали — автовыдача молча не сработает!).
 • Безопасность: меняет ТОЛЬКО поле «Наличие» (цену/название/описание не трогает);
   лоты со встроенной автовыдачей FunPay и лоты с нулем товаров пропускаются
   (нулем занимается автовыключение Кардинала).

Команды Telegram:
 • /sync — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from Utils import cardinal_tools

logger = logging.getLogger("FPC.StockSync")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "Синхронизация лотов автовыдачи"
VERSION = "1.0.0"
DESCRIPTION = ("Держит «Наличие» лотов на FunPay в соответствии с товарными файлами автовыдачи: "
               "синхронизация после каждой выдачи + периодическая + ручная. Диагностика конфигов, "
               "которые не подходят ни одному лоту. Команда: /sync")
CREDITS = "Qwen (для FunPay Cardinal)"
UUID = "8090130c-fc77-48d2-8a0d-dd6db599e54a"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "stock_sync")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "enabled": True,           # периодическая синхронизация вкл/выкл
    "sync_on_delivery": True,  # синхронизировать лот сразу после автовыдачи
    "interval": 600,           # интервал полной синхронизации, сек (10 мин)
    "last_sync": {},           # {"ts": float, "checked": int, "changed": int}
}
INTERVAL_RANGE = (120, 7200)
PER_PAGE = 10
SAVE_DELAY = 0.8   # пауза между сохранениями лотов (анти-флуд)
LOT_DELAY = 0.4    # пауза между лотами

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "ssy:home"
CB_NOW = "ssy:now"
CB_TOG = "ssy:tog"
CB_DELTOG = "ssy:deltog"
CB_INT = "ssy:int"
CB_DIAG = "ssy:diag"      # ssy:diag:{offset}
CB_NOOP = "ssy:noop"
ST_INT = "ssy:st:int"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_sync_lock = threading.Lock()
_busy = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("StockSync: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        settings["enabled"] = bool(settings["enabled"])
        settings["sync_on_delivery"] = bool(settings["sync_on_delivery"])
        try:
            settings["interval"] = _clamp(int(settings["interval"]), *INTERVAL_RANGE)
        except (TypeError, ValueError):
            settings["interval"] = DEFAULT_SETTINGS["interval"]
        if not isinstance(settings["last_sync"], dict):
            settings["last_sync"] = {}
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("StockSync: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _notify(crd: "Cardinal", text: str) -> None:
    if not crd.telegram:
        return
    kwargs = {"notification_type": tg_utils.NotificationTypes.lots_restore} if tg_utils else {}
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════════ СИНХРОНИЗАЦИЯ ═══════════════════════════════════
def _match_section(crd: "Cardinal", lot) -> tuple | None:
    """
    Ищет секцию конфига автовыдачи для лота (как это делает Кардинал):
    самое длинное название-секция, являющееся подстрокой описания лота.
    """
    try:
        desc = ", ".join([i for i in [getattr(lot, "server", None), getattr(lot, "side", None),
                                      getattr(lot, "description", None)] if i])
    except Exception:
        return None
    if not desc:
        return None
    best = None
    try:
        names = crd.AD_CFG.sections()
    except Exception:
        return None
    for name in names:
        if name and name in desc:
            if best is None or len(name) > len(best):
                best = name
    if best is None:
        return None
    return best, crd.AD_CFG[best]


def _lots_to_sync(crd: "Cardinal") -> list:
    """[(LotShortcut, имя секции, имя файла товаров), ...] — активные лоты автовыдачи."""
    profile = getattr(crd, "profile", None)
    if profile is None:
        return []
    result = []
    try:
        lots = list(profile.get_sorted_lots(1).values())
    except Exception:
        logger.debug("StockSync: профиль недоступен.")
        return []
    for lot in lots:
        m = _match_section(crd, lot)
        if m is None:
            continue
        try:
            fname = m[1].get("productsFileName")
        except Exception:
            continue
        if not fname:
            continue
        result.append((lot, m[0], fname))
    return result


def sync_lot(crd: "Cardinal", lot_id, fname: str) -> tuple:
    """
    Синхронизирует «Наличие» одного лота с количеством товаров в файле.

    :return: (статус, old, new). Статусы: "changed", "ok", "skip_zero", "skip_builtin",
             "skip_nofield", "skip_noamount", "error".
    """
    path = os.path.join("storage", "products", fname)
    try:
        count = cardinal_tools.count_products(path)
    except Exception:
        logger.error(f"StockSync: не удалось посчитать товары в {fname}.")
        logger.debug("TRACEBACK", exc_info=True)
        return "error", None, None
    if count <= 0:
        return "skip_zero", None, 0  # нулем занимается автовыключение Кардинала
    try:
        fields = crd.account.get_lot_fields(int(lot_id))
        if getattr(fields, "auto_delivery", False):
            return "skip_builtin", None, None  # встроенная автовыдача FunPay — наличие из secrets
        if getattr(fields, "_amount", None) is None:
            return "skip_nofield", None, None  # у лота нет поля «Наличие»
        old = int(fields.amount)
        if old == count:
            return "ok", old, count
        fields.amount = count
        crd.account.save_lot(fields)
        logger.info(f"StockSync: лот {lot_id} — наличие {old} → {count}.")
        return "changed", old, count
    except Exception:
        logger.error(f"StockSync: не удалось синхронизировать лот {lot_id}.")
        logger.debug("TRACEBACK", exc_info=True)
        return "error", None, None


def _format_changes(changes: list) -> str:
    return "\n".join(f"• <code>{_escape(_short(desc, 60))}</code>: {old} → <b>{new}</b>"
                     for desc, old, new in changes[:15])


def full_sync(crd: "Cardinal", report_to: int | None = None) -> dict:
    """
    Полная синхронизация всех лотов автовыдачи.
    :param report_to: если указан chat_id — подробный отчет придет в этот чат (ручной запуск).
    """
    if not _busy.acquire(blocking=False):
        if report_to and crd.telegram:
            try:
                crd.telegram.bot.send_message(report_to, "⏳ Синхронизация уже идет. Дождитесь отчета.")
            except Exception:
                pass
        return {}
    stats = {"checked": 0, "changed": 0, "ok": 0, "skipped": 0, "errors": 0}
    changes = []
    try:
        with _sync_lock:
            for lot, section, fname in _lots_to_sync(crd):
                if _stop_event.is_set():
                    break
                stats["checked"] += 1
                status, old, new = sync_lot(crd, lot.id, fname)
                if status == "changed":
                    stats["changed"] += 1
                    desc = ", ".join([i for i in [getattr(lot, "server", None),
                                                  getattr(lot, "side", None),
                                                  getattr(lot, "description", None)] if i])
                    changes.append((desc or f"лот {lot.id}", old, new))
                elif status == "ok":
                    stats["ok"] += 1
                elif status == "error":
                    stats["errors"] += 1
                else:
                    stats["skipped"] += 1
                time.sleep(LOT_DELAY if status != "changed" else SAVE_DELAY)

        settings = _load_settings()
        settings["last_sync"] = {"ts": time.time(), "checked": stats["checked"],
                                 "changed": stats["changed"]}
        _save_settings(settings)

        if changes:
            text = (f"🔄 <b>StockSync: «Наличие» синхронизировано.</b>\n\n"
                    f"Обновлено лотов: <b>{stats['changed']}</b>\n"
                    f"{_format_changes(changes)}")
            _notify(crd, text)
            logger.info(f"StockSync: синхронизировано {stats['changed']} лотов.")

        if report_to and crd.telegram:
            lines = [f"🔄 <b>Синхронизация наличия завершена.</b>\n",
                     f"Проверено лотов автовыдачи: <b>{stats['checked']}</b>",
                     f"Обновлено: <b>{stats['changed']}</b>",
                     f"Уже синхронно: <b>{stats['ok']}</b>",
                     f"Пропущено (0 товаров / встроенная автовыдача / нет поля): <b>{stats['skipped']}</b>",
                     f"Ошибок: <b>{stats['errors']}</b>"]
            if changes:
                lines.append("\n<b>Изменения:</b>\n" + _format_changes(changes))
            else:
                lines.append("\n✅ Все «Наличие» соответствуют товарным файлам.")
            try:
                crd.telegram.bot.send_message(report_to, "\n".join(lines), parse_mode="HTML")
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        logger.error("StockSync: ошибка полной синхронизации.")
        logger.debug("TRACEBACK", exc_info=True)
    finally:
        _busy.release()
    return stats


def post_delivery_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_POST_DELIVERY: синхронизирует «Наличие» выданного лота сразу после выдачи."""
    try:
        if not _plugin_enabled(c):
            return
        settings = _load_settings()
        if not settings["enabled"] or not settings["sync_on_delivery"]:
            return
        if not getattr(e, "delivered", False):
            return
        lot_id = getattr(e, "lot_id", None)
        section = getattr(e, "config_section_obj", None)
        if lot_id is None or section is None:
            return
        fname = section.get("productsFileName")
        if not fname:
            return
        threading.Thread(target=_delivery_sync_worker, args=(c, lot_id, fname), daemon=True).start()
    except Exception:
        logger.error("StockSync: ошибка в обработчике выдачи.")
        logger.debug("TRACEBACK", exc_info=True)


def _delivery_sync_worker(crd: "Cardinal", lot_id, fname: str) -> None:
    with _sync_lock:
        status, old, new = sync_lot(crd, lot_id, fname)
    if status == "changed":
        settings = _load_settings()
        settings["last_sync"] = {"ts": time.time(), "checked": 1, "changed": 1}
        _save_settings(settings)


def _sync_loop(crd: "Cardinal") -> None:
    logger.info("StockSync: периодическая синхронизация запущена.")
    last = 0.0
    while not _stop_event.wait(10):
        try:
            if not _plugin_enabled(crd):
                last = time.time()
                continue
            if not crd.account.is_initiated or getattr(crd, "profile", None) is None:
                continue
            settings = _load_settings()
            if not settings["enabled"]:
                last = time.time()
                continue
            if time.time() - last < settings["interval"]:
                continue
            last = time.time()
            full_sync(crd)
        except Exception:
            logger.error("StockSync: ошибка в цикле синхронизации.")
            logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════════ ДИАГНОСТИКА ═════════════════════════════════════
def _diagnostics(crd: "Cardinal") -> list:
    """
    [(имя секции, кол-во подходящих активных лотов, есть ли товарный файл), ...]
    """
    profile = getattr(crd, "profile", None)
    matched: dict = {}
    if profile is not None:
        try:
            for lot in profile.get_sorted_lots(1).values():
                m = _match_section(crd, lot)
                if m:
                    matched[m[0]] = matched.get(m[0], 0) + 1
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
    result = []
    try:
        for name in crd.AD_CFG.sections():
            try:
                has_file = bool(crd.AD_CFG[name].get("productsFileName"))
            except Exception:
                has_file = False
            result.append((name, matched.get(name, 0), has_file))
    except Exception:
        pass
    result.sort(key=lambda x: (x[1] != 0, x[0]))  # проблемные сверху
    return result


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    ls = settings["last_sync"]
    if ls.get("ts"):
        last_str = (datetime.fromtimestamp(ls["ts"]).strftime("%d.%m.%Y %H:%M")
                    + f" (проверено: {ls.get('checked', '?')}, обновлено: {ls.get('changed', '?')})")
    else:
        last_str = "еще не выполнялась"
    try:
        ad_lots = len(_lots_to_sync(crd))
    except Exception:
        ad_lots = "?"
    return (f"🔄 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Поле «Наличие» лотов на FunPay приводится в соответствие с товарными "
            f"файлами автовыдачи.\n\n"
            f"Периодическая синхронизация: <b>{'ВКЛ 🟢' if settings['enabled'] else 'ВЫКЛ 🔴'}</b>"
            f" (каждые <b>{settings['interval'] // 60}</b> мин.)\n"
            f"После каждой выдачи: <b>{'ВКЛ 🟢' if settings['sync_on_delivery'] else 'ВЫКЛ 🔴'}</b>\n"
            f"Лотов автовыдачи найдено: <b>{ad_lots}</b>\n"
            f"Последняя синхронизация: <b>{_escape(last_str)}</b>")


def _home_kb() -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    kb.add(B("🔄 Синхронизировать сейчас", None, CB_NOW))
    kb.add(B("🩺 Диагностика конфигов", None, f"{CB_DIAG}:0"))
    kb.add(B(f"{'🟢 Периодическая: ВКЛ' if settings['enabled'] else '🔴 Периодическая: ВЫКЛ'}",
             None, CB_TOG))
    kb.add(B(f"{'🟢 После выдачи: ВКЛ' if settings['sync_on_delivery'] else '🔴 После выдачи: ВЫКЛ'}",
             None, CB_DELTOG))
    kb.add(B(f"⏱ Интервал: {settings['interval'] // 60} мин.", None, CB_INT))
    kb.add(B("🔃 Обновить меню", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _diag_kb(crd: "Cardinal", offset: int) -> tuple:
    items = _diagnostics(crd)
    if not items:
        return ("🩺 <b>Диагностика</b>\n\nКонфиг автовыдачи пуст — добавьте лоты через ПУ Кардинала.",
                K().add(B("🏠 Главное меню", None, CB_HOME)))
    problems = sum(1 for _, cnt, _f in items if cnt == 0)
    text = (f"🩺 <b>Диагностика конфигов автовыдачи</b>\n\n"
            f"Секций: {len(items)} | ⚠️ Не подходят ни одному активному лоту: <b>{problems}</b>\n\n"
            f"<i>Секция без лота = автовыдача по ней НЕ сработает (лот переименован "
            f"или отключен). Проверьте названия!</i>")
    kb = K(row_width=1)
    for name, cnt, has_file in items[offset:offset + PER_PAGE]:
        mark = "✅" if cnt else "⚠️"
        file_mark = "📦" if has_file else "💬"
        kb.add(B(f"{mark} {file_mark} {_short(name, 40)} — {cnt} лот.", None, CB_NOOP))
    pages = max(1, -(-len(items) // PER_PAGE))
    if pages > 1:
        row = []
        if offset > 0:
            row.append(B("⬅️", None, f"{CB_DIAG}:{offset - PER_PAGE}"))
        row.append(B(f"{offset // PER_PAGE + 1}/{pages}", None, CB_NOOP))
        if offset + PER_PAGE < len(items):
            row.append(B("➡️", None, f"{CB_DIAG}:{offset + PER_PAGE}"))
        kb.row(*row)
    kb.add(B("🏠 Главное меню", None, CB_HOME))
    return text, kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def sync_now(c: CallbackQuery) -> None:
            if not crd.account.is_initiated or getattr(crd, "profile", None) is None:
                _answer(bot, c, "Аккаунт/профиль FunPay еще не инициализированы. Попробуйте позже.",
                        alert=True)
                return
            _answer(bot, c, "🔄 Запустил синхронизацию — отчет придет в этот чат.", alert=True)
            threading.Thread(target=full_sync, args=(crd,),
                             kwargs={"report_to": c.message.chat.id}, daemon=True).start()

        def toggle_enabled(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["enabled"] = not settings["enabled"]
            _save_settings(settings)
            _answer(bot, c, f"🔄 Периодическая синхронизация: "
                            f"{'ВКЛ' if settings['enabled'] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def toggle_delivery(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["sync_on_delivery"] = not settings["sync_on_delivery"]
            _save_settings(settings)
            _answer(bot, c, f"📦 Синхронизация после выдачи: "
                            f"{'ВКЛ' if settings['sync_on_delivery'] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def open_diag(c: CallbackQuery) -> None:
            _answer(bot, c)
            offset = 0
            parts = str(c.data).split(":")
            if len(parts) > 2 and parts[2].isdigit():
                offset = int(parts[2])
            text, kb = _diag_kb(crd, offset)
            _edit_or_send(bot, c, text, kb)

        def act_interval(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"⏱ Введите интервал периодической синхронизации в МИНУТАХ "
                f"(<code>{INTERVAL_RANGE[0] // 60}</code> - <code>{INTERVAL_RANGE[1] // 60}</code>).\n\n"
                f"Текущее значение: <b>{s['interval'] // 60}</b> мин.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_INT)
            _answer(bot, c)

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def edit_interval(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                minutes = int(str(m.text).strip())
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число минут. Попробуйте еще раз.")
                return
            settings = _load_settings()
            settings["interval"] = _clamp(minutes * 60, *INTERVAL_RANGE)
            _save_settings(settings)
            bot.reply_to(m, f"✅ Интервал синхронизации: <b>{settings['interval'] // 60}</b> мин.",
                         parse_mode="HTML")

        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(sync_now, lambda c: c.data == CB_NOW)
        tg.cbq_handler(toggle_enabled, lambda c: c.data == CB_TOG)
        tg.cbq_handler(toggle_delivery, lambda c: c.data == CB_DELTOG)
        tg.cbq_handler(open_diag, lambda c: c.data.startswith(f"{CB_DIAG}:"))
        tg.cbq_handler(act_interval, lambda c: c.data == CB_INT)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_interval, func=lambda m: _state_eq(m, ST_INT))
        tg.msg_handler(cmd_open, commands=["sync"])

        try:
            crd.add_telegram_commands(UUID, [("sync", "Синхронизация «Наличия» лотов автовыдачи.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"StockSync v{VERSION}: Telegram-меню зарегистрировано (команда /sync).")
    else:
        logger.info(f"StockSync v{VERSION}: Telegram выключен — синхронизация работает в фоне.")

    threading.Thread(target=_sync_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_POST_DELIVERY = [post_delivery_handler]
BIND_TO_DELETE = on_delete
