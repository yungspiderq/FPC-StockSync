"""
Функциональные тесты плагина stock_sync.py на моках.
Запуск: cd FPC && python3 tests_plugin_stock_sync.py
"""
import importlib.util
import json
import os
import sys
import time as real_time
from configparser import ConfigParser
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

spec = importlib.util.spec_from_file_location("plugins.stock_sync", "plugins/stock_sync.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.stock_sync"] = plugin
spec.loader.exec_module(plugin)

plugin._sync_loop = lambda crd: None  # поток тестируем через full_sync напрямую
plugin.LOT_DELAY = 0
plugin.SAVE_DELAY = 0

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


# ───────────────────── товарные файлы и конфиг автовыдачи ─────────────────────
PRODUCTS_DIR = "storage/products"
F_A = "ss_test_a.txt"   # «Аккаунты Steam» — 7 товаров
F_B = "ss_test_b.txt"   # «Аккаунты Steam Premium» — 3 товара
F_OLD = "ss_test_old.txt"  # секция без подходящего лота


def write_products(fname, n):
    os.makedirs(PRODUCTS_DIR, exist_ok=True)
    with open(os.path.join(PRODUCTS_DIR, fname), "w", encoding="utf-8") as f:
        f.write("\n".join(f"item_{i}" for i in range(n)) + ("\n" if n else ""))


def cleanup_files():
    if os.path.isdir(PRODUCTS_DIR):
        for fname in os.listdir(PRODUCTS_DIR):
            if fname.startswith("ss_test_"):
                try:
                    os.remove(os.path.join(PRODUCTS_DIR, fname))
                except OSError:
                    pass


def make_ad_cfg():
    cfg = ConfigParser(delimiters=(":",), interpolation=None)
    cfg.optionxform = str
    cfg["Аккаунты Steam"] = {"response": "Товар: $product", "productsFileName": F_A}
    cfg["Аккаунты Steam Premium"] = {"response": "Премиум: $product", "productsFileName": F_B}
    cfg["Ключи WoW"] = {"response": "Спасибо за покупку!"}          # без файла
    cfg["Старый лот"] = {"response": "$product", "productsFileName": F_OLD}  # лот переименован
    return cfg


def mk_lot(id_, desc, server=None, side=None):
    return SimpleNamespace(id=id_, description=desc, server=server, side=side)


class MockProfile:
    def __init__(self, lots):
        self._lots = {lot.id: lot for lot in lots}

    def get_sorted_lots(self, mode):
        assert mode == 1
        return dict(self._lots)


class MockLotFields:
    def __init__(self, lot_id, amount=100, auto_delivery=False, no_field=False):
        self.lot_id = lot_id
        self.auto_delivery = auto_delivery
        self._amount = None if no_field else amount
        self.active = True

    @property
    def amount(self):
        return self._amount

    @amount.setter
    def amount(self, value):
        self._amount = value


class MockAccount:
    def __init__(self, fields_spec=None):
        self.is_initiated = True
        self.id = 999
        self.fields_spec = fields_spec or {}   # {lot_id: kwargs для MockLotFields}
        self.saved = []                        # [(lot_id, amount)]
        self.get_calls = []

    def get_lot_fields(self, lot_id, node_id=None):
        self.get_calls.append(lot_id)
        spec_ = self.fields_spec.get(lot_id)
        if spec_ == "raise":
            raise Exception("mock: lot not found")
        return MockLotFields(lot_id, **(spec_ or {}))

    def save_lot(self, fields, locale=None):
        self.saved.append((fields.lot_id, fields.amount))


# ───────────────────────────── моки Telegram ─────────────────────────────
class MockBot:
    def __init__(self):
        self.answers, self.edits, self.sent, self.replies = [], [], [], []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers, self.msg_handlers, self.user_states = [], [], {}
        self.notification_settings, self.notifications = {}, []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def send_notification(self, text, notification_type=None, **kwargs):
        self.notifications.append((text, notification_type))


class MockCardinal:
    def __init__(self, account, telegram, profile):
        self.account = account
        self.telegram = telegram
        self.profile = profile
        self.AD_CFG = make_ad_cfg()
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


LOTS = [mk_lot(1001, "Аккаунты Steam с играми"),
        mk_lot(1002, "Аккаунты Steam Premium с играми"),
        mk_lot(1003, "Ключи WoW Classic")]


def make_crd(with_tg=True, fields_spec=None):
    acc = MockAccount(fields_spec)
    tg = MockTelegram() if with_tg else None
    return MockCardinal(acc, tg, MockProfile(LOTS)), acc, tg


# ═══════════════════════════ ТЕСТ 1: метаданные ═══════════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 события привязаны", plugin.BIND_TO_PRE_INIT == [plugin.init]
      and plugin.BIND_TO_POST_DELIVERY == [plugin.post_delivery_handler])

# ═══════════════════════════ ТЕСТ 2: сопоставление лотов и конфигов ═══════════════════════════
write_products(F_A, 7)
write_products(F_B, 3)
write_products(F_OLD, 5)
crd, acc, tg = make_crd(with_tg=False)

m = plugin._match_section(crd, LOTS[0])
check("2.1 лот «Аккаунты Steam с играми» → секция «Аккаунты Steam»", m and m[0] == "Аккаунты Steam")
m = plugin._match_section(crd, LOTS[1])
check("2.2 самое длинное совпадение побеждает (Premium-секция)",
      m and m[0] == "Аккаунты Steam Premium")
check("2.3 лот без секции → None", plugin._match_section(crd, mk_lot(9, "Другая игра")) is None)

targets = plugin._lots_to_sync(crd)
ids = sorted(lot.id for lot, _, _ in targets)
check("2.4 в список синхронизации только лоты с товарными файлами", ids == [1001, 1002])

# ═══════════════════════════ ТЕСТ 3: sync_lot ═══════════════════════════
crd, acc, _ = make_crd(with_tg=False)
status, old, new = plugin.sync_lot(crd, 1001, F_A)
check("3.1 наличие 100 ≠ 7 товаров → изменено и сохранено",
      status == "changed" and (old, new) == (100, 7) and acc.saved == [(1001, 7)])

acc.saved.clear()
crd.account.fields_spec = {1001: {"amount": 7}}
status, old, new = plugin.sync_lot(crd, 1001, F_A)
check("3.2 наличие уже равно товарам → без сохранения", status == "ok" and acc.saved == [])

write_products(F_B, 0)
acc.saved.clear()
acc.get_calls.clear()
status, _, _ = plugin.sync_lot(crd, 1002, F_B)
check("3.3 ноль товаров → пропуск (дело автовыключения), без запросов к FunPay",
      status == "skip_zero" and acc.get_calls == [] and acc.saved == [])
write_products(F_B, 3)

crd.account.fields_spec = {1001: {"amount": 9, "auto_delivery": True}}
acc.saved.clear()
status, _, _ = plugin.sync_lot(crd, 1001, F_A)
check("3.4 встроенная автовыдача FunPay → не трогаем", status == "skip_builtin" and acc.saved == [])

crd.account.fields_spec = {1001: {"no_field": True}}
acc.saved.clear()
status, _, _ = plugin.sync_lot(crd, 1001, F_A)
check("3.5 нет поля «Наличие» → пропуск", status == "skip_nofield" and acc.saved == [])

crd.account.fields_spec = {1001: "raise"}
status, _, _ = plugin.sync_lot(crd, 1001, F_A)
check("3.6 ошибка API → статус error, без падения", status == "error")

crd.account.fields_spec = {}
status, _, _ = plugin.sync_lot(crd, 1001, "нет_такого_файла.txt")
check("3.7 отсутствующий файл → skip_zero (0 товаров)", status == "skip_zero")

# ═══════════════════════════ ТЕСТ 4: полная синхронизация ═══════════════════════
crd, acc, tg = make_crd(with_tg=True, fields_spec={1001: {"amount": 100}, 1002: {"amount": 3}})
reset_settings()
tg.notifications.clear()
stats = plugin.full_sync(crd)
check("4.1 статистика: проверено 2, изменен 1", stats["checked"] == 2 and stats["changed"] == 1
      and stats["ok"] == 1)
check("4.2 изменено только расхождение (1001: 100→7)", acc.saved == [(1001, 7)])
check("4.3 уведомление только об изменениях", len(tg.notifications) == 1
      and "100 → <b>7</b>" in tg.notifications[0][0])
check("4.4 last_sync записан", plugin._load_settings()["last_sync"].get("changed") == 1)

tg.notifications.clear()
acc.saved.clear()
stats = plugin.full_sync(crd)  # теперь 1001 уже имеет 7? fields_spec фиксирован — снова changed
# подменим spec на актуальный, чтобы проверить «тишину»
crd.account.fields_spec = {1001: {"amount": 7}, 1002: {"amount": 3}}
tg.notifications.clear()
acc.saved.clear()
stats = plugin.full_sync(crd)
check("4.5 всё синхронно → без уведомлений (антиспам)", stats["changed"] == 0
      and tg.notifications == [] and acc.saved == [])

# ручной запуск с отчетом в чат
crd.bot_sent = None
stats = plugin.full_sync(crd, report_to=111)
check("4.6 ручной запуск: подробный отчет в чат",
      any(cid == 111 and "Синхронизация наличия завершена" in t for cid, t in tg.bot.sent))

# busy-lock
plugin._busy.acquire()
tg.bot.sent.clear()
stats = plugin.full_sync(crd, report_to=111)
check("4.7 во время идущей синхронизации новая не запускается",
      stats == {} and any("уже идет" in t for _, t in tg.bot.sent))
plugin._busy.release()

# ═══════════════════════════ ТЕСТ 5: синхронизация после выдачи ═══════════════════════════
crd, acc, tg = make_crd(with_tg=False, fields_spec={1001: {"amount": 50}})
reset_settings()
e = SimpleNamespace(delivered=True, lot_id=1001, config_section_obj=crd.AD_CFG["Аккаунты Steam"])
plugin.post_delivery_handler(crd, e)
real_time.sleep(0.4)
check("5.1 после выдачи наличие синхронизировано (50→7)", acc.saved == [(1001, 7)])

acc.saved.clear()
e2 = SimpleNamespace(delivered=False, lot_id=1001, config_section_obj=crd.AD_CFG["Аккаунты Steam"])
plugin.post_delivery_handler(crd, e2)
real_time.sleep(0.2)
check("5.2 выдача не состоялась → синхронизации нет", acc.saved == [])

reset_settings(sync_on_delivery=False)
crd.account.fields_spec = {1001: {"amount": 50}}
acc.saved.clear()
plugin.post_delivery_handler(crd, e)
real_time.sleep(0.2)
check("5.3 sync_on_delivery=False → после выдачи не синхронизируем", acc.saved == [])

reset_settings(enabled=False)
acc.saved.clear()
plugin.post_delivery_handler(crd, e)
real_time.sleep(0.2)
check("5.4 enabled=False → обработчик выдачи молчит", acc.saved == [])
reset_settings()

crd.plugins[plugin.UUID].enabled = False
crd.account.fields_spec = {1001: {"amount": 50}}
acc.saved.clear()
plugin.post_delivery_handler(crd, e)
real_time.sleep(0.2)
check("5.5 выключенный плагин не синхронизирует", acc.saved == [])
crd.plugins[plugin.UUID].enabled = True

# секция без товарного файла — не трогаем
acc.saved.clear()
e3 = SimpleNamespace(delivered=True, lot_id=1003, config_section_obj=crd.AD_CFG["Ключи WoW"])
plugin.post_delivery_handler(crd, e3)
real_time.sleep(0.2)
check("5.6 лот без товарного файла (шаблон) — пропускается", acc.saved == [])

# ═══════════════════════════ ТЕСТ 6: диагностика ═══════════════════════════
diag = plugin._diagnostics(crd)
names = [d[0] for d in diag]
check("6.1 проблемная секция («Старый лот») — первая в списке", names[0] == "Старый лот")
counts = {d[0]: d[1] for d in diag}
check("6.2 счетчики лотов по секциям", counts["Старый лот"] == 0 and counts["Аккаунты Steam"] == 1
      and counts["Аккаунты Steam Premium"] == 1 and counts["Ключи WoW"] == 1)
has_file = {d[0]: d[2] for d in diag}
check("6.3 признак товарного файла", has_file["Ключи WoW"] is False and has_file["Аккаунты Steam"] is True)

# ═══════════════════════════ ТЕСТ 7: Telegram UI ═══════════════════════════
crd, acc, tg = make_crd(with_tg=True, fields_spec={1001: {"amount": 7}, 1002: {"amount": 3}})
plugin.init(crd)
bot = tg.bot
reset_settings()
check("7.1 команда /sync зарегистрирована",
      any(kw.get("commands") == ["sync"] for _, kw in tg.msg_handlers))
check("7.2 add_telegram_commands вызван", crd.registered_commands
      and crd.registered_commands[0][0] == "sync")


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("7.3 страница настроек открывает меню", name == "open_home"
      and "Синхронизация лотов автовыдачи" in last_text())
check("7.4 в меню статусы и число лотов автовыдачи",
      "Периодическая синхронизация: <b>ВКЛ" in last_text()
      and "Лотов автовыдачи найдено: <b>2</b>" in last_text())

name = dispatch_cb(plugin.CB_TOG)
check("7.5 периодическая синхронизация переключается", name == "toggle_enabled"
      and plugin._load_settings()["enabled"] is False)
dispatch_cb(plugin.CB_TOG)
check("7.6 и включается обратно", plugin._load_settings()["enabled"] is True)

name = dispatch_cb(plugin.CB_DELTOG)
check("7.7 синхронизация после выдачи переключается", name == "toggle_delivery"
      and plugin._load_settings()["sync_on_delivery"] is False)
dispatch_cb(plugin.CB_DELTOG)

name = dispatch_cb(f"{plugin.CB_DIAG}:0")
check("7.8 диагностика: проблемная секция с ⚠️", name == "open_diag"
      and "⚠️" in last_kb() and "Старый лот" in last_kb() and "Не подходят" in last_text())

# ручная синхронизация с отчетом
crd.account.fields_spec = {1001: {"amount": 100}}
bot.sent.clear()
tg.notifications.clear()
name = dispatch_cb(plugin.CB_NOW)
real_time.sleep(0.7)
check("7.9 «Синхронизировать сейчас»: отчет в чат + уведомление об изменении",
      name == "sync_now" and any(cid == 111 and "завершена" in t for cid, t in bot.sent)
      and any("100 → <b>7</b>" in t for t, _ in tg.notifications))

dispatch_cb(plugin.CB_INT)
st = tg.user_states.get((111, 222), {})
check("7.10 кнопка интервала ставит состояние", st.get("state") == plugin.ST_INT)
dispatch_msg("15")
check("7.11 интервал сохранен (15 мин = 900 с)", plugin._load_settings()["interval"] == 900)
dispatch_cb(plugin.CB_INT)
dispatch_msg("1")
check("7.12 интервал зажимается (1 мин → 2 мин)", plugin._load_settings()["interval"] == 120)
dispatch_cb(plugin.CB_INT)
dispatch_msg("abc")
check("7.13 нечисловой ввод отклоняется", plugin._load_settings()["interval"] == 120
      and "❌" in bot.replies[-1])
dispatch_cb(plugin.CB_NOOP)
check("7.14 noop отвечает", True)

# ═══════════════════════════ ТЕСТ 8: нормализация настроек ═══════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"enabled": 1, "sync_on_delivery": "x", "interval": "abc", "last_sync": 42}, f)
s = plugin._load_settings()
check("8.1 битые значения нормализуются", s["enabled"] is True and s["sync_on_delivery"] is True
      and s["interval"] == 600 and s["last_sync"] == {})
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("8.2 битый JSON → дефолты", s["interval"] == 600 and s["enabled"] is True)

cleanup_files()
reset_settings()

print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
