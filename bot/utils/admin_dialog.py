"""Single-card transport helpers for administrator input dialogs."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)

_ADMIN_DIALOG_MESSAGE_ID_KEY = "_admin_dialog_message_id"


async def _remember_dialog_message(
    state: FSMContext,
    rendered: Message,
    fallback: Message,
) -> Message:
    message_id = getattr(rendered, "message_id", None)
    if message_id is None:
        message_id = getattr(fallback, "message_id", None)
    if message_id is not None:
        await state.update_data(**{_ADMIN_DIALOG_MESSAGE_ID_KEY: int(message_id)})
    else:
        logger.warning("Admin dialog render returned no message id")
    return rendered


async def render_admin_dialog(
    message: Message,
    state: FSMContext,
    text: str,
    *,
    reply_markup: Any = None,
    show_web_page_preview: bool = False,
) -> Message:
    """Render one administrator dialog card and remember its actual message id."""
    rendered = await safe_edit_or_send(
        message,
        text,
        reply_markup=reply_markup,
        show_web_page_preview=show_web_page_preview,
    )
    return await _remember_dialog_message(state, rendered, message)


def _is_missing_dialog_message_error(error: TelegramBadRequest) -> bool:
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "message to edit not found",
            "message not found",
            "message_id_invalid",
            "message identifier is not specified",
        )
    )


async def render_admin_dialog_from_input(
    message: Message,
    state: FSMContext,
    text: str,
    *,
    reply_markup: Any = None,
    show_web_page_preview: bool = False,
) -> Message:
    """Delete administrator input and redraw the dialog card it belongs to."""
    data = await state.get_data()
    stored_message_id = data.get(_ADMIN_DIALOG_MESSAGE_ID_KEY)
    target = None
    try:
        target_message_id = int(stored_message_id)
        if target_message_id != int(message.message_id):
            target = message.model_copy(update={"message_id": target_message_id})
    except (AttributeError, TypeError, ValueError):
        target = None

    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete administrator dialog input", exc_info=True)

    if target is not None:
        try:
            return await render_admin_dialog(
                target,
                state,
                text,
                reply_markup=reply_markup,
                show_web_page_preview=show_web_page_preview,
            )
        except TelegramBadRequest as error:
            if not _is_missing_dialog_message_error(error):
                raise
            logger.info(
                "Stored administrator dialog message is unavailable; sending one replacement"
            )
    else:
        logger.info(
            "Administrator dialog message id is missing; sending one replacement"
        )

    rendered = await safe_edit_or_send(
        message,
        text,
        reply_markup=reply_markup,
        show_web_page_preview=show_web_page_preview,
        force_new=True,
    )
    return await _remember_dialog_message(state, rendered, message)
