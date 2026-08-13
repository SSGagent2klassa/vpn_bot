"""User-facing Yadreno Admin error messages."""
from __future__ import annotations

from bot.services.yadreno_admin import YadrenoAdminError
from bot.utils.telegram_links import build_telegram_link
from bot.utils.text import escape_html


def _is_api_key_error(error: YadrenoAdminError) -> bool:
    """Return True when the hub rejected the configured API key."""
    technical_message = str(error).casefold()
    return (
        error.kind == "authentication"
        or error.status_code in {401, 403}
        or "invalid api_key" in technical_message
    )


def yadreno_admin_error_alert(error: YadrenoAdminError) -> str:
    """Build a short plain-text error for a Telegram callback alert."""
    if error.kind == "configuration":
        return (
            "Ключ Yadreno Admin повреждён. "
            "Замените его в настройках Yadreno Admin."
        )
    if _is_api_key_error(error):
        return (
            "Текущий ключ Yadreno Admin больше не подходит. "
            "Если вы меняли сервер, выпустите новый ключ в @YadrenoAdmin_Bot."
        )
    if error.user_message:
        return error.user_message[:180]
    return (
        "Хаб Yadreno Admin временно недоступен. Возможно, идёт техническое "
        "обслуживание или обновление. Попробуйте ещё раз чуть позже."
    )


def format_yadreno_admin_error(
    error: YadrenoAdminError,
    *,
    title: str | None = None,
) -> str:
    """Build a safe, actionable Telegram HTML error without hub internals."""
    if error.kind == "configuration":
        default_title = "Ключ Yadreno Admin повреждён"
        icon = "❌"
        body = (
            "Сохранённый ключ имеет некорректный формат и не может быть "
            "отправлен в Yadreno Admin.\n\n"
            "Замените его в настройках Yadreno Admin."
        )
    elif _is_api_key_error(error):
        default_title = "Ключ Yadreno Admin не принят"
        icon = "❌"
        bot_link = build_telegram_link("YadrenoAdmin_Bot")
        body = (
            "Текущий ключ доступа больше не подходит.\n\n"
            "Возможно, вы меняли сервер. Выпустите новый ключ в "
            f'<a href="{bot_link}">@YadrenoAdmin_Bot</a>, затем замените его '
            "в настройках Yadreno Admin."
        )
    elif error.kind == "maintenance":
        default_title = "Хаб на техническом обслуживании"
        icon = "⏳"
        body = escape_html(
            error.user_message
            or "Сервис временно на обслуживании. Попробуйте снова чуть позже."
        )
    elif error.kind == "service_unavailable":
        default_title = "Хаб временно недоступен"
        icon = "⏳"
        body = escape_html(
            error.user_message
            or "Возможно, идёт техническое обслуживание или обновление. "
            "Попробуйте снова через несколько минут."
        )
    elif error.user_message:
        default_title = "Запрос не выполнен"
        icon = "⚠️"
        body = escape_html(error.user_message)
    else:
        default_title = "Хаб временно недоступен"
        icon = "⏳"
        body = (
            "Сервис временно не отвечает. Возможно, на хабе идёт техническое "
            "обслуживание или обновление. Попробуйте ещё раз чуть позже."
        )

    return f"{icon} <b>{escape_html(title or default_title)}</b>\n\n{body}"
