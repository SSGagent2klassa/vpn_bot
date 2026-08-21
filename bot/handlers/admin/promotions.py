import datetime
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.admin import (
    promocode_detail_kb,
    promocodes_list_kb,
    promotion_cancel_kb,
)
from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.admin_dialog import (
    render_admin_dialog,
    render_admin_dialog_from_input,
)
from bot.utils.telegram_links import build_telegram_link, get_telegram_link_domain
from bot.utils.text import escape_html, get_message_text_for_storage, safe_edit_or_send
from database.requests import (
    create_promo_code,
    get_promo_code_by_code,
    get_promo_code_by_id,
    get_promo_codes,
    is_base62_code,
    set_promo_code_active,
    update_promo_code,
)

logger = logging.getLogger(__name__)
router = Router()


def _format_date(value) -> str:
    return value or "без срока"


def _parse_expires(value: str):
    value = value.strip()
    if value in {"0", "-", "нет", "Нет"}:
        return None
    dt = datetime.datetime.strptime(value, "%Y-%m-%d")
    return dt.replace(hour=23, minute=59, second=59)


def _promocode_add_prompt(
    title: str,
    prompt: str,
    *,
    error: str | None = None,
) -> str:
    parts = [title]
    if error:
        parts.append(f"❌ {error}")
    parts.append(prompt)
    return "\n\n".join(parts)


async def _delete_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


def _promocode_text(promo: dict, bot_username: str | None = None) -> str:
    limit = promo.get("activation_limit")
    limit_text = str(limit) if limit else "без лимита"
    usage = int(promo.get("usage_count") or 0)
    status = "включён" if promo.get("is_active") else "выключен"
    link_line = ""
    if bot_username:
        promo_link = build_telegram_link(bot_username, f"pr_{promo['code']}")
        link_line = f"\n🔗 Промо-ссылка: <code>{escape_html(promo_link)}</code>"
    return (
        "🎟 <b>Промокод</b>\n\n"
        f"Код: <b>{escape_html(promo['code'])}</b>\n"
        f"Скидка: <b>{int(promo.get('discount_percent') or 0)}%</b>\n"
        f"Срок: <b>{escape_html(_format_date(promo.get('expires_at')))}</b>\n"
        f"Лимит: <b>{escape_html(limit_text)}</b>\n"
        f"Активаций: <b>{usage}</b>\n"
        f"Статус: <b>{status}</b>"
        f"{link_line}\n\n"
        "Промокод можно вводить в поле оплаты или использовать как промо-ссылку."
    )


@router.callback_query(F.data == "admin_promocodes")
async def admin_promocodes(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminStates.admin_menu)
    promocodes = get_promo_codes("promo")
    text = (
        "🎟 <b>Промокоды</b>\n\n"
        "Промокоды многоразовые для разных клиентов, но каждый пользователь может применить один и тот же код только один раз. "
        "Их можно вводить вручную при оплате, а ещё каждый промокод работает как промо-ссылка формата "
        f"<code>https://{escape_html(get_telegram_link_domain())}/&lt;bot&gt;?start=pr_CODE</code>.\n\n"
        "Промо-ссылки удобно использовать в рекламе и партнёрских размещениях: бот сохранит код пользователю, а успешная покупка попадёт в аналитику."
    )
    await safe_edit_or_send(callback.message, text, reply_markup=promocodes_list_kb(promocodes))
    await callback.answer()


@router.callback_query(F.data == "admin_promocode_add")
async def admin_promocode_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.promocode_add_code)
    await render_admin_dialog(
        callback.message,
        state,
        _promocode_add_prompt(
            "➕ <b>Новый промокод</b>",
            "Введите имя промокода. Можно использовать только <code>0-9</code>, "
            "<code>A-Z</code>, <code>a-z</code>.",
        ),
        reply_markup=promotion_cancel_kb("admin_promocodes"),
    )
    await callback.answer()


@router.message(AdminStates.promocode_add_code, F.text, ~F.text.startswith("/"))
async def promocode_add_code(message: Message, state: FSMContext):
    code = get_message_text_for_storage(message, "plain").strip()
    if not is_base62_code(code):
        await render_admin_dialog_from_input(
            message,
            state,
            _promocode_add_prompt(
                "➕ <b>Новый промокод</b>",
                "Введите имя промокода. Можно использовать только <code>0-9</code>, "
                "<code>A-Z</code>, <code>a-z</code>.",
                error=(
                    "Код должен быть в base62: <code>0-9</code>, "
                    "<code>A-Z</code>, <code>a-z</code>."
                ),
            ),
            reply_markup=promotion_cancel_kb("admin_promocodes"),
        )
        return
    if get_promo_code_by_code(code):
        await render_admin_dialog_from_input(
            message,
            state,
            _promocode_add_prompt(
                "➕ <b>Новый промокод</b>",
                "Введите другое имя промокода.",
                error="Такой код уже существует.",
            ),
            reply_markup=promotion_cancel_kb("admin_promocodes"),
        )
        return
    await state.update_data(promocode_code=code)
    await state.set_state(AdminStates.promocode_add_discount)
    await render_admin_dialog_from_input(
        message,
        state,
        _promocode_add_prompt(
            "📊 <b>Скидка</b>",
            "Введите размер скидки от 0 до 100%.",
        ),
        reply_markup=promotion_cancel_kb("admin_promocodes"),
    )


@router.message(AdminStates.promocode_add_discount, F.text, ~F.text.startswith("/"))
async def promocode_add_discount(message: Message, state: FSMContext):
    value = get_message_text_for_storage(message, "plain").strip()
    if not value.isdigit() or not 0 <= int(value) <= 100:
        await render_admin_dialog_from_input(
            message,
            state,
            _promocode_add_prompt(
                "📊 <b>Скидка</b>",
                "Введите размер скидки от 0 до 100%.",
                error="Введите целое число от 0 до 100.",
            ),
            reply_markup=promotion_cancel_kb("admin_promocodes"),
        )
        return
    await state.update_data(promocode_discount=int(value))
    await state.set_state(AdminStates.promocode_add_expires)
    await render_admin_dialog_from_input(
        message,
        state,
        _promocode_add_prompt(
            "⏳ <b>Срок действия</b>",
            "Введите дату в формате <code>YYYY-MM-DD</code> или <code>0</code>, "
            "если срок не ограничен.",
        ),
        reply_markup=promotion_cancel_kb("admin_promocodes"),
    )


@router.message(AdminStates.promocode_add_expires, F.text, ~F.text.startswith("/"))
async def promocode_add_expires(message: Message, state: FSMContext):
    raw = get_message_text_for_storage(message, "plain").strip()
    try:
        expires_at = _parse_expires(raw)
    except ValueError:
        await render_admin_dialog_from_input(
            message,
            state,
            _promocode_add_prompt(
                "⏳ <b>Срок действия</b>",
                "Введите дату в формате <code>YYYY-MM-DD</code> или <code>0</code>, "
                "если срок не ограничен.",
                error="Неверная дата.",
            ),
            reply_markup=promotion_cancel_kb("admin_promocodes"),
        )
        return
    await state.update_data(promocode_expires=expires_at)
    await state.set_state(AdminStates.promocode_add_limit)
    await render_admin_dialog_from_input(
        message,
        state,
        "🔢 <b>Лимит активаций</b>\n\n"
        "Введите общее количество применений разными пользователями или <code>0</code> без общего лимита. "
        "Один пользователь в любом случае может применить этот промокод только один раз.",
        reply_markup=promotion_cancel_kb("admin_promocodes"),
    )


@router.message(AdminStates.promocode_add_limit, F.text, ~F.text.startswith("/"))
async def promocode_add_limit(message: Message, state: FSMContext):
    raw = get_message_text_for_storage(message, "plain").strip()
    if not raw.isdigit():
        await render_admin_dialog_from_input(
            message,
            state,
            _promocode_add_prompt(
                "🔢 <b>Лимит активаций</b>",
                "Введите общее количество применений разными пользователями или "
                "<code>0</code> без общего лимита.",
                error="Введите целое неотрицательное число.",
            ),
            reply_markup=promotion_cancel_kb("admin_promocodes"),
        )
        return
    data = await state.get_data()
    target = await render_admin_dialog_from_input(
        message,
        state,
        "⏳ <b>Создание промокода</b>\n\nСохраняю введённые параметры…",
    )
    promo_id = create_promo_code(
        code=data["promocode_code"],
        discount_percent=data["promocode_discount"],
        expires_at=data.get("promocode_expires"),
        activation_limit=int(raw),
        created_by_admin_id=message.from_user.id,
        source="admin",
        code_type="promo",
    )
    try:
        promo = get_promo_code_by_id(promo_id)
        bot_info = await message.bot.get_me()
        await render_admin_dialog(
            target,
            state,
            _promocode_text(promo, bot_info.username),
            reply_markup=promocode_detail_kb(promo),
        )
    finally:
        await state.clear()

@router.callback_query(F.data.startswith("admin_promocode_view:"))
async def admin_promocode_view(callback: CallbackQuery, state: FSMContext):
    promo_id = int(callback.data.split(":")[1])
    promo = get_promo_code_by_id(promo_id)
    if not promo:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    bot_info = await callback.bot.get_me()
    await safe_edit_or_send(callback.message, _promocode_text(promo, bot_info.username), reply_markup=promocode_detail_kb(promo))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_promocode_toggle:"))
async def admin_promocode_toggle(callback: CallbackQuery):
    promo_id = int(callback.data.split(":")[1])
    promo = get_promo_code_by_id(promo_id)
    if not promo:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    set_promo_code_active(promo_id, not bool(promo.get("is_active")))
    promo = get_promo_code_by_id(promo_id)
    bot_info = await callback.bot.get_me()
    await safe_edit_or_send(callback.message, _promocode_text(promo, bot_info.username), reply_markup=promocode_detail_kb(promo))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_promocode_edit_"))
async def admin_promocode_edit_start(callback: CallbackQuery, state: FSMContext):
    action, promo_id = callback.data.rsplit(":", 1)
    field = action.replace("admin_promocode_edit_", "")
    await state.update_data(promocode_edit_id=int(promo_id), promocode_edit_field=field)
    await state.set_state(AdminStates.promocode_edit_value)
    hints = {
        "discount": "Введите скидку от 0 до 100%.",
        "expires": "Введите дату <code>YYYY-MM-DD</code> или <code>0</code> без срока.",
        "limit": "Введите лимит применений или <code>0</code> без лимита.",
    }
    await safe_edit_or_send(callback.message, f"✏️ <b>Редактирование</b>\n\n{hints[field]}", reply_markup=promotion_cancel_kb(f"admin_promocode_view:{promo_id}"))
    await callback.answer()


@router.message(AdminStates.promocode_edit_value, F.text, ~F.text.startswith("/"))
async def admin_promocode_edit_value(message: Message, state: FSMContext):
    await _delete_input(message)
    data = await state.get_data()
    promo_id = int(data["promocode_edit_id"])
    field = data["promocode_edit_field"]
    raw = get_message_text_for_storage(message, "plain").strip()
    try:
        if field == "discount":
            if not raw.isdigit() or not 0 <= int(raw) <= 100:
                raise ValueError()
            update_promo_code(promo_id, discount_percent=int(raw))
        elif field == "expires":
            update_promo_code(promo_id, expires_at=_parse_expires(raw))
        elif field == "limit":
            if not raw.isdigit():
                raise ValueError()
            update_promo_code(promo_id, activation_limit=int(raw))
    except ValueError:
        await safe_edit_or_send(message, "❌ Значение не принято. Проверьте формат и попробуйте ещё раз.", reply_markup=promotion_cancel_kb(f"admin_promocode_view:{promo_id}"), force_new=True)
        return
    await state.clear()
    promo = get_promo_code_by_id(promo_id)
    bot_info = await message.bot.get_me()
    await safe_edit_or_send(message, _promocode_text(promo, bot_info.username), reply_markup=promocode_detail_kb(promo), force_new=True)
