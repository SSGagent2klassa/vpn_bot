"""Administrator wizard for generating batches of one-time coupons."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.admin import promotion_cancel_kb
from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.admin_dialog import (
    render_admin_dialog,
    render_admin_dialog_from_input,
)
from bot.utils.text import get_message_text_for_storage
from database.requests import create_coupon_batch

router = Router()


def _coupon_generator_prompt(
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


@router.callback_query(F.data == "admin_coupons_generate")
async def admin_coupons_generate(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminStates.coupon_generate_discount)
    await render_admin_dialog(
        callback.message,
        state,
        _coupon_generator_prompt(
            "🎲 <b>Генератор купонов</b>",
            "Введите размер скидки от 0 до 100%.",
        ),
        reply_markup=promotion_cancel_kb("admin_coupons"),
    )
    await callback.answer()


@router.message(
    AdminStates.coupon_generate_discount,
    F.text,
    ~F.text.startswith("/"),
)
async def admin_coupons_generate_discount(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return
    raw = get_message_text_for_storage(message, "plain").strip()
    if not raw.isdigit() or not 0 <= int(raw) <= 100:
        await render_admin_dialog_from_input(
            message,
            state,
            _coupon_generator_prompt(
                "🎲 <b>Генератор купонов</b>",
                "Введите размер скидки от 0 до 100%.",
                error="Введите целое число от 0 до 100.",
            ),
            reply_markup=promotion_cancel_kb("admin_coupons"),
        )
        return
    await state.update_data(coupon_generate_discount=int(raw))
    await state.set_state(AdminStates.coupon_generate_lifetime)
    await render_admin_dialog_from_input(
        message,
        state,
        _coupon_generator_prompt(
            "⏳ <b>Срок жизни</b>",
            "Введите количество дней.",
        ),
        reply_markup=promotion_cancel_kb("admin_coupons"),
    )


@router.message(
    AdminStates.coupon_generate_lifetime,
    F.text,
    ~F.text.startswith("/"),
)
async def admin_coupons_generate_lifetime(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return
    raw = get_message_text_for_storage(message, "plain").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await render_admin_dialog_from_input(
            message,
            state,
            _coupon_generator_prompt(
                "⏳ <b>Срок жизни</b>",
                "Введите количество дней.",
                error="Введите целое число больше 0.",
            ),
            reply_markup=promotion_cancel_kb("admin_coupons"),
        )
        return
    await state.update_data(coupon_generate_lifetime=int(raw))
    await state.set_state(AdminStates.coupon_generate_count)
    await render_admin_dialog_from_input(
        message,
        state,
        _coupon_generator_prompt(
            "🔢 <b>Количество</b>",
            "Введите количество купонов. За один раз можно создать до 500.",
        ),
        reply_markup=promotion_cancel_kb("admin_coupons"),
    )


@router.message(
    AdminStates.coupon_generate_count,
    F.text,
    ~F.text.startswith("/"),
)
async def admin_coupons_generate_count(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return
    raw = get_message_text_for_storage(message, "plain").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 500:
        await render_admin_dialog_from_input(
            message,
            state,
            _coupon_generator_prompt(
                "🔢 <b>Количество</b>",
                "Введите количество купонов. За один раз можно создать до 500.",
                error="Введите целое число от 1 до 500.",
            ),
            reply_markup=promotion_cancel_kb("admin_coupons"),
        )
        return
    data = await state.get_data()
    target = await render_admin_dialog_from_input(
        message,
        state,
        "⏳ <b>Генерация купонов</b>\n\nСоздаю коды…",
    )
    coupons = create_coupon_batch(
        discount_percent=data["coupon_generate_discount"],
        lifetime_days=data["coupon_generate_lifetime"],
        count=int(raw),
        source="admin_generated",
        created_by_admin_id=message.from_user.id,
    )
    codes = "\n".join(coupon["code"] for coupon in coupons)
    text = (
        "✅ <b>Купоны сгенерированы</b>\n\n"
        f"Скидка: <b>{data['coupon_generate_discount']}%</b>\n"
        f"Срок жизни: <b>{data['coupon_generate_lifetime']} дн.</b>\n"
        f"Количество: <b>{len(coupons)}</b>\n\n"
        f"<pre>{html.escape(codes)}</pre>"
    )
    try:
        await render_admin_dialog(
            target,
            state,
            text,
            reply_markup=promotion_cancel_kb("admin_coupons"),
        )
    finally:
        await state.clear()
